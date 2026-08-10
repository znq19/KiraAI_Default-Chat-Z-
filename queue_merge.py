"""队列合并 / 积压处理调度器（v2.3）

设计要点（对齐方案文档 v2.3）：
- 只在"当前批次（in-flight）处理中"时拦截后续批次进 pending；
- 推送决策三分支（都在 in-flight 完成时执行，串行）：
    分支① 软合并：pending 消息总数 <= 软合并上限 且 <= 合并消息数上限 且估 token <= 上限 -> 提前合并（不等超时）
    分支② 超时合并：pending 攒批时间 >= max_merge_seconds -> 合并（无论消息数，超限拆批）；
                   =0 时恒成立（不攒批，当前批次完成即全量合并，拆批由各上限控制）
    分支③ 独立推送：都不满足 -> 只推第一个批次（1:1），其余留 pending 等下一轮
- 用"事件配对"判定 in-flight 完成（0 延迟，无 release_delay）：
    ON_LLM_RESPONSE 无 tool_calls = 最后一步（文本收尾）-> 标记 _final_marked
    ON_STEP_RESULT（消息发送后触发）同 event_id 且已标记 -> 执行推送决策
- 自拦截防护：自己推送的（合并/重放）批次在 on_batch_message 直接放行，防死循环
- 积压媒体限制（media_preprocess_enabled + media_preprocess_max_batches）：
    含媒体批次在 pending 已满上限时直接放行独立处理，避免媒体无限积压 + VLM/STT 重复预处理
- 调试日志开关（debug_log_enabled）：开启后打印放行/拦截/三分支/拆批等状态，便于排查
- 合并批次必须沿用原 KiraIMMessage 引用（并行识图 _pir_images 依赖，绝不克隆）
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

from core.plugin import logger
from core.chat.message_utils import KiraMessageBatchEvent
from core.chat.message_elements import Image, Sticker, Record
from core.provider import LLMResponse


@dataclass
class PendingBatch:
    """待推送批次（记录进入 pending 的时刻，用于超时合并判定）"""
    arrival_ts: float
    batch: KiraMessageBatchEvent


class BatchMergeScheduler:
    """队列合并调度器：作为 mixin 组件挂在聊天插件上，不引用插件私有状态。"""

    def __init__(self, ctx, plugin_cfg: dict, bot_cfg: dict):
        self.ctx = ctx
        sec = plugin_cfg.get("section_queue_merge", {})
        self.enabled = sec.get("enabled", True)
        self.max_merge_seconds = float(sec.get("max_merge_seconds", 0))
        self.max_merge_batches_limit = int(sec.get("max_merge_batches_limit", 0))
        self.max_merge_messages = int(sec.get("max_merge_messages", -1))
        self.max_merge_est_tokens = int(sec.get("max_merge_est_tokens", 0))
        self.token_est_ratio = float(sec.get("token_est_ratio", 2.0))
        self.short_merge_max_messages = int(sec.get("short_merge_max_messages", -1))
        self.media_preprocess_enabled = sec.get("media_preprocess_enabled", True)
        self.media_preprocess_max_batches = int(sec.get("media_preprocess_max_batches", 0))
        self.debug_log_enabled = sec.get("debug_log_enabled", False)
        # in-flight 卡死兜底：从"最后一次 LLM 响应活动"起超过该时长仍无动静，才强制推送
        # 积压批次（防"批次被拦截但收尾事件永远不来"导致的会话队列死锁）。
        # 说明：批次一旦放行就走原版同一管线，LLM 挂起由框架 httpx 超时兜底（错误响应仍会
        # 触发收尾事件、自动释放 pending），本兜底只覆盖"根本没有收尾事件"的异常路径
        # （非 provider 异常任务崩溃 / 被其它插件 stop / 任务被取消）。
        # 阈值 = LLM 超时 + 工具执行超时：两次 LLM 活动之间最多夹一轮工具执行（无心跳），
        # 故余量取 tool_call_timeout 而非写死，用户改了配置也能自动对齐。
        # 配置：-1=自动（默认 LLM timeout + tool_call_timeout，默认值）；0=不设置（关闭兜底，
        #       完全信任事件配对，LLM 挂起由框架 httpx 超时兜底）；正整数=手动覆盖
        stall_cfg = float(sec.get("inflight_stall_timeout", -1))
        llm_timeout = 120.0
        tool_timeout = 60.0
        try:
            pm = getattr(ctx, "provider_mgr", None)
            if pm is not None and hasattr(pm, "get_default_llm"):
                client = pm.get_default_llm()
                if client is not None:
                    mc = getattr(client.model, "model_config", None) or {}
                    llm_timeout = float(mc.get("timeout", 120) or 120)
            tc = ctx.config.get_config("bot_config.agent.tool_call_timeout")
            if tc:
                tool_timeout = float(tc)
        except Exception:
            pass
        if stall_cfg == 0:
            self.inflight_stall_timeout = 0.0  # 关闭兜底
        elif stall_cfg > 0:
            self.inflight_stall_timeout = stall_cfg
        else:
            self.inflight_stall_timeout = llm_timeout + tool_timeout  # -1 自动

        # -1 自动解析
        buffer_cap = int(bot_cfg.get("max_buffer_messages", 5))
        recv_unmentioned = plugin_cfg.get("receive_unmentioned", False)
        unmentioned_cap = int(plugin_cfg.get("max_unmentioned_messages", 5)) if recv_unmentioned else 0
        if self.max_merge_messages == -1:
            # 自动 = (未提及消息缓冲上限 + 最大缓冲消息数) × 3；未提及关闭则不计前者
            self.max_merge_messages = (unmentioned_cap + buffer_cap) * 3
        if self.short_merge_max_messages == -1:
            # 软合并消息数上限默认继承 bot 的 max_buffer_messages
            self.short_merge_max_messages = buffer_cap

        # per-sid 状态
        self._inflight: dict[str, str] = {}          # sid -> event_id（当前正在处理的批次）
        self._inflight_since: dict[str, float] = {}  # sid -> 最近一次 LLM 活动时间（卡死兜底用）
        self._final_marked: set[str] = set()         # 该 sid 的 in-flight 批次已进入最后一步
        self._pending: dict[str, list[PendingBatch]] = {}   # sid -> 待推送队列
        self._lock = asyncio.Lock()
        self._merge_task: Optional[asyncio.Task] = None

    # ================= 调试日志 =================

    def _log(self, sid: str, msg: str):
        """debug_log_enabled 开启时打印队列合并状态日志（info 级别，便于排查）。"""
        if self.debug_log_enabled:
            logger.info(f"[QueueMerge] {sid} {msg}")

    # ================= 钩子（由宿主插件的 @on.xxx 转发调用） =================

    async def on_batch_message(self, event: KiraMessageBatchEvent, *_):
        """ON_IM_BATCH_MESSAGE：主入口，放行 or 拦截入队。"""
        if not self.enabled:
            return
        sid = event.session.sid
        async with self._lock:
            if self._inflight.get(sid) == event.event_id:
                # 自己推送的（合并/重放）批次：in-flight 就是它自己，直接放行，
                # 防止"推送 -> 到达 -> 拦截自己 -> 超时再推 -> 再拦截"的死循环
                return
            if event.extra and event.extra.get("_qm_self"):
                # 自发布批次兜底：即使 _inflight 被并发路径误清（见 _push_pending 注释），
                # 带自发布标记的批次也绝对放行，并恢复 inflight 跟踪，防状态悬空
                if self._inflight.get(sid) != event.event_id:
                    self._inflight[sid] = event.event_id
                    self._inflight_since[sid] = time.time()
                return
            if sid in self._inflight or self._pending.get(sid):
                # 积压媒体限制：pending 中已积压的含媒体批次达到上限时，
                # 新到的含媒体批次直接放行独立处理（媒体及时识别，不无限积压/重复 VLM）
                if (self.media_preprocess_enabled and self.media_preprocess_max_batches > 0
                        and self._has_media(PendingBatch(time.time(), event))
                        and self._count_media_batches(self._pending.get(sid, [])) >= self.media_preprocess_max_batches):
                    self._log(sid, f"媒体批次超积压上限({self.media_preprocess_max_batches})，直接放行独立处理 {event.event_id}")
                    return
                # 已有批次处理中 / 已有积压 -> 拦截进 pending
                self._pending.setdefault(sid, []).append(PendingBatch(time.time(), event))
                event.stop()
                pend_n = len(self._pending[sid])
                self._log(sid, f"拦截批次 {event.event_id} 进 pending（pending={pend_n}）")
                self._ensure_task_locked()
            else:
                # 空闲 -> 放行
                self._inflight[sid] = event.event_id
                self._inflight_since[sid] = time.time()
                self._log(sid, f"放行批次 {event.event_id}")

    async def on_llm_response(self, event: KiraMessageBatchEvent, resp: LLMResponse, *_):
        """ON_LLM_RESPONSE：任何响应都刷新 in-flight 活动计时（LLM 慢但活着 = 不判卡死）；
        无 tool_calls = 该批次最后一步（文本收尾）-> 标记。"""
        if not self.enabled:
            return
        async with self._lock:
            sid = event.session.sid
            if self._inflight.get(sid) == event.event_id:
                self._inflight_since[sid] = time.time()  # 活动心跳（含工具中间步）
                if not resp.tool_calls:
                    self._final_marked.add(sid)
                    self._log(sid, f"批次 {event.event_id} 进入最后一步（文本收尾）")

    async def on_step_result(self, event: KiraMessageBatchEvent, *_):
        """ON_STEP_RESULT：最后一步消息已发送完（事实 #9）-> 执行推送决策（0 延迟）。"""
        if not self.enabled:
            return
        sid = event.session.sid
        need_push = False
        async with self._lock:
            if self._inflight.get(sid) == event.event_id and sid in self._final_marked:
                need_push = True
        if need_push:
            await self._push_pending(sid, event.event_id)

    # ================= 推送决策（三分支，串行） =================

    async def _push_pending(self, sid: str, done_event_id: Optional[str] = None):
        """锁内决策 + 状态更新，锁外 publish。

        done_event_id：触发本次推送的完成批次 event_id（ON_STEP_RESULT 的事件）。
        锁内先校验 _inflight[sid] 仍是该批次才执行——防止并发/重复事件（ON_STEP_RESULT
        重复广播、hook 重复注册、tick 竞争等）在「发布合并批次 → 该批次到达
        on_batch_message」的异步窗口内误清 _inflight，导致自己发布的批次被自己
        拦截进 pending（会话队列死锁根因）。校验失败直接 return，不动任何状态。
        """
        to_publish = None
        async with self._lock:
            if done_event_id is not None and self._inflight.get(sid) != done_event_id:
                return
            to_publish = self._decide_and_apply_locked(sid)
        if to_publish is not None:
            n_msgs = len(to_publish.messages)
            self._log(sid, f"发布批次 {to_publish.event_id}（{n_msgs} 条消息，来自 {len(to_publish.extra.get('merged_from', []))} 个来源）")
            await self.ctx.event_bus.publish(to_publish)

    def _decide_and_apply_locked(self, sid: str) -> Optional[KiraMessageBatchEvent]:
        """三分支推送决策（须持有 _lock）：返回要发布的合并批次，状态已更新。"""
        pending = self._pending.pop(sid, [])
        self._inflight.pop(sid, None)
        self._inflight_since.pop(sid, None)
        self._final_marked.discard(sid)
        if not pending:
            return None

        total_msgs = sum(len(pb.batch.messages) for pb in pending)
        est_tokens = self._estimate_tokens(pending)

        # 分支① 软合并：小积压提前合并（消息数 <= 软合并上限 且 <= 合并消息数上限 且 token 不超）
        if (total_msgs <= self.short_merge_max_messages
                and total_msgs <= self.max_merge_messages
                and (self.max_merge_est_tokens == 0 or est_tokens <= self.max_merge_est_tokens)):
            self._log(sid, f"软合并：{len(pending)}批次/{total_msgs}条（≤软合并上限{self.short_merge_max_messages}）")
            to_merge, rest = self._split_by_limits(pending)
        # 分支② 超时合并：攒批到点合并（无论消息多少，超限拆批）
        elif time.time() - pending[0].arrival_ts >= self.max_merge_seconds:
            waited = time.time() - pending[0].arrival_ts
            self._log(sid, f"超时合并：攒批 {waited:.1f}s ≥ {self.max_merge_seconds}s，{len(pending)}批次/{total_msgs}条")
            to_merge, rest = self._split_by_limits(pending)
        # 分支③ 独立推送：只推第一个批次（1:1），其余留 pending 等下一轮
        else:
            self._log(sid, f"独立推送：第1个批次（{len(pending[0].batch.messages)}条），其余 {len(pending) - 1} 个留 pending")
            to_merge, rest = [pending[0]], pending[1:]

        if rest:
            self._log(sid, f"拆批/留存：本次合 {len(to_merge)} 批次，{len(rest)} 批次留待下轮")

        self._pending[sid] = rest
        merged = self._build_merged_batch(to_merge)
        self._inflight[sid] = merged.event_id
        self._inflight_since[sid] = time.time()
        return merged

    # ================= 阈值防护 =================

    def _split_by_limits(self, pending: list[PendingBatch]):
        """按 批次数 / 媒体批次 / 消息数 / token 上限拆批，返回 (to_merge, rest)。"""
        to_merge: list[PendingBatch] = []
        rest: list[PendingBatch] = []
        total_msgs = 0
        total_tokens = 0
        media_batches = 0
        for pb in pending:
            msgs = len(pb.batch.messages)
            toks = self._estimate_tokens([pb])
            has_media = self._has_media(pb)
            if (self.max_merge_batches_limit and len(to_merge) >= self.max_merge_batches_limit):
                rest.append(pb)
                continue
            if (self.media_preprocess_enabled and self.media_preprocess_max_batches
                    and has_media and media_batches >= self.media_preprocess_max_batches):
                rest.append(pb)
                continue
            if self.max_merge_messages and total_msgs + msgs > self.max_merge_messages:
                rest.append(pb)
                continue
            if self.max_merge_est_tokens and total_tokens + toks > self.max_merge_est_tokens:
                rest.append(pb)
                continue
            to_merge.append(pb)
            total_msgs += msgs
            total_tokens += toks
            if has_media:
                media_batches += 1
        if not to_merge and pending:
            # 极端：第一个批次自身就超限 -> 仍合并它（1:1 语义，宁可超限不可丢/死循环）
            to_merge = [pending[0]]
            rest = pending[1:]
        return to_merge, rest

    def _estimate_tokens(self, batches: list[PendingBatch]) -> int:
        """粗略估算 token：文本字符数 / 估算系数。"""
        ratio = max(1, int(self.token_est_ratio))
        total = 0
        for pb in batches:
            for m in pb.batch.messages:
                msg_str = getattr(m, "message_str", None) or ""
                total += len(msg_str) // ratio
        return total

    def _has_media(self, pb: PendingBatch) -> bool:
        for m in pb.batch.messages:
            # 本插件 stage1 暂存（_pir_media）与并行识图插件（PIR）暂存（_pir_images）
            # 都要检查：stage1 已把媒体替换为 Text 占位符，只看 chain 元素会漏判
            if getattr(m, "_pir_media", None):
                return True
            if getattr(m, "_pir_images", None):
                return True
            for elem in getattr(m, "chain", []) or []:
                if isinstance(elem, (Image, Sticker, Record)):
                    return True
        return False

    def _count_media_batches(self, pending: list[PendingBatch]) -> int:
        """统计 pending 中含媒体元素的批次数（积压媒体限制用）。"""
        return sum(1 for pb in pending if self._has_media(pb))

    # ================= 批次构造 =================

    def _build_merged_batch(self, batches: list[PendingBatch]) -> KiraMessageBatchEvent:
        """合并批次构造：必须沿用原 KiraIMMessage 引用（绝不克隆），属性取最后一批。"""
        msgs = []
        for pb in batches:
            msgs.extend(pb.batch.messages)
        last = batches[-1].batch
        merged = KiraMessageBatchEvent(
            message_types=last.message_types,
            timestamp=int(time.time()),
            adapter=last.adapter,
            session=last.session,
            messages=msgs,
            # _qm_self：自发布标记，on_batch_message 凭此兜底放行自己发布的批次
            #（详见 on_batch_message / _push_pending 注释），防异步窗口竞态自拦截
            extra={"merged_from": [b.batch.event_id for b in batches], "_qm_self": True},
        )
        return merged

    # ================= 兜底 tick（仅 in-flight 卡死） =================

    def _ensure_task_locked(self):
        if self._merge_task is None or self._merge_task.done():
            self._merge_task = asyncio.create_task(self._tick_loop())

    async def _tick_loop(self):
        try:
            while True:
                await asyncio.sleep(0.5)
                await self._tick()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("[QueueMerge] tick loop error")

    async def _tick(self):
        to_publish = []
        async with self._lock:
            now = time.time()
            for sid in list(self._pending):
                pending = self._pending.get(sid)
                if not pending:
                    continue
                inflight = self._inflight.get(sid)
                if inflight and sid not in self._final_marked:
                    # in-flight 仍在处理（未到最后一步）
                    # 卡死判定：自最后一次 LLM 活动（on_llm_response 心跳，含工具中间步）
                    # 起超过 inflight_stall_timeout 仍无动静 —— LLM 挂起/任务崩溃/被其他插件
                    # stop 都会让收尾事件永远不来，此时强制清场推送 pending 防队列死锁。
                    # 配置 0（不设置）时 timeout=0，恒不判定。
                    stalled = (
                        self.inflight_stall_timeout > 0
                        and now - self._inflight_since.get(sid, now) >= self.inflight_stall_timeout
                    )
                    if not stalled:
                        # 攒批超时（max_merge_seconds>0 且到点）时，即使 in-flight 未收尾也强制推送
                        if self.max_merge_seconds > 0 and now - pending[0].arrival_ts >= self.max_merge_seconds:
                            self._log(sid, f"超时兜底：in-flight 未收尾且攒批到点，强制推送 pending")
                        else:
                            continue
                    else:
                        self._log(sid, f"in-flight 卡死兜底（>{self.inflight_stall_timeout:.0f}s 无收尾），强制推送 pending")
                merged = self._decide_and_apply_locked(sid)
                if merged is not None:
                    to_publish.append(merged)
        for merged in to_publish:
            n_msgs = len(merged.messages)
            self._log(merged.session.sid, f"发布批次 {merged.event_id}（{n_msgs} 条）")
            await self.ctx.event_bus.publish(merged)

    # ================= 生命周期 =================

    async def shutdown(self):
        """terminate 时调用：pending 以【全新 batch】按 sid 合并重发（新 event_id、干净 stop 状态），
        取消 tick。可重入。

        ⚠️ 必须用全新 batch 对象：KiraMessageBatchEvent._is_stopped 一旦 stop() 置 True 无法复位，
        event_id 也只在构造时生成。原对象（is_stopped=True）重进 ON_IM_BATCH_MESSAGE 管线后，
        钩子循环执行第一个钩子就会判停并 return，消息永远不会再被处理（热重载丢消息根因）。
        """
        task = None
        to_publish: list[PendingBatch] = []
        async with self._lock:
            task = self._merge_task
            self._merge_task = None
            for sid, pend in self._pending.items():
                to_publish.extend(pend)
                for pb in pend:
                    self._log(sid, f"shutdown 重发 pending 批次 {pb.batch.event_id}")
            self._pending.clear()
            self._inflight.clear()
            self._inflight_since.clear()
            self._final_marked.clear()
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # 按 sid 分组，每 sid 合并为一个全新批次发布
        by_sid: dict[str, list[PendingBatch]] = {}
        for pb in to_publish:
            by_sid.setdefault(pb.batch.session.sid, []).append(pb)
        for sid, pend in by_sid.items():
            try:
                fresh = self._build_merged_batch(pend)
                self._log(sid, f"shutdown 重发合并批次 {fresh.event_id}（{len(pend)} 批次 / {len(fresh.messages)} 条）")
                await self.ctx.event_bus.publish(fresh)
            except Exception:
                logger.exception("[QueueMerge] shutdown republish failed")

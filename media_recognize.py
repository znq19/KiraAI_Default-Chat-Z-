"""并行媒体识别模块（v2.3.2）—— 图片 VLM + 音频 STT 并行预处理

设计要点（对齐方案文档 KiraAI并行媒体识别模块对齐方案.md v1.1）：
- 三阶段架构（stage1 ON_IM_MESSAGE / stage2 ON_IM_BATCH_MESSAGE / stage3 ON_LLM_REQUEST）
- v2.3.2 核心变更（Plus-One 复读兼容 + 官方格式对齐，用户拍板）：
    * Image/Sticker **元素保留在 chain 中**（不再替换为标识符删除）——Plus-One 复读
      表情包依赖 Sticker 元素；图片元素保留则纯图片消息天然不参与复读（Plus-One 只认
      Text/Sticker）。转发消息的媒体同样保留。
    * "识别/不识别"通过**预置 elem.caption** 表达：缓存命中 → desc（框架渲染官方
      [Image desc, file_path: p] / [Sticker desc]（官方无路径，本模块增强追加路径））；
      未命中且宿主标记不识别（仅唤醒/概率未中/超限）→ caption=""（官方空占位
      [Image , file_path: p] / [Sticker ]，阻止框架自动 VLM，LLM 知道有媒体未识别）。
    * 只有需要识别的媒体暂存 _pir_media，stage2 并行 VLM 后回填 message_str（锚点
      替换官方空占位）与 elem.caption。
    * Record 语音照旧替换 [Record #id: ] 标识符（阻止框架自动 STT，走本模块限流+缓存）。
- 原生多模态：native 模式运行时实时检测（_native_mode()）——图片由框架直传，本模块
  不预置/不识别；语音 STT 归本模块照旧。
- PIR 互斥（pir_auto_disable 默认开）：检测到 parallel_image_reader 启用 → 自动关闭
  （本模块已覆盖其全部能力）；竞态/关闭失败降级让位，绝不双重处理。
- 缓存：复用框架 image_desc_cache 表；VLM 描述词跟随 WebUI desc_prompt 配置。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from io import BytesIO
from typing import Optional

from core.plugin import logger
from core.chat.message_utils import KiraMessageEvent, KiraMessageBatchEvent
from core.chat.message_elements import Text, Image, Sticker, Record, Reply, Forward
from core.provider import LLMRequest
from core.utils.common_utils import get_default_vlm_prompt, speech_to_text

# 标识符匹配（内容三态：空 / 描述 / (未识别) (已过期)）
_IMAGE_RE = re.compile(r"\[Image #([^\]\s:]+): ([^\]]*)\]")
_RECORD_RE = re.compile(r"\[Record #([^\]\s:]+): ([^\]]*)\]")
_ALL_RE = re.compile(r"\[(?:Image|Record) #([^\]\s:]+): ([^\]]*)\]")


class ParallelMediaRecognizer:
    """并行媒体识别：作为 mixin 组件挂在聊天插件上，与 queue_merge 解耦。"""

    def __init__(self, ctx, plugin_cfg: dict, bot_cfg: dict):
        self.ctx = ctx
        sec = plugin_cfg.get("section_media_recognition", {})
        self.enabled = sec.get("enabled", True)
        # 三层并发限制（VLM / STT 各自独立），按 批次级 → 会话级 → 全局级 依次获取：
        #   ① 批次级（max_parallel_images / max_parallel_audios）：单个批次内同时识别的最大数，
        #      防"一批 10 张图一次全轰出去"的突发；每个批次使用独立临时信号量
        #   ② 会话级（vlm/stt_max_parallel_per_session）：单个会话累积的最大并行数
        #   ③ 全局级（vlm/stt_max_parallel_global）：所有会话合计的最大并行数
        self.max_parallel_images = int(sec.get("max_parallel_images", 3))
        self.max_parallel_audios = int(sec.get("max_parallel_audios", 3))
        self.vlm_max_parallel_per_session = int(sec.get("vlm_max_parallel_per_session", 15))
        self.vlm_max_parallel_global = int(sec.get("vlm_max_parallel_global", 40))
        self.stt_max_parallel_per_session = int(sec.get("stt_max_parallel_per_session", 15))
        self.stt_max_parallel_global = int(sec.get("stt_max_parallel_global", 40))
        self.media_timeout = float(sec.get("media_timeout", 60.0))
        # 并行识图插件（PIR）自动互斥（默认开）：检测到 PIR 处于启用状态时自动关闭它，
        # 图片识别完全由本模块接管（本模块能力已覆盖 PIR：并行 VLM + 缓存 + 限流 + 转发拍平 + 语音）。
        # 运行时实时检测（与 _pir_active 同理），PIR 热插拔/手动开启后自动再次关闭。
        self.pir_auto_disable = bool(sec.get("pir_auto_disable", True))
        self.quality_enabled = sec.get("quality_enabled", False)
        self.quality_value = int(sec.get("quality_value", 85))

        self._global_img_sem = asyncio.Semaphore(max(1, self.vlm_max_parallel_global))
        self._global_aud_sem = asyncio.Semaphore(max(1, self.stt_max_parallel_global))
        # 每会话信号量（惰性创建，热重载后自动重建）
        self._session_img_sems: dict[str, asyncio.Semaphore] = {}
        self._session_aud_sems: dict[str, asyncio.Semaphore] = {}

        # VLM 描述语言：读全局 locale.lang；未设置默认中文（对齐并行识图插件中文 DESC_PROMPT）。
        # 实际 prompt 优先取 WebUI 配置 desc_prompt（§_describe_image），此处 lang 仅作默认兜底
        self._vlm_lang = "zh"
        try:
            if hasattr(ctx, "config") and ctx.config is not None:
                cfg_lang = ctx.config.get_config("locale.lang")
                if cfg_lang:
                    self._vlm_lang = str(cfg_lang)
        except Exception:
            pass

        # 原生多模态模式（KiraAI v2.31.0+）：bot_config.capabilities.image_recognition.mode == "native"
        # 时，图片由框架原生多模态直接传给模型（官方压缩 + kira_image_ref 持久化引用），
        # 本模块只做音频 STT，stage1 不再替换 Image/Sticker —— 否则 _build_native_content
        # 遍历 chain 找不到图片元素，原生多模态内容为空（模型收不到图），且 stage2 仍会
        # 调用 VLM 描述，与 native 模式"省 VLM token 直传图片"的初衷冲突。
        # 注意：非唤醒消息的图片仍由宿主 handle_msg 按"非唤醒不识别"策略替换为 [图片] 占位，
        # 只有唤醒消息的图片会保留并走原生多模态 —— 与 z/s 版省 token 设计一致。
        # 模式检测不做 __init__ 快照，改由 _native_mode() 每次事件实时读取（见下）：
        # 用户在 WebUI 直接切换 mode 而不重启/重载时，快照会过时——切到 native 后 stage1 仍
        # 替换图片（原生多模态收不到图）、切回 vlm_description 后图片无人识别（VLM 被跳过）。
        # 实时读配置走内存缓存（微秒级），无卡顿无延迟，WebUI 保存后立即生效。

        # 动态属性挂载名（沿用并行识图插件协议语义）
        self._media_attr = "_pir_media"
        # 当前回合暂存原媒体的 id 索引（stage3 现场识别用）。
        # 按 sid 分层：多会话并发处理时互不串扰
        self._round_media: dict[str, dict[str, dict]] = {}

    # ================= 调试日志 =================

    def _log(self, msg: str):
        logger.debug(f"[MediaRecognize] {msg}")

    def _pir_active(self) -> bool:
        """运行时实时检测并行识图插件（PIR）是否已加载，且未被自动互斥关闭。

        语义 v2.3.2 起简化（用户确认）：不再"装了就让位/只做音频"——本模块已覆盖并超越
        PIR（并行 VLM、缓存、三层限流、转发拍平、语音 STT 全都有），PIR 的 stage1 会把
        Image/Sticker 替换为 [Image #id: ] 标识符并删除原元素，破坏 Plus-One 复读表情包。
        因此 pir_auto_disable=True（默认）时：检测到 PIR 启用 → 自动 set_plugin_enabled(False)
        关闭它，图片完全归本模块；关闭失败/竞态（本轮事件 PIR 已先替换）时降级为旧语义
        （图片归 PIR、本模块只做音频），绝不双重处理。
        """
        try:
            pm = getattr(self.ctx, "plugin_mgr", None)
            if pm is None:
                return False
            inst = pm.get_plugin_inst("parallel_image_reader")
            if inst is None:
                return False
            # PIR 已加载：auto-disable 开启则尝试自动关闭（只关一次，防每事件重复 terminate）
            if self.pir_auto_disable:
                if not getattr(self, "_pir_disable_attempted", False):
                    self._pir_disable_attempted = True
                    asyncio.create_task(self._auto_disable_pir())
                # 本轮事件：PIR 处于启用态，stage1 可能已先替换——降级让位，避免双重处理
                return True
            return True  # 互斥关闭：图片归 PIR，本模块只做音频（旧语义）
        except Exception:
            return False

    async def _auto_disable_pir(self):
        """自动关闭并行识图插件（pir_auto_disable=True 时，任务启动后只执行一次）。"""
        if not getattr(self, "pir_auto_disable", False):
            return  # 防御：开关关闭时绝不操作（_pir_active 已保证，双保险）
        try:
            pm = getattr(self.ctx, "plugin_mgr", None)
            if pm is None:
                return
            try:
                enabled = await pm.is_plugin_enabled("parallel_image_reader")
            except TypeError:
                enabled = pm.is_plugin_enabled("parallel_image_reader")
            if enabled:
                try:
                    await pm.set_plugin_enabled("parallel_image_reader", False)
                except TypeError:
                    pm.set_plugin_enabled("parallel_image_reader", False)
                logger.info(
                    "[MediaRecognize] 检测到并行识图插件已启用，已自动禁用（pir_auto_disable，"
                    "图片识别由本模块全权接管；如需恢复 PIR 请在 WebUI 关闭本插件的自动互斥开关）"
                )
        except Exception as e:
            logger.warning(f"[MediaRecognize] 自动禁用并行识图插件失败（不影响识别）: {type(e).__name__}: {e}")

    def _native_mode(self) -> bool:
        """运行时实时检测原生多模态模式（KiraAI v2.31.0+）。

        与 _pir_active 同理，不做 __init__ 一次性快照：用户可能在 WebUI 直接切换
        bot_config.capabilities.image_recognition.mode 而不重启 Kira / 重载插件，
        快照会过时。每次事件实时读取配置（框架配置走内存缓存，微秒级，
        无卡顿延迟），WebUI 保存后立即生效。
        """
        try:
            if hasattr(self.ctx, "config") and self.ctx.config is not None:
                mode = self.ctx.config.get_config(
                    "bot_config.capabilities.image_recognition.mode", "vlm_description"
                )
                return str(mode or "").lower() == "native"
        except Exception:
            pass
        return False

    # ================= 三级并发限流（批次级 + 每会话 + 全局） =================

    def _session_sem(self, sems: dict, sid: str, limit: int) -> asyncio.Semaphore:
        """惰性获取/创建某会话的信号量。"""
        sem = sems.get(sid)
        if sem is None:
            sem = asyncio.Semaphore(max(1, limit))
            sems[sid] = sem
        return sem

    # ================= stage1：拍平嵌套 Forward + 替换为标识符 =================

    # 递归遍历/拍平的深度上限：防恶意超深嵌套（Forward 层层套娃）触发 RecursionError。
    # 超深时安全降级——深层 Forward 保留原样，由核心过滤兜底（内容无痕省略但不崩溃）。
    _MAX_CHAIN_DEPTH = 64

    @staticmethod
    def _flatten_forwards(chain, stack=None, depth=0, max_depth=None):
        """就地拍平嵌套 Forward（借鉴并行识图插件 _flatten_forwards，main.py:234-285）。

        KiraAI 核心 message_format_to_text 渲染 Forward 时会过滤嵌套 Forward 元素
        （`[x for x in chain if not isinstance(x, Forward)]`，message_manager.py:371，防无限递归），
        导致嵌套转发的内容（含图片标识符）不进 message_str，LLM 看不到。stage1 先把嵌套
        Forward 展开为平铺元素，保证嵌套内容完整渲染。

        语义：depth=0 的顶层 Forward（消息本身是转发）保留壳；depth>0 的嵌套 Forward
        逐层展开为其子链内容。覆盖路径：Forward.chains 与 Reply.chain。防环：stack 记录
        当前展开路径上的 chain（id），环中子链保留 Forward 元素（核心过滤兜底）。
        深度上限 max_depth（默认 _MAX_CHAIN_DEPTH）：超限不展开（深层内容无痕省略）。
        """
        if max_depth is None:
            max_depth = ParallelMediaRecognizer._MAX_CHAIN_DEPTH
        if stack is None:
            stack = set()
        cid = id(chain)
        if cid in stack:
            return  # 环：同一展开路径上再次出现
        stack.add(cid)
        i = 0
        while i < len(chain):
            ele = chain[i]
            if isinstance(ele, Reply) and ele.chain is not None:
                if depth < max_depth:
                    ParallelMediaRecognizer._flatten_forwards(
                        ele.chain, stack, depth + 1, max_depth)
            elif isinstance(ele, Forward) and ele.chains:
                if depth < max_depth:
                    for c in ele.chains:
                        ParallelMediaRecognizer._flatten_forwards(
                            c, stack, depth + 1, max_depth)
                if depth > 0 and depth < max_depth:
                    # 嵌套 Forward：展开为其子链内容（平铺替换元素本身）
                    expanded = []
                    for c in ele.chains:
                        if id(c) in stack:
                            continue  # 环：跳过该子链（内容无痕省略）
                        expanded.extend(c)
                    if expanded:
                        chain[i:i + 1] = expanded
                        i += len(expanded) - 1
            i += 1
        stack.remove(cid)

    async def on_im_message(self, event: KiraMessageEvent, *_):
        """ON_IM_MESSAGE：先拍平嵌套 Forward（防核心渲染丢内容），再处理媒体。

        核心设计 v2.3.2（复读兼容 + 官方格式对齐）：
        - Image/Sticker 元素**保留在 chain 中**（不再替换为标识符/删除）——Plus-One
          复读表情包依赖 chain 里存在 Sticker 元素；图片元素保留则纯图片消息天然
          不参与复读（Plus-One 只认 Text/Sticker）。
        - "识别/不识别"通过**预置 elem.caption** 表达：缓存命中 → desc（框架渲染
          官方 [Image desc, file_path: p]）；未命中 → ""（阻止框架自动 VLM，渲染
          官方空占位 [Image , file_path: p]，LLM 知道有媒体但未识别）。
        - 宿主 handle_msg 已按"仅唤醒识别/识别概率"给不识别媒体打 _media_skip 标记
          （caption=""），本阶段尊重标记：跳过的不暂存不 VLM；唤醒/概率命中的
          未命中媒体才暂存 _pir_media 供 stage2 并行 VLM 后回填官方格式。
        - Record 语音照旧替换为 [Record #id: ] 标识符（阻止框架自动 STT、走本模块
          三层限流 + 缓存；语音不进复读判定，替换无副作用）。
        """
        if not self.enabled:
            return
        try:
            self._flatten_forwards(event.message.chain)
            media: dict[str, dict] = {}
            await self._walk_chain(
                event.message.chain, media, set(),
                is_mentioned=bool(getattr(event, "is_mentioned", False)),
            )
            if media:
                # 合并而非覆盖：并行识图插件（PIR）可能已先写入 Image 索引，
                # 直接覆盖会让它 stage2/stage3 拿不到图片（图片标识符永远空）
                existing = getattr(event.message, self._media_attr, None) or {}
                setattr(event.message, self._media_attr, {**existing, **media})
        except Exception:
            logger.exception("[MediaRecognize] stage1 error")

    async def _walk_chain(self, chain, media: dict, visited: set, is_mentioned: bool = False):
        """递归遍历 chain（含 Reply.chain / Forward.chains，带环检测）。嵌套 Forward 已拍平。"""
        if chain is None:
            return
        cid = id(chain)
        if cid in visited:
            return
        visited.add(cid)
        for idx, elem in enumerate(chain):
            if isinstance(elem, Text):
                continue
            if isinstance(elem, (Image, Sticker)):
                # 并行识图插件接管中（自动互斥关闭未生效/竞态降级）：图片归它，本模块不碰。
                # 运行时实时检测（不是 __init__ 快照），PIR 热重载/启停后自动生效
                if self._pir_active():
                    continue
                # 原生多模态模式（KiraAI v2.31.0+）：元素保留在 chain 中，
                # 由框架 _build_native_content 收集并直传模型（官方压缩 + 持久化引用）。
                # 本模块不预置 caption、不识别图片，只做音频 STT。
                if self._native_mode():
                    continue
                mtype = "Image" if isinstance(elem, Image) else "Sticker"
                await self._prefill_media(elem, mtype, media)
            elif isinstance(elem, Record):
                replaced = await self._replace_media(elem, "Record", media)
                if replaced is not None:
                    chain[idx] = replaced
            elif isinstance(elem, Reply):
                await self._walk_chain(getattr(elem, "chain", None), media, visited, is_mentioned)
            elif isinstance(elem, Forward):
                for sub in (getattr(elem, "chains", None) or []):
                    await self._walk_chain(sub, media, visited, is_mentioned)

    async def _prefill_media(self, elem, mtype: str, media: dict):
        """图片/表情包 → 预置 caption（元素保留，不替换、不删除）。

        对齐官方渲染（core/message_manager.py：Image → [Image {caption}, file_path: {p}]；
        Sticker → [Sticker {caption}]），并按"仅唤醒识别/概率"省 VLM：
        - 缓存命中 → elem.caption = desc：框架渲染官方带描述格式，零 VLM、零暂存；
        - 未命中且宿主标记 _media_skip（非唤醒仅唤醒开 / 概率未中 / 超限）→ caption=""
          （官方空占位 [Image , file_path: p] / [Sticker ]，LLM 知道有媒体但未识别），不暂存不 VLM；
        - 未命中且未标记（唤醒 / 概率命中）→ caption="" + 暂存 _pir_media，
          stage2 并行 VLM 后回填 message_str（官方格式）与 elem.caption。
        Sticker 与 Image 同规则：元素永远保留 → Plus-One 复读表情包不受识别影响。
        """
        # 宿主 handle_msg 已做"仅唤醒/概率"决策：_media_skip=True = 本次不识别（省 VLM）
        if getattr(elem, "_media_skip", False):
            elem.caption = ""  # 官方空占位 + 阻止框架自动 VLM（caption 非 None）
            return
        try:
            md5 = await elem.hash_image()
        except Exception:
            md5 = None
        short_id = md5[:8] if md5 else f"noid_{id(elem)}"
        if md5:
            desc = await self._cache_get(md5) or ""
            if desc and not self._is_valid_desc(desc):
                desc = ""
            if desc:
                # 缓存命中：直接预置官方描述（零 VLM）。不进 media（_done 隐含），
                # 同一批消息重发时无需再处理——stage2 只认 _pir_media 里的媒体。
                elem.caption = desc
                return
        # 未命中：暂存原元素供 stage2 并行识别（唤醒/概率命中路径）
        elem.caption = ""  # 先阻止框架自动 VLM，stage2 识别完成后回填官方格式
        media[short_id] = {"md5": md5, "elem": elem, "type": mtype, "_done": False}

    async def _replace_media(self, elem, mtype: str, media: dict) -> Optional[Text]:
        """语音 Record → 标识符 Text（仅供 Record 使用；图片/表情包走 _prefill_media）。

        语音替换为 [Record #id: ] 标识符：阻止框架自动 STT（串行、无限流），改由
        stage2 并行 STT（三层限流 + image_desc_cache 缓存复用），语义与旧版一致。
        _done 标记：缓存命中（已含内容）或已识别过 → 重发跳过，防重复 STT/429。
        """
        try:
            md5 = await self._record_md5(elem)
        except Exception:
            md5 = None
        if md5:
            short_id = md5[:8]
            desc = await self._cache_get(md5) or ""
            if desc and not self._is_valid_desc(desc):
                desc = ""
        else:
            short_id = f"noid_{id(elem)}"
            desc = ""
        media[short_id] = {"md5": md5, "elem": elem, "type": mtype, "_done": bool(desc)}
        if desc:
            # 缓存命中：直接带 file_path（to_path 幂等，_temp_path 已缓存不重复下载）
            p = await self._media_path(elem)
            if p:
                return Text(f"[Record #{short_id}: {desc}, file_path: {p}]")
        return Text(f"[Record #{short_id}: {desc}]")

    async def _media_path(self, elem) -> Optional[str]:
        """对齐原版 message_format_to_text：to_path 落盘后转 data/ 相对路径。

        原版（core/message_manager.py Image 分支）：to_path() → relative_to(data_dir)
        → "data/xxx"，失败降级绝对路径。本模块 stage1 把媒体替换为标识符绕过了
        原版渲染，这里补回 file_path，让 LLM 能拿到本地路径做图生图/上传等。
        """
        try:
            from pathlib import Path
            from core.utils.path_utils import get_data_path
            path = Path(await elem.to_path())
            data_dir = get_data_path()
            try:
                rel = path.relative_to(data_dir)
                return f"data/{rel}"
            except ValueError:
                return str(path)
        except Exception:
            return None

    async def _record_md5(self, elem) -> Optional[str]:
        """音频指纹：to_base64 后取 md5（Record 无 hash_image）。"""
        try:
            b64 = await elem.to_base64()
            if b64.startswith("data:"):
                b64 = b64.split(",", 1)[1]
            return hashlib.md5(base64.b64decode(b64)).hexdigest()
        except Exception:
            return None

    # ================= stage2：并行识别 + 填充（核心） =================

    async def on_im_batch_message(self, event: KiraMessageBatchEvent, *_):
        """ON_IM_BATCH_MESSAGE：收集批次暂存媒体，VLM 与 STT 混合 gather 并行识别，填充。"""
        if not self.enabled:
            return
        try:
            tasks = []  # [(message, media)]
            for message in event.messages:
                media = getattr(message, self._media_attr, None)
                if media:
                    tasks.append((message, media))
            if not tasks:
                return

            # 当前回合原媒体索引（stage3 用）；按 sid 分层防多会话并发串扰，
            # 同一 sid 的并发批次用 setdefault+update 合并，避免后到批次清掉先到批次
            sess_sid = event.session.sid
            self._round_media.setdefault(sess_sid, {})
            for _, media in tasks:
                for short_id, info in media.items():
                    self._round_media[sess_sid][short_id] = info
            # 防无界增长：最多保留 128 个 sid 的索引，超出清最旧
            if len(self._round_media) > 128:
                for old_sid in list(self._round_media)[: len(self._round_media) - 64]:
                    self._round_media.pop(old_sid, None)

            # 只识别未处理（_done=False）的媒体：缓存命中（stage1 已填描述）或
            # 已识别过（成功/失败）的跳过——队列合并重发同一批消息时不会重复 VLM/STT
            pending_tasks = [
                (message, {k: v for k, v in media.items() if not v.get("_done")})
                for message, media in tasks
            ]
            pending_tasks = [(m, md) for m, md in pending_tasks if md]
            # 原生多模态模式：图片/表情包已由框架直传模型，stage2 只做音频 STT
            if self._native_mode():
                pending_tasks = [
                    (m, {k: v for k, v in md.items() if v.get("type") not in ("Image", "Sticker")})
                    for m, md in pending_tasks
                ]
                pending_tasks = [(m, md) for m, md in pending_tasks if md]

            # 混合并行：图片 VLM 与 音频 STT 同一 gather，各自限流互不阻塞。
            # 批次级信号量：每批次临时创建，限制本批次内同时识别的数量（突发保护）
            batch_img_sem = asyncio.Semaphore(max(1, self.max_parallel_images))
            batch_aud_sem = asyncio.Semaphore(max(1, self.max_parallel_audios))
            results: dict[str, str] = {}
            coros = []
            for _, media in pending_tasks:
                for short_id, info in media.items():
                    if info["type"] in ("Image", "Sticker"):
                        coros.append(self._describe_one(sess_sid, short_id, info, results, batch_sem=batch_img_sem))
                    else:
                        coros.append(self._transcribe_one(sess_sid, short_id, info, results, batch_sem=batch_aud_sem))
            await asyncio.gather(*coros, return_exceptions=True)

            # 预取 file_path（to_path 落盘 + data/ 相对路径），填充时带上，
            # 对齐原版 message_format_to_text 的 [Image desc, file_path: xxx] 格式
            paths: dict[str, str] = {}
            for _, media in tasks:
                for sid, info in media.items():
                    if sid in results and sid not in paths and info.get("elem") is not None:
                        p = await self._media_path(info["elem"])
                        if p:
                            paths[sid] = p

            # 填充 message_str 与 chain
            for message, media in tasks:
                hit = any(sid in results for sid in media)
                if hit:
                    if message.message_str:
                        message.message_str = self._fill_message_str(
                            message.message_str, results, paths, message.chain)
                    self._fill_chain(message.chain, results, paths)
        except Exception:
            logger.exception("[MediaRecognize] stage2 error")

    def _fill_message_str(self, text: str, results: dict, paths: dict,
                          chain=None) -> str:
        """填充 message_str：以 chain 里 Image/Sticker 元素（官方空占位锚点）优先，
        找不到时按 [Media #id: ] 标识符兜底（语音 Record / 历史遗留标识符）。

        chain 优先：官方格式占位的 file_path 与 chain 元素逐位对应，按序替换
        （同一消息多个 [Image , file_path: data/x] 各自独立、互不误伤）。
        chain 不可得/无匹配时退回 _fill_text（Record 标识符与旧格式兼容）。
        """
        if chain is not None:
            # 递归遍历 chain 中所有 Image/Sticker（含 Reply.chain / Forward.chains），
            # 按官方空占位顺序逐一回填——嵌套引用/转发里的媒体同样生效
            replaced = False
            for elem in self._iter_media_elems(chain):
                filled = self._fill_official(elem, results, paths)
                if not filled or not filled[0]:
                    continue
                short_id, desc, p = filled
                mtype = "Image" if isinstance(elem, Image) else "Sticker"
                new_text = self._fill_official_text(text, mtype, desc, p)
                if new_text != text:
                    text = new_text
                    replaced = True
            if replaced:
                return text
        # chain 无 Image/Sticker 命中：退回标识符填充（Record / 嵌套链 / 兼容）
        return self._fill_text(text, results, paths)

    @staticmethod
    def _iter_media_elems(chain):
        """递归 yield chain 内所有 Image/Sticker（含 Reply.chain / Forward.chains，防环）。"""
        seen = set()
        def _walk(c):
            if c is None:
                return
            cid = id(c)
            if cid in seen:
                return
            seen.add(cid)
            for ele in c:
                if isinstance(ele, (Image, Sticker)):
                    yield ele
                elif isinstance(ele, Reply):
                    yield from _walk(getattr(ele, "chain", None))
                elif isinstance(ele, Forward):
                    for sub in (getattr(ele, "chains", None) or []):
                        yield from _walk(sub)
        yield from _walk(chain)

    async def _describe_one(self, sess_sid: str, media_id: str, info: dict, results: dict,
                            batch_sem: Optional[asyncio.Semaphore] = None):
        md5 = info["md5"]
        cached = await self._cache_get(md5) if md5 else None
        if cached:
            info["_done"] = True
            results[media_id] = cached
            return
        try:
            sess_sem = self._session_sem(self._session_img_sems, sess_sid, self.vlm_max_parallel_per_session)
            # 三层限流：批次级 → 会话级 → 全局级（固定获取顺序，无死锁）
            if batch_sem is not None:
                async with batch_sem, sess_sem, self._global_img_sem:
                    desc = await asyncio.wait_for(self._describe_image(info["elem"]), self.media_timeout)
            else:
                async with sess_sem, self._global_img_sem:
                    desc = await asyncio.wait_for(self._describe_image(info["elem"]), self.media_timeout)
            # 无论成功失败都标记已处理：同一条消息重发不再重复识别（防 429 风暴）
            info["_done"] = True
            if desc and self._is_valid_desc(desc):
                if md5:
                    await self._cache_set(md5, desc)
                results[media_id] = desc
            else:
                logger.warning(f"[MediaRecognize] image VLM returned empty/invalid desc id={media_id} md5={md5[:8] if md5 else 'n/a'}")
                results[media_id] = "(未识别)"
        except Exception as e:
            info["_done"] = True
            logger.warning(f"[MediaRecognize] image describe failed id={media_id}: {type(e).__name__}: {e}")
            results[media_id] = "(未识别)"

    async def _transcribe_one(self, sess_sid: str, media_id: str, info: dict, results: dict,
                              batch_sem: Optional[asyncio.Semaphore] = None):
        md5 = info["md5"]
        cached = await self._cache_get(md5) if md5 else None
        if cached:
            info["_done"] = True
            results[media_id] = cached
            return
        try:
            provider_mgr = getattr(self.ctx, "provider_mgr", None)
            stt_client = provider_mgr.get_default_stt() if provider_mgr is not None else None
            if stt_client is None:
                info["_done"] = True
                logger.warning(f"[MediaRecognize] STT client unavailable (no default STT model) id={media_id}")
                results[media_id] = "(未识别)"
                return
            sess_sem = self._session_sem(self._session_aud_sems, sess_sid, self.stt_max_parallel_per_session)
            # 三层限流：批次级 → 会话级 → 全局级（固定获取顺序，无死锁）
            if batch_sem is not None:
                async with batch_sem, sess_sem, self._global_aud_sem:
                    text = await asyncio.wait_for(
                        speech_to_text(client=stt_client, record=info["elem"]), self.media_timeout)
            else:
                async with sess_sem, self._global_aud_sem:
                    text = await asyncio.wait_for(
                        speech_to_text(client=stt_client, record=info["elem"]), self.media_timeout)
            # 无论成功失败都标记已处理：同一条消息重发不再重复识别（防 429 风暴）
            info["_done"] = True
            if text and self._is_valid_desc(text):
                if md5:
                    await self._cache_set(md5, text)
                results[media_id] = text
            else:
                logger.warning(f"[MediaRecognize] STT returned empty/invalid text id={media_id}")
                results[media_id] = "(未识别)"
        except Exception as e:
            info["_done"] = True
            logger.warning(f"[MediaRecognize] STT failed id={media_id}: {type(e).__name__}: {e}")
            results[media_id] = "(未识别)"

    async def _describe_image(self, elem) -> str:
        """图片 VLM：统一 to_data_url → vlm.chat 路径（对齐并行识图插件已验证路径）；
        to_data_url 失败时 fallback 直接 httpx 下载（带 UA + pixiv Referer，覆盖图床防盗链）；
        quality_enabled 时 JPEG 压缩。失败返回 ""（调用方降级为 (未识别) 并打日志）。"""
        try:
            vlm = self.ctx.provider_mgr.get_default_vlm()
            if vlm is None:
                logger.warning("[MediaRecognize] get_default_vlm() returned None")
                return ""
            data_url = None
            try:
                data_url = await elem.to_data_url()
            except Exception as e:
                logger.debug(f"[MediaRecognize] to_data_url failed ({type(e).__name__}), try direct download")
                data_url = await self._try_direct_download(elem)
            if not data_url:
                logger.warning(
                    f"[MediaRecognize] cannot fetch image data: "
                    f"file_type={getattr(elem, 'file_type', '?')} "
                    f"file={str(getattr(elem, 'file', ''))[:80]}"
                )
                return ""
            if self.quality_enabled:
                _, _, b64 = data_url.partition(",")
                if not b64:
                    logger.warning("[MediaRecognize] empty base64 after to_data_url")
                    return ""
                img = _open_image(base64.b64decode(b64))
                q = max(10, min(100, self.quality_value))
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=q)
                data_url = f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}"
            prompt = self._vlm_prompt()
            request = LLMRequest(messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
                    {"type": "text", "text": prompt},
                ],
            }])
            resp = await vlm.chat(request)
            return (resp.text_response or "").strip() if resp else ""
        except Exception as e:
            logger.warning(f"[MediaRecognize] describe image failed: {type(e).__name__}: {e}")
            return ""

    def _vlm_prompt(self) -> str:
        """VLM 描述词：跟随 WebUI 配置 bot_config.capabilities.image_recognition.desc_prompt
        （对齐框架 message_format_to_text 行为）；未配置/为空时用 locale.lang 语言默认 prompt。"""
        try:
            caps = self.ctx.config.get_config("bot_config.capabilities.image_recognition", {})
            desc_prompt = (caps or {}).get("desc_prompt", "") or ""
            if desc_prompt.strip():
                return desc_prompt.strip()
        except Exception:
            pass
        return get_default_vlm_prompt(self._vlm_lang)

    async def _try_direct_download(self, elem) -> Optional[str]:
        """to_data_url 失败时：直接 httpx 下载图片（带 UA，pixiv 图床补 Referer），返回 data_url 或 None。"""
        url = getattr(elem, "file", None) or getattr(elem, "image", None)
        if not url or not str(url).startswith(("http://", "https://")):
            return None
        try:
            import httpx
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            }
            async with httpx.AsyncClient(follow_redirects=True, timeout=self.media_timeout) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    # pixiv 图床防盗链：补 Referer 重试
                    resp = await client.get(url, headers={**headers, "Referer": "https://www.pixiv.net/"})
                if resp.status_code == 200 and resp.content:
                    return "data:image/jpeg;base64," + base64.b64encode(resp.content).decode()
        except Exception as e:
            logger.debug(f"[MediaRecognize] direct download failed: {type(e).__name__}: {e}")
        return None

    # ================= stage3：历史/残留标识符兜底 =================

    async def on_llm_request(self, event: KiraMessageBatchEvent, req: LLMRequest, *_):
        """ON_LLM_REQUEST：扫描 req.user_prompt 残留空标识符：缓存命中填、有原媒体现场识别、否则 (已过期)。"""
        if not self.enabled:
            return
        try:
            need: dict[str, str] = {}  # sid -> 标识符类型
            for p in getattr(req, "user_prompt", []) or []:
                text = getattr(p, "content", "") or ""
                for m in _ALL_RE.finditer(text):
                    if not m.group(2).strip():
                        need[m.group(1)] = m.group(0)
            if not need:
                return
            results: dict[str, str] = {}
            # 批次级限流同样作用于 stage3 兜底识别（一个 LLM 请求内的残留标识符 = 一个批次）
            batch_img_sem = asyncio.Semaphore(max(1, self.max_parallel_images))
            batch_aud_sem = asyncio.Semaphore(max(1, self.max_parallel_audios))
            coros = []
            # 只查本会话当前回合暂存的媒体（按 sid 分层，多会话不串扰）
            round_media = self._round_media.get(event.sid, {})
            for media_id in need:
                info = round_media.get(media_id)
                if info and not info.get("_done"):
                    # 有原媒体且未识别过 → 现场识别
                    if info["type"] == "Image":
                        # 原生多模态模式：图片不识别，直接标 (未识别) 占位
                        if self._native_mode():
                            results[media_id] = "(未识别)"
                            continue
                        coros.append(self._describe_one(event.sid, media_id, info, results, batch_sem=batch_img_sem))
                    else:
                        coros.append(self._transcribe_one(event.sid, media_id, info, results, batch_sem=batch_aud_sem))
                elif info:
                    # 已识别过但占位符仍空（异常路径）：直接标未识别，不重复撞模型
                    results[media_id] = "(未识别)"
                else:
                    results[media_id] = "(已过期)"
            if coros:
                await asyncio.gather(*coros, return_exceptions=True)
            # 预取 file_path（stage3 兜底同样带路径，与 stage1/stage2 格式一致）
            paths: dict[str, str] = {}
            for media_id in need:
                info = round_media.get(media_id)
                if info and info.get("elem") is not None:
                    p = await self._media_path(info["elem"])
                    if p:
                        paths[media_id] = p
            for p in getattr(req, "user_prompt", []) or []:
                text = getattr(p, "content", "") or ""
                new_text = self._fill_text(text, results, paths)
                if new_text != text:
                    p.content = new_text
        except Exception:
            logger.exception("[MediaRecognize] stage3 error")
        finally:
            # 无论正常/异常/提前 return 都清理本会话暂存媒体索引，防单 sid 无限累积（内存泄漏）。
            # stage2 的 setdefault+update 是同步原子块，pop 后新批次会重建，无并发风险
            self._round_media.pop(event.sid, None)

    # ================= 填充 =================

    def _fill_text(self, text: str, results: dict, paths: Optional[dict] = None) -> str:
        """按 [Media #id: ] 标识符填充（Record 语音标识符；兼容历史/占位模式遗留标识符）。"""
        for sid, desc in results.items():
            # 用 str.replace 而非 re.sub：replacement 是模板字符串，desc 含 \U/\x 等
            # 反斜杠序列（如 Windows 路径）会抛 bad escape；replace 无转义问题
            fp = ""
            if paths and sid in paths:
                fp = f", file_path: {paths[sid]}"
            text = text.replace(f"[Image #{sid}: ]", f"[Image #{sid}: {desc}{fp}]")
            text = text.replace(f"[Record #{sid}: ]", f"[Record #{sid}: {desc}{fp}]")
        return text

    def _fill_official(self, elem, results: dict, paths: Optional[dict] = None):
        """官方格式回填：把识别结果写回 Image/Sticker 元素（chain 保留原元素）。

        对齐框架渲染（core/message_manager.py）：
          Image  → [Image {caption}, file_path: {p}]
          Sticker→ [Sticker {caption}]（官方无 file_path；本模块增强追加 , file_path: {p}，
                   让 LLM 也能拿到表情包本地路径做图生图/上传——复读不受影响，元素始终保留）
        返回 (short_id, desc, path) 供 _fill_official_text 在 message_str 里锚点替换；
        找不到对应 media 时返回 None。
        """
        md5 = None
        try:
            md5 = getattr(elem, "md5", None) or None
        except Exception:
            md5 = None
        if not md5:
            return None
        short_id = md5[:8]
        desc = results.get(short_id)
        if desc is None:
            desc = results.get(f"noid_{id(elem)}")
        if desc is None:
            return None
        p = ""
        if paths and short_id in paths:
            p = paths[short_id]
        return (short_id, desc, p)

    def _fill_official_text(self, text: str, mtype: str, desc: str, p: str) -> str:
        """把 message_str 里的官方空占位替换为带描述的官方格式（只替换第一处）。

        空占位形态（caption="" 时框架渲染）：
          Image  → "[Image , file_path: {p}]"（to_path 成功）或 "[Image ]"（落盘失败降级）
          Sticker→ "[Sticker ]"
        识别后形态："[Image {desc}, file_path: {p}]" / "[Sticker {desc}, file_path: {p}]"
        """
        if mtype == "Image":
            filled = f"[Image {desc}, file_path: {p}]" if p else f"[Image {desc}]"
            if p:
                text = text.replace(f"[Image , file_path: {p}]", filled, 1)
            return text.replace("[Image ]", filled, 1)
        filled = f"[Sticker {desc}, file_path: {p}]" if p else f"[Sticker {desc}]"
        return text.replace("[Sticker ]", filled, 1)

    def _fill_chain(self, chain, results: dict, paths: Optional[dict] = None):
        """回填 chain：Image/Sticker 元素写回 caption（元素保留）；Text 内 Record/历史标识符替换。"""
        if chain is None:
            return
        for elem in chain:
            if isinstance(elem, Text):
                # 与 _fill_text 一致：全文 replace（不依赖 match 只匹配开头），
                # 避免 Text 前有前缀时 chain 漏填而 message_str 已填的不一致
                new_text = self._fill_text(elem.text or "", results, paths)
                if new_text != elem.text:
                    elem.text = new_text
            elif isinstance(elem, (Image, Sticker)):
                filled = self._fill_official(elem, results, paths)
                if filled:
                    short_id, desc, p = filled
                    elem.caption = desc
            elif isinstance(elem, Reply):
                self._fill_chain(getattr(elem, "chain", None), results, paths)
            elif isinstance(elem, Forward):
                for sub in (getattr(elem, "chains", None) or []):
                    self._fill_chain(sub, results, paths)

    # ================= 缓存（复用 image_desc_cache 表） =================

    async def _cache_get(self, md5: str) -> Optional[str]:
        try:
            row = await self.ctx.db.get_image_desc_cache(md5)
            return row["description"] if row else None
        except Exception:
            return None

    async def _cache_set(self, md5: str, text: str):
        if not md5 or not text:
            return
        try:
            await self.ctx.db.add_image_desc_cache(md5, text, count=1, last_seen=0)
        except Exception:
            pass

    # ================= 校验 =================

    @staticmethod
    def _is_valid_desc(desc: str) -> bool:
        if not desc or not desc.strip():
            return False
        if "\x00" in desc:
            return False
        if "<!--PIR:" in desc:
            return False
        if "[Image #" in desc or "[Record #" in desc:
            return False  # 防嵌套标识符注入缓存并扩散
        return True


def _open_image(data: bytes):
    from PIL import Image as PILImage
    return PILImage.open(BytesIO(data)).convert("RGB")

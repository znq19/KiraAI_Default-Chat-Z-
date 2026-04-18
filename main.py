import asyncio
import random

from core.plugin import BasePlugin, logger, on, Priority
from core.chat.message_utils import KiraMessageEvent, KiraMessageBatchEvent
from core.provider import LLMRequest
from core.chat.message_elements import Text, Image, Reply, Sticker, Forward


class DebouncePlugin(BasePlugin):
    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        self.session_events: dict[str, asyncio.Event] = {}
        self.session_tasks: dict[str, asyncio.Task] = {}
        bot_cfg = ctx.config["bot_config"].get("bot", {})
        self.debounce_interval = float(bot_cfg.get("max_message_interval", 1.5))
        self.max_buffer_messages = int(bot_cfg.get("max_buffer_messages", 3))
        self.max_unmentioned_messages = int(self.plugin_cfg.get("max_unmentioned_messages", 5))
        self.receive_unmentioned = self.plugin_cfg.get("receive_unmentioned", False)
        self.group_chat_prompt = self.plugin_cfg.get("group_chat_prompt", "")
        self.group_proactive_chat = self.plugin_cfg.get("group_proactive_chat", False)
        self.group_proactive_chat_probability = self.plugin_cfg.get("group_proactive_chat_probability", 0.1)

        self.waking_words = cfg.get("waking_words", [])

        # ========== 新增：图片/表情/转发消息处理配置（仅群聊） ==========
        self.image_recognition_only_on_mention = cfg.get("image_recognition_only_on_mention", True)
        self.image_recognition_probability = float(cfg.get("image_recognition_probability", 0.5))
        self.max_images_per_message = int(cfg.get("max_images_per_message", 3))
        self.forward_recognition_only_on_mention = cfg.get("forward_recognition_only_on_mention", True)

    async def initialize(self):
        logger.info(f"[Debounce] enabled (group media/forward control, private unchanged)")

    async def terminate(self):
        """清理所有未完成的 debounce 任务，防止资源泄漏"""
        # 取消所有会话的 debounce 循环任务
        for sid, task in list(self.session_tasks.items()):
            if not task.done():
                task.cancel()
        # 等待所有任务真正取消（可选，确保资源释放）
        if self.session_tasks:
            await asyncio.gather(*self.session_tasks.values(), return_exceptions=True)
        self.session_tasks.clear()
        self.session_events.clear()
        logger.debug("[Debounce] All debounce tasks cancelled")

    # ========== 新增：图片/表情/转发处理函数（仅群聊调用） ==========
    def _process_media(self, chain, is_mentioned: bool):
        """处理消息链中的图片、动画表情和合并转发消息（仅群聊）"""
        for i, elem in enumerate(chain.message_list):
            if isinstance(elem, (Image, Sticker)):
                if is_mentioned:
                    continue  # 唤醒消息：始终保留
                # 非唤醒消息
                if self.image_recognition_only_on_mention:
                    chain.message_list[i] = Text("[图片]" if isinstance(elem, Image) else "[动画表情]")
                else:
                    if random.random() >= self.image_recognition_probability:
                        chain.message_list[i] = Text("[图片]" if isinstance(elem, Image) else "[动画表情]")
            elif isinstance(elem, Forward):
                if is_mentioned:
                    if self.forward_recognition_only_on_mention:
                        continue  # 唤醒消息且开关开启，保留原转发内容
                    else:
                        chain.message_list[i] = Text("[转发消息]")
                else:
                    chain.message_list[i] = Text("[转发消息]")
            elif isinstance(elem, Reply) and elem.chain:
                self._process_media(elem.chain, is_mentioned)

    def _limit_media_count(self, chain, max_count: int):
        """限制消息链中图片+表情的数量（仅群聊，且仅在非唤醒且关闭仅唤醒识图时调用）"""
        if self.image_recognition_only_on_mention:
            return
        media_indices = [i for i, e in enumerate(chain.message_list) if isinstance(e, (Image, Sticker))]
        if len(media_indices) <= max_count:
            return
        for idx in reversed(media_indices[max_count:]):
            elem = chain.message_list[idx]
            chain.message_list[idx] = Text("[图片]" if isinstance(elem, Image) else "[动画表情]")

    @on.im_message(priority=Priority.HIGH)
    async def handle_msg(self, event: KiraMessageEvent):
        # === Check waking words ===
        for m in event.message.chain:
            if isinstance(m, Text) and any(w in m.text for w in self.waking_words):
                event.message.is_mentioned = True
                break

        # ========== 新增：仅对群聊进行媒体/转发处理 ==========
        if event.is_group_message():
            is_mentioned = event.is_mentioned
            self._process_media(event.message.chain, is_mentioned)
            if not is_mentioned and not self.image_recognition_only_on_mention:
                self._limit_media_count(event.message.chain, self.max_images_per_message)

        # Ignore unmentioned messages (官方原版逻辑，使用 discard)
        if not event.is_mentioned:
            if self.receive_unmentioned:
                buffer = self.ctx.get_buffer(str(event.session))
                if buffer.get_length() >= self.max_unmentioned_messages:
                    buffer.pop(count=buffer.get_length()-self.max_unmentioned_messages+1)
                event.buffer()
                if self.group_proactive_chat:
                    if random.random() < self.group_proactive_chat_probability:
                        logger.info("[Chat] Triggered proactive chat")
                        event.flush()
            else:
                event.discard()
            return

        sid = event.session.sid
        event.buffer()

        buffer_len = self.ctx.message_processor.get_session_buffer_length(sid)
        if buffer_len + 1 >= self.max_buffer_messages:
            event.flush()
            return

        if sid not in self.session_events:
            self.session_events[sid] = asyncio.Event()
        if sid not in self.session_tasks:
            self.session_tasks[sid] = asyncio.create_task(self._debounce_loop(sid))
        self.session_events[sid].set()

    async def _debounce_loop(self, sid: str):
        event = self.session_events[sid]
        try:
            while True:
                await event.wait()
                event.clear()
                try:
                    await asyncio.sleep(self.debounce_interval)
                except asyncio.CancelledError:
                    break
                if event.is_set() and not self.receive_unmentioned:
                    continue
                buffer_len = self.ctx.message_processor.get_session_buffer_length(sid)
                if buffer_len == 0:
                    continue
                try:
                    await self.ctx.message_processor.flush_session_messages(sid)
                except Exception:
                    logger.exception(f"[Debounce] Error flushing session {sid}")
        except asyncio.CancelledError:
            # 任务被取消时正常退出，无需额外处理
            logger.debug(f"[Debounce] Debounce loop for session {sid} cancelled")
        finally:
            # 清理会话相关的资源
            self.session_tasks.pop(sid, None)
            self.session_events.pop(sid, None)

    @on.llm_request(priority=Priority.MEDIUM)
    async def inject_group_prompt(self, event: KiraMessageBatchEvent, req: LLMRequest, *_):
        if not event.is_group_message():
            return
        if self.group_chat_prompt:
            for p in req.system_prompt:
                if p.name == "chat_env":
                    p.content += self.group_chat_prompt
                    break

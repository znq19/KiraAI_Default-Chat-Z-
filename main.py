import asyncio
import base64
import io
import os
import random
import wave
from typing import Optional

from core.plugin import BasePlugin, logger, on, Priority
from core.chat.message_utils import KiraMessageEvent, KiraMessageBatchEvent
from core.provider import LLMRequest
from core.chat.message_elements import Text, Image, Reply, Sticker, Forward, Record


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

        # 图片/表情/转发消息处理配置
        self.image_recognition_only_on_mention = cfg.get("image_recognition_only_on_mention", True)
        self.image_recognition_probability = float(cfg.get("image_recognition_probability", 0.5))
        self.max_images_per_message = int(cfg.get("max_images_per_message", 3))
        self.forward_recognition_only_on_mention = cfg.get("forward_recognition_only_on_mention", True)

        # 语音消息处理配置
        self.voice_recognition_only_on_mention = cfg.get("voice_recognition_only_on_mention", True)
        self.voice_private_need_mention = cfg.get("voice_private_need_mention", True)  # 私聊是否需要@/回复
        self.voice_max_duration = int(cfg.get("voice_max_duration", 0))

    async def initialize(self):
        logger.info(f"[Debounce] enabled (group media/forward/voice control, private unchanged)")

    async def terminate(self):
        for sid, task in list(self.session_tasks.items()):
            if not task.done():
                task.cancel()
        if self.session_tasks:
            await asyncio.gather(*self.session_tasks.values(), return_exceptions=True)
        self.session_tasks.clear()
        self.session_events.clear()
        logger.debug("[Debounce] All debounce tasks cancelled")

    # MP3 码率表（kbps）：MPEG1 Layer III / MPEG2&2.5 Layer III
    _MP3_BR_V1 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
    _MP3_BR_V2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]

    def _record_bytes(self, elem) -> Optional[bytes]:
        """尽力取出语音的原始字节（url 不做同步下载，返回 None）"""
        try:
            ft = getattr(elem, "file_type", "")
            if ft == "base64":
                return base64.b64decode(elem.file)
            if ft == "data_url":
                _, _, b64 = elem.file.partition(",")
                return base64.b64decode(b64) if b64 else None
            if ft == "path" and os.path.exists(elem.file):
                if os.path.getsize(elem.file) <= 50 * 1024 * 1024:
                    with open(elem.file, "rb") as f:
                        return f.read()
            return None
        except Exception:
            return None

    def _estimate_mp3_duration(self, data: bytes) -> int:
        """按第一个有效帧头的码率估算 MP3 时长（秒），失败返回 0"""
        try:
            offset = 0
            if data[:3] == b"ID3" and len(data) >= 10:
                tag_size = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) \
                    | ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
                offset = 10 + tag_size
            limit = min(len(data) - 4, offset + 65536)
            i = offset
            while i < limit:
                if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:
                    version = (data[i + 1] >> 3) & 0x03
                    layer = (data[i + 1] >> 1) & 0x03
                    br_idx = (data[i + 2] >> 4) & 0x0F
                    if layer == 1 and version in (0, 2, 3) and br_idx not in (0, 15):
                        table = self._MP3_BR_V1 if version == 3 else self._MP3_BR_V2
                        br = table[br_idx]
                        if br:
                            return round(len(data) * 8 / (br * 1000))
                    i += 1
                else:
                    i += 1
            return 0
        except Exception:
            return 0

    def _estimate_record_duration(self, elem) -> int:
        """Record 缺少 duration 元数据时尽力估算时长（秒），失败返回 0。

        典型场景：机器人自己发出的语音被用户引用回来时不带 duration，
        导致长语音限制被绕过。QQ 适配器会把语音统一转成 mp3 base64，
        本地 TTS 文件多为 wav，二者都可估算。
        """
        data = self._record_bytes(elem)
        if not data:
            return 0
        try:
            if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
                with wave.open(io.BytesIO(data)) as wf:
                    rate = wf.getframerate()
                    return round(wf.getnframes() / rate) if rate else 0
            return self._estimate_mp3_duration(data)
        except Exception:
            return 0

    def _get_record_duration(self, elem) -> int:
        """优先读元数据 duration；缺失时从音频字节估算（如被引用的机器人自己的语音）"""
        try:
            duration = int(float(getattr(elem, "duration", 0) or 0))
        except (TypeError, ValueError):
            duration = 0
        if duration <= 0:
            duration = self._estimate_record_duration(elem)
        return duration

    def _process_media(self, chain, is_mentioned: bool, is_private: bool = False):
        """处理消息链中的图片、动画表情、合并转发消息和语音"""
        for i, elem in enumerate(chain.message_list):
            if isinstance(elem, (Image, Sticker)):
                if is_mentioned:
                    continue
                if self.image_recognition_only_on_mention:
                    chain.message_list[i] = Text("[图片]" if isinstance(elem, Image) else "[动画表情]")
                else:
                    if random.random() >= self.image_recognition_probability:
                        chain.message_list[i] = Text("[图片]" if isinstance(elem, Image) else "[动画表情]")
            elif isinstance(elem, Forward):
                if is_mentioned:
                    if self.forward_recognition_only_on_mention:
                        continue
                    else:
                        chain.message_list[i] = Text("[转发消息]")
                else:
                    chain.message_list[i] = Text("[转发消息]")
            elif isinstance(elem, Record):
                # 语音消息处理
                duration = self._get_record_duration(elem)
                # 长语音限制
                if self.voice_max_duration > 0 and duration > self.voice_max_duration:
                    chain.message_list[i] = Text(f"[长语音 {duration}秒]")
                    continue

                # 决定是否尝试识别语音
                should_try_stt = False
                if is_private:
                    # 私聊：根据 voice_private_need_mention 判断是否需要提及
                    if self.voice_private_need_mention:
                        should_try_stt = is_mentioned
                    else:
                        should_try_stt = True
                else:
                    # 群聊：根据 voice_recognition_only_on_mention 判断
                    if self.voice_recognition_only_on_mention:
                        should_try_stt = is_mentioned
                    else:
                        should_try_stt = True

                if should_try_stt:
                    try:
                        stt_client = self.ctx.provider_mgr.get_default_stt()
                        if stt_client:
                            # 保留原始语音元素，由框架后续识别
                            pass
                        else:
                            chain.message_list[i] = Text("[语音]")
                    except Exception:
                        chain.message_list[i] = Text("[语音]")
                else:
                    chain.message_list[i] = Text("[语音]")
            elif isinstance(elem, Reply) and elem.chain:
                self._process_media(elem.chain, is_mentioned, is_private)

    def _limit_media_count(self, chain, max_count: int):
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
        # 检查唤醒词
        for m in event.message.chain:
            if isinstance(m, Text) and any(w in m.text for w in self.waking_words):
                event.message.is_mentioned = True
                break

        if event.is_group_message():
            is_mentioned = event.is_mentioned
            self._process_media(event.message.chain, is_mentioned, is_private=False)
            if not is_mentioned and not self.image_recognition_only_on_mention:
                self._limit_media_count(event.message.chain, self.max_images_per_message)
        else:
            # 私聊
            is_mentioned = event.is_mentioned
            self._process_media(event.message.chain, is_mentioned, is_private=True)
            # 私聊中不需要限制图片数量（因为一对一）

        if not event.is_mentioned:
            if self.receive_unmentioned:
                buffer = self.ctx.get_buffer(str(event.session))
                if buffer.get_length() >= self.max_unmentioned_messages:
                    buffer.pop(count=buffer.get_length()-self.max_unmentioned_messages+1)
                event.buffer()
                if self.group_proactive_chat and not event.is_group_message():
                    # 主动回复仅支持群聊
                    pass
                elif self.group_proactive_chat and event.is_group_message():
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
            logger.debug(f"[Debounce] Debounce loop for session {sid} cancelled")
        finally:
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

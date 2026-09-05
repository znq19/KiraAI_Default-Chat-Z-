import asyncio
import base64
import io
import json
import os
import random
import sys
import time
import wave
from typing import Optional

# 插件管理器用 spec_from_file_location 加载 main.py，不会把插件目录加入 sys.path；
# 显式加入以便导入同目录的 queue_merge 模块（独立插件部署必需）
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

# 热重载只重新 import main.py，sys.modules 里缓存的同目录模块不会更新；
# 强制重载，避免改了 queue_merge / media_recognize / chat_enhance 后热重载不生效
import importlib
for _m in ("queue_merge", "media_recognize", "chat_enhance"):
    if _m in sys.modules:
        try:
            importlib.reload(sys.modules[_m])
        except Exception:
            pass

from core.plugin import BasePlugin, logger, on, Priority, register
from core.chat.message_utils import KiraMessageEvent, KiraMessageBatchEvent
from core.provider import LLMRequest
from core.chat.message_elements import Text, Image, Reply, Sticker, Forward, Record
from queue_merge import BatchMergeScheduler
from media_recognize import ParallelMediaRecognizer
from chat_enhance import ChatEnhanceEngine, _safe_int, _safe_float


class DebouncePlugin(BasePlugin):
    def _migrate_flat_config(self, cfg: dict):
        """首次更新自动迁移：把旧版扁平键迁移为新版分组（section）结构。

        v1.7.0 起 z 版配置项从扁平改为分组模式（与 s 版一致）。老用户升级后，
        其配置文件里仍是扁平键（如 presence_window_size），且新 section 已被
        框架填入默认值。此函数检测到旧扁平键时，将其值搬入对应 section，
        删除旧的扁平键，写回配置文件。通过 cfg 内的 config_version 标记确保只迁一次。
        """
        if cfg.get("config_version", 1) >= 2:
            return
        # 扁平键 → section 的映射（与 schema.json 分组一致）
        _section_map = {
            "section_basic": ["waking_words", "receive_unmentioned", "max_unmentioned_messages",
                              "merge_window_seconds", "group_chat_prompt", "group_proactive_chat",
                              "group_proactive_chat_probability", "proactive_scope_sessions",
                              "enable_manage_ignore", "proactive_score_gate_deny",
                              "proactive_score_gate_boost", "proactive_k_prob_enabled"],
            "section_media": ["image_recognition_only_on_mention", "image_recognition_probability",
                              "max_images_per_message", "forward_recognition_only_on_mention",
                              "voice_recognition_only_on_mention", "voice_private_need_mention",
                              "voice_max_duration"],
            "section_presence": ["presence_window_size", "presence_decay_minutes", "presence_target_ratio",
                                 "presence_k_min", "presence_k_max", "idle_bonus_score", "force_suppress",
                                 "score_threshold", "score_increment", "score_penalty", "score_cap",
                                 "idle_bonus_ratio"],
            "section_dm_presence": ["dm_presence_enabled", "dm_presence_window_size", "dm_presence_target_ratio",
                                    "dm_presence_k_min", "dm_presence_k_max", "dm_score_threshold",
                                    "dm_score_increment", "dm_score_penalty", "dm_score_cap",
                                    "dm_idle_bonus_score", "dm_idle_bonus_ratio"],
            "section_poke": ["poke_enabled", "poke_window_seconds", "poke_threshold", "poke_default_duration",
                             "poke_allow_bot_duration", "poke_max_duration", "poke_scope"],
            "section_at": ["at_enabled", "at_window_seconds", "at_threshold", "at_default_duration",
                           "at_allow_bot_duration", "at_max_duration", "at_scope"],
            "section_keyword": ["keyword_enabled", "keyword_window_seconds", "keyword_threshold",
                                "keyword_default_duration", "keyword_allow_bot_duration",
                                "keyword_max_duration", "keyword_scope"],
            "section_reply": ["reply_enabled", "reply_window_seconds", "reply_threshold",
                              "reply_default_duration", "reply_allow_bot_duration", "reply_max_duration",
                              "reply_scope"],
            "section_dormant": ["dormant_ranges", "dormant_wake_probability", "wake_keep_mode",
                                "wake_keep_seconds", "wake_max_rounds", "wake_max_extensions",
                                "dormant_scope_sessions", "dormant_whitelist_users", "dormant_whitelist_sessions"],
            "section_harass_scope": ["harass_scope_sessions", "harass_whitelist_users", "harass_whitelist_sessions"],
        }
        migrated = 0
        for sec, keys in _section_map.items():
            section_cfg = cfg.setdefault(sec, {})
            # 框架生成的 section 是扁平 dict（{key: default}）；若已有 "fields" 包装（s 版风格）则用之
            if isinstance(section_cfg, dict) and "fields" in section_cfg and isinstance(section_cfg["fields"], dict):
                fields = section_cfg["fields"]
            else:
                # 无 fields 包装：直接把 section 当扁平 dict 用
                fields = section_cfg
                if not isinstance(fields, dict):
                    fields = {}
                    cfg[sec] = fields
            for key in keys:
                if key in cfg:
                    fields[key] = cfg[key]
                    del cfg[key]
                    migrated += 1
        cfg["config_version"] = 2
        if migrated > 0:
            logger.info(f"[Debounce] 检测到旧版扁平配置，已自动迁移 {migrated} 个配置项到分组结构（config_version=2）")
            try:
                from core.utils.path_utils import get_config_path
                _cfg_path = get_config_path() / "plugins" / "default-chat（z）.json"
                if _cfg_path.parent.exists():
                    # 安全写入：先序列化生成字符串（不碰原文件），成功后再原子替换，
                    # 任何异常都保证原配置文件不被清空（旧实现 open("w") 先截断再 dump，失败即清空）
                    _content = json.dumps(cfg, indent=4, ensure_ascii=False)
                    _tmp = _cfg_path.with_suffix(".json.tmp")
                    with open(_tmp, "w", encoding="utf-8") as f:
                        f.write(_content)
                    _tmp.replace(_cfg_path)
                    logger.info(f"[Debounce] 迁移配置已写回: {_cfg_path}")
            except Exception as e:
                logger.warning(f"[Debounce] 迁移写回配置文件失败（不影响本次运行）: {e}")
        else:
            # 没有旧扁平键（已是新结构），仍需标记版本
            cfg["config_version"] = 2

    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        self._migrate_flat_config(cfg)
        self.session_events: dict[str, asyncio.Event] = {}
        self.session_tasks: dict[str, asyncio.Task] = {}
        # 消息批次状态（"前文 + 批次"模型）：
        # - batch_started[sid]: 会话是否已出现首个唤醒消息（批次已开始）
        # - batch_count[sid]: 从批次首个唤醒消息起进入 buffer 的消息数（含唤醒本身）
        #   达到 max_buffer_messages 即满即推；批次内唤醒/普通消息一视同仁（不重置）
        self.batch_started: dict[str, bool] = {}
        self.batch_count: dict[str, int] = {}
        bot_cfg = ctx.config["bot_config"].get("bot", {})
        self.debounce_interval = _safe_float(bot_cfg.get("max_message_interval"), 1.5)
        self.max_buffer_messages = _safe_int(bot_cfg.get("max_buffer_messages"), 3)
        # === 分组读取辅助：从 section 结构取值（兼容旧扁平残留） ===
        def _sec(section_name, key, default):
            _sec_cfg = self.plugin_cfg.get(section_name, {}) or {}
            if isinstance(_sec_cfg, dict) and "fields" in _sec_cfg and isinstance(_sec_cfg["fields"], dict):
                # s 版风格：{key: {...}, fields: {child: {...}}}
                _fields = _sec_cfg["fields"]
            else:
                # 扁平 section：直接查键
                _fields = _sec_cfg
            if not isinstance(_fields, dict):
                return default
            return _fields.get(key, default)
        _basic = lambda k, d: _sec("section_basic", k, d)
        _media = lambda k, d: _sec("section_media", k, d)
        _pres = lambda k, d: _sec("section_presence", k, d)
        _dmp = lambda k, d: _sec("section_dm_presence", k, d)
        _dorm = lambda k, d: _sec("section_dormant", k, d)
        _hscope = lambda k, d: _sec("section_harass_scope", k, d)
        # 消息合并间隔顺延（默认 -1=自动取框架值）
        _mws = _basic("merge_window_seconds", -1)
        if _mws is None or _mws == -1:
            self.merge_window_seconds = self.debounce_interval
        elif _mws == 0:
            self.merge_window_seconds = 0
        else:
            self.merge_window_seconds = float(_mws)
        # 顺延调试日志开关（section_basic.debug_log_enabled）
        self._merge_debug = _basic("debug_log_enabled", False)
        self.max_unmentioned_messages = _safe_int(_basic("max_unmentioned_messages", 5), 5)
        self.receive_unmentioned = _basic("receive_unmentioned", False)
        self.group_chat_prompt = _basic("group_chat_prompt", '### 群聊环境说明\r\n\r\n当前为群聊环境，你需要聚焦于**和你有直接关联**或**你十分感兴趣**的消息，对于仅显示为[动画表情]或[图片]的消息不用互动，注意不要刷屏，可以选择不回复任何消息，直接输出<msg/>即可。\r\n\r\n## 消息感知\r\n\r\n你可能会同时收到多条消息，请根据上下文自主决策该回复哪些消息，注意不要刷屏，也可以选择不回复任何消息，直接输出<msg/>即可。\r\n你可以使用 <reasoning>reasoning_content</reasoning> 的标签格式来输出推理内容放在整个输出的最前面，用于推理应该回复哪些消息，回复语气，回复条数，消息分段情况等。\r\n<reasoning>标签和<msg>标签同级，**禁止**将次标签放到<msg>标签内。\r\n**符合以上规则的情况下**确保你想发的聊天消息在<text>标签内，不要遗漏。\r\n')
        self.group_proactive_chat = _basic("group_proactive_chat", False)
        self.group_proactive_chat_probability = _safe_float(_basic("group_proactive_chat_probability", 0.1), 0.1)
        self.proactive_k_prob_enabled = _basic("proactive_k_prob_enabled", True)
        self.proactive_scope_sessions = set(
            str(x) for x in (_basic("proactive_scope_sessions", []) or [])
        )
        # 主动屏蔽工具开关（manage_ignore）：关闭后 bot 不再能主动屏蔽骚扰
        self.enable_manage_ignore = _basic("enable_manage_ignore", True)

        self.waking_words = _basic("waking_words", [])

        # 图片/表情/转发消息处理配置
        self.image_recognition_only_on_mention = _media("image_recognition_only_on_mention", True)
        self.image_recognition_probability = _safe_float(_media("image_recognition_probability", 1.0), 1.0)
        self.max_images_per_message = _safe_int(_media("max_images_per_message", 3), 3)
        self.forward_recognition_only_on_mention = _media("forward_recognition_only_on_mention", True)

        # 语音消息处理配置
        self.voice_recognition_only_on_mention = _media("voice_recognition_only_on_mention", True)
        self.voice_private_need_mention = _media("voice_private_need_mention", True)  # 私聊是否需要@/回复
        self.voice_max_duration = _safe_int(_media("voice_max_duration", 0), 0)

        # 队列合并 / 积压处理（BatchMergeScheduler）
        self.merge_scheduler = BatchMergeScheduler(ctx, self.plugin_cfg, bot_cfg)
        # 并行媒体识别（ParallelMediaRecognizer）
        self.media_recognizer = ParallelMediaRecognizer(ctx, self.plugin_cfg, bot_cfg)

        # ========== 聊天增强引擎（存在感节流/骚扰感知化/休眠状态机/通知合并） ==========
        # z 版 schema 已改为分组模式，从 section 结构读取（与 s 版一致）
        _enhance_cfg = {
            "presence_window_size": _pres("presence_window_size", 20),
            "presence_decay_minutes": _pres("presence_decay_minutes", 10),
            "presence_target_ratio": _pres("presence_target_ratio", 0.3),
            "presence_k_min": _pres("presence_k_min", 0.2),
            "presence_k_max": _pres("presence_k_max", 2.0),
            "idle_bonus_score": _pres("idle_bonus_score", 15),
            "force_suppress": _pres("force_suppress", False),
            "score_gate_deny": _basic("proactive_score_gate_deny", True),
            "score_gate_boost": _basic("proactive_score_gate_boost", True),
            "score_threshold": _pres("score_threshold", 60),
            "score_increment": _pres("score_increment", 1),
            "score_penalty": _pres("score_penalty", 5),
            "score_cap": _pres("score_cap", 100),
            "idle_bonus_ratio": _pres("idle_bonus_ratio", 1.5),
            "dormant_ranges": _dorm("dormant_ranges", []),
            "dormant_wake_probability": _dorm("dormant_wake_probability", 0.3),
            "wake_keep_mode": _dorm("wake_keep_mode", "renew"),
            "wake_keep_seconds": _dorm("wake_keep_seconds", 300),
            "wake_max_rounds": _dorm("wake_max_rounds", -1),
            "wake_max_extensions": _dorm("wake_max_extensions", -1),
            "harass_scope_sessions": _hscope("harass_scope_sessions", []),
            "harass_whitelist_users": _hscope("harass_whitelist_users", []),
            "harass_whitelist_sessions": _hscope("harass_whitelist_sessions", []),
            "dormant_scope_sessions": _dorm("dormant_scope_sessions", []),
            "dormant_whitelist_users": _dorm("dormant_whitelist_users", []),
            "dormant_whitelist_sessions": _dorm("dormant_whitelist_sessions", []),
            # 私聊独立参数
            "dm_presence_enabled": _dmp("dm_presence_enabled", True),
            "dm_presence_window_size": _dmp("dm_presence_window_size", 10),
            "dm_presence_target_ratio": _dmp("dm_presence_target_ratio", 0.7),
            "dm_presence_k_min": _dmp("dm_presence_k_min", 0.5),
            "dm_presence_k_max": _dmp("dm_presence_k_max", 2.0),
            "dm_score_threshold": _dmp("dm_score_threshold", 30),
            "dm_score_increment": _dmp("dm_score_increment", 2),
            "dm_score_penalty": _dmp("dm_score_penalty", 3),
            "dm_score_cap": _dmp("dm_score_cap", 50),
            "dm_idle_bonus_score": _dmp("dm_idle_bonus_score", 15),
            "dm_idle_bonus_ratio": _dmp("dm_idle_bonus_ratio", 1.5),
            # 额外信号（user_msgs/session_msgs 群聊+私聊开关与参数；bot_speech 仅群聊）
            "detect_user_msgs": _sec("section_detect", "detect_user_msgs", False),
            "detect_session_msgs": _sec("section_detect", "detect_session_msgs", False),
            "detect_bot_speech": _sec("section_detect", "detect_bot_speech", False),
            "dm_detect_user_msgs": _sec("section_detect", "dm_detect_user_msgs", False),
            "dm_detect_session_msgs": _sec("section_detect", "dm_detect_session_msgs", False),
            "user_msgs_window_seconds": _sec("section_thresholds", "user_msgs_window_seconds", 60),
            "user_msgs_threshold": _sec("section_thresholds", "user_msgs_threshold", 10),
            "session_msgs_window_seconds": _sec("section_thresholds", "session_msgs_window_seconds", 60),
            "session_msgs_threshold": _sec("section_thresholds", "session_msgs_threshold", 20),
            "bot_speech_window_seconds": _sec("section_thresholds", "bot_speech_window_seconds", 300),
            "bot_speech_threshold": _sec("section_thresholds", "bot_speech_threshold", 10),
            "dm_user_msgs_window_seconds": _sec("section_thresholds", "dm_user_msgs_window_seconds", 60),
            "dm_user_msgs_threshold": _sec("section_thresholds", "dm_user_msgs_threshold", 10),
            "dm_session_msgs_window_seconds": _sec("section_thresholds", "dm_session_msgs_window_seconds", 60),
            "dm_session_msgs_threshold": _sec("section_thresholds", "dm_session_msgs_threshold", 20),
            "extra_default_duration": _sec("section_thresholds", "extra_default_duration", 180),
        }
        for _kind in ("poke", "at", "keyword", "reply"):
            _pk = _sec(f"section_{_kind}", "enabled", False)
            _enhance_cfg[f"section_{_kind}"] = {
                "enabled": _sec(f"section_{_kind}", "enabled", _kind in ("poke", "at")),
                "window_seconds": _sec(f"section_{_kind}", "window_seconds", 60),
                "threshold": _sec(f"section_{_kind}", "threshold", 3 if _kind != "keyword" else 5),
                "default_duration": _sec(f"section_{_kind}", "default_duration", 180),
                "allow_bot_duration": _sec(f"section_{_kind}", "allow_bot_duration", True),
                "max_duration": _sec(f"section_{_kind}", "max_duration", 300),
                "scope": _sec(f"section_{_kind}", "scope", "per_user"),
            }
        self.enhance = ChatEnhanceEngine(ctx, _enhance_cfg, self, merge_seconds=self.debounce_interval)

    async def initialize(self):
        logger.info(f"[Debounce] enabled (group media/forward/voice control, private unchanged)")
        # 启动聊天增强引擎（存在感/骚扰/休眠/通知合并）
        self.enhance.start()
        # 接管互斥：检测独立防骚扰插件是否已加载，已加载则提示停用（本插件内置同能力）
        try:
            _pm = self.ctx.plugin_mgr
            if _pm is not None:
                _loaded = set()
                try:
                    # 框架 PluginManager 无 get_loaded_plugin_ids，用 list_plugins 取 plugin_id
                    _infos = _pm.list_plugins() if hasattr(_pm, "list_plugins") else []
                    _loaded = set(getattr(i, "plugin_id", "") for i in (_infos or []))
                except Exception:
                    pass
                if any("anti-harass" in str(pid).lower() for pid in _loaded):
                    logger.warning(
                        "[Enhance] 检测到独立防骚扰插件已加载，本插件已内置完整骚扰屏蔽能力，"
                        "建议停用独立防骚扰插件避免重复检测/重复通知"
                    )
        except Exception:
            pass

    async def terminate(self):
        for sid, task in list(self.session_tasks.items()):
            if not task.done():
                task.cancel()
        if self.session_tasks:
            await asyncio.gather(*self.session_tasks.values(), return_exceptions=True)
        self.session_tasks.clear()
        self.session_events.clear()
        # 清理合并调度器（重发 pending + 取消 tick）
        await self.merge_scheduler.shutdown()
        # 关闭聊天增强引擎（await 等待 prune 任务退出）
        await self.enhance.shutdown()
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

    # ========== 骚扰屏蔽 XML tag（戳/at/关键词/引用） ==========

    @register.tag(name="wake_extend", description="休眠唤醒后主动续窗。输出 <wake_extend>yes</wake_extend> 延长维持期（受 wake_max_extensions 限制）。")
    async def handle_wake_extend(self, value: str, **kwargs) -> list:
        try:
            sid = self._last_ignore_sid
        except AttributeError:
            sid = None
        if sid is None:
            return []
        if (value or "").strip().lower() == "yes":
            result = self.enhance.dormant.extend(sid, __import__("time").time())
            if result:
                logger.info(f"[Enhance] 主动续窗: {result}")
        return []

    @register.tag(name="poke_ignore", description="屏蔽戳一戳骚扰。输出 <poke_ignore>user|duration:N</poke_ignore> 屏蔽目标用户，<poke_ignore>all|duration:N</poke_ignore> 屏蔽所有用户，<poke_ignore>none</poke_ignore> 不屏蔽。duration 为秒，留空用默认值。")
    async def handle_poke_ignore(self, value: str, **kwargs) -> list:
        return self._apply_ignore_tag("poke", value)

    @register.tag(name="ignore", description="拉黑用户：屏蔽后该用户/会话的所有消息不再进入（含戳一戳/at/关键词/引用/刷屏）。输出 <ignore>user|duration:N</ignore> 拉黑目标用户，<ignore>all|duration:N</ignore> 拉黑所有用户，<ignore>none</ignore> 不屏蔽。duration 为秒，留空用默认值。")
    async def handle_ignore(self, value: str, **kwargs) -> list:
        return self._apply_ignore_tag("all", value)

    def _apply_ignore_tag(self, kind: str, value: str) -> list:
        """解析骚扰屏蔽 tag 值并执行屏蔽。返回空列表（tag 不产生消息输出）。"""
        try:
            sid = self._last_ignore_sid
        except AttributeError:
            sid = None
        if sid is None:
            return []
        result = self.enhance.harass.apply_ignore_from_tag(sid, kind, value)
        if result:
            logger.info(f"[Enhance] {kind} 屏蔽: {result}")
        return []

    @register.tool(
        name="manage_ignore",
        description="管理骚扰屏蔽：屏蔽某个用户/会话/某种唤醒方式，或提前解除屏蔽。bot 觉得被骚扰、或人设要求时调用。",
        params={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["block", "unblock", "list"],
                    "description": "block=屏蔽，unblock=解除屏蔽，list=查看当前屏蔽列表",
                },
                "target_type": {
                    "type": "string",
                    "enum": ["user", "session", "all"],
                    "description": "屏蔽对象：user=某个用户，session=某个会话，all=全局（所有用户所有会话）",
                },
                "target_id": {
                    "type": "string",
                    "description": "目标 ID：target_type=user 时是用户 ID，=session 时是会话 ID，=all 时留空",
                },
                "block_type": {
                    "type": "string",
                    "enum": ["poke", "all"],
                    "description": "屏蔽类型：poke=只屏蔽戳一戳（其他形式正常），all=拉黑（该用户/会话所有消息不再进入，含戳一戳）",
                    "default": "all",
                },
                "duration": {
                    "type": "integer",
                    "description": "屏蔽时长（秒）。留空用默认值；-1 表示永久",
                    "default": 0,
                },
            },
            "required": ["action", "target_type"],
        },
    )
    async def manage_ignore(self, event, action: str, target_type: str, target_id: str = "",
                            block_type: str = "all", duration: int = 0) -> str:
        """bot 主动管理骚扰屏蔽。"""
        try:
            sid = str(event.session.sid)
        except Exception:
            sid = str(getattr(event, "sid", ""))
        if action == "list":
            return self.enhance.harass.list_ignored(sid)
        if action == "unblock":
            if target_type == "all":
                return "请指定要解除的用户或会话"
            result = self.enhance.harass.unblock(sid, target_id, block_type)
            logger.info(f"[Enhance] 解除屏蔽(工具): {target_type} {target_id} {block_type} → {result}")
            return result
        # block
        if target_type == "all":
            result = self.enhance.harass.apply_ignore("*", "*", block_type, duration)
            logger.info(f"[Enhance] 屏蔽(工具): all {block_type} {duration}s → {result}")
            return result
        if target_type == "session":
            result = self.enhance.harass.apply_ignore(sid, "*", block_type, duration)
            logger.info(f"[Enhance] 屏蔽(工具): session {sid} {block_type} {duration}s → {result}")
            return result
        result = self.enhance.harass.apply_ignore(sid, target_id, block_type, duration)
        logger.info(f"[Enhance] 屏蔽(工具): user {target_id} {block_type} {duration}s → {result}")
        return result

    def _is_proactive_allowed(self, sid: str) -> bool:
        """群聊积极概率作用域检查：scope 非空时仅这些会话生效（空=全部）。"""
        if not self.proactive_scope_sessions:
            return True
        return sid in self.proactive_scope_sessions

    def _update_tool_hint(self, req):
        """动态更新 manage_ignore 工具描述：把 duration 默认时长从配置读出（不写死）。

        框架在 ON_LLM_REQUEST 钩子后会重新 request.tools = tool_set.to_list()，
        因此修改工具实例的参数描述会真实生效；实例被 tool_mgr 持有，配置热重载
        后每次请求自动反映最新值。
        """
        try:
            tool_set = getattr(req, "tool_set", None)
            if not tool_set:
                return
            tool = tool_set.get("manage_ignore")
            if tool is None:
                return
            try:
                default_d = self.enhance.harass._conf.get("poke", {}).get("default_duration", 180)
            except Exception:
                default_d = 180
            params = getattr(tool, "parameters", None)
            if not isinstance(params, dict):
                return
            dur = params.get("properties", {}).get("duration")
            if isinstance(dur, dict):
                dur["description"] = (
                    f"屏蔽时长（秒）。留空/0=用默认时长（当前配置 {default_d} 秒）；-1 表示永久"
                )
        except Exception:
            pass

    def _filter_tools(self, tool_set, blacklist, mode: str):
        """按黑名单过滤 ToolSet（与 s 版一致：BaseTool 实例，tool.name + remove）。"""
        if not blacklist or not tool_set or not getattr(tool_set, "tools", None):
            return
        to_remove: list[str] = []
        for tool in list(tool_set.tools):
            name = getattr(tool, "name", None) or ""
            if not name:
                continue
            if mode == "partial":
                if any(kw in name for kw in blacklist):
                    to_remove.append(name)
            else:
                if name in blacklist:
                    to_remove.append(name)
        if to_remove:
            tool_set.remove(*to_remove)
            logger.debug(f"[Enhance] 已从 tool_set 移除工具: {to_remove}")

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
    async def handle_msg(self, event: KiraMessageEvent, *_):
        # === 拉黑拦截：被屏蔽的用户/会话消息完全不进 LLM（不 buffer/flush/不触发） ===
        try:
            _sid = event.session.sid
            _uid = str(event.message.sender.user_id) if event.message.sender else "unknown"
            if self.enhance.harass.is_blocked(_sid, _uid, time.time()):
                logger.debug(f"[Enhance] 拉黑拦截: {_sid} 用户 {_uid} 的消息不进 LLM")
                event.discard()
                return
        except Exception:
            pass

        # === poke 屏蔽拦截：poke 单独屏蔽不拉黑普通消息（is_blocked 跳过 poke），
        #     但戳一戳事件本身要精确拦截：被屏蔽用户的 poke 事件不进 LLM ===
        try:
            if self.enhance._detect_kind(event) == "poke" and \
               self.enhance.harass.is_ignored(_sid, _uid, "poke", time.time()):
                logger.debug(f"[Enhance] poke屏蔽拦截: {_sid} 用户 {_uid} 的戳一戳不进 LLM")
                event.discard()
                return
        except Exception:
            pass

        # 检查唤醒词（区分真 @ 与唤醒词命中：框架在循环前已标记真 @）
        _was_mentioned = bool(getattr(event, "is_mentioned", False))
        for m in event.message.chain:
            if isinstance(m, Text) and any(w in m.text for w in self.waking_words):
                event.message.is_mentioned = True
                if not _was_mentioned:
                    event._wake_source = "keyword"
                break
        if _was_mentioned:
            event._wake_source = "at"

        sid = event.session.sid

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

        # === 聊天增强引擎：存在感记录 + 骚扰检测 + 休眠判定 ===
        # 必须在未提及分支之前调用：未提及消息也要统计存在感/骚扰/休眠
        self.enhance.on_im_message(event)

        # 评分补正对提及消息的影响（存在感节流下独立控制）
        if event.is_mentioned and not self.enhance.dormant.in_dormant(self.enhance._now_hhmm(), sid):
            is_dm = not event.is_group_message()
            scope = "mentioned_dm" if is_dm else "mentioned"
            mentioned_gate = self.enhance.score_gate(sid, True, scope=scope, is_dm=is_dm)
            if not mentioned_gate:
                event.is_mentioned = False

        # 休眠期内起夜未命中：抑制触发（不推送 LLM）
        if getattr(event, "_enhance_dormant_blocked", False):
            event.discard()
            return
        # 强制通路超额抑制：占比超标且评分不足时，被唤醒也抑制（等评分补上）
        if getattr(event, "_enhance_force_suppressed", False):
            event.discard()
            return

        if not event.is_mentioned:
            if self.receive_unmentioned:
                _batch_on = self.batch_started.get(sid, False)
                if not _batch_on:
                    # 前文阶段（批次未开始）：维持前文上限，弹最老前文
                    buffer = self.ctx.get_buffer(str(event.session))
                    if buffer.get_length() >= self.max_unmentioned_messages:
                        buffer.pop(count=buffer.get_length()-self.max_unmentioned_messages+1)
                # 批次已开始：不裁剪（批次内消息只进不出，直到满即推/顺延到点）
                event.buffer()
                if _batch_on:
                    # 批次计数 +1，满即推检查
                    self.batch_count[sid] = self.batch_count.get(sid, 0) + 1
                    if self.max_buffer_messages > 0 and self.batch_count[sid] >= self.max_buffer_messages:
                        event.flush()
                        # 批次已满即推，清理批次状态（下一批从 0 开始）
                        self.batch_started.pop(sid, None)
                        self.batch_count.pop(sid, None)
                        return
                # 顺延进行中：非唤醒消息也重置计时器（最后一条消息到达后 N 秒无新消息才 flush）
                # —— 仅当批次已开启（有唤醒/命中）时才允许重置顺延：无唤醒来历的非唤醒
                #    消息绝不能启动顺延/flush（否则开了次 LLM 后所有围观消息都进批次）
                if _batch_on and sid in self.session_events and sid in self.session_tasks:
                    self.session_events[sid].set()
                _psid = sid
                if self.group_proactive_chat and not event.is_group_message():
                    # 主动回复仅支持群聊
                    pass
                elif self.group_proactive_chat and event.is_group_message() \
                        and not self.enhance.dormant.in_dormant(self.enhance._now_hhmm(), _psid) \
                        and self._is_proactive_allowed(_psid):
                    # 存在感节流：概率 × k_prob（回少提高/回多降低）
                    prob = self.group_proactive_chat_probability
                    _kf = 1.0
                    if self.proactive_k_prob_enabled:
                        _kf = self.enhance.k_prob(_psid)
                        prob *= _kf
                    _rand = random.random()
                    prob_hit = _rand < prob
                    _gate = self.enhance.score_gate(_psid, prob_hit)
                    logger.debug(
                        f"[Sustain] 群 {_psid} 积极概率判定: 概率 {self.group_proactive_chat_probability:.2f}"
                        f"{'×k_prob ' + f'{_kf:.2f}' if self.proactive_k_prob_enabled else ''}"
                        f"→ 有效 {prob:.3f}, 随机 {_rand:.3f}, 概率命中={prob_hit}, "
                        f"评分门={_gate} → {'触发' if _gate else '未触发'}"
                    )
                    # 评分补正：评分不足概率命中作废；评分够概率未命中补触发
                    if _gate:
                        logger.info("[Chat] Triggered proactive chat")
                        event.flush()
            else:
                event.discard()
            return

        # === 唤醒消息：启动/延续批次 ===
        event.buffer()
        if not self.batch_started.get(sid, False):
            # 首个唤醒消息：批次开始，计数从 1（含唤醒本身）
            self.batch_started[sid] = True
            self.batch_count[sid] = 1
        else:
            # 批次中的唤醒消息：只当普通消息计数，不重置批次
            self.batch_count[sid] = self.batch_count.get(sid, 0) + 1
        # 满即推：批次计数达到 max_buffer_messages
        if self.max_buffer_messages > 0 and self.batch_count[sid] >= self.max_buffer_messages:
            event.flush()
            # 批次已满即推，清理批次状态（下一批从 0 开始）
            self.batch_started.pop(sid, None)
            self.batch_count.pop(sid, None)
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
                if self.merge_window_seconds > 0:
                    # 消息合并间隔顺延：新消息到达时重置计时器
                    if self._merge_debug:
                        logger.info(f"[Debounce] 顺延开始 session={sid}, 窗口={self.merge_window_seconds}s")
                    remaining = self.merge_window_seconds
                    while remaining > 0:
                        try:
                            await asyncio.wait_for(event.wait(), timeout=remaining)
                            event.clear()
                            if self._merge_debug:
                                logger.info(f"[Debounce] 顺延重置 session={sid}（新消息到达，重新等待 {self.merge_window_seconds}s）")
                            remaining = self.merge_window_seconds
                        except asyncio.TimeoutError:
                            break
                        # 满即推安全阀：顺延等待中批次计数达到 max_buffer_messages，提前结束顺延
                        if self.max_buffer_messages > 0 and self.batch_count.get(sid, 0) >= self.max_buffer_messages:
                            if self._merge_debug:
                                logger.info(
                                    f"[Debounce] 顺延提前结束（批次 {self.batch_count.get(sid, 0)} 条 ≥ 上限 {self.max_buffer_messages}）: {sid}"
                                )
                            break
                else:
                    # 0 = 不启用顺延，固定间隔 flush（框架原行为）
                    try:
                        await asyncio.sleep(self.debounce_interval)
                    except asyncio.CancelledError:
                        break
                if event.is_set() and not self.receive_unmentioned:
                    continue
                buffer_len = self.ctx.message_processor.get_session_buffer_length(sid)
                if buffer_len == 0:
                    # 批次已被外部消费（框架/其他插件在顺延窗口内 flush 了批次）：
                    # 清理批次状态防止残留 —— 否则 batch_started 遗留 True 会让
                    # 后续非唤醒消息被误当作"批次内消息"重置顺延 → flush 进 LLM
                    self.batch_started.pop(sid, None)
                    self.batch_count.pop(sid, None)
                    continue
                # 保险丝：flush 只发生在"批次由唤醒/持续命中开启"时；无唤醒来历
                # （纯围观消息）不 flush，只留作前文等下次真唤醒。防任何路径误触发。
                if not self.batch_started.get(sid, False):
                    if self._merge_debug:
                        logger.debug(
                            f"[Debounce] 未检测到唤醒来历批次，跳过 flush（前文保留）: {sid}"
                        )
                    continue
                if self._merge_debug:
                    logger.info(f"[Debounce] 顺延结束 session={sid}（{self.merge_window_seconds}s 无新消息），flush {buffer_len} 条")
                try:
                    await self.ctx.message_processor.flush_session_messages(sid)
                except Exception:
                    logger.exception(f"[Debounce] Error flushing session {sid}")
                # 批次已随 flush 送出，清理批次状态（下一批从 0 开始）
                self.batch_started.pop(sid, None)
                self.batch_count.pop(sid, None)
        except asyncio.CancelledError:
            logger.debug(f"[Debounce] Debounce loop for session {sid} cancelled")
        finally:
            self.session_tasks.pop(sid, None)
            self.session_events.pop(sid, None)
            self.batch_started.pop(sid, None)
            self.batch_count.pop(sid, None)

    @on.llm_request(priority=Priority.MEDIUM)
    async def inject_group_prompt(self, event: KiraMessageBatchEvent, req: LLMRequest, *_):
        # 聊天增强引擎：注入合并通知（骚扰/唤醒/存在感状态）
        self.enhance.on_llm_request(event, req)
        # 动态工期说明：manage_ignore 的 duration 默认值取自当前配置（不写死）
        self._update_tool_hint(req)
        # 主动屏蔽工具开关：关闭时从 tool_set 移除 manage_ignore
        if not self.enable_manage_ignore:
            self._filter_tools(req.tool_set, ["manage_ignore"], "exact")
            logger.debug("[Enhance] manage_ignore 工具已禁用（enable_manage_ignore=false）")
        if not event.is_group_message():
            return
        if self.group_chat_prompt:
            for p in req.system_prompt:
                if p.name == "chat_env":
                    p.content += self.group_chat_prompt
                    break

    # ================= 队列合并 / 积压处理（转发给 BatchMergeScheduler） =================

    @on.im_batch_message(priority=Priority.HIGH)
    async def on_queue_merge_batch(self, event: KiraMessageBatchEvent, *_):
        await self.merge_scheduler.on_batch_message(event)

    @on.llm_response(priority=Priority.HIGH)
    async def on_llm_response_enhance(self, event: KiraMessageBatchEvent, resp, *_):
        # 聊天增强引擎：存在感记录 + 休眠维持期（仅最终文本回复时，工具中间步不记）
        if getattr(resp, "tool_calls", None):
            return
        self.enhance.on_llm_response(event, resp)
        # 休眠维持期次数限制：达上限则结束维持期（wake_max_rounds 生效）
        try:
            sid = str(event.sid)
        except Exception:
            sid = None
        if sid and not self.enhance.dormant.can_reply(sid):
            self.enhance.dormant._awake_until.pop(sid, None)
            logger.debug(f"[Enhance] 休眠维持期达最大互动次数，结束: {sid}")
        # 记录本次 LLM 回复所属会话（ignore/wake_extend tag 处理器用）。
        # 必须在最终文本回复时写：框架 tag 处理器无 event 上下文，_last_ignore_sid
        # 是唯一通道。写入已把竞态窗口缩到最小（on_llm_response 返回后框架才解析
        # XML 执行 tag，多会话并发回复时可能被覆盖，已知限制）。
        if sid:
            self._last_ignore_sid = sid

    @on.llm_response(priority=Priority.HIGH)
    async def on_queue_merge_resp(self, event: KiraMessageBatchEvent, resp, *_):
        await self.merge_scheduler.on_llm_response(event, resp)

    @on.step_result(priority=Priority.HIGH)
    async def on_queue_merge_step(self, event: KiraMessageBatchEvent, *_):
        await self.merge_scheduler.on_step_result(event)

    # ================= 并行媒体识别（转发给 ParallelMediaRecognizer） =================
    # 注意：im_message 钩子必须定义在 handle_msg 之后（同优先级按注册顺序执行），
    #       保证"非唤醒不识别"配置先由 handle_msg 处理（兼容前提）

    @on.im_message(priority=Priority.HIGH)
    async def on_media_rec_im(self, event: KiraMessageEvent, *_):
        await self.media_recognizer.on_im_message(event)

    @on.im_batch_message(priority=Priority.HIGH)
    async def on_media_rec_batch(self, event: KiraMessageBatchEvent, *_):
        await self.media_recognizer.on_im_batch_message(event)

    @on.llm_request(priority=Priority.HIGH)
    async def on_media_rec_llm(self, event: KiraMessageBatchEvent, req: LLMRequest, *_):
        await self.media_recognizer.on_llm_request(event, req)

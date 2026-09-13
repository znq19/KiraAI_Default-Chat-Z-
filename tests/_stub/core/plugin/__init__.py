"""Stub of core.plugin: BasePlugin / logger / on / Priority / register."""
from enum import IntEnum


class Priority(IntEnum):
    SYS_LOW = -100
    LOW = -50
    MEDIUM = 0
    HIGH = 50
    SYS_HIGH = 100


class _Logger:
    def __init__(self):
        self.records = []

    def _emit(self, level, msg):
        self.records.append((level, msg))
        print(f"  [{level}] {msg}")

    def info(self, msg, *a, **k):
        self._emit("INFO", msg)

    def debug(self, msg, *a, **k):
        self._emit("DEBUG", msg)

    def warning(self, msg, *a, **k):
        self._emit("WARN", msg)

    def error(self, msg, *a, **k):
        self._emit("ERROR", msg)

    def exception(self, msg, *a, **k):
        self._emit("EXC", msg)


logger = _Logger()


class _On:
    """Decorators that just return the function (hooks are invoked manually)."""

    def _deco(self, *_a, **_k):
        def inner(func):
            return func
        return inner

    im_message = _deco
    message_buffered = _deco
    im_batch_message = _deco
    llm_request = _deco
    llm_response = _deco
    tool_result = _deco
    after_xml_parse = _deco
    message_sent = _deco
    step_result = _deco
    final_result = _deco
    loaded = _deco
    shutdown = _deco
    custom_event = _deco


on = _On()


class _Register:
    """Stub of core.plugin.register (RegisterDeco) — identity decorators."""

    @staticmethod
    def _deco(*_a, **_k):
        def inner(func):
            return func
        return inner

    tool = _deco
    tag = _deco
    page = _deco
    api = _deco
    ws = _deco
    widget = _deco

    def __call__(self, *_a, **_k):
        return self._deco()


register = _Register()


class BasePlugin:
    def __init__(self, ctx, cfg):
        self.ctx = ctx
        self.cfg = cfg
        self.plugin_cfg = cfg

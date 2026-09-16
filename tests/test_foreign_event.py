"""S/Z 版：外来事件（第三方插件的桩事件）不得被当成主 bot 的一轮 + final_result 兜底。

背景（2026-09-16 实测）：
  子代理插件（KiraAI-subagent-plugin）`_make_stub_event(task.sid)` 造 KiraMessageBatchEvent
  ——**携带发起会话的真实 sid**——并由框架 AgentExecutor 用它派发 ON_LLM_RESPONSE。
  本插件的 `on_llm_response` 会把它当成主 bot 的最终回复：存在感时间线 +1、扣分、
  S 版还会误开/误停持续对话窗口（子代理汇报里出现"再见"就停窗）。

  修法（v2.5.18/v1.8.9）：`ON_LLM_REQUEST` 登记"框架真实批次"（只有框架自己会派发），
  `on_llm_response` 只处理登记过的；桩事件若带显式标记（extra/_subagent_stub）也直接跳过。

另含 QueueMerge 的 `ON_FINAL_RESULT` 兜底推送测试（框架 v2.34.4 起派发）。

用法: python3 tests/test_foreign_event.py [<plugin_dir> ...]
"""
import asyncio
import ast
import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
STUB = HERE / "_stub"
sys.path.insert(0, str(STUB))

from core.chat import Group, KiraIMMessage, MessageChain, Session, User  # noqa: E402
from core.chat.message_elements import Text  # noqa: E402
from core.chat.message_utils import KiraMessageBatchEvent  # noqa: E402

SID = "qq:gm:10001"

_CFG_MAP = {
    "bot_config.agent.max_tool_loop": 2,
    "bot_config.agent.tool_call_timeout": 60,
    "bot_config.bot.max_buffer_messages": 5,
    "bot_config.bot.max_message_interval": 30,
}


class Cfg:
    def get_config(self, key, default=None):
        return _CFG_MAP.get(key, default)

    def __getitem__(self, key):
        if key == "bot_config":
            return {"agent": {"max_tool_loop": 2, "tool_call_timeout": 60},
                    "bot": {"max_buffer_messages": 5, "max_message_interval": 30}}
        return {}


class Bus:
    def __init__(self):
        self.published = []

    async def publish(self, event):
        self.published.append(event)


class Ctx:
    def __init__(self):
        self.config = Cfg()
        self.plugin_mgr = None
        self.session_mgr = None
        self.event_bus = Bus()
        self.buffers = {}

    def get_buffer(self, sid):
        return self.buffers.setdefault(sid, Buf())


class Buf:
    def __init__(self):
        self.buffer = []
        self.lock = asyncio.Lock()

    def get_length(self):
        return len(self.buffer)

    def pop(self, count=1):
        for _ in range(count):
            if self.buffer:
                self.buffer.pop(0)

    def flush(self):
        out = list(self.buffer)
        self.buffer.clear()
        return out


def load_plugin(plugin_dir: Path):
    for m in ("queue_merge", "media_recognize", "chat_enhance"):
        sys.modules.pop(m, None)
    spec = importlib.util.spec_from_file_location(f"plug_fe_{plugin_dir.name}",
                                                  plugin_dir / "main.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    cls = getattr(mod, "DebouncePlugin", None)
    if cls is None:
        raise RuntimeError("DebouncePlugin 未找到")
    return mod, cls(Ctx(), {})


def load_sched(plugin_dir: Path):
    for m in ("queue_merge",):
        sys.modules.pop(m, None)
    spec = importlib.util.spec_from_file_location(f"qm_fe_{plugin_dir.name}",
                                                  plugin_dir / "queue_merge.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def msg(mid, text, mentioned=True):
    return KiraIMMessage(timestamp=time.time(), sender=User("20001", "小明"),
                         group=Group("10001", "测试群"), message_id=str(mid),
                         self_id="10000", chain=MessageChain([Text(text)]),
                         is_mentioned=mentioned)


class StubAdapter:
    enabled = True
    adapter_id = "subagent"
    name = "subagent"
    platform = "subagent"


def real_batch(*msgs):
    return KiraMessageBatchEvent(timestamp=int(time.time()), session=Session(),
                                 messages=list(msgs))


def stub_batch(extra=None):
    """照抄子代理插件 _make_stub_event 的形状（同 sid、新事件对象）。"""
    dummy = KiraIMMessage(timestamp=int(time.time()), sender=User("subagent", "subagent"),
                          group=Group("10001", "测试群"), message_id="subagent_stub",
                          self_id="subagent", chain=MessageChain([]))
    ev = KiraMessageBatchEvent(timestamp=int(time.time()), session=Session(),
                               messages=[dummy])
    ev.adapter = StubAdapter()
    ev.extra = dict(extra or {})
    return ev


def _resp(text="<msg>你好</msg>", tool_calls=None):
    return SimpleNamespace(text_response=text, tool_calls=tool_calls,
                           reasoning_content="", agent_step_index=1)


def _hook(plug):
    """S 版叫 on_llm_response，Z 版叫 on_llm_response_enhance（同一个钩子的两个名字）。"""
    fn = getattr(plug, "on_llm_response", None) or getattr(plug, "on_llm_response_enhance", None)
    if fn is None:
        raise RuntimeError("找不到 LLM 响应钩子")
    return fn


def _timeline_len(plug, sid=SID):
    # 事件来自群聊（消息带 group）→ 与 enhance.on_llm_response 的 is_dm 判定一致
    pres = plug.enhance._get_presence(False)
    return len(pres._timeline.get(sid, []))


# --------------------------------------------------------------- 场景

async def g1_stub_event_not_a_reply(plugin_dir):
    """子代理桩事件 → on_llm_response 不记录存在感、不动持续窗口。"""
    mod, plug = load_plugin(plugin_dir)
    before = _timeline_len(plug)
    if hasattr(plug, "sustain_count"):
        plug.sustain_count[SID] = 0
    await _hook(plug)(stub_batch(), _resp("任务完成。再见"))
    after = _timeline_len(plug)
    r = {"桩事件未记入存在感": after == before}
    if hasattr(plug, "sustain_stopped"):
        r["桩事件未触发停窗/开窗"] = plug.sustain_stopped.get(SID) is None
    await _shutdown(plug)
    return r


async def g2_real_batch_still_works(plugin_dir):
    """对照：真实批次（经 ON_LLM_REQUEST 登记）照常记录存在感。"""
    mod, plug = load_plugin(plugin_dir)
    ev = real_batch(msg(1, "你好"))
    remember = getattr(plug, "_remember_real_batch", None)
    if remember is not None:
        await remember(ev)                       # 框架：ON_LLM_REQUEST 先派发
    before = _timeline_len(plug)
    await _hook(plug)(ev, _resp("你好呀"))
    after = _timeline_len(plug)
    await _shutdown(plug)
    return {"真实轮照常记录存在感": after == before + 1}


async def g3_explicit_stub_marker(plugin_dir):
    """双保险：即使被登记过，带显式桩标记的事件也要跳过。"""
    mod, plug = load_plugin(plugin_dir)
    ev = stub_batch(extra={"_subagent_stub": True})
    remember = getattr(plug, "_remember_real_batch", None)
    if remember is not None:
        await remember(ev)                       # 故意登记（模拟"误登记"）
    before = _timeline_len(plug)
    await _hook(plug)(ev, _resp("汇报"))
    after = _timeline_len(plug)
    await _shutdown(plug)
    return {"显式标记优先于登记表": after == before}


async def g4_queue_merge_final_result_push(plugin_dir):
    """ON_FINAL_RESULT 兜底：in-flight 就是本事件时把 pending 推出去（不再干等 180s）。"""
    mod = load_sched(plugin_dir)
    sched = mod.BatchMergeScheduler(Ctx(), {"section_queue_merge": {"enabled": True}}, {})
    sid = SID
    ev1 = real_batch(msg(1, "第一批"))
    ev2 = real_batch(msg(2, "第二批"))
    await sched.on_batch_message(ev1)                    # 放行（in-flight=ev1）
    await sched.on_batch_message(ev2)                    # 拦截进 pending
    pending_before = len(sched._pending.get(sid, []))
    await sched.on_final_result(ev1, None)
    published = [e for e in sched.ctx.event_bus.published]
    pending_after = len(sched._pending.get(sid, []))
    return {"第二批已进 pending": pending_before == 1,
            "final_result 推出去 1 个批次": len(published) == 1,
            "推送批次带自发布标记": bool(published and published[0].extra.get("_qm_self")),
            "pending 已清": pending_after == 0}


async def g5_queue_merge_final_result_idempotent(plugin_dir):
    """ON_FINAL_RESULT 幂等/不误推：事件不在 in-flight（已推过）时什么都不做。"""
    mod = load_sched(plugin_dir)
    sched = mod.BatchMergeScheduler(Ctx(), {"section_queue_merge": {"enabled": True}}, {})
    ev1 = real_batch(msg(1, "第一批"))
    await sched.on_batch_message(ev1)
    await sched.on_final_result(ev1, None)               # 第一次（无 pending）
    n1 = len(sched.ctx.event_bus.published)
    await sched.on_final_result(ev1, None)               # 重复调用（in-flight 已换）
    n2 = len(sched.ctx.event_bus.published)
    await sched.on_final_result(stub_batch(), None)      # 外来事件
    n3 = len(sched.ctx.event_bus.published)
    return {"无 pending 不发布": n1 == 0,
            "重复调用不重复发布": n2 == n1,
            "外来事件不发布": n3 == n2}


async def g6_handle_msg_no_direct_prefetch(plugin_dir):
    """静态回归：handle_msg 不再直接调度预取（改打 _batch_entered 标记），
    预取调度只在 stage1 末尾 —— 防止有人把"先调度的竞态写法"再加回来。"""
    src = (plugin_dir / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    handle = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_msg":
            handle = node
            break
    if handle is None:
        return {"找到 handle_msg": False}
    calls = [n for n in ast.walk(handle)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "schedule_prefetch"]
    marked = "schedule_prefetch" not in ast.dump(handle)
    src_marked = "_batch_entered" in ast.dump(handle)
    mr = (plugin_dir / "media_recognize.py").read_text(encoding="utf-8")
    return {"handle_msg 不直接调度预取": marked,
            "handle_msg 打了 _batch_entered 标记": src_marked,
            "stage1 里有预取调度": "schedule_prefetch" in mr}


async def _shutdown(plug):
    for name in ("shutdown", "terminate"):
        fn = getattr(plug, name, None)
        if callable(fn):
            try:
                await fn()
            except Exception:
                pass
            return


SCENARIOS = [
    ("G1 子代理桩事件不被当成主 bot 回复", g1_stub_event_not_a_reply),
    ("G2 对照：真实批次照常记录", g2_real_batch_still_works),
    ("G3 显式桩标记双保险（优先于登记表）", g3_explicit_stub_marker),
    ("G4 QueueMerge final_result 兜底推送", g4_queue_merge_final_result_push),
    ("G5 QueueMerge final_result 幂等/不误推", g5_queue_merge_final_result_idempotent),
    ("G6 静态回归：预取调度只在 stage1", g6_handle_msg_no_direct_prefetch),
]


async def main():
    dirs = [Path(d).resolve() for d in (sys.argv[1:] or [".."])]
    failed = 0
    for d in dirs:
        print("=" * 78)
        print(f"### {d.name}")
        for name, fn in SCENARIOS:
            try:
                r = await fn(d)
                bad = [k for k, v in r.items() if v is False]
                if bad:
                    failed += 1
                    print(f"  ✗ FAIL   {name}")
                    print(f"            {r}")
                else:
                    print(f"  ✓ PASS   {name}")
                    print(f"            {r}")
            except Exception as e:
                failed += 1
                print(f"  ERR       {name}: {type(e).__name__}: {e}")
    print("=" * 78)
    print("ALL TESTS PASSED" if failed == 0 else f"{failed} CHECK(S) FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

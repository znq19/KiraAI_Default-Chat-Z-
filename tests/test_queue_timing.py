"""S/Z：兜底节拍（原固定 0.5s）与"in-flight 已停"的即时推送（v2.5.18 / v1.8.9）。

背景（用户诉求）：把 0.5s 这个数字省掉，最好无感知，但必须安全。

改动（只改"什么时候醒来"，`_tick` 的判断逻辑一字未动）：
  * 拦截批次进 pending 时：若 in-flight **已被 stop**（停止词/交棒/其它插件批次阶段掐停），
    它不会再有收尾事件 → **锁外立刻推送**（0 延迟，不等任何节拍）；
  * `_tick_loop` 改为 `wait_for(唤醒事件, timeout=_next_watch_delay())`：
    空闲懒睡 5s、已停 0.05s 复查、其余贴「卡死兜底剩余 / 攒批窗口剩余」的较小值；
    拦截时 `_ensure_task_locked()` 会 set 唤醒事件 → 立刻重算。

用例：
  Q1 in-flight 已停 + 新批次到达 → **立即推送**（0 延迟）
  Q2 对照：in-flight 正常 → 新批次仍进 pending，不提前推
  Q3 `_next_watch_delay()`：无 pending=5s / 已停=0.05s / 卡死兜底≈180s / 攒批窗口≈3s
  Q4 唤醒语义：循环处于长睡眠时，`_ensure_task_locked()` 能立刻叫醒它跑 `_tick`
  Q5 正常收尾路径不受影响（step_result 推送 pending）

Run: python3 tests/test_queue_timing.py [<plugin_dir> ...]
"""
import asyncio
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

SID = "qq:gm:427674145"
results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label + ((" — " + str(detail)) if (detail and not cond) else ""))


class Cfg:
    def get_config(self, key, default=None):
        return {"bot_config.agent.max_tool_loop": 2,
                "bot_config.agent.tool_call_timeout": 60,
                "bot_config.bot.max_buffer_messages": 5,
                "bot_config.bot.max_message_interval": 30}.get(key, default)

    def __getitem__(self, key):
        return {"agent": {"max_tool_loop": 2, "tool_call_timeout": 60},
                "bot": {"max_buffer_messages": 5, "max_message_interval": 30}} if key == "bot_config" else {}


class Bus:
    def __init__(self):
        self.published = []

    async def publish(self, event):
        self.published.append(event)


class Ctx:
    def __init__(self):
        self.config = Cfg()
        self.event_bus = Bus()


def load_qm(plugin_dir: Path):
    for m in ("queue_merge",):
        sys.modules.pop(m, None)
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"qm_timing_{plugin_dir.name}",
                                                  plugin_dir / "queue_merge.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def msg(mid, text):
    return KiraIMMessage(timestamp=0, sender=User("1", "n"), group=Group("1", "g"),
                         message_id=str(mid), self_id="10000", chain=MessageChain([Text(text)]))


def batch(*msgs, sid=SID):
    sess = Session(adapter_name="qq", session_type="gm", session_id=sid.split(":")[-1])
    return KiraMessageBatchEvent(timestamp=0, session=sess, messages=list(msgs))


def make_sched(mod, cfg=None):
    return mod.BatchMergeScheduler(Ctx(), cfg or {"section_queue_merge": {"enabled": True}}, {})


async def q1_stopped_inflight_pushes_immediately(plugin_dir):
    mod = load_qm(plugin_dir)
    sched = make_sched(mod)
    e1 = batch(msg(1, "运行中的轮"))
    await sched.on_batch_message(e1)           # 放行 → in-flight = e1
    e1.stop()                                  # 外部把它停了（交棒/停止词/其它插件）
    e2 = batch(msg(2, "停后的新消息"))
    t0 = time.time()
    await sched.on_batch_message(e2)
    dt = time.time() - t0
    published = sched.ctx.event_bus.published
    return {"Q1 新批次被拦截": e2.is_stopped,
            "Q1 立即推送 pending（0 延迟，不等节拍）": len(published) == 1 and dt < 0.05,
            "Q1 推送的是合并批次（带 _qm_self）": bool(published and published[0].extra.get("_qm_self")),
            "Q1 pending 已清空": not sched._pending.get(SID),
            "耗时(秒)": round(dt, 4)}


async def q2_normal_inflight_waits(plugin_dir):
    mod = load_qm(plugin_dir)
    sched = make_sched(mod)
    e1 = batch(msg(1, "正常的轮"))
    await sched.on_batch_message(e1)
    e2 = batch(msg(2, "运行中插入"))
    await sched.on_batch_message(e2)
    return {"Q2 新批次进 pending": e2.is_stopped and len(sched._pending.get(SID, [])) == 1,
            "Q2 不提前推送（等收尾）": len(sched.ctx.event_bus.published) == 0}


async def q3_next_watch_delay(plugin_dir):
    mod = load_qm(plugin_dir)
    sched = make_sched(mod)
    d_idle = sched._next_watch_delay()
    e1 = batch(msg(1, "轮"))
    await sched.on_batch_message(e1)
    e2 = batch(msg(2, "积压"))
    await sched.on_batch_message(e2)           # 进 pending（未停）
    d_stall = sched._next_watch_delay()
    e1.stop()
    d_stopped = sched._next_watch_delay()

    sched2 = make_sched(mod, {"section_queue_merge": {"enabled": True, "max_merge_seconds": 3}})
    e3 = batch(msg(3, "轮"))
    await sched2.on_batch_message(e3)
    e4 = batch(msg(4, "积压"))
    await sched2.on_batch_message(e4)
    d_merge = sched2._next_watch_delay()
    return {"Q3 空闲时懒睡（5s）": abs(d_idle - 5.0) < 1e-6,
            "Q3 积压+运行中 → 不超过懒睡间隔（真正的截止点=卡死兜底 180s）": 0 < d_stall <= 5.0,
            "Q3 in-flight 已停 → 0.05s 复查": abs(d_stopped - 0.05) < 1e-6,
            "Q3 攒批窗口 → ≈3s": 0.5 < d_merge <= 3.0,
            "细节": f"idle={d_idle} stall={d_stall:.1f} stopped={d_stopped} merge={d_merge:.2f}"}


async def q4_wake_events(plugin_dir):
    mod = load_qm(plugin_dir)
    sched = make_sched(mod)
    ticks = []
    orig_tick = sched._tick

    async def spy_tick():
        ticks.append(time.time())
        return await orig_tick()

    sched._tick = spy_tick
    sched._ensure_task_locked()                # 启动循环：无 pending → 计划睡 5s
    await asyncio.sleep(0.3)
    n_before = len(ticks)
    # 有新 pending：_ensure_task_locked 应立即叫醒它重算
    e1 = batch(msg(1, "轮"))
    await sched.on_batch_message(e1)
    e2 = batch(msg(2, "积压"))
    await sched.on_batch_message(e2)
    await asyncio.sleep(0.3)
    n_after = len(ticks)
    await sched.shutdown()
    await asyncio.sleep(0.1)
    return {"Q4 长睡眠期被立即唤醒（≤0.3s 内跑了一次）": n_after > n_before,
            "细节": f"ticks {n_before} → {n_after}"}


async def q5_normal_push_unchanged(plugin_dir):
    mod = load_qm(plugin_dir)
    sched = make_sched(mod)
    e1 = batch(msg(1, "轮"))
    await sched.on_batch_message(e1)
    e2 = batch(msg(2, "积压"))
    await sched.on_batch_message(e2)
    await sched.on_llm_response(e1, SimpleNamespace(tool_calls=None))   # 最后一步
    await sched.on_step_result(e1)
    return {"Q5 收尾时照常推送 pending": len(sched.ctx.event_bus.published) == 1,
            "Q5 推送批次带 _qm_self": bool(sched.ctx.event_bus.published
                                          and sched.ctx.event_bus.published[0].extra.get("_qm_self"))}


SCENARIOS = [
    ("Q1 in-flight 已停 → 立即推送（0 延迟）", q1_stopped_inflight_pushes_immediately),
    ("Q2 对照：in-flight 正常 → 仍等收尾", q2_normal_inflight_waits),
    ("Q3 兜底节拍按截止点计算", q3_next_watch_delay),
    ("Q4 有 pending 时立即唤醒（不空转）", q4_wake_events),
    ("Q5 正常收尾推送不受影响", q5_normal_push_unchanged),
]


async def main():
    dirs = [Path(d).resolve() for d in (sys.argv[1:] or [".."])]
    for d in dirs:
        print("=" * 78)
        print(f"### {d.name}/queue_merge.py")
        for name, fn in SCENARIOS:
            try:
                r = await fn(d)
                bad = [k for k, v in r.items() if v is False]
                results.append((name, not bad))
                if bad:
                    print(f"  FAIL  {name}")
                    print(f"        {r}")
                else:
                    print(f"  PASS  {name}")
                    if "细节" in r:
                        print(f"        {r['细节']}")
            except Exception as e:
                print(f"  ERR   {name}: {type(e).__name__}: {e}")
                results.append((name, False))
    print()
    passed = sum(1 for _, ok in results if ok)
    print("TOTAL %d/%d passed" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""S/Z 版：检测「async 方法被当同步函数调用」（含"走真实钩子"的运行时验证）。

为什么需要这个测试
------------------
线上日志出现过：

    main.py:1812: RuntimeWarning: coroutine 'ParallelMediaRecognizer.guard_captions'
                  was never awaited
      self.media_recognizer.guard_captions(event)

这不是普通告警：**协程根本没执行** —— 官方 VLM 保护网 100% 失效（框架照旧付费识图），
而且静默失效、不抛异常。原有 repro_media/run_media_repro.py 全绿是因为它
**直接 `await p.guard_captions(...)`**，绕过了真正出问题的钩子 `guard_official_vlm`。
本测试补上这条缝：

  1. 静态：AST 扫全部 async 方法名，找出被当同步调用（未 await / 未包装 / 未收集）的点。
  2. 运行时：加载真实插件，给 caption=None 的 Image，**通过钩子**调用，断言
     caption 真被占成 ""（bug 时协程不执行 → caption 仍是 None → 失败）。

用法: python3 repro_media/test_guard_await.py <plugin_dir> ...
      （plugin_dir 里放 main.py，即 s/ 或 z/）
"""
import ast
import asyncio
import importlib.util
import sys
import time
import warnings
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
STUB = HERE / "_stub"                     # 仓库内自带的框架桩
sys.path.insert(0, str(STUB))

from core.chat import Group, KiraIMMessage, MessageChain, Session, User  # noqa: E402
from core.chat.message_elements import Image, Sticker, Text  # noqa: E402
from core.chat.message_utils import KiraMessageEvent  # noqa: E402

SID = "qq:gm:10001"

# --------------------------------------------------------------------- 静态
CALL_WRAPPERS = {"create_task", "ensure_future", "gather", "wait_for", "shield",
                 "run_coroutine_threadsafe", "run_until_complete", "to_thread"}
# 变量名像"协程/任务/未来"的赋值 = 故意收集后统一 await
import re as _re
CORO_VAR = _re.compile(r"(?i)(coro|task|future|fut|awaitable|awaitables)")

# async 方法调用必然出现的文件名（限定扫描范围，减少同名误报）
SCAN_FILES = {"main.py", "media_recognize.py", "queue_merge.py", "chat_enhance.py"}


def static_scan(plugin_dir: Path):
    py_files = [p for p in plugin_dir.rglob("*.py")
                if "__pycache__" not in str(p) and p.name in SCAN_FILES]

    # 1) 收集方法名 → {async, sync}；两种都有 = 有歧义，跳过（如 enhance.* / shutdown）
    kinds = {}
    trees = {}
    for p in py_files:
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        trees[p] = tree
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kinds.setdefault(node.name, set()).add(
                    "async" if isinstance(node, ast.AsyncFunctionDef) else "sync")
    async_only = {n for n, k in kinds.items() if k == {"async"}}

    # 2) 逐文件建父节点映射后判定
    findings = []
    for p, tree in trees.items():
        src = p.read_text(encoding="utf-8")
        lines = src.splitlines()
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            cname = fn.attr if isinstance(fn, ast.Attribute) else (
                fn.id if isinstance(fn, ast.Name) else None)
            if cname not in async_only or cname in ("__init__", "__aenter__", "__aexit__"):
                continue

            # 同一行该调用之前是否有 await
            head = lines[node.lineno - 1]
            if cname in head and "await" in head.split(cname)[0]:
                continue

            # 向上找父节点：包装调用 / 收集容器
            handled = False
            anc = parents.get(node)
            while anc is not None:
                if isinstance(anc, ast.Await):
                    handled = True
                    break
                if isinstance(anc, ast.Call):
                    afn = anc.func
                    aname = afn.attr if isinstance(afn, ast.Attribute) else (
                        afn.id if isinstance(afn, ast.Name) else None)
                    if aname in CALL_WRAPPERS:
                        handled = True
                        break
                    if aname in ("append", "add", "extend"):
                        handled = True          # coros.append(coro(...)) 等收集
                        break
                if isinstance(anc, ast.Assign) and any(
                        isinstance(t, ast.Name) and CORO_VAR.search(t.id)
                        for t in anc.targets):
                    handled = True              # coro = self._xxx(...)
                    break
                if isinstance(anc, (ast.AsyncFunctionDef, ast.FunctionDef, ast.Module)):
                    break
                anc = parents.get(anc)
            if not handled:
                findings.append((p, node.lineno, head.strip()[:90]))
    return findings


# --------------------------------------------------------------------- 运行时
def make_plugin(plugin_dir: Path):
    main_py = plugin_dir / "main.py"
    for m in ("queue_merge", "media_recognize", "chat_enhance"):
        sys.modules.pop(m, None)
    spec = importlib.util.spec_from_file_location(f"plug_{plugin_dir.name}", str(main_py))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # ctx.config 既要支持 ["bot_config"]["bot"] 下标，也要支持 .get_config(key, default)
    _cfg_map = {
        "bot_config.agent.max_tool_loop": 2,
        "bot_config.agent.tool_call_timeout": 60,
        "bot_config.bot.max_buffer_messages": 5,
        "bot_config.bot.max_message_interval": 30,
    }

    class Cfg:
        def get_config(self, key, default=None):
            return _cfg_map.get(key, default)

        def __getitem__(self, key):
            if key == "bot_config":
                return {"agent": {"max_tool_loop": 2, "tool_call_timeout": 60},
                        "bot": {"max_buffer_messages": 5, "max_message_interval": 30}}
            return {}

    class Ctx:
        def __init__(self):
            self.config = Cfg()
            self.plugin_mgr = None
            self.session_mgr = None

    cls = getattr(mod, "DebouncePlugin", None)
    return mod, cls(Ctx(), {})


async def runtime_via_hook(plugin_dir: Path):
    """给 caption=None 的 Image，**通过真实钩子 guard_official_vlm** 调用保护网。

    修复后：钩子被 await → caption 变成 ""，且无 RuntimeWarning。
    bug 时：协程没执行 → caption 仍 None，并发出 "never awaited" 警告。
    """
    mod, plug = make_plugin(plugin_dir)
    mr = plug.media_recognizer
    # 只验证保护网本身，强制打开（不受用户配置影响），并屏蔽"谁负责图片"的两个分流
    mr.enabled = True
    mr.guard_enabled = True
    mr._pir_active = lambda *a, **k: False
    mr._native_mode = lambda *a, **k: False

    img = Image(image="/tmp/await_test.jpg")     # caption=None
    stk = Sticker(sticker_id="s", sticker="/tmp/a.webp")
    kept = Image(image="/tmp/kept.jpg", caption="已有描述")
    msg = SimpleNamespace(chain=[img, stk, kept], message_str=None)
    event = KiraMessageEvent(message=msg, session=Session())

    hook = getattr(plug, "guard_official_vlm", None)
    if hook is None:
        for name in ("shutdown", "terminate"):
            fn = getattr(plug, name, None)
            if callable(fn):
                try:
                    await fn()
                except Exception:
                    pass
                break
        return {"has_hook": False}

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await hook(event)
        not_awaited = [w for w in caught
                       if issubclass(w.category, RuntimeWarning)
                       and "never awaited" in str(w.message)]

    for name in ("shutdown", "terminate"):
        fn = getattr(plug, name, None)
        if callable(fn):
            try:
                await fn()
            except Exception:
                pass
            break

    return {
        "has_hook": True,
        "img_caption_occupied": img.caption == "",
        "sticker_caption_occupied": stk.caption == "",
        "existing_desc_untouched": kept.caption == "已有描述",
        "never_awaited_warnings": len(not_awaited),
    }


async def main():
    dirs = [Path(d).resolve() for d in (sys.argv[1:] or [".."])]
    all_ok = True
    for d in dirs:
        print("=" * 78)
        print(f"### {d.name}")
        findings = static_scan(d)
        if findings:
            all_ok = False
            print(f"  ✗ 静态：发现 {len(findings)} 处 async 方法被当同步调用：")
            for p, ln, code in findings:
                print(f"      {p.name}:{ln}  {code}")
        else:
            print("  ✓ 静态：无『async 方法当同步调用』")

        r = await runtime_via_hook(d)
        if not r.get("has_hook"):
            print("  - 运行时：无 guard_official_vlm 钩子（该版本未含保护网），跳过")
            continue
        ok = (r["img_caption_occupied"] and r["sticker_caption_occupied"]
              and r["existing_desc_untouched"] and r["never_awaited_warnings"] == 0)
        all_ok &= ok
        print(f"  {'✓' if ok else '✗'} 运行时（走真实钩子 guard_official_vlm）: {r}")

    print("=" * 78)
    print("ALL TESTS PASSED" if all_ok else "SOME TESTS FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

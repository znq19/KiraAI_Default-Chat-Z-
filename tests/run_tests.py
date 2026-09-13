"""S/Z 插件测试入口（无需框架）：python3 tests/run_tests.py"""
import subprocess, sys, pathlib
HERE = pathlib.Path(__file__).resolve().parent
PLUGIN = HERE.parent
rc = subprocess.call([sys.executable, str(HERE / "test_guard_await.py"), str(PLUGIN)])
sys.exit(rc)

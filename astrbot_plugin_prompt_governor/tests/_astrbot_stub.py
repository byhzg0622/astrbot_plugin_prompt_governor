"""测试用 astrbot 运行时替身（不依赖真实 AstrBot）。

必须在 import 本插件的任何模块之前调用 install()：它往 sys.modules 里塞
最小的 astrbot 包（logger / filter / Star / StarTools / sp），让 main.py 与
api/webapi.py 能像在宿主里一样被导入。真实环境里 api.web 存在，这里的
ImportError 会让插件走自带垫片分支 —— 两条分支的返回形状一致。
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
import tempfile
import types
from pathlib import Path
from typing import Any

_STATE: dict[str, Any] = {
    "data_dir": None,
    "sp": None,
}


class _LoggerStub:
    """astrbot.api.logger 的最小替身：吞掉日志，不吞异常。"""

    def __init__(self, name: str = "astrbot") -> None:
        self._log = logging.getLogger(name)

    def info(self, *args: Any, **kwargs: Any) -> None:
        self._log.info(*args)

    def debug(self, *args: Any, **kwargs: Any) -> None:
        self._log.debug(*args)

    def warning(self, *args: Any, **kwargs: Any) -> None:
        self._log.warning(*args)

    def error(self, *args: Any, **kwargs: Any) -> None:
        self._log.error(*args)

    def exception(self, *args: Any, **kwargs: Any) -> None:
        self._log.exception(*args)


class _FilterStub:
    """filter 装饰器替身：原样返回函数，行为等价于"没装钩子"。"""

    def on_llm_request(self, *args: Any, **kwargs: Any):
        def decorator(func: Any) -> Any:
            return func

        return decorator

    def on_agent_done(self, *args: Any, **kwargs: Any):
        def decorator(func: Any) -> Any:
            return func

        return decorator

    def llm_tool(self, *args: Any, **kwargs: Any):
        def decorator(func: Any) -> Any:
            return func

        return decorator


class _StarToolsStub:
    @staticmethod
    def get_data_dir(name: str) -> str:
        if _STATE["data_dir"] is None:
            _STATE["data_dir"] = Path(tempfile.mkdtemp(prefix="pg_test_"))
        return str(Path(_STATE["data_dir"]) / name)


class _StarStub:
    def __init__(self, context: Any = None, config: Any = None) -> None:
        self.context = context
        self.config = config


class _FakeSp:
    """AstrBot core.sp 的会话规则存储替身（{ (umo, key): value }）。"""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], Any] = {}

    async def session_get(self, umo: str, key: str, default: Any = None) -> Any:
        return self.store.get((umo, key), default)

    async def session_put(self, umo: str, key: str, value: Any) -> None:
        self.store[(umo, key)] = value

    async def session_remove(self, umo: str, key: str) -> None:
        self.store.pop((umo, key), None)


def set_data_dir(path: str | Path) -> None:
    _STATE["data_dir"] = Path(path)


def fake_sp() -> _FakeSp:
    return _STATE["sp"]


def _register_plugin_package() -> None:
    """让测试能用固定包名 import 插件（目录名可带中文/括号，不影响）。

    用 namespace 包的方式注册：顶层与 core/api/tests 子包只建立 __path__，
    不执行任何模块体（测试真正 import main 时，import 机制按 __path__ 找到
    包根目录下的 main.py 正常执行；core/api 子包的 __init__ 在首次被
    import 时执行）。相对 import（from .core.models）走 __path__ 解析。
    """
    pkg_name = "astrbot_plugin_prompt_governor"
    if pkg_name in sys.modules:
        return
    try:
        from pathlib import Path
        pkg_root = Path(__file__).resolve().parent.parent
        top = types.ModuleType(pkg_name)
        top.__path__ = [str(pkg_root)]
        sys.modules[pkg_name] = top
        for sub in ("core", "api", "tests"):
            sub_dir = pkg_root / sub
            if not sub_dir.is_dir():
                continue
            sub_mod = types.ModuleType(f"{pkg_name}.{sub}")
            sub_mod.__path__ = [str(sub_dir)]
            sub_mod.__spec__ = None  # 标记：让 import 机制去找 __init__.py 并执行它
            sys.modules[f"{pkg_name}.{sub}"] = sub_mod
            setattr(top, sub, sub_mod)
        # 顶层包的 spec：submodule_search_locations 指向包根，
        # import 机制据此解析 main / core / api / tests 子模块
        top.__spec__ = importlib.util.spec_from_file_location(
            pkg_name,
            str(pkg_root / "main.py"),
            submodule_search_locations=[str(pkg_root)],
        )
    except Exception as exc:
        print("[pg-stub] register plugin package failed:", exc)


def install() -> None:
    """把替身注册进 sys.modules（重复调用是幂等的）。"""
    if "astrbot" in sys.modules and getattr(sys.modules["astrbot"], "__pg_stub__", False):
        return
    _register_plugin_package()

    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []  # type: ignore[attr-defined]
    astrbot.__pg_stub__ = True  # type: ignore[attr-defined]

    api = types.ModuleType("astrbot.api")
    api.__path__ = []  # type: ignore[attr-defined]
    api.logger = _LoggerStub()

    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = type("AstrMessageEvent", (), {})
    event.filter = _FilterStub()

    provider = types.ModuleType("astrbot.api.provider")
    provider.ProviderRequest = type("ProviderRequest", (), {})
    provider.LLMResponse = type("LLMResponse", (), {})

    star = types.ModuleType("astrbot.api.star")
    star.Context = type("Context", (), {})
    star.Star = _StarStub
    star.StarTools = _StarToolsStub

    def register(*args: Any, **kwargs: Any):
        def decorator(cls: Any) -> Any:
            return cls

        return decorator

    star.register = register

    core = types.ModuleType("astrbot.core")
    core.__path__ = []  # type: ignore[attr-defined]
    sp = _FakeSp()
    _STATE["sp"] = sp
    core.sp = sp

    api.event = event
    api.provider = provider
    api.star = star
    astrbot.api = api
    astrbot.core = core

    sys.modules.setdefault("astrbot", astrbot)
    sys.modules.setdefault("astrbot.api", api)
    sys.modules.setdefault("astrbot.api.event", event)
    sys.modules.setdefault("astrbot.api.provider", provider)
    sys.modules.setdefault("astrbot.api.star", star)
    sys.modules.setdefault("astrbot.core", core)

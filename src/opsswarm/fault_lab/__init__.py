"""本地确定性故障演练服务。"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opsswarm.fault_lab.app import create_app

__all__ = ["create_app"]

# 读取数据契约无需加载模型运行器或演练 HTTP 应用；保留既有包级导出。
_EXPORT_MODULES = {
    "create_app": "opsswarm.fault_lab.app",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"模块 {__name__!r} 没有属性 {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

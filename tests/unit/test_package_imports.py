import subprocess
import sys
from pathlib import Path

import pytest


def test_contract_imports_do_not_load_agent_or_http_runtimes() -> None:
    """独立解释器避免其他测试已加载的框架掩盖包入口的依赖回归。"""

    source = Path(__file__).resolve().parents[2] / "src"
    code = f"""
import sys
sys.path.insert(0, {str(source)!r})
from opsswarm.investigation.models import RootCause
from opsswarm.fault_lab.models import FaultStatus
assert RootCause.RELEASE_500_REGRESSION.value == 'release_500_regression'
assert FaultStatus(name='release_500', enabled=True).enabled
for prefix in ('langchain', 'langchain_core', 'langgraph', 'fastapi'):
    assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules), prefix
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_existing_package_exports_keep_their_implementations() -> None:
    from opsswarm.fault_lab import create_app
    from opsswarm.fault_lab.app import create_app as implementation
    from opsswarm.investigation import (
        LangGraphParallelInvestigator,
        RootCause,
        SingleAgentInvestigator,
    )
    from opsswarm.investigation.agent import SingleAgentInvestigator as single
    from opsswarm.investigation.models import RootCause as root_cause
    from opsswarm.investigation.parallel_graph import LangGraphParallelInvestigator as parallel

    assert (create_app, RootCause, SingleAgentInvestigator, LangGraphParallelInvestigator) == (
        implementation,
        root_cause,
        single,
        parallel,
    )


@pytest.mark.parametrize("name", ["opsswarm.investigation", "opsswarm.fault_lab"])
def test_package_exports_remain_discoverable_and_reject_unknown_names(name: str) -> None:
    from importlib import import_module

    package = import_module(name)
    assert set(package.__all__) <= set(dir(package))
    with pytest.raises(AttributeError, match="没有属性"):
        _ = package.missing_public_export

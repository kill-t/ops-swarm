"""身份测试使用固定合成凭据，并隔离宿主认证环境。"""

import os

import pytest


@pytest.fixture(autouse=True)
def isolate_operator_auth_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in os.environ:
        if name.upper().startswith("OPSSWARM_OPERATOR_AUTH_"):
            monkeypatch.delenv(name)

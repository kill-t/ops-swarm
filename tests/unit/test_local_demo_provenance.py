"""验证发布证据绑定实际镜像，拒绝旧构建和非当前数据库。"""

import json
from types import SimpleNamespace

import pytest

from scripts.local_demo import provenance
from scripts.local_demo.client import DemoError


@pytest.mark.parametrize("failure", [None, "dirty", "image", "schema"])
def test_build_identity_is_checked_and_recorded(monkeypatch, failure):
    commit = "a" * 40

    def command(args):
        if args[:2] == ["git", "rev-parse"]:
            return commit
        if args[:2] == ["git", "status"]:
            return " M src/example.py" if failure == "dirty" else ""
        assert args[:2] == ["docker", "inspect"]
        assert ".Config.Env" not in args[3]
        return json.dumps(
            dict(
                service="control-api",
                image_id="sha256:actual-image",
                revision="old" if failure == "image" else commit,
            )
        )

    monkeypatch.setattr(provenance, "read_command", command)
    head = provenance.ScriptDirectory.from_config(
        provenance.Config(str(provenance.ROOT / "alembic.ini"))
    ).get_current_head()

    def compose(*args):
        if args == ("ps", "-q"):
            return "container-id"
        assert args[-1] == "SELECT version_num FROM alembic_version"
        return "old-head" if failure == "schema" else head

    control = SimpleNamespace(_run=compose)
    if failure:
        with pytest.raises(DemoError) as caught:
            provenance.collect(control)
        result = caught.value.details["provenance"]
        assert (
            caught.value.code
            == {
                "dirty": "tracked_worktree_dirty",
                "image": "runtime_revision_mismatch",
                "schema": "schema_head_mismatch",
            }[failure]
        )
    else:
        result = provenance.collect(control)
    assert result["commit"] == commit
    assert result["images"][0]["image_id"] == "sha256:actual-image"
    assert set(result["configuration_sha256"]) == {
        "compose.yaml",
        "compose.local-release.yaml",
        "compose.local-demo.yaml",
        "uv.lock",
    }

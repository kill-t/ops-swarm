"""只读取构建身份和脱敏配置摘要，不输出环境或容器秘密。"""

import hashlib
import json
import subprocess

from alembic.config import Config
from alembic.script import ScriptDirectory

from scripts.local_demo.client import DemoError
from scripts.local_demo.config import ROOT


def read_command(args: list[str]) -> str:
    try:
        return subprocess.run(
            args,
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise DemoError("provenance", "build_identity_unavailable") from None


def collect(control) -> dict:
    commit = read_command(["git", "rev-parse", "HEAD"])
    dirty = bool(read_command(["git", "status", "--porcelain", "--untracked-files=no"]))
    ids = control._run("ps", "-q").split()
    if not ids:
        raise DemoError("provenance", "running_images_missing")
    template = (
        '{"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
        '"image_id":{{json .Image}},'
        '"revision":{{json (index .Config.Labels "org.opencontainers.image.revision")}}}'
    )
    images = [
        json.loads(line)
        for line in read_command(["docker", "inspect", "--format", template, *ids]).splitlines()
    ]
    schema = control._run(
        "exec",
        "-T",
        "control-db",
        "psql",
        "-U",
        "opsswarm",
        "-d",
        "opsswarm",
        "-At",
        "-c",
        "SELECT version_num FROM alembic_version",
    ).strip()
    expected = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()
    config_hashes = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in (
            "compose.yaml",
            "compose.local-release.yaml",
            "compose.local-demo.yaml",
            "uv.lock",
        )
    }
    result = dict(
        version="build-provenance-v1",
        commit=commit,
        tracked_dirty=dirty,
        images=sorted(images, key=lambda item: item["service"]),
        schema_head=schema,
        expected_schema_head=expected,
        configuration_sha256=config_hashes,
    )
    application = [
        item
        for item in images
        if item["service"]
        in {
            "web",
            "control-api",
            "order-service",
            "change-service",
            "demo-model",
            "redis-proxy",
            "telemetry-mcp",
            "telemetry-metrics-mcp",
        }
        or "worker" in item["service"]
    ]
    reason = (
        "tracked_worktree_dirty"
        if dirty
        else "runtime_revision_mismatch"
        if not application or any(item["revision"] != commit for item in application)
        else "schema_head_mismatch"
        if schema != expected
        else None
    )
    if reason:
        raise DemoError("provenance", reason, details={"provenance": result})
    return result

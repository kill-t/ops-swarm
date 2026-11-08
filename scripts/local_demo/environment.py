"""只控制本演示项目的依赖，观测环境而不修补系统时钟。"""

import json
import subprocess
from pathlib import Path

from scripts.local_demo.client import DemoClient, DemoError, require
from scripts.local_demo.config import CHANGE, ORDER, PROJECT, ROOT, directory

CLOCK_PROBE = """
import json,time
from datetime import UTC,datetime
start=time.monotonic(); last_mono=start; last_wall=datetime.now(UTC).timestamp()
negative=steps=0
while time.monotonic()-start < 35:
    time.sleep(0.25)
    wall=datetime.now(UTC).timestamp(); mono=time.monotonic()
    dw=wall-last_wall; dm=mono-last_mono
    negative+=dw<0; steps+=abs(dw-dm)>0.1
    last_wall,last_mono=wall,mono
print(json.dumps(dict(negative_steps=negative,steps_over_100ms=steps,
    elapsed=time.monotonic()-start,finished_at=datetime.now(UTC).isoformat())))
"""


class ComposeControl:
    def __init__(self, path: Path) -> None:
        self.path = directory(path)

    def command(self, *args: str) -> list[str]:
        return [
            "docker",
            "compose",
            "--project-directory",
            str(ROOT),
            "-p",
            PROJECT,
            "--env-file",
            str(self.path / "demo.env"),
            "--profile",
            "human-reassessment",
            "--profile",
            "candidate-evaluation",
            "-f",
            str(ROOT / "compose.yaml"),
            "-f",
            str(ROOT / "compose.local-release.yaml"),
            "-f",
            str(ROOT / "compose.local-demo.yaml"),
            *args,
        ]

    def _run(self, *args: str, timeout=60) -> str:
        try:
            result = subprocess.run(
                self.command(*args),
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
                timeout=timeout,
            )
            return result.stdout
        except (subprocess.SubprocessError, OSError) as error:
            raise DemoError("compose", "local_demo_command_failed") from error

    def sampling(self, enabled: bool) -> None:
        if enabled:
            self._run("up", "-d", "--no-deps", "--no-build", "recovery-sampling-worker")
        else:
            self._run("stop", "-t", "15", "recovery-sampling-worker")

    def clock(self) -> dict:
        running = set(self._run("ps", "--status", "running", "--services").split())
        require(
            {"web", "control-api", "order-service", "change-service", "demo-model"} <= running,
            "preflight",
            "demo_services_not_running",
        )
        try:
            return json.loads(self._run("exec", "-T", "demo-model", "python", "-c", CLOCK_PROBE))
        except ValueError as error:
            raise DemoError("preflight", "invalid_clock_observation") from error


def preflight(client: DemoClient, control: ComposeControl) -> dict:
    clock = control.clock()
    if clock["negative_steps"] != 0 or clock["steps_over_100ms"] != 0:
        raise DemoError("preflight", "unstable_container_clock", details={"clock": clock})
    client.call("/api/v1/operators/me", role=None, status=401)
    for role in ("engineer", "approver", "executor"):
        require(
            client.call("/api/v1/operators/me", role=role)["role"] == role,
            "preflight",
            "role_mismatch",
        )
    for origin in (ORDER, CHANGE):
        client.call("/health", origin=origin)
    return dict(clock=clock, authenticated_roles=True, anonymous_rejected=True)

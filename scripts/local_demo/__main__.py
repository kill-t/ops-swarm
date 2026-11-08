"""正式演示命令入口；不读取旧缓存案例或运行时注入脚本。"""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from scripts.local_demo.client import DemoClient, DemoError
from scripts.local_demo.config import PROFILE, directory, prepare, read_config
from scripts.local_demo.disposition import approve_and_verify, execute_and_converge, reinvestigate
from scripts.local_demo.environment import ComposeControl, preflight
from scripts.local_demo.investigation import Investigation


class Report:
    def __init__(self, path: Path, scenario: str) -> None:
        self.case_id = str(uuid4())
        self.path = path / "runs" / self.case_id / "report.json"
        self.path.parent.mkdir(parents=True)
        self.data = dict(
            version=PROFILE,
            case_id=self.case_id,
            scenario=scenario,
            scripted_model=True,
            started_at=datetime.now(UTC).isoformat(),
            status="running",
            events=[],
        )
        self.save()

    def save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def emit(self, stage: str, **values) -> None:
        self.data["events"].append(dict(stage=stage, **values))
        self.save()
        print(stage, json.dumps(values, ensure_ascii=False), flush=True)


def execute(path: Path, *, scenario: str | None, client=None, control=None) -> tuple[int, Path]:
    config = read_config(path)
    report = Report(path, scenario or "check")
    c = client or DemoClient(config["tokens"])
    control = control or ComposeControl(path)
    ready = False
    try:
        report.emit("preflight", **preflight(c, control))
        ready = True
        if scenario is not None:
            flow = Investigation(c, report.case_id, report.emit)
            flow.start()
            if scenario != "handoff":
                candidate = flow.continue_human()
                version = approve_and_verify(flow, candidate)
                convergence = execute_and_converge(
                    flow, version, control, failure=scenario == "reflow"
                )
                if scenario == "reflow":
                    reinvestigate(flow, convergence)
        report.data["status"] = "passed"
        return 0, report.path
    except DemoError as error:
        report.data.update(
            status="failed" if ready else "blocked_environment",
            error=dict(stage=error.stage, code=error.code, **error.details),
        )
        return 1 if ready else 2, report.path
    except (KeyError, ValueError, TypeError) as error:
        # 不把响应正文、认证信息或任意异常上下文写入共享报告。
        report.data.update(status="failed", error=dict(stage="contract", code=type(error).__name__))
        return 1, report.path
    finally:
        report.data["finished_at"] = datetime.now(UTC).isoformat()
        report.save()
        c.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="隔离本地脚本模型演示")
    parser.add_argument("action", choices=("path", "prepare", "check", "run"))
    parser.add_argument("--directory")
    parser.add_argument("--scenario", choices=("handoff", "recovery", "reflow"), default="handoff")
    args = parser.parse_args()
    try:
        path = directory(args.directory)
        if args.action == "path":
            print(path / "demo.env")
            return 0
        if args.action == "prepare":
            print(prepare(path))
            return 0
        code, report = execute(path, scenario=args.scenario if args.action == "run" else None)
        print(f"report={report}")
        return code
    except (OSError, ValueError) as error:
        print(f"演示配置不可用：{type(error).__name__}；请检查演示目录与身份有效期")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

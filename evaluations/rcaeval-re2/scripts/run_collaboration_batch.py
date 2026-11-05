"""运行 15 个留出案例的离线协作对照并保存逐次数据。"""

import argparse
import json
import subprocess
from collections import Counter
from pathlib import Path
from time import perf_counter

from collaboration_experiment import ModelRun, run_investigation
from fetch_pilot import CASE_DIR, fetch_case
from openai import APIError, OpenAI
from replay_evidence import load_evidence
from run_batch import MODEL

from opsswarm.investigation.settings import LlmSettings


def prepare():
    manifest = json.loads((CASE_DIR / "batch-04.json").read_text(encoding="utf-8-sig"))
    groups = Counter(case["group"] for case in manifest["cases"])
    if groups != {"resource": 5, "network": 10}:
        raise ValueError("案例分组不符合固定协议")
    for case in manifest["cases"]:
        directory = fetch_case(
            manifest["dataset"], manifest["revision"], case["case_id"], case["files"]
        )
        yield case, load_evidence(directory), load_evidence(directory, "expanded")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    cases = list(prepare())
    settings = LlmSettings.from_dotenv()
    if not settings.base_url:
        raise ValueError("缺少本地模型端点配置")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    with (
        OpenAI(
            api_key=settings.api_key.get_secret_value(),
            base_url=settings.base_url,
            timeout=35,
            max_retries=0,
        ) as client,
        args.output.open("x", encoding="utf-8") as output,
    ):
        for index, (case, compact, expanded) in enumerate(cases):
            modes = ("single", "multi") if index % 2 == 0 else ("multi", "single")
            for mode in modes:
                row = {
                    "case_id": case["case_id"],
                    "group": case["group"],
                    "expected_service": case["expected_service"],
                    "mode": mode,
                    "code_base": commit,
                    "model": MODEL,
                    "roles": [],
                    "follow_up": [],
                }
                run = ModelRun(client)
                started = perf_counter()
                try:
                    result = run_investigation(run, mode, compact, expanded, row)
                    row.update(status="completed", predicted_service=result)
                except APIError as error:
                    row.update(
                        status="api_error",
                        error=type(error).__name__,
                        http_status=getattr(error, "status_code", None),
                    )
                    raise SystemExit(f"模型请求失败 {row['error']} {row['http_status']}") from None
                except (ValueError, KeyError, TypeError) as error:
                    row.update(status="invalid_response", error=type(error).__name__)
                finally:
                    row.update(
                        duration_ms=round((perf_counter() - started) * 1000),
                        requests=run.requests,
                        input_bytes=run.input_bytes,
                        usage=dict(run.usage) if run.complete_usage and run.requests else None,
                        correct=row.get("predicted_service") == case["expected_service"],
                    )
                    output.write(json.dumps(row, ensure_ascii=False) + "\n")
                    output.flush()
                    print(
                        case["case_id"],
                        mode,
                        row["status"],
                        row["correct"],
                        row["requests"],
                        row["duration_ms"],
                        flush=True,
                    )


if __name__ == "__main__":
    main()

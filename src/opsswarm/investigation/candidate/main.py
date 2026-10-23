"""运行独立候选评估 Worker，只装配结构化模型，不装配故障环境写工具。"""

import argparse
import asyncio
import signal
import sys
from contextlib import suppress
from uuid import uuid4

from opsswarm.database import Database, DatabaseSettings
from opsswarm.database.asyncio_compat import create_selector_event_loop
from opsswarm.investigation.budget.model_contracts import ModelBudgetPolicy
from opsswarm.investigation.budget.model_settings import ModelBudgetSettings
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.candidate.job_store import CandidateJobStore
from opsswarm.investigation.candidate.ledger import CandidateLedger
from opsswarm.investigation.candidate.model import CandidateModel
from opsswarm.investigation.candidate.worker import CandidateWorker
from opsswarm.investigation.settings import (
    LlmSettings,
    ModelPricingSettings,
    create_openai_compatible_model,
)


async def run(*, once: bool) -> bool | None:
    settings = LlmSettings()
    pricing = ModelPricingSettings().to_pricing()
    profile = ModelBudgetSettings().to_profile(settings, pricing)
    if profile is None or profile.structured_output_method != "function_calling":
        raise CallBudgetExceeded("model_budget_profile_mismatch")
    model = create_openai_compatible_model(settings, disable_retries=True)
    database = Database(DatabaseSettings().database_url)

    def model_factory(policy: ModelBudgetPolicy) -> CandidateModel:
        if policy.profile != profile:
            raise CallBudgetExceeded("model_budget_profile_mismatch")
        return CandidateModel(model, policy)

    worker = CandidateWorker(
        CandidateJobStore(database.session_factory),
        CandidateLedger(database.session_factory),
        model_factory=model_factory,
        worker_id=f"candidate-{uuid4().hex}",
    )
    try:
        if once:
            return await worker.run_once()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        if sys.platform != "win32":
            loop.add_signal_handler(signal.SIGTERM, stop.set)
            loop.add_signal_handler(signal.SIGINT, stop.set)
        await worker.run_forever(stop)
        return None
    finally:
        await database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="运行可信候选评估 Worker")
    parser.add_argument("--once", action="store_true", help="至多处理一个持久候选评估任务")
    args = parser.parse_args()
    loop_factory = create_selector_event_loop if sys.platform == "win32" else None
    with suppress(KeyboardInterrupt):
        result = asyncio.run(run(once=args.once), loop_factory=loop_factory)
        if args.once:
            print("已处理一个候选评估任务" if result else "当前没有候选评估任务")


if __name__ == "__main__":
    main()

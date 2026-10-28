"""运行独立回流审查 Worker，只装配结构化模型，不装配故障环境写工具。"""

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
from opsswarm.investigation.reinvestigation_review.job_store import ReflowReviewJobStore
from opsswarm.investigation.reinvestigation_review.ledger import ReflowReviewLedger
from opsswarm.investigation.reinvestigation_review.model import ReflowReviewModel
from opsswarm.investigation.reinvestigation_review.worker import ReflowReviewWorker
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

    def model_factory(policy: ModelBudgetPolicy) -> ReflowReviewModel:
        if policy.profile != profile:
            raise CallBudgetExceeded("model_budget_profile_mismatch")
        return ReflowReviewModel(model, policy)

    worker = ReflowReviewWorker(
        ReflowReviewJobStore(database.session_factory),
        ReflowReviewLedger(database.session_factory),
        model_factory=model_factory,
        worker_id=f"reflow-{uuid4().hex}",
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
    parser = argparse.ArgumentParser(description="运行可信回流审查 Worker")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="预备一份准入并至多领取一个审查任务")
    mode.add_argument("--continuous", action="store_true", help="持续发现并处理回流审查阶段")
    args = parser.parse_args()
    loop_factory = create_selector_event_loop if sys.platform == "win32" else None
    with suppress(KeyboardInterrupt):
        result = asyncio.run(run(once=args.once), loop_factory=loop_factory)
        if args.once:
            print("已处理一个回流审查任务" if result else "当前没有回流审查任务")


if __name__ == "__main__":
    main()

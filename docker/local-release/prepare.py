"""初始化本地发布依赖，校验配置但不请求模型。"""

from datetime import UTC, datetime

from opsswarm.checkpoints.setup import main as setup_checkpoints
from opsswarm.identity.models import OperatorRole
from opsswarm.identity.settings import OperatorAuthSettings
from opsswarm.investigation.budget.model_settings import ModelBudgetSettings
from opsswarm.investigation.settings import LlmSettings, ModelPricingSettings


def main() -> None:
    now = datetime.now(UTC)
    required = {
        OperatorRole.ENGINEER,
        OperatorRole.APPROVER,
        OperatorRole.EXECUTOR,
        OperatorRole.ALERT_SOURCE,
    }
    try:
        auth = OperatorAuthSettings()
        roles = {item.role for item in auth.credentials if item.expires_at > now}
        if auth.mode != "bearer" or not required <= roles:
            raise ValueError
    except ValueError:
        raise SystemExit("本地发布需要有效的工程师、审批者、执行者和告警来源凭据") from None

    try:
        profile = ModelBudgetSettings().to_profile(
            LlmSettings(), ModelPricingSettings().to_pricing()
        )
        if profile is None or profile.structured_output_method != "function_calling":
            raise ValueError
    except ValueError:
        raise SystemExit("本地发布需要完整的工具调用模型预算配置") from None

    setup_checkpoints()


if __name__ == "__main__":
    main()

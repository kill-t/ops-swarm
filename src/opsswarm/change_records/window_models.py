"""历史变更窗口的查询与响应契约。"""

from typing import Literal, Self

from pydantic import model_validator

from opsswarm.change_records.models import ChangeRecordListResponse
from opsswarm.observation_window import ObservationWindow


class ChangeWindowQuery(ObservationWindow):
    service_name: Literal["order-service"]


class ChangeRecordWindowResponse(ChangeRecordListResponse):
    window: ObservationWindow

    @model_validator(mode="after")
    def validate_generation_time(self) -> Self:
        self.window.require_elapsed(self.generated_at)
        return self

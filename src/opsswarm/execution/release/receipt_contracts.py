"""带采集时间和查询路径的回执材料；不代表派发或执行授权。"""

from datetime import datetime
from typing import Literal, Self

from pydantic import AwareDatetime, field_validator, model_validator

from opsswarm.fault_lab.release.contracts import ReleaseModel
from opsswarm.fault_lab.release.receipts import ReleaseReceipt, receipt_time

RECEIPT_PATH = "/__admin/simulated-release/commands/"


class ReceiptObservation(ReleaseModel):
    receipt: ReleaseReceipt
    observed_at: AwareDatetime
    source_path: str

    @field_validator("observed_at", mode="before")
    @classmethod
    def require_timestamp(cls, value: object) -> datetime:
        return receipt_time(value)

    @model_validator(mode="after")
    def require_query_binding(self) -> Self:
        if self.source_path != RECEIPT_PATH + self.receipt.idempotency_key:
            raise ValueError("回执必须来自精确键的只读查询路径")
        if self.receipt.current.updated_at > self.observed_at:
            raise ValueError("回执不能包含采集时刻之后的状态或命令")
        return self


class ReleaseReconciliation(ReleaseModel):
    status: Literal["not_recorded", "matched_current", "matched_superseded", "conflict"]


class ReceiptError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

"""对精确预期CAS及有界只读材料进行纯规则对账。"""

from datetime import datetime, timedelta

from opsswarm.execution.release.receipt_contracts import (
    ReceiptError,
    ReceiptObservation,
    ReleaseReconciliation,
)
from opsswarm.fault_lab.release.contracts import ReleaseCommand
from opsswarm.fault_lab.release.policy import command_hash
from opsswarm.fault_lab.release.receipts import receipt_time


def reconcile_receipt(
    expected: ReleaseCommand,
    observation: ReceiptObservation,
    *,
    not_before: datetime,
    now: datetime,
) -> ReleaseReconciliation:
    try:
        command = ReleaseCommand.model_validate(expected.model_dump())
        checked = ReceiptObservation.model_validate(observation.model_dump())
        earliest, current = receipt_time(not_before), receipt_time(now)
        if (
            earliest > current
            or checked.observed_at < earliest
            or checked.observed_at > current
            or current - checked.observed_at >= timedelta(seconds=30)
        ):
            raise ValueError("对账材料不在允许的30秒观测窗口")
    except (ValueError, TypeError, AttributeError) as error:
        raise ReceiptError("release_receipt_invalid", "回执对账材料无法严格校验") from error
    receipt = checked.receipt
    if receipt.idempotency_key != command.idempotency_key:
        return ReleaseReconciliation(status="conflict")
    if receipt.command is None:
        return ReleaseReconciliation(status="not_recorded")
    record = receipt.command
    if (
        record.kind != "cas"
        or record.command != command
        or record.command_sha256 != command_hash(command.model_dump(mode="json"))
        or record.created_at < earliest
    ):
        return ReleaseReconciliation(status="conflict")
    return ReleaseReconciliation(
        status="matched_current" if receipt.effect_current else "matched_superseded"
    )

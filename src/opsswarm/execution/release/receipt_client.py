"""按稳定键查询本地发布回执的有界只读客户端。"""

from collections.abc import Callable
from datetime import datetime

import httpx2
from pydantic import TypeAdapter

from opsswarm.execution.release.observer import utc_now
from opsswarm.execution.release.read_transport import read_json
from opsswarm.execution.release.receipt_contracts import (
    RECEIPT_PATH,
    ReceiptError,
    ReceiptObservation,
)
from opsswarm.execution.settings import ExecutionSettings
from opsswarm.fault_lab.release.contracts import Key
from opsswarm.fault_lab.release.receipts import ReleaseReceipt


class FaultLabReceiptClient:
    def __init__(
        self,
        settings: ExecutionSettings | None = None,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        provided = settings if settings is not None else ExecutionSettings()
        self._settings = ExecutionSettings.model_validate(provided.model_dump())
        self._transport, self._clock = transport, clock

    async def get(self, idempotency_key: str) -> ReceiptObservation:
        try:
            key = TypeAdapter(Key).validate_python(idempotency_key)
            path = RECEIPT_PATH + key
            status, raw = await read_json(
                self._settings, self._transport, path, allow_not_found=True
            )
            receipt = ReleaseReceipt.model_validate(raw)
            if status != (200 if receipt.status == "recorded" else 404):
                raise ValueError("HTTP状态与回执状态不一致")
            if receipt.idempotency_key != key:
                raise ValueError("响应不是查询的稳定键")
            return ReceiptObservation(receipt=receipt, observed_at=self._clock(), source_path=path)
        except (TimeoutError, httpx2.TimeoutException) as error:
            raise ReceiptError("release_receipt_timeout", "发布回执查询超时") from error
        except httpx2.HTTPError as error:
            raise ReceiptError("release_receipt_unavailable", "发布回执查询不可用") from error
        except (ValueError, TypeError) as error:
            raise ReceiptError("release_receipt_invalid", "发布回执无法严格校验") from error

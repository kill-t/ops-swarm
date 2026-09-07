"""变更查询响应的完整性、身份与时间范围校验。"""

from datetime import datetime
from uuid import UUID

from opsswarm.change_records.models import ChangeRecordListResponse
from opsswarm.investigation.errors import InvestigationDataSourceFailure


def validate_change_records(
    response: ChangeRecordListResponse,
    *,
    window_start: datetime,
    window_end: datetime,
    recorded_until: datetime,
) -> None:
    if response.truncated:
        raise InvestigationDataSourceFailure(
            error_code="change_result_truncated", message="变更结果已截断，不能形成完整时间线"
        )
    record_ids: set[UUID] = set()
    external_ids: set[tuple[str, str]] = set()
    for record in response.items:
        identity = (record.source, record.change.external_change_id)
        if record.record_id in record_ids or identity in external_ids:
            raise InvestigationDataSourceFailure(
                error_code="change_duplicate_record", message="变更响应包含重复记录"
            )
        record_ids.add(record.record_id)
        external_ids.add(identity)
        if not window_start <= record.occurred_at <= window_end:
            raise InvestigationDataSourceFailure(
                error_code="change_invalid_timestamp", message="变更发生时间不在请求窗口内"
            )
        if record.recorded_at > recorded_until:
            raise InvestigationDataSourceFailure(
                error_code="change_invalid_timestamp", message="变更入库时间晚于允许截止时间"
            )

"""变更记录精确契约测试。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from opsswarm.change_records.models import (
    ChangeRecord,
    ConfigurationChange,
    DependencyMaintenanceChange,
    DeploymentChange,
    change_content_hash,
)


def test_three_whitelisted_change_types_have_stable_hashes() -> None:
    changes = (
        DeploymentChange(
            external_change_id="release-rc1",
            from_version="stable",
            to_version="2026.01.01-rc1",
        ),
        ConfigurationChange(
            external_change_id="pool-size-5",
            from_value=50,
            to_value=5,
        ),
        DependencyMaintenanceChange(external_change_id="redis-restart-1"),
    )

    assert len({change_content_hash(change) for change in changes}) == 3
    assert all(len(change_content_hash(change)) == 64 for change in changes)
    assert change_content_hash(changes[0]) == change_content_hash(
        DeploymentChange(
            external_change_id="release-rc1",
            from_version="stable",
            to_version="2026.01.01-rc1",
        )
    )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: DeploymentChange(
            external_change_id="release-1",
            from_version="stable",
            to_version="stable",
        ),
        lambda: ConfigurationChange(
            external_change_id="pool-1",
            from_value=50,
            to_value=50,
        ),
        lambda: ConfigurationChange(
            external_change_id="pool-1",
            from_value=True,
            to_value=5,
        ),
        lambda: DependencyMaintenanceChange(
            external_change_id="redis-1",
            dependency="memcached",  # type: ignore[arg-type]
        ),
        lambda: DeploymentChange(
            external_change_id="Release With Spaces",
            from_version="stable",
            to_version="rc1",
        ),
    ],
)
def test_invalid_or_non_whitelisted_change_is_rejected(factory: object) -> None:
    with pytest.raises(ValidationError):
        factory()  # type: ignore[operator]


def test_record_requires_timezone_and_monotonic_timestamps() -> None:
    change = DependencyMaintenanceChange(external_change_id="redis-1")
    current_time = datetime(2026, 8, 30, 12, tzinfo=UTC)

    with pytest.raises(ValidationError):
        ChangeRecord(
            record_id=UUID(int=1),
            occurred_at=current_time.replace(tzinfo=None),
            recorded_at=current_time,
            change=change,
        )

    with pytest.raises(ValidationError):
        ChangeRecord(
            record_id=UUID(int=1),
            occurred_at=current_time + timedelta(seconds=1),
            recorded_at=current_time,
            change=change,
        )

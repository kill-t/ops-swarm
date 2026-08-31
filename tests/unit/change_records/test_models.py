"""变更记录精确契约测试。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from opsswarm.change_records.models import (
    CI_DEPLOYMENT_SOURCE,
    ChangeRecord,
    ConfigurationChange,
    DependencyMaintenanceChange,
    DeploymentChange,
    DeploymentCodeReference,
    change_content_hash,
)

COMMIT_SHA = "a" * 40
LEGACY_DEPLOYMENT_HASH = "040fe205932461edea2dd67af7d247fafd17ca7c4ca73b36a3f0e46e34ee5f54"


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
    assert change_content_hash(changes[0]) == LEGACY_DEPLOYMENT_HASH

    linked = DeploymentChange(
        external_change_id="release-rc1",
        from_version="stable",
        to_version="2026.01.01-rc1",
        code_reference=DeploymentCodeReference(
            repository="kill-t/ops-swarm",
            commit_sha=COMMIT_SHA,
        ),
    )
    assert change_content_hash(linked) != LEGACY_DEPLOYMENT_HASH
    assert linked.model_dump(mode="json")["code_reference"] == {
        "repository": "kill-t/ops-swarm",
        "commit_sha": COMMIT_SHA,
    }


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
        lambda: DeploymentCodeReference(
            repository="kill-t/..",
            commit_sha=COMMIT_SHA,
        ),
        lambda: DeploymentCodeReference(
            repository="kill-t/ops-swarm",
            commit_sha="not-a-commit-sha",
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


def test_ci_record_requires_non_admin_identity_and_full_deployment_reference() -> None:
    current_time = datetime(2026, 8, 30, 12, tzinfo=UTC)
    deployment = DeploymentChange(
        external_change_id="delivery-1",
        from_version="stable",
        to_version="rc1",
        code_reference=DeploymentCodeReference(
            repository="kill-t/ops-swarm",
            commit_sha=COMMIT_SHA,
        ),
    )

    record = ChangeRecord(
        record_id=UUID(int=2),
        source=CI_DEPLOYMENT_SOURCE,
        source_identity="github-actions-production",
        occurred_at=current_time,
        recorded_at=current_time,
        change=deployment,
    )

    assert record.source_identity == "github-actions-production"

    with pytest.raises(ValidationError):
        ChangeRecord(
            record_id=UUID(int=3),
            source=CI_DEPLOYMENT_SOURCE,
            source_identity="local-admin",
            occurred_at=current_time,
            recorded_at=current_time,
            change=deployment,
        )
    with pytest.raises(ValidationError):
        ChangeRecord(
            record_id=UUID(int=4),
            source=CI_DEPLOYMENT_SOURCE,
            source_identity="github-actions-production",
            occurred_at=current_time,
            recorded_at=current_time,
            change=DependencyMaintenanceChange(external_change_id="redis-ci-1"),
        )

"""发布演练的版本漂移、ABA及严格输入边界。"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from opsswarm.fault_lab.release.contracts import (
    LegacyFaultCommand,
    ReleaseCommand,
    ReleaseError,
    ReleaseState,
)
from opsswarm.fault_lab.release.policy import transition

STABLE, BAD = "stable", "2026.01.01-rc1"


def command(**changes):
    return ReleaseCommand.model_validate(
        {
            **dict(
                expected_version=STABLE,
                expected_revision=1,
                target_version=BAD,
                idempotency_key="release-cas-123",
            ),
            **changes,
        }
    )


@pytest.mark.parametrize(
    "current,target", [(STABLE, BAD), (BAD, STABLE), (STABLE, STABLE), (BAD, BAD)]
)
def test_revision_tracks_real_changes_and_noop_still_checks_prestate(current, target):
    body = command(expected_version=current, expected_revision=7, target_version=target)
    assert transition(current, 7, body) == (target, 7 + (current != target))
    with pytest.raises(ReleaseError, match="修订已变化"):
        transition(current, 8, body)


def test_aba_does_not_accept_an_old_revision_even_when_version_matches():
    old = command()
    assert transition(STABLE, 1, old) == (BAD, 2)
    assert transition(
        BAD, 2, command(expected_version=BAD, expected_revision=2, target_version=STABLE)
    ) == (STABLE, 3)
    with pytest.raises(ReleaseError):
        transition(STABLE, 3, old)


@pytest.mark.parametrize(
    "enabled,current", [(True, STABLE), (False, BAD), (True, BAD), (False, STABLE)]
)
def test_legacy_switch_uses_the_same_catalog_and_revision(enabled, current):
    target = BAD if enabled else STABLE
    body = LegacyFaultCommand(enabled=enabled, idempotency_key="legacy-command-123")
    assert transition(current, 3, body) == (target, 3 + (target != current))


@pytest.mark.parametrize("revision", [True, 0, -1, 1.0, "1", 2147483648])
def test_invalid_revision_is_rejected_at_both_contract_and_domain(revision):
    with pytest.raises(ValidationError):
        command(expected_revision=revision)
    with pytest.raises(ValueError):
        transition(STABLE, revision, command())


@pytest.mark.parametrize("field", ["expected_version", "target_version"])
@pytest.mark.parametrize("value", ["latest", "stable ", "2026.01.01-rc2", "../../secret", None, 1])
def test_only_fixed_exact_simulation_versions_are_accepted(field, value):
    with pytest.raises(ValidationError):
        command(**{field: value})


@pytest.mark.parametrize("key", ["short", " key-1234", "bad/key-123", "x" * 101])
def test_idempotency_keys_are_bounded_and_exact(key):
    with pytest.raises(ValidationError):
        command(idempotency_key=key)


@pytest.mark.parametrize(
    "field", ["actor_id", "approval_id", "url", "shell", "service_name", "revision"]
)
def test_command_cannot_smuggle_identity_or_execution_authority(field):
    with pytest.raises(ValidationError):
        command(**{field: "injected"})


def test_revision_exhaustion_does_not_wrap_or_reject_a_valid_noop():
    body = command(expected_revision=2147483647)
    with pytest.raises(ReleaseError, match="上限"):
        transition(STABLE, 2147483647, body)
    noop = command(expected_revision=2147483647, target_version=STABLE)
    assert transition(STABLE, 2147483647, noop) == (STABLE, 2147483647)


@pytest.mark.parametrize("enabled", [0, 1, "true", None])
def test_internal_legacy_commands_also_require_actual_booleans(enabled):
    with pytest.raises(ValidationError):
        LegacyFaultCommand(enabled=enabled, idempotency_key="legacy-command-123")


def test_response_cannot_claim_healthy_bad_version_or_production_identity():
    original = dict(
        version=BAD,
        revision=2,
        command_count=1,
        release_500_enabled=True,
        updated_at=datetime(2026, 10, 25, tzinfo=UTC),
    )
    state = ReleaseState.model_validate(original)
    assert state.simulation is True and state.source == "local-release-simulation-v1"
    for changes in (
        {"release_500_enabled": False},
        {"source": "deployment-environment"},
        {"simulation": False},
    ):
        with pytest.raises(ValidationError):
            ReleaseState.model_validate({**original, **changes})

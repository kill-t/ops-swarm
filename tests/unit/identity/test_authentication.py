"""固定摘要认证、严格令牌边界及统一失败契约。"""

import hashlib
from datetime import UTC, datetime, timedelta, timezone

import pytest

from opsswarm.identity.authentication import authenticate_operator
from opsswarm.identity.models import OperatorAccessError, OperatorRole
from opsswarm.identity.settings import OperatorAuthSettings, OperatorCredential

SYNTHETIC_SECRET = "unit_test_Cq8Zr2Lp6Wv4Yx9Mhn7Kf3Aa5"
ROTATION_SECRET = "unit_test_Tf6Qa4Bg8Ds2Hp9Wzr5Jk7Ln3"
NOW = datetime(2026, 10, 20, 8, tzinfo=UTC)
EXPIRES_AT = NOW + timedelta(hours=1)


def settings_with_credentials(*, secret: str = SYNTHETIC_SECRET) -> OperatorAuthSettings:
    return OperatorAuthSettings(
        mode="bearer",
        credentials=(
            OperatorCredential(
                credential_id="engineer-primary",
                subject_id="engineer-001",
                role=OperatorRole.ENGINEER,
                secret_sha256=hashlib.sha256(secret.encode("ascii")).hexdigest(),
                expires_at=EXPIRES_AT,
            ),
        ),
    )


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER", "bEaReR"])
def test_valid_bearer_resolves_only_server_configured_subject_and_role(scheme: str) -> None:
    principal = authenticate_operator(
        settings_with_credentials(),
        f"{scheme} engineer-primary.{SYNTHETIC_SECRET}",
        NOW,
    )

    assert principal.subject_id == "engineer-001"
    assert principal.credential_id == "engineer-primary"
    assert principal.role is OperatorRole.ENGINEER
    assert principal.expires_at == EXPIRES_AT
    assert SYNTHETIC_SECRET not in repr(principal)


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Bearer",
        "Basic engineer-primary." + SYNTHETIC_SECRET,
        "Bearer\tengineer-primary." + SYNTHETIC_SECRET,
        " Bearer engineer-primary." + SYNTHETIC_SECRET,
        "Bearer  engineer-primary." + SYNTHETIC_SECRET,
        "Bearer engineer-primary." + SYNTHETIC_SECRET + " ",
        "Bearer engineer-primary." + SYNTHETIC_SECRET + "\n",
        "Bearer engineer-primary." + SYNTHETIC_SECRET + ".approver",
        "Bearer Engineer-primary." + SYNTHETIC_SECRET,
        "Bearer ../engineer." + SYNTHETIC_SECRET,
        "Bearer engineer-primary." + "a" * 31,
        "Bearer engineer-primary." + "a" * 129,
        "Bearer engineer-primary." + "汉" * 32,
        "Bearer engineer-primary." + "a" * 32 + "+",
        "Bearer " + "a" * 221,
        "Bearer unknown-credential." + SYNTHETIC_SECRET,
        "Bearer engineer-primary." + ROTATION_SECRET,
    ],
)
def test_missing_malformed_unknown_and_wrong_credentials_share_public_error(
    authorization: str | None,
) -> None:
    settings = settings_with_credentials()

    with pytest.raises(OperatorAccessError) as captured:
        authenticate_operator(settings, authorization, NOW)

    error = captured.value
    assert error.code == "operator_unauthenticated"
    assert error.status_code == 401
    assert str(error) == "需要有效操作者认证"
    assert SYNTHETIC_SECRET not in str(error)
    assert settings.credentials[0].secret_sha256 not in str(error)


@pytest.mark.parametrize("length", [32, 128])
def test_minimum_and_maximum_secret_lengths_are_accepted(length: int) -> None:
    secret = (SYNTHETIC_SECRET * 4)[:length]

    principal = authenticate_operator(
        settings_with_credentials(secret=secret),
        f"Bearer engineer-primary.{secret}",
        NOW,
    )

    assert principal.subject_id == "engineer-001"


@pytest.mark.parametrize("offset", [timedelta(0), timedelta(microseconds=1), timedelta(days=1)])
def test_expiry_at_or_before_request_time_is_rejected(offset: timedelta) -> None:
    with pytest.raises(OperatorAccessError) as captured:
        authenticate_operator(
            settings_with_credentials(),
            f"Bearer engineer-primary.{SYNTHETIC_SECRET}",
            EXPIRES_AT + offset,
        )

    assert captured.value.code == "operator_unauthenticated"
    assert captured.value.status_code == 401


def test_last_instant_before_expiry_is_valid() -> None:
    principal = authenticate_operator(
        settings_with_credentials(),
        f"Bearer engineer-primary.{SYNTHETIC_SECRET}",
        EXPIRES_AT - timedelta(microseconds=1),
    )

    assert principal.subject_id == "engineer-001"


def test_expiry_comparison_uses_instant_across_timezones() -> None:
    request_time = EXPIRES_AT.astimezone(timezone(timedelta(hours=8)))

    with pytest.raises(OperatorAccessError):
        authenticate_operator(
            settings_with_credentials(),
            f"Bearer engineer-primary.{SYNTHETIC_SECRET}",
            request_time,
        )


def test_bearer_token_cannot_authenticate_in_legacy_mode() -> None:
    bearer = settings_with_credentials()
    legacy = OperatorAuthSettings(mode="legacy_local", credentials=bearer.credentials)

    with pytest.raises(OperatorAccessError) as captured:
        authenticate_operator(legacy, f"Bearer engineer-primary.{SYNTHETIC_SECRET}", NOW)

    assert captured.value.status_code == 401


def test_credential_id_and_secret_must_belong_to_same_registry_entry() -> None:
    first = settings_with_credentials().credentials[0]
    rotated = OperatorCredential(
        credential_id="engineer-rotation",
        subject_id=first.subject_id,
        role=first.role,
        secret_sha256=hashlib.sha256(ROTATION_SECRET.encode()).hexdigest(),
        expires_at=EXPIRES_AT,
    )
    settings = OperatorAuthSettings(mode="bearer", credentials=(first, rotated))

    with pytest.raises(OperatorAccessError):
        authenticate_operator(settings, f"Bearer engineer-primary.{ROTATION_SECRET}", NOW)

    principal = authenticate_operator(settings, f"Bearer engineer-rotation.{ROTATION_SECRET}", NOW)
    assert principal.subject_id == first.subject_id
    assert principal.credential_id == "engineer-rotation"


def test_digest_of_full_token_is_not_accepted_as_secret_digest() -> None:
    token = f"engineer-primary.{SYNTHETIC_SECRET}"
    settings = settings_with_credentials(secret=token)

    with pytest.raises(OperatorAccessError):
        authenticate_operator(settings, f"Bearer {token}", NOW)

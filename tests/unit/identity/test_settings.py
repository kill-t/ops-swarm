"""认证配置的失败关闭、凭据轮换约束与敏感字段隐藏。"""

import hashlib
import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from opsswarm.identity.models import OperatorRole
from opsswarm.identity.settings import OperatorAuthSettings, OperatorCredential

SYNTHETIC_SECRET = "unit_test_Cq8Zr2Lp6Wv4Yx9Mhn7Kf3Aa5"


def credential_data(**overrides: object) -> dict[str, object]:
    return {
        "credential_id": "engineer-primary",
        "subject_id": "engineer-001",
        "role": "engineer",
        "secret_sha256": hashlib.sha256(SYNTHETIC_SECRET.encode()).hexdigest(),
        "expires_at": "2026-10-20T16:00:00+08:00",
        **overrides,
    }


def test_default_mode_preserves_local_compatibility_without_credentials() -> None:
    settings = OperatorAuthSettings()

    assert settings.mode == "legacy_local"
    assert settings.credentials == ()


def test_bearer_mode_without_credentials_fails_closed() -> None:
    with pytest.raises(ValidationError, match="必须配置"):
        OperatorAuthSettings(mode="bearer")


@pytest.mark.parametrize("mode", ["", "Bearer", "disabled", "legacy", "unknown"])
def test_unknown_mode_cannot_silently_disable_authentication(mode: str) -> None:
    with pytest.raises(ValidationError):
        OperatorAuthSettings.model_validate({"mode": mode})


def test_settings_load_explicit_environment_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSSWARM_OPERATOR_AUTH_MODE", "bearer")
    monkeypatch.setenv(
        "OPSSWARM_OPERATOR_AUTH_CREDENTIALS",
        json.dumps([credential_data()]),
    )

    settings = OperatorAuthSettings()

    assert settings.mode == "bearer"
    assert len(settings.credentials) == 1
    assert settings.credentials[0].subject_id == "engineer-001"
    assert settings.credentials[0].role is OperatorRole.ENGINEER


def test_empty_environment_credentials_cannot_downgrade_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPSSWARM_OPERATOR_AUTH_MODE", "bearer")
    monkeypatch.setenv("OPSSWARM_OPERATOR_AUTH_CREDENTIALS", "[]")

    with pytest.raises(ValidationError, match="必须配置"):
        OperatorAuthSettings()


@pytest.mark.parametrize(
    "overrides",
    [
        {"credential_id": "../operator"},
        {"credential_id": "Uppercase"},
        {"credential_id": "a" * 65},
        {"subject_id": ""},
        {"subject_id": "engineer 001"},
        {"subject_id": "a" * 101},
        {"role": "admin"},
        {"secret_sha256": "a" * 63},
        {"secret_sha256": "A" * 64},
        {"expires_at": "2026-10-20T08:00:00"},
        {"expires_at": "2026-10-20"},
        {"secret": SYNTHETIC_SECRET},
        {"permissions": ["execute_action"]},
    ],
)
def test_invalid_or_expanded_credential_configuration_is_rejected(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        OperatorCredential.model_validate(credential_data(**overrides))


def test_credential_expiry_is_normalized_to_utc() -> None:
    credential = OperatorCredential.model_validate(credential_data())

    assert credential.expires_at == datetime(2026, 10, 20, 8, tzinfo=UTC)
    assert credential.expires_at.tzinfo is UTC


@pytest.mark.parametrize("duplicated_field", ["credential_id", "secret_sha256"])
def test_duplicate_credential_id_or_digest_is_rejected(duplicated_field: str) -> None:
    first = credential_data()
    second = credential_data(
        credential_id="approver-primary",
        subject_id="approver-001",
        role="approver",
        secret_sha256="b" * 64,
    )
    second[duplicated_field] = first[duplicated_field]

    with pytest.raises(ValidationError, match="重复"):
        OperatorAuthSettings.model_validate({"mode": "bearer", "credentials": [first, second]})


def test_same_subject_can_rotate_credentials_without_changing_role() -> None:
    settings = OperatorAuthSettings.model_validate(
        {
            "mode": "bearer",
            "credentials": [
                credential_data(),
                credential_data(credential_id="engineer-rotation", secret_sha256="b" * 64),
            ],
        }
    )

    assert len(settings.credentials) == 2
    assert {item.subject_id for item in settings.credentials} == {"engineer-001"}


def test_same_subject_cannot_accumulate_roles_using_multiple_credentials() -> None:
    with pytest.raises(ValidationError, match="相同角色"):
        OperatorAuthSettings.model_validate(
            {
                "mode": "bearer",
                "credentials": [
                    credential_data(),
                    credential_data(
                        credential_id="engineer-extra",
                        role="approver",
                        secret_sha256="b" * 64,
                    ),
                ],
            }
        )


def test_bounded_credential_registry_rejects_more_than_32_entries() -> None:
    credentials = [
        credential_data(
            credential_id=f"credential-{index}",
            subject_id=f"engineer-{index}",
            secret_sha256=hashlib.sha256(f"synthetic-{index}".encode()).hexdigest(),
        )
        for index in range(33)
    ]

    with pytest.raises(ValidationError):
        OperatorAuthSettings.model_validate({"mode": "bearer", "credentials": credentials})


def test_credential_digest_is_hidden_from_representations_and_serialization() -> None:
    credential = OperatorCredential.model_validate(credential_data())
    settings = OperatorAuthSettings(mode="bearer", credentials=(credential,))

    for rendered in (
        repr(credential),
        str(credential),
        credential.model_dump_json(),
        repr(settings),
        str(settings),
        settings.model_dump_json(),
    ):
        assert credential.secret_sha256 not in rendered
        assert SYNTHETIC_SECRET not in rendered
    assert "secret_sha256" not in credential.model_dump()


def test_invalid_configuration_error_hides_credential_input() -> None:
    data = credential_data(role="unknown")

    with pytest.raises(ValidationError) as captured:
        OperatorAuthSettings.model_validate({"mode": "bearer", "credentials": [data]})

    assert str(data["secret_sha256"]) not in str(captured.value)
    assert SYNTHETIC_SECRET not in str(captured.value)

import pytest

from aimanager.governance import (
    KeyGovernanceError,
    SHARED_KEY_ENFORCED_PARAMS,
    normalize_key_request,
)


def _valid_key_request() -> dict[str, object]:
    return {
        "user_id": "u_001",
        "team_id": "team_market",
        "models": ["gemini-2.5-flash", "deepseek-chat"],
        "max_budget": 100.0,
        "rpm_limit": 60,
        "tpm_limit": 120000,
        "duration": "30d",
        "metadata": {
            "owner": "alice",
            "department_id": "market",
            "project_id": "campaign-2026-q3",
            "cost_center_id": "cc-market",
            "scenario_l1": "marketing",
            "scenario_l2": "copywriting",
            "approver": "cfo",
            "internal_or_external": "internal",
        },
    }


def test_normalize_key_request_accepts_required_metadata() -> None:
    normalized = normalize_key_request(_valid_key_request())

    assert normalized["metadata"]["department_id"] == "market"
    assert normalized["metadata"]["project_id"] == "campaign-2026-q3"
    assert normalized["metadata"]["cost_center_id"] == "cc-market"
    assert normalized["models"] == ["gemini-2.5-flash", "deepseek-chat"]


def test_normalize_key_request_coerces_admin_ui_numeric_strings() -> None:
    payload = _valid_key_request()
    payload["max_budget"] = "100.5"
    payload["rpm_limit"] = "60"
    payload["tpm_limit"] = "120000"

    normalized = normalize_key_request(payload)

    assert normalized["max_budget"] == 100.5
    assert isinstance(normalized["max_budget"], float)
    assert normalized["rpm_limit"] == 60
    assert isinstance(normalized["rpm_limit"], int)
    assert normalized["tpm_limit"] == 120000
    assert isinstance(normalized["tpm_limit"], int)


@pytest.mark.parametrize(
    "field_name",
    ["user_id", "team_id", "models", "max_budget", "rpm_limit", "tpm_limit", "duration"],
)
def test_normalize_key_request_rejects_missing_top_level_fields(field_name: str) -> None:
    payload = _valid_key_request()
    payload.pop(field_name)

    with pytest.raises(KeyGovernanceError, match=field_name):
        normalize_key_request(payload)


@pytest.mark.parametrize(
    "field_name",
    [
        "owner",
        "department_id",
        "project_id",
        "cost_center_id",
        "scenario_l1",
        "scenario_l2",
        "approver",
        "internal_or_external",
    ],
)
def test_normalize_key_request_rejects_missing_metadata_fields(field_name: str) -> None:
    payload = _valid_key_request()
    metadata = dict(payload["metadata"])  # type: ignore[arg-type]
    metadata.pop(field_name)
    payload["metadata"] = metadata

    with pytest.raises(KeyGovernanceError, match=field_name):
        normalize_key_request(payload)


def test_normalize_key_request_rejects_blank_metadata_values() -> None:
    payload = _valid_key_request()
    metadata = dict(payload["metadata"])  # type: ignore[arg-type]
    metadata["cost_center_id"] = " "
    payload["metadata"] = metadata

    with pytest.raises(KeyGovernanceError, match="cost_center_id"):
        normalize_key_request(payload)


def test_normalize_key_request_rejects_model_wildcard() -> None:
    payload = _valid_key_request()
    payload["models"] = ["*"]

    with pytest.raises(KeyGovernanceError, match="models"):
        normalize_key_request(payload)


@pytest.mark.parametrize("field_name", ["max_budget", "rpm_limit", "tpm_limit"])
@pytest.mark.parametrize("bad_value", ["zero", "nan", "inf", "-inf", "-1", "0"])
def test_normalize_key_request_rejects_invalid_numeric_strings(field_name: str, bad_value: str) -> None:
    payload = _valid_key_request()
    payload[field_name] = bad_value

    with pytest.raises(KeyGovernanceError, match=field_name):
        normalize_key_request(payload)


def test_normalize_key_request_rejects_huge_integer_without_overflow() -> None:
    payload = _valid_key_request()
    payload["max_budget"] = int("9" * 400)

    with pytest.raises(KeyGovernanceError, match="max_budget"):
        normalize_key_request(payload)


def test_shared_key_adds_enforced_params() -> None:
    payload = _valid_key_request()

    normalized = normalize_key_request(payload, shared_key=True)

    assert normalized["metadata"]["enforced_params"] == SHARED_KEY_ENFORCED_PARAMS


def test_shared_key_preserves_existing_metadata() -> None:
    payload = _valid_key_request()
    metadata = dict(payload["metadata"])  # type: ignore[arg-type]
    metadata["approval_ref"] = "approval-123"
    payload["metadata"] = metadata

    normalized = normalize_key_request(payload, shared_key=True)

    assert normalized["metadata"]["approval_ref"] == "approval-123"
    assert normalized["metadata"]["enforced_params"] == SHARED_KEY_ENFORCED_PARAMS

from __future__ import annotations

import pytest

from aimanager.scripts.smoke_admin_ui_key_creation import (
    AdminUiKeyCreationError,
    build_governance_metadata,
    validate_key_generate_payload,
)


def test_build_governance_metadata_contains_required_aimanager_fields() -> None:
    metadata = build_governance_metadata("ui-smoke-123")

    assert metadata == {
        "owner": "aimanager-ui-smoke",
        "department_id": "dept_smoke",
        "project_id": "proj_aimanager_admin_ui_smoke",
        "cost_center_id": "cc_smoke",
        "scenario_l1": "engineering",
        "scenario_l2": "admin-ui-key-creation-smoke",
        "approver": "aimanager-ci",
        "internal_or_external": "internal",
        "end_user_principal": "employee-ui-smoke-001",
        "aimanager_smoke_id": "ui-smoke-123",
    }


def test_validate_key_generate_payload_accepts_governed_ui_request() -> None:
    payload = {
        "key_alias": "aimanager-ui-smoke-ui-smoke-123",
        "user_id": "aimanager-ui-smoke-user",
        "team_id": "team_aimanager_smoke",
        "models": ["gemini-2.5-flash"],
        "max_budget": 1.0,
        "rpm_limit": 60,
        "tpm_limit": 120000,
        "duration": "1h",
        "metadata": build_governance_metadata("ui-smoke-123"),
    }

    validate_key_generate_payload(
        payload,
        request_marker="ui-smoke-123",
        expected_model="gemini-2.5-flash",
    )


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("user_id", ""),
        ("team_id", None),
        ("models", []),
        ("max_budget", 0),
        ("rpm_limit", None),
        ("tpm_limit", None),
        ("duration", ""),
    ],
)
def test_validate_key_generate_payload_rejects_missing_required_top_level_fields(
    field_name: str,
    field_value: object,
) -> None:
    payload = {
        "key_alias": "aimanager-ui-smoke-ui-smoke-123",
        "user_id": "aimanager-ui-smoke-user",
        "team_id": "team_aimanager_smoke",
        "models": ["gemini-2.5-flash"],
        "max_budget": 1.0,
        "rpm_limit": 60,
        "tpm_limit": 120000,
        "duration": "1h",
        "metadata": build_governance_metadata("ui-smoke-123"),
    }
    payload[field_name] = field_value

    with pytest.raises(AdminUiKeyCreationError, match=field_name):
        validate_key_generate_payload(
            payload,
            request_marker="ui-smoke-123",
            expected_model="gemini-2.5-flash",
        )


def test_validate_key_generate_payload_rejects_missing_governance_metadata() -> None:
    metadata = build_governance_metadata("ui-smoke-123")
    del metadata["cost_center_id"]
    payload = {
        "key_alias": "aimanager-ui-smoke-ui-smoke-123",
        "user_id": "aimanager-ui-smoke-user",
        "team_id": "team_aimanager_smoke",
        "models": ["gemini-2.5-flash"],
        "max_budget": 1.0,
        "rpm_limit": 60,
        "tpm_limit": 120000,
        "duration": "1h",
        "metadata": metadata,
    }

    with pytest.raises(AdminUiKeyCreationError, match="metadata.cost_center_id"):
        validate_key_generate_payload(
            payload,
            request_marker="ui-smoke-123",
            expected_model="gemini-2.5-flash",
        )


def test_validate_key_generate_payload_rejects_wrong_marker() -> None:
    payload = {
        "key_alias": "aimanager-ui-smoke-ui-smoke-123",
        "user_id": "aimanager-ui-smoke-user",
        "team_id": "team_aimanager_smoke",
        "models": ["gemini-2.5-flash"],
        "max_budget": 1.0,
        "rpm_limit": 60,
        "tpm_limit": 120000,
        "duration": "1h",
        "metadata": build_governance_metadata("other-marker"),
    }

    with pytest.raises(AdminUiKeyCreationError, match="aimanager_smoke_id"):
        validate_key_generate_payload(
            payload,
            request_marker="ui-smoke-123",
            expected_model="gemini-2.5-flash",
        )

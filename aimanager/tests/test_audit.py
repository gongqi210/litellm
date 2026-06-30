from __future__ import annotations

import pytest

from aimanager.audit import AuditEventError, build_audit_event


def test_passthrough_blocked_event_has_stable_required_fields() -> None:
    event = build_audit_event(
        "passthrough_blocked",
        event_id="evt_passthrough_1",
        occurred_at="2026-06-30T11:00:00+08:00",
        actor="aimanager-policy",
        subject_key_alias="market-campaign-key",
        team_id="team_growth",
        department_id="dept_market",
        project_id="proj_launch",
        cost_center_id="cc_growth",
        reason="blocked provider passthrough route",
        request_id="req_blocked_1",
        metadata={"method": "POST", "path": "/anthropic/messages"},
    )

    assert event == {
        "event_id": "evt_passthrough_1",
        "event_type": "passthrough_blocked",
        "severity": "warning",
        "occurred_at": "2026-06-30T11:00:00+08:00",
        "actor": "aimanager-policy",
        "subject_key_alias": "market-campaign-key",
        "team_id": "team_growth",
        "department_id": "dept_market",
        "project_id": "proj_launch",
        "cost_center_id": "cc_growth",
        "reason": "blocked provider passthrough route",
        "request_id": "req_blocked_1",
        "metadata": {"method": "POST", "path": "/anthropic/messages"},
    }


def test_budget_blocked_event_defaults_to_high_severity() -> None:
    event = build_audit_event(
        "budget_blocked",
        event_id="evt_budget_1",
        occurred_at="2026-06-30T12:00:00+08:00",
        actor="litellm-budget",
        subject_key_alias="dev-shared-key",
        team_id="team_dev",
        department_id="dept_rd",
        project_id="proj_internal_tool",
        cost_center_id="cc_rd",
        reason="monthly budget exceeded",
        request_id="req_budget_1",
        metadata={"budget": "100.00", "current_spend": "101.20"},
    )

    assert event["severity"] == "high"
    assert event["metadata"]["budget"] == "100.00"


def test_policy_blocked_event_defaults_to_warning_severity() -> None:
    event = build_audit_event(
        "policy_blocked",
        event_id="evt_policy_1",
        occurred_at="2026-06-30T12:30:00+08:00",
        actor="aimanager-policy",
        subject_key_alias="unassigned",
        team_id="team_dev",
        department_id="dept_rd",
        project_id="proj_internal_tool",
        cost_center_id="cc_rd",
        reason="aimanager_config_immutable",
        request_id="req_policy_1",
        metadata={"method": "POST", "path": "/config/update"},
    )

    assert event["event_type"] == "policy_blocked"
    assert event["severity"] == "warning"
    assert event["reason"] == "aimanager_config_immutable"


def test_enforced_params_blocked_event_defaults_to_warning_severity() -> None:
    event = build_audit_event(
        "enforced_params_blocked",
        event_id="evt_enforced_params_1",
        occurred_at="2026-06-30T12:45:00+08:00",
        actor="aimanager-enforced-params-guard",
        subject_key_alias="market-shared-key",
        team_id="team_market",
        department_id="dept_market",
        project_id="proj_launch",
        cost_center_id="cc_growth",
        reason="aimanager_enforced_params_missing",
        request_id="req_shared_1",
        metadata={"missing_params": ["metadata.end_user_principal"]},
    )

    assert event["event_type"] == "enforced_params_blocked"
    assert event["severity"] == "warning"
    assert event["metadata"]["missing_params"] == ["metadata.end_user_principal"]


def test_key_lifecycle_event_requires_reason() -> None:
    with pytest.raises(AuditEventError, match="reason"):
        build_audit_event(
            "key_frozen",
            event_id="evt_key_1",
            occurred_at="2026-06-30T13:00:00+08:00",
            actor="admin@gongsi.local",
            subject_key_alias="unknown-shared-key",
            team_id="team_dev",
            department_id="dept_rd",
            project_id="proj_internal_tool",
            cost_center_id="cc_rd",
            reason="",
            request_id="req_key_1",
        )


def test_key_revoked_event_uses_critical_severity() -> None:
    event = build_audit_event(
        "key_revoked",
        event_id="evt_revoke_1",
        occurred_at="2026-06-30T14:00:00+08:00",
        actor="security@gongsi.local",
        subject_key_alias="leaked-key",
        team_id="team_dev",
        department_id="dept_rd",
        project_id="proj_internal_tool",
        cost_center_id="cc_rd",
        reason="credential shared outside approved workspace",
        request_id="req_revoke_1",
    )

    assert event["severity"] == "critical"
    assert event["metadata"] == {}


def test_unknown_event_type_is_rejected() -> None:
    with pytest.raises(AuditEventError, match="event_type"):
        build_audit_event(
            "model_created",
            event_id="evt_unknown",
            occurred_at="2026-06-30T15:00:00+08:00",
            actor="admin@gongsi.local",
            subject_key_alias="admin-key",
            team_id="team_platform",
            department_id="dept_platform",
            project_id="proj_aimanager",
            cost_center_id="cc_platform",
            reason="not supported by M1 audit schema",
            request_id="req_unknown",
        )


def test_blank_dimensions_are_marked_unassigned() -> None:
    event = build_audit_event(
        "passthrough_blocked",
        event_id="evt_unassigned",
        occurred_at="2026-06-30T16:00:00+08:00",
        actor="aimanager-policy",
        subject_key_alias="",
        team_id="",
        department_id="",
        project_id="",
        cost_center_id="",
        reason="blocked provider passthrough route",
        request_id="req_unassigned",
    )

    assert event["subject_key_alias"] == "unassigned"
    assert event["team_id"] == "unassigned"
    assert event["department_id"] == "unassigned"
    assert event["project_id"] == "unassigned"
    assert event["cost_center_id"] == "unassigned"

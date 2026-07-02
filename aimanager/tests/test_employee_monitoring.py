from __future__ import annotations

import csv
import json
from pathlib import Path

from aimanager.employee_monitoring import validate_employee_monitoring_controls
from aimanager.scripts.validate_employee_monitoring_policy import main

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_employee_monitoring_policy_passes_with_notice_rules_and_boundaries() -> None:
    result = validate_employee_monitoring_controls(
        policy=_policy(),
        employee_roster=_employee_roster(),
        acknowledgments=_acknowledgments(),
    )

    assert result["status"] == "PASS"
    assert result["policy_id"] == "aimanager-employee-monitoring-v1"
    assert result["policy_version"] == "2026-06"
    assert result["summary"] == {
        "active_employee_count": 2,
        "acknowledged_employee_count": 2,
        "missing_acknowledgment_count": 0,
        "rule_count": 2,
        "retention_days": 90,
    }
    assert [rule["type"] for rule in result["controls"]["rules"]] == [
        "off_hours_usage",
        "key_sharing",
    ]
    assert result["controls"]["permission_boundary"]["raw_prompt_access"] == "prohibited"
    assert result["controls"]["permission_boundary"]["customer_content_access"] == "prohibited"
    assert "direct_manager" not in result["controls"]["permission_boundary"]["allowed_review_roles"]
    assert "# AiManager 员工监控制度验收 - 2026-06" in result["markdown"]
    assert "PASS" in result["markdown"]


def test_committed_employee_monitoring_policy_register_matches_validator_contract() -> None:
    policy_file = PROJECT_ROOT / "docs/aimanager/aimanager-employee-monitoring-policy.json"
    policy = json.loads(policy_file.read_text(encoding="utf-8"))

    result = validate_employee_monitoring_controls(
        policy=policy,
        employee_roster=_employee_roster(),
        acknowledgments=_acknowledgments(),
    )

    assert result["status"] == "PASS"
    assert result["policy_id"] == "aimanager-employee-monitoring-v1"
    assert result["controls"]["permission_boundary"]["raw_prompt_access"] == "prohibited"
    assert result["controls"]["permission_boundary"]["customer_content_access"] == "prohibited"


def test_employee_monitoring_rejects_raw_prompt_or_response_monitoring() -> None:
    policy = _policy()
    policy["monitored_metadata_fields"] = [
        *policy["monitored_metadata_fields"],
        "prompt_text",
        "response_text",
    ]

    result = validate_employee_monitoring_controls(
        policy=policy,
        employee_roster=_employee_roster(),
        acknowledgments=_acknowledgments(),
    )

    assert result["status"] == "FAIL"
    assert "monitored_metadata_fields.prompt_text" in result["errors"]
    assert "monitored_metadata_fields.response_text" in result["errors"]


def test_employee_monitoring_requires_off_hours_and_key_sharing_rules() -> None:
    policy = _policy()
    policy["rules"] = [policy["rules"][0]]

    result = validate_employee_monitoring_controls(
        policy=policy,
        employee_roster=_employee_roster(),
        acknowledgments=_acknowledgments(),
    )

    assert result["status"] == "FAIL"
    assert "rules.key_sharing" in result["errors"]


def test_employee_monitoring_rejects_unbounded_manager_review_access() -> None:
    policy = _policy()
    policy["permission_boundary"]["allowed_review_roles"] = [
        "ai_platform_admin",
        "direct_manager",
    ]
    policy["permission_boundary"]["requires_hr_or_legal_for_disciplinary_action"] = False

    result = validate_employee_monitoring_controls(
        policy=policy,
        employee_roster=_employee_roster(),
        acknowledgments=_acknowledgments(),
    )

    assert result["status"] == "FAIL"
    assert "permission_boundary.allowed_review_roles.direct_manager" in result["errors"]
    assert "permission_boundary.requires_hr_or_legal_for_disciplinary_action" in result["errors"]


def test_employee_monitoring_rejects_raw_content_review_boundary() -> None:
    policy = _policy()
    policy["permission_boundary"]["raw_prompt_access"] = "security_review"
    policy["permission_boundary"]["customer_content_access"] = "case_by_case"

    result = validate_employee_monitoring_controls(
        policy=policy,
        employee_roster=_employee_roster(),
        acknowledgments=_acknowledgments(),
    )

    assert result["status"] == "FAIL"
    assert "permission_boundary.raw_prompt_access" in result["errors"]
    assert "permission_boundary.customer_content_access" in result["errors"]


def test_employee_monitoring_blocks_without_active_roster() -> None:
    result = validate_employee_monitoring_controls(
        policy=_policy(),
        employee_roster=[{"employee_id": "u_left_1", "department_id": "dept_sales", "status": "inactive"}],
        acknowledgments=_acknowledgments(),
    )

    assert result["status"] == "BLOCKED"
    assert result["detail"] == "missing active employee roster"


def test_employee_monitoring_rejects_missing_prohibited_fields_and_retention_over_cap() -> None:
    policy = _policy()
    policy["prohibited_monitoring_fields"] = ["prompt_text", "response_text"]
    policy["retention_days"] = 365

    result = validate_employee_monitoring_controls(
        policy=policy,
        employee_roster=_employee_roster(),
        acknowledgments=_acknowledgments(),
    )

    assert result["status"] == "FAIL"
    assert "retention_days" in result["errors"]
    assert "prohibited_monitoring_fields.raw_ip" in result["errors"]
    assert "prohibited_monitoring_fields.customer_content" in result["errors"]


def test_employee_monitoring_fails_when_active_employee_has_not_acknowledged_latest_notice() -> None:
    acknowledgments = _acknowledgments()
    acknowledgments[1]["notice_version"] = "2026-05"

    result = validate_employee_monitoring_controls(
        policy=_policy(),
        employee_roster=_employee_roster(),
        acknowledgments=acknowledgments,
    )

    assert result["status"] == "FAIL"
    assert result["summary"]["missing_acknowledgment_count"] == 1
    assert result["acknowledgment_gaps"] == [
        {
            "employee_id": "u_sales_1",
            "department_id": "dept_sales",
            "reason": "missing latest notice acknowledgment",
        }
    ]


def test_employee_monitoring_fails_when_acknowledgment_predates_policy_publication() -> None:
    acknowledgments = _acknowledgments()
    acknowledgments[0]["acknowledged_at"] = "2026-06-29T23:59:00+08:00"

    result = validate_employee_monitoring_controls(
        policy=_policy(),
        employee_roster=_employee_roster(),
        acknowledgments=acknowledgments,
    )

    assert result["status"] == "FAIL"
    assert result["acknowledgment_gaps"][0]["employee_id"] == "u_market_1"


def test_employee_monitoring_preserves_employee_identifier_casing() -> None:
    roster = [{"employee_id": "User_Market_01", "department_id": "Dept_Market", "status": "active"}]
    acknowledgments = [
        {
            "employee_id": "User_Market_01",
            "notice_version": "2026-06",
            "acknowledged_at": "2026-06-30T10:00:00+08:00",
            "channel": "wecom",
            "understood_purpose": True,
            "understood_scope": True,
            "understood_appeal": True,
            "understood_no_raw_content": True,
        }
    ]

    result = validate_employee_monitoring_controls(
        policy=_policy(),
        employee_roster=roster,
        acknowledgments=acknowledgments,
    )

    assert result["status"] == "PASS"
    assert result["acknowledgment_gaps"] == []


def test_employee_monitoring_does_not_merge_employee_ids_by_lowercase() -> None:
    roster = [{"employee_id": "User_Market_01", "department_id": "Dept_Market", "status": "active"}]
    acknowledgments = [
        {
            "employee_id": "user_market_01",
            "notice_version": "2026-06",
            "acknowledged_at": "2026-06-30T10:00:00+08:00",
            "channel": "wecom",
            "understood_purpose": True,
            "understood_scope": True,
            "understood_appeal": True,
            "understood_no_raw_content": True,
        }
    ]

    result = validate_employee_monitoring_controls(
        policy=_policy(),
        employee_roster=roster,
        acknowledgments=acknowledgments,
    )

    assert result["status"] == "FAIL"
    assert result["acknowledgment_gaps"] == [
        {
            "employee_id": "User_Market_01",
            "department_id": "Dept_Market",
            "reason": "missing latest notice acknowledgment",
        }
    ]


def test_employee_monitoring_output_does_not_serialize_token_like_extras() -> None:
    policy = _policy()
    policy["internal_admin_token"] = "sk-should-not-leak"
    acknowledgments = _acknowledgments()
    acknowledgments[0]["authorization"] = "Bearer should-not-leak"

    result = validate_employee_monitoring_controls(
        policy=policy,
        employee_roster=_employee_roster(),
        acknowledgments=acknowledgments,
    )

    serialized = json.dumps(result, sort_keys=True)
    assert result["status"] == "PASS"
    assert "sk-should-not-leak" not in serialized
    assert "Bearer should-not-leak" not in serialized


def test_employee_monitoring_cli_writes_json_and_markdown(tmp_path) -> None:
    policy_file = tmp_path / "policy.json"
    roster_file = tmp_path / "roster.csv"
    ack_file = tmp_path / "acknowledgments.csv"
    output_json = tmp_path / "employee-monitoring.json"
    output_markdown = tmp_path / "employee-monitoring.md"
    policy_file.write_text(json.dumps(_policy()), encoding="utf-8")
    _write_csv(roster_file, _employee_roster())
    _write_csv(ack_file, _acknowledgments())

    exit_code = main(
        [
            "--policy-file",
            str(policy_file),
            "--employee-roster-file",
            str(roster_file),
            "--acknowledgment-file",
            str(ack_file),
            "--output-json-file",
            str(output_json),
            "--output-markdown-file",
            str(output_markdown),
        ]
    )

    assert exit_code == 0
    result = json.loads(output_json.read_text(encoding="utf-8"))
    assert result["status"] == "PASS"
    assert "员工监控制度验收" in output_markdown.read_text(encoding="utf-8")


def test_employee_monitoring_cli_blocks_when_acknowledgment_file_is_missing(tmp_path) -> None:
    policy_file = tmp_path / "policy.json"
    roster_file = tmp_path / "roster.csv"
    output_json = tmp_path / "employee-monitoring.json"
    policy_file.write_text(json.dumps(_policy()), encoding="utf-8")
    _write_csv(roster_file, _employee_roster())

    exit_code = main(
        [
            "--policy-file",
            str(policy_file),
            "--employee-roster-file",
            str(roster_file),
            "--acknowledgment-file",
            str(tmp_path / "missing.csv"),
            "--output-json-file",
            str(output_json),
        ]
    )

    assert exit_code == 2
    result = json.loads(output_json.read_text(encoding="utf-8"))
    assert result["status"] == "BLOCKED"
    assert result["detail"] == "missing acknowledgment file"


def test_employee_monitoring_rejects_role_in_both_allowed_and_prohibited() -> None:
    policy = _policy()
    policy["permission_boundary"]["prohibited_roles"] = ["direct_manager", "hr"]

    result = validate_employee_monitoring_controls(
        policy=policy,
        employee_roster=_employee_roster(),
        acknowledgments=_acknowledgments(),
    )

    assert result["status"] == "FAIL"
    assert "permission_boundary.contradiction.hr" in result["errors"]


def _policy() -> dict[str, object]:
    return {
        "policy_id": "aimanager-employee-monitoring-v1",
        "version": "2026-06",
        "title": "AiManager employee monitoring notice and permission boundary",
        "published_at": "2026-06-30",
        "effective_at": "2026-07-01",
        "owner": "ai-platform",
        "notice_url": "https://intranet.example/policies/aimanager-employee-monitoring-v1",
        "notice_channels": ["wecom", "employee-handbook"],
        "monitored_metadata_fields": [
            "request_id",
            "timestamp",
            "employee_id",
            "department_id",
            "key_alias",
            "model",
            "endpoint",
            "scenario_l1",
            "scenario_l2",
            "project_id",
            "cost_center_id",
            "currency",
            "spend",
            "ip_hash",
            "user_agent_hash",
        ],
        "prohibited_monitoring_fields": [
            "prompt_text",
            "response_text",
            "raw_ip",
            "raw_user_agent",
            "customer_content",
        ],
        "retention_days": 90,
        "rules": [
            {
                "rule_id": "off-hours-usage-v1",
                "type": "off_hours_usage",
                "enabled": True,
                "severity": "warning",
                "timezone": "Asia/Shanghai",
                "workday_start": "09:00",
                "workday_end": "18:30",
                "threshold_request_count": 20,
                "threshold_spend": "50",
                "lookback_hours": 24,
                "requires_exception_ticket": True,
            },
            {
                "rule_id": "key-sharing-v1",
                "type": "key_sharing",
                "enabled": True,
                "severity": "high",
                "lookback_hours": 24,
                "distinct_ip_hash_threshold": 3,
                "distinct_user_agent_hash_threshold": 3,
                "requires_disposition": True,
            },
        ],
        "permission_boundary": {
            "allowed_review_roles": [
                "ai_platform_admin",
                "security",
                "audit",
                "hr",
                "legal",
            ],
            "prohibited_roles": ["direct_manager"],
            "raw_prompt_access": "prohibited",
            "customer_content_access": "prohibited",
            "requires_hr_or_legal_for_disciplinary_action": True,
            "employee_appeal_channel": "wecom://ai-compliance-helpdesk",
        },
    }


def _employee_roster() -> list[dict[str, str]]:
    return [
        {"employee_id": "u_market_1", "department_id": "dept_market", "status": "active"},
        {"employee_id": "u_sales_1", "department_id": "dept_sales", "status": "active"},
        {"employee_id": "u_left_1", "department_id": "dept_sales", "status": "inactive"},
    ]


def _acknowledgments() -> list[dict[str, object]]:
    return [
        {
            "employee_id": "u_market_1",
            "notice_version": "2026-06",
            "acknowledged_at": "2026-06-30T10:00:00+08:00",
            "channel": "wecom",
            "understood_purpose": True,
            "understood_scope": True,
            "understood_appeal": True,
            "understood_no_raw_content": True,
        },
        {
            "employee_id": "u_sales_1",
            "notice_version": "2026-06",
            "acknowledged_at": "2026-06-30T10:05:00+08:00",
            "channel": "employee-handbook",
            "understood_purpose": True,
            "understood_scope": True,
            "understood_appeal": True,
            "understood_no_raw_content": True,
        },
    ]


def _write_csv(path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

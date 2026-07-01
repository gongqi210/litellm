from __future__ import annotations

import json

from aimanager.scripts.validate_key_inventory import collect_key_inventory_validation, main

GENERATED_AT = "2026-07-01T02:30:00Z"


def test_key_inventory_passes_when_all_active_keys_are_governed(tmp_path) -> None:
    inventory_file = tmp_path / "keys.json"
    inventory_file.write_text(
        json.dumps(
            {
                "keys": [
                    _governed_key("market-campaign-key"),
                    _governed_key(
                        "department-shared-key",
                        metadata_extra={
                            "shared_key": True,
                            "enforced_params": [
                                "user",
                                "metadata.scenario_l1",
                                "metadata.end_user_principal",
                            ],
                        },
                    ),
                    {
                        "key_alias": "blocked-legacy-key",
                        "blocked": True,
                        "metadata": {},
                    },
                ],
                **_trusted_export_metadata(expected_total_key_count=3),
            }
        ),
        encoding="utf-8",
    )

    result = collect_key_inventory_validation(inventory_file=inventory_file, generated_at=GENERATED_AT)

    assert result["status"] == "PASS"
    assert result["summary"] == {
        "total_key_count": 3,
        "active_key_count": 2,
        "blocked_key_count": 1,
        "violation_count": 0,
    }


def test_key_inventory_passes_when_active_keys_belong_to_acknowledged_employees(tmp_path) -> None:
    inventory_file = tmp_path / "keys.json"
    policy_file, roster_file, acknowledgment_file = _employee_acknowledgment_files(tmp_path)
    inventory_file.write_text(
        json.dumps(
            {
                "keys": [_governed_key("market-campaign-key", user_id="u_market_1", department_id="dept_market")],
                **_trusted_export_metadata(expected_total_key_count=1),
            }
        ),
        encoding="utf-8",
    )

    result = collect_key_inventory_validation(
        inventory_file=inventory_file,
        employee_monitoring_policy_file=policy_file,
        employee_roster_file=roster_file,
        acknowledgment_file=acknowledgment_file,
        require_acknowledged_employees=True,
        generated_at=GENERATED_AT,
    )

    assert result["status"] == "PASS"
    assert result["employee_acknowledgment"] == {
        "policy_id": "aimanager-employee-monitoring-v1",
        "policy_version": "2026-06",
        "active_employee_count": 2,
        "acknowledged_employee_count": 2,
        "missing_acknowledgment_count": 0,
    }


def test_key_inventory_fails_keys_for_unknown_unacknowledged_or_wrong_department_employees(tmp_path) -> None:
    inventory_file = tmp_path / "keys.json"
    policy_file, roster_file, acknowledgment_file = _employee_acknowledgment_files(
        tmp_path,
        acknowledgments=[
            {
                "employee_id": "u_market_1",
                "notice_version": "2026-06",
                "acknowledged_at": "2026-06-30T10:00:00+08:00",
                "understood_purpose": "true",
                "understood_scope": "true",
                "understood_appeal": "true",
                "understood_no_raw_content": "true",
            }
        ],
    )
    inventory_file.write_text(
        json.dumps(
            {
                "keys": [
                    _governed_key("known-but-unacknowledged", user_id="u_sales_1", department_id="dept_sales"),
                    _governed_key("unknown-employee", user_id="u_shadow_1", department_id="dept_shadow"),
                    _governed_key("wrong-department", user_id="u_market_1", department_id="dept_sales"),
                ],
                **_trusted_export_metadata(expected_total_key_count=3),
            }
        ),
        encoding="utf-8",
    )

    result = collect_key_inventory_validation(
        inventory_file=inventory_file,
        employee_monitoring_policy_file=policy_file,
        employee_roster_file=roster_file,
        acknowledgment_file=acknowledgment_file,
        require_acknowledged_employees=True,
        generated_at=GENERATED_AT,
    )

    assert result["status"] == "FAIL"
    reasons = {violation["reason"] for violation in result["violations"]}
    assert "active key user_id has not acknowledged employee monitoring policy" in reasons
    assert "active key user_id is not in active employee roster" in reasons
    assert "active key department_id does not match employee roster" in reasons


def test_key_inventory_blocks_when_employee_acknowledgment_evidence_is_required_but_missing(tmp_path) -> None:
    inventory_file = tmp_path / "keys.json"
    inventory_file.write_text(
        json.dumps(
            {
                "keys": [_governed_key("market-campaign-key")],
                **_trusted_export_metadata(expected_total_key_count=1),
            }
        ),
        encoding="utf-8",
    )

    result = collect_key_inventory_validation(
        inventory_file=inventory_file,
        require_acknowledged_employees=True,
        env={},
        generated_at=GENERATED_AT,
    )

    assert result["status"] == "BLOCKED"
    assert "employee roster and acknowledgment evidence" in result["detail"]
    assert result["required_env"] == [
        "AIMANAGER_EMPLOYEE_MONITORING_POLICY_FILE",
        "AIMANAGER_EMPLOYEE_ROSTER_FILE",
        "AIMANAGER_EMPLOYEE_ACKNOWLEDGMENT_FILE",
    ]


def test_key_inventory_fails_active_legacy_keys_without_governance(tmp_path) -> None:
    inventory_file = tmp_path / "keys.json"
    inventory_file.write_text(
        json.dumps(
            {
                "keys": [
                    _governed_key("ok-key"),
                    {
                        "key_alias": "legacy-unbounded-key",
                        "user_id": "employee-2",
                        "models": ["*"],
                        "max_budget": 0,
                        "metadata": {"department_id": "sales"},
                    },
                    _governed_key(
                        "shared-without-enforced-params",
                        metadata_extra={"shared_key": True, "enforced_params": ["user"]},
                    ),
                ],
                **_trusted_export_metadata(expected_total_key_count=3),
            }
        ),
        encoding="utf-8",
    )

    result = collect_key_inventory_validation(inventory_file=inventory_file, generated_at=GENERATED_AT)

    assert result["status"] == "FAIL"
    assert result["summary"]["violation_count"] >= 2
    reasons = {violation["reason"] for violation in result["violations"]}
    assert "active key is missing required governance fields" in reasons
    assert "shared key is missing required enforced_params" in reasons
    payload = json.dumps(result, ensure_ascii=False)
    assert "legacy-unbounded-key" in payload
    assert "sk-" not in payload


def test_key_inventory_blocks_empty_or_zero_active_exports(tmp_path) -> None:
    empty_inventory_file = tmp_path / "empty.json"
    all_blocked_inventory_file = tmp_path / "all-blocked.json"
    empty_inventory_file.write_text(
        json.dumps({"keys": [], **_trusted_export_metadata(expected_total_key_count=0)}),
        encoding="utf-8",
    )
    all_blocked_inventory_file.write_text(
        json.dumps(
            {
                "keys": [
                    {"key_alias": "old-blocked-key", "blocked": True},
                    {"key_alias": "old-revoked-key", "revoked": True},
                ],
                **_trusted_export_metadata(expected_total_key_count=2),
            }
        ),
        encoding="utf-8",
    )

    empty_result = collect_key_inventory_validation(inventory_file=empty_inventory_file, generated_at=GENERATED_AT)
    all_blocked_result = collect_key_inventory_validation(inventory_file=all_blocked_inventory_file, generated_at=GENERATED_AT)

    assert empty_result["status"] == "BLOCKED"
    assert empty_result["summary"]["active_key_count"] == 0
    assert all_blocked_result["status"] == "BLOCKED"
    assert all_blocked_result["summary"] == {
        "total_key_count": 2,
        "active_key_count": 0,
        "blocked_key_count": 2,
        "violation_count": 0,
    }


def test_key_inventory_fails_raw_secret_values_without_echoing_them(tmp_path) -> None:
    inventory_file = tmp_path / "keys.json"
    inventory_file.write_text(
        json.dumps(
            {
                "keys": [
                    {
                        **_governed_key("bad-secret-key"),
                        "token": "sk-live-raw-secret-that-must-not-leak",
                    }
                ],
                **_trusted_export_metadata(expected_total_key_count=1),
            }
        ),
        encoding="utf-8",
    )

    result = collect_key_inventory_validation(inventory_file=inventory_file, generated_at=GENERATED_AT)

    payload = json.dumps(result, ensure_ascii=False)
    assert result["status"] == "FAIL"
    assert "secret-like" in result["detail"]
    assert "sk-live-raw-secret" not in payload
    assert "[redacted:secret-like-value]" in payload


def test_key_inventory_fails_unresolved_evidence_template_markers(tmp_path) -> None:
    inventory_file = tmp_path / "keys.json"
    inventory_file.write_text(
        json.dumps(
            {
                "template_marker": "TEMPLATE_DO_NOT_SUBMIT",
                "keys": [_governed_key("template-looking-key")],
                **_trusted_export_metadata(expected_total_key_count=1),
            }
        ),
        encoding="utf-8",
    )

    result = collect_key_inventory_validation(inventory_file=inventory_file, generated_at=GENERATED_AT)

    assert result["status"] == "FAIL"
    assert "template" in result["detail"]
    assert result["violations"][0]["fields"] == ["template_marker"]


def test_key_inventory_cli_writes_json_and_returns_blocked_for_missing_input(tmp_path, capsys) -> None:
    output_file = tmp_path / "result.json"

    exit_code = main(["--output-json-file", str(output_file)])

    assert exit_code == 2
    output = capsys.readouterr().out
    assert "BLOCKED key inventory validation" in output
    result = json.loads(output_file.read_text(encoding="utf-8"))
    assert result["status"] == "BLOCKED"
    assert "AIMANAGER_KEY_INVENTORY_FILE" in json.dumps(result, ensure_ascii=False)


def test_key_inventory_blocks_exports_without_trusted_provenance(tmp_path) -> None:
    inventory_file = tmp_path / "keys.json"
    inventory_file.write_text(
        json.dumps({"keys": [_governed_key("partial-handwritten-export")]}),
        encoding="utf-8",
    )

    result = collect_key_inventory_validation(inventory_file=inventory_file, generated_at=GENERATED_AT)

    assert result["status"] == "BLOCKED"
    assert "trusted export metadata" in result["detail"]
    assert result["required_fields"] == [
        "exported_at",
        "export_source",
        "export_scope",
        "exported_by",
        "expected_total_key_count",
    ]
    assert result["summary"]["total_key_count"] == 1


def test_key_inventory_fails_when_declared_total_count_does_not_match_exported_keys(tmp_path) -> None:
    inventory_file = tmp_path / "keys.json"
    inventory_file.write_text(
        json.dumps(
            {
                "keys": [_governed_key("missing-from-export")],
                **_trusted_export_metadata(expected_total_key_count=2),
            }
        ),
        encoding="utf-8",
    )

    result = collect_key_inventory_validation(inventory_file=inventory_file, generated_at=GENERATED_AT)

    assert result["status"] == "FAIL"
    assert "expected_total_key_count does not match exported key count" in result["detail"]
    assert result["violations"] == [
        {
            "key_ref": "export",
            "reason": "declared export total does not match keys list",
            "fields": ["expected_total_key_count"],
        }
    ]
    assert result["summary"]["total_key_count"] == 1


def test_key_inventory_blocks_stale_exports(tmp_path) -> None:
    inventory_file = tmp_path / "keys.json"
    inventory_file.write_text(
        json.dumps(
            {
                "keys": [_governed_key("stale-but-governed-key")],
                **_trusted_export_metadata(
                    expected_total_key_count=1,
                    exported_at="2026-07-01T00:00:00Z",
                ),
            }
        ),
        encoding="utf-8",
    )

    result = collect_key_inventory_validation(
        inventory_file=inventory_file,
        generated_at="2026-07-03T00:00:01Z",
    )

    assert result["status"] == "BLOCKED"
    assert "stale" in result["detail"]
    assert result["summary"]["total_key_count"] == 1


def test_key_inventory_blocks_exports_from_the_future(tmp_path) -> None:
    inventory_file = tmp_path / "keys.json"
    inventory_file.write_text(
        json.dumps(
            {
                "keys": [_governed_key("future-dated-key")],
                **_trusted_export_metadata(
                    expected_total_key_count=1,
                    exported_at="2026-07-01T02:40:01Z",
                ),
            }
        ),
        encoding="utf-8",
    )

    result = collect_key_inventory_validation(
        inventory_file=inventory_file,
        generated_at="2026-07-01T02:30:00Z",
    )

    assert result["status"] == "BLOCKED"
    assert "future" in result["detail"]


def test_key_inventory_fails_active_keys_without_duration(tmp_path) -> None:
    key = _governed_key("no-duration-key")
    key.pop("duration")
    inventory_file = tmp_path / "keys.json"
    inventory_file.write_text(
        json.dumps({"keys": [key], **_trusted_export_metadata(expected_total_key_count=1)}),
        encoding="utf-8",
    )

    result = collect_key_inventory_validation(inventory_file=inventory_file, generated_at=GENERATED_AT)

    assert result["status"] == "FAIL"
    assert result["violations"][0]["fields"] == ["duration"]


def test_key_inventory_fails_non_boolean_shared_key_marker(tmp_path) -> None:
    inventory_file = tmp_path / "keys.json"
    inventory_file.write_text(
        json.dumps(
            {
                "keys": [
                    _governed_key(
                        "shared-string-true-bypass",
                        metadata_extra={
                            "shared_key": "true",
                            "enforced_params": [],
                        },
                    )
                ],
                **_trusted_export_metadata(expected_total_key_count=1),
            }
        ),
        encoding="utf-8",
    )

    result = collect_key_inventory_validation(inventory_file=inventory_file, generated_at=GENERATED_AT)

    assert result["status"] == "FAIL"
    assert result["violations"] == [
        {
            "key_ref": "shared-string-true-bypass",
            "reason": "metadata.shared_key must be a boolean when present",
            "fields": ["metadata.shared_key"],
        }
    ]


def _governed_key(
    key_alias: str,
    *,
    user_id: str = "employee-1",
    department_id: str = "dept_marketing",
    metadata_extra: dict[str, object] | None = None,
) -> dict[str, object]:
    metadata = {
        "owner": "alice",
        "department_id": department_id,
        "project_id": "proj_launch",
        "cost_center_id": "cc_growth",
        "scenario_l1": "marketing",
        "scenario_l2": "campaign_brief",
        "approver": "finance-controller",
        "internal_or_external": "internal",
    }
    if metadata_extra:
        metadata.update(metadata_extra)
    return {
        "key_alias": key_alias,
        "blocked": False,
        "user_id": user_id,
        "team_id": "team-marketing",
        "models": ["gemini-2.5-flash"],
        "max_budget": 100,
        "rpm_limit": 60,
        "tpm_limit": 120000,
        "duration": "30d",
        "metadata": metadata,
    }


def _trusted_export_metadata(
    *,
    expected_total_key_count: int,
    exported_at: str = "2026-07-01T10:00:00+08:00",
) -> dict[str, object]:
    return {
        "exported_at": exported_at,
        "export_source": "litellm-production-verification-token-table",
        "export_scope": "all_virtual_keys",
        "exported_by": "security-ops",
        "expected_total_key_count": expected_total_key_count,
    }


def _employee_acknowledgment_files(
    tmp_path,
    *,
    acknowledgments: list[dict[str, object]] | None = None,
) -> tuple:
    policy_file = tmp_path / "employee-monitoring-policy.json"
    roster_file = tmp_path / "employee-roster.json"
    acknowledgment_file = tmp_path / "employee-acknowledgments.json"
    policy_file.write_text(json.dumps(_employee_monitoring_policy()), encoding="utf-8")
    roster_file.write_text(
        json.dumps(
            [
                {"employee_id": "u_market_1", "department_id": "dept_market", "status": "active"},
                {"employee_id": "u_sales_1", "department_id": "dept_sales", "status": "active"},
                {"employee_id": "u_left_1", "department_id": "dept_sales", "status": "inactive"},
            ]
        ),
        encoding="utf-8",
    )
    acknowledgment_file.write_text(
        json.dumps(
            acknowledgments
            or [
                {
                    "employee_id": "u_market_1",
                    "notice_version": "2026-06",
                    "acknowledged_at": "2026-06-30T10:00:00+08:00",
                    "understood_purpose": "true",
                    "understood_scope": "true",
                    "understood_appeal": "true",
                    "understood_no_raw_content": "true",
                },
                {
                    "employee_id": "u_sales_1",
                    "notice_version": "2026-06",
                    "acknowledged_at": "2026-06-30T10:05:00+08:00",
                    "understood_purpose": "true",
                    "understood_scope": "true",
                    "understood_appeal": "true",
                    "understood_no_raw_content": "true",
                },
            ]
        ),
        encoding="utf-8",
    )
    return policy_file, roster_file, acknowledgment_file


def _employee_monitoring_policy() -> dict[str, object]:
    return {
        "policy_id": "aimanager-employee-monitoring-v1",
        "version": "2026-06",
        "title": "AiManager employee monitoring notice and permission boundary",
        "published_at": "2026-06-30",
        "effective_at": "2026-07-01",
        "owner": "ai-platform",
        "notice_url": "https://intranet.internal/policies/aimanager-employee-monitoring-v1",
        "notice_channels": ["wecom", "employee-handbook"],
        "monitored_metadata_fields": [
            "request_id",
            "timestamp",
            "employee_id",
            "department_id",
            "key_alias",
            "scenario_l1",
            "cost_center_id",
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
            "allowed_review_roles": ["ai_platform_admin", "security", "audit", "hr", "legal"],
            "prohibited_roles": ["direct_manager"],
            "raw_prompt_access": "prohibited",
            "customer_content_access": "prohibited",
            "requires_hr_or_legal_for_disciplinary_action": True,
            "employee_appeal_channel": "wecom://ai-compliance-helpdesk",
        },
    }

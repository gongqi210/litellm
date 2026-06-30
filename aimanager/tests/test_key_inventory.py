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


def _governed_key(key_alias: str, *, metadata_extra: dict[str, object] | None = None) -> dict[str, object]:
    metadata = {
        "owner": "alice",
        "department_id": "dept_marketing",
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
        "user_id": "employee-1",
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

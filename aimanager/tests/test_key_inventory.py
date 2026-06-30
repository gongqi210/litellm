from __future__ import annotations

import json

from aimanager.scripts.validate_key_inventory import collect_key_inventory_validation, main


def test_key_inventory_passes_when_all_active_keys_are_governed(tmp_path) -> None:
    inventory_file = tmp_path / "keys.json"
    inventory_file.write_text(
        json.dumps(
            {
                "exported_at": "2026-07-01T10:00:00+08:00",
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
            }
        ),
        encoding="utf-8",
    )

    result = collect_key_inventory_validation(inventory_file=inventory_file)

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
            }
        ),
        encoding="utf-8",
    )

    result = collect_key_inventory_validation(inventory_file=inventory_file)

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
    empty_inventory_file.write_text(json.dumps({"keys": []}), encoding="utf-8")
    all_blocked_inventory_file.write_text(
        json.dumps(
            {
                "keys": [
                    {"key_alias": "old-blocked-key", "blocked": True},
                    {"key_alias": "old-revoked-key", "revoked": True},
                ]
            }
        ),
        encoding="utf-8",
    )

    empty_result = collect_key_inventory_validation(inventory_file=empty_inventory_file)
    all_blocked_result = collect_key_inventory_validation(inventory_file=all_blocked_inventory_file)

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
                ]
            }
        ),
        encoding="utf-8",
    )

    result = collect_key_inventory_validation(inventory_file=inventory_file)

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
        "metadata": metadata,
    }

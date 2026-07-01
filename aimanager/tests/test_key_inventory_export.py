from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from aimanager.scripts import export_key_inventory as export_module
from aimanager.scripts.export_key_inventory import export_key_inventory
from aimanager.scripts.smoke_live_ycapi import HttpResponse
from aimanager.scripts.validate_key_inventory import collect_key_inventory_validation

GENERATED_AT = "2026-07-01T02:30:00Z"


def test_export_key_inventory_fetches_litellm_key_list_and_writes_metadata_only_inventory(tmp_path) -> None:
    output_file = tmp_path / "key-inventory.json"
    calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        calls.append((method, url, headers, body))
        return HttpResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {
                    "keys": [
                        {
                            **_governed_key("marketing-key"),
                            "key_name": "sk-...aB3d",
                            "key": "sk-live-raw-key-that-must-not-be-exported",
                            "token": "sk-live-token-that-must-not-be-exported",
                            "key_hash": "hash-not-needed-for-evidence",
                            "created_by": "ops@example.com",
                            "metadata": {
                                **_governed_key("marketing-key")["metadata"],
                                "shared_key": True,
                                "enforced_params": [
                                    "user",
                                    "metadata.scenario_l1",
                                    "metadata.end_user_principal",
                                ],
                                "raw_customer_note": "drop this extra field",
                            },
                        }
                    ],
                    "total": 1,
                }
            ).encode("utf-8"),
        )

    result = export_key_inventory(
        admin_base_url="https://admin.aimanager.local",
        admin_key="litellm-master-secret",
        exported_by="security-ops",
        output_file=output_file,
        generated_at=GENERATED_AT,
        fetch=fetch,
    )

    assert result["status"] == "PASS"
    assert result["summary"]["exported_key_count"] == 1
    assert calls == [
        (
            "GET",
            "https://admin.aimanager.local/key/list?page=1&size=100&include_team_keys=true&return_full_object=true",
            {
                "Authorization": "Bearer litellm-master-secret",
                "Accept": "application/json",
                "x-request-id": "aimanager-key-inventory-export",
            },
            None,
        )
    ]
    inventory = json.loads(output_file.read_text(encoding="utf-8"))
    serialized_inventory = json.dumps(inventory, ensure_ascii=False)
    assert inventory["export_scope"] == "all_virtual_keys"
    assert inventory["export_source"] == "litellm-management-api:/key/list"
    assert inventory["exported_by"] == "security-ops"
    assert inventory["expected_total_key_count"] == 1
    assert inventory["keys"] == [
        {
            "key_alias": "marketing-key",
            "blocked": False,
            "user_id": "employee-1",
            "team_id": "team-marketing",
            "models": ["gemini-2.5-flash"],
            "max_budget": 100,
            "rpm_limit": 60,
            "tpm_limit": 120000,
            "duration": "30d",
            "metadata": {
                "owner": "alice",
                "department_id": "dept_marketing",
                "project_id": "proj_launch",
                "cost_center_id": "cc_growth",
                "scenario_l1": "marketing",
                "scenario_l2": "campaign_brief",
                "approver": "finance-controller",
                "internal_or_external": "internal",
                "shared_key": True,
                "enforced_params": [
                    "user",
                    "metadata.scenario_l1",
                    "metadata.end_user_principal",
                ],
            },
        }
    ]
    assert "sk-live" not in serialized_inventory
    assert "sk-...aB3d" not in serialized_inventory
    assert "key_name" not in serialized_inventory
    assert "hash-not-needed" not in serialized_inventory
    assert "raw_customer_note" not in serialized_inventory
    validation = collect_key_inventory_validation(inventory_file=output_file, generated_at=GENERATED_AT)
    assert validation["status"] == "PASS"


def test_export_key_inventory_walks_litellm_paginated_key_list_before_writing_inventory(tmp_path) -> None:
    output_file = tmp_path / "key-inventory.json"
    calls: list[str] = []

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        calls.append(url)
        if "page=1" in url:
            return HttpResponse(
                status_code=200,
                headers={"content-type": "application/json"},
                body=json.dumps(
                    {
                        "keys": [_governed_key("first-page-key")],
                        "total_count": 2,
                        "current_page": 1,
                        "total_pages": 2,
                    }
                ).encode("utf-8"),
            )
        if "page=2" in url:
            return HttpResponse(
                status_code=200,
                headers={"content-type": "application/json"},
                body=json.dumps(
                    {
                        "keys": [_governed_key("second-page-key")],
                        "total_count": 2,
                        "current_page": 2,
                        "total_pages": 2,
                    }
                ).encode("utf-8"),
            )
        raise AssertionError(f"unexpected page URL: {url}")

    result = export_key_inventory(
        admin_base_url="https://admin.aimanager.local",
        admin_key="litellm-master-secret",
        exported_by="security-ops",
        output_file=output_file,
        generated_at=GENERATED_AT,
        fetch=fetch,
    )

    assert result["status"] == "PASS"
    assert result["summary"]["exported_key_count"] == 2
    assert result["summary"]["expected_total_key_count"] == 2
    assert calls == [
        "https://admin.aimanager.local/key/list?page=1&size=100&include_team_keys=true&return_full_object=true",
        "https://admin.aimanager.local/key/list?page=2&size=100&include_team_keys=true&return_full_object=true",
    ]
    inventory = json.loads(output_file.read_text(encoding="utf-8"))
    assert [item["key_alias"] for item in inventory["keys"]] == ["first-page-key", "second-page-key"]
    validation = collect_key_inventory_validation(inventory_file=output_file, generated_at=GENERATED_AT)
    assert validation["status"] == "PASS"


def test_export_key_inventory_fails_partial_paginated_exports_without_writing_inventory(tmp_path) -> None:
    output_file = tmp_path / "key-inventory.json"

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        return HttpResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            body=json.dumps({"keys": [_governed_key("only-first-page")], "total": 2}).encode("utf-8"),
        )

    result = export_key_inventory(
        admin_base_url="http://localhost:4001",
        admin_key="litellm-master-secret",
        exported_by="security-ops",
        output_file=output_file,
        generated_at=GENERATED_AT,
        fetch=fetch,
    )

    assert result["status"] == "FAIL"
    assert "partial" in result["detail"]
    assert result["summary"]["expected_total_key_count"] == 2
    assert not output_file.exists()


def test_export_key_inventory_fails_without_trusted_total_count(tmp_path) -> None:
    output_file = tmp_path / "key-inventory.json"

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        return HttpResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            body=json.dumps({"keys": [_governed_key("missing-total")]}).encode("utf-8"),
        )

    result = export_key_inventory(
        admin_base_url="http://localhost:4001",
        admin_key="litellm-master-secret",
        exported_by="security-ops",
        output_file=output_file,
        generated_at=GENERATED_AT,
        fetch=fetch,
    )

    assert result["status"] == "FAIL"
    assert "total" in result["detail"]
    assert not output_file.exists()


def test_export_key_inventory_fails_malformed_litellm_responses_without_writing_inventory(tmp_path) -> None:
    output_file = tmp_path / "key-inventory.json"

    cases = [
        HttpResponse(status_code=401, headers={}, body=b'{"error":"unauthorized"}'),
        HttpResponse(status_code=200, headers={}, body=b"not json"),
        HttpResponse(status_code=200, headers={}, body=b"[]"),
        HttpResponse(status_code=200, headers={}, body=b'{"total_count": 1}'),
    ]

    for index, response in enumerate(cases):
        case_output = output_file.with_name(f"key-inventory-{index}.json")

        def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
            return response

        result = export_key_inventory(
            admin_base_url="http://localhost:4001",
            admin_key="litellm-master-secret",
            exported_by="security-ops",
            output_file=case_output,
            generated_at=GENERATED_AT,
            fetch=fetch,
        )

        assert result["status"] == "FAIL"
        assert not case_output.exists()


def test_export_key_inventory_fails_transport_error_without_writing_inventory(tmp_path) -> None:
    output_file = tmp_path / "key-inventory.json"

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        raise OSError("connection refused")

    result = export_key_inventory(
        admin_base_url="http://localhost:4001",
        admin_key="litellm-master-secret",
        exported_by="security-ops",
        output_file=output_file,
        generated_at=GENERATED_AT,
        fetch=fetch,
    )

    assert result["status"] == "FAIL"
    assert "request failed" in result["detail"]
    assert not output_file.exists()


def test_export_key_inventory_fails_when_pagination_total_changes_without_writing_inventory(tmp_path) -> None:
    output_file = tmp_path / "key-inventory.json"

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        if "page=1" in url:
            payload = {"keys": [_governed_key("first-page-key")], "total_count": 2, "current_page": 1, "total_pages": 2}
        else:
            payload = {"keys": [_governed_key("second-page-key")], "total_count": 3, "current_page": 2, "total_pages": 2}
        return HttpResponse(status_code=200, headers={}, body=json.dumps(payload).encode("utf-8"))

    result = export_key_inventory(
        admin_base_url="http://localhost:4001",
        admin_key="litellm-master-secret",
        exported_by="security-ops",
        output_file=output_file,
        generated_at=GENERATED_AT,
        fetch=fetch,
    )

    assert result["status"] == "FAIL"
    assert "total changed" in result["detail"]
    assert not output_file.exists()


def test_export_key_inventory_fails_unexpected_current_page_without_writing_inventory(tmp_path) -> None:
    output_file = tmp_path / "key-inventory.json"

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        payload = {"keys": [_governed_key("wrong-page")], "total_count": 1, "current_page": 2, "total_pages": 1}
        return HttpResponse(status_code=200, headers={}, body=json.dumps(payload).encode("utf-8"))

    result = export_key_inventory(
        admin_base_url="http://localhost:4001",
        admin_key="litellm-master-secret",
        exported_by="security-ops",
        output_file=output_file,
        generated_at=GENERATED_AT,
        fetch=fetch,
    )

    assert result["status"] == "FAIL"
    assert "current_page" in result["detail"]
    assert not output_file.exists()


def test_export_key_inventory_fails_non_terminating_pagination_without_writing_inventory(tmp_path, monkeypatch) -> None:
    output_file = tmp_path / "key-inventory.json"
    monkeypatch.setattr(export_module, "_MAX_PAGES", 2)

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        query = parse_qs(urlparse(url).query)
        page = int(query["page"][0])
        payload = {"keys": [], "total_count": 1, "current_page": page, "has_more": True}
        return HttpResponse(status_code=200, headers={}, body=json.dumps(payload).encode("utf-8"))

    result = export_key_inventory(
        admin_base_url="http://localhost:4001",
        admin_key="litellm-master-secret",
        exported_by="security-ops",
        output_file=output_file,
        generated_at=GENERATED_AT,
        fetch=fetch,
    )

    assert result["status"] == "FAIL"
    assert "pagination did not terminate" in result["detail"]
    assert not output_file.exists()


def test_export_key_inventory_cli_keeps_pass_when_optional_result_file_write_fails(tmp_path, monkeypatch, capsys) -> None:
    output_file = tmp_path / "key-inventory.json"

    def fake_export_key_inventory(**kwargs: object) -> dict[str, object]:
        path = kwargs["output_file"]
        assert isinstance(path, Path)
        path.write_text("{}\n", encoding="utf-8")
        return {
            "status": "PASS",
            "generated_at": GENERATED_AT,
            "detail": "metadata-only LiteLLM virtual-key inventory exported",
            "status_code": 200,
            "summary": {"exported_key_count": 1, "expected_total_key_count": 1, "violation_count": 0},
            "violations": [],
        }

    monkeypatch.setenv("LITELLM_MASTER_KEY", "litellm-master-secret")
    monkeypatch.setattr(export_module, "export_key_inventory", fake_export_key_inventory)

    exit_code = export_module.main(
        [
            "--output-inventory-file",
            str(output_file),
            "--output-json-file",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "PASS key inventory export" in captured.out
    assert "WARN key inventory export result file not written" in captured.err
    assert output_file.exists()


def test_export_key_inventory_fails_when_metadata_only_inventory_would_still_contain_secret_like_values(tmp_path) -> None:
    output_file = tmp_path / "key-inventory.json"

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        key = _governed_key("secret-in-metadata")
        key["metadata"] = {**key["metadata"], "owner": "sk-secret-owner-value"}
        return HttpResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            body=json.dumps({"keys": [key], "total": 1}).encode("utf-8"),
        )

    result = export_key_inventory(
        admin_base_url="http://localhost:4001",
        admin_key="litellm-master-secret",
        exported_by="security-ops",
        output_file=output_file,
        generated_at=GENERATED_AT,
        fetch=fetch,
    )

    payload = json.dumps(result, ensure_ascii=False)
    assert result["status"] == "FAIL"
    assert "secret-like" in result["detail"]
    assert "sk-secret-owner-value" not in payload
    assert "[redacted:secret-like-value]" in payload
    assert not output_file.exists()


def test_export_key_inventory_blocks_without_admin_key(tmp_path) -> None:
    output_file = tmp_path / "key-inventory.json"
    calls: list[object] = []

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        calls.append((method, url, headers, body))
        raise AssertionError("fetch should not be called without admin key")

    result = export_key_inventory(
        admin_base_url="http://localhost:4001",
        admin_key="",
        exported_by="security-ops",
        output_file=output_file,
        generated_at=GENERATED_AT,
        fetch=fetch,
    )

    assert result["status"] == "BLOCKED"
    assert result["required_env"] == ["LITELLM_MASTER_KEY"]
    assert calls == []
    assert not output_file.exists()


def _governed_key(key_alias: str) -> dict[str, object]:
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
        "metadata": {
            "owner": "alice",
            "department_id": "dept_marketing",
            "project_id": "proj_launch",
            "cost_center_id": "cc_growth",
            "scenario_l1": "marketing",
            "scenario_l2": "campaign_brief",
            "approver": "finance-controller",
            "internal_or_external": "internal",
        },
    }

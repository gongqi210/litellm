import json

from aimanager.scripts.validate_work_context import main
from aimanager.work_context import validate_work_context


def _valid_external_marketing_context() -> dict[str, object]:
    return {
        "work_item_id": "mk-2026-q3-launch-001",
        "employee_id": "u_market_1",
        "department_id": "dept_market",
        "end_user_principal": "u_market_1",
        "scenario_l1": "marketing",
        "scenario_l2": "wechat_article",
        "internal_or_external": "external",
        "channel": "wechat",
        "project_id": "proj_launch_q3",
        "sensitivity_level": "internal",
        "approval_required": True,
        "workflow": {
            "brief_ref": "brief://mk-2026-q3-launch-001",
            "human_reviewer": "u_marketing_lead",
            "approval_policy_ref": "policy://brand-external-content-v1",
            "draft_ref": "draft://mk-2026-q3-launch-001",
            "human_review_ref": "review://mk-2026-q3-launch-001",
            "final_ref": "final://mk-2026-q3-launch-001",
            "external_approval_ref": "approval://mk-2026-q3-launch-001",
            "archive_ref": "archive://mk-2026-q3-launch-001",
            "retrospective_ref": "retro://mk-2026-q3-launch-001",
        },
        "brand_safety": {
            "policy_ref": "policy://brand-safety-v1",
            "brand_voice_checked": True,
            "forbidden_commitments_checked": True,
            "price_or_effect_claims_checked": True,
            "competitor_comparison_checked": True,
            "customer_case_checked": True,
            "copyright_checked": True,
            "portrait_rights_checked": True,
            "fact_check_required": True,
            "external_approval_required": True,
        },
    }


def test_work_context_preflight_accepts_external_marketing_context() -> None:
    result = validate_work_context(_valid_external_marketing_context(), mode="preflight")

    assert result.status == "PASS"
    assert result.normalized_context["scenario_l1"] == "marketing"
    assert result.normalized_context["scenario_l2"] == "wechat_article"
    assert result.normalized_context["workflow_mode"] == "preflight"
    assert result.normalized_context["requires_external_approval"] is True


def test_work_context_closure_requires_full_marketing_workflow() -> None:
    payload = _valid_external_marketing_context()
    workflow = dict(payload["workflow"])  # type: ignore[arg-type]
    workflow.pop("archive_ref")
    payload["workflow"] = workflow

    result = validate_work_context(payload, mode="closure")

    assert result.status == "FAIL"
    assert "workflow.archive_ref" in result.errors


def test_work_context_preflight_accepts_request_time_workflow_only() -> None:
    payload = _valid_external_marketing_context()
    payload["workflow"] = {
        "brief_ref": "brief://mk-2026-q3-launch-001",
        "human_reviewer": "u_marketing_lead",
        "approval_policy_ref": "policy://brand-external-content-v1",
    }

    preflight_result = validate_work_context(payload, mode="preflight")
    closure_result = validate_work_context(payload, mode="closure")

    assert preflight_result.status == "PASS"
    assert closure_result.status == "FAIL"
    assert "workflow.final_ref" in closure_result.errors


def test_work_context_rejects_external_content_without_approval_required() -> None:
    payload = _valid_external_marketing_context()
    payload["approval_required"] = False

    result = validate_work_context(payload, mode="preflight")

    assert result.status == "FAIL"
    assert "approval_required" in result.errors


def test_work_context_rejects_invalid_scenario_pair() -> None:
    payload = _valid_external_marketing_context()
    payload["scenario_l2"] = "code_assist"

    result = validate_work_context(payload, mode="preflight")

    assert result.status == "FAIL"
    assert "scenario_l2" in " ".join(result.errors)


def test_work_context_rejects_external_marketing_without_brand_safety() -> None:
    payload = _valid_external_marketing_context()
    brand_safety = dict(payload["brand_safety"])  # type: ignore[arg-type]
    brand_safety["fact_check_required"] = False
    payload["brand_safety"] = brand_safety

    result = validate_work_context(payload, mode="preflight")

    assert result.status == "FAIL"
    assert "brand_safety.fact_check_required" in result.errors


def test_work_context_rejects_non_string_workflow_refs() -> None:
    payload = _valid_external_marketing_context()
    workflow = dict(payload["workflow"])  # type: ignore[arg-type]
    workflow["brief_ref"] = False
    payload["workflow"] = workflow

    result = validate_work_context(payload, mode="preflight")

    assert result.status == "FAIL"
    assert "workflow.brief_ref" in result.errors


def test_work_context_rejects_non_string_contract_fields() -> None:
    payload = _valid_external_marketing_context()
    payload["work_item_id"] = 123
    payload["project_id"] = 456
    brand_safety = dict(payload["brand_safety"])  # type: ignore[arg-type]
    brand_safety["policy_ref"] = 789
    payload["brand_safety"] = brand_safety

    result = validate_work_context(payload, mode="preflight")

    assert result.status == "FAIL"
    assert "work_item_id" in result.errors
    assert "project_id" in result.errors
    assert "project_id_or_customer_id" in result.errors
    assert "brand_safety.policy_ref" in result.errors


def test_work_context_rejects_missing_project_or_customer() -> None:
    payload = _valid_external_marketing_context()
    payload.pop("project_id")

    result = validate_work_context(payload, mode="preflight")

    assert result.status == "FAIL"
    assert "project_id_or_customer_id" in result.errors


def test_work_context_accepts_customer_without_project() -> None:
    payload = _valid_external_marketing_context()
    payload.pop("project_id")
    payload["customer_id"] = "cust_launch_q3"

    result = validate_work_context(payload, mode="preflight")

    assert result.status == "PASS"
    assert result.normalized_context["customer_id"] == "cust_launch_q3"


def test_work_context_rejects_invalid_enums() -> None:
    payload = _valid_external_marketing_context()
    payload["internal_or_external"] = "partner"
    payload["sensitivity_level"] = "secret"

    result = validate_work_context(payload, mode="preflight")

    assert result.status == "FAIL"
    assert "internal_or_external" in result.errors
    assert "sensitivity_level" in result.errors


def test_work_context_cli_writes_json_and_returns_pass(tmp_path, capsys) -> None:
    context_file = tmp_path / "context.json"
    output_file = tmp_path / "result.json"
    context_file.write_text(json.dumps(_valid_external_marketing_context()), encoding="utf-8")

    exit_code = main(["--context-file", str(context_file), "--mode", "closure", "--output-json-file", str(output_file)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "PASS work context" in output
    result = json.loads(output_file.read_text(encoding="utf-8"))
    assert result["status"] == "PASS"


def test_work_context_cli_writes_json_and_returns_fail(tmp_path, capsys) -> None:
    payload = _valid_external_marketing_context()
    payload["scenario_l2"] = "code_assist"
    context_file = tmp_path / "context.json"
    output_file = tmp_path / "result.json"
    context_file.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = main(["--context-file", str(context_file), "--mode", "preflight", "--output-json-file", str(output_file)])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL work context" in output
    result = json.loads(output_file.read_text(encoding="utf-8"))
    assert result["status"] == "FAIL"
    assert "scenario_l2" in result["errors"]


def test_work_context_cli_fails_malformed_json(tmp_path, capsys) -> None:
    context_file = tmp_path / "context.json"
    output_file = tmp_path / "result.json"
    context_file.write_text("{not-json", encoding="utf-8")

    exit_code = main(["--context-file", str(context_file), "--mode", "preflight", "--output-json-file", str(output_file)])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL work context" in output
    result = json.loads(output_file.read_text(encoding="utf-8"))
    assert result["status"] == "FAIL"
    assert result["errors"] == ["context_file"]


def test_work_context_cli_blocks_when_context_file_missing(tmp_path, capsys) -> None:
    output_file = tmp_path / "result.json"

    exit_code = main(
        ["--context-file", str(tmp_path / "missing.json"), "--mode", "preflight", "--output-json-file", str(output_file)]
    )

    output = capsys.readouterr().out
    assert exit_code == 2
    assert "BLOCKED work context" in output
    result = json.loads(output_file.read_text(encoding="utf-8"))
    assert result["status"] == "BLOCKED"

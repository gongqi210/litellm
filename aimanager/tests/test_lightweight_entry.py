import json

from aimanager.lightweight_entry import (
    HttpResponse,
    prepare_lightweight_entry,
    submit_lightweight_entry,
)
from aimanager.scripts.submit_lightweight_entry import main


def _valid_form() -> dict[str, object]:
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
        "cost_center_id": "cc_marketing_growth",
        "currency": "CNY",
        "pricing_version": "m2-trial-v1",
        "sensitivity_level": "internal",
        "approval_required": True,
        "brief_ref": "brief://mk-2026-q3-launch-001",
        "human_reviewer": "u_marketing_lead",
        "approval_policy_ref": "policy://brand-external-content-v1",
        "brand_safety_policy_ref": "policy://brand-safety-v1",
        "brand_voice_checked": True,
        "forbidden_commitments_checked": True,
        "price_or_effect_claims_checked": True,
        "competitor_comparison_checked": True,
        "customer_case_checked": True,
        "copyright_checked": True,
        "portrait_rights_checked": True,
        "fact_check_required": True,
        "external_approval_required": True,
        "model": "gemini-2.5-flash",
        "prompt": "Write a launch article draft for internal review.",
        "max_tokens": 256,
    }


def test_lightweight_entry_prepares_governed_chat_request_from_flat_form() -> None:
    result = prepare_lightweight_entry(_valid_form())

    assert result.status == "PASS"
    assert result.request is not None
    assert result.request["method"] == "POST"
    assert result.request["path"] == "/v1/chat/completions"
    assert "headers" not in result.request
    body = result.request["body"]
    assert body["model"] == "gemini-2.5-flash"
    assert body["user"] == "u_market_1"
    assert body["messages"] == [{"role": "user", "content": "Write a launch article draft for internal review."}]
    assert body["max_tokens"] == 256
    metadata = body["metadata"]
    assert metadata["entry_type"] == "lightweight_non_sdk"
    assert metadata["work_item_id"] == "mk-2026-q3-launch-001"
    assert metadata["department_id"] == "dept_market"
    assert metadata["project_id"] == "proj_launch_q3"
    assert metadata["cost_center_id"] == "cc_marketing_growth"
    assert metadata["currency"] == "CNY"
    assert metadata["pricing_version"] == "m2-trial-v1"
    assert metadata["scenario_l1"] == "marketing"
    assert metadata["scenario_l2"] == "wechat_article"
    assert metadata["internal_or_external"] == "external"
    assert metadata["end_user_principal"] == "u_market_1"
    assert metadata["requires_external_approval"] is True
    assert metadata["approval_required"] is True
    assert metadata["workflow"] == {
        "brief_ref": "brief://mk-2026-q3-launch-001",
        "human_reviewer": "u_marketing_lead",
        "approval_policy_ref": "policy://brand-external-content-v1",
    }
    assert metadata["brand_safety"] == {
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
    }
    assert "YCAPI_API_TOKEN" not in json.dumps(result.to_dict())
    assert "Authorization" not in json.dumps(result.to_dict())


def test_lightweight_entry_rejects_missing_prompt() -> None:
    form = _valid_form()
    form["prompt"] = " "

    result = prepare_lightweight_entry(form)

    assert result.status == "FAIL"
    assert "prompt" in result.errors
    assert result.request is None


def test_lightweight_entry_rejects_missing_finance_metadata() -> None:
    form = _valid_form()
    form.pop("cost_center_id")

    result = prepare_lightweight_entry(form)

    assert result.status == "FAIL"
    assert "cost_center_id" in result.errors
    assert result.request is None


def test_lightweight_entry_preserves_identifier_casing_for_finance_attribution() -> None:
    form = _valid_form()
    form["work_item_id"] = "MK-Launch-Q3"
    form["employee_id"] = "User_Market_01"
    form["department_id"] = "Dept_Market"
    form["end_user_principal"] = "User_Market_01"
    form["project_id"] = "Proj_Launch_Q3"
    form["cost_center_id"] = "CC_Marketing_Growth"

    result = prepare_lightweight_entry(form)

    assert result.status == "PASS"
    assert result.request is not None
    body = result.request["body"]
    metadata = body["metadata"]
    assert body["user"] == "User_Market_01"
    assert metadata["work_item_id"] == "MK-Launch-Q3"
    assert metadata["employee_id"] == "User_Market_01"
    assert metadata["department_id"] == "Dept_Market"
    assert metadata["project_id"] == "Proj_Launch_Q3"
    assert metadata["cost_center_id"] == "CC_Marketing_Growth"


def test_lightweight_entry_rejects_non_chat_model() -> None:
    form = _valid_form()
    form["model"] = "ycapi-image-1"

    result = prepare_lightweight_entry(form)

    assert result.status == "FAIL"
    assert "model" in result.errors
    assert result.request is None


def test_lightweight_entry_rejects_token_like_form_fields() -> None:
    form = _valid_form()
    ycapi_token_value = "ycapi-" + "token-secret"
    employee_header_value = "Bearer " + "employee-secret"
    form["ycapi_api_token"] = ycapi_token_value
    form["authorization"] = employee_header_value

    result = prepare_lightweight_entry(form)

    assert result.status == "FAIL"
    assert "forbidden_secret_field:authorization" in result.errors
    assert "forbidden_secret_field:ycapi_api_token" in result.errors
    assert result.request is None
    serialized = json.dumps(result.to_dict())
    assert ycapi_token_value not in serialized
    assert "employee-secret" not in serialized


def test_lightweight_entry_rejects_token_like_form_values_without_echoing_them() -> None:
    form = _valid_form()
    bearer_value = "Bearer " + "employee-secret-value"
    openai_like_value = "sk-" + "secretvalue123456789012345"
    form["prompt"] = f"Use {bearer_value} in the draft"
    form["notes"] = openai_like_value

    result = prepare_lightweight_entry(form)

    assert result.status == "FAIL"
    assert "forbidden_secret_value:notes" in result.errors
    assert "forbidden_secret_value:prompt" in result.errors
    serialized = json.dumps(result.to_dict())
    assert "employee-secret-value" not in serialized
    assert openai_like_value not in serialized


def test_lightweight_entry_scrubs_work_context_when_context_field_contains_secret_like_value() -> None:
    form = _valid_form()
    secret_like_department = "Bearer " + "department-secret-value"
    form["department_id"] = secret_like_department

    result = prepare_lightweight_entry(form)

    assert result.status == "FAIL"
    assert "forbidden_secret_value:department_id" in result.errors
    serialized = json.dumps(result.to_dict())
    assert secret_like_department not in serialized
    assert secret_like_department.lower() not in serialized
    assert result.work_context["normalized_context"] == {}


def test_lightweight_entry_submission_uses_employee_key_and_business_gateway() -> None:
    calls: list[tuple[str, str, dict[str, str], dict[str, object]]] = []

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        assert body is not None
        payload = json.loads(body.decode("utf-8"))
        calls.append((method, url, headers, payload))
        return HttpResponse(
            status_code=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps({"choices": [{"message": {"content": "draft"}}]}).encode("utf-8"),
        )

    result = submit_lightweight_entry(
        _valid_form(),
        base_url="http://localhost:4000",
        employee_key="employee-virtual-key",
        fetch=fetch,
    )

    assert result.status == "PASS"
    assert result.status_code == 200
    assert result.checked_endpoint == "/v1/chat/completions"
    assert result.assistant_text == "draft"
    assert len(calls) == 1
    method, url, headers, payload = calls[0]
    assert method == "POST"
    assert url == "http://localhost:4000/v1/chat/completions"
    assert headers["Authorization"] == f"Bearer {'employee-virtual-key'}"
    assert headers["Content-Type"] == "application/json"
    assert payload["metadata"]["entry_type"] == "lightweight_non_sdk"  # type: ignore[index]
    assert "ycapi" not in headers["Authorization"].lower()
    assert "employee-virtual-key" not in result.detail


def test_lightweight_entry_submission_blocks_without_employee_virtual_key() -> None:
    calls: list[str] = []

    result = submit_lightweight_entry(
        _valid_form(),
        base_url="http://localhost:4000",
        employee_key=" ",
        fetch=lambda method, url, headers, body: calls.append(url) or _json_chat_response(),
    )

    assert result.status == "BLOCKED"
    assert "AIMANAGER_EMPLOYEE_VIRTUAL_KEY" in result.detail
    assert calls == []


def test_lightweight_entry_submission_rejects_ycapi_token_env_name_without_fetch() -> None:
    calls: list[str] = []

    result = submit_lightweight_entry(
        _valid_form(),
        base_url="http://localhost:4000",
        employee_key="employee-virtual-key",
        employee_key_env_name="YCAPI_API_TOKEN",
        fetch=lambda method, url, headers, body: calls.append(url) or _json_chat_response(),
    )

    assert result.status == "FAIL"
    assert result.detail == "employee key env cannot be YCAPI_API_TOKEN; use a LiteLLM virtual key env"
    assert calls == []


def test_lightweight_entry_submission_fails_safely_on_non_200_response() -> None:
    fake_header_value = "Bearer " + "employee-virtual-key"

    result = submit_lightweight_entry(
        _valid_form(),
        base_url="http://localhost:4000",
        employee_key="employee-virtual-key",
        fetch=lambda method, url, headers, body: HttpResponse(
            status_code=500,
            headers={"Content-Type": "text/plain"},
            body=f"failed with {fake_header_value}".encode("utf-8"),
        ),
    )

    assert result.status == "FAIL"
    assert result.detail == "business gateway returned HTTP 500"
    assert result.status_code == 500
    assert "employee-virtual-key" not in json.dumps(result.to_dict())


def test_lightweight_entry_submission_fails_safely_on_non_openai_response_shape() -> None:
    result = submit_lightweight_entry(
        _valid_form(),
        base_url="http://localhost:4000",
        employee_key="employee-virtual-key",
        fetch=lambda method, url, headers, body: HttpResponse(
            status_code=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps({"ok": True}).encode("utf-8"),
        ),
    )

    assert result.status == "FAIL"
    assert result.detail == "business gateway response missing choices message content"
    assert result.status_code == 200
    assert "employee-virtual-key" not in json.dumps(result.to_dict())


def test_lightweight_entry_submission_fails_safely_on_transport_exception() -> None:
    fake_header_value = "Bearer " + "employee-virtual-key"

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        raise OSError(f"failed with {fake_header_value}")

    result = submit_lightweight_entry(
        _valid_form(),
        base_url="http://localhost:4000",
        employee_key="employee-virtual-key",
        fetch=fetch,
    )

    assert result.status == "FAIL"
    assert result.detail == "lightweight entry request failed before receiving an HTTP response"
    assert "employee-virtual-key" not in json.dumps(result.to_dict())


def test_lightweight_entry_cli_dry_run_writes_request_without_secret(tmp_path, capsys, monkeypatch) -> None:
    ycapi_token_value = "ycapi-" + "token-secret"
    monkeypatch.setenv("YCAPI_API_TOKEN", ycapi_token_value)
    form_file = tmp_path / "form.json"
    output_file = tmp_path / "result.json"
    form_file.write_text(json.dumps(_valid_form()), encoding="utf-8")

    exit_code = main(["--form-file", str(form_file), "--dry-run", "--output-json-file", str(output_file)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "PASS lightweight entry" in output
    assert ycapi_token_value not in output
    result = json.loads(output_file.read_text(encoding="utf-8"))
    assert result["status"] == "PASS"
    assert result["entry"]["request"]["body"]["metadata"]["entry_type"] == "lightweight_non_sdk"
    assert "Authorization" not in json.dumps(result)


def test_lightweight_entry_cli_rejects_employee_key_equal_to_ycapi_token(tmp_path, capsys, monkeypatch) -> None:
    ycapi_token_value = "ycapi-" + "token-secret"
    monkeypatch.setenv("AIMANAGER_EMPLOYEE_VIRTUAL_KEY", ycapi_token_value)
    monkeypatch.setenv("YCAPI_API_TOKEN", ycapi_token_value)
    form_file = tmp_path / "form.json"
    form_file.write_text(json.dumps(_valid_form()), encoding="utf-8")

    exit_code = main(["--form-file", str(form_file), "--base-url", "http://localhost:4000"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL lightweight entry" in output
    assert "employee key matches YCAPI_API_TOKEN" in output
    assert ycapi_token_value not in output


def _json_chat_response() -> HttpResponse:
    return HttpResponse(
        status_code=200,
        headers={"Content-Type": "application/json"},
        body=json.dumps({"choices": [{"message": {"content": "draft"}}]}).encode("utf-8"),
    )

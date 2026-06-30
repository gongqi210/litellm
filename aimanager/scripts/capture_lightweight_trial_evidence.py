from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import ValidationError

from aimanager.scripts.business_trial_acceptance_bundle import TrialEvidence


_EXIT_CODES = {"PASS": 0, "FAIL": 1, "BLOCKED": 2}
_FORBIDDEN_INPUT_KEYS = {
    "api_key",
    "api_token",
    "authorization",
    "bearer",
    "cookie",
    "employee_key",
    "headers",
    "litellm_master_key",
    "master_key",
    "token",
    "virtual_key",
    "ycapi_api_key",
    "ycapi_api_token",
    "ycapi_token",
}
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9._-]{3,}\b"),
    re.compile(r"https://qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=[A-Za-z0-9._~+/=-]+"),
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture safe AC-23 evidence from a successful lightweight-entry submission result."
    )
    parser.add_argument("--lightweight-entry-result-file", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--spend", required=True)
    parser.add_argument("--operator-role", required=True)
    parser.add_argument("--identity-source", required=True)
    parser.add_argument("--employee-virtual-key-alias", required=True)
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--completed-at", required=True)
    parser.add_argument("--observer", required=True)
    parser.add_argument("--captured-at", required=True)
    parser.add_argument("--entry-channel", default="lightweight_web")
    parser.add_argument("--live-ycapi-confirmed", action="store_true")
    parser.add_argument("--brand-safety-confirmed", action="store_true")
    parser.add_argument("--no-secret-echo-confirmed", action="store_true")
    parser.add_argument("--html-escaped-confirmed", action="store_true")
    parser.add_argument("--output-json-file", required=True)
    args = parser.parse_args(argv)

    submission = _load_submission_result(Path(args.lightweight_entry_result_file))
    if submission["status"] != "PASS":
        return _finish(submission, output_json_file=None)

    result = build_trial_evidence(
        submission_result=submission["submission_result"],
        request_id=args.request_id,
        spend=args.spend,
        operator_role=args.operator_role,
        identity_source=args.identity_source,
        employee_virtual_key_alias=args.employee_virtual_key_alias,
        started_at=args.started_at,
        completed_at=args.completed_at,
        observer=args.observer,
        captured_at=args.captured_at,
        entry_channel=args.entry_channel,
        live_ycapi_confirmed=args.live_ycapi_confirmed,
        brand_safety_confirmed=args.brand_safety_confirmed,
        no_secret_echo_confirmed=args.no_secret_echo_confirmed,
        html_escaped_confirmed=args.html_escaped_confirmed,
    )
    return _finish(result, output_json_file=args.output_json_file)


def build_trial_evidence(
    *,
    submission_result: Mapping[str, Any],
    request_id: str,
    spend: str,
    operator_role: str,
    identity_source: str,
    employee_virtual_key_alias: str,
    started_at: str,
    completed_at: str,
    observer: str,
    captured_at: str,
    live_ycapi_confirmed: bool,
    brand_safety_confirmed: bool,
    no_secret_echo_confirmed: bool,
    html_escaped_confirmed: bool,
    entry_channel: str = "lightweight_web",
) -> dict[str, Any]:
    if not live_ycapi_confirmed:
        return _blocked("live ycapi confirmation is required before AC-23 evidence can be captured")
    if _contains_secret_like_value(employee_virtual_key_alias):
        return _failed("employee virtual key alias must not look like a raw key")
    missing_flags = [
        name
        for name, enabled in (
            ("brand_safety_confirmed", brand_safety_confirmed),
            ("no_secret_echo_confirmed", no_secret_echo_confirmed),
            ("html_escaped_confirmed", html_escaped_confirmed),
        )
        if not enabled
    ]
    if missing_flags:
        return _blocked("missing confirmation flag(s): " + ", ".join(missing_flags))
    unsafe_paths = _unsafe_input_paths(submission_result)
    if unsafe_paths:
        return _failed(f"lightweight entry result contains forbidden secret-bearing field(s): {len(unsafe_paths)}")
    assistant_text = submission_result.get("assistant_text")
    if isinstance(assistant_text, str) and _contains_secret_like_value(assistant_text):
        return _failed("assistant response contains secret-like content; no_secret_echo cannot be confirmed")
    if submission_result.get("status") != "PASS":
        return _blocked("lightweight entry submission did not PASS")

    try:
        metadata = _metadata(submission_result)
        endpoint = _endpoint(submission_result)
        status_code = int(submission_result.get("status_code") or 0)
        model = _request_model(submission_result)
    except (TypeError, ValueError, KeyError):
        return _failed("lightweight entry result is missing required safe metadata")
    if not _has_valid_work_context_proof(submission_result, metadata):
        return _blocked("validated work context preflight PASS is required before AC-23 evidence can be captured")

    evidence = {
        "trial_id": f"trial-{metadata['work_item_id']}-{request_id}",
        "ac": "AC-23",
        "entry_channel": entry_channel,
        "operator": {
            "employee_id": metadata["employee_id"],
            "department_id": metadata["department_id"],
            "role": operator_role,
            "uses_sdk": False,
        },
        "identity_injection": {
            "source": identity_source,
            "employee_virtual_key_used": True,
            "employee_virtual_key_alias": employee_virtual_key_alias,
            "ycapi_token_exposed": False,
        },
        "work_context": {
            "scenario_l1": metadata["scenario_l1"],
            "scenario_l2": metadata["scenario_l2"],
            "project_id": metadata.get("project_id") or "",
            "customer_id": metadata.get("customer_id") or "",
            "cost_center_id": metadata["cost_center_id"],
        },
        "request_evidence": {
            "request_id": request_id,
            "model": model,
            "endpoint": endpoint,
            "http_status": status_code,
            "spend": spend,
            "currency": metadata["currency"],
            "pricing_version": metadata["pricing_version"],
        },
        "timing": {
            "started_at": started_at,
            "completed_at": completed_at,
        },
        "compliance": {
            "work_context_status": "PASS",
            "brand_safety_confirmed": brand_safety_confirmed,
            "no_secret_echo": no_secret_echo_confirmed,
            "html_escaped": html_escaped_confirmed,
        },
        "live_ycapi": True,
        "attestation": {
            "observer": observer,
            "captured_at": captured_at,
        },
    }
    if _contains_secret_like_value(json.dumps(evidence, ensure_ascii=False)):
        return _failed("captured evidence contains secret-like value")
    try:
        TrialEvidence.model_validate(evidence)
    except ValidationError as exc:
        return _failed("captured evidence is not AC-23 compatible: " + _validation_detail(exc))
    return {
        "status": "PASS",
        "detail": "safe AC-23 lightweight trial evidence captured",
        "evidence": evidence,
    }


def _load_submission_result(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _blocked(f"lightweight entry result file does not exist: {path}")
    except Exception as exc:
        return _failed(f"lightweight entry result could not be loaded: {type(exc).__name__}")
    if not isinstance(payload, dict):
        return _failed("lightweight entry result must contain a JSON object")
    return {
        "status": "PASS",
        "detail": "lightweight entry result loaded",
        "submission_result": payload,
    }


def _finish(result: Mapping[str, Any], *, output_json_file: str | None) -> int:
    status = str(result.get("status") or "FAIL")
    detail = str(result.get("detail") or "")
    if status == "PASS" and output_json_file:
        try:
            output_path = Path(output_json_file)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(result["evidence"], ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception as exc:
            print(f"FAIL lightweight trial evidence: output file not written: {type(exc).__name__}", file=sys.stderr)
            return 1
    print(f"{status} lightweight trial evidence: {detail}")
    return _EXIT_CODES.get(status, 1)


def _metadata(submission_result: Mapping[str, Any]) -> dict[str, Any]:
    entry = _mapping(submission_result.get("entry"))
    request = _mapping(entry.get("request"))
    body = _mapping(request.get("body"))
    metadata = _mapping(body.get("metadata"))
    required = (
        "work_item_id",
        "employee_id",
        "department_id",
        "scenario_l1",
        "scenario_l2",
        "cost_center_id",
        "currency",
        "pricing_version",
        "workflow_mode",
    )
    missing = [field for field in required if not _text(metadata.get(field))]
    if missing:
        raise KeyError(missing[0])
    return metadata


def _has_valid_work_context_proof(submission_result: Mapping[str, Any], metadata: Mapping[str, Any]) -> bool:
    try:
        entry = _mapping(submission_result.get("entry"))
        work_context = _mapping(entry.get("work_context"))
        normalized_context = _mapping(work_context.get("normalized_context"))
    except (TypeError, ValueError, KeyError):
        return False
    if _text(work_context.get("status")) != "PASS":
        return False
    if _text(metadata.get("workflow_mode")) != "preflight":
        return False
    if _text(normalized_context.get("workflow_mode")) != "preflight":
        return False
    matched_fields = ("work_item_id", "employee_id", "department_id", "scenario_l1", "scenario_l2")
    return all(_text(normalized_context.get(field)).casefold() == _text(metadata.get(field)).casefold() for field in matched_fields)


def _endpoint(submission_result: Mapping[str, Any]) -> str:
    checked_endpoint = _text(submission_result.get("checked_endpoint"))
    if checked_endpoint:
        return checked_endpoint
    entry = _mapping(submission_result.get("entry"))
    request = _mapping(entry.get("request"))
    endpoint = _text(request.get("path"))
    if not endpoint:
        raise KeyError("checked_endpoint")
    return endpoint


def _request_model(submission_result: Mapping[str, Any]) -> str:
    entry = _mapping(submission_result.get("entry"))
    request = _mapping(entry.get("request"))
    body = _mapping(request.get("body"))
    model = _text(body.get("model"))
    if not model:
        raise KeyError("model")
    return model


def _unsafe_input_paths(value: Any, *, path: str = "$") -> list[str]:
    violations: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            next_path = f"{path}.{key_text}" if path != "$" else key_text
            if key_text.lower() in _FORBIDDEN_INPUT_KEYS:
                violations.append(next_path)
                continue
            violations.extend(_unsafe_input_paths(item, path=next_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            violations.extend(_unsafe_input_paths(item, path=f"{path}[{index}]"))
    return violations


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("expected object")
    return dict(value)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _contains_secret_like_value(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS)


def _validation_detail(exc: ValidationError) -> str:
    first = exc.errors(include_input=False, include_url=False)[0]
    location = ".".join(str(part) for part in first.get("loc", ()) if part != "__root__")
    message = str(first.get("msg") or "invalid")
    return f"{location}: {message}" if location else message


def _blocked(detail: str) -> dict[str, Any]:
    return {"status": "BLOCKED", "detail": detail}


def _failed(detail: str) -> dict[str, Any]:
    return {"status": "FAIL", "detail": detail}


if __name__ == "__main__":
    raise SystemExit(main())

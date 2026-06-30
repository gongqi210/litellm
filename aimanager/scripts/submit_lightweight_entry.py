from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from aimanager.lightweight_entry import (
    DEFAULT_BASE_URL,
    DEFAULT_EMPLOYEE_KEY_ENV,
    YCAPI_TOKEN_ENV,
    LightweightEntryResult,
    LightweightEntrySubmissionResult,
    prepare_lightweight_entry,
    submit_lightweight_entry,
)
from aimanager.work_context import WorkContextMode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Submit an AiManager lightweight non-SDK entry form.")
    parser.add_argument("--form-file", required=True, help="JSON object from a lightweight form or WeCom adapter.")
    parser.add_argument("--base-url", default=os.environ.get("AIMANAGER_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--employee-key-env", default=DEFAULT_EMPLOYEE_KEY_ENV)
    parser.add_argument(
        "--mode",
        choices=("preflight", "closure"),
        default="preflight",
        help="Work-context validation mode.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Only validate and render the governed request body.")
    parser.add_argument("--output-json-file", help="Optional path to write the machine-readable result.")
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args(argv)

    payload_result = _load_form(Path(args.form_file))
    if isinstance(payload_result, LightweightEntrySubmissionResult):
        return _finish(payload_result, output_json_file=args.output_json_file)

    mode: WorkContextMode = args.mode
    if args.dry_run:
        entry = prepare_lightweight_entry(payload_result, mode=mode)
        return _finish(_submission_from_entry(entry), output_json_file=args.output_json_file, dry_run=True)

    key_guard = _employee_key_guard(args.employee_key_env)
    if key_guard is not None:
        return _finish(key_guard, output_json_file=args.output_json_file)

    result = submit_lightweight_entry(
        payload_result,
        base_url=args.base_url,
        employee_key=os.environ.get(args.employee_key_env, ""),
        employee_key_env_name=args.employee_key_env,
        mode=mode,
        timeout_seconds=args.timeout,
    )
    return _finish(result, output_json_file=args.output_json_file)


def _load_form(path: Path) -> dict[str, Any] | LightweightEntrySubmissionResult:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return LightweightEntrySubmissionResult(
            status="BLOCKED",
            detail=f"lightweight entry form does not exist: {path}",
            errors=["form_file"],
        )
    except Exception as exc:
        return LightweightEntrySubmissionResult(
            status="FAIL",
            detail=f"lightweight entry form could not be loaded: {type(exc).__name__}",
            errors=["form_file"],
        )
    if not isinstance(payload, dict):
        return LightweightEntrySubmissionResult(
            status="FAIL",
            detail="lightweight entry form must contain a JSON object",
            errors=["form_file"],
        )
    return payload


def _employee_key_guard(employee_key_env: str) -> LightweightEntrySubmissionResult | None:
    if employee_key_env == YCAPI_TOKEN_ENV:
        return LightweightEntrySubmissionResult(
            status="FAIL",
            detail=f"employee key env cannot be {YCAPI_TOKEN_ENV}; use a LiteLLM virtual key env",
        )
    employee_key = os.environ.get(employee_key_env, "").strip()
    ycapi_token = os.environ.get(YCAPI_TOKEN_ENV, "").strip()
    if employee_key and ycapi_token and employee_key == ycapi_token:
        return LightweightEntrySubmissionResult(
            status="FAIL",
            detail=f"employee key matches {YCAPI_TOKEN_ENV}; use a LiteLLM virtual key env",
        )
    return None


def _submission_from_entry(entry: LightweightEntryResult) -> LightweightEntrySubmissionResult:
    return LightweightEntrySubmissionResult(
        status=entry.status,
        detail=entry.detail,
        errors=entry.errors,
        warnings=entry.warnings,
        entry=entry,
        checked_endpoint="/v1/chat/completions" if entry.request is not None else None,
    )


def _finish(
    result: LightweightEntrySubmissionResult,
    *,
    output_json_file: str | None,
    dry_run: bool = False,
) -> int:
    write_error = ""
    if output_json_file:
        try:
            _write_result(Path(output_json_file), result)
        except Exception as exc:
            write_error = type(exc).__name__

    print(
        f"{result.status} lightweight entry: errors={len(result.errors)} "
        f"dry_run={str(dry_run).lower()} endpoint={result.checked_endpoint or '-'} {result.detail}"
    )
    if write_error:
        print(f"WARN lightweight entry result file not written: {write_error}", file=sys.stderr)
    if result.status == "FAIL":
        return 1
    if result.status == "BLOCKED":
        return 2
    if write_error:
        return 1
    return 0


def _write_result(path: Path, result: LightweightEntrySubmissionResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

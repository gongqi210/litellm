from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from aimanager.governance import REQUIRED_METADATA_FIELDS
from aimanager.redaction import (
    SECRET_LIKE_REDACTION,
    contains_secret_like,
    sanitize_value as sanitize_secret_value,
)
from aimanager.scripts.smoke_live_ycapi import HttpResponse

Status = Literal["PASS", "FAIL", "BLOCKED"]
Fetch = Callable[[str, str, dict[str, str], bytes | None], HttpResponse]

_EXIT_CODES = {"PASS": 0, "FAIL": 1, "BLOCKED": 2}
_PAGE_SIZE = 100
_MAX_PAGES = 1000
_EXPORT_SOURCE = "litellm-management-api:/key/list"
_EXPORT_SCOPE = "all_virtual_keys"
_SAFE_KEY_FIELDS = (
    "key_alias",
    "alias",
    "blocked",
    "revoked",
    "deleted",
    "user_id",
    "team_id",
    "models",
    "max_budget",
    "rpm_limit",
    "tpm_limit",
    "duration",
)
_SAFE_METADATA_FIELDS = tuple(REQUIRED_METADATA_FIELDS) + ("shared_key", "enforced_params")


def export_key_inventory(
    *,
    admin_base_url: str,
    admin_key: str,
    exported_by: str,
    output_file: Path | None,
    admin_key_env_name: str = "LITELLM_MASTER_KEY",
    generated_at: str | None = None,
    fetch: Fetch | None = None,
    timeout_seconds: float = 10,
) -> dict[str, object]:
    if not admin_key.strip():
        return _result(
            status="BLOCKED",
            detail=f"missing {admin_key_env_name}; cannot export LiteLLM key inventory",
            generated_at=generated_at,
            required_env=[admin_key_env_name],
        )
    if not admin_base_url.strip():
        return _result(
            status="BLOCKED",
            detail="missing AiManager management base URL; cannot call LiteLLM /key/list",
            generated_at=generated_at,
            required_env=["AIMANAGER_ADMIN_BASE_URL"],
        )
    if not exported_by.strip():
        return _result(
            status="BLOCKED",
            detail="missing exported_by; cannot produce trusted key inventory provenance",
            generated_at=generated_at,
        )
    if output_file is None:
        return _result(
            status="BLOCKED",
            detail="missing output inventory file; set --output-inventory-file or AIMANAGER_KEY_INVENTORY_FILE",
            generated_at=generated_at,
            required_env=["AIMANAGER_KEY_INVENTORY_FILE"],
        )

    fetcher = fetch or _fetch_with_urllib(timeout_seconds=timeout_seconds)
    raw_keys: list[Any] = []
    expected_total_key_count: int | None = None
    status_code: int | None = None
    page = 1
    while True:
        try:
            response = fetcher(
                "GET",
                _key_list_url(admin_base_url, page=page),
                {
                    "Authorization": f"Bearer {admin_key}",
                    "Accept": "application/json",
                    "x-request-id": "aimanager-key-inventory-export",
                },
                None,
            )
        except (OSError, ValueError, http.client.HTTPException) as exc:
            return _result(
                status="FAIL",
                detail=f"LiteLLM /key/list request failed: {type(exc).__name__}",
                generated_at=generated_at,
                exported_key_count=len(raw_keys),
                expected_total_key_count=expected_total_key_count or 0,
            )

        status_code = response.status_code
        if response.status_code != 200:
            return _result(
                status="FAIL",
                detail=f"LiteLLM /key/list returned HTTP {response.status_code}",
                generated_at=generated_at,
                status_code=response.status_code,
                exported_key_count=len(raw_keys),
                expected_total_key_count=expected_total_key_count or 0,
            )

        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _result(
                status="FAIL",
                detail="LiteLLM /key/list returned non-JSON response",
                generated_at=generated_at,
                status_code=response.status_code,
                exported_key_count=len(raw_keys),
                expected_total_key_count=expected_total_key_count or 0,
            )
        if not isinstance(payload, Mapping):
            return _result(
                status="FAIL",
                detail="LiteLLM /key/list response root must be a JSON object",
                generated_at=generated_at,
                status_code=response.status_code,
                exported_key_count=len(raw_keys),
                expected_total_key_count=expected_total_key_count or 0,
            )

        page_keys = _extract_keys(payload)
        if page_keys is None:
            return _result(
                status="FAIL",
                detail="LiteLLM /key/list response must contain keys or data list",
                generated_at=generated_at,
                status_code=response.status_code,
                exported_key_count=len(raw_keys),
                expected_total_key_count=expected_total_key_count or 0,
            )

        page_expected_total = _expected_total_key_count(payload)
        if page_expected_total is None:
            return _result(
                status="FAIL",
                detail="LiteLLM /key/list response did not include a trusted total_count/total/count",
                generated_at=generated_at,
                status_code=response.status_code,
                exported_key_count=len(raw_keys),
                expected_total_key_count=0,
                violations=[
                    {
                        "key_ref": "export",
                        "reason": "missing trusted pagination total",
                        "fields": ["total_count"],
                    }
                ],
            )
        if expected_total_key_count is None:
            expected_total_key_count = page_expected_total
        elif page_expected_total != expected_total_key_count:
            return _result(
                status="FAIL",
                detail="LiteLLM /key/list pagination total changed during export",
                generated_at=generated_at,
                status_code=response.status_code,
                exported_key_count=len(raw_keys),
                expected_total_key_count=expected_total_key_count,
                violations=[
                    {
                        "key_ref": "export",
                        "reason": "inconsistent pagination total",
                        "fields": ["expected_total_key_count", "total_count"],
                    }
                ],
            )

        current_page = _current_page(payload, requested_page=page)
        if current_page != page:
            return _result(
                status="FAIL",
                detail="LiteLLM /key/list pagination returned an unexpected current_page",
                generated_at=generated_at,
                status_code=response.status_code,
                exported_key_count=len(raw_keys),
                expected_total_key_count=expected_total_key_count,
                violations=[
                    {
                        "key_ref": "export",
                        "reason": "unexpected pagination page",
                        "fields": ["current_page"],
                    }
                ],
            )

        raw_keys.extend(page_keys)
        total_pages = _total_pages(payload)
        if not _has_more_pages(payload, current_page=current_page, total_pages=total_pages):
            break
        page += 1
        if page > _MAX_PAGES:
            return _result(
                status="FAIL",
                detail="LiteLLM /key/list pagination did not terminate before the maximum page limit",
                generated_at=generated_at,
                status_code=response.status_code,
                exported_key_count=len(raw_keys),
                expected_total_key_count=expected_total_key_count,
                violations=[
                    {
                        "key_ref": "export",
                        "reason": "non-terminating pagination",
                        "fields": ["has_more", "total_pages"],
                    }
                ],
            )

    expected_total_key_count = expected_total_key_count if expected_total_key_count is not None else len(raw_keys)
    if expected_total_key_count != len(raw_keys):
        return _result(
            status="FAIL",
            detail=(
                "LiteLLM /key/list response appears partial; rerun with a larger page size "
                "or add pagination before declaring all_virtual_keys"
            ),
            generated_at=generated_at,
            status_code=status_code,
            exported_key_count=len(raw_keys),
            expected_total_key_count=expected_total_key_count,
            violations=[
                {
                    "key_ref": "export",
                    "reason": "partial key inventory export",
                    "fields": ["expected_total_key_count", "keys"],
                }
            ],
        )

    inventory = {
        "exported_at": _timestamp(generated_at),
        "export_source": _EXPORT_SOURCE,
        "export_scope": _EXPORT_SCOPE,
        "exported_by": exported_by.strip(),
        "expected_total_key_count": expected_total_key_count,
        "keys": [_metadata_only_key(item) for item in raw_keys],
    }

    serialized_inventory = json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if contains_secret_like(serialized_inventory):
        return _result(
            status="FAIL",
            detail=f"metadata-only key inventory still contains {SECRET_LIKE_REDACTION}; fix source metadata before export",
            generated_at=generated_at,
            status_code=status_code,
            exported_key_count=len(raw_keys),
            expected_total_key_count=expected_total_key_count,
            violations=[
                {
                    "key_ref": "export",
                    "reason": "metadata-only inventory contains secret-like values",
                    "fields": [SECRET_LIKE_REDACTION],
                }
            ],
            raw_preview=sanitize_secret_value(serialized_inventory)[:240],
        )

    try:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(serialized_inventory, encoding="utf-8")
    except Exception as exc:
        return _result(
            status="FAIL",
            detail=f"key inventory export file not written: {type(exc).__name__}",
            generated_at=generated_at,
            status_code=status_code,
            exported_key_count=len(raw_keys),
            expected_total_key_count=expected_total_key_count,
        )

    return _result(
        status="PASS",
        detail="metadata-only LiteLLM virtual-key inventory exported",
        generated_at=generated_at,
        status_code=status_code,
        exported_key_count=len(raw_keys),
        expected_total_key_count=expected_total_key_count,
        output_file=str(output_file),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export metadata-only LiteLLM virtual-key inventory for AC-08.")
    parser.add_argument(
        "--admin-base-url",
        default=os.environ.get("AIMANAGER_ADMIN_BASE_URL", "http://127.0.0.1:4001"),
        help="AiManager management surface base URL.",
    )
    parser.add_argument(
        "--admin-key-env",
        default="LITELLM_MASTER_KEY",
        help="Environment variable containing the LiteLLM management key.",
    )
    parser.add_argument(
        "--exported-by",
        default=os.environ.get("AIMANAGER_KEY_INVENTORY_EXPORTED_BY", os.environ.get("USER", "")),
        help="Human or automation identity recorded in inventory provenance.",
    )
    parser.add_argument(
        "--output-inventory-file",
        default=os.environ.get("AIMANAGER_KEY_INVENTORY_FILE", ""),
        help="Destination metadata-only key inventory JSON file.",
    )
    parser.add_argument("--output-json-file", help="Optional destination JSON result.")
    parser.add_argument("--generated-at", help="Override generated_at/exported_at timestamp for deterministic tests.")
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args(argv)

    output_file = Path(args.output_inventory_file) if args.output_inventory_file else None
    result = export_key_inventory(
        admin_base_url=args.admin_base_url,
        admin_key=os.environ.get(args.admin_key_env, ""),
        admin_key_env_name=args.admin_key_env,
        exported_by=args.exported_by,
        output_file=output_file,
        generated_at=args.generated_at,
        timeout_seconds=args.timeout,
    )

    write_error = ""
    if args.output_json_file:
        try:
            result_file = Path(args.output_json_file)
            result_file.parent.mkdir(parents=True, exist_ok=True)
            result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception as exc:
            write_error = type(exc).__name__

    summary = result["summary"]
    print(
        f"{result['status']} key inventory export: "
        f"exported={summary['exported_key_count']} expected={summary['expected_total_key_count']} "
        f"{result['detail']}"
    )
    if write_error:
        print(f"WARN key inventory export result file not written: {write_error}", file=sys.stderr)
    status = str(result["status"])
    return _EXIT_CODES.get(status, 1)


def _result(
    *,
    status: Status,
    detail: str,
    generated_at: str | None,
    status_code: int | None = None,
    exported_key_count: int = 0,
    expected_total_key_count: int = 0,
    required_env: Sequence[str] | None = None,
    violations: Sequence[Mapping[str, object]] | None = None,
    raw_preview: str | None = None,
    output_file: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "status": status,
        "generated_at": _timestamp(generated_at),
        "detail": detail,
        "status_code": status_code,
        "summary": {
            "exported_key_count": exported_key_count,
            "expected_total_key_count": expected_total_key_count,
            "violation_count": len(violations or ()),
        },
        "violations": [dict(item) for item in violations or ()],
    }
    if required_env:
        result["required_env"] = list(required_env)
    if raw_preview:
        result["raw_preview"] = raw_preview
    if output_file:
        result["output_file"] = output_file
    return sanitize_secret_value(result)


def _fetch_with_urllib(*, timeout_seconds: float) -> Fetch:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return HttpResponse(status_code=response.status, headers=dict(response.headers.items()), body=response.read())
        except urllib.error.HTTPError as exc:
            return HttpResponse(status_code=exc.code, headers=dict(exc.headers.items()), body=exc.read())

    return fetch


def _key_list_url(base_url: str, *, page: int) -> str:
    query = urllib.parse.urlencode(
        {
            "page": page,
            "size": _PAGE_SIZE,
            "include_team_keys": "true",
            "return_full_object": "true",
        }
    )
    return f"{base_url.rstrip('/')}/key/list?{query}"


def _extract_keys(payload: Mapping[str, Any]) -> list[Any] | None:
    for field_name in ("keys", "data"):
        value = payload.get(field_name)
        if isinstance(value, list):
            return value
    return None


def _expected_total_key_count(payload: Mapping[str, Any]) -> int | None:
    for field_name in ("total_count", "total", "count"):
        value = payload.get(field_name)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            return value
    return None


def _current_page(payload: Mapping[str, Any], *, requested_page: int) -> int:
    for field_name in ("current_page", "page"):
        value = payload.get(field_name)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value > 0:
            return value
    return requested_page


def _total_pages(payload: Mapping[str, Any]) -> int | None:
    value = payload.get("total_pages")
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _has_more_pages(payload: Mapping[str, Any], *, current_page: int, total_pages: int | None) -> bool:
    if payload.get("has_more") is True:
        return True
    for field_name in ("next_page", "next_page_token", "nextPageToken"):
        if payload.get(field_name):
            return True
    return total_pages is not None and current_page < total_pages


def _metadata_only_key(item: Any) -> dict[str, object]:
    if not isinstance(item, Mapping):
        return {}
    exported: dict[str, object] = {}
    for field_name in _SAFE_KEY_FIELDS:
        if field_name in item:
            exported[field_name] = item[field_name]
    metadata = item.get("metadata")
    if isinstance(metadata, Mapping):
        exported["metadata"] = {
            field_name: metadata[field_name] for field_name in _SAFE_METADATA_FIELDS if field_name in metadata
        }
    return exported


def _timestamp(value: str | None) -> str:
    if value:
        return value
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())

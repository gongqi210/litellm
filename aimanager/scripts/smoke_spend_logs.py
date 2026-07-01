from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from aimanager.scripts.smoke_blocked_routes import HttpResponse


@dataclass(frozen=True)
class SpendLogRow:
    request_id: str
    call_type: str
    model: str
    spend: float


@dataclass(frozen=True)
class SpendLogSmokeResult:
    passed: bool
    detail: str
    chat_spend: float
    image_spend: float
    rows: tuple[SpendLogRow, ...]


Fetch = Callable[[str, str, dict[str, str], bytes | None], HttpResponse]
PollSpendLogs = Callable[[str], list[SpendLogRow]]
Sleep = Callable[[float], None]

_MARKER_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")
_ADMIN_RBAC_ROLE = "proxy_admin"


def build_governed_key_payload(
    *,
    request_marker: str,
    models: Sequence[str],
    shared_key: bool = True,
    max_budget: float = 1.0,
    key_alias_prefix: str = "aimanager-smoke",
    scenario_l2: str = "code_assist",
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "owner": "aimanager-smoke",
        "work_item_id": request_marker,
        "employee_id": "employee-smoke-001",
        "department_id": "dept_smoke",
        "project_id": "proj_aimanager_runtime_smoke",
        "cost_center_id": "cc_smoke",
        "scenario_l1": "engineering",
        "scenario_l2": scenario_l2,
        "approver": "aimanager-ci",
        "internal_or_external": "internal",
        "channel": "sdk",
        "sensitivity_level": "internal",
        "approval_required": False,
        "end_user_principal": "employee-smoke-001",
        "aimanager_smoke_id": request_marker,
    }
    if shared_key:
        metadata["shared_key"] = True
    return {
        "key_alias": f"{key_alias_prefix}-{request_marker}",
        "user_id": "aimanager-smoke-user",
        "team_id": "team_aimanager_smoke",
        "models": list(models),
        "max_budget": max_budget,
        "rpm_limit": 60,
        "tpm_limit": 120000,
        "duration": "1h",
        "metadata": metadata,
    }


def run_spend_log_smoke(
    *,
    business_base_url: str,
    admin_base_url: str,
    master_key: str,
    request_marker: str,
    fetch: Fetch | None = None,
    poll_spend_logs: PollSpendLogs | None = None,
    sleep: Sleep = time.sleep,
    poll_attempts: int = 30,
    poll_interval_seconds: float = 1.0,
    chat_model: str = "gemini-2.5-flash",
    image_model: str = "ycapi-image-1",
    timeout_seconds: float = 30,
    shared_key: bool = False,
) -> SpendLogSmokeResult:
    _validate_marker(request_marker)
    if poll_attempts <= 0:
        raise ValueError("poll_attempts must be positive")

    fetcher = fetch or _fetch_with_urllib(timeout_seconds=timeout_seconds)
    poller = poll_spend_logs or _default_psql_poller()
    virtual_key: str | None = None
    rows: list[SpendLogRow] = []

    try:
        key_payload = build_governed_key_payload(
            request_marker=request_marker,
            models=[chat_model, image_model],
            shared_key=shared_key,
        )
        key_response = _post_json(
            fetcher,
            _join_url(admin_base_url, "/key/generate"),
            _admin_auth_headers(master_key, request_id=f"key-{request_marker}"),
            key_payload,
        )
        virtual_key = _extract_virtual_key(key_response)

        chat_metadata = _request_metadata(request_marker, image_count=0)
        request_headers = _auth_headers(
            virtual_key,
            request_id=f"chat-{request_marker}",
            spend_logs_metadata=chat_metadata,
        )
        _post_json(
            fetcher,
            _join_url(business_base_url, "/v1/chat/completions"),
            request_headers,
            {
                "model": chat_model,
                "messages": [{"role": "user", "content": "AiManager spend smoke"}],
                "max_tokens": 8,
                "user": "employee-smoke-001",
                "metadata": chat_metadata,
            },
        )

        image_metadata = _request_metadata(request_marker, image_count=1)
        _post_json(
            fetcher,
            _join_url(business_base_url, "/v1/images/generations"),
            _auth_headers(
                virtual_key,
                request_id=f"image-{request_marker}",
                spend_logs_metadata=image_metadata,
            ),
            {
                "model": image_model,
                "prompt": "AiManager runtime spend smoke image",
                "n": 1,
                "size": "1024x1024",
                "user": "employee-smoke-001",
                "metadata": image_metadata,
            },
        )

        chat_spend = 0.0
        image_spend = 0.0
        for attempt in range(poll_attempts):
            rows = poller(request_marker)
            chat_spend = _matching_spend(rows, model=chat_model, image=False)
            image_spend = _matching_spend(rows, model=image_model, image=True)
            if chat_spend > 0 and image_spend > 0:
                return SpendLogSmokeResult(
                    passed=True,
                    detail="chat and image spend logs are nonzero",
                    chat_spend=chat_spend,
                    image_spend=image_spend,
                    rows=tuple(rows),
                )
            if attempt < poll_attempts - 1:
                sleep(poll_interval_seconds)

        missing = []
        if chat_spend <= 0:
            missing.append("chat spend log missing or zero")
        if image_spend <= 0:
            missing.append("image spend log missing or zero")
        return SpendLogSmokeResult(
            passed=False,
            detail="; ".join(missing),
            chat_spend=chat_spend,
            image_spend=image_spend,
            rows=tuple(rows),
        )
    except Exception as exc:
        return SpendLogSmokeResult(
            passed=False,
            detail=f"smoke failed: {type(exc).__name__}: {exc}",
            chat_spend=0.0,
            image_spend=0.0,
            rows=tuple(rows),
        )
    finally:
        if virtual_key:
            _delete_virtual_key(fetcher, admin_base_url, master_key, virtual_key, request_marker)


def parse_spend_log_rows(raw_json: str) -> list[SpendLogRow]:
    raw_json = raw_json.strip()
    if not raw_json:
        return []
    payload = json.loads(raw_json)
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise ValueError("spend log query must return a JSON list")

    rows: list[SpendLogRow] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        request_id = item.get("request_id")
        call_type = item.get("call_type")
        model = item.get("model")
        spend = item.get("spend")
        if not isinstance(request_id, str) or not isinstance(call_type, str) or not isinstance(model, str):
            continue
        try:
            spend_float = float(spend)
        except (TypeError, ValueError):
            continue
        rows.append(
            SpendLogRow(
                request_id=request_id,
                call_type=call_type,
                model=model,
                spend=spend_float,
            )
        )
    return rows


def poll_spend_logs_with_psql(
    marker: str,
    *,
    compose_file: str = "aimanager/docker-compose.yml",
    project_directory: str = ".",
    db_service: str = "db",
    db_user: str = "aimanager",
    db_name: str = "aimanager",
) -> list[SpendLogRow]:
    _validate_marker(marker)
    sql = _spend_log_sql(marker)
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            compose_file,
            "exec",
            "-T",
            db_service,
            "psql",
            "-U",
            db_user,
            "-d",
            db_name,
            "-t",
            "-A",
            "-c",
            sql,
        ],
        cwd=project_directory,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return parse_spend_log_rows(completed.stdout)


def _default_psql_poller() -> PollSpendLogs:
    def poll(marker: str) -> list[SpendLogRow]:
        return poll_spend_logs_with_psql(marker)

    return poll


def _post_json(
    fetch: Fetch,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> dict[str, Any]:
    response = fetch("POST", url, headers, json.dumps(payload).encode("utf-8"))
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"POST {url} returned HTTP {response.status_code}: {_body_excerpt(response.body)}")
    try:
        parsed = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"POST {url} did not return JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"POST {url} returned non-object JSON")
    return parsed


def _extract_virtual_key(payload: dict[str, Any]) -> str:
    value = payload.get("key") or payload.get("token")
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("key generation response did not include a virtual key")
    return value


def _delete_virtual_key(
    fetch: Fetch,
    admin_base_url: str,
    master_key: str,
    virtual_key: str,
    request_marker: str,
) -> None:
    headers = _admin_auth_headers(master_key, request_id=f"delete-key-{request_marker}")
    headers.update(
        {
            "litellm-changed-by": "aimanager-ci",
            "x-aimanager-actor": "aimanager-ci",
            "x-aimanager-reason": "AiManager smoke cleanup",
        }
    )
    try:
        _post_json(
            fetch,
            _join_url(admin_base_url, "/key/delete"),
            headers,
            {"keys": [virtual_key]},
        )
    except Exception:
        pass


def _request_metadata(
    marker: str, *, scenario_l2: str = "code_assist", image_count: int = 0
) -> dict[str, str | int]:
    return {
        "work_item_id": marker,
        "employee_id": "employee-smoke-001",
        "department_id": "dept_smoke",
        "project_id": "proj_aimanager_runtime_smoke",
        "cost_center_id": "cc_smoke",
        "pricing_version": "m1-runtime-smoke",
        "currency": "CNY",
        "scenario_l1": "engineering",
        "scenario_l2": scenario_l2,
        "internal_or_external": "internal",
        "channel": "sdk",
        "sensitivity_level": "internal",
        "approval_required": False,
        "end_user_principal": "employee-smoke-001",
        "aimanager_smoke_id": marker,
        "image_count": image_count,
    }


def _matching_spend(rows: Sequence[SpendLogRow], *, model: str, image: bool) -> float:
    total = 0.0
    for row in rows:
        if not _model_matches(row.model, model):
            continue
        is_image_row = "image" in row.call_type
        if image != is_image_row:
            continue
        total += row.spend
    return total


def _model_matches(spend_log_model: str, requested_model: str) -> bool:
    return spend_log_model == requested_model or spend_log_model.endswith(f"/{requested_model}")


def _auth_headers(
    token: str,
    *,
    request_id: str,
    spend_logs_metadata: dict[str, Any] | None = None,
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "x-request-id": request_id,
    }
    if spend_logs_metadata is not None:
        headers["x-litellm-spend-logs-metadata"] = json.dumps(spend_logs_metadata, separators=(",", ":"))
    return headers


def _admin_auth_headers(token: str, *, request_id: str) -> dict[str, str]:
    headers = _auth_headers(token, request_id=request_id)
    headers["x-aimanager-role"] = _ADMIN_RBAC_ROLE
    headers["x-aimanager-actor"] = "aimanager-ci"
    return headers


def _fetch_with_urllib(*, timeout_seconds: float) -> Fetch:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return HttpResponse(
                    status_code=response.status,
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                status_code=exc.code,
                headers=dict(exc.headers.items()),
                body=exc.read(),
            )

    return fetch


def _spend_log_sql(marker: str) -> str:
    marker_literal = _sql_literal(marker)
    chat_request_id = _sql_literal(f"chat-{marker}")
    image_request_id = _sql_literal(f"image-{marker}")
    return f"""
SELECT COALESCE(
  json_agg(
    json_build_object(
      'request_id', request_id,
      'call_type', call_type,
      'model', model,
      'spend', spend
    )
    ORDER BY "startTime" DESC
  ),
  '[]'::json
)
FROM "LiteLLM_SpendLogs"
WHERE metadata::text LIKE '%' || {marker_literal} || '%'
   OR request_id IN ({chat_request_id}, {image_request_id});
"""


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _validate_marker(marker: str) -> None:
    if not marker or not _MARKER_PATTERN.fullmatch(marker):
        raise ValueError("request_marker must contain only letters, digits, underscore, dot, colon, or hyphen")


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _body_excerpt(body: bytes, limit: int = 500) -> str:
    text = body.decode("utf-8", errors="replace")
    return text[:limit]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Smoke test AiManager chat/image calls and nonzero LiteLLM spend logs."
    )
    parser.add_argument(
        "--business-base-url",
        default=os.environ.get("AIMANAGER_BASE_URL", "http://localhost:4000"),
    )
    parser.add_argument(
        "--admin-base-url",
        default=os.environ.get("AIMANAGER_ADMIN_BASE_URL", "http://localhost:4001"),
    )
    parser.add_argument("--master-key", default=os.environ.get("LITELLM_MASTER_KEY", ""))
    parser.add_argument("--request-marker", default=f"aimanager-smoke-{uuid4().hex[:12]}")
    parser.add_argument("--poll-attempts", type=int, default=30)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--compose-file", default="aimanager/docker-compose.yml")
    parser.add_argument("--project-directory", default=".")
    parser.add_argument("--db-service", default="db")
    parser.add_argument("--db-user", default=os.environ.get("POSTGRES_USER", "aimanager"))
    parser.add_argument("--db-name", default=os.environ.get("POSTGRES_DB", "aimanager"))
    parser.add_argument(
        "--shared-key",
        action="store_true",
        help="Generate a shared key with enforced_params and exercise AiManager OSS runtime enforcement.",
    )
    args = parser.parse_args(argv)

    if not args.master_key.strip():
        print("FAIL missing LITELLM_MASTER_KEY or --master-key", file=sys.stderr)
        return 2

    def poller(marker: str) -> list[SpendLogRow]:
        return poll_spend_logs_with_psql(
            marker,
            compose_file=args.compose_file,
            project_directory=args.project_directory,
            db_service=args.db_service,
            db_user=args.db_user,
            db_name=args.db_name,
        )

    result = run_spend_log_smoke(
        business_base_url=args.business_base_url,
        admin_base_url=args.admin_base_url,
        master_key=args.master_key,
        request_marker=args.request_marker,
        poll_spend_logs=poller,
        poll_attempts=args.poll_attempts,
        poll_interval_seconds=args.poll_interval,
        timeout_seconds=args.timeout,
        shared_key=args.shared_key,
    )

    status = "PASS" if result.passed else "FAIL"
    print(f"{status}\t{result.detail}\tchat_spend={result.chat_spend}\timage_spend={result.image_spend}")
    for row in result.rows:
        print(f"ROW\t{row.request_id}\t{row.call_type}\t{row.model}\t{row.spend}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

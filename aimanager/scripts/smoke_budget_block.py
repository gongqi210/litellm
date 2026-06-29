from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from aimanager.scripts.smoke_blocked_routes import HttpResponse
from aimanager.scripts.smoke_spend_logs import (
    Fetch,
    Sleep,
    _admin_auth_headers,
    _auth_headers,
    _body_excerpt,
    _delete_virtual_key,
    _extract_virtual_key,
    _fetch_with_urllib,
    _join_url,
    _post_json,
    _request_metadata,
    _sql_literal,
    _validate_marker,
    build_governed_key_payload,
)


@dataclass(frozen=True)
class KeyBudgetRow:
    key_alias: str
    spend: float
    max_budget: float


@dataclass(frozen=True)
class BudgetBlockSmokeResult:
    passed: bool
    detail: str
    key_alias: str
    prime_status_code: int | None
    blocked_status_code: int | None
    budget_error_type: str
    budget_error_message: str
    key_spend: float
    max_budget: float
    rows: tuple[KeyBudgetRow, ...]


PollKeyBudget = Callable[[str], list[KeyBudgetRow]]

_KEY_ALIAS_PREFIX = "aimanager-budget-smoke"
_SCENARIO_L2 = "runtime-budget-block-smoke"


def run_budget_block_smoke(
    *,
    business_base_url: str,
    admin_base_url: str,
    master_key: str,
    request_marker: str,
    budget: float = 0.005,
    fetch: Fetch | None = None,
    poll_key_budget: PollKeyBudget | None = None,
    sleep: Sleep = time.sleep,
    poll_attempts: int = 30,
    poll_interval_seconds: float = 1.0,
    chat_model: str = "gemini-2.5-flash",
    image_model: str = "ycapi-image-1",
    timeout_seconds: float = 30,
) -> BudgetBlockSmokeResult:
    _validate_marker(request_marker)
    _validate_budget(budget)
    if poll_attempts <= 0:
        raise ValueError("poll_attempts must be positive")

    fetcher = fetch or _fetch_with_urllib(timeout_seconds=timeout_seconds)
    key_alias = f"{_KEY_ALIAS_PREFIX}-{request_marker}"
    virtual_key: str | None = None
    rows: list[KeyBudgetRow] = []
    key_spend = 0.0
    observed_max_budget = budget
    prime_status_code: int | None = None
    blocked_status_code: int | None = None
    budget_error_type = ""
    budget_error_message = ""
    poll_error = ""

    try:
        key_payload = build_governed_key_payload(
            request_marker=request_marker,
            models=[image_model, chat_model],
            shared_key=False,
            max_budget=budget,
            key_alias_prefix=_KEY_ALIAS_PREFIX,
            scenario_l2=_SCENARIO_L2,
        )
        key_response = _post_json(
            fetcher,
            _join_url(admin_base_url, "/key/generate"),
            _admin_auth_headers(master_key, request_id=f"budget-key-{request_marker}"),
            key_payload,
        )
        virtual_key = _extract_virtual_key(key_response)

        request_metadata = _request_metadata(request_marker, scenario_l2=_SCENARIO_L2)
        prime_response = fetcher(
            "POST",
            _join_url(business_base_url, "/v1/images/generations"),
            _auth_headers(
                virtual_key,
                request_id=f"budget-prime-{request_marker}",
                spend_logs_metadata=request_metadata,
            ),
            json.dumps(
                {
                    "model": image_model,
                    "prompt": "AiManager budget-block smoke image",
                    "n": 1,
                    "size": "1024x1024",
                    "user": "employee-smoke-001",
                    "metadata": request_metadata,
                }
            ).encode("utf-8"),
        )
        prime_status_code = prime_response.status_code
        if prime_status_code < 200 or prime_status_code >= 300:
            return _result(
                passed=False,
                detail=f"budget prime image request failed with HTTP {prime_status_code}: {_body_excerpt(prime_response.body)}",
                key_alias=key_alias,
                prime_status_code=prime_status_code,
                blocked_status_code=None,
                budget_error_type="",
                budget_error_message="",
                key_spend=key_spend,
                max_budget=observed_max_budget,
                rows=rows,
            )

        for attempt in range(poll_attempts):
            if poll_key_budget is not None:
                try:
                    rows = poll_key_budget(key_alias)
                    key_spend, observed_max_budget = _latest_key_budget(rows, fallback_budget=budget)
                    poll_error = ""
                except Exception as exc:
                    poll_error = f"key budget poll failed: {type(exc).__name__}: {exc}"

            block_response = fetcher(
                "POST",
                _join_url(business_base_url, "/v1/chat/completions"),
                _auth_headers(
                    virtual_key,
                    request_id=f"budget-block-{request_marker}-{attempt + 1}",
                    spend_logs_metadata=request_metadata,
                ),
                json.dumps(
                    {
                        "model": chat_model,
                        "messages": [{"role": "user", "content": "AiManager budget-block smoke"}],
                        "max_tokens": 8,
                        "user": "employee-smoke-001",
                        "metadata": request_metadata,
                    }
                ).encode("utf-8"),
            )
            blocked_status_code = block_response.status_code
            budget_error_type, budget_error_message = _budget_error(block_response)
            if _is_budget_block_response(block_response):
                return _result(
                    passed=True,
                    detail="request blocked by LiteLLM key budget",
                    key_alias=key_alias,
                    prime_status_code=prime_status_code,
                    blocked_status_code=blocked_status_code,
                    budget_error_type=budget_error_type,
                    budget_error_message=budget_error_message,
                    key_spend=key_spend,
                    max_budget=observed_max_budget,
                    rows=rows,
                )
            if attempt < poll_attempts - 1:
                sleep(poll_interval_seconds)

        detail = f"budget request was not blocked after {poll_attempts} attempt(s); last HTTP {blocked_status_code}"
        if poll_error:
            detail = f"{detail}; {poll_error}"
        return _result(
            passed=False,
            detail=detail,
            key_alias=key_alias,
            prime_status_code=prime_status_code,
            blocked_status_code=blocked_status_code,
            budget_error_type=budget_error_type,
            budget_error_message=budget_error_message,
            key_spend=key_spend,
            max_budget=observed_max_budget,
            rows=rows,
        )
    except Exception as exc:
        return _result(
            passed=False,
            detail=f"smoke failed: {type(exc).__name__}: {exc}",
            key_alias=key_alias,
            prime_status_code=prime_status_code,
            blocked_status_code=blocked_status_code,
            budget_error_type=budget_error_type,
            budget_error_message=budget_error_message,
            key_spend=key_spend,
            max_budget=observed_max_budget,
            rows=rows,
        )
    finally:
        if virtual_key:
            _delete_virtual_key(fetcher, admin_base_url, master_key, virtual_key, request_marker)


def parse_key_budget_rows(raw_json: str) -> list[KeyBudgetRow]:
    raw_json = raw_json.strip()
    if not raw_json:
        return []
    payload = json.loads(raw_json)
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise ValueError("key budget query must return a JSON list")

    rows: list[KeyBudgetRow] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        key_alias = item.get("key_alias")
        spend = item.get("spend")
        max_budget = item.get("max_budget")
        if not isinstance(key_alias, str):
            continue
        try:
            spend_float = float(spend)
            max_budget_float = float(max_budget)
        except (TypeError, ValueError):
            continue
        rows.append(
            KeyBudgetRow(
                key_alias=key_alias,
                spend=spend_float,
                max_budget=max_budget_float,
            )
        )
    return rows


def poll_key_budget_with_psql(
    key_alias: str,
    *,
    compose_file: str = "aimanager/docker-compose.yml",
    project_directory: str = ".",
    db_service: str = "db",
    db_user: str = "aimanager",
    db_name: str = "aimanager",
) -> list[KeyBudgetRow]:
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
            _key_budget_sql(key_alias),
        ],
        cwd=project_directory,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return parse_key_budget_rows(completed.stdout)


def _result(
    *,
    passed: bool,
    detail: str,
    key_alias: str,
    prime_status_code: int | None,
    blocked_status_code: int | None,
    budget_error_type: str,
    budget_error_message: str,
    key_spend: float,
    max_budget: float,
    rows: Sequence[KeyBudgetRow],
) -> BudgetBlockSmokeResult:
    return BudgetBlockSmokeResult(
        passed=passed,
        detail=detail,
        key_alias=key_alias,
        prime_status_code=prime_status_code,
        blocked_status_code=blocked_status_code,
        budget_error_type=budget_error_type,
        budget_error_message=budget_error_message,
        key_spend=key_spend,
        max_budget=max_budget,
        rows=tuple(rows),
    )


def _latest_key_budget(rows: Sequence[KeyBudgetRow], *, fallback_budget: float) -> tuple[float, float]:
    if not rows:
        return 0.0, fallback_budget
    latest = rows[0]
    return latest.spend, latest.max_budget


def _is_budget_block_response(response: HttpResponse) -> bool:
    error_type, message = _budget_error(response)
    if response.status_code != 429:
        return False
    normalized_type = error_type.lower()
    normalized_message = message.lower()
    return (
        normalized_type == "budget_exceeded"
        or "budget has been exceeded" in normalized_message
        or ("current cost" in normalized_message and "max budget" in normalized_message)
    )


def _budget_error(response: HttpResponse) -> tuple[str, str]:
    try:
        payload: Any = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "", ""
    if not isinstance(payload, dict):
        return "", ""
    error = payload.get("error")
    if not isinstance(error, dict):
        return "", ""
    error_type = error.get("type") or error.get("code")
    message = error.get("message")
    return (
        error_type.strip() if isinstance(error_type, str) else str(error_type or ""),
        message.strip() if isinstance(message, str) else str(message or ""),
    )


def _key_budget_sql(key_alias: str) -> str:
    key_alias_literal = _sql_literal(key_alias)
    return f"""
SELECT COALESCE(
  json_agg(
    json_build_object(
      'key_alias', key_alias,
      'spend', spend,
      'max_budget', max_budget
    )
    ORDER BY updated_at DESC
  ),
  '[]'::json
)
FROM "LiteLLM_VerificationToken"
WHERE key_alias = {key_alias_literal};
"""


def _validate_budget(budget: float) -> None:
    if not math.isfinite(budget) or budget <= 0:
        raise ValueError("budget must be a positive finite number")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test AiManager LiteLLM key max_budget blocking via mock ycapi.")
    parser.add_argument(
        "--business-base-url",
        default=os.environ.get("AIMANAGER_BASE_URL", "http://localhost:4000"),
    )
    parser.add_argument(
        "--admin-base-url",
        default=os.environ.get("AIMANAGER_ADMIN_BASE_URL", "http://localhost:4001"),
    )
    parser.add_argument("--master-key", default=os.environ.get("LITELLM_MASTER_KEY", ""))
    parser.add_argument("--request-marker", default=f"aimanager-budget-{uuid4().hex[:12]}")
    parser.add_argument("--budget", type=float, default=0.005)
    parser.add_argument("--poll-attempts", type=int, default=30)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--compose-file", default="aimanager/docker-compose.yml")
    parser.add_argument("--project-directory", default=".")
    parser.add_argument("--db-service", default="db")
    parser.add_argument("--db-user", default=os.environ.get("POSTGRES_USER", "aimanager"))
    parser.add_argument("--db-name", default=os.environ.get("POSTGRES_DB", "aimanager"))
    args = parser.parse_args(argv)

    if not args.master_key.strip():
        print("FAIL missing LITELLM_MASTER_KEY or --master-key", file=sys.stderr)
        return 2

    def poller(key_alias: str) -> list[KeyBudgetRow]:
        return poll_key_budget_with_psql(
            key_alias,
            compose_file=args.compose_file,
            project_directory=args.project_directory,
            db_service=args.db_service,
            db_user=args.db_user,
            db_name=args.db_name,
        )

    result = run_budget_block_smoke(
        business_base_url=args.business_base_url,
        admin_base_url=args.admin_base_url,
        master_key=args.master_key,
        request_marker=args.request_marker,
        budget=args.budget,
        poll_key_budget=poller,
        poll_attempts=args.poll_attempts,
        poll_interval_seconds=args.poll_interval,
        timeout_seconds=args.timeout,
    )

    status = "PASS" if result.passed else "FAIL"
    print(
        f"{status}\t{result.detail}"
        f"\tkey_alias={result.key_alias}"
        f"\tprime_status={result.prime_status_code}"
        f"\tblocked_status={result.blocked_status_code}"
        f"\tbudget_error_type={result.budget_error_type or 'missing'}"
        f"\tkey_spend={result.key_spend}"
        f"\tmax_budget={result.max_budget}"
    )
    if result.budget_error_message:
        print(f"REASON\t{result.budget_error_message}")
    for row in result.rows:
        print(f"ROW\t{row.key_alias}\tspend={row.spend}\tmax_budget={row.max_budget}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
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
    _validate_marker,
    build_governed_key_payload,
)


@dataclass(frozen=True)
class AuditLogEvent:
    event_type: str
    request_id: str
    reason: str


@dataclass(frozen=True)
class KeyLifecycleSmokeResult:
    passed: bool
    detail: str
    freeze_status_code: int | None
    freeze_reject_status_code: int | None
    revoke_status_code: int | None
    revoke_reject_status_code: int | None
    freeze_error_message: str
    revoke_error_message: str
    audit_event_types: tuple[str, ...]


PollAuditEvents = Callable[[str], list[AuditLogEvent]]

_SCENARIO_L2 = "runtime-key-lifecycle-smoke"
_FREEZE_ALIAS_PREFIX = "aimanager-lifecycle-freeze"
_REVOKE_ALIAS_PREFIX = "aimanager-lifecycle-revoke"
_ACTOR = "aimanager-ci"
_FREEZE_REASON = "AC-11 freeze lifecycle smoke"
_REVOKE_REASON = "AC-11 revoke lifecycle smoke"


def run_key_lifecycle_smoke(
    *,
    business_base_url: str,
    admin_base_url: str,
    master_key: str,
    request_marker: str,
    fetch: Fetch | None = None,
    poll_audit_events: PollAuditEvents | None = None,
    sleep: Sleep = time.sleep,
    poll_attempts: int = 30,
    poll_interval_seconds: float = 1.0,
    chat_model: str = "gemini-2.5-flash",
    timeout_seconds: float = 30,
) -> KeyLifecycleSmokeResult:
    _validate_marker(request_marker)
    if poll_attempts <= 0:
        raise ValueError("poll_attempts must be positive")

    fetcher = fetch or _fetch_with_urllib(timeout_seconds=timeout_seconds)
    audit_poller = poll_audit_events or _default_audit_poller()
    freeze_key: str | None = None
    revoke_key: str | None = None
    freeze_status_code: int | None = None
    freeze_reject_status_code: int | None = None
    revoke_status_code: int | None = None
    revoke_reject_status_code: int | None = None
    freeze_error_message = ""
    revoke_error_message = ""
    audit_events: list[AuditLogEvent] = []

    freeze_alias = f"{_FREEZE_ALIAS_PREFIX}-{request_marker}"
    revoke_alias = f"{_REVOKE_ALIAS_PREFIX}-{request_marker}"
    freeze_request_id = f"lifecycle-freeze-{request_marker}"
    revoke_request_id = f"lifecycle-revoke-{request_marker}"

    try:
        freeze_key = _create_lifecycle_key(
            fetcher,
            admin_base_url,
            master_key,
            request_marker=request_marker,
            key_alias_prefix=_FREEZE_ALIAS_PREFIX,
            request_id=f"lifecycle-key-freeze-{request_marker}",
            chat_model=chat_model,
        )
        _assert_chat_allowed(
            fetcher,
            business_base_url,
            freeze_key,
            request_marker=request_marker,
            request_id=f"lifecycle-freeze-baseline-{request_marker}",
            chat_model=chat_model,
        )

        freeze_response = fetcher(
            "POST",
            _join_url(admin_base_url, "/key/block"),
            _lifecycle_admin_headers(
                master_key,
                request_id=freeze_request_id,
                reason=_FREEZE_REASON,
                key_alias=freeze_alias,
            ),
            json.dumps({"key": freeze_key}).encode("utf-8"),
        )
        freeze_status_code = freeze_response.status_code
        if freeze_status_code < 200 or freeze_status_code >= 300:
            return _result(
                passed=False,
                detail=f"key freeze failed with HTTP {freeze_status_code}: {_body_excerpt(freeze_response.body)}",
                freeze_status_code=freeze_status_code,
                freeze_reject_status_code=freeze_reject_status_code,
                revoke_status_code=revoke_status_code,
                revoke_reject_status_code=revoke_reject_status_code,
                freeze_error_message=freeze_error_message,
                revoke_error_message=revoke_error_message,
                audit_events=audit_events,
            )

        freeze_rejected, freeze_reject_status_code, freeze_error_message = _poll_inference_rejection(
            fetcher,
            business_base_url,
            freeze_key,
            request_marker=request_marker,
            request_id_prefix="lifecycle-freeze-reject",
            chat_model=chat_model,
            expected_message_fragment="key is blocked",
            sleep=sleep,
            poll_attempts=poll_attempts,
            poll_interval_seconds=poll_interval_seconds,
        )
        if not freeze_rejected:
            return _result(
                passed=False,
                detail=f"frozen key was not rejected after {poll_attempts} attempt(s); last HTTP {freeze_reject_status_code}",
                freeze_status_code=freeze_status_code,
                freeze_reject_status_code=freeze_reject_status_code,
                revoke_status_code=revoke_status_code,
                revoke_reject_status_code=revoke_reject_status_code,
                freeze_error_message=freeze_error_message,
                revoke_error_message=revoke_error_message,
                audit_events=audit_events,
            )

        revoke_key = _create_lifecycle_key(
            fetcher,
            admin_base_url,
            master_key,
            request_marker=request_marker,
            key_alias_prefix=_REVOKE_ALIAS_PREFIX,
            request_id=f"lifecycle-key-revoke-{request_marker}",
            chat_model=chat_model,
        )
        _assert_chat_allowed(
            fetcher,
            business_base_url,
            revoke_key,
            request_marker=request_marker,
            request_id=f"lifecycle-revoke-baseline-{request_marker}",
            chat_model=chat_model,
        )

        revoke_response = fetcher(
            "POST",
            _join_url(admin_base_url, "/key/delete"),
            _lifecycle_admin_headers(
                master_key,
                request_id=revoke_request_id,
                reason=_REVOKE_REASON,
                key_alias=revoke_alias,
            ),
            json.dumps({"keys": [revoke_key]}).encode("utf-8"),
        )
        revoke_status_code = revoke_response.status_code
        if revoke_status_code < 200 or revoke_status_code >= 300:
            return _result(
                passed=False,
                detail=f"key revoke failed with HTTP {revoke_status_code}: {_body_excerpt(revoke_response.body)}",
                freeze_status_code=freeze_status_code,
                freeze_reject_status_code=freeze_reject_status_code,
                revoke_status_code=revoke_status_code,
                revoke_reject_status_code=revoke_reject_status_code,
                freeze_error_message=freeze_error_message,
                revoke_error_message=revoke_error_message,
                audit_events=audit_events,
            )
        revoke_key = None

        revoke_rejected, revoke_reject_status_code, revoke_error_message = _poll_inference_rejection(
            fetcher,
            business_base_url,
            "sk-revoked-placeholder" if revoke_key is None else revoke_key,
            request_marker=request_marker,
            request_id_prefix="lifecycle-revoke-reject",
            chat_model=chat_model,
            expected_message_fragment="",
            sleep=sleep,
            poll_attempts=poll_attempts,
            poll_interval_seconds=poll_interval_seconds,
            revoked_key=revoke_response,
        )
        if not revoke_rejected:
            return _result(
                passed=False,
                detail=f"revoked key was not rejected after {poll_attempts} attempt(s); last HTTP {revoke_reject_status_code}",
                freeze_status_code=freeze_status_code,
                freeze_reject_status_code=freeze_reject_status_code,
                revoke_status_code=revoke_status_code,
                revoke_reject_status_code=revoke_reject_status_code,
                freeze_error_message=freeze_error_message,
                revoke_error_message=revoke_error_message,
                audit_events=audit_events,
            )

        for attempt in range(poll_attempts):
            audit_events = audit_poller(request_marker)
            if _has_audit_event(audit_events, "key_frozen", freeze_request_id) and _has_audit_event(
                audit_events,
                "key_revoked",
                revoke_request_id,
            ):
                return _result(
                    passed=True,
                    detail="key freeze and revoke are enforced and audited",
                    freeze_status_code=freeze_status_code,
                    freeze_reject_status_code=freeze_reject_status_code,
                    revoke_status_code=revoke_status_code,
                    revoke_reject_status_code=revoke_reject_status_code,
                    freeze_error_message=freeze_error_message,
                    revoke_error_message=revoke_error_message,
                    audit_events=audit_events,
                )
            if attempt < poll_attempts - 1:
                sleep(poll_interval_seconds)

        return _result(
            passed=False,
            detail="missing key_frozen or key_revoked audit event",
            freeze_status_code=freeze_status_code,
            freeze_reject_status_code=freeze_reject_status_code,
            revoke_status_code=revoke_status_code,
            revoke_reject_status_code=revoke_reject_status_code,
            freeze_error_message=freeze_error_message,
            revoke_error_message=revoke_error_message,
            audit_events=audit_events,
        )
    except Exception as exc:
        return _result(
            passed=False,
            detail=f"smoke failed: {type(exc).__name__}: {exc}",
            freeze_status_code=freeze_status_code,
            freeze_reject_status_code=freeze_reject_status_code,
            revoke_status_code=revoke_status_code,
            revoke_reject_status_code=revoke_reject_status_code,
            freeze_error_message=freeze_error_message,
            revoke_error_message=revoke_error_message,
            audit_events=audit_events,
        )
    finally:
        if freeze_key:
            _delete_virtual_key(fetcher, admin_base_url, master_key, freeze_key, request_marker)
        if revoke_key:
            _delete_virtual_key(fetcher, admin_base_url, master_key, revoke_key, request_marker)


def parse_audit_events_from_logs(raw_logs: str) -> list[AuditLogEvent]:
    events: list[AuditLogEvent] = []
    marker = "aimanager_audit_event="
    for line in raw_logs.splitlines():
        if marker not in line:
            continue
        _, raw_event = line.split(marker, 1)
        try:
            payload: Any = json.loads(raw_event.strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        event_type = payload.get("event_type")
        request_id = payload.get("request_id")
        reason = payload.get("reason")
        if not isinstance(event_type, str) or not isinstance(request_id, str) or not isinstance(reason, str):
            continue
        events.append(AuditLogEvent(event_type=event_type, request_id=request_id, reason=reason))
    return events


def poll_audit_events_with_docker_logs(
    request_marker: str,
    *,
    compose_file: str = "aimanager/docker-compose.yml",
    project_directory: str = ".",
    admin_service: str = "aimanager-admin",
    tail: int = 400,
) -> list[AuditLogEvent]:
    _validate_marker(request_marker)
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            compose_file,
            "logs",
            "--no-color",
            "--tail",
            str(tail),
            admin_service,
        ],
        cwd=project_directory,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return [event for event in parse_audit_events_from_logs(completed.stdout) if request_marker in event.request_id]


def _create_lifecycle_key(
    fetcher: Fetch,
    admin_base_url: str,
    master_key: str,
    *,
    request_marker: str,
    key_alias_prefix: str,
    request_id: str,
    chat_model: str,
) -> str:
    key_response = _post_json(
        fetcher,
        _join_url(admin_base_url, "/key/generate"),
        _admin_auth_headers(master_key, request_id=request_id),
        build_governed_key_payload(
            request_marker=request_marker,
            models=[chat_model],
            shared_key=False,
            key_alias_prefix=key_alias_prefix,
            scenario_l2=_SCENARIO_L2,
        ),
    )
    return _extract_virtual_key(key_response)


def _assert_chat_allowed(
    fetcher: Fetch,
    business_base_url: str,
    virtual_key: str,
    *,
    request_marker: str,
    request_id: str,
    chat_model: str,
) -> None:
    _post_json(
        fetcher,
        _join_url(business_base_url, "/v1/chat/completions"),
        _auth_headers(
            virtual_key,
            request_id=request_id,
            spend_logs_metadata=_request_metadata(request_marker, scenario_l2=_SCENARIO_L2),
        ),
        {
            "model": chat_model,
            "messages": [{"role": "user", "content": "AiManager key lifecycle baseline"}],
            "max_tokens": 8,
            "user": "employee-smoke-001",
            "metadata": _request_metadata(request_marker, scenario_l2=_SCENARIO_L2),
        },
    )


def _poll_inference_rejection(
    fetcher: Fetch,
    business_base_url: str,
    virtual_key: str,
    *,
    request_marker: str,
    request_id_prefix: str,
    chat_model: str,
    expected_message_fragment: str,
    sleep: Sleep,
    poll_attempts: int,
    poll_interval_seconds: float,
    revoked_key: HttpResponse | None = None,
) -> tuple[bool, int | None, str]:
    status_code: int | None = None
    error_message = ""
    token = virtual_key
    if revoked_key is not None:
        token = _deleted_key_from_response(revoked_key) or virtual_key
    for attempt in range(poll_attempts):
        response = fetcher(
            "POST",
            _join_url(business_base_url, "/v1/chat/completions"),
            _auth_headers(
                token,
                request_id=f"{request_id_prefix}-{request_marker}-{attempt + 1}",
                spend_logs_metadata=_request_metadata(request_marker, scenario_l2=_SCENARIO_L2),
            ),
            json.dumps(
                {
                    "model": chat_model,
                    "messages": [{"role": "user", "content": "AiManager key lifecycle rejection"}],
                    "max_tokens": 8,
                    "user": "employee-smoke-001",
                    "metadata": _request_metadata(request_marker, scenario_l2=_SCENARIO_L2),
                }
            ).encode("utf-8"),
        )
        status_code = response.status_code
        error_message = _error_message(response)
        if status_code in {401, 403} and (
            not expected_message_fragment or expected_message_fragment in error_message.lower()
        ):
            return True, status_code, error_message
        if attempt < poll_attempts - 1:
            sleep(poll_interval_seconds)
    return False, status_code, error_message


def _deleted_key_from_response(response: HttpResponse) -> str:
    try:
        payload: Any = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    deleted_keys = payload.get("deleted_keys")
    if not isinstance(deleted_keys, list) or not deleted_keys:
        return ""
    first_key = deleted_keys[0]
    return first_key if isinstance(first_key, str) else ""


def _error_message(response: HttpResponse) -> str:
    try:
        payload: Any = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if not isinstance(error, dict):
        return ""
    message = error.get("message")
    return message.strip() if isinstance(message, str) else str(message or "")


def _lifecycle_admin_headers(
    master_key: str,
    *,
    request_id: str,
    reason: str,
    key_alias: str,
) -> dict[str, str]:
    headers = _admin_auth_headers(master_key, request_id=request_id)
    headers.update(
        {
            "litellm-changed-by": _ACTOR,
            "x-aimanager-actor": _ACTOR,
            "x-aimanager-reason": reason,
            "x-aimanager-key-alias": key_alias,
            "x-aimanager-team-id": "team_aimanager_smoke",
            "x-aimanager-department-id": "dept_smoke",
            "x-aimanager-project-id": "proj_aimanager_runtime_smoke",
            "x-aimanager-cost-center-id": "cc_smoke",
        }
    )
    return headers


def _has_audit_event(events: Sequence[AuditLogEvent], event_type: str, request_id: str) -> bool:
    return any(event.event_type == event_type and event.request_id == request_id for event in events)


def _result(
    *,
    passed: bool,
    detail: str,
    freeze_status_code: int | None,
    freeze_reject_status_code: int | None,
    revoke_status_code: int | None,
    revoke_reject_status_code: int | None,
    freeze_error_message: str,
    revoke_error_message: str,
    audit_events: Sequence[AuditLogEvent],
) -> KeyLifecycleSmokeResult:
    return KeyLifecycleSmokeResult(
        passed=passed,
        detail=detail,
        freeze_status_code=freeze_status_code,
        freeze_reject_status_code=freeze_reject_status_code,
        revoke_status_code=revoke_status_code,
        revoke_reject_status_code=revoke_reject_status_code,
        freeze_error_message=freeze_error_message,
        revoke_error_message=revoke_error_message,
        audit_event_types=tuple(event.event_type for event in audit_events),
    )


def _default_audit_poller() -> PollAuditEvents:
    def poll(request_marker: str) -> list[AuditLogEvent]:
        return poll_audit_events_with_docker_logs(request_marker)

    return poll


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Smoke test AiManager key freeze/revoke runtime enforcement and audit."
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
    parser.add_argument("--request-marker", default=f"aimanager-lifecycle-{uuid4().hex[:12]}")
    parser.add_argument("--poll-attempts", type=int, default=30)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--compose-file", default="aimanager/docker-compose.yml")
    parser.add_argument("--project-directory", default=".")
    parser.add_argument("--admin-service", default="aimanager-admin")
    args = parser.parse_args(argv)

    if not args.master_key.strip():
        print("FAIL missing LITELLM_MASTER_KEY or --master-key", file=sys.stderr)
        return 2

    def poller(request_marker: str) -> list[AuditLogEvent]:
        return poll_audit_events_with_docker_logs(
            request_marker,
            compose_file=args.compose_file,
            project_directory=args.project_directory,
            admin_service=args.admin_service,
        )

    result = run_key_lifecycle_smoke(
        business_base_url=args.business_base_url,
        admin_base_url=args.admin_base_url,
        master_key=args.master_key,
        request_marker=args.request_marker,
        poll_audit_events=poller,
        poll_attempts=args.poll_attempts,
        poll_interval_seconds=args.poll_interval,
        timeout_seconds=args.timeout,
    )

    status = "PASS" if result.passed else "FAIL"
    print(
        f"{status}\t{result.detail}"
        f"\tfreeze_status={result.freeze_status_code}"
        f"\tfreeze_reject_status={result.freeze_reject_status_code}"
        f"\trevoke_status={result.revoke_status_code}"
        f"\trevoke_reject_status={result.revoke_reject_status_code}"
        f"\taudit_events={','.join(result.audit_event_types) or 'missing'}"
    )
    if result.freeze_error_message:
        print(f"FREEZE_REJECT_REASON\t{result.freeze_error_message}")
    if result.revoke_error_message:
        print(f"REVOKE_REJECT_REASON\t{result.revoke_error_message}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

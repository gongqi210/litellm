from __future__ import annotations

import html
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol
from urllib.parse import parse_qs
from uuid import uuid4

from aimanager.lightweight_entry import (
    DEFAULT_BASE_URL,
    DEFAULT_CHAT_MODEL,
    DEFAULT_EMPLOYEE_KEY_ENV,
    MAX_TOKENS_DEFAULT,
    YCAPI_TOKEN_ENV,
    LightweightEntrySubmissionResult,
    prepare_lightweight_entry,
    submit_lightweight_entry,
)

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
EmployeeKeyProvider = Callable[[], tuple[str, str]]
TokenProvider = Callable[[], str]
LightweightWebStatus = Literal["PASS", "FAIL", "BLOCKED"]

INTERNAL_TRIAL_SCENARIOS = {
    "collaboration": ("summary", "translation", "search", "training_material"),
    "management": ("meeting_notes", "data_analysis", "presentation", "policy_draft"),
}
MAX_FORM_BODY_BYTES = 64 * 1024


class Submitter(Protocol):
    def __call__(
        self,
        *,
        form_payload: Mapping[str, Any],
        base_url: str,
        employee_key: str,
        employee_key_env_name: str,
    ) -> LightweightEntrySubmissionResult: ...


@dataclass(frozen=True)
class LightweightTrialDefaults:
    employee_id: str
    department_id: str
    end_user_principal: str
    project_id: str
    cost_center_id: str
    currency: str
    pricing_version: str
    customer_id: str = ""
    model: str = DEFAULT_CHAT_MODEL
    max_tokens: int = MAX_TOKENS_DEFAULT
    sensitivity_level: str = "internal"


@dataclass(frozen=True)
class LightweightTrialFormResult:
    status: LightweightWebStatus
    detail: str
    errors: tuple[str, ...] = ()
    form: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _RequestBodyResult:
    body: bytes
    too_large: bool = False


def build_trial_form(
    user_input: Mapping[str, Any],
    *,
    defaults: LightweightTrialDefaults,
    work_item_id: str | None = None,
) -> LightweightTrialFormResult:
    prompt = _text(user_input.get("prompt"))
    scenario_l1 = _text(user_input.get("scenario_l1")).lower()
    scenario_l2 = _text(user_input.get("scenario_l2")).lower()
    errors = tuple(
        error
        for error, failed in (
            ("prompt", not prompt),
            ("scenario", not _is_allowed_trial_scenario(scenario_l1, scenario_l2)),
            ("employee_id", not defaults.employee_id.strip()),
            ("department_id", not defaults.department_id.strip()),
            ("end_user_principal", not defaults.end_user_principal.strip()),
            ("project_id_or_customer_id", not defaults.project_id.strip() and not defaults.customer_id.strip()),
            ("cost_center_id", not defaults.cost_center_id.strip()),
            ("currency", not defaults.currency.strip()),
            ("pricing_version", not defaults.pricing_version.strip()),
        )
        if failed
    )
    if errors:
        return LightweightTrialFormResult(
            status="FAIL",
            detail=f"lightweight web trial form failed validation with {len(errors)} error(s)",
            errors=errors,
        )

    form = {
        "work_item_id": work_item_id or f"lightweight-{uuid4().hex}",
        "prompt": prompt,
        "model": defaults.model,
        "max_tokens": defaults.max_tokens,
        "employee_id": defaults.employee_id.strip(),
        "department_id": defaults.department_id.strip(),
        "end_user_principal": defaults.end_user_principal.strip(),
        "scenario_l1": scenario_l1,
        "scenario_l2": scenario_l2,
        "internal_or_external": "internal",
        "channel": "lightweight_web",
        "sensitivity_level": defaults.sensitivity_level.strip() or "internal",
        "approval_required": False,
        "cost_center_id": defaults.cost_center_id.strip(),
        "currency": defaults.currency.strip(),
        "pricing_version": defaults.pricing_version.strip(),
    }
    if defaults.project_id.strip():
        form["project_id"] = defaults.project_id.strip()
    if defaults.customer_id.strip():
        form["customer_id"] = defaults.customer_id.strip()
    return LightweightTrialFormResult(
        status="PASS",
        detail="lightweight web trial form is ready for governed submission",
        form=form,
    )


def create_app(
    *,
    defaults: LightweightTrialDefaults | None = None,
    employee_key_provider: EmployeeKeyProvider | None = None,
    ycapi_token_provider: TokenProvider | None = None,
    submitter: Submitter | None = None,
    business_base_url: str | None = None,
) -> ASGIApp:
    return _LightweightWebApp(
        defaults=defaults or load_trial_defaults_from_env(),
        employee_key_provider=employee_key_provider or _employee_key_from_env,
        ycapi_token_provider=ycapi_token_provider or _ycapi_token_from_env,
        submitter=submitter or submit_lightweight_entry,
        business_base_url=business_base_url or os.environ.get("AIMANAGER_BASE_URL", DEFAULT_BASE_URL),
    )


def load_trial_defaults_from_env() -> LightweightTrialDefaults:
    return LightweightTrialDefaults(
        employee_id=os.environ.get("AIMANAGER_LIGHTWEIGHT_EMPLOYEE_ID", "trial-employee"),
        department_id=os.environ.get("AIMANAGER_LIGHTWEIGHT_DEPARTMENT_ID", "trial-department"),
        end_user_principal=os.environ.get("AIMANAGER_LIGHTWEIGHT_END_USER_PRINCIPAL", "trial-user"),
        project_id=os.environ.get("AIMANAGER_LIGHTWEIGHT_PROJECT_ID", "trial-project"),
        customer_id=os.environ.get("AIMANAGER_LIGHTWEIGHT_CUSTOMER_ID", ""),
        cost_center_id=os.environ.get("AIMANAGER_LIGHTWEIGHT_COST_CENTER_ID", "trial-cost-center"),
        currency=os.environ.get("AIMANAGER_LIGHTWEIGHT_CURRENCY", "CNY"),
        pricing_version=os.environ.get("AIMANAGER_LIGHTWEIGHT_PRICING_VERSION", "local-trial"),
    )


class _LightweightWebApp:
    def __init__(
        self,
        *,
        defaults: LightweightTrialDefaults,
        employee_key_provider: EmployeeKeyProvider,
        ycapi_token_provider: TokenProvider,
        submitter: Submitter,
        business_base_url: str,
    ) -> None:
        self.defaults = defaults
        self.employee_key_provider = employee_key_provider
        self.ycapi_token_provider = ycapi_token_provider
        self.submitter = submitter
        self.business_base_url = business_base_url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await _send_text(send, "unsupported scope", status_code=404)
            return
        method = str(scope.get("method", "")).upper()
        path = _normalized_path(str(scope.get("path", "")))
        if method == "GET" and path == "/healthz":
            await _send_text(send, "ok")
            return
        if method == "GET" and path == "/":
            await _send_html(send, _render_form_page())
            return
        if method == "POST" and path == "/entry":
            await self._handle_entry(receive, send)
            return
        await _send_text(send, "not found", status_code=404)

    async def _handle_entry(self, receive: Receive, send: Send) -> None:
        body_result = await _read_request_body(receive)
        if body_result.too_large:
            await _send_html(
                send,
                _render_result_page(
                    "FAIL",
                    "lightweight web form body is too large",
                    errors=("request_body_too_large",),
                ),
                status_code=413,
            )
            return
        form_input = _parse_urlencoded_form(body_result.body)
        form_result = build_trial_form(form_input, defaults=self.defaults)
        if form_result.status != "PASS":
            await _send_html(send, _render_result_page(form_result.status, form_result.detail, errors=form_result.errors))
            return
        entry = prepare_lightweight_entry(form_result.form)
        if entry.status != "PASS":
            await _send_html(send, _render_result_page(entry.status, entry.detail, errors=tuple(entry.errors)))
            return
        key_env, employee_key = self.employee_key_provider()
        key_guard = _employee_key_guard(key_env, employee_key, self.ycapi_token_provider())
        if key_guard is not None:
            await _send_html(send, _render_result_page(key_guard.status, key_guard.detail, errors=tuple(key_guard.errors)))
            return
        result = self.submitter(
            form_payload=form_result.form,
            base_url=self.business_base_url,
            employee_key=employee_key,
            employee_key_env_name=key_env,
        )
        await _send_html(
            send,
            _render_result_page(
                result.status,
                result.detail,
                errors=tuple(result.errors),
                assistant_text=result.assistant_text,
            ),
        )


def _employee_key_guard(
    employee_key_env: str,
    employee_key: str,
    ycapi_token: str,
) -> LightweightEntrySubmissionResult | None:
    if employee_key_env == YCAPI_TOKEN_ENV:
        return LightweightEntrySubmissionResult(
            status="FAIL",
            detail=f"employee key env cannot be {YCAPI_TOKEN_ENV}; use a LiteLLM employee key env",
        )
    if not employee_key.strip():
        return LightweightEntrySubmissionResult(
            status="BLOCKED",
            detail=f"missing {employee_key_env}; cannot submit lightweight web trial through AiManager",
            errors=["employee_key"],
        )
    if employee_key.strip() and ycapi_token.strip() and employee_key.strip() == ycapi_token.strip():
        return LightweightEntrySubmissionResult(
            status="FAIL",
            detail=f"employee key matches {YCAPI_TOKEN_ENV}; use a LiteLLM employee key env",
        )
    return None


def _employee_key_from_env() -> tuple[str, str]:
    key_env = os.environ.get("AIMANAGER_LIGHTWEIGHT_EMPLOYEE_KEY_ENV", DEFAULT_EMPLOYEE_KEY_ENV).strip()
    normalized_key_env = key_env or DEFAULT_EMPLOYEE_KEY_ENV
    return normalized_key_env, os.environ.get(normalized_key_env, "")


def _ycapi_token_from_env() -> str:
    return os.environ.get(YCAPI_TOKEN_ENV, "")


def _render_form_page() -> str:
    options = "\n".join(
        f'<option value="{_escape(l1)}:{_escape(l2)}">{_escape(l1)} / {_escape(l2)}</option>'
        for l1, values in INTERNAL_TRIAL_SCENARIOS.items()
        for l2 in values
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AiManager Trial</title>
  <style>
    :root {{ color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; background: #f7f8fa; color: #1f2933; }}
    main {{ width: min(760px, calc(100vw - 32px)); margin: 40px auto; }}
    form {{ display: grid; gap: 14px; background: #fff; border: 1px solid #d8dee4; padding: 20px; }}
    label {{ display: grid; gap: 6px; font-size: 14px; font-weight: 600; }}
    textarea, select, input, button {{ font: inherit; border: 1px solid #b8c0cc; padding: 10px 12px; }}
    textarea {{ min-height: 132px; resize: vertical; }}
    button {{ width: max-content; background: #1f6feb; color: #fff; border-color: #1f6feb; cursor: pointer; }}
    .status {{ margin-bottom: 12px; font-size: 13px; color: #59636e; }}
  </style>
</head>
<body>
  <main data-verify-unit="aimanager-lightweight-web" data-verify-entry-endpoint="/entry">
    <div class="status">AiManager governed internal trial</div>
    <form method="post" action="/entry">
      <label>Scenario
        <select name="scenario">
          {options}
        </select>
      </label>
      <input type="hidden" name="scenario_l1" value="collaboration">
      <input type="hidden" name="scenario_l2" value="summary">
      <label>Prompt
        <textarea name="prompt" required></textarea>
      </label>
      <button type="submit">Submit</button>
    </form>
  </main>
  <script>
    const scenario = document.querySelector("select[name=scenario]");
    const l1 = document.querySelector("input[name=scenario_l1]");
    const l2 = document.querySelector("input[name=scenario_l2]");
    scenario.addEventListener("change", () => {{
      const parts = scenario.value.split(":");
      l1.value = parts[0] || "collaboration";
      l2.value = parts[1] || "summary";
    }});
  </script>
</body>
</html>"""


def _render_result_page(
    status: str,
    detail: str,
    *,
    errors: tuple[str, ...] = (),
    assistant_text: str | None = None,
) -> str:
    escaped_errors = "".join(f"<li>{_escape(error)}</li>" for error in errors)
    error_block = f"<ul>{escaped_errors}</ul>" if escaped_errors else ""
    answer_block = f"<section><h2>Answer</h2><pre>{_escape(assistant_text)}</pre></section>" if assistant_text else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AiManager Trial Result</title>
  <style>
    :root {{ color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; background: #f7f8fa; color: #1f2933; }}
    main {{ width: min(760px, calc(100vw - 32px)); margin: 40px auto; background: #fff; border: 1px solid #d8dee4; padding: 20px; }}
    .badge {{ display: inline-block; padding: 4px 8px; border: 1px solid #b8c0cc; font-weight: 700; }}
    pre {{ white-space: pre-wrap; overflow-wrap: anywhere; background: #f1f4f8; padding: 14px; }}
    a {{ color: #1f6feb; }}
  </style>
</head>
<body>
  <main data-verify-unit="aimanager-lightweight-web-result" data-status="{_escape(status)}">
    <div class="badge">{_escape(status)}</div>
    <p>{_escape(detail)}</p>
    {error_block}
    {answer_block}
    <a href="/">New request</a>
  </main>
</body>
</html>"""


async def _read_request_body(receive: Receive) -> _RequestBodyResult:
    chunks: list[bytes] = []
    total_size = 0
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            break
        if message.get("type") != "http.request":
            continue
        chunk = message.get("body", b"")
        body_chunk = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
        if not isinstance(body_chunk, bytes):
            break
        total_size += len(body_chunk)
        if total_size > MAX_FORM_BODY_BYTES:
            return _RequestBodyResult(body=b"", too_large=True)
        chunks.append(body_chunk)
        if not bool(message.get("more_body", False)):
            break
    return _RequestBodyResult(body=b"".join(chunks))


def _parse_urlencoded_form(raw_body: bytes) -> dict[str, str]:
    fields = parse_qs(raw_body.decode("utf-8", errors="replace"), keep_blank_values=True)
    parsed = {key: values[-1] if values else "" for key, values in fields.items()}
    scenario = parsed.get("scenario", "")
    if scenario and ":" in scenario:
        scenario_l1, scenario_l2 = scenario.split(":", 1)
        parsed["scenario_l1"] = scenario_l1
        parsed["scenario_l2"] = scenario_l2
    return parsed


async def _send_html(send: Send, body_text: str, *, status_code: int = 200) -> None:
    await _send_response(send, body_text, status_code=status_code, content_type="text/html; charset=utf-8")


async def _send_text(send: Send, body_text: str, *, status_code: int = 200) -> None:
    await _send_response(send, body_text, status_code=status_code, content_type="text/plain; charset=utf-8")


async def _send_response(
    send: Send,
    body_text: str,
    *,
    status_code: int,
    content_type: str,
) -> None:
    body = body_text.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", content_type.encode("ascii")),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _is_allowed_trial_scenario(scenario_l1: str, scenario_l2: str) -> bool:
    return scenario_l2 in INTERNAL_TRIAL_SCENARIOS.get(scenario_l1, ())


def _normalized_path(path: str) -> str:
    normalized = path.split("?", 1)[0].strip() or "/"
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if len(normalized) > 1:
        normalized = normalized.rstrip("/")
    return normalized


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)

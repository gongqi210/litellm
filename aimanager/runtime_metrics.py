from __future__ import annotations

from collections import Counter
from threading import Lock
from typing import Any


class AiManagerMetrics:
    def __init__(self) -> None:
        self._lock = Lock()
        self._audit_events: Counter[tuple[str, str]] = Counter()
        self._http_responses: Counter[tuple[str, str, str]] = Counter()

    def record_audit_event(self, event: dict[str, Any]) -> None:
        event_type = _label_value(event.get("event_type"), default="unknown")
        severity = _label_value(event.get("severity"), default="unknown")
        with self._lock:
            self._audit_events[(event_type, severity)] += 1

    def record_http_response(self, *, method: str, status_code: int) -> None:
        normalized_method = _label_value(method.upper(), default="UNKNOWN")
        normalized_status_code = str(status_code)
        status_class = _status_class(status_code)
        with self._lock:
            self._http_responses[(normalized_method, status_class, normalized_status_code)] += 1

    def render_prometheus_text(self) -> str:
        with self._lock:
            audit_events = dict(self._audit_events)
            http_responses = dict(self._http_responses)

        lines = [
            "# HELP aimanager_audit_events_total AiManager audit events emitted by runtime policy controls.",
            "# TYPE aimanager_audit_events_total counter",
        ]
        for (event_type, severity), count in sorted(audit_events.items()):
            lines.append(
                "aimanager_audit_events_total"
                f'{{event_type="{_escape_label(event_type)}",severity="{_escape_label(severity)}"}} {count}'
            )

        lines.extend(
            [
                "# HELP aimanager_http_responses_total AiManager HTTP responses by bounded status labels.",
                "# TYPE aimanager_http_responses_total counter",
            ]
        )
        for (method, status_class, status_code), count in sorted(http_responses.items()):
            lines.append(
                "aimanager_http_responses_total"
                f'{{method="{_escape_label(method)}",status_class="{_escape_label(status_class)}",'
                f'status_code="{_escape_label(status_code)}"}} {count}'
            )
        return "\n".join(lines) + "\n"


DEFAULT_AIMANAGER_METRICS = AiManagerMetrics()


def _status_class(status_code: int) -> str:
    if 100 <= status_code <= 599:
        return f"{status_code // 100}xx"
    return "unknown"


def _label_value(value: Any, *, default: str) -> str:
    if value is None:
        return default
    text = value.strip() if isinstance(value, str) else str(value)
    return text if text else default


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')

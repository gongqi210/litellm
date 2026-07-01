from __future__ import annotations

import re
from typing import Any, Mapping

SECRET_LIKE_REDACTION = "[redacted:secret-like-value]"
WECOM_WEBHOOK_REDACTION = "[redacted:AIMANAGER_WECOM_WEBHOOK_URL]"
WECOM_WEBHOOK_URL_REDACTION = (
    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
    f"{WECOM_WEBHOOK_REDACTION}"
)
SECRET_ENV_NAME_MARKERS = ("TOKEN", "KEY", "SECRET", "WEBHOOK", "PASSWORD")
SECRET_BEARING_KEY_NAMES = frozenset(
    {
        "apikey",
        "apitoken",
        "bearer",
        "employeekey",
        "litellmmasterkey",
        "masterkey",
        "password",
        "privatekey",
        "secret",
        "token",
        "virtualkey",
        "ycapiapikey",
        "ycapiapitoken",
        "ycapitoken",
    }
)
SECRET_CARRIER_KEY_NAMES = frozenset({"authorization", "cookie", "headers"})

_MIN_SECRET_ENV_VALUE_LENGTH = 8
_COMMON_NON_SECRET_VALUES = {
    "0",
    "1",
    "blocked",
    "disabled",
    "enabled",
    "false",
    "fail",
    "none",
    "no",
    "null",
    "off",
    "on",
    "pass",
    "skip",
    "true",
    "yes",
}

_WECOM_WEBHOOK_PATTERN = re.compile(
    r"https://qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key="
    r"[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+"
)
_BEARER_PATTERN = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9._~-]+")
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"\b((?:[A-Za-z0-9_-]*(?:TOKEN|API[_-]?KEY|SECRET|WEBHOOK|PASSWORD)[A-Za-z0-9_-]*)"
    r"\s*[:=]\s*)(?!os\.environ/)([^,\s\"'`]+)",
    re.IGNORECASE,
)
_DSN_PASSWORD_PATTERN = re.compile(
    r"((?:postgres(?:ql)?|mysql|mariadb|redis|mongodb(?:\+srv)?)://[^:\s/@]+:)[^@\s]+(@)",
    re.IGNORECASE,
)

_SECRET_PATTERNS = (
    (_WECOM_WEBHOOK_PATTERN, WECOM_WEBHOOK_URL_REDACTION),
    (_BEARER_PATTERN, f"Bearer {SECRET_LIKE_REDACTION}"),
    (_SECRET_KEY_PATTERN, SECRET_LIKE_REDACTION),
    (_DSN_PASSWORD_PATTERN, rf"\1{SECRET_LIKE_REDACTION}\2"),
)


def normalize_secret_field_name(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def build_secret_redactions(env: Mapping[str, str] | None) -> dict[str, str]:
    redactions: dict[str, str] = {}
    if not env:
        return redactions
    for name, raw_value in sorted(env.items()):
        if not any(marker in name.upper() for marker in SECRET_ENV_NAME_MARKERS):
            continue
        value = raw_value.strip() if isinstance(raw_value, str) else ""
        if _should_redact_env_value(value):
            redactions[value] = f"[redacted:{name}]"
    return redactions


def sanitize_value(value: Any, redactions: Mapping[str, str] | None = None) -> Any:
    if isinstance(value, str):
        return sanitize_text(value, redactions)
    if isinstance(value, list):
        return [sanitize_value(item, redactions) for item in value]
    if isinstance(value, tuple):
        return [sanitize_value(item, redactions) for item in value]
    if isinstance(value, dict):
        return {str(key): sanitize_value(item, redactions) for key, item in value.items()}
    return value


def sanitize_text(value: str, redactions: Mapping[str, str] | None = None) -> str:
    sanitized = value
    for secret, replacement in _sorted_redactions(redactions).items():
        sanitized = sanitized.replace(secret, replacement)
    for pattern, replacement in _SECRET_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    sanitized = _SECRET_ASSIGNMENT_PATTERN.sub(_sanitize_secret_assignment, sanitized)
    return sanitized


def contains_secret_like(value: str) -> bool:
    return any(pattern.search(value) for pattern, _replacement in _SECRET_PATTERNS) or any(
        _should_redact_assignment_value(match.group(2))
        for match in _SECRET_ASSIGNMENT_PATTERN.finditer(value)
    )


def _sorted_redactions(redactions: Mapping[str, str] | None) -> dict[str, str]:
    if not redactions:
        return {}
    return {
        secret: replacement
        for secret, replacement in sorted(redactions.items(), key=lambda item: len(item[0]), reverse=True)
        if secret
    }


def _should_redact_env_value(value: str) -> bool:
    if len(value) < _MIN_SECRET_ENV_VALUE_LENGTH:
        return False
    if value.lower() in _COMMON_NON_SECRET_VALUES:
        return False
    return True


def _sanitize_secret_assignment(match: re.Match[str]) -> str:
    value = match.group(2)
    if not _should_redact_assignment_value(value):
        return match.group(0)
    return f"{match.group(1)}{SECRET_LIKE_REDACTION}"


def _should_redact_assignment_value(value: str) -> bool:
    return value.strip().lower() not in _COMMON_NON_SECRET_VALUES

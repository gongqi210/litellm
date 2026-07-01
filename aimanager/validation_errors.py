from __future__ import annotations

from pydantic import ValidationError


def compact_validation_errors(exc: ValidationError, *, max_errors: int = 3) -> str:
    messages: list[str] = []
    for error in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in error.get("loc", ()) if str(part) and part != "__root__")
        message = str(error.get("msg") or "invalid value")
        messages.append(f"{location}: {message}" if location else message)
    if not messages:
        return "validation failed"
    if len(messages) <= max_errors:
        return "; ".join(messages)
    return "; ".join([*messages[:max_errors], f"... {len(messages) - max_errors} more"])

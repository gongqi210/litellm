from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from aimanager.governance import REQUIRED_METADATA_FIELDS, SHARED_KEY_ENFORCED_PARAMS
from aimanager.redaction import (
    SECRET_LIKE_REDACTION,
    contains_secret_like,
    sanitize_value as sanitize_secret_value,
)

Status = Literal["PASS", "FAIL", "BLOCKED"]
_EXIT_CODES = {"PASS": 0, "FAIL": 1, "BLOCKED": 2}
_REQUIRED_ACTIVE_KEY_FIELDS = (
    "user_id",
    "team_id",
    "models",
    "max_budget",
    "rpm_limit",
    "tpm_limit",
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate AiManager LiteLLM virtual key inventory governance.")
    parser.add_argument("--inventory-file", help="JSON object with keys[] exported from LiteLLM key inventory.")
    parser.add_argument("--output-json-file", help="Optional destination JSON result.")
    parser.add_argument("--generated-at", help="Override generated_at timestamp for deterministic tests.")
    args = parser.parse_args(argv)

    inventory_path = Path(args.inventory_file) if args.inventory_file else None
    result = collect_key_inventory_validation(inventory_file=inventory_path, generated_at=args.generated_at)
    if args.output_json_file:
        try:
            output_path = Path(args.output_json_file)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception as exc:
            print(f"FAIL key inventory validation: result file not written: {type(exc).__name__}", file=sys.stderr)
            return 1

    status = str(result["status"])
    summary = result["summary"]
    print(
        f"{status} key inventory validation: "
        f"active={summary['active_key_count']} violations={summary['violation_count']}"
    )
    return _EXIT_CODES.get(status, 1)


def collect_key_inventory_validation(
    *,
    inventory_file: Path | None = None,
    env: Mapping[str, str] | None = None,
    generated_at: str | None = None,
) -> dict[str, object]:
    current_env = os.environ if env is None else env
    selected_file = inventory_file or _env_path(current_env, "AIMANAGER_KEY_INVENTORY_FILE")
    if selected_file is None:
        return _result(
            status="BLOCKED",
            detail="missing AIMANAGER_KEY_INVENTORY_FILE; cannot verify governed production virtual key inventory",
            generated_at=generated_at,
            required_env=["AIMANAGER_KEY_INVENTORY_FILE"],
        )

    try:
        raw_text = selected_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _result(
            status="BLOCKED",
            detail=f"key inventory file does not exist: {selected_file}",
            generated_at=generated_at,
        )
    except Exception as exc:
        return _result(
            status="FAIL",
            detail=f"key inventory could not be loaded: {type(exc).__name__}",
            generated_at=generated_at,
        )

    sanitized_raw = sanitize_secret_value(raw_text)
    if contains_secret_like(raw_text):
        return _result(
            status="FAIL",
            detail=f"key inventory contains {SECRET_LIKE_REDACTION}; export metadata-only inventory without raw keys",
            generated_at=generated_at,
            violations=[
                {
                    "key_ref": "input",
                    "reason": "key inventory contains secret-like raw values",
                    "fields": [SECRET_LIKE_REDACTION],
                }
            ],
            raw_preview=str(sanitized_raw)[:240],
        )

    try:
        payload = json.loads(raw_text)
    except Exception as exc:
        return _result(
            status="FAIL",
            detail=f"key inventory could not be parsed: {type(exc).__name__}",
            generated_at=generated_at,
        )
    if not isinstance(payload, Mapping):
        return _result(status="FAIL", detail="key inventory root must be a JSON object", generated_at=generated_at)

    keys = _extract_keys(payload)
    if keys is None:
        return _result(status="FAIL", detail="key inventory must contain a keys or data list", generated_at=generated_at)

    violations: list[dict[str, object]] = []
    active_key_count = 0
    blocked_key_count = 0
    for index, key in enumerate(keys):
        if not isinstance(key, Mapping):
            violations.append({"key_ref": f"index:{index}", "reason": "key inventory item must be an object", "fields": []})
            continue
        if _is_blocked_key(key):
            blocked_key_count += 1
            continue
        active_key_count += 1
        violations.extend(_active_key_violations(key, index=index))

    if not violations and active_key_count == 0:
        return _result(
            status="BLOCKED",
            detail="key inventory contains no active virtual keys; cannot prove governed production usage",
            generated_at=generated_at,
            total_key_count=len(keys),
            active_key_count=active_key_count,
            blocked_key_count=blocked_key_count,
        )

    status: Status = "FAIL" if violations else "PASS"
    detail = (
        f"key inventory has {len(violations)} governance violation(s)"
        if violations
        else "all active production virtual keys are governed"
    )
    return _result(
        status=status,
        detail=detail,
        generated_at=generated_at,
        total_key_count=len(keys),
        active_key_count=active_key_count,
        blocked_key_count=blocked_key_count,
        violations=violations,
    )


def _active_key_violations(key: Mapping[str, Any], *, index: int) -> list[dict[str, object]]:
    violations: list[dict[str, object]] = []
    missing_fields: list[str] = []
    for field_name in _REQUIRED_ACTIVE_KEY_FIELDS:
        if field_name == "models":
            if not _explicit_models(key.get("models")):
                missing_fields.append(field_name)
        elif field_name in {"max_budget", "rpm_limit", "tpm_limit"}:
            if _positive_number(key.get(field_name)) <= 0:
                missing_fields.append(field_name)
        elif _is_missing(key.get(field_name)):
            missing_fields.append(field_name)

    metadata = key.get("metadata")
    if not isinstance(metadata, Mapping):
        missing_fields.append("metadata")
        metadata = {}
    for field_name in REQUIRED_METADATA_FIELDS:
        if _is_missing(metadata.get(field_name)):
            missing_fields.append(f"metadata.{field_name}")

    if missing_fields:
        violations.append(
            {
                "key_ref": _key_ref(key, index=index),
                "reason": "active key is missing required governance fields",
                "fields": missing_fields,
            }
        )

    if metadata.get("shared_key") is True:
        enforced_params = _string_list(metadata.get("enforced_params"))
        missing_enforced_params = [param for param in SHARED_KEY_ENFORCED_PARAMS if param not in enforced_params]
        if missing_enforced_params:
            violations.append(
                {
                    "key_ref": _key_ref(key, index=index),
                    "reason": "shared key is missing required enforced_params",
                    "fields": [f"metadata.enforced_params.{param}" for param in missing_enforced_params],
                }
            )
    return violations


def _result(
    *,
    status: Status,
    detail: str,
    generated_at: str | None,
    total_key_count: int = 0,
    active_key_count: int = 0,
    blocked_key_count: int = 0,
    violations: Sequence[Mapping[str, object]] | None = None,
    required_env: Sequence[str] | None = None,
    raw_preview: str | None = None,
) -> dict[str, object]:
    violation_list = [dict(item) for item in violations or ()]
    result: dict[str, object] = {
        "status": status,
        "generated_at": generated_at or datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "detail": detail,
        "summary": {
            "total_key_count": total_key_count,
            "active_key_count": active_key_count,
            "blocked_key_count": blocked_key_count,
            "violation_count": len(violation_list),
        },
        "violations": violation_list,
    }
    if required_env:
        result["required_env"] = list(required_env)
    if raw_preview:
        result["raw_preview"] = raw_preview
    return sanitize_secret_value(result)


def _extract_keys(payload: Mapping[str, Any]) -> list[Any] | None:
    for field_name in ("keys", "data"):
        value = payload.get(field_name)
        if isinstance(value, list):
            return value
    return None


def _env_path(env: Mapping[str, str], name: str) -> Path | None:
    value = env.get(name, "")
    value = value.strip() if isinstance(value, str) else ""
    return Path(value) if value else None


def _is_blocked_key(key: Mapping[str, Any]) -> bool:
    return key.get("blocked") is True or key.get("revoked") is True or key.get("deleted") is True


def _key_ref(key: Mapping[str, Any], *, index: int) -> str:
    for field_name in ("key_alias", "alias", "key_name"):
        value = key.get(field_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"index:{index}"


def _explicit_models(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    return all(isinstance(model, str) and model.strip() and model.strip() != "*" for model in value)


def _positive_number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else 0.0
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return 0.0
        return number if math.isfinite(number) else 0.0
    return 0.0


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, list) and not value:
        return True
    return False


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


if __name__ == "__main__":
    raise SystemExit(main())

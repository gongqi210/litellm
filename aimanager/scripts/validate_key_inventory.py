from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import UTC, datetime, timedelta
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
    "duration",
)
_REQUIRED_EXPORT_METADATA_FIELDS = (
    "exported_at",
    "export_source",
    "export_scope",
    "exported_by",
    "expected_total_key_count",
)
_EXPECTED_EXPORT_SCOPE = "all_virtual_keys"
_MAX_EXPORT_AGE = timedelta(hours=24)
_MAX_EXPORT_FUTURE_SKEW = timedelta(minutes=5)


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

    export_metadata_status, export_metadata_detail, export_metadata_violations = _validate_export_metadata(
        payload,
        exported_key_count=len(keys),
        generated_at=generated_at,
    )
    if export_metadata_status == "BLOCKED":
        return _result(
            status="BLOCKED",
            detail=export_metadata_detail,
            generated_at=generated_at,
            total_key_count=len(keys),
            required_fields=_REQUIRED_EXPORT_METADATA_FIELDS,
        )
    if export_metadata_status == "FAIL":
        return _result(
            status="FAIL",
            detail=export_metadata_detail,
            generated_at=generated_at,
            total_key_count=len(keys),
            violations=export_metadata_violations,
        )

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
        export_metadata=_export_metadata_evidence(payload),
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

    shared_key_value = metadata.get("shared_key")
    if shared_key_value is not None and not isinstance(shared_key_value, bool):
        violations.append(
            {
                "key_ref": _key_ref(key, index=index),
                "reason": "metadata.shared_key must be a boolean when present",
                "fields": ["metadata.shared_key"],
            }
        )
    if shared_key_value is True:
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
    required_fields: Sequence[str] | None = None,
    raw_preview: str | None = None,
    export_metadata: Mapping[str, object] | None = None,
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
    if required_fields:
        result["required_fields"] = list(required_fields)
    if raw_preview:
        result["raw_preview"] = raw_preview
    if export_metadata:
        result["export_metadata"] = dict(export_metadata)
    return sanitize_secret_value(result)


def _extract_keys(payload: Mapping[str, Any]) -> list[Any] | None:
    for field_name in ("keys", "data"):
        value = payload.get(field_name)
        if isinstance(value, list):
            return value
    return None


def _validate_export_metadata(
    payload: Mapping[str, Any],
    *,
    exported_key_count: int,
    generated_at: str | None,
) -> tuple[Status, str, list[dict[str, object]]]:
    missing_fields = [field_name for field_name in _REQUIRED_EXPORT_METADATA_FIELDS if _is_missing(payload.get(field_name))]
    if missing_fields:
        return (
            "BLOCKED",
            "key inventory is missing trusted export metadata; cannot prove this is a complete LiteLLM virtual-key export",
            [],
        )

    violations: list[dict[str, object]] = []
    exported_at = _parse_datetime(payload.get("exported_at"))
    if exported_at is None:
        violations.append(
            {
                "key_ref": "export",
                "reason": "exported_at must be an ISO-8601 timestamp",
                "fields": ["exported_at"],
            }
        )
    else:
        reference_time = _parse_datetime(generated_at) or datetime.now(UTC)
        if exported_at - reference_time > _MAX_EXPORT_FUTURE_SKEW:
            return (
                "BLOCKED",
                "key inventory export timestamp is in the future; rerun export with synchronized production clock",
                [],
            )
        if reference_time - exported_at > _MAX_EXPORT_AGE:
            return (
                "BLOCKED",
                "key inventory export is stale; rerun a fresh complete LiteLLM virtual-key export",
                [],
            )

    export_scope = str(payload.get("export_scope")).strip()
    if export_scope != _EXPECTED_EXPORT_SCOPE:
        violations.append(
            {
                "key_ref": "export",
                "reason": "export_scope must prove a full virtual-key inventory",
                "fields": ["export_scope"],
            }
        )

    expected_total = _non_negative_int(payload.get("expected_total_key_count"))
    if expected_total is None:
        violations.append(
            {
                "key_ref": "export",
                "reason": "expected_total_key_count must be a non-negative integer",
                "fields": ["expected_total_key_count"],
            }
        )
    elif expected_total != exported_key_count:
        violations.append(
            {
                "key_ref": "export",
                "reason": "declared export total does not match keys list",
                "fields": ["expected_total_key_count"],
            }
        )

    if violations:
        detail = (
            "expected_total_key_count does not match exported key count"
            if any("expected_total_key_count" in violation.get("fields", []) for violation in violations)
            else "trusted export metadata is invalid"
        )
        return ("FAIL", detail, violations)
    return ("PASS", "trusted export metadata is present", [])


def _export_metadata_evidence(payload: Mapping[str, Any]) -> dict[str, object]:
    return {
        "exported_at": str(payload.get("exported_at", "")).strip(),
        "export_source": str(payload.get("export_source", "")).strip(),
        "export_scope": str(payload.get("export_scope", "")).strip(),
        "exported_by": str(payload.get("exported_by", "")).strip(),
        "expected_total_key_count": payload.get("expected_total_key_count"),
    }


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from aimanager.employee_monitoring import validate_employee_monitoring_controls
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
_EMPLOYEE_ACKNOWLEDGMENT_ENV = (
    "AIMANAGER_EMPLOYEE_MONITORING_POLICY_FILE",
    "AIMANAGER_EMPLOYEE_ROSTER_FILE",
    "AIMANAGER_EMPLOYEE_ACKNOWLEDGMENT_FILE",
)
_REQUIRED_ACK_FLAGS = (
    "understood_purpose",
    "understood_scope",
    "understood_appeal",
    "understood_no_raw_content",
)


@dataclass(frozen=True)
class _EmployeeAcknowledgmentContext:
    policy_id: str
    policy_version: str
    active_employee_departments: Mapping[str, str]
    acknowledged_employee_ids: frozenset[str]
    missing_acknowledgment_count: int

    def evidence(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "active_employee_count": len(self.active_employee_departments),
            "acknowledged_employee_count": len(self.acknowledged_employee_ids),
            "missing_acknowledgment_count": self.missing_acknowledgment_count,
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate AiManager LiteLLM virtual key inventory governance.")
    parser.add_argument("--inventory-file", help="JSON object with keys[] exported from LiteLLM key inventory.")
    parser.add_argument("--employee-monitoring-policy-file", help="JSON employee monitoring policy for roster cross-check.")
    parser.add_argument("--employee-roster-file", help="CSV or JSON active employee roster for key ownership cross-check.")
    parser.add_argument("--acknowledgment-file", help="CSV or JSON employee notice acknowledgments for key ownership cross-check.")
    parser.add_argument(
        "--require-acknowledged-employees",
        action="store_true",
        help="Require every active key user_id to be in the active roster and have acknowledged the monitoring policy.",
    )
    parser.add_argument("--output-json-file", help="Optional destination JSON result.")
    parser.add_argument("--generated-at", help="Override generated_at timestamp for deterministic tests.")
    args = parser.parse_args(argv)

    inventory_path = Path(args.inventory_file) if args.inventory_file else None
    result = collect_key_inventory_validation(
        inventory_file=inventory_path,
        employee_monitoring_policy_file=Path(args.employee_monitoring_policy_file)
        if args.employee_monitoring_policy_file
        else None,
        employee_roster_file=Path(args.employee_roster_file) if args.employee_roster_file else None,
        acknowledgment_file=Path(args.acknowledgment_file) if args.acknowledgment_file else None,
        require_acknowledged_employees=args.require_acknowledged_employees,
        generated_at=args.generated_at,
    )
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
    employee_monitoring_policy_file: Path | None = None,
    employee_roster_file: Path | None = None,
    acknowledgment_file: Path | None = None,
    require_acknowledged_employees: bool = False,
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

    employee_context: _EmployeeAcknowledgmentContext | None = None
    employee_check = _employee_acknowledgment_context(
        policy_file=employee_monitoring_policy_file
        or _env_path(current_env, "AIMANAGER_EMPLOYEE_MONITORING_POLICY_FILE"),
        roster_file=employee_roster_file or _env_path(current_env, "AIMANAGER_EMPLOYEE_ROSTER_FILE"),
        acknowledgment_file=acknowledgment_file or _env_path(current_env, "AIMANAGER_EMPLOYEE_ACKNOWLEDGMENT_FILE"),
        require=require_acknowledged_employees,
    )
    if employee_check["status"] != "SKIP":
        if employee_check["status"] != "PASS":
            return _result(
                status=employee_check["status"],  # type: ignore[arg-type]
                detail=str(employee_check["detail"]),
                generated_at=generated_at,
                total_key_count=len(keys),
                violations=employee_check.get("violations"),  # type: ignore[arg-type]
                required_env=employee_check.get("required_env"),  # type: ignore[arg-type]
                employee_acknowledgment=employee_check.get("employee_acknowledgment"),  # type: ignore[arg-type]
            )
        context = employee_check.get("context")
        if isinstance(context, _EmployeeAcknowledgmentContext):
            employee_context = context

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
        violations.extend(_active_key_violations(key, index=index, employee_context=employee_context))

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
        employee_acknowledgment=employee_context.evidence() if employee_context is not None else None,
    )


def _active_key_violations(
    key: Mapping[str, Any],
    *,
    index: int,
    employee_context: _EmployeeAcknowledgmentContext | None = None,
) -> list[dict[str, object]]:
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
    if employee_context is not None:
        violations.extend(_employee_ownership_violations(key, metadata=metadata, index=index, context=employee_context))
    return violations


def _employee_ownership_violations(
    key: Mapping[str, Any],
    *,
    metadata: Mapping[str, Any],
    index: int,
    context: _EmployeeAcknowledgmentContext,
) -> list[dict[str, object]]:
    user_id = _text_value(key.get("user_id"))
    if not user_id:
        return []
    key_ref = _key_ref(key, index=index)
    expected_department = context.active_employee_departments.get(user_id)
    if expected_department is None:
        return [
            {
                "key_ref": key_ref,
                "reason": "active key user_id is not in active employee roster",
                "fields": ["user_id"],
            }
        ]

    violations: list[dict[str, object]] = []
    if user_id not in context.acknowledged_employee_ids:
        violations.append(
            {
                "key_ref": key_ref,
                "reason": "active key user_id has not acknowledged employee monitoring policy",
                "fields": ["user_id"],
            }
        )
    key_department = _text_value(metadata.get("department_id"))
    if key_department and key_department != expected_department:
        violations.append(
            {
                "key_ref": key_ref,
                "reason": "active key department_id does not match employee roster",
                "fields": ["metadata.department_id"],
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
    employee_acknowledgment: Mapping[str, object] | None = None,
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
    if employee_acknowledgment:
        result["employee_acknowledgment"] = dict(employee_acknowledgment)
    return sanitize_secret_value(result)


def _employee_acknowledgment_context(
    *,
    policy_file: Path | None,
    roster_file: Path | None,
    acknowledgment_file: Path | None,
    require: bool,
) -> dict[str, object]:
    selected_files = (policy_file, roster_file, acknowledgment_file)
    if not require and not any(selected_files):
        return {"status": "SKIP", "detail": "employee acknowledgment cross-check not requested"}
    missing_env = [
        env_name
        for env_name, selected_file in zip(_EMPLOYEE_ACKNOWLEDGMENT_ENV, selected_files, strict=True)
        if selected_file is None or not str(selected_file).strip()
    ]
    if missing_env:
        return {
            "status": "BLOCKED",
            "detail": "missing employee roster and acknowledgment evidence for key ownership cross-check",
            "required_env": list(_EMPLOYEE_ACKNOWLEDGMENT_ENV),
        }
    assert policy_file is not None
    assert roster_file is not None
    assert acknowledgment_file is not None

    try:
        policy = _load_json_object(policy_file)
        roster = _load_records(roster_file)
        acknowledgments = _load_records(acknowledgment_file)
    except FileNotFoundError as exc:
        return {
            "status": "BLOCKED",
            "detail": f"employee acknowledgment evidence file does not exist: {exc.filename}",
            "required_env": list(_EMPLOYEE_ACKNOWLEDGMENT_ENV),
        }
    except Exception as exc:
        return {
            "status": "FAIL",
            "detail": f"employee acknowledgment evidence could not be loaded: {type(exc).__name__}",
            "violations": [
                {
                    "key_ref": "employee_acknowledgment",
                    "reason": "employee acknowledgment evidence could not be loaded",
                    "fields": [type(exc).__name__],
                }
            ],
        }

    monitoring_result = validate_employee_monitoring_controls(
        policy=policy,
        employee_roster=roster,
        acknowledgments=acknowledgments,
    )
    monitoring_status = str(monitoring_result.get("status"))
    monitoring_summary = _mapping(monitoring_result.get("summary"))
    employee_acknowledgment = {
        "policy_id": str(monitoring_result.get("policy_id") or ""),
        "policy_version": str(monitoring_result.get("policy_version") or ""),
        "active_employee_count": monitoring_summary.get("active_employee_count", 0),
        "acknowledged_employee_count": monitoring_summary.get("acknowledged_employee_count", 0),
        "missing_acknowledgment_count": monitoring_summary.get("missing_acknowledgment_count", 0),
    }
    monitoring_errors = {str(error) for error in monitoring_result.get("errors") or []}
    if monitoring_status != "PASS" and (monitoring_errors - {"acknowledgments.latest_notice"}):
        return {
            "status": _coerce_status(monitoring_status),
            "detail": (
                "employee monitoring evidence must PASS before key inventory can prove employee ownership: "
                f"{monitoring_result.get('detail')}"
            ),
            "employee_acknowledgment": employee_acknowledgment,
            "violations": [
                {
                    "key_ref": "employee_acknowledgment",
                    "reason": "employee monitoring evidence did not pass",
                    "fields": list(monitoring_result.get("errors") or []),
                }
            ],
        }

    active_departments = _active_employee_departments(roster)
    acknowledged_ids = _acknowledged_employee_ids(
        acknowledgments=acknowledgments,
        policy_version=str(policy.get("version") or "").strip(),
        published_at=_parse_datetime(policy.get("published_at")),
    )
    context = _EmployeeAcknowledgmentContext(
        policy_id=str(monitoring_result.get("policy_id") or ""),
        policy_version=str(monitoring_result.get("policy_version") or ""),
        active_employee_departments=active_departments,
        acknowledged_employee_ids=frozenset(acknowledged_ids),
        missing_acknowledgment_count=int(employee_acknowledgment["missing_acknowledgment_count"]),
    )
    return {
        "status": "PASS",
        "detail": "employee acknowledgment evidence passed",
        "employee_acknowledgment": context.evidence(),
        "context": context,
    }


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _load_records(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"{path} must contain a JSON array")
        return [dict(item) for item in payload if isinstance(item, dict)]
    if suffix == ".csv":
        with path.open(encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    raise ValueError(f"unsupported file type for {path}; expected .json or .csv")


def _active_employee_departments(rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    departments: dict[str, str] = {}
    for row in rows:
        if _text_value(row.get("status") or "active").lower() != "active":
            continue
        employee_id = _text_value(row.get("employee_id"))
        department_id = _text_value(row.get("department_id"))
        if employee_id and department_id:
            departments[employee_id] = department_id
    return departments


def _acknowledged_employee_ids(
    *,
    acknowledgments: Sequence[Mapping[str, Any]],
    policy_version: str,
    published_at: datetime | None,
) -> set[str]:
    acknowledged: set[str] = set()
    for row in acknowledgments:
        employee_id = _text_value(row.get("employee_id"))
        if not employee_id or _text_value(row.get("notice_version")) != policy_version:
            continue
        acknowledged_at = _parse_datetime(row.get("acknowledged_at"))
        if published_at is not None and (acknowledged_at is None or _is_before(acknowledged_at, published_at)):
            continue
        if all(_truthy(row.get(field_name)) for field_name in _REQUIRED_ACK_FLAGS):
            acknowledged.add(employee_id)
    return acknowledged


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


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _coerce_status(value: Any) -> Status:
    return value if value in {"PASS", "FAIL", "BLOCKED"} else "FAIL"


def _text_value(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _is_before(left: datetime, right: datetime) -> bool:
    if (left.tzinfo is None) != (right.tzinfo is None):
        return left.replace(tzinfo=None) < right.replace(tzinfo=None)
    return left < right


def _truthy(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y", "acknowledged"}
    return False


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

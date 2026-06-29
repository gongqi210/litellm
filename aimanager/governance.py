from __future__ import annotations

import copy
import math
from typing import Any


REQUIRED_TOP_LEVEL_FIELDS = (
    "user_id",
    "team_id",
    "models",
    "max_budget",
    "rpm_limit",
    "tpm_limit",
    "duration",
)

REQUIRED_METADATA_FIELDS = (
    "owner",
    "department_id",
    "project_id",
    "cost_center_id",
    "scenario_l1",
    "scenario_l2",
    "approver",
    "internal_or_external",
)

SHARED_KEY_ENFORCED_PARAMS = [
    "user",
    "metadata.scenario_l1",
    "metadata.end_user_principal",
]


class KeyGovernanceError(ValueError):
    """Raised when an AiManager key request lacks required governance metadata."""


def normalize_key_request(payload: dict[str, Any], *, shared_key: bool = False) -> dict[str, Any]:
    normalized = copy.deepcopy(payload)
    _validate_required_top_level(normalized)
    metadata = _validate_metadata(normalized)
    _validate_models(normalized["models"])
    _validate_positive_number(normalized["max_budget"], "max_budget")
    _validate_positive_number(normalized["rpm_limit"], "rpm_limit")
    _validate_positive_number(normalized["tpm_limit"], "tpm_limit")

    if shared_key:
        metadata["enforced_params"] = list(SHARED_KEY_ENFORCED_PARAMS)

    normalized["metadata"] = metadata
    return normalized


def _validate_required_top_level(payload: dict[str, Any]) -> None:
    for field_name in REQUIRED_TOP_LEVEL_FIELDS:
        if _is_missing(payload.get(field_name)):
            raise KeyGovernanceError(f"{field_name} is required for AiManager key creation")


def _validate_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise KeyGovernanceError("metadata is required for AiManager key creation")
    for field_name in REQUIRED_METADATA_FIELDS:
        if _is_missing(metadata.get(field_name)):
            raise KeyGovernanceError(f"metadata.{field_name} is required for AiManager key creation")
    return dict(metadata)


def _validate_models(models: Any) -> None:
    if not isinstance(models, list) or not models:
        raise KeyGovernanceError("models must be a non-empty list")
    for model in models:
        if not isinstance(model, str) or not model.strip() or model == "*":
            raise KeyGovernanceError("models must list explicit AiManager model names")


def _validate_positive_number(value: Any, field_name: str) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise KeyGovernanceError(f"{field_name} must be a positive number")


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, list) and not value:
        return True
    return False

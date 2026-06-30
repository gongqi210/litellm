from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Mapping


WorkContextStatus = Literal["PASS", "FAIL", "BLOCKED"]
WorkContextMode = Literal["preflight", "closure"]

SCENARIO_TAGS: dict[str, tuple[str, ...]] = {
    "engineering": ("code_assist", "test_generation", "technical_docs", "incident_debugging"),
    "marketing": (
        "campaign_planning",
        "wechat_article",
        "sales_material",
        "customer_email",
        "industry_research",
        "competitor_analysis",
        "image_asset",
    ),
    "management": ("meeting_notes", "data_analysis", "presentation", "policy_draft"),
    "customer_support": ("faq", "ticket_summary", "customer_reply_draft"),
    "collaboration": ("translation", "summary", "search", "training_material"),
}

CONTENT_USE_VALUES = {"internal", "external"}
SENSITIVITY_LEVELS = {"public", "internal", "confidential", "restricted"}

REQUIRED_TOP_LEVEL_FIELDS = (
    "work_item_id",
    "employee_id",
    "department_id",
    "end_user_principal",
    "scenario_l1",
    "scenario_l2",
    "internal_or_external",
    "channel",
    "sensitivity_level",
    "approval_required",
)
REQUIRED_TOP_LEVEL_TEXT_FIELDS = tuple(
    field_name for field_name in REQUIRED_TOP_LEVEL_FIELDS if field_name != "approval_required"
)

PREFLIGHT_WORKFLOW_FIELDS = (
    "brief_ref",
    "human_reviewer",
    "approval_policy_ref",
)

CLOSURE_WORKFLOW_FIELDS = (
    "brief_ref",
    "draft_ref",
    "human_review_ref",
    "final_ref",
    "external_approval_ref",
    "archive_ref",
    "retrospective_ref",
)

REQUIRED_BRAND_SAFETY_CHECKS = (
    "brand_voice_checked",
    "forbidden_commitments_checked",
    "price_or_effect_claims_checked",
    "competitor_comparison_checked",
    "customer_case_checked",
    "copyright_checked",
    "portrait_rights_checked",
    "fact_check_required",
    "external_approval_required",
)


@dataclass(frozen=True)
class WorkContextValidationResult:
    status: WorkContextStatus
    detail: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    normalized_context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_work_context(payload: Mapping[str, Any], *, mode: WorkContextMode = "preflight") -> WorkContextValidationResult:
    if mode not in {"preflight", "closure"}:
        return WorkContextValidationResult(status="FAIL", detail=f"unsupported work context mode: {mode}", errors=["mode"])

    errors: list[str] = []
    normalized: dict[str, Any] = {"workflow_mode": mode}

    for field_name in REQUIRED_TOP_LEVEL_TEXT_FIELDS:
        value = payload.get(field_name)
        if not _is_non_empty_text(value):
            errors.append(field_name)
        else:
            normalized[field_name] = _normalize_text(value)

    project_id = payload.get("project_id")
    customer_id = payload.get("customer_id")
    has_project_id = _is_non_empty_text(project_id)
    has_customer_id = _is_non_empty_text(customer_id)
    if _is_missing(project_id) and _is_missing(customer_id):
        errors.append("project_id_or_customer_id")
    else:
        if not _is_missing(project_id) and not has_project_id:
            errors.append("project_id")
        if not _is_missing(customer_id) and not has_customer_id:
            errors.append("customer_id")
        if not has_project_id and not has_customer_id:
            errors.append("project_id_or_customer_id")
        if has_project_id:
            normalized["project_id"] = _normalize_text(project_id)
        if has_customer_id:
            normalized["customer_id"] = _normalize_text(customer_id)

    scenario_l1 = _normalize_text(payload.get("scenario_l1"))
    scenario_l2 = _normalize_text(payload.get("scenario_l2"))
    if scenario_l1:
        normalized["scenario_l1"] = scenario_l1
    if scenario_l2:
        normalized["scenario_l2"] = scenario_l2
    if scenario_l1 and scenario_l1 not in SCENARIO_TAGS:
        errors.append("scenario_l1")
    elif scenario_l1 and scenario_l2 and scenario_l2 not in SCENARIO_TAGS[scenario_l1]:
        errors.append("scenario_l2")

    content_use = _normalize_text(payload.get("internal_or_external"))
    if content_use and content_use not in CONTENT_USE_VALUES:
        errors.append("internal_or_external")
    if content_use:
        normalized["internal_or_external"] = content_use

    sensitivity_level = _normalize_text(payload.get("sensitivity_level"))
    if sensitivity_level and sensitivity_level not in SENSITIVITY_LEVELS:
        errors.append("sensitivity_level")
    if sensitivity_level:
        normalized["sensitivity_level"] = sensitivity_level

    approval_required = payload.get("approval_required")
    if not isinstance(approval_required, bool):
        errors.append("approval_required")
    elif content_use == "external" and not approval_required:
        errors.append("approval_required")
    elif isinstance(approval_required, bool):
        normalized["approval_required"] = approval_required

    requires_external_approval = scenario_l1 == "marketing" and content_use == "external"
    normalized["requires_external_approval"] = requires_external_approval
    if requires_external_approval:
        errors.extend(_validate_workflow(payload.get("workflow"), mode=mode))
        errors.extend(_validate_brand_safety(payload.get("brand_safety")))

    if errors:
        return WorkContextValidationResult(
            status="FAIL",
            detail=f"work context failed validation with {len(errors)} error(s)",
            errors=sorted(set(errors)),
            normalized_context=normalized,
        )

    return WorkContextValidationResult(
        status="PASS",
        detail="work context is valid for AiManager governed usage",
        normalized_context=normalized,
    )


def _validate_workflow(value: Any, *, mode: WorkContextMode) -> list[str]:
    if not isinstance(value, Mapping):
        return ["workflow"]
    required_fields = PREFLIGHT_WORKFLOW_FIELDS if mode == "preflight" else CLOSURE_WORKFLOW_FIELDS
    return [
        f"workflow.{field_name}"
        for field_name in required_fields
        if not _is_non_empty_text(value.get(field_name))
    ]


def _validate_brand_safety(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return ["brand_safety"]
    errors: list[str] = []
    if not _is_non_empty_text(value.get("policy_ref")):
        errors.append("brand_safety.policy_ref")
    for field_name in REQUIRED_BRAND_SAFETY_CHECKS:
        if value.get(field_name) is not True:
            errors.append(f"brand_safety.{field_name}")
    return errors


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _is_non_empty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _normalize_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower()

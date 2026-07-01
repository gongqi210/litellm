from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from aimanager.redaction import sanitize_text as sanitize_secret_text
from aimanager.redaction import sanitize_value as sanitize_secret_value

_EXIT_CODES = {"PASS": 0, "FAIL": 1, "BLOCKED": 2}
_STATUS_ORDER = {"FAIL": 0, "BLOCKED": 1, "PASS": 2}

_GAP_CATALOG: dict[str, dict[str, object]] = {
    "AC-08-KEY-INVENTORY": {
        "owner": "security/ops",
        "required_env": [
            "AIMANAGER_KEY_INVENTORY_FILE",
            "LITELLM_MASTER_KEY",
            "AIMANAGER_EMPLOYEE_MONITORING_POLICY_FILE",
            "AIMANAGER_EMPLOYEE_ROSTER_FILE",
            "AIMANAGER_EMPLOYEE_ACKNOWLEDGMENT_FILE",
        ],
        "command": "make key-inventory-readiness",
        "next_action": (
            "用 export_key_inventory 导出 24 小时内的生产 LiteLLM virtual key metadata-only 全量 inventory，并提供 exported_at、export_source、"
            "export_scope=all_virtual_keys、exported_by、expected_total_key_count；确认所有 active key 都有员工/团队、"
            "模型、预算、限流、duration 和治理 metadata；shared virtual key 必须带 enforced_params；"
            "同时用员工名册和最新监控制度确认导出交叉验证 key user_id 属于 active employee 且已完成确认。"
        ),
    },
    "AC-15": {
        "owner": "architecture/security/ops",
        "required_env": [
            "AIMANAGER_BUSINESS_BASE_URL",
            "AIMANAGER_PUBLIC_ADMIN_URL",
            "AIMANAGER_ALLOWED_SSO_REDIRECT_HOSTS",
        ],
        "command": "make admin-boundary-readiness",
        "next_action": (
            "补齐生产业务 URL、公开管理 URL；如果公开管理面通过 SSO 302/303 跳转保护，"
            "用 AIMANAGER_ALLOWED_SSO_REDIRECT_HOSTS 配置允许的 SSO host（逗号分隔）。"
            "先跑 smoke_admin_boundary 预览逐探测项，再由 production_readiness_bundle 生成统一 JSON 证据；"
            "验证业务 URL 不能伪造 role header 解锁管理面。"
        ),
    },
    "AC-16-WECOM": {
        "owner": "ops",
        "required_env": ["AIMANAGER_OBSERVABILITY_REPORT_FILE", "AIMANAGER_WECOM_WEBHOOK_URL"],
        "command": "make wecom-alert-readiness",
        "next_action": (
            "先用 route_observability_alerts dry-run 渲染企业微信 payload，再由 production_readiness_bundle 做唯一一次 live 投递；"
            "delivered_count=0 仍保持 BLOCKED。"
        ),
    },
    "AC-12-13-FINANCE": {
        "owner": "finance",
        "required_env": ["AIMANAGER_SPEND_FILE", "AIMANAGER_YCAPI_BILL_FILE"],
        "command": "make finance-readiness",
        "next_action": (
            "用 export_finance 从真实 LiteLLM spend 导出和 ycapi 月账单生成 usage/monthly/reconciliation CSV；"
            "两侧都必须有非零计费金额，空表头或占位行不能解除 BLOCKED。"
        ),
    },
    "AC-19": {
        "owner": "architecture/ops",
        "required_env": ["YCAPI_API_TOKEN"],
        "command": "make live-ycapi-roundtrip",
        "next_action": (
            "在生产侧注入真实 ycapi token，跑 /models + chat/image roundtrip；"
            "输出只保留状态码、模型数量、usage/image 计数和 request id，不得包含 token、请求 URL、prompt 或响应 body。"
        ),
    },
    "AC-20": {
        "owner": "product/engineering/security/ops",
        "required_env": ["AIMANAGER_BUSINESS_BASE_URL", "AIMANAGER_EMPLOYEE_VIRTUAL_KEY"],
        "command": "make work-context-enforcement-readiness",
        "next_action": (
            "在生产 business surface 用员工 LiteLLM virtual key 跑 work-context fail-closed smoke；"
            "chat/image 缺 metadata 必须返回 400 aimanager_work_context_invalid，且不能回显 prompt。"
        ),
    },
    "AC-POLICY": {
        "owner": "general_manager/finance/security/legal",
        "required_env": ["AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE"],
        "required_files": ["docs/aimanager/production_policy_attestation.example.json"],
        "command": "make production-policy-readiness",
        "next_action": "基于示例文件填写真实 showback/chargeback、价格审批、ycapi 限额、数据边界和 finance/security/legal 三方独立审批记录。",
    },
    "AC-23": {
        "owner": "business_owner/market/ops",
        "required_env": [
            "AIMANAGER_LIGHTWEIGHT_TRIAL_EVIDENCE_FILE",
            "AIMANAGER_LIGHTWEIGHT_ENTRY_RESULT_FILE",
            "AIMANAGER_LIGHTWEIGHT_TRIAL_REQUEST_ID",
            "AIMANAGER_LIGHTWEIGHT_TRIAL_SPEND",
            "AIMANAGER_LIGHTWEIGHT_TRIAL_OPERATOR_ROLE",
            "AIMANAGER_LIGHTWEIGHT_TRIAL_IDENTITY_SOURCE",
            "AIMANAGER_LIGHTWEIGHT_TRIAL_KEY_ALIAS",
            "AIMANAGER_LIGHTWEIGHT_TRIAL_STARTED_AT",
            "AIMANAGER_LIGHTWEIGHT_TRIAL_COMPLETED_AT",
            "AIMANAGER_LIGHTWEIGHT_TRIAL_OBSERVER",
            "AIMANAGER_LIGHTWEIGHT_TRIAL_CAPTURED_AT",
            "AIMANAGER_LIGHTWEIGHT_TRIAL_LIVE_YCAPI_CONFIRMED",
            "AIMANAGER_LIGHTWEIGHT_TRIAL_BRAND_SAFETY_CONFIRMED",
            "AIMANAGER_LIGHTWEIGHT_TRIAL_NO_SECRET_ECHO_CONFIRMED",
            "AIMANAGER_LIGHTWEIGHT_TRIAL_HTML_ESCAPED_CONFIRMED",
        ],
        "command": "make lightweight-trial-evidence-capture",
        "next_action": "安排市场或业务人员通过非 SDK 入口完成 0-300 秒真人试用，并从真实 request/spend log 采集 evidence。",
    },
    "AC-26": {
        "owner": "HR/legal/security",
        "required_env": [
            "AIMANAGER_EMPLOYEE_MONITORING_POLICY_FILE",
            "AIMANAGER_EMPLOYEE_ROSTER_FILE",
            "AIMANAGER_EMPLOYEE_ACKNOWLEDGMENT_FILE",
        ],
        "command": "make employee-monitoring-validate",
        "next_action": "发布 metadata-only 员工监控制度，补齐 HR/法务边界、员工名册和最新版本全员确认导出。",
    },
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate an actionable AiManager launch gap plan from gate bundle JSON files.")
    parser.add_argument("--production-readiness-file", help="JSON output from production_readiness_bundle.")
    parser.add_argument("--business-trial-file", help="JSON output from business_trial_acceptance_bundle.")
    parser.add_argument("--output-json-file", required=True, help="Destination launch gap plan JSON.")
    parser.add_argument("--output-markdown-file", help="Optional destination Markdown plan.")
    parser.add_argument("--generated-at", help="Override generated_at timestamp for deterministic tests.")
    args = parser.parse_args(argv)

    result = collect_launch_gap_plan(
        production_readiness_file=Path(args.production_readiness_file) if args.production_readiness_file else None,
        business_trial_file=Path(args.business_trial_file) if args.business_trial_file else None,
        generated_at=args.generated_at,
    )
    _write_json(Path(args.output_json_file), result)
    if args.output_markdown_file:
        _write_text(Path(args.output_markdown_file), str(result.get("markdown") or ""))
    status = str(result["status"])
    print(
        f"{status} launch gap plan: gaps={len(result['gaps'])} "
        f"fail={result['summary']['FAIL']} blocked={result['summary']['BLOCKED']}"
    )
    return _EXIT_CODES.get(status, 1)


def collect_launch_gap_plan(
    *,
    production_readiness_file: Path | None = None,
    business_trial_file: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, object]:
    collected: dict[str, dict[str, object]] = {}
    input_gaps: list[dict[str, object]] = []
    for source, path in (
        ("production_readiness", production_readiness_file),
        ("business_trial", business_trial_file),
    ):
        if path is None:
            continue
        if not path.exists():
            input_gaps.append(_input_gap(f"{source} bundle file does not exist: {path}", source=source))
            continue
        try:
            bundle = _load_json_object(path)
        except Exception as exc:
            input_gaps.append(_input_gap(f"{source} bundle could not be loaded: {type(exc).__name__}", source=source))
            continue
        _merge_checks(collected, source=source, checks=bundle.get("checks"))

    if not collected and not input_gaps:
        input_gaps.append(
            _input_gap(
                "missing production_readiness and business_trial bundle files; run the gate bundles before planning launch gaps",
                source="input",
            )
        )

    gaps = [*input_gaps, *(_gap_from_check(check) for check in collected.values() if check["status"] != "PASS")]
    gaps = sorted(gaps, key=lambda gap: (_STATUS_ORDER.get(str(gap["status"]), 3), str(gap["id"])))
    summary = _summary(collected.values(), input_gaps=input_gaps)
    status = _overall_status([str(gap["status"]) for gap in gaps])
    result = {
        "status": status,
        "generated_at": generated_at or datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "summary": summary,
        "gaps": gaps,
    }
    result["markdown"] = _markdown(result)
    return _sanitize_value(result)


def _merge_checks(collected: dict[str, dict[str, object]], *, source: str, checks: object) -> None:
    if not isinstance(checks, list):
        collected[f"{source}:MALFORMED"] = {
            "id": "MALFORMED",
            "name": "malformed_bundle",
            "status": "FAIL",
            "detail": f"{source} bundle checks must be a list",
            "evidence": {},
            "sources": [source],
        }
        return
    for item in checks:
        if not isinstance(item, Mapping):
            continue
        check_id = str(item.get("id") or "UNKNOWN")
        status = _coerce_status(item.get("status"))
        current = collected.get(check_id)
        current_sources = list(current.get("sources", [])) if current else []
        current_evidence = _mapping(current.get("evidence")) if current else {}
        item_evidence = _mapping(item.get("evidence"))
        if current is None or _STATUS_ORDER[status] < _STATUS_ORDER[str(current["status"])]:
            collected[check_id] = {
                "id": check_id,
                "name": str(item.get("name") or "launch_gate_check"),
                "status": status,
                "detail": str(item.get("detail") or ""),
                "evidence": item_evidence or current_evidence,
                "sources": sorted({source, *current_sources}),
            }
        else:
            current["sources"] = sorted({source, *current.get("sources", [])})
            if not current.get("evidence") and item_evidence:
                current["evidence"] = item_evidence


def _gap_from_check(check: Mapping[str, object]) -> dict[str, object]:
    check_id = str(check["id"])
    catalog = _GAP_CATALOG.get(check_id, {})
    evidence = _mapping(check.get("evidence"))
    required_env = _ordered_unique(
        [
            *_string_list(evidence.get("required_env")),
            *_string_list(catalog.get("required_env")),
        ]
    )
    required_files = _string_list(evidence.get("required_files")) or list(catalog.get("required_files", []))
    return {
        "id": check_id,
        "name": str(check.get("name") or "launch_gate_check"),
        "status": _coerce_status(check.get("status")),
        "owner": str(catalog.get("owner") or "project_owner"),
        "detail": str(check.get("detail") or ""),
        "sources": list(check.get("sources", [])),
        "required_env": required_env,
        "required_files": required_files,
        "command": str(catalog.get("command") or "rerun the corresponding AiManager gate command and inspect this check"),
        "next_action": str(catalog.get("next_action") or "inspect the failed or blocked check and provide the missing evidence"),
    }


def _input_gap(detail: str, *, source: str) -> dict[str, object]:
    return {
        "id": "INPUT",
        "name": "launch_gap_plan_input",
        "status": "BLOCKED",
        "owner": "ops",
        "detail": detail,
        "sources": [source],
        "required_env": [],
        "required_files": ["production_readiness", "business_trial"],
        "command": (
            "PYTHONPATH=\"$PWD\" uv run --no-project --with pyyaml python -m "
            "aimanager.scripts.production_readiness_bundle --output-json-file /tmp/aimanager-production-readiness.json"
        ),
        "next_action": "先运行 production_readiness_bundle；业务试点前再运行 business_trial_acceptance_bundle。",
    }


def _summary(checks: Sequence[Mapping[str, object]] | Any, *, input_gaps: Sequence[Mapping[str, object]]) -> dict[str, int]:
    summary = {status: 0 for status in ("PASS", "FAIL", "BLOCKED")}
    for check in checks:
        status = _coerce_status(check.get("status"))
        summary[status] += 1
    for gap in input_gaps:
        summary[_coerce_status(gap.get("status"))] += 1
    return summary


def _overall_status(statuses: Sequence[str]) -> str:
    if any(status == "FAIL" for status in statuses):
        return "FAIL"
    if any(status == "BLOCKED" for status in statuses):
        return "BLOCKED"
    return "PASS"


def _markdown(result: Mapping[str, object]) -> str:
    lines = [
        "# AiManager Launch Gap Plan",
        "",
        f"- Status: {result['status']}",
        f"- Generated At: {result['generated_at']}",
        f"- PASS/FAIL/BLOCKED: {result['summary']['PASS']}/{result['summary']['FAIL']}/{result['summary']['BLOCKED']}",
        "",
    ]
    gaps = result.get("gaps", [])
    if not gaps:
        lines.append("No launch gaps remain in the supplied bundle files.")
        return "\n".join(lines) + "\n"
    for gap in gaps:
        lines.extend(
            [
                f"## {gap['status']} {gap['id']} - {gap['name']}",
                "",
                f"- Owner: {gap['owner']}",
                f"- Sources: {', '.join(gap['sources'])}",
                f"- Detail: {gap['detail']}",
                f"- Required Env: {', '.join(gap['required_env']) or 'none'}",
                f"- Required Files: {', '.join(gap['required_files']) or 'none'}",
                f"- Command: `{gap['command']}`",
                f"- Next Action: {gap['next_action']}",
                "",
            ]
        )
    return "\n".join(lines)


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _coerce_status(status: object) -> str:
    return str(status) if status in {"PASS", "FAIL", "BLOCKED"} else "FAIL"


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _ordered_unique(values: Sequence[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _sanitize_value(value: object) -> object:
    return sanitize_secret_value(value)


def _sanitize_text(value: str) -> str:
    return sanitize_secret_text(value)


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

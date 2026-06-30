from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Sequence

_EXIT_CODES = {"PASS": 0, "FAIL": 1, "BLOCKED": 2}
_STATUS_ORDER = {"FAIL": 0, "BLOCKED": 1, "PASS": 2}
_SOURCE_LABELS = {
    "business_trial": "business_trial_acceptance",
    "launch_gap_plan": "launch_gap_plan",
    "acceptance_coverage": "acceptance_coverage",
}
_STAGE_SCORE_LABELS = {
    "business_trial": "business_trial_acceptance",
    "launch_gap_plan": "launch_gap_closure",
    "acceptance_coverage": "acceptance_coverage",
}
_SECRET_PATTERNS = (
    (
        re.compile(r"https://qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+"),
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=[redacted:AIMANAGER_WECOM_WEBHOOK_URL]",
    ),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [redacted:secret-like-value]"),
    (re.compile(r"\bsk-[A-Za-z0-9._~-]+"), "[redacted:secret-like-value]"),
    (
        re.compile(r"(postgres(?:ql)?://[^:\s/@]+:)[^@\s]+(@)", re.IGNORECASE),
        r"\1[redacted:secret-like-value]\2",
    ),
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the final AiManager acceptance report from machine-readable gate outputs."
    )
    parser.add_argument("--business-trial-file", help="JSON output from business_trial_acceptance_bundle.")
    parser.add_argument("--launch-gap-plan-file", help="JSON output from generate_launch_gap_plan.")
    parser.add_argument("--acceptance-coverage-file", help="JSON output from acceptance_coverage_matrix.")
    parser.add_argument("--output-json-file", required=True)
    parser.add_argument("--output-markdown-file")
    parser.add_argument("--generated-at", help="Override generated_at timestamp for deterministic tests.")
    args = parser.parse_args(argv)

    result = collect_final_acceptance_report(
        business_trial_file=Path(args.business_trial_file) if args.business_trial_file else None,
        launch_gap_plan_file=Path(args.launch_gap_plan_file) if args.launch_gap_plan_file else None,
        acceptance_coverage_file=Path(args.acceptance_coverage_file) if args.acceptance_coverage_file else None,
        generated_at=args.generated_at,
    )
    _write_json(Path(args.output_json_file), result)
    if args.output_markdown_file:
        _write_text(Path(args.output_markdown_file), str(result.get("markdown") or ""))
    status = str(result["status"])
    summary = result["summary"]
    print(
        f"{status} final acceptance report: blockers={summary['blockers']} "
        f"fail={summary['FAIL']} blocked={summary['BLOCKED']}"
    )
    return _EXIT_CODES.get(status, 1)


def collect_final_acceptance_report(
    *,
    business_trial_file: Path | None = None,
    launch_gap_plan_file: Path | None = None,
    acceptance_coverage_file: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, object]:
    inputs = {
        "business_trial": _load_input("business_trial", business_trial_file),
        "launch_gap_plan": _load_input("launch_gap_plan", launch_gap_plan_file),
        "acceptance_coverage": _load_input("acceptance_coverage", acceptance_coverage_file),
    }
    blockers = _collect_blockers(inputs)
    statuses = [str(item["load_status"]) for item in inputs.values()]
    statuses.extend(str(item.get("status") or "FAIL") for item in inputs.values() if item["load_status"] == "PASS")
    statuses.extend(str(blocker["status"]) for blocker in blockers)
    status = _overall_status(statuses)
    result = {
        "status": status,
        "conclusion": _conclusion(status),
        "generated_at": generated_at or datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "input_statuses": {
            _SOURCE_LABELS[source]: {
                "load_status": input_state["load_status"],
                "status": input_state.get("status"),
                "supplied": input_state["supplied"],
                "detail": input_state.get("detail", ""),
            }
            for source, input_state in inputs.items()
        },
        "summary": _summary(inputs, blockers),
        "stage_scores": _stage_scores(inputs),
        "blockers": blockers,
    }
    result["markdown"] = _markdown(result)
    return _sanitize_value(result)


def _load_input(source: str, path: Path | None) -> dict[str, object]:
    if path is None:
        return {
            "source": source,
            "supplied": False,
            "load_status": "BLOCKED",
            "status": "BLOCKED",
            "data": {},
            "detail": f"{_SOURCE_LABELS[source]} file was not supplied",
        }
    if not path.exists():
        return {
            "source": source,
            "supplied": True,
            "load_status": "BLOCKED",
            "status": "BLOCKED",
            "data": {},
            "detail": f"{_SOURCE_LABELS[source]} file does not exist: {path}",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "source": source,
            "supplied": True,
            "load_status": "FAIL",
            "status": "FAIL",
            "data": {},
            "detail": f"{_SOURCE_LABELS[source]} file could not be loaded: {type(exc).__name__}",
        }
    if not isinstance(payload, Mapping):
        return {
            "source": source,
            "supplied": True,
            "load_status": "FAIL",
            "status": "FAIL",
            "data": {},
            "detail": f"{_SOURCE_LABELS[source]} root must be a JSON object",
        }
    return {
        "source": source,
        "supplied": True,
        "load_status": "PASS",
        "status": _coerce_status(payload.get("status")),
        "data": dict(payload),
        "detail": str(payload.get("detail") or ""),
    }


def _collect_blockers(inputs: Mapping[str, Mapping[str, object]]) -> list[dict[str, object]]:
    blockers: list[dict[str, object]] = []
    for source in ("business_trial", "launch_gap_plan", "acceptance_coverage"):
        input_state = inputs[source]
        if input_state["load_status"] != "PASS":
            blockers.append(_input_blocker(input_state))
    launch_state = inputs["launch_gap_plan"]
    if launch_state["load_status"] == "PASS":
        blockers.extend(_launch_gap_blockers(launch_state))
    business_state = inputs["business_trial"]
    if business_state["load_status"] == "PASS":
        blockers.extend(_check_blockers("business_trial_acceptance", business_state))
    coverage_state = inputs["acceptance_coverage"]
    if coverage_state["load_status"] == "PASS":
        blockers.extend(_coverage_blockers(coverage_state))
    return blockers


def _input_blocker(input_state: Mapping[str, object]) -> dict[str, object]:
    source = str(input_state["source"])
    return {
        "id": f"INPUT:{source}",
        "name": "final_acceptance_input",
        "source": _SOURCE_LABELS[source],
        "status": _coerce_status(input_state.get("load_status")),
        "owner": "ops",
        "detail": str(input_state.get("detail") or ""),
        "required_env": [],
        "required_files": [_SOURCE_LABELS[source]],
        "command": _input_command(source),
        "next_action": f"Generate and supply the {_SOURCE_LABELS[source]} JSON before final acceptance.",
    }


def _launch_gap_blockers(input_state: Mapping[str, object]) -> list[dict[str, object]]:
    data = _mapping(input_state.get("data"))
    gaps = data.get("gaps")
    if not isinstance(gaps, list):
        return [
            {
                "id": "LAUNCH-GAPS",
                "name": "launch_gap_plan",
                "source": "launch_gap_plan",
                "status": "FAIL",
                "owner": "ops",
                "detail": "launch_gap_plan gaps must be a list",
                "required_env": [],
                "required_files": [],
                "command": _input_command("launch_gap_plan"),
                "next_action": "Regenerate the launch gap plan JSON.",
            }
        ]
    blockers = []
    for gap in gaps:
        if not isinstance(gap, Mapping):
            continue
        status = _coerce_status(gap.get("status"))
        if status == "PASS":
            continue
        blockers.append(
            {
                "id": str(gap.get("id") or "UNKNOWN"),
                "name": str(gap.get("name") or "launch_gap"),
                "source": "launch_gap_plan",
                "status": status,
                "owner": str(gap.get("owner") or "project_owner"),
                "detail": str(gap.get("detail") or ""),
                "required_env": _string_list(gap.get("required_env")),
                "required_files": _string_list(gap.get("required_files")),
                "command": str(gap.get("command") or _input_command("launch_gap_plan")),
                "next_action": str(gap.get("next_action") or "Resolve this launch gap and rerun final acceptance."),
            }
        )
    return blockers


def _check_blockers(source: str, input_state: Mapping[str, object]) -> list[dict[str, object]]:
    data = _mapping(input_state.get("data"))
    checks = data.get("checks")
    if not isinstance(checks, list):
        return [
            {
                "id": f"{source}:CHECKS",
                "name": "checks",
                "source": source,
                "status": "FAIL",
                "owner": "qa",
                "detail": f"{source} checks must be a list",
                "required_env": [],
                "required_files": [],
                "command": _input_command("business_trial"),
                "next_action": f"Regenerate the {source} JSON.",
            }
        ]
    blockers = []
    for check in checks:
        if not isinstance(check, Mapping):
            continue
        status = _coerce_status(check.get("status"))
        if status == "PASS":
            continue
        blockers.append(
            {
                "id": str(check.get("id") or "UNKNOWN"),
                "name": str(check.get("name") or "acceptance_check"),
                "source": source,
                "status": status,
                "owner": str(_mapping(check.get("evidence")).get("owner") or "project_owner"),
                "detail": str(check.get("detail") or ""),
                "required_env": _string_list(_mapping(check.get("evidence")).get("required_env")),
                "required_files": _string_list(_mapping(check.get("evidence")).get("required_files")),
                "command": str(_mapping(check.get("evidence")).get("command") or _input_command("business_trial")),
                "next_action": str(_mapping(check.get("evidence")).get("next_action") or "Resolve this check and rerun final acceptance."),
            }
        )
    return blockers


def _coverage_blockers(input_state: Mapping[str, object]) -> list[dict[str, object]]:
    data = _mapping(input_state.get("data"))
    blockers = []
    for failure in _list_of_mappings(data.get("coverage_failures")):
        status = _coerce_status(failure.get("status"))
        if status == "PASS":
            continue
        blockers.append(
            {
                "id": str(failure.get("id") or "COVERAGE"),
                "name": "coverage_failure",
                "source": "acceptance_coverage",
                "status": status,
                "owner": "qa",
                "detail": str(failure.get("detail") or ""),
                "required_env": [],
                "required_files": [],
                "command": _input_command("acceptance_coverage"),
                "next_action": "Fix acceptance coverage drift and regenerate the coverage matrix.",
            }
        )
    raw_criteria = data.get("criteria")
    criteria = _list_of_mappings(raw_criteria)
    if not isinstance(raw_criteria, list) or not criteria or len(criteria) != len(raw_criteria):
        blockers.append(
            {
                "id": "acceptance_coverage:CRITERIA",
                "name": "criteria",
                "source": "acceptance_coverage",
                "status": "FAIL",
                "owner": "qa",
                "detail": "acceptance_coverage criteria must be a non-empty list of objects",
                "required_env": [],
                "required_files": [],
                "command": _input_command("acceptance_coverage"),
                "next_action": "Regenerate the acceptance coverage matrix from the project acceptance criteria.",
            }
        )
        return blockers
    for criterion in criteria:
        status = _coerce_status(criterion.get("status"))
        if status == "PASS":
            continue
        blockers.append(
            {
                "id": str(criterion.get("id") or "AC-UNKNOWN"),
                "name": str(criterion.get("gate_kind") or "acceptance_criterion"),
                "source": "acceptance_coverage",
                "status": status,
                "owner": str(criterion.get("owner") or "project_owner"),
                "detail": str(criterion.get("detail") or ""),
                "required_env": [],
                "required_files": _missing_or_local_artifacts(criterion),
                "command": _input_command("acceptance_coverage"),
                "next_action": "Resolve the acceptance criterion blocker and regenerate the coverage matrix.",
            }
        )
    return blockers


def _summary(inputs: Mapping[str, Mapping[str, object]], blockers: Sequence[Mapping[str, object]]) -> dict[str, int]:
    summary = {status: 0 for status in ("PASS", "FAIL", "BLOCKED")}
    for input_state in inputs.values():
        if input_state["load_status"] == "PASS":
            summary[_coerce_status(input_state.get("status"))] += 1
        else:
            summary[_coerce_status(input_state.get("load_status"))] += 1
    return {
        **summary,
        "inputs": len(inputs),
        "blockers": len(blockers),
        "unique_blockers": len({str(blocker.get("id") or "") for blocker in blockers}),
    }


def _stage_scores(inputs: Mapping[str, Mapping[str, object]]) -> dict[str, int]:
    return {
        _STAGE_SCORE_LABELS[source]: _score_input(input_state)
        for source, input_state in inputs.items()
    }


def _score_input(input_state: Mapping[str, object]) -> int:
    if input_state.get("load_status") != "PASS":
        return 0
    status = _coerce_status(input_state.get("status"))
    if status == "PASS":
        return 100
    if status == "BLOCKED":
        return 50
    return 0


def _markdown(result: Mapping[str, object]) -> str:
    lines = [
        "# AiManager Final Acceptance Report",
        "",
        f"- Status: {result['status']}",
        f"- Final conclusion: {result['conclusion']}",
        f"- Generated At: {result['generated_at']}",
        f"- Inputs PASS/FAIL/BLOCKED: {result['summary']['PASS']}/{result['summary']['FAIL']}/{result['summary']['BLOCKED']}",
        f"- Blockers: {result['summary']['blockers']}",
        f"- Unique Blockers: {result['summary']['unique_blockers']}",
        "",
        "## Stage Scores",
        "",
    ]
    for name, score in _mapping(result.get("stage_scores")).items():
        lines.append(f"- {name}: {score}")
    lines.append("")
    blockers = result.get("blockers")
    if not isinstance(blockers, list) or not blockers:
        lines.append("No unresolved acceptance blockers remain in the supplied machine-readable gates.")
        return "\n".join(lines) + "\n"
    lines.extend(["## Unresolved Blockers", ""])
    for blocker in blockers:
        if not isinstance(blocker, Mapping):
            continue
        lines.extend(
            [
                f"### {blocker['status']} {blocker['id']} - {blocker['name']}",
                "",
                f"- Source: {blocker['source']}",
                f"- Owner: {blocker['owner']}",
                f"- Detail: {_markdown_cell(str(blocker['detail']))}",
                f"- Required Env: {', '.join(blocker['required_env']) or 'none'}",
                f"- Required Files: {', '.join(blocker['required_files']) or 'none'}",
                f"- Command: `{blocker['command']}`",
                f"- Next Action: {blocker['next_action']}",
                "",
            ]
        )
    return "\n".join(lines)


def _input_command(source: str) -> str:
    if source == "business_trial":
        return (
            "PYTHONPATH=\"$PWD\" uv run --no-project --with pyyaml python -m "
            "aimanager.scripts.business_trial_acceptance_bundle --output-json-file "
            "/tmp/aimanager-business-trial-acceptance.json"
        )
    if source == "launch_gap_plan":
        return (
            "PYTHONPATH=\"$PWD\" uv run --no-project python -m aimanager.scripts.generate_launch_gap_plan "
            "--production-readiness-file /tmp/aimanager-production-readiness.json "
            "--business-trial-file /tmp/aimanager-business-trial-acceptance.json "
            "--output-json-file /tmp/aimanager-launch-gap-plan.json "
            "--output-markdown-file /tmp/aimanager-launch-gap-plan.md"
        )
    return (
        "PYTHONPATH=\"$PWD\" uv run --no-project python -m aimanager.scripts.acceptance_coverage_matrix "
        "--production-readiness-file /tmp/aimanager-production-readiness.json "
        "--business-trial-file /tmp/aimanager-business-trial-acceptance.json "
        "--output-json-file /tmp/aimanager-acceptance-coverage.json "
        "--output-markdown-file /tmp/aimanager-acceptance-coverage.md"
    )


def _missing_or_local_artifacts(criterion: Mapping[str, object]) -> list[str]:
    missing = _string_list(criterion.get("missing_artifacts"))
    if missing:
        return missing
    artifacts = criterion.get("local_artifacts")
    if not isinstance(artifacts, list):
        return []
    paths = []
    for artifact in artifacts:
        if isinstance(artifact, Mapping) and isinstance(artifact.get("path"), str):
            paths.append(str(artifact["path"]))
    return paths


def _overall_status(statuses: Sequence[str]) -> str:
    if any(status == "FAIL" for status in statuses):
        return "FAIL"
    if any(status == "BLOCKED" for status in statuses):
        return "BLOCKED"
    return "PASS"


def _conclusion(status: str) -> str:
    if status == "PASS":
        return "PASS"
    if status == "FAIL":
        return "FAIL"
    return "CONDITIONAL"


def _coerce_status(status: object) -> str:
    return str(status) if status in {"PASS", "FAIL", "BLOCKED"} else "FAIL"


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list_of_mappings(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _sanitize_value(value: object) -> object:
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _sanitize_value(item) for key, item in value.items()}
    return value


def _sanitize_text(text: str) -> str:
    sanitized = text
    for pattern, replacement in _SECRET_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


def _markdown_cell(text: str) -> str:
    return text.replace("\n", "<br>")


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

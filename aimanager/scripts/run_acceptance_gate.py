from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from aimanager.redaction import (
    build_secret_redactions,
    sanitize_text as sanitize_secret_text,
    sanitize_value as sanitize_secret_value,
)
from aimanager.scripts.acceptance_coverage_matrix import collect_acceptance_coverage_matrix
from aimanager.scripts.business_trial_acceptance_bundle import collect_business_trial_acceptance
from aimanager.scripts.generate_final_acceptance_report import collect_final_acceptance_report
from aimanager.scripts.generate_launch_gap_plan import collect_launch_gap_plan
from aimanager.scripts.production_readiness_bundle import collect_production_readiness


_EXIT_CODES = {"PASS": 0, "FAIL": 1, "BLOCKED": 2}
_STATUS_ORDER = {"FAIL": 0, "BLOCKED": 1, "PASS": 2}

ProductionReadinessCollector = Callable[..., dict[str, Any]]
BusinessTrialCollector = Callable[..., dict[str, Any]]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the complete AiManager acceptance gate into one run-scoped artifact directory."
    )
    parser.add_argument("--output-dir", required=True, help="Directory for all generated acceptance artifacts.")
    parser.add_argument("--acceptance-doc-file", default="docs/aimanager/1_acceptance_criteria.md")
    parser.add_argument("--project-directory", default=".")
    parser.add_argument("--generated-at", help="Override generated_at timestamp for deterministic runs.")
    args = parser.parse_args(argv)

    try:
        result = collect_acceptance_gate(
            output_dir=Path(args.output_dir),
            acceptance_doc_file=Path(args.acceptance_doc_file),
            project_directory=Path(args.project_directory),
            generated_at=args.generated_at,
        )
    except Exception as exc:
        sys.stderr.write(f"FAIL acceptance gate: {type(exc).__name__}\n")
        return 1

    status = str(result["status"])
    summary = _mapping(result.get("summary"))
    final_report = _mapping(result.get("final_report"))
    sys.stdout.write(
        f"{status} acceptance gate: final={final_report.get('status', 'FAIL')} "
        f"blockers={summary.get('blockers', 0)} unique={summary.get('unique_blockers', 0)} "
        f"artifacts={result['artifact_dir']}\n"
    )
    return _EXIT_CODES.get(status, 1)


def collect_acceptance_gate(
    *,
    output_dir: Path,
    acceptance_doc_file: Path,
    project_directory: Path,
    env: Mapping[str, str] | None = None,
    generated_at: str | None = None,
    production_readiness_collector: ProductionReadinessCollector = collect_production_readiness,
    business_trial_collector: BusinessTrialCollector = collect_business_trial_acceptance,
) -> dict[str, object]:
    current_env = os.environ if env is None else env
    gate_generated_at = generated_at or _now_iso()
    redactions = _redactions(current_env)

    output_dir.mkdir(parents=True, exist_ok=True)
    paths = _artifact_paths(output_dir)

    production = _sanitize_value(
        production_readiness_collector(env=current_env, generated_at=gate_generated_at),
        redactions,
    )
    _write_json(paths["production_json"], production)

    def cached_production_readiness_collector(**_: object) -> dict[str, Any]:
        return dict(production)

    business = _sanitize_value(
        business_trial_collector(
            env=current_env,
            generated_at=gate_generated_at,
            production_readiness_collector=cached_production_readiness_collector,
        ),
        redactions,
    )
    _write_json(paths["business_json"], business)

    launch = _sanitize_value(
        collect_launch_gap_plan(
            production_readiness_file=paths["production_json"],
            business_trial_file=paths["business_json"],
            generated_at=gate_generated_at,
        ),
        redactions,
    )
    _write_json(paths["launch_json"], launch)
    _write_text(paths["launch_markdown"], str(_mapping(launch).get("markdown") or ""))

    coverage = _sanitize_value(
        collect_acceptance_coverage_matrix(
            acceptance_doc_file=acceptance_doc_file,
            project_directory=project_directory,
            production_readiness_file=paths["production_json"],
            business_trial_file=paths["business_json"],
            generated_at=gate_generated_at,
        ),
        redactions,
    )
    _write_json(paths["coverage_json"], coverage)
    _write_text(paths["coverage_markdown"], str(_mapping(coverage).get("markdown") or ""))

    final_report = _sanitize_value(
        collect_final_acceptance_report(
            business_trial_file=paths["business_json"],
            launch_gap_plan_file=paths["launch_json"],
            acceptance_coverage_file=paths["coverage_json"],
            generated_at=gate_generated_at,
        ),
        redactions,
    )
    _write_json(paths["final_json"], final_report)
    _write_text(paths["final_markdown"], str(_mapping(final_report).get("markdown") or ""))

    result = _manifest(
        output_dir=output_dir,
        generated_at=gate_generated_at,
        production=production,
        business=business,
        launch=launch,
        coverage=coverage,
        final_report=final_report,
        paths=paths,
    )
    result = _sanitize_value(result, redactions)
    _write_json(paths["gate_json"], result)
    _write_text(paths["gate_markdown"], str(_mapping(result).get("markdown") or ""))
    return result


def _manifest(
    *,
    output_dir: Path,
    generated_at: str,
    production: object,
    business: object,
    launch: object,
    coverage: object,
    final_report: object,
    paths: Mapping[str, Path],
) -> dict[str, object]:
    steps = [
        _step("production_readiness", "production readiness bundle", production, json_file=paths["production_json"]),
        _step(
            "business_trial_acceptance", "business trial acceptance bundle", business, json_file=paths["business_json"]
        ),
        _step(
            "launch_gap_plan",
            "launch gap plan",
            launch,
            json_file=paths["launch_json"],
            markdown_file=paths["launch_markdown"],
        ),
        _step(
            "acceptance_coverage_matrix",
            "acceptance coverage matrix",
            coverage,
            json_file=paths["coverage_json"],
            markdown_file=paths["coverage_markdown"],
        ),
        _step(
            "final_acceptance_report",
            "final acceptance report",
            final_report,
            json_file=paths["final_json"],
            markdown_file=paths["final_markdown"],
        ),
    ]
    status = _overall_status(str(step["status"]) for step in steps)
    final_summary = _mapping(_mapping(final_report).get("summary"))
    result = {
        "status": status,
        "generated_at": generated_at,
        "artifact_dir": str(output_dir),
        "summary": {
            "steps": len(steps),
            "PASS": sum(1 for step in steps if step["status"] == "PASS"),
            "FAIL": sum(1 for step in steps if step["status"] == "FAIL"),
            "BLOCKED": sum(1 for step in steps if step["status"] == "BLOCKED"),
            "blockers": int(final_summary.get("blockers", 0) or 0),
            "unique_blockers": int(final_summary.get("unique_blockers", 0) or 0),
        },
        "steps": steps,
        "final_report": {
            "status": _coerce_status(_mapping(final_report).get("status")),
            "conclusion": str(_mapping(final_report).get("conclusion") or ""),
            "json_file": str(paths["final_json"]),
            "markdown_file": str(paths["final_markdown"]),
            "summary": final_summary,
        },
    }
    result["markdown"] = _markdown(result)
    return result


def _step(
    step_id: str,
    name: str,
    payload: object,
    *,
    json_file: Path,
    markdown_file: Path | None = None,
) -> dict[str, object]:
    data = _mapping(payload)
    summary = _mapping(data.get("summary"))
    step: dict[str, object] = {
        "id": step_id,
        "name": name,
        "status": _coerce_status(data.get("status")),
        "json_file": str(json_file),
        "summary": summary,
    }
    if markdown_file:
        step["markdown_file"] = str(markdown_file)
    return step


def _markdown(result: Mapping[str, object]) -> str:
    summary = _mapping(result.get("summary"))
    lines = [
        "# AiManager Acceptance Gate",
        "",
        f"- Status: {result['status']}",
        f"- Generated At: {result['generated_at']}",
        f"- Artifact Dir: {result['artifact_dir']}",
        f"- Steps PASS/FAIL/BLOCKED: {summary.get('PASS', 0)}/{summary.get('FAIL', 0)}/{summary.get('BLOCKED', 0)}",
        f"- Final Blockers: {summary.get('blockers', 0)}",
        f"- Unique Final Blockers: {summary.get('unique_blockers', 0)}",
        "",
        "| Step | Status | JSON | Markdown |",
        "| --- | --- | --- | --- |",
    ]
    for step in result.get("steps", []):
        if not isinstance(step, Mapping):
            continue
        lines.append(
            "| {name} | {status} | `{json_file}` | {markdown_file} |".format(
                name=step.get("name", ""),
                status=step.get("status", ""),
                json_file=step.get("json_file", ""),
                markdown_file=f"`{step['markdown_file']}`" if step.get("markdown_file") else "-",
            )
        )
    final_report = _mapping(result.get("final_report"))
    if final_report:
        lines.extend(
            [
                "",
                "## Final Report",
                "",
                f"- Status: {final_report.get('status', '')}",
                f"- Conclusion: {final_report.get('conclusion', '')}",
                f"- JSON: `{final_report.get('json_file', '')}`",
                f"- Markdown: `{final_report.get('markdown_file', '')}`",
            ]
        )
    return "\n".join(lines) + "\n"


def _artifact_paths(output_dir: Path) -> dict[str, Path]:
    return {
        "production_json": output_dir / "production-readiness.json",
        "business_json": output_dir / "business-trial-acceptance.json",
        "launch_json": output_dir / "launch-gap-plan.json",
        "launch_markdown": output_dir / "launch-gap-plan.md",
        "coverage_json": output_dir / "acceptance-coverage.json",
        "coverage_markdown": output_dir / "acceptance-coverage.md",
        "final_json": output_dir / "final-acceptance-report.json",
        "final_markdown": output_dir / "final-acceptance-report.md",
        "gate_json": output_dir / "acceptance-gate.json",
        "gate_markdown": output_dir / "acceptance-gate.md",
    }


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _sanitize_value(value: object, redactions: Mapping[str, str]) -> object:
    return sanitize_secret_value(value, redactions)


def _sanitize_text(value: str, redactions: Mapping[str, str]) -> str:
    return sanitize_secret_text(value, redactions)


def _redactions(env: Mapping[str, str]) -> dict[str, str]:
    return build_secret_redactions(env)


def _overall_status(statuses: Sequence[str] | Any) -> str:
    values = list(statuses)
    if any(status == "FAIL" for status in values):
        return "FAIL"
    if any(status == "BLOCKED" for status in values):
        return "BLOCKED"
    return "PASS"


def _coerce_status(value: object) -> str:
    return str(value) if value in _EXIT_CODES else "FAIL"


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())

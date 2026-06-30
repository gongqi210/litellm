from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from aimanager.work_context import WorkContextMode, WorkContextValidationResult, validate_work_context


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate AiManager work-context metadata before governed usage.")
    parser.add_argument("--context-file", required=True, help="JSON object containing AiManager work-context metadata.")
    parser.add_argument(
        "--mode",
        choices=("preflight", "closure"),
        default="preflight",
        help="preflight validates request-time metadata; closure validates the full marketing content workflow.",
    )
    parser.add_argument("--output-json-file", help="Optional path to write the validation result JSON.")
    args = parser.parse_args(argv)

    result = _load_and_validate(Path(args.context_file), mode=args.mode)
    write_error = ""
    if args.output_json_file:
        try:
            _write_result(Path(args.output_json_file), result)
        except Exception as exc:
            write_error = type(exc).__name__

    print(
        f"{result.status} work context: "
        f"errors={len(result.errors)} warnings={len(result.warnings)} {result.detail}"
    )
    if write_error:
        print(f"WARN work context result file not written: {write_error}", file=sys.stderr)
    if result.status == "FAIL":
        return 1
    if result.status == "BLOCKED":
        return 2
    if write_error:
        return 1
    return 0


def _load_and_validate(path: Path, *, mode: WorkContextMode) -> WorkContextValidationResult:
    try:
        payload = _load_json_object(path)
    except FileNotFoundError:
        return WorkContextValidationResult(
            status="BLOCKED",
            detail=f"work context file does not exist: {path}",
            errors=["context_file"],
        )
    except Exception as exc:
        return WorkContextValidationResult(
            status="FAIL",
            detail=f"work context file could not be loaded: {type(exc).__name__}",
            errors=["context_file"],
        )
    return validate_work_context(payload, mode=mode)


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _write_result(path: Path, result: WorkContextValidationResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

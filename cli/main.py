from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT_ROOT / "cli" / "aimanager_cli_manifest.json"


def load_manifest() -> dict[str, Any]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("CLI manifest must be a JSON object")
    commands = manifest.get("commands")
    if not isinstance(commands, dict) or not commands:
        raise ValueError("CLI manifest must include commands")
    return manifest


def build_status_payload() -> dict[str, Any]:
    manifest = load_manifest()
    return {
        "status": "PASS",
        "project": manifest.get("project", "AiManager"),
        "project_type": manifest.get("project_type", "software-cli"),
        "commands": manifest["commands"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AiManager project-level CLI entry.")
    parser.add_argument("--json", action="store_true", help="Emit one machine-readable status JSON object.")
    parser.add_argument("--list-commands", action="store_true", help="Print documented AiManager command names.")
    args = parser.parse_args(argv)

    try:
        payload = build_status_payload()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"FAIL aimanager cli manifest: {type(exc).__name__}\n")
        return 1

    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        return 0

    if args.list_commands:
        for name in sorted(payload["commands"]):
            sys.stdout.write(f"{name}\n")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

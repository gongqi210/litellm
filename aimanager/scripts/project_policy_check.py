from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple


VALID_STATUSES = {"PASS", "FAIL", "BLOCKED", "SKIP"}
SOURCE_LINE_LIMIT_DEFAULT = 1600
DOC_LINE_LIMIT_DEFAULT = 800
FORBIDDEN_ENV_EXAMPLE_MARKERS = (
    "sk-",
    "AKIA",
    "AIza",
    "BEGIN PRIVATE KEY",
    "xoxb-",
)
FORBIDDEN_CONFIG_MARKERS = (
    "api.openai.com",
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "AZURE_API_KEY",
    "BEDROCK",
    "VERTEX",
)


class CheckResult(NamedTuple):
    status: str
    name: str
    message: str


def _result(status: str, name: str, message: str) -> CheckResult:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid policy status: {status}")
    return CheckResult(status=status, name=name, message=message)


def _load_simple_yaml(path: Path) -> dict[str, object]:
    data: dict[str, object] = {}
    current_list: str | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith("  - ") and current_list:
            value = line.removeprefix("  - ").strip()
            item = data.setdefault(current_list, [])
            if isinstance(item, list):
                item.append(value)
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        current_list = None
        if not value:
            data[key] = []
            current_list = key
        elif value.isdigit():
            data[key] = int(value)
        else:
            data[key] = value.strip("'\"")
    return data


def _project_type(project_root: Path) -> CheckResult:
    path = project_root / "PROJECT_TYPE"
    if not path.exists():
        return _result("FAIL", "project_type", "PROJECT_TYPE is missing")
    value = path.read_text(encoding="utf-8").strip()
    if value != "software-cli":
        return _result("FAIL", "project_type", f"expected software-cli, got {value or '<empty>'}")
    return _result("PASS", "project_type", "PROJECT_TYPE=software-cli")


def _required_scaffold(project_root: Path) -> CheckResult:
    required = [
        "PROJECT_TYPE",
        "AIMANAGER.md",
        "Makefile",
        "cli/main.py",
        "cli/README.md",
        "cli/aimanager_cli_manifest.json",
        "docs/architecture/allowed_deps.yml",
        "logs/README.md",
        "scripts/project_policy_check.py",
    ]
    missing = [item for item in required if not (project_root / item).exists()]
    if missing:
        return _result("FAIL", "required_scaffold", f"missing required file(s): {', '.join(missing)}")
    makefile = (project_root / "Makefile").read_text(encoding="utf-8")
    if "\npolicy-check:" not in f"\n{makefile}":
        return _result("FAIL", "required_scaffold", "Makefile must expose policy-check")
    return _result("PASS", "required_scaffold", "project scaffold files are present")


def _cli_contract(project_root: Path) -> CheckResult:
    cli = project_root / "cli" / "main.py"
    help_result = subprocess.run(
        [sys.executable, str(cli), "--help"],
        cwd=project_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if help_result.returncode != 0:
        return _result("FAIL", "cli_contract", "cli/main.py --help failed")

    json_result = subprocess.run(
        [sys.executable, str(cli), "--json"],
        cwd=project_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if json_result.returncode != 0:
        return _result("FAIL", "cli_contract", "cli/main.py --json failed")
    try:
        payload = json.loads(json_result.stdout)
    except json.JSONDecodeError:
        return _result("FAIL", "cli_contract", "cli/main.py --json did not emit JSON")
    if payload.get("status") != "PASS":
        return _result("FAIL", "cli_contract", "cli/main.py --json did not report PASS")
    commands = payload.get("commands")
    if not isinstance(commands, dict) or "production_readiness" not in commands:
        return _result("FAIL", "cli_contract", "CLI manifest must include production_readiness")
    return _result("PASS", "cli_contract", "project CLI help/json contract is runnable")


def _owned_text_files(project_root: Path) -> list[Path]:
    roots = [
        project_root / "aimanager",
        project_root / "docs" / "aimanager",
        project_root / "cli",
        project_root / "docs" / "architecture",
    ]
    files: list[Path] = [
        project_root / "AIMANAGER.md",
        project_root / "PROJECT_TYPE",
        project_root / "logs" / "README.md",
        project_root / "scripts" / "project_policy_check.py",
    ]
    suffixes = {".py", ".md", ".yaml", ".yml", ".json", ".example"}
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and (path.suffix in suffixes or path.name.endswith(".example")):
                files.append(path)
    return sorted(path for path in set(files) if path.exists())


def _file_length(project_root: Path, config: dict[str, object]) -> CheckResult:
    source_limit = int(config.get("source_line_limit", SOURCE_LINE_LIMIT_DEFAULT))
    doc_limit = int(config.get("doc_line_limit", DOC_LINE_LIMIT_DEFAULT))
    violations: list[str] = []
    for path in _owned_text_files(project_root):
        try:
            line_count = len(path.read_text(encoding="utf-8").splitlines())
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(project_root)
        limit = doc_limit if path.suffix == ".md" else source_limit
        if line_count > limit:
            violations.append(f"{rel}:{line_count}>{limit}")
    if violations:
        return _result("FAIL", "file_length", "line limit exceeded: " + ", ".join(violations[:8]))
    return _result("PASS", "file_length", "AiManager-owned files are within configured line limits")


def _allowed_deps(project_root: Path, config: dict[str, object]) -> CheckResult:
    allowed = set(config.get("allowed_top_level_imports", []))
    forbidden = set(config.get("forbidden_top_level_imports", []))
    stdlib = set(getattr(sys, "stdlib_module_names", set()))
    seen: set[str] = set()
    forbidden_seen: set[str] = set()
    disallowed_seen: set[str] = set()

    for path in sorted((project_root / "aimanager").rglob("*.py")) + sorted((project_root / "cli").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module.split(".", 1)[0])
            for module in modules:
                seen.add(module)
                if module in forbidden:
                    forbidden_seen.add(module)
                if module not in stdlib and module not in allowed:
                    disallowed_seen.add(module)

    if forbidden_seen:
        return _result("FAIL", "allowed_deps", "forbidden import(s): " + ", ".join(sorted(forbidden_seen)))
    if disallowed_seen:
        return _result("FAIL", "allowed_deps", "undocumented import(s): " + ", ".join(sorted(disallowed_seen)))
    return _result("PASS", "allowed_deps", f"non-stdlib imports are documented: {', '.join(sorted(seen & allowed))}")


def _ycapi_boundary(project_root: Path) -> CheckResult:
    config_path = project_root / "aimanager" / "config.yaml"
    if not config_path.exists():
        return _result("FAIL", "ycapi_boundary", "aimanager/config.yaml is missing")
    raw = config_path.read_text(encoding="utf-8")
    for marker in FORBIDDEN_CONFIG_MARKERS:
        if marker in raw:
            return _result("FAIL", "ycapi_boundary", f"forbidden direct-provider marker: {marker}")
    required = [
        "api_base: os.environ/YCAPI_BASE_URL",
        "api_key: os.environ/YCAPI_API_TOKEN",
        "master_key: os.environ/LITELLM_MASTER_KEY",
        "store_model_in_db: false",
    ]
    missing = [marker for marker in required if marker not in raw]
    if missing:
        return _result("FAIL", "ycapi_boundary", "missing ycapi invariant(s): " + ", ".join(missing))
    if "ycapi-video-1" in raw:
        return _result("FAIL", "ycapi_boundary", "ycapi-video-1 must stay out of LiteLLM model_list")
    return _result("PASS", "ycapi_boundary", "config text remains ycapi-only")


def _env_example_secrets(project_root: Path) -> CheckResult:
    env_path = project_root / "aimanager" / ".env.example"
    if not env_path.exists():
        return _result("FAIL", "env_example_secrets", "aimanager/.env.example is missing")
    raw = env_path.read_text(encoding="utf-8")
    leaked = [marker for marker in FORBIDDEN_ENV_EXAMPLE_MARKERS if marker in raw]
    if leaked:
        return _result("FAIL", "env_example_secrets", "secret-like marker(s) in .env.example: " + ", ".join(leaked))
    if ".env" not in (project_root / ".gitignore").read_text(encoding="utf-8"):
        return _result("FAIL", "env_example_secrets", ".gitignore must ignore .env files")
    return _result("PASS", "env_example_secrets", ".env.example uses placeholders and .env is ignored")


def run_policy_checks(project_root: Path | str) -> list[CheckResult]:
    root = Path(project_root)
    allowed_deps_path = root / "docs" / "architecture" / "allowed_deps.yml"
    config: dict[str, object] = {}
    if allowed_deps_path.exists():
        config = _load_simple_yaml(allowed_deps_path)

    return [
        _project_type(root),
        _required_scaffold(root),
        _cli_contract(root),
        _file_length(root, config),
        _allowed_deps(root, config),
        _ycapi_boundary(root),
        _env_example_secrets(root),
    ]


def exit_code_for(results: list[CheckResult]) -> int:
    if any(result.status == "FAIL" for result in results):
        return 1
    if any(result.status == "BLOCKED" for result in results):
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    project_root = Path(argv[0]).resolve() if argv else Path(__file__).resolve().parents[2]
    results = run_policy_checks(project_root)
    for result in results:
        sys.stdout.write(f"{result.status}\t{result.name}\t{result.message}\n")
    return exit_code_for(results)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

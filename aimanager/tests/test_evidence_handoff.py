from __future__ import annotations

import json

from aimanager.scripts.generate_evidence_handoff import collect_evidence_handoff, main


def test_evidence_handoff_groups_launch_gaps_by_owner_without_unblocking_them(tmp_path) -> None:
    launch_file = tmp_path / "launch-gap-plan.json"
    launch_file.write_text(
        json.dumps(
            _launch_plan(
                status="FAIL",
                gaps=[
                    _gap(
                        "AC-12-13-FINANCE",
                        "finance_export_reconciliation",
                        "BLOCKED",
                        "finance",
                        required_files=["/secure/spend.csv", "/secure/ycapi-bill.csv"],
                    ),
                    _gap(
                        "AC-23",
                        "nontechnical_lightweight_trial",
                        "FAIL",
                        "business_owner/market/ops",
                        required_env=["AIMANAGER_LIGHTWEIGHT_TRIAL_EVIDENCE_FILE"],
                    ),
                ],
            )
        ),
        encoding="utf-8",
    )

    result = collect_evidence_handoff(
        launch_gap_plan_file=launch_file,
        generated_at="2026-07-01T00:00:00Z",
    )

    assert result["status"] == "FAIL"
    assert result["summary"] == {"owners": 2, "gaps": 2, "PASS": 0, "FAIL": 1, "BLOCKED": 1}
    assert [owner["owner"] for owner in result["owners"]] == ["business_owner/market/ops", "finance"]
    finance = next(owner for owner in result["owners"] if owner["owner"] == "finance")
    assert finance["status"] == "BLOCKED"
    assert finance["gaps"][0]["required_files"] == ["/secure/spend.csv", "/secure/ycapi-bill.csv"]
    assert "Evidence request only" in result["markdown"]
    assert "not a PASS artifact" in result["markdown"]


def test_evidence_handoff_cli_writes_manifest_and_owner_markdown_with_redaction(tmp_path, capsys) -> None:
    launch_file = tmp_path / "launch-gap-plan.json"
    output_dir = tmp_path / "handoff"
    launch_file.write_text(
        json.dumps(
            _launch_plan(
                status="BLOCKED",
                gaps=[
                    _gap(
                        "AC-16-WECOM",
                        "wecom_alert_routing",
                        "BLOCKED",
                        "ops",
                        detail=(
                            "deliver alert with Bearer should-not-leak and "
                            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=wecom-secret-key"
                        ),
                        required_env=["AIMANAGER_WECOM_WEBHOOK_URL"],
                    )
                ],
            )
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--launch-gap-plan-file",
            str(launch_file),
            "--output-dir",
            str(output_dir),
            "--generated-at",
            "2026-07-01T00:00:00Z",
        ]
    )

    output = capsys.readouterr().out
    manifest = json.loads((output_dir / "evidence-handoff.json").read_text(encoding="utf-8"))
    owner_markdown = (output_dir / "ops.md").read_text(encoding="utf-8")
    serialized = json.dumps(manifest, ensure_ascii=False) + owner_markdown

    assert exit_code == 2
    assert "BLOCKED evidence handoff" in output
    assert manifest["status"] == "BLOCKED"
    assert manifest["owners"][0]["markdown_file"] == str(output_dir / "ops.md")
    assert "should-not-leak" not in serialized
    assert "wecom-secret-key" not in serialized
    assert "[redacted:secret-like-value]" in serialized
    assert "[redacted:AIMANAGER_WECOM_WEBHOOK_URL]" in serialized
    assert "Do not paste secrets" in owner_markdown


def test_evidence_handoff_preserves_fail_when_launch_plan_is_not_an_object(tmp_path) -> None:
    launch_file = tmp_path / "launch-gap-plan.json"
    launch_file.write_text("[]", encoding="utf-8")

    result = collect_evidence_handoff(launch_gap_plan_file=launch_file)

    assert result["status"] == "FAIL"
    assert result["summary"]["FAIL"] == 1
    assert result["owners"][0]["gaps"][0]["name"] == "invalid_launch_gap_plan"


def test_evidence_handoff_fails_for_bad_launch_plan_json(tmp_path) -> None:
    launch_file = tmp_path / "launch-gap-plan.json"
    launch_file.write_text("{not-json", encoding="utf-8")

    result = collect_evidence_handoff(launch_gap_plan_file=launch_file)

    assert result["status"] == "FAIL"
    assert result["summary"]["FAIL"] == 1
    assert result["owners"][0]["gaps"][0]["name"] == "bad_launch_gap_plan_json"


def test_evidence_handoff_preserves_non_pass_top_level_status_without_gaps(tmp_path) -> None:
    launch_file = tmp_path / "launch-gap-plan.json"
    launch_file.write_text(json.dumps(_launch_plan(status="BLOCKED", gaps=[])), encoding="utf-8")

    result = collect_evidence_handoff(launch_gap_plan_file=launch_file)

    assert result["status"] == "BLOCKED"
    assert result["summary"]["BLOCKED"] == 1
    assert result["owners"][0]["gaps"][0]["id"] == "INPUT:launch_gap_plan"


def test_evidence_handoff_surfaces_non_object_gap_entries(tmp_path) -> None:
    launch_file = tmp_path / "launch-gap-plan.json"
    plan = {
        "status": "BLOCKED",
        "generated_at": "2026-07-01T00:00:00Z",
        "summary": {"PASS": 0, "FAIL": 0, "BLOCKED": 1},
        "gaps": [
            _gap("AC-19", "live_ycapi_preflight", "BLOCKED", "architecture/security/ops"),
            "not-a-gap-object",
        ],
    }
    launch_file.write_text(json.dumps(plan), encoding="utf-8")

    result = collect_evidence_handoff(launch_gap_plan_file=launch_file)

    assert result["status"] == "FAIL"
    assert result["summary"] == {"owners": 2, "gaps": 2, "PASS": 0, "FAIL": 1, "BLOCKED": 1}
    assert [owner["owner"] for owner in result["owners"]] == ["architecture/security/ops", "project_owner"]
    project_owner = next(owner for owner in result["owners"] if owner["owner"] == "project_owner")
    assert project_owner["gaps"][0]["name"] == "invalid_launch_gap_plan_gaps"


def test_evidence_handoff_cli_exits_fail_for_malformed_launch_plan(tmp_path, capsys) -> None:
    launch_file = tmp_path / "launch-gap-plan.json"
    output_dir = tmp_path / "handoff"
    launch_file.write_text("[]", encoding="utf-8")

    exit_code = main(["--launch-gap-plan-file", str(launch_file), "--output-dir", str(output_dir)])

    output = capsys.readouterr().out
    manifest = json.loads((output_dir / "evidence-handoff.json").read_text(encoding="utf-8"))
    assert exit_code == 1
    assert "FAIL evidence handoff" in output
    assert manifest["status"] == "FAIL"


def _launch_plan(*, status: str, gaps: list[dict[str, object]]) -> dict[str, object]:
    return {
        "status": status,
        "generated_at": "2026-07-01T00:00:00Z",
        "summary": {
            "PASS": 0,
            "FAIL": sum(1 for gap in gaps if gap["status"] == "FAIL"),
            "BLOCKED": sum(1 for gap in gaps if gap["status"] == "BLOCKED"),
        },
        "gaps": gaps,
    }


def _gap(
    gap_id: str,
    name: str,
    status: str,
    owner: str,
    *,
    detail: str = "missing evidence",
    required_env: list[str] | None = None,
    required_files: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": gap_id,
        "name": name,
        "status": status,
        "owner": owner,
        "detail": detail,
        "required_env": required_env or [],
        "required_files": required_files or [],
        "command": f"python -m aimanager.scripts.resolve_{name}",
        "next_action": f"collect evidence for {gap_id}",
        "sources": ["launch_gap_plan"],
    }

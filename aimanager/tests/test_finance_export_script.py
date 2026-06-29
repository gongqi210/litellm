from __future__ import annotations

import csv
import json

from aimanager.scripts.export_finance import main


def test_finance_export_script_writes_usage_monthly_and_reconciliation_csv(tmp_path) -> None:
    spend_file = tmp_path / "spend.json"
    bill_file = tmp_path / "ycapi_bill.csv"
    output_dir = tmp_path / "out"

    spend_file.write_text(
        json.dumps(
            [
                {
                    "startTime": "2026-06-30T02:15:00Z",
                    "call_type": "completion",
                    "user": "u_market_1",
                    "model": "openai/gemini-2.5-flash",
                    "prompt_tokens": 150,
                    "completion_tokens": 40,
                    "total_tokens": 190,
                    "spend": "2.00",
                    "currency": "CNY",
                    "metadata": {
                        "user_api_key_alias": "market-campaign-key",
                        "user_api_key_metadata": {
                            "department_id": "dept_market",
                            "project_id": "proj_launch",
                            "cost_center_id": "cc_growth",
                            "pricing_version": "m1-2026-06",
                        },
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    with bill_file.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "billing_month",
                "model_name",
                "api_path",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "amount",
                "currency",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "billing_month": "2026-06",
                "model_name": "gemini-2.5-flash",
                "api_path": "/v1/chat/completions",
                "prompt_tokens": "150",
                "completion_tokens": "40",
                "total_tokens": "190",
                "amount": "2.10",
                "currency": "CNY",
            }
        )

    exit_code = main(
        [
            "--spend-file",
            str(spend_file),
            "--ycapi-bill-file",
            str(bill_file),
            "--output-dir",
            str(output_dir),
        ]
    )

    assert exit_code == 0
    assert sorted(path.name for path in output_dir.iterdir()) == [
        "aimanager_finance_monthly.csv",
        "aimanager_reconciliation.csv",
        "aimanager_usage_daily.csv",
    ]

    usage_rows = list(csv.DictReader((output_dir / "aimanager_usage_daily.csv").open(encoding="utf-8")))
    assert usage_rows[0]["department_id"] == "dept_market"
    assert usage_rows[0]["project_id"] == "proj_launch"
    assert usage_rows[0]["key_alias"] == "market-campaign-key"
    assert usage_rows[0]["model"] == "gemini-2.5-flash"
    assert usage_rows[0]["spend"] == "2"

    monthly_rows = list(csv.DictReader((output_dir / "aimanager_finance_monthly.csv").open(encoding="utf-8")))
    assert monthly_rows[0]["close_status"] == "ready"

    reconciliation_rows = list(csv.DictReader((output_dir / "aimanager_reconciliation.csv").open(encoding="utf-8")))
    assert reconciliation_rows[0]["aimanager_amount"] == "2"
    assert reconciliation_rows[0]["ycapi_amount"] == "2.1"
    assert reconciliation_rows[0]["difference"] == "0.1"
    assert reconciliation_rows[0]["status"] == "matched"

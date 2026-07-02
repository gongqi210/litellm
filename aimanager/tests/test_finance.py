from __future__ import annotations

from decimal import Decimal

from aimanager.finance import (
    aggregate_daily_usage,
    aggregate_monthly_usage,
    build_finance_export_bundle,
    normalize_spend_record,
    normalize_ycapi_bill_record,
    reconcile_monthly_usage,
)


def _spend_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "started_at": "2026-06-30T10:15:00+08:00",
        "request_id": "req_chat_1",
        "user_id": "u_market_1",
        "key_alias": "market-campaign-key",
        "model": "gemini-2.5-flash",
        "endpoint": "/v1/chat/completions",
        "status": "success",
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
        "image_count": 0,
        "spend": "0.000024",
        "currency": "CNY",
        "pricing_version": "m1-2026-06",
        "metadata": {
            "department_id": "dept_market",
            "project_id": "proj_launch",
            "cost_center_id": "cc_growth",
        },
    }
    row.update(overrides)
    return row


def test_daily_usage_aggregates_chat_and_image_rows() -> None:
    rows = [
        _spend_row(),
        _spend_row(
            request_id="req_image_1",
            model="ycapi-image-1",
            endpoint="/v1/images/generations",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            image_count=2,
            spend="0.02",
        ),
    ]

    daily_rows = aggregate_daily_usage(rows)

    assert len(daily_rows) == 2
    chat_row = next(row for row in daily_rows if row["model"] == "gemini-2.5-flash")
    assert chat_row == {
        "date": "2026-06-30",
        "department_id": "dept_market",
        "project_id": "proj_launch",
        "cost_center_id": "cc_growth",
        "user_id": "u_market_1",
        "key_alias": "market-campaign-key",
        "model": "gemini-2.5-flash",
        "endpoint": "/v1/chat/completions",
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
        "image_count": 0,
        "spend": Decimal("0.000024"),
        "currency": "CNY",
        "pricing_version": "m1-2026-06",
        "request_count": 1,
        "successful_requests": 1,
        "failed_requests": 0,
    }
    image_row = next(row for row in daily_rows if row["model"] == "ycapi-image-1")
    assert image_row["image_count"] == 2
    assert image_row["spend"] == Decimal("0.02")


def test_missing_ownership_metadata_is_mapped_to_unassigned() -> None:
    daily_rows = aggregate_daily_usage(
        [
            _spend_row(
                request_id="req_unassigned",
                user_id=None,
                key_alias="",
                metadata={},
            )
        ]
    )

    row = daily_rows[0]
    assert row["department_id"] == "unassigned"
    assert row["project_id"] == "unassigned"
    assert row["cost_center_id"] == "unassigned"
    assert row["user_id"] == "unassigned"
    assert row["key_alias"] == "unassigned"


def test_monthly_usage_groups_finance_dimensions() -> None:
    rows = [
        _spend_row(total_tokens=100, prompt_tokens=80, completion_tokens=20, spend="1.25"),
        _spend_row(
            request_id="req_chat_2",
            started_at="2026-06-30T19:20:00+08:00",
            total_tokens=90,
            prompt_tokens=70,
            completion_tokens=20,
            spend="0.75",
        ),
    ]

    monthly_rows = aggregate_monthly_usage(rows)

    assert monthly_rows == [
        {
            "month": "2026-06",
            "department_id": "dept_market",
            "project_id": "proj_launch",
            "cost_center_id": "cc_growth",
            "user_id": "u_market_1",
            "key_alias": "market-campaign-key",
            "model": "gemini-2.5-flash",
            "endpoint": "/v1/chat/completions",
            "prompt_tokens": 150,
            "completion_tokens": 40,
            "total_tokens": 190,
            "image_count": 0,
            "spend": Decimal("2.00"),
            "currency": "CNY",
            "pricing_version": "m1-2026-06",
            "request_count": 2,
            "successful_requests": 2,
            "failed_requests": 0,
        }
    ]


def test_reconcile_monthly_usage_flags_material_differences() -> None:
    aimanager_rows = [
        {
            "month": "2026-06",
            "model": "gemini-2.5-flash",
            "endpoint": "/v1/chat/completions",
            "spend": Decimal("1000.00"),
            "currency": "CNY",
        },
        {
            "month": "2026-06",
            "model": "ycapi-image-1",
            "endpoint": "/v1/images/generations",
            "spend": Decimal("1000.00"),
            "currency": "CNY",
        },
    ]
    ycapi_rows = [
        {
            "month": "2026-06",
            "model": "gemini-2.5-flash",
            "endpoint": "/v1/chat/completions",
            "spend": Decimal("1009.00"),
            "currency": "CNY",
        },
        {
            "month": "2026-06",
            "model": "ycapi-image-1",
            "endpoint": "/v1/images/generations",
            "spend": Decimal("1012.00"),
            "currency": "CNY",
        },
    ]

    rows = reconcile_monthly_usage(aimanager_rows, ycapi_rows)

    chat_row = next(row for row in rows if row["model"] == "gemini-2.5-flash")
    assert chat_row["difference"] == Decimal("9.00")
    assert chat_row["status"] == "matched"
    image_row = next(row for row in rows if row["model"] == "ycapi-image-1")
    assert image_row["difference"] == Decimal("12.00")
    assert image_row["status"] == "needs_review"


def test_failed_requests_are_counted_and_paid_spend_is_not_lost() -> None:
    daily_rows = aggregate_daily_usage(
        [
            _spend_row(
                request_id="req_failed_paid",
                status="failed",
                total_tokens=20,
                prompt_tokens=20,
                completion_tokens=0,
                spend="0.01",
            )
        ]
    )

    row = daily_rows[0]
    assert row["request_count"] == 1
    assert row["successful_requests"] == 0
    assert row["failed_requests"] == 1
    assert row["spend"] == Decimal("0.01")


def test_litellm_spend_log_metadata_is_normalized_for_finance_dimensions() -> None:
    record = normalize_spend_record(
        {
            "startTime": "2026-06-30T02:15:00Z",
            "request_id": "req_litellm_1",
            "call_type": "completion",
            "user": "u_market_1",
            "model": "openai/gemini-2.5-flash",
            "prompt_tokens": 150,
            "completion_tokens": 40,
            "total_tokens": 190,
            "spend": "2.00",
            "currency": "cny",
            "status": "success",
            "metadata": {
                "user_api_key_alias": "market-campaign-key",
                "user_api_key_metadata": {
                    "department_id": "dept_market",
                    "project_id": "proj_launch",
                    "cost_center_id": "cc_growth",
                    "pricing_version": "m1-2026-06",
                },
                "spend_logs_metadata": {
                    "scenario_l1": "marketing",
                    "scenario_l2": "campaign-copy",
                    "image_count": 1,
                },
            },
        }
    )

    assert record["date"] == "2026-06-30"
    assert record["month"] == "2026-06"
    assert record["department_id"] == "dept_market"
    assert record["project_id"] == "proj_launch"
    assert record["cost_center_id"] == "cc_growth"
    assert record["user_id"] == "u_market_1"
    assert record["key_alias"] == "market-campaign-key"
    assert record["model"] == "gemini-2.5-flash"
    assert record["endpoint"] == "/v1/chat/completions"
    assert record["image_count"] == 1
    assert record["currency"] == "CNY"
    assert record["pricing_version"] == "m1-2026-06"


def test_ycapi_bill_rows_are_normalized_for_monthly_reconciliation() -> None:
    row = normalize_ycapi_bill_record(
        {
            "billing_month": "2026-06",
            "model_name": "openai/gemini-2.5-flash",
            "api_path": "/v1/chat/completions",
            "input_tokens": "150",
            "output_tokens": "40",
            "tokens": "190",
            "image_count": "0",
            "amount": "2.10",
            "currency": "cny",
        }
    )

    assert row == {
        "month": "2026-06",
        "model": "gemini-2.5-flash",
        "endpoint": "/v1/chat/completions",
        "prompt_tokens": 150,
        "completion_tokens": 40,
        "total_tokens": 190,
        "image_count": 0,
        "spend": Decimal("2.10"),
        "currency": "CNY",
    }


def test_finance_export_bundle_contains_usage_monthly_and_reconciliation_rows() -> None:
    bundle = build_finance_export_bundle(
        spend_rows=[
            _spend_row(
                started_at="2026-06-30T10:15:00+08:00",
                model="openai/gemini-2.5-flash",
                prompt_tokens=150,
                completion_tokens=40,
                total_tokens=190,
                spend="2.00",
            )
        ],
        ycapi_bill_rows=[
            {
                "billing_month": "2026-06",
                "model_name": "gemini-2.5-flash",
                "api_path": "/v1/chat/completions",
                "prompt_tokens": 150,
                "completion_tokens": 40,
                "total_tokens": 190,
                "amount": "2.10",
                "currency": "CNY",
            }
        ],
    )

    assert set(bundle) == {
        "aimanager_usage_daily.csv",
        "aimanager_finance_monthly.csv",
        "aimanager_reconciliation.csv",
    }
    usage_row = bundle["aimanager_usage_daily.csv"][0]
    assert usage_row["date"] == "2026-06-30"
    assert usage_row["department_id"] == "dept_market"
    assert usage_row["project_id"] == "proj_launch"
    assert usage_row["key_alias"] == "market-campaign-key"
    assert usage_row["model"] == "gemini-2.5-flash"
    assert usage_row["total_tokens"] == 190
    assert usage_row["spend"] == Decimal("2.00")

    monthly_row = bundle["aimanager_finance_monthly.csv"][0]
    assert monthly_row["month"] == "2026-06"
    assert monthly_row["cost_center_id"] == "cc_growth"
    assert monthly_row["close_status"] == "ready"

    reconciliation_row = bundle["aimanager_reconciliation.csv"][0]
    assert reconciliation_row["model"] == "gemini-2.5-flash"
    assert reconciliation_row["prompt_tokens"] == 150
    assert reconciliation_row["completion_tokens"] == 40
    assert reconciliation_row["total_tokens"] == 190
    assert reconciliation_row["image_count"] == 0
    assert reconciliation_row["aimanager_amount"] == Decimal("2.00")
    assert reconciliation_row["ycapi_amount"] == Decimal("2.10")
    assert reconciliation_row["difference"] == Decimal("0.10")
    assert reconciliation_row["currency"] == "CNY"
    assert reconciliation_row["status"] == "matched"


def test_reconcile_monthly_usage_escalates_systematic_underbill_below_per_row_floor() -> None:
    aimanager_rows = [
        {
            "month": "2026-06",
            "model": f"ycapi-model-{index}",
            "endpoint": f"/v1/chat/completions/{index}",
            "spend": Decimal("100.00"),
            "currency": "CNY",
        }
        for index in range(12)
    ]
    ycapi_rows = [
        {
            "month": "2026-06",
            "model": f"ycapi-model-{index}",
            "endpoint": f"/v1/chat/completions/{index}",
            "spend": Decimal("91.00"),
            "currency": "CNY",
        }
        for index in range(12)
    ]

    rows = reconcile_monthly_usage(aimanager_rows, ycapi_rows)

    assert len(rows) == 12
    assert all(abs(row["difference"]) == Decimal("9.00") for row in rows)
    assert all(row["status"] == "needs_review" for row in rows)

from __future__ import annotations

from decimal import Decimal

from aimanager.finance import (
    aggregate_daily_usage,
    aggregate_monthly_usage,
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

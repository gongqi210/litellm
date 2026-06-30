from __future__ import annotations

import json

from aimanager.redaction import build_secret_redactions, contains_secret_like, sanitize_value


def test_sanitize_value_redacts_common_secret_shapes_and_secret_env_values() -> None:
    redactions = build_secret_redactions(
        {
            "YCAPI_API_TOKEN": "ycapi-env-secret-value",
            "AIMANAGER_WECOM_WEBHOOK_URL": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=env-wecom-secret",
        }
    )
    payload = {
        "detail": (
            "Bearer should-not-leak sk-secret-value "
            "postgresql://aimanager:db-secret@example/db "
            "mysql://finance:mysql-secret@example/fin "
            "mariadb://finance:maria-secret@example/fin "
            "mongodb+srv://audit:mongo-secret@example/audit "
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=query-wecom-secret "
            "ycapi-env-secret-value"
        ),
        "nested": ["redis://cache:redis-secret@example:6379/0"],
    }

    sanitized = sanitize_value(payload, redactions)
    serialized = json.dumps(sanitized, ensure_ascii=False)

    assert "should-not-leak" not in serialized
    assert "sk-secret-value" not in serialized
    assert "db-secret" not in serialized
    assert "mysql-secret" not in serialized
    assert "maria-secret" not in serialized
    assert "mongo-secret" not in serialized
    assert "redis-secret" not in serialized
    assert "query-wecom-secret" not in serialized
    assert "ycapi-env-secret-value" not in serialized
    assert "[redacted:YCAPI_API_TOKEN]" in serialized
    assert "[redacted:AIMANAGER_WECOM_WEBHOOK_URL]" in serialized
    assert "[redacted:secret-like-value]" in serialized


def test_build_secret_redactions_ignores_short_common_env_values() -> None:
    redactions = build_secret_redactions(
        {
            "AIMANAGER_SECRET_FLAG": "true",
            "AIMANAGER_API_KEY": "  long-secret-value  ",
        }
    )

    sanitized = sanitize_value(
        {
            "detail": "true positive status; long-secret-value must not leak",
            "ok": True,
        },
        redactions,
    )
    serialized = json.dumps(sanitized, ensure_ascii=False)

    assert "true positive status" in serialized
    assert "long-secret-value" not in serialized
    assert "[redacted:AIMANAGER_API_KEY]" in serialized


def test_build_secret_redactions_ignores_short_non_common_env_values() -> None:
    redactions = build_secret_redactions({"AIMANAGER_ADMIN_UI_PASSWORD": "abc1234"})

    sanitized = sanitize_value("abc1234 is below the env redaction length boundary", redactions)

    assert sanitized == "abc1234 is below the env redaction length boundary"


def test_contains_secret_like_flags_structured_secrets_without_false_substrings() -> None:
    assert contains_secret_like("Bearer should-not-leak")
    assert contains_secret_like("sk-abc")
    assert contains_secret_like("mongodb://audit:mongo-secret@example/audit")
    assert contains_secret_like(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=query-wecom-secret"
    )

    assert not contains_secret_like("risk-control task-id myBearerLabel")

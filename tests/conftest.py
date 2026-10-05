from __future__ import annotations

import os
import pathlib
import tempfile

import pytest

TMP = pathlib.Path(tempfile.mkdtemp(prefix="asa-test-"))

# The suite deletes rows between tests, so it must NEVER be able to reach a real
# database. These are hard overrides, not defaults: an ambient DB_PATH from a deploy
# shell or a CI secret cannot leak in. Verified by test_suite_cannot_touch_a_real_db.
os.environ.update(
    {
        "APP_ENV": "development",
        "APP_DEBUG": "0",
        "SECRET_KEY": "test-secret-key-not-a-real-one-0123456789",
        "DB_PATH": str(TMP / "test.db"),
        "BACKUP_DIR": str(TMP / "backups"),
        "NOTIFY_PROVIDER": "file",
        "NOTIFY_FILE": str(TMP / "outbox.jsonl"),
        "ALERT_FILE": str(TMP / "alerts.log"),
        "NOTIFY_WEBHOOK": "",
        "ALERT_WEBHOOK": "",
        "AI_ENABLED": "0",
        "AI_API_KEY": "",
        "STATIC_DIR": str(TMP / "public"),
        "SMS_PROVIDER": "file",
        "SMS_FILE": str(TMP / "sms.txt"),
        "SMS_WEBHOOK": "",
        "SMS_API_KEY": "",
        "SMS_INBOUND_SECRET": "",
        "UPLOAD_DIR": str(TMP / "uploads"),
        "RL_REGISTER_IP_PER_DAY": "1000",
        "RL_PATIENT_LOGIN_IP_PER_15MIN": "1000",
        "RL_APPT_PATIENT_PER_DAY": "1000",
        "RL_IP_PER_MIN": "10000",
        "RL_BOOKING_IP_PER_HOUR": "1000",
        "RL_BOOKING_PHONE_PER_DAY": "1000",
    }
)


@pytest.fixture(scope="session", autouse=True)
def _db():
    from api import db

    db.migrate()
    yield
    db.close_conn()


@pytest.fixture(autouse=True)
def _clean():
    from api import db

    conn = db.get_conn()
    for t in (
        "outcome_events",
        "deliveries",
        "bookings",
        "idempotency_keys",
        "rate_events",
        "cost_events",
        "sessions",
        "sms_inbound",
        "sms_messages",
        "appointments",
        "patient_sessions",
        "patients",
        "audit_logs",
    ):
        conn.execute(f"DELETE FROM {t}")
    conn.execute("UPDATE breaker_state SET open_until = 0, reason = ''")
    from api import cache

    cache.purge()
    yield


@pytest.fixture(autouse=True)
def _settings_identity():
    """Two tests reload `api.config` on purpose (to prove production refuses a fake
    provider). A reload rebinds `api.config.settings` to a NEW object, while every other
    module still holds the old one — so a later `monkeypatch.setattr(settings, ...)`
    would silently patch an object nobody reads. Restore the original identity."""
    import api.config as cfg

    original = cfg.settings
    yield
    cfg.settings = original


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from api.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def owner_client(client):
    from api import auth, repo
    from api.policies import SYSTEM

    repo.delete(SYSTEM, "admin_users", where="username = ?", params=["t_owner"])
    auth.create_user("t_owner", "a-very-long-test-password", role="owner")
    r = client.post(
        "/api/admin/login", json={"username": "t_owner", "password": "a-very-long-test-password"}
    )
    assert r.status_code == 200, r.text
    client.headers["X-CSRF-Token"] = r.json()["csrf"]
    return client


def booking_payload(**kw):
    base = {
        "name": "زهرا محمدی",
        "phone": "09121234567",
        "service": "لیزر پرتوان",
        "note": "درد زانو",
    }
    base.update(kw)
    return base

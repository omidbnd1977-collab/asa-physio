"""Operational SMS allowlist — admin + patient kinds; mock HTTP only (no real SMS.ir)."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from api import repo, sms
from api.config import settings
from api.jalali import now_tehran
from api.policies import SYSTEM

_OPS_ALLOW = "admin_appointment,booked,confirm_request,cancelled,rescheduled"


def _verify_ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={"status": 1, "message": "ok", "data": {"messageId": 42, "cost": 1.0}},
        request=httpx.Request("POST", "https://api.sms.ir/v1/send/verify"),
    )


def _insert_patient_phone(phone: str = "09121112233") -> int:
    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return repo.insert(
        SYSTEM,
        "patients",
        {
            "public_id": f"patopsms{phone[-4:]}",
            "full_name": "بیمار تست",
            "phone": phone,
            "national_id": f"49{phone[-8:]}",
            "birth_date": "1991-07-25",
            "birth_jalali": "1370/05/03",
            "created_at": now,
            "updated_at": now,
        },
    )


def _insert_booked_appt(patient_id: int) -> int:
    day = (now_tehran().date() + dt.timedelta(days=2)).isoformat()
    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return repo.insert(
        SYSTEM,
        "appointments",
        {
            "public_id": "OP12CD34EF",
            "patient_id": patient_id,
            "slot_date": day,
            "slot_time": "16:00",
            "cabin": 1,
            "status": "booked",
            "kind": "first",
            "attendance": "unknown",
            "created_at": now,
            "updated_at": now,
        },
    )


class TestOperationalAllowlistGates:
    def test_fail_closed_empty_allowlist(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_IR_ALLOWED_KINDS", "")
        assert sms.sms_ir_allowed_kinds() == frozenset()
        for kind in sms.SMSIR_HARD_ALLOWED_KINDS:
            assert sms.kind_may_use_smsir(kind) is False

    def test_custom_never_in_hard_ceiling(self):
        assert "custom" not in sms.SMSIR_HARD_ALLOWED_KINDS
        assert "registered" not in sms.SMSIR_HARD_ALLOWED_KINDS
        assert "welcome_back" not in sms.SMSIR_HARD_ALLOWED_KINDS

    def test_custom_rejected_even_when_env_lists_it(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        monkeypatch.setattr(settings, "SMS_IR_ALLOWED_KINDS", f"{_OPS_ALLOW},custom")
        calls: list[str] = []
        monkeypatch.setattr(sms.httpx, "post", lambda *a, **k: calls.append("hit") or _verify_ok())
        with pytest.raises(RuntimeError, match="not permitted"):
            sms.send_smsir("09121112233", "promo", kind="custom")
        assert calls == []


class TestOperationalPatientQueueMock:
    @pytest.mark.parametrize("kind", sorted(sms.PATIENT_SMS_KINDS))
    def test_queue_deliver_patient_kind_when_allowlisted(self, monkeypatch, kind: str):
        monkeypatch.setattr(settings, "SMS_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "SMS_IR_ALLOWED_KINDS", _OPS_ALLOW)
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        calls: list[dict] = []

        def fake_post(url, **kwargs):
            calls.append({"url": url, "json": kwargs.get("json")})
            return _verify_ok()

        monkeypatch.setattr(sms.httpx, "post", fake_post)
        pid = _insert_patient_phone()
        aid = _insert_booked_appt(pid)
        mid, ok = sms.send_now(
            "09121112233",
            "body",
            kind,
            patient_id=pid,
            appointment_id=aid,
        )
        assert ok and mid
        assert len(calls) == 1
        assert calls[0]["url"] == "https://api.sms.ir/v1/send/verify"
        assert calls[0]["json"]["templateId"] == sms.VERIFY_TEMPLATES[kind]
        row = repo.select(SYSTEM, "sms_messages", where="id = ?", params=[mid], limit=1)[0]
        assert row["provider"] == "smsir"

    def test_admin_appointment_still_rejects_patient_phone(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "SMS_IR_ALLOWED_KINDS", _OPS_ALLOW)
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        monkeypatch.setattr(settings, "ADMIN_NOTIFICATION_PHONE", "09127776655")
        calls: list[str] = []
        monkeypatch.setattr(sms.httpx, "post", lambda *a, **k: calls.append("hit") or _verify_ok())
        pid = _insert_patient_phone("09121112233")
        aid = _insert_booked_appt(pid)
        mid, ok = sms.send_now(
            "09121112233",
            "admin body",
            "admin_appointment",
            patient_id=pid,
            appointment_id=aid,
        )
        assert ok is False
        assert calls == []
        row = repo.select(SYSTEM, "sms_messages", where="id = ?", params=[mid], limit=1)[0]
        assert row["status"] == "failed"


class TestReminderCancelReschedulePaths:
    def test_confirm_request_uses_verify_when_allowlisted(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "SMS_IR_ALLOWED_KINDS", _OPS_ALLOW)
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        calls: list[str] = []

        def fake_post(url, **kwargs):
            calls.append(url)
            return _verify_ok()

        monkeypatch.setattr(sms.httpx, "post", fake_post)
        pid = _insert_patient_phone("09123334455")
        aid = _insert_booked_appt(pid)
        mid, ok = sms.send_now(
            "09123334455",
            sms.body_confirm("بیمار تست", "1404-08-20 17:00"),
            "confirm_request",
            patient_id=pid,
            appointment_id=aid,
        )
        assert ok and mid
        assert calls == ["https://api.sms.ir/v1/send/verify"]

    def test_cancel_and_reschedule_kinds_when_allowlisted(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "SMS_IR_ALLOWED_KINDS", _OPS_ALLOW)
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        calls: list[str] = []

        def fake_post(url, **kwargs):
            calls.append(url)
            return _verify_ok()

        monkeypatch.setattr(sms.httpx, "post", fake_post)
        pid = _insert_patient_phone("09124445566")
        aid = _insert_booked_appt(pid)
        for kind in ("cancelled", "rescheduled"):
            calls.clear()
            mid, ok = sms.send_now(
                "09124445566",
                "status body",
                kind,
                patient_id=pid,
                appointment_id=aid,
            )
            assert ok and mid
            assert calls == ["https://api.sms.ir/v1/send/verify"]


class TestWebhookAndFileSinkRegression:
    def test_webhook_provider_still_posts_local_webhook(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_PROVIDER", "webhook")
        monkeypatch.setattr(settings, "SMS_WEBHOOK", "https://hooks.example.test/sms")
        monkeypatch.setattr(settings, "SMS_IR_ALLOWED_KINDS", _OPS_ALLOW)
        monkeypatch.setattr(settings, "SMS_API_KEY", "")
        captured: dict = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            return httpx.Response(200, request=httpx.Request("POST", url))

        monkeypatch.setattr(sms.httpx, "post", fake_post)
        mid, ok = sms.send_now("09124445566", "webhook body", "booked")
        assert ok and mid
        assert captured["url"] == "https://hooks.example.test/sms"
        assert "sms.ir" not in captured["url"]

    def test_file_provider_ignores_allowlist(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "SMS_PROVIDER", "file")
        monkeypatch.setattr(settings, "SMS_IR_ALLOWED_KINDS", _OPS_ALLOW)
        out = tmp_path / "sms.txt"
        monkeypatch.setattr(settings, "SMS_FILE", out)
        calls: list[str] = []
        monkeypatch.setattr(sms.httpx, "post", lambda *a, **k: calls.append("hit") or _verify_ok())
        mid, ok = sms.send_now("09124445566", "file body", "booked")
        assert ok and mid
        assert calls == []
        assert out.is_file()

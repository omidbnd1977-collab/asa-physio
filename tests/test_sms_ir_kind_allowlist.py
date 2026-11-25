"""Server-side SMS.ir kind allowlist — mock HTTP only, never hits SMS.ir."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from api import repo, sms
from api.config import settings
from api.jalali import now_tehran
from api.policies import SYSTEM


def _verify_ok(message_id: int = 91000001) -> httpx.Response:
    return httpx.Response(
        200,
        json={"status": 1, "message": "ok", "data": {"messageId": message_id, "cost": 1.0}},
        request=httpx.Request("POST", "https://api.sms.ir/v1/send/verify"),
    )


def _insert_patient_phone(phone: str = "09121112233") -> int:
    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    # patients.public_id CHECK length = 12
    return repo.insert(
        SYSTEM,
        "patients",
        {
            "public_id": f"patallow{phone[-4:]}",
            "full_name": "بیمار تست",
            "phone": phone,
            "national_id": f"49{phone[-8:]}",
            "birth_date": "1991-07-25",
            "birth_jalali": "1370/05/03",
            "sms_service_consent": 1,
            "created_at": now,
            "updated_at": now,
        },
    )


def _insert_booked_appt(patient_id: int) -> int:
    day = (now_tehran().date() + dt.timedelta(days=2)).isoformat()
    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    # appointments.public_id CHECK length = 10
    return repo.insert(
        SYSTEM,
        "appointments",
        {
            "public_id": "AB12CD34EF",
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


class TestSmsIrKindAllowlist:
    def test_empty_allowlist_blocks_all_smsir_even_when_provider_smsir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "SMS_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "SMS_IR_ALLOWED_KINDS", "")
        monkeypatch.setattr(settings, "SMS_API_KEY", "test-key-not-real")
        monkeypatch.setattr(settings, "SMS_SENDER", "3000000000")
        out = tmp_path / "sms-out.txt"
        monkeypatch.setattr(settings, "SMS_FILE", out)

        calls: list[str] = []

        def fake_post(url, **kwargs):
            calls.append(url)
            return _verify_ok()

        monkeypatch.setattr(sms.httpx, "post", fake_post)

        pid = _insert_patient_phone()
        aid = _insert_booked_appt(pid)
        mid, ok = sms.send_now(
            "09129990000",
            "admin body",
            "admin_appointment",
            appointment_id=aid,
        )
        assert ok is False and mid  # file-sink only — not a live SMS.ir delivery
        assert calls == []  # no HTTP to SMS.ir
        assert out.is_file() and out.read_text(encoding="utf-8")
        row = repo.select(SYSTEM, "sms_messages", where="id = ?", params=[mid], limit=1)[0]
        assert row["status"] == "logged"
        assert row["provider"] == "file"
        assert "not allowlisted" in row["last_error"]

    def test_admin_appointment_allowlisted_uses_verify_template_753761(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "SMS_IR_ALLOWED_KINDS", "admin_appointment")
        monkeypatch.setattr(settings, "ADMIN_NOTIFICATION_PHONE", "09127776655")
        monkeypatch.setattr(settings, "SMS_API_KEY", "test-key-not-real")
        monkeypatch.setattr(settings, "SMS_SENDER", "3000000000")

        captured: dict = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            # Never assert on API key value beyond presence of header name.
            assert "X-API-KEY" in (kwargs.get("headers") or {})
            return _verify_ok(77001)

        monkeypatch.setattr(sms.httpx, "post", fake_post)

        pid = _insert_patient_phone("09123334455")
        aid = _insert_booked_appt(pid)
        # Queue as admin alert would: phone is manager number, kind admin_appointment
        mid, ok = sms.send_now(
            "09127776655",
            "نوبت جدید",
            "admin_appointment",
            appointment_id=aid,
            patient_id=None,
        )
        assert ok is True
        assert captured["url"] == "https://api.sms.ir/v1/send/verify"
        assert captured["json"]["templateId"] == 753761
        assert captured["json"]["mobile"] == "09127776655"
        row = repo.select(SYSTEM, "sms_messages", where="id = ?", params=[mid], limit=1)[0]
        assert row["provider"] == "smsir"
        # No raw phone in a way that dumps key — fingerprint-only logging is elsewhere.

    def test_admin_appointment_rejects_non_admin_destination(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_IR_ALLOWED_KINDS", "admin_appointment")
        monkeypatch.setattr(settings, "ADMIN_NOTIFICATION_PHONE", "09127776655")
        with pytest.raises(RuntimeError, match="not ADMIN_NOTIFICATION_PHONE"):
            sms.send_smsir_for_message(
                {
                    "kind": "admin_appointment",
                    "phone": "09121112233",
                    "body": "x",
                    "patient_id": None,
                    "appointment_id": None,
                }
            )

    def test_admin_appointment_rejects_when_admin_equals_patient(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_IR_ALLOWED_KINDS", "admin_appointment")
        monkeypatch.setattr(settings, "ADMIN_NOTIFICATION_PHONE", "09123334455")
        pid = _insert_patient_phone("09123334455")
        aid = _insert_booked_appt(pid)
        with pytest.raises(RuntimeError, match="must not equal the patient phone"):
            sms.send_smsir_for_message(
                {
                    "kind": "admin_appointment",
                    "phone": "09123334455",
                    "body": "x",
                    "patient_id": None,
                    "appointment_id": aid,
                }
            )

    @pytest.mark.parametrize("kind", sorted(sms.PATIENT_SMS_KINDS))
    def test_patient_kinds_blocked_when_only_admin_allowlisted(
        self, monkeypatch, tmp_path, kind: str
    ):
        monkeypatch.setattr(settings, "SMS_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "SMS_IR_ALLOWED_KINDS", "admin_appointment")
        monkeypatch.setattr(settings, "SMS_API_KEY", "test-key-not-real")
        monkeypatch.setattr(settings, "SMS_SENDER", "3000000000")
        out = tmp_path / "sms-out.txt"
        monkeypatch.setattr(settings, "SMS_FILE", out)

        calls: list[str] = []

        def fake_post(url, **kwargs):
            calls.append(url)
            return _verify_ok()

        monkeypatch.setattr(sms.httpx, "post", fake_post)

        pid = _insert_patient_phone("09124445566")
        aid = _insert_booked_appt(pid)
        mid, ok = sms.send_now(
            "09124445566",
            "patient sms body",
            kind,
            patient_id=pid,
            appointment_id=aid,
        )
        assert ok is False and mid  # local sink only — not live SMS.ir
        assert calls == [], f"{kind} must not call SMS.ir"
        assert out.is_file()
        row = repo.select(SYSTEM, "sms_messages", where="id = ?", params=[mid], limit=1)[0]
        assert row["provider"] == "file"
        assert row["status"] == "logged"
        assert row["kind"] == kind
        assert "not allowlisted" in row["last_error"]

    @pytest.mark.parametrize("kind", sorted(sms.PATIENT_SMS_KINDS))
    def test_patient_kinds_use_smsir_when_allowlisted(self, monkeypatch, tmp_path, kind: str):
        """Operational patient kinds hit Verify when explicitly allowlisted (HTTP mocked)."""
        monkeypatch.setattr(settings, "SMS_PROVIDER", "smsir")
        monkeypatch.setattr(
            settings,
            "SMS_IR_ALLOWED_KINDS",
            "admin_appointment,booked,confirm_request,cancelled,rescheduled",
        )
        monkeypatch.setattr(settings, "SMS_API_KEY", "test-key-not-real")
        monkeypatch.setattr(settings, "SMS_SENDER", "3000000000")
        out = tmp_path / "sms-out.txt"
        monkeypatch.setattr(settings, "SMS_FILE", out)

        calls: list[str] = []

        def fake_post(url, **kwargs):
            calls.append(url)
            return _verify_ok()

        monkeypatch.setattr(sms.httpx, "post", fake_post)

        assert kind in sms.sms_ir_allowed_kinds()
        assert sms.kind_may_use_smsir(kind) is True

        pid = _insert_patient_phone("09125556677")
        aid = _insert_booked_appt(pid)
        mid, ok = sms.send_now(
            "09125556677",
            "patient sms body",
            kind,
            patient_id=pid,
            appointment_id=aid,
        )
        assert ok is True and mid
        assert calls == ["https://api.sms.ir/v1/send/verify"]
        row = repo.select(SYSTEM, "sms_messages", where="id = ?", params=[mid], limit=1)[0]
        assert row["provider"] == "smsir"
        assert row["kind"] == kind

    def test_env_listing_only_patient_kinds_enables_those_kinds(self, monkeypatch):
        monkeypatch.setattr(
            settings,
            "SMS_IR_ALLOWED_KINDS",
            "booked,confirm_request,cancelled,rescheduled",
        )
        assert sms.sms_ir_allowed_kinds() == sms.PATIENT_SMS_KINDS
        assert sms.kind_may_use_smsir("admin_appointment") is False
        for kind in sms.PATIENT_SMS_KINDS:
            assert sms.kind_may_use_smsir(kind) is True

    def test_non_hard_allowed_kind_blocked_even_if_listed_in_env(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_IR_ALLOWED_KINDS", "admin_appointment,custom,registered")
        assert sms.sms_ir_allowed_kinds() == frozenset({"admin_appointment"})
        assert sms.kind_may_use_smsir("custom") is False
        assert sms.kind_may_use_smsir("registered") is False
        assert sms.kind_may_use_smsir("admin_appointment") is True

    def test_send_smsir_for_message_rejects_custom_despite_env(self, monkeypatch):
        monkeypatch.setattr(
            settings,
            "SMS_IR_ALLOWED_KINDS",
            "admin_appointment,booked,custom",
        )
        with pytest.raises(RuntimeError, match="not allowlisted|not permitted"):
            sms.send_smsir_for_message(
                {
                    "kind": "custom",
                    "phone": "09121112233",
                    "body": "promo",
                    "patient_id": None,
                    "appointment_id": None,
                }
            )

    def test_send_smsir_for_message_rejects_non_allowlisted_kind(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_IR_ALLOWED_KINDS", "admin_appointment")
        with pytest.raises(RuntimeError, match="not allowlisted"):
            sms.send_smsir_for_message(
                {
                    "kind": "booked",
                    "phone": "09121112233",
                    "body": "x",
                    "patient_id": None,
                    "appointment_id": None,
                }
            )

"""Central SMS.ir send guards — mock HTTP only; never hits SMS.ir (N1–N6)."""

from __future__ import annotations

import httpx
import pytest

from api import sms
from api.config import settings


def _verify_ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={"status": 1, "message": "ok", "data": {"messageId": 1, "cost": 1.0}},
        request=httpx.Request("POST", "https://api.sms.ir/v1/send/verify"),
    )


def _bulk_ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "status": 1,
            "message": "ok",
            "data": {"messageIds": [1], "cost": 1.0},
        },
        request=httpx.Request("POST", "https://api.sms.ir/v1/send/bulk"),
    )


class TestSmsIrCentralSendGuards:
    """N1–N6: direct bypasses of hard-deny must not reach SMS.ir HTTP."""

    @pytest.mark.parametrize("kind", sorted(sms.PATIENT_SMS_KINDS))
    def test_n1_send_smsir_rejects_patient_kinds_when_not_allowlisted(self, monkeypatch, kind: str):
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        monkeypatch.setattr(settings, "SMS_IR_ALLOWED_KINDS", "admin_appointment")
        calls: list[str] = []

        def fake_post(url, **kwargs):
            calls.append(url)
            return _bulk_ok()

        monkeypatch.setattr(sms.httpx, "post", fake_post)
        with pytest.raises(RuntimeError, match="not allowlisted|not permitted"):
            sms.send_smsir("09121112233", "patient body", kind=kind)
        assert calls == []

    @pytest.mark.parametrize("kind", sorted(sms.PATIENT_SMS_KINDS))
    def test_n2_send_smsir_verify_rejects_patient_kinds_when_not_allowlisted(
        self, monkeypatch, kind: str
    ):
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_IR_ALLOWED_KINDS", "admin_appointment")
        calls: list[str] = []

        def fake_post(url, **kwargs):
            calls.append(url)
            return _verify_ok()

        monkeypatch.setattr(sms.httpx, "post", fake_post)
        with pytest.raises(RuntimeError, match="not allowlisted|not permitted"):
            sms.send_smsir_verify(
                "09121112233",
                371089,
                [{"name": "FULLNAME", "value": "تست"}],
                kind=kind,
            )
        assert calls == []

    def test_n3_cancelled_rescheduled_blocked_without_allowlist(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        monkeypatch.setattr(settings, "SMS_IR_ALLOWED_KINDS", "admin_appointment")
        calls: list[str] = []
        monkeypatch.setattr(sms.httpx, "post", lambda *a, **k: calls.append(k) or _bulk_ok())
        for kind in ("cancelled", "rescheduled"):
            with pytest.raises(RuntimeError, match="not allowlisted"):
                sms.send_smsir("09121112233", "x", kind=kind)
        assert calls == []

    def test_n4_send_now_patient_kind_stays_file_when_not_allowlisted(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "SMS_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        monkeypatch.setattr(settings, "SMS_IR_ALLOWED_KINDS", "admin_appointment")
        out = tmp_path / "sms-out.txt"
        monkeypatch.setattr(settings, "SMS_FILE", out)
        calls: list[str] = []

        def fake_post(url, **kwargs):
            calls.append(url)
            return _verify_ok()

        monkeypatch.setattr(sms.httpx, "post", fake_post)
        mid, ok = sms.send_now("09124445566", "booked body", "booked")
        assert ok and mid
        assert calls == []
        from api import repo
        from api.policies import SYSTEM

        row = repo.select(SYSTEM, "sms_messages", where="id = ?", params=[mid], limit=1)[0]
        assert row["provider"] == "file"

    def test_n5_send_smsir_for_message_patient_kind_rejected_without_allowlist(self, monkeypatch):
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

    def test_n6_dispatch_requires_kind_and_blocks_non_allowlisted_patient(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        monkeypatch.setattr(settings, "SMS_IR_ALLOWED_KINDS", "admin_appointment")
        calls: list[str] = []
        monkeypatch.setattr(sms.httpx, "post", lambda *a, **k: calls.append("hit") or _bulk_ok())
        with pytest.raises(TypeError):
            sms.dispatch("09121112233", "x")  # type: ignore[call-arg]
        with pytest.raises(RuntimeError, match="not allowlisted"):
            sms.dispatch("09121112233", "x", kind="booked")
        assert calls == []

    def test_providers_smsir_entry_cannot_send_unguarded(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        calls: list[str] = []
        monkeypatch.setattr(sms.httpx, "post", lambda *a, **k: calls.append("hit") or _bulk_ok())
        with pytest.raises(RuntimeError, match="PROVIDERS entry"):
            sms.PROVIDERS["smsir"]("09121112233", "x")
        assert calls == []

    def test_send_smsir_requires_kind_keyword(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        with pytest.raises(TypeError):
            sms.send_smsir("09121112233", "x")  # type: ignore[call-arg]

    def test_notify_booking_admin_rejects_arbitrary_destination(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        monkeypatch.setattr(settings, "ADMIN_NOTIFICATION_PHONE", "09127776655")
        calls: list[str] = []
        monkeypatch.setattr(sms.httpx, "post", lambda *a, **k: calls.append("hit") or _bulk_ok())
        with pytest.raises(RuntimeError, match="not ADMIN_NOTIFICATION_PHONE"):
            sms.send_smsir(
                "09121112233",
                "admin alert",
                kind=sms.NOTIFY_BOOKING_ADMIN_KIND,
            )
        assert calls == []

    def test_notify_booking_admin_rejects_empty_admin_phone(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        monkeypatch.setattr(settings, "ADMIN_NOTIFICATION_PHONE", "")
        calls: list[str] = []
        monkeypatch.setattr(sms.httpx, "post", lambda *a, **k: calls.append("hit") or _bulk_ok())
        with pytest.raises(RuntimeError, match="ADMIN_NOTIFICATION_PHONE is empty"):
            sms.send_smsir(
                "09127776655",
                "admin alert",
                kind=sms.NOTIFY_BOOKING_ADMIN_KIND,
            )
        assert calls == []

    def test_notify_booking_admin_allows_admin_destination(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        monkeypatch.setattr(settings, "ADMIN_NOTIFICATION_PHONE", "+98 912 777 6655")
        captured: dict = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            return _bulk_ok()

        monkeypatch.setattr(sms.httpx, "post", fake_post)
        sms.send_smsir(
            "09127776655",
            "admin alert",
            kind=sms.NOTIFY_BOOKING_ADMIN_KIND,
        )
        assert captured["url"] == "https://api.sms.ir/v1/send/bulk"
        assert captured["json"]["mobiles"] == ["09127776655"]

    def test_dispatch_cannot_bypass_notify_booking_admin_destination(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        monkeypatch.setattr(settings, "ADMIN_NOTIFICATION_PHONE", "09127776655")
        calls: list[str] = []
        monkeypatch.setattr(sms.httpx, "post", lambda *a, **k: calls.append("hit") or _bulk_ok())
        with pytest.raises(RuntimeError, match="not ADMIN_NOTIFICATION_PHONE"):
            sms.dispatch(
                "09121112233",
                "admin alert",
                kind=sms.NOTIFY_BOOKING_ADMIN_KIND,
            )
        assert calls == []

    def test_sms_webhook_provider_unaffected_by_smsir_guards(self, monkeypatch):
        """Regression: SMS_PROVIDER=webhook still posts to SMS_WEBHOOK, not SMS.ir."""
        monkeypatch.setattr(settings, "SMS_PROVIDER", "webhook")
        monkeypatch.setattr(settings, "SMS_WEBHOOK", "https://hooks.example.test/sms")
        monkeypatch.setattr(settings, "SMS_API_KEY", "")
        monkeypatch.setattr(settings, "ADMIN_NOTIFICATION_PHONE", "")
        monkeypatch.setattr(settings, "SMS_IR_ALLOWED_KINDS", "")
        captured: dict = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            return httpx.Response(200, request=httpx.Request("POST", url))

        monkeypatch.setattr(sms.httpx, "post", fake_post)
        mid, ok = sms.send_now("09124445566", "webhook body", "booked")
        assert ok and mid
        assert captured["url"] == "https://hooks.example.test/sms"
        assert captured["json"] == {"to": "09124445566", "text": "webhook body"}
        from api import repo
        from api.policies import SYSTEM

        row = repo.select(SYSTEM, "sms_messages", where="id = ?", params=[mid], limit=1)[0]
        assert row["provider"] == "webhook"
        assert row["status"] == "sent"

    def test_verify_notify_booking_admin_rejects_wrong_destination(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "ADMIN_NOTIFICATION_PHONE", "09127776655")
        calls: list[str] = []
        monkeypatch.setattr(sms.httpx, "post", lambda *a, **k: calls.append("hit") or _verify_ok())
        with pytest.raises(RuntimeError, match="not ADMIN_NOTIFICATION_PHONE"):
            sms.send_smsir_verify(
                "09121112233",
                753761,
                [{"name": "FULLNAME", "value": "تست"}],
                kind=sms.NOTIFY_BOOKING_ADMIN_KIND,
            )
        assert calls == []

    def test_verify_rejects_empty_or_invalid_kind(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "ADMIN_NOTIFICATION_PHONE", "09127776655")
        calls: list[str] = []
        monkeypatch.setattr(sms.httpx, "post", lambda *a, **k: calls.append("hit") or _verify_ok())
        with pytest.raises(TypeError):
            sms.send_smsir_verify(  # type: ignore[call-arg]
                "09127776655",
                753761,
                [{"name": "FULLNAME", "value": "تست"}],
            )
        with pytest.raises(RuntimeError, match="requires kind="):
            sms.send_smsir_verify(
                "09127776655",
                753761,
                [{"name": "FULLNAME", "value": "تست"}],
                kind="",
            )
        with pytest.raises(RuntimeError, match="not permitted"):
            sms.send_smsir_verify(
                "09127776655",
                753761,
                [{"name": "FULLNAME", "value": "تست"}],
                kind="not_a_real_kind",
            )
        assert calls == []

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
    def test_n1_send_smsir_rejects_patient_kinds(self, monkeypatch, kind: str):
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        monkeypatch.setattr(
            settings,
            "SMS_IR_ALLOWED_KINDS",
            "admin_appointment,booked,confirm_request,cancelled,rescheduled",
        )
        calls: list[str] = []

        def fake_post(url, **kwargs):
            calls.append(url)
            return _bulk_ok()

        monkeypatch.setattr(sms.httpx, "post", fake_post)
        with pytest.raises(RuntimeError, match="hard-denied|not permitted|not allowlisted"):
            sms.send_smsir("09121112233", "patient body", kind=kind)
        assert calls == []

    @pytest.mark.parametrize("kind", sorted(sms.PATIENT_SMS_KINDS))
    def test_n2_send_smsir_verify_rejects_patient_kinds(self, monkeypatch, kind: str):
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(
            settings,
            "SMS_IR_ALLOWED_KINDS",
            "admin_appointment,booked,confirm_request,cancelled,rescheduled",
        )
        calls: list[str] = []

        def fake_post(url, **kwargs):
            calls.append(url)
            return _verify_ok()

        monkeypatch.setattr(sms.httpx, "post", fake_post)
        with pytest.raises(RuntimeError, match="hard-denied|not permitted|not allowlisted"):
            sms.send_smsir_verify(
                "09121112233",
                371089,
                [{"name": "FULLNAME", "value": "تست"}],
                kind=kind,
            )
        assert calls == []

    def test_n3_send_smsir_rejects_cancelled_and_rescheduled_explicitly(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        monkeypatch.setattr(
            settings, "SMS_IR_ALLOWED_KINDS", "cancelled,rescheduled,admin_appointment"
        )
        calls: list[str] = []
        monkeypatch.setattr(sms.httpx, "post", lambda *a, **k: calls.append(k) or _bulk_ok())
        for kind in ("cancelled", "rescheduled"):
            with pytest.raises(RuntimeError, match="hard-denied"):
                sms.send_smsir("09121112233", "x", kind=kind)
        assert calls == []

    def test_n4_send_now_patient_kind_stays_file_despite_env(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "SMS_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        monkeypatch.setattr(
            settings,
            "SMS_IR_ALLOWED_KINDS",
            "admin_appointment,booked,confirm_request,cancelled,rescheduled",
        )
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

    def test_n5_send_smsir_for_message_patient_kind_rejected(self, monkeypatch):
        monkeypatch.setattr(
            settings,
            "SMS_IR_ALLOWED_KINDS",
            "admin_appointment,booked,confirm_request,cancelled,rescheduled",
        )
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

    def test_n6_dispatch_requires_kind_and_blocks_patient(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        monkeypatch.setattr(settings, "SMS_IR_ALLOWED_KINDS", "admin_appointment,booked")
        calls: list[str] = []
        monkeypatch.setattr(sms.httpx, "post", lambda *a, **k: calls.append("hit") or _bulk_ok())
        with pytest.raises(TypeError):
            sms.dispatch("09121112233", "x")  # type: ignore[call-arg]
        with pytest.raises(RuntimeError, match="hard-denied"):
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

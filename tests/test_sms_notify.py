"""SMS.ir patient delivery + clinic admin alerts — all HTTP mocked, no real SMS."""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import httpx
import pytest

from api import notify, repo, sms
from api.config import settings
from api.policies import SYSTEM


def _smsir_ok(message_id: int = 86522023) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "status": 1,
            "message": "موفق",
            "data": {
                "packId": "2b99e63c-9bf8-4a21-9bfe-3f72dc1b46f1",
                "messageIds": [message_id],
                "cost": 1.0,
            },
        },
        request=httpx.Request("POST", "https://api.sms.ir/v1/send/bulk"),
    )


class TestNormalizeMobile:
    def test_plus98_and_spaces(self):
        assert sms.normalize_mobile("+98 912 345 6789") == "09123456789"

    def test_0098_prefix(self):
        assert sms.normalize_mobile("00989123456789") == "09123456789"


class TestSmsIrProvider:
    def test_bulk_payload_matches_official_api(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "30004505000017")
        captured: dict = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers")
            captured["json"] = kwargs.get("json")
            return _smsir_ok()

        monkeypatch.setattr(sms.httpx, "post", fake_post)
        sms.send_smsir("+989121111111", "سلام آزمون")

        assert captured["url"] == "https://api.sms.ir/v1/send/bulk"
        assert captured["headers"]["X-API-KEY"] == "panel-key"
        assert captured["headers"]["Accept"] == "application/json"
        assert captured["json"]["lineNumber"] == 30004505000017
        assert captured["json"]["messageText"] == "سلام آزمون"
        assert captured["json"]["mobiles"] == ["09121111111"]

    def test_rejects_logical_failure_status(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")

        def fake_post(url, **kwargs):
            return httpx.Response(
                200,
                json={"status": 10, "message": "خطای منطقی", "data": None},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(sms.httpx, "post", fake_post)
        with pytest.raises(RuntimeError, match="sms.ir status 10"):
            sms.send_smsir("09121111111", "x")

    def test_rejects_blacklist_message_id_zero(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")

        def fake_post(url, **kwargs):
            return _smsir_ok(message_id=0)

        monkeypatch.setattr(sms.httpx, "post", fake_post)
        with pytest.raises(RuntimeError, match="blacklist"):
            sms.send_smsir("09121111111", "x")

    def test_queue_deliver_marks_sent(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        monkeypatch.setattr(sms.httpx, "post", lambda *a, **k: _smsir_ok())

        mid, ok = sms.send_now("09123334444", "بدن تست", "booked")
        assert ok is True
        row = repo.select(SYSTEM, "sms_messages", where="id = ?", params=[mid], limit=1)[0]
        assert row["status"] == "sent"
        assert row["provider"] == "smsir"

    def test_deliver_failure_is_retryable(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")

        def boom(*a, **k):
            raise httpx.HTTPStatusError(
                "401",
                request=httpx.Request("POST", "https://api.sms.ir/v1/send/bulk"),
                response=httpx.Response(401),
            )

        monkeypatch.setattr(sms.httpx, "post", boom)
        mid = sms.queue("09123334444", "بدن", "confirm_request")
        assert sms.deliver(mid, attempts=1) is False
        row = repo.select(SYSTEM, "sms_messages", where="id = ?", params=[mid], limit=1)[0]
        assert row["status"] == "failed"
        assert row["attempts"] >= 1

        monkeypatch.setattr(sms.httpx, "post", lambda *a, **k: _smsir_ok())
        assert sms.deliver(mid, attempts=1) is True
        row = repo.select(SYSTEM, "sms_messages", where="id = ?", params=[mid], limit=1)[0]
        assert row["status"] == "sent"


class TestAdminNotifySmsIr:
    def _booking(self) -> dict:
        bid = repo.insert(
            SYSTEM,
            "bookings",
            {
                "public_id": "bkadmin00001",
                "name": "بیمار تست",
                "phone": "09120001122",
                "service": "consult",
                "note": "نیاز به تماس",
                "status": "new",
            },
        )
        return repo.select(SYSTEM, "bookings", where="id = ?", params=[bid], limit=1)[0]

    def test_smsir_notify_goes_only_to_admin_phone(self, monkeypatch):
        monkeypatch.setattr(settings, "NOTIFY_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "ADMIN_NOTIFICATION_PHONE", "09129998877")
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        seen: list[str] = []

        def fake_post(url, **kwargs):
            seen.extend(kwargs["json"]["mobiles"])
            return _smsir_ok()

        monkeypatch.setattr(sms.httpx, "post", fake_post)
        booking = self._booking()
        assert notify.deliver(booking, attempts=1) is True
        assert seen == ["09129998877"]
        assert "09120001122" not in seen

        # idempotent — second deliver does not resend
        seen.clear()
        assert notify.deliver(booking, attempts=1) is True
        assert seen == []

    def test_smsir_notify_refuses_admin_phone_equal_patient(self, monkeypatch):
        monkeypatch.setattr(settings, "NOTIFY_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "ADMIN_NOTIFICATION_PHONE", "09120001122")
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        monkeypatch.setattr(sms.httpx, "post", MagicMock(side_effect=AssertionError("no HTTP")))
        booking = self._booking()
        assert notify.deliver(booking, attempts=1) is False
        d = repo.select(
            SYSTEM,
            "deliveries",
            where="booking_id = ? AND provider = ?",
            params=[booking["id"], "smsir"],
            limit=1,
        )[0]
        assert d["status"] == "failed"

    def test_failed_delivery_can_be_retried(self, monkeypatch):
        monkeypatch.setattr(settings, "NOTIFY_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "ADMIN_NOTIFICATION_PHONE", "09129998877")
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")

        calls = {"n": 0}

        def flaky(url, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("down")
            return _smsir_ok()

        monkeypatch.setattr(sms.httpx, "post", flaky)
        booking = self._booking()
        assert notify.deliver(booking, attempts=1) is False
        assert notify.deliver(booking, attempts=1) is True
        d = repo.select(
            SYSTEM,
            "deliveries",
            where="booking_id = ? AND provider = ?",
            params=[booking["id"], "smsir"],
            limit=1,
        )[0]
        assert d["status"] == "delivered"

    def test_alert_new_appointment_queues_admin_sms_only(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "ADMIN_NOTIFICATION_PHONE", "09127776655")
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        monkeypatch.setattr(sms.httpx, "post", lambda *a, **k: _smsir_ok())

        mid, ok = notify.alert_new_appointment(
            name="بیمار",
            phone="09121112233",
            when="شنبه ۱۲:۰۰",
            code="appt000001",
            appointment_id=None,
        )
        assert ok is True and mid is not None
        row = repo.select(SYSTEM, "sms_messages", where="id = ?", params=[mid], limit=1)[0]
        assert row["kind"] == "admin_appointment"
        assert row["phone"] == "09127776655"
        assert row["status"] == "sent"
        assert "بیمار" in row["body"]

    def test_alert_skips_when_admin_phone_missing(self, monkeypatch):
        monkeypatch.setattr(settings, "ADMIN_NOTIFICATION_PHONE", "")
        mid, ok = notify.alert_new_appointment(
            name="بیمار", phone="09121112233", when="x", code="c"
        )
        assert mid is None and ok is False

    def test_alert_dedupes_same_appointment_id(self, monkeypatch):
        from api import scheduling
        from tests.test_portal import nid, tomorrow

        pid = repo.insert(
            SYSTEM,
            "patients",
            {
                "public_id": "patdedupe001",
                "full_name": "بیمار تست",
                "national_id": nid(48837089),
                "birth_date": "1991-07-25",
                "birth_jalali": "1370/05/03",
                "phone": "09121112233",
            },
        )
        appt = scheduling.allocate(pid, tomorrow(), "19:00")
        monkeypatch.setattr(settings, "SMS_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "ADMIN_NOTIFICATION_PHONE", "09127776655")
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        calls = {"n": 0}

        def counting_post(*a, **k):
            calls["n"] += 1
            return _smsir_ok()

        monkeypatch.setattr(sms.httpx, "post", counting_post)
        mid1, ok1 = notify.alert_new_appointment(
            name="بیمار",
            phone="09121112233",
            when="شنبه",
            code=appt["public_id"],
            appointment_id=appt["id"],
        )
        mid2, ok2 = notify.alert_new_appointment(
            name="بیمار",
            phone="09121112233",
            when="شنبه",
            code=appt["public_id"],
            appointment_id=appt["id"],
        )
        assert ok1 and ok2 and mid1 == mid2
        assert calls["n"] == 1
        rows = repo.select(
            SYSTEM,
            "sms_messages",
            where="appointment_id = ? AND kind = 'admin_appointment'",
            params=[appt["id"]],
        )
        assert len(rows) == 1

    def test_alert_failure_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(settings, "SMS_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "ADMIN_NOTIFICATION_PHONE", "09127776655")
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")

        def boom(*a, **k):
            raise RuntimeError("gateway down")

        monkeypatch.setattr(sms, "send_now", boom)
        mid, ok = notify.alert_new_appointment(
            name="بیمار", phone="09121112233", when="x", code="c", appointment_id=None
        )
        assert mid is None and ok is False


class TestStaffFollowupAdminAlert:
    def test_staff_followup_sends_admin_sms(self, client, owner_client, monkeypatch):
        import io

        from tests.test_portal import PNG, nid, reg_form, tomorrow

        reg = client.post(
            "/api/portal/register",
            data=reg_form(national_id=nid(48837089), phone="۰۹۱۲۵۵۵۶۶۷۷"),
            files={"med_photo": ("m.png", io.BytesIO(PNG), "image/png")},
        )
        assert reg.status_code == 201, reg.text
        public_id = repo.select(SYSTEM, "patients")[0]["public_id"]

        monkeypatch.setattr(settings, "SMS_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "ADMIN_NOTIFICATION_PHONE", "09129990000")
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        seen: list[str] = []

        def fake_post(url, **kwargs):
            seen.extend(kwargs["json"]["mobiles"])
            return _smsir_ok()

        monkeypatch.setattr(sms.httpx, "post", fake_post)
        r = owner_client.post(
            f"/api/admin/patients/{public_id}/followup",
            json={
                "slot_date": tomorrow(),
                "slot_time": "19:00",
                "note": "جلسه دوم",
                "notify": False,
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["ok"] is True
        assert r.json()["sms_sent"] is False  # patient notify off
        admin_rows = repo.select(SYSTEM, "sms_messages", where="kind = 'admin_appointment'")
        assert len(admin_rows) == 1
        assert admin_rows[0]["phone"] == "09129990000"
        assert admin_rows[0]["status"] == "sent"
        assert seen == ["09129990000"]

    def test_staff_followup_survives_admin_sms_failure(self, client, owner_client, monkeypatch):
        import io

        from tests.test_portal import PNG, nid, reg_form, tomorrow

        reg = client.post(
            "/api/portal/register",
            data=reg_form(national_id=nid(47737089), phone="۰۹۱۲۳۳۳۴۴۵۵"),
            files={"med_photo": ("m.png", io.BytesIO(PNG), "image/png")},
        )
        assert reg.status_code == 201, reg.text
        public_id = repo.select(SYSTEM, "patients")[0]["public_id"]

        monkeypatch.setattr(settings, "SMS_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "ADMIN_NOTIFICATION_PHONE", "09129990000")
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")

        def boom(*a, **k):
            raise httpx.ConnectError("down")

        monkeypatch.setattr(sms.httpx, "post", boom)
        r = owner_client.post(
            f"/api/admin/patients/{public_id}/followup",
            json={"slot_date": tomorrow(), "slot_time": "17:30", "notify": False},
        )
        assert r.status_code == 201, r.text
        assert r.json()["ok"] is True
        appt = repo.select(SYSTEM, "appointments", where="kind = 'followup'")[0]
        assert appt["status"] == "booked"
        admin_rows = repo.select(
            SYSTEM,
            "sms_messages",
            where="appointment_id = ? AND kind = 'admin_appointment'",
            params=[appt["id"]],
        )
        assert len(admin_rows) == 1
        assert admin_rows[0]["status"] == "failed"

    def test_staff_followup_admin_sms_not_duplicated_on_replay(
        self, client, owner_client, monkeypatch
    ):
        import io

        from tests.test_portal import PNG, nid, reg_form, tomorrow

        reg = client.post(
            "/api/portal/register",
            data=reg_form(national_id=nid(46637089), phone="۰۹۱۲۴۴۴۵۵۶۶"),
            files={"med_photo": ("m.png", io.BytesIO(PNG), "image/png")},
        )
        assert reg.status_code == 201, reg.text
        public_id = repo.select(SYSTEM, "patients")[0]["public_id"]
        monkeypatch.setattr(settings, "SMS_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "ADMIN_NOTIFICATION_PHONE", "09129990000")
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        monkeypatch.setattr(sms.httpx, "post", lambda *a, **k: _smsir_ok())

        day = tomorrow()
        r = owner_client.post(
            f"/api/admin/patients/{public_id}/followup",
            json={"slot_date": day, "slot_time": "20:30", "notify": False},
        )
        assert r.status_code == 201, r.text
        appt_id = repo.select(SYSTEM, "appointments", where="kind = 'followup'")[0]["id"]
        notify.alert_new_appointment(
            name="x", phone="09121112233", when="y", code="z", appointment_id=appt_id
        )
        rows = repo.select(
            SYSTEM,
            "sms_messages",
            where="appointment_id = ? AND kind = 'admin_appointment'",
            params=[appt_id],
        )
        assert len(rows) == 1


class TestAdminAlertConcurrencyAndClaim:
    def _patient_appt(self):
        from api import scheduling
        from tests.test_portal import nid, tomorrow

        pid = repo.insert(
            SYSTEM,
            "patients",
            {
                "public_id": "patrace00001",
                "full_name": "بیمار هم‌زمان",
                "national_id": nid(45537089),
                "birth_date": "1991-07-25",
                "birth_jalali": "1370/05/03",
                "phone": "09121112233",
            },
        )
        return scheduling.allocate(pid, tomorrow(), "19:00")

    def test_concurrent_alerts_send_sms_once(self, monkeypatch):
        """Two threads race on the same appointment; SQLite allows only one row and one send."""
        import threading
        import time

        from api import db

        appt = self._patient_appt()
        monkeypatch.setattr(settings, "SMS_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "ADMIN_NOTIFICATION_PHONE", "09127776655")
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")

        lock = threading.Lock()
        calls = {"n": 0}

        def slow_post(*a, **k):
            with lock:
                calls["n"] += 1
            time.sleep(0.15)
            return _smsir_ok()

        monkeypatch.setattr(sms.httpx, "post", slow_post)
        barrier = threading.Barrier(2)
        results: list[tuple[int | None, bool]] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                db.close_conn()
                barrier.wait(timeout=5)
                results.append(
                    notify.alert_new_appointment(
                        name="بیمار",
                        phone="09121112233",
                        when="شنبه",
                        code=appt["public_id"],
                        appointment_id=appt["id"],
                    )
                )
            except BaseException as exc:  # noqa: BLE001 — capture for main thread
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, errors
        assert len(results) == 2
        assert calls["n"] == 1
        rows = repo.select(
            SYSTEM,
            "sms_messages",
            where="appointment_id = ? AND kind = 'admin_appointment'",
            params=[appt["id"]],
        )
        assert len(rows) == 1
        assert rows[0]["status"] == "sent"
        assert sum(1 for _mid, ok in results if ok) >= 1

    def test_second_claim_does_not_resend(self, monkeypatch):
        appt = self._patient_appt()
        monkeypatch.setattr(settings, "SMS_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "ADMIN_NOTIFICATION_PHONE", "09127776655")
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        monkeypatch.setattr(sms.httpx, "post", lambda *a, **k: _smsir_ok())

        mid = sms.ensure_admin_appointment("09127776655", "نوبت جدید", appt["id"])
        # Hold an active claim without finishing send.
        claimed = sms.claim_for_send(mid)
        assert claimed is not None and claimed["status"] == "sending"
        assert sms.deliver(mid) is False
        assert (
            repo.select(SYSTEM, "sms_messages", where="id = ?", params=[mid])[0]["status"]
            == "sending"
        )
        # Finish via reclaim after marking failed (simulates gateway path).
        repo.update(
            SYSTEM,
            "sms_messages",
            {"status": "failed", "updated_at": "2000-01-01T00:00:00Z"},
            where="id = ?",
            params=[mid],
        )
        assert sms.deliver(mid) is True
        assert (
            repo.select(SYSTEM, "sms_messages", where="id = ?", params=[mid])[0]["status"] == "sent"
        )

    def test_stale_sending_is_reclaimed_and_retried(self, monkeypatch):
        appt = self._patient_appt()
        monkeypatch.setattr(settings, "SMS_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "ADMIN_NOTIFICATION_PHONE", "09127776655")
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")
        calls = {"n": 0}

        def counting_post(*a, **k):
            calls["n"] += 1
            return _smsir_ok()

        monkeypatch.setattr(sms.httpx, "post", counting_post)
        mid = sms.ensure_admin_appointment("09127776655", "نوبت", appt["id"])
        repo.update(
            SYSTEM,
            "sms_messages",
            {
                "status": "sending",
                "updated_at": "2000-01-01T00:00:00Z",
                "attempts": 1,
                "last_error": "worker died",
            },
            where="id = ?",
            params=[mid],
        )
        out = sms.retry_failed(limit=5)
        assert out["retried"] == 1 and out["sent"] == 1
        assert calls["n"] == 1
        row = repo.select(SYSTEM, "sms_messages", where="id = ?", params=[mid])[0]
        assert row["status"] == "sent"

    def test_failed_alert_retries_same_row(self, monkeypatch):
        appt = self._patient_appt()
        monkeypatch.setattr(settings, "SMS_PROVIDER", "smsir")
        monkeypatch.setattr(settings, "ADMIN_NOTIFICATION_PHONE", "09127776655")
        monkeypatch.setattr(settings, "SMS_API_KEY", "panel-key")
        monkeypatch.setattr(settings, "SMS_SENDER", "300000000000")

        def boom(*a, **k):
            raise httpx.ConnectError("down")

        monkeypatch.setattr(sms.httpx, "post", boom)
        mid1, ok1 = notify.alert_new_appointment(
            name="بیمار",
            phone="09121112233",
            when="x",
            code=appt["public_id"],
            appointment_id=appt["id"],
        )
        assert ok1 is False and mid1 is not None
        assert (
            repo.select(SYSTEM, "sms_messages", where="id = ?", params=[mid1])[0]["status"]
            == "failed"
        )

        monkeypatch.setattr(sms.httpx, "post", lambda *a, **k: _smsir_ok())
        mid2, ok2 = notify.alert_new_appointment(
            name="بیمار",
            phone="09121112233",
            when="x",
            code=appt["public_id"],
            appointment_id=appt["id"],
        )
        assert ok2 is True and mid2 == mid1
        rows = repo.select(
            SYSTEM,
            "sms_messages",
            where="appointment_id = ? AND kind = 'admin_appointment'",
            params=[appt["id"]],
        )
        assert len(rows) == 1 and rows[0]["status"] == "sent"


class TestProductionConfigSmsIr:
    def test_smsir_notify_requires_admin_phone(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("SECRET_KEY", "x" * 40)
        monkeypatch.setenv("NOTIFY_PROVIDER", "smsir")
        monkeypatch.setenv("ADMIN_NOTIFICATION_PHONE", "")
        monkeypatch.setenv("SMS_PROVIDER", "smsir")
        monkeypatch.setenv("SMS_API_KEY", "k")
        monkeypatch.setenv("SMS_SENDER", "300000000000")
        import api.config as cfg

        with pytest.raises(RuntimeError, match="ADMIN_NOTIFICATION_PHONE"):
            importlib.reload(cfg)
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("NOTIFY_PROVIDER", "file")
        monkeypatch.setenv("SMS_PROVIDER", "file")
        importlib.reload(cfg)

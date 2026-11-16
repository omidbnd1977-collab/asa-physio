"""SMS.ir divert adapter + hardened inbound matching (Issue #27) — mock only."""

from __future__ import annotations

import datetime as dt
import io

import pytest


def nid(n: int) -> str:
    base = f"{n:09d}"
    s = sum(int(base[i]) * (10 - i) for i in range(9))
    r = s % 11
    return base + str(r if r < 2 else 11 - r)


NID = nid(49937089)
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
    b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def tomorrow() -> str:
    from api.jalali import now_tehran

    return (now_tehran().date() + dt.timedelta(days=1)).isoformat()


def day_after_tomorrow() -> str:
    from api.jalali import now_tehran

    return (now_tehran().date() + dt.timedelta(days=2)).isoformat()


@pytest.fixture()
def patient(client):
    from api import auth, repo
    from api.policies import SYSTEM

    owners = repo.select(SYSTEM, "admin_users", where="role = 'owner' AND is_active = 1", limit=2)
    if not owners:
        auth.create_user("t_owner", "a-very-long-test-password", role="owner", full_name="پزشک تست")
    r = client.post(
        "/api/portal/register",
        data={
            "full_name": "سارا کریمی",
            "national_id": NID,
            "birth_date": "۱۳۷۰/۰۵/۰۳",
            "phone": "۰۹۱۲۱۱۱۲۲۳۳",
            "sms_service_consent": "true",
            "mri_link": "https://drive.google.com/file/d/abc",
            "ortho_doctor": "دکتر رضایی",
        },
        files={"med_photo": ("m.png", io.BytesIO(PNG), "image/png")},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _book(client, *, slot_date: str | None = None, slot_time: str = "16:00") -> None:
    r = client.post(
        "/api/portal/appointments",
        json={"slot_date": slot_date or tomorrow(), "slot_time": slot_time},
    )
    assert r.status_code in (200, 201), r.text


def _mark_confirm_sent(*, slot_date: str | None = None, when: str = "2026-01-01T10:00:00Z") -> None:
    from api import repo
    from api.policies import SYSTEM

    where = "status = 'booked'"
    params: list = []
    if slot_date:
        where += " AND slot_date = ?"
        params.append(slot_date)
    repo.update(SYSTEM, "appointments", {"confirm_sent_at": when}, where=where, params=params)


class TestNormalizeMobileInbound:
    def test_912_becomes_09(self):
        from api.sms import normalize_mobile

        assert normalize_mobile("9121112233") == "09121112233"
        assert normalize_mobile("+989121112233") == "09121112233"
        assert normalize_mobile("09121112233") == "09121112233"


class TestHandleInboundHardening:
    def test_confirm_one_with_confirm_sent(self, client, patient):
        from api import reminders, repo
        from api.policies import SYSTEM

        _book(client)
        _mark_confirm_sent()
        out = reminders.handle_inbound("9121112233", "1", provider_msg_id="rr-100")
        assert out["handled"] == "confirmed" and out["duplicate"] is False
        row = repo.select(SYSTEM, "appointments")[0]
        assert row["attendance"] == "coming" and row["confirmed_at"]
        inbound = repo.select(SYSTEM, "sms_inbound")[0]
        assert inbound["provider_msg_id"] == "rr-100"

    def test_decline_two_with_confirm_sent(self, client, patient):
        from api import reminders, repo
        from api.policies import SYSTEM

        _book(client)
        _mark_confirm_sent()
        out = reminders.handle_inbound("09121112233", "2", provider_msg_id="rr-101")
        assert out["handled"] == "declined"
        assert repo.select(SYSTEM, "appointments")[0]["attendance"] == "not_coming"

    def test_without_confirm_sent_ignored(self, client, patient):
        from api import reminders, repo
        from api.policies import SYSTEM

        _book(client)
        out = reminders.handle_inbound("09121112233", "1")
        assert out["handled"] == "ignored"
        assert out["matched"] is False
        appt = repo.select(SYSTEM, "appointments")[0]
        assert appt["attendance"] in (None, "", "unknown")
        assert appt["confirmed_at"] in (None, "")

    def test_unrelated_body_ignored(self, client, patient):
        from api import reminders

        _book(client)
        _mark_confirm_sent()
        out = reminders.handle_inbound("09121112233", "سلام")
        assert out["handled"] == "ignored"

    def test_unknown_phone(self, client):
        from api import reminders, repo
        from api.policies import SYSTEM

        out = reminders.handle_inbound("09998887766", "1", provider_msg_id="rr-unk")
        assert out["reason"] == "unknown_phone"
        assert repo.select(SYSTEM, "sms_inbound")[0]["provider_msg_id"] == "rr-unk"

    def test_duplicate_provider_msg_id_does_not_flip_attendance(self, client, patient):
        from api import reminders, repo
        from api.policies import SYSTEM

        _book(client)
        _mark_confirm_sent()
        first = reminders.handle_inbound("09121112233", "1", provider_msg_id="rr-dup")
        assert first["handled"] == "confirmed" and first["duplicate"] is False
        # Change mind would be "2", but same ReceiveReturnId must be ignored.
        second = reminders.handle_inbound("09121112233", "2", provider_msg_id="rr-dup")
        assert second["duplicate"] is True
        assert second["handled"] == "confirmed"
        assert repo.select(SYSTEM, "appointments")[0]["attendance"] == "coming"
        assert len(repo.select(SYSTEM, "sms_inbound")) == 1

    def test_prefers_appointment_with_confirm_sent_not_nearest_without(self, client, patient):
        from api import reminders, repo
        from api.policies import SYSTEM

        # Nearer slot tomorrow 16:00 — no confirm_sent
        _book(client, slot_date=tomorrow(), slot_time="16:00")
        # Later slot day-after — confirm was sent
        _book(client, slot_date=day_after_tomorrow(), slot_time="16:00")
        later = repo.select(
            SYSTEM,
            "appointments",
            where="slot_date = ?",
            params=[day_after_tomorrow()],
            limit=1,
        )[0]
        repo.update(
            SYSTEM,
            "appointments",
            {"confirm_sent_at": "2026-01-02T12:00:00Z"},
            where="id = ?",
            params=[later["id"]],
        )

        out = reminders.handle_inbound("09121112233", "1", provider_msg_id="rr-match")
        assert out["handled"] == "confirmed"
        assert out["appointment"] == later["public_id"]
        near = repo.select(
            SYSTEM, "appointments", where="slot_date = ?", params=[tomorrow()], limit=1
        )[0]
        assert near["attendance"] in (None, "", "unknown")
        assert (
            repo.select(SYSTEM, "appointments", where="id = ?", params=[later["id"]])[0][
                "attendance"
            ]
            == "coming"
        )


def _divert_url(token: str = "s3cret") -> str:
    return f"/api/sms/inbound/smsir/{token}"


class TestSmsIrDivertEndpoint:
    def test_path_token_required(self, client, monkeypatch):
        from api.config import settings

        monkeypatch.setattr(settings, "SMS_INBOUND_SECRET", "s3cret")
        r = client.get(
            _divert_url("wrong-token"),
            params={"mobile": "9121112233", "messageText": "1", "receiveReturnId": "1"},
        )
        assert r.status_code == 403

    def test_query_secret_rejected(self, client, monkeypatch):
        from api.config import settings

        monkeypatch.setattr(settings, "SMS_INBOUND_SECRET", "s3cret")
        r = client.get(
            _divert_url("s3cret"),
            params={
                "mobile": "9121112233",
                "messageText": "1",
                "receiveReturnId": "1",
                "secret": "s3cret",
            },
        )
        assert r.status_code == 403
        assert "query" in r.json().get("message", "").lower() or r.status_code == 403

    def test_get_divert_confirm_with_path_token(self, client, patient, monkeypatch):
        from api import repo
        from api.config import settings
        from api.policies import SYSTEM

        monkeypatch.setattr(settings, "SMS_INBOUND_SECRET", "s3cret")
        _book(client)
        _mark_confirm_sent()
        r = client.get(
            _divert_url(),
            params={
                "mobile": "9121112233",
                "messageText": "1",
                "receiveReturnId": "555001",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["handled"] == "confirmed" and body["duplicate"] is False
        assert "s3cret" not in r.text
        assert repo.select(SYSTEM, "appointments")[0]["attendance"] == "coming"

    def test_get_divert_decline_with_header_secret(self, client, patient, monkeypatch):
        from api import repo
        from api.config import settings
        from api.policies import SYSTEM

        monkeypatch.setattr(settings, "SMS_INBOUND_SECRET", "s3cret")
        _book(client)
        _mark_confirm_sent()
        # Wrong path token rejected unless header matches.
        r = client.get(
            _divert_url("not-the-path-token"),
            params={
                "Mobile": "9121112233",
                "MessageText": "2",
                "ReceiveReturnId": "555002",
            },
            headers={"X-SMS-Secret": "s3cret"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["handled"] == "declined"
        assert repo.select(SYSTEM, "appointments")[0]["attendance"] == "not_coming"

    def test_divert_duplicate_receive_return_id(self, client, patient, monkeypatch):
        from api import repo
        from api.config import settings
        from api.policies import SYSTEM

        monkeypatch.setattr(settings, "SMS_INBOUND_SECRET", "s3cret")
        _book(client)
        _mark_confirm_sent()
        params = {
            "mobile": "9121112233",
            "messageText": "1",
            "receiveReturnId": "555003",
        }
        assert client.get(_divert_url(), params=params).json()["handled"] == "confirmed"
        again = client.get(
            _divert_url(),
            params={**params, "messageText": "2"},
        ).json()
        assert again["duplicate"] is True
        assert again["handled"] == "confirmed"
        assert repo.select(SYSTEM, "appointments")[0]["attendance"] == "coming"
        assert len(repo.select(SYSTEM, "sms_inbound")) == 1

    def test_divert_invalid_mobile_rejected(self, client, monkeypatch):
        from api.config import settings

        monkeypatch.setattr(settings, "SMS_INBOUND_SECRET", "s3cret")
        r = client.get(
            _divert_url(),
            params={
                "mobile": "12345",
                "messageText": "1",
                "receiveReturnId": "555004",
            },
        )
        assert r.status_code == 400

    def test_divert_unknown_registered_format_phone(self, client, monkeypatch):
        from api.config import settings

        monkeypatch.setattr(settings, "SMS_INBOUND_SECRET", "s3cret")
        r = client.get(
            _divert_url(),
            params={
                "mobile": "9301112233",
                "messageText": "1",
                "receiveReturnId": "555005",
            },
        )
        assert r.status_code == 200
        assert r.json()["reason"] == "unknown_phone"

    def test_divert_no_matching_confirm_appointment(self, client, patient, monkeypatch):
        from api.config import settings

        monkeypatch.setattr(settings, "SMS_INBOUND_SECRET", "s3cret")
        _book(client)  # no confirm_sent_at
        r = client.get(
            _divert_url(),
            params={
                "mobile": "9121112233",
                "messageText": "1",
                "receiveReturnId": "555006",
            },
        )
        assert r.status_code == 200
        assert r.json()["handled"] == "ignored"

    def test_json_inbound_accepts_provider_msg_id(self, client, patient, monkeypatch):
        from api import repo
        from api.config import settings
        from api.policies import SYSTEM

        monkeypatch.setattr(settings, "SMS_INBOUND_SECRET", "s3cret")
        _book(client)
        _mark_confirm_sent()
        r = client.post(
            "/api/sms/inbound",
            json={"phone": "09121112233", "body": "1", "provider_msg_id": "json-1"},
            headers={"X-SMS-Secret": "s3cret"},
        )
        assert r.status_code == 200
        assert r.json()["handled"] == "confirmed"
        assert repo.select(SYSTEM, "sms_inbound")[0]["provider_msg_id"] == "json-1"


class TestInboundSecretFailClosed:
    """HTTP gate must refuse traffic when secret is missing, wrong, or absent."""

    def test_no_secret_header_rejected_when_configured(self, client, monkeypatch):
        from api.config import settings

        monkeypatch.setattr(settings, "SMS_INBOUND_SECRET", "s3cret")
        r = client.post("/api/sms/inbound", json={"phone": "09121112233", "body": "1"})
        assert r.status_code == 403

    def test_wrong_secret_header_rejected(self, client, monkeypatch):
        from api.config import settings

        monkeypatch.setattr(settings, "SMS_INBOUND_SECRET", "s3cret")
        r = client.post(
            "/api/sms/inbound",
            json={"phone": "09121112233", "body": "1"},
            headers={"X-SMS-Secret": "not-s3cret"},
        )
        assert r.status_code == 403

    def test_correct_secret_header_accepted(self, client, monkeypatch):
        from api.config import settings

        monkeypatch.setattr(settings, "SMS_INBOUND_SECRET", "s3cret")
        r = client.post(
            "/api/sms/inbound",
            json={"phone": "09121112233", "body": "1"},
            headers={"X-SMS-Secret": "s3cret"},
        )
        assert r.status_code == 200
        assert r.json().get("reason") == "unknown_phone" or "handled" in r.json()

    def test_empty_secret_config_rejects_even_with_header(self, client, monkeypatch):
        from api.config import settings

        monkeypatch.setattr(settings, "SMS_INBOUND_SECRET", "")
        r = client.post(
            "/api/sms/inbound",
            json={"phone": "09121112233", "body": "1"},
            headers={"X-SMS-Secret": "s3cret"},
        )
        assert r.status_code == 403

    def test_divert_fails_closed_when_secret_unset(self, client, monkeypatch):
        from api.config import settings

        monkeypatch.setattr(settings, "SMS_INBOUND_SECRET", "")
        r = client.get(
            _divert_url("any-token"),
            params={"mobile": "9121112233", "messageText": "1", "receiveReturnId": "1"},
        )
        assert r.status_code == 403


class TestInboundConcurrencyAndSecurity:
    def test_concurrent_same_receive_return_id_single_winner(self, client, patient):
        """Two races with same ReceiveReturnId: only one mutates attendance."""
        import threading

        from api import reminders, repo
        from api.policies import SYSTEM

        _book(client)
        _mark_confirm_sent()
        pmid = "rr-race-1"
        gate = threading.Barrier(2)
        out: list[dict] = []

        def worker(body: str) -> None:
            gate.wait(timeout=5)
            out.append(reminders.handle_inbound("09121112233", body, provider_msg_id=pmid))

        t1 = threading.Thread(target=worker, args=("1",))
        t2 = threading.Thread(target=worker, args=("2",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        assert len(out) == 2
        winners = [r for r in out if not r.get("duplicate")]
        dupes = [r for r in out if r.get("duplicate")]
        assert len(winners) == 1 and len(dupes) == 1
        assert winners[0]["handled"] in ("confirmed", "declined")
        assert dupes[0]["handled"] == winners[0]["handled"]
        assert len(repo.select(SYSTEM, "sms_inbound")) == 1
        attendance = repo.select(SYSTEM, "appointments")[0]["attendance"]
        if winners[0]["handled"] == "confirmed":
            assert attendance == "coming"
        else:
            assert attendance == "not_coming"

    def test_query_secret_alone_never_authorizes(self, client, monkeypatch):
        from api.config import settings

        monkeypatch.setattr(settings, "SMS_INBOUND_SECRET", "s3cret")
        # Legacy path without token + query secret must not authenticate.
        r = client.get(
            "/api/sms/inbound/smsir",
            params={
                "mobile": "9121112233",
                "messageText": "1",
                "receiveReturnId": "x",
                "secret": "s3cret",
            },
        )
        assert r.status_code in (403, 404, 405, 422)

    def test_response_body_never_echoes_path_token(self, client, patient, monkeypatch):
        from api.config import settings

        monkeypatch.setattr(settings, "SMS_INBOUND_SECRET", "super-secret-token-xyz")
        _book(client)
        _mark_confirm_sent()
        r = client.get(
            _divert_url("super-secret-token-xyz"),
            params={
                "mobile": "9121112233",
                "messageText": "1",
                "receiveReturnId": "sec-echo-1",
            },
        )
        assert r.status_code == 200
        assert "super-secret-token-xyz" not in r.text
        assert "s3cret" not in r.text

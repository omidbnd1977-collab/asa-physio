"""Doctor <-> patient messaging: the channel that ties the two panels together."""

from __future__ import annotations

import io

import pytest

from tests.test_portal import PNG, reg_form


@pytest.fixture()
def patient(client):
    r = client.post(
        "/api/portal/register",
        data=reg_form(),
        files={"med_photo": ("m.png", io.BytesIO(PNG), "image/png")},
    )
    assert r.status_code == 201, r.text
    return r.json()


def book_one(client) -> dict:
    from tests.test_portal import tomorrow

    r = client.post(
        "/api/portal/appointments",
        json={"slot_date": tomorrow(), "slot_time": "16:00", "note": ""},
    )
    assert r.status_code == 201, r.text
    return r.json()


class TestPatientToDoctor:
    def test_patient_message_lands_in_the_doctor_inbox(self, client, patient, owner_client):
        r = client.post("/api/portal/messages", json={"body": "زانویم هنوز درد می‌کند."})
        assert r.status_code == 201, r.text

        inbox = owner_client.get("/api/admin/messages")
        assert inbox.status_code == 200
        items = inbox.json()["items"]
        assert len(items) == 1
        assert items[0]["unread"] == 1
        assert "زانویم" in items[0]["last"]["body"]
        assert inbox.json()["unread"] == 1

    def test_opening_the_thread_marks_it_read(self, client, patient, owner_client):
        client.post("/api/portal/messages", json={"body": "سلام، سؤال داشتم."})
        pid = patient["patient"]["public_id"]
        r = owner_client.get(f"/api/admin/patients/{pid}/messages")
        assert r.status_code == 200
        assert r.json()["unread"] == 0
        assert owner_client.get("/api/admin/messages/unread").json()["unread"] == 0

    def test_empty_and_too_long_messages_are_refused(self, client, patient):
        assert client.post("/api/portal/messages", json={"body": ""}).status_code == 422
        assert client.post("/api/portal/messages", json={"body": "x" * 1201}).status_code == 422
        r = client.post("/api/portal/messages", json={"body": "<script>alert(1)</script>"})
        assert r.status_code == 422


class TestDoctorToPatient:
    def test_reply_reaches_the_patient_and_marks_read_on_open(self, client, patient, owner_client):
        pid = patient["patient"]["public_id"]
        r = owner_client.post(
            f"/api/admin/patients/{pid}/messages", json={"body": "فردا سر نوبت تشریف بیارید."}
        )
        assert r.status_code == 201, r.text
        assert r.json()["sms_sent"] is False

        me = client.get("/api/portal/me").json()
        assert me["unread_messages"] == 1

        thread = client.get("/api/portal/messages").json()["messages"]
        assert any("سر نوبت" in m["body"] and m["sender"] == "staff" for m in thread)
        # opened → read
        assert client.get("/api/portal/me").json()["unread_messages"] == 0

    def test_reply_can_also_go_by_sms(self, client, patient, owner_client):
        pid = patient["patient"]["public_id"]
        r = owner_client.post(
            f"/api/admin/patients/{pid}/messages",
            json={"body": "داروها را فراموش نکنید.", "notify": True},
        )
        assert r.status_code == 201
        assert r.json()["sms_sent"] is True
        from api import repo
        from api.policies import SYSTEM

        sms_rows = repo.select(SYSTEM, "sms_messages", where="kind = 'custom'")
        assert len(sms_rows) == 1
        assert "داروها" in sms_rows[0]["body"]

    def test_staff_reply_needs_csrf(self, client, patient, owner_client):
        pid = patient["patient"]["public_id"]
        client.headers.pop("X-CSRF-Token", None)
        r = client.post(f"/api/admin/patients/{pid}/messages", json={"body": "بدون توکن"})
        assert r.status_code == 403


class TestEventTrail:
    """Both panels watch the same thread: what happens to an appointment shows up
    as a note that each side can read."""

    def test_booking_and_reschedule_leave_notes_for_the_patient(
        self, client, patient, owner_client
    ):
        from tests.test_portal import tomorrow

        appt = book_one(client)
        r = owner_client.patch(
            f"/api/admin/appointments/{appt['code']}/move",
            json={"slot_date": tomorrow(), "slot_time": "17:00"},
        )
        assert r.status_code == 200, r.text
        thread = client.get("/api/portal/messages").json()["messages"]
        kinds = [m["kind"] for m in thread]
        assert "booked" in kinds and "rescheduled" in kinds
        moved = next(m for m in thread if m["kind"] == "rescheduled")
        assert "زمان جدید" in moved["body"]

    def test_patient_cancellation_lights_up_the_staff_inbox(self, client, patient, owner_client):
        appt = book_one(client)
        r = client.post(f"/api/portal/appointments/{appt['code']}/cancel")
        assert r.status_code == 200, r.text
        inbox = owner_client.get("/api/admin/messages").json()
        assert inbox["unread"] >= 1
        assert any("لغو شد" in it["last"]["body"] for it in inbox["items"])

    def test_recording_a_session_shares_the_plan_not_the_findings(
        self, client, patient, owner_client
    ):
        cat = owner_client.get("/api/admin/catalog").json()
        bp = cat["body_parts"][0]["items"][0]["id"]
        tx = cat["treatments"][0]["id"]
        pid = patient["patient"]["public_id"]
        r = owner_client.post(
            f"/api/admin/patients/{pid}/sessions",
            json={
                "body_part_ids": [bp],
                "treatment_ids": [tx],
                "findings": "یادداشت داخلی پزشک",
                "plan": "تمرین کشش روزانه",
            },
        )
        assert r.status_code == 201, r.text

        hist = client.get("/api/portal/history").json()["history"]
        assert len(hist) == 1
        assert hist[0]["plan"] == "تمرین کشش روزانه"
        assert "findings" not in hist[0]
        # and the thread tells the patient a summary was recorded
        thread = client.get("/api/portal/messages").json()["messages"]
        assert any(m["kind"] == "session" for m in thread)


class TestAccess:
    def test_anonymous_cannot_use_the_channel(self, client):
        assert client.get("/api/portal/messages").status_code == 401
        assert client.post("/api/portal/messages", json={"body": "سلام"}).status_code == 401
        assert client.get("/api/portal/history").status_code == 401
        assert client.get("/api/admin/messages").status_code == 401
        assert client.get("/api/admin/messages/unread").status_code == 401

    def test_a_patient_cannot_reach_the_staff_inbox(self, client, patient):
        assert client.get("/api/admin/messages").status_code in (401, 403)
        pid = patient["patient"]["public_id"]
        assert client.get(f"/api/admin/patients/{pid}/messages").status_code in (401, 403)

    def test_unknown_patient_thread_is_a_404(self, owner_client):
        assert owner_client.get("/api/admin/patients/zzzzzzzzzzzz/messages").status_code == 404

    def test_the_table_has_a_policy(self):
        from api import db
        from api.policies import missing_policies

        assert "messages" in db.tables()
        assert missing_policies(db.tables()) == []

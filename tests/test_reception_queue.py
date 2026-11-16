"""reception.queue — unassigned SMS inbound and consult-lead queues."""

from __future__ import annotations

import uuid

import pytest

from api import auth, repo
from api import permissions as perms
from api.policies import SYSTEM
from tests.conftest import booking_payload
from tests.test_owner_isolation import _login, _patient_for


def idem() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex}


@pytest.fixture()
def clinic(client):
    oid = repo.select(SYSTEM, "admin_users", where="username = ?", params=["t_owner"], limit=1)[0][
        "id"
    ]
    return {"owner_id": oid, "client": client}


def _sec(owner_id: int, username: str, caps: list[str]) -> int:
    uid = auth.create_user(
        username, "secretary-iso-12", role="staff", full_name=username, created_by=owner_id
    )
    auth.replace_permissions(uid, caps)
    return uid


class TestReceptionPermissionGrant:
    def test_only_owner_can_grant_reception_queue(self, client, clinic):
        oid = clinic["owner_id"]
        sec = _sec(oid, "sec_recv_grant", [perms.MESSAGES_VIEW])
        _login(client, "t_owner", "a-very-long-test-password")
        r = client.patch(
            f"/api/admin/secretaries/{sec}",
            json={"permissions": [perms.MESSAGES_VIEW, perms.RECEPTION_QUEUE]},
        )
        assert r.status_code == 200, r.text
        assert perms.RECEPTION_QUEUE in r.json()["permissions"]
        # Secretary cannot manage secretaries / self-escalate
        _login(client, "sec_recv_grant", "secretary-iso-12")
        assert client.get("/api/admin/secretaries").status_code == 403
        assert (
            client.patch(
                f"/api/admin/secretaries/{sec}",
                json={"permissions": list(perms.ALL)},
            ).status_code
            == 403
        )


class TestBookingQueue:
    def test_public_booking_is_unassigned(self, client):
        r = client.post("/api/bookings", json=booking_payload(), headers=idem())
        assert r.status_code in (201, 202), r.text
        pid = r.json()["public_id"]
        row = repo.select(SYSTEM, "bookings", where="public_id = ?", params=[pid], limit=1)[0]
        assert row["owner_id"] is None

    def test_secretary_without_reception_cannot_see_unassigned(self, client, clinic):
        oid = clinic["owner_id"]
        _sec(oid, "sec_no_recv", [perms.MESSAGES_VIEW])
        created = client.post(
            "/api/bookings", json=booking_payload(name="لید مخفی"), headers=idem()
        )
        pid = created.json()["public_id"]
        _login(client, "sec_no_recv", "secretary-iso-12")
        items = client.get("/api/admin/bookings").json()["items"]
        assert all(x["public_id"] != pid for x in items)
        assert client.post(f"/api/admin/bookings/{pid}/assign", json={}).status_code == 403

    def test_reception_secretary_sees_and_assigns_unassigned(self, client, clinic):
        oid = clinic["owner_id"]
        _sec(oid, "sec_recv", [perms.RECEPTION_QUEUE, perms.MESSAGES_VIEW])
        created = client.post(
            "/api/bookings", json=booking_payload(name="لید پذیرش"), headers=idem()
        )
        pid = created.json()["public_id"]
        _login(client, "sec_recv", "secretary-iso-12")
        items = client.get("/api/admin/bookings").json()["items"]
        assert any(x["public_id"] == pid for x in items)
        assigned = client.post(f"/api/admin/bookings/{pid}/assign", json={})
        assert assigned.status_code == 200, assigned.text
        assert assigned.json()["owner_id"] == oid
        row = repo.select(SYSTEM, "bookings", where="public_id = ?", params=[pid], limit=1)[0]
        assert row["owner_id"] == oid
        events = __import__("api.audit", fromlist=["list_for"]).list_for("booking", pid)
        assert any(e["action"] == "booking.owner_assigned" for e in events)

    def test_after_assign_messages_view_sees_lead(self, client, clinic):
        oid = clinic["owner_id"]
        _sec(oid, "sec_recv2", [perms.RECEPTION_QUEUE])
        _sec(oid, "sec_msg_only", [perms.MESSAGES_VIEW])
        created = client.post(
            "/api/bookings", json=booking_payload(name="بعد تخصیص"), headers=idem()
        )
        pid = created.json()["public_id"]
        _login(client, "sec_recv2", "secretary-iso-12")
        assert client.post(f"/api/admin/bookings/{pid}/assign", json={}).status_code == 200
        _login(client, "sec_msg_only", "secretary-iso-12")
        items = client.get("/api/admin/bookings").json()["items"]
        assert any(x["public_id"] == pid for x in items)


class TestInboundSmsQueue:
    def test_unknown_phone_lands_in_unassigned_queue(self, client):
        from api import reminders

        out = reminders.handle_inbound("09129998877", "1")
        assert out["matched"] is False
        rows = repo.select(
            SYSTEM, "sms_inbound", where="phone = ? AND owner_id IS NULL", params=["09129998877"]
        )
        assert rows

    def test_two_secretaries_inbound_visibility(self, client, clinic):
        from api import reminders

        oid = clinic["owner_id"]
        _sec(oid, "sec_in_recv", [perms.RECEPTION_QUEUE])
        _sec(oid, "sec_in_msg", [perms.MESSAGES_VIEW])
        reminders.handle_inbound("09121110011", "سلام")
        _login(client, "sec_in_recv", "secretary-iso-12")
        inbound = client.get("/api/admin/sms").json()["inbound"]
        assert any(r["phone"] == "09121110011" for r in inbound)
        _login(client, "sec_in_msg", "secretary-iso-12")
        inbound2 = client.get("/api/admin/sms").json()["inbound"]
        assert all(r["phone"] != "09121110011" or r.get("owner_id") for r in inbound2)
        # messages-only secretary must not see unassigned rows
        assert not any(r["phone"] == "09121110011" and r.get("owner_id") is None for r in inbound2)

    def test_assign_inbound_to_patient_audited(self, client, clinic):
        from api import audit, reminders

        oid = clinic["owner_id"]
        p = _patient_for(oid, 52037089)
        _sec(oid, "sec_in_assign", [perms.RECEPTION_QUEUE, perms.PATIENTS_VIEW])
        reminders.handle_inbound("09123334455", "1")
        row = repo.select(
            SYSTEM,
            "sms_inbound",
            where="phone = ? AND owner_id IS NULL",
            params=["09123334455"],
            limit=1,
        )[0]
        _login(client, "sec_in_assign", "secretary-iso-12")
        r = client.post(
            f"/api/admin/sms/inbound/{row['id']}/assign",
            json={"patient_public_id": p["public_id"]},
        )
        assert r.status_code == 200, r.text
        fresh = repo.select(SYSTEM, "sms_inbound", where="id = ?", params=[row["id"]], limit=1)[0]
        assert fresh["owner_id"] == oid
        assert fresh["patient_id"] == p["id"]
        events = audit.list_for("sms_inbound", row["id"])
        assert any(e["action"] == "sms_inbound.assigned" for e in events)

    def test_matched_inbound_gets_patient_owner(self, client, clinic):
        from api import reminders

        oid = clinic["owner_id"]
        p = _patient_for(oid, 52137089)
        reminders.handle_inbound(p["phone"], "بله")
        row = repo.select(SYSTEM, "sms_inbound", where="phone = ?", params=[p["phone"]], limit=1)[0]
        assert row["patient_id"] == p["id"]
        assert row["owner_id"] == oid

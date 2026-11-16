"""Multi-owner isolation, orphan assign, booked_by identity, IDOR guards."""

from __future__ import annotations

import io

import pytest

from api import audit, auth, patients, repo, scheduling
from api import permissions as perms
from api.policies import SYSTEM
from tests.test_portal import PNG, nid, tomorrow


def _login(client, username: str, password: str):
    client.cookies.clear()
    r = client.post("/api/admin/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    client.headers["X-CSRF-Token"] = r.json()["csrf"]
    return r.json()


def _make_owner(username: str, password: str = "owner-isolation-12") -> int:
    return auth.create_user(username, password, role="owner", full_name=username)


def _make_secretary(owner_id: int, username: str, password: str = "secretary-iso-12") -> int:
    uid = auth.create_user(
        username, password, role="staff", full_name=username, created_by=owner_id
    )
    auth.replace_permissions(
        uid,
        [
            perms.PATIENTS_VIEW,
            perms.PATIENTS_EDIT,
            perms.PATIENTS_CREATE,
            perms.APPOINTMENTS_VIEW,
            perms.APPOINTMENTS_CREATE,
            perms.APPOINTMENTS_MOVE,
            perms.APPOINTMENTS_CANCEL,
            perms.MESSAGES_VIEW,
        ],
    )
    return uid


def _patient_for(owner_id: int, n: int, name: str = "بیمار تست") -> dict:
    row = patients.register(
        {
            "full_name": name,
            "national_id": nid(n),
            "birth_date": "1991-07-25",
            "phone": f"0912{n % 10000000:07d}",
            "mri_link": "",
            "ortho_doctor": "",
        },
        med_photo="",
        ip="127.0.0.1",
        owner_id=owner_id,
    )
    return dict(row)


@pytest.fixture()
def two_clinics(client):
    """Two independent owners, each with a secretary and one owned patient."""
    a_id = _make_owner("iso_owner_a")
    b_id = _make_owner("iso_owner_b")
    sec_a = _make_secretary(a_id, "iso_sec_a")
    sec_b = _make_secretary(b_id, "iso_sec_b")
    pa = _patient_for(a_id, 50137089, "بیمار الف")
    pb = _patient_for(b_id, 50237089, "بیمار ب")
    return {
        "a": {"owner_id": a_id, "sec_id": sec_a, "patient": pa},
        "b": {"owner_id": b_id, "sec_id": sec_b, "patient": pb},
        "client": client,
    }


class TestOwnerIsolationIDOR:
    def test_owner_cannot_list_other_owners_patients(self, two_clinics):
        c = two_clinics["client"]
        _login(c, "iso_owner_a", "owner-isolation-12")
        items = c.get("/api/admin/patients").json()["items"]
        ids = {p["public_id"] for p in items}
        assert two_clinics["a"]["patient"]["public_id"] in ids
        assert two_clinics["b"]["patient"]["public_id"] not in ids

    def test_owner_cannot_read_other_patient_messages_mri_sessions(self, two_clinics):
        c = two_clinics["client"]
        _login(c, "iso_owner_a", "owner-isolation-12")
        other = two_clinics["b"]["patient"]["public_id"]
        assert c.get(f"/api/admin/patients/{other}/messages").status_code == 404
        assert c.get(f"/api/admin/patients/{other}/mri").status_code == 404
        assert c.get(f"/api/admin/patients/{other}/sessions").status_code == 404
        assert c.get(f"/api/admin/patients/{other}/photo").status_code == 404
        assert (
            c.patch(f"/api/admin/patients/{other}/note", json={"staff_note": "x"}).status_code
            == 404
        )

    def test_secretary_scoped_to_owning_doctor(self, two_clinics):
        c = two_clinics["client"]
        _login(c, "iso_sec_a", "secretary-iso-12")
        items = c.get("/api/admin/patients").json()["items"]
        ids = {p["public_id"] for p in items}
        assert two_clinics["a"]["patient"]["public_id"] in ids
        assert two_clinics["b"]["patient"]["public_id"] not in ids
        other = two_clinics["b"]["patient"]["public_id"]
        assert c.get(f"/api/admin/patients/{other}/messages").status_code == 404

    def test_appointment_idor_blocked(self, two_clinics):
        c = two_clinics["client"]
        pb = two_clinics["b"]["patient"]
        appt = scheduling.allocate(
            pb["id"],
            tomorrow(),
            "17:30",
            kind="first",
            booked_by="staff",
            booked_by_user_id=two_clinics["b"]["owner_id"],
        )
        _login(c, "iso_owner_a", "owner-isolation-12")
        assert c.get(f"/api/admin/appointments/{appt['public_id']}/audit").status_code == 404
        assert (
            c.patch(
                f"/api/admin/appointments/{appt['public_id']}/status",
                json={"status": "cancelled", "notify": False},
            ).status_code
            == 404
        )
        assert (
            c.patch(
                f"/api/admin/appointments/{appt['public_id']}/move",
                json={"slot_date": tomorrow(), "slot_time": "19:00", "notify": False},
            ).status_code
            == 404
        )


class TestOrphanAssign:
    def test_orphan_secretary_cannot_login_until_assigned(self, client):
        auth.create_user("orphan_sec", "orphan-sec-pass12", role="staff", full_name="منشی یتیم")
        r = client.post(
            "/api/admin/login",
            json={"username": "orphan_sec", "password": "orphan-sec-pass12"},
        )
        assert r.status_code == 401
        owner_id = _make_owner("orphan_claim_owner")
        _login(client, "orphan_claim_owner", "owner-isolation-12")
        orphans = client.get("/api/admin/orphan-secretaries").json()["items"]
        assert any(x["username"] == "orphan_sec" for x in orphans)
        sid = next(x["id"] for x in orphans if x["username"] == "orphan_sec")
        assigned = client.post(f"/api/admin/orphan-secretaries/{sid}/assign")
        assert assigned.status_code == 200, assigned.text
        # no auto-claim on list
        still = client.get("/api/admin/orphan-secretaries").json()["items"]
        assert not any(x["username"] == "orphan_sec" for x in still)
        ok = client.post(
            "/api/admin/login",
            json={"username": "orphan_sec", "password": "orphan-sec-pass12"},
        )
        assert ok.status_code == 200
        row = repo.select(SYSTEM, "admin_users", where="id = ?", params=[sid], limit=1)[0]
        assert row["created_by"] == owner_id
        events = audit.list_for("admin_user", sid)
        assert any(e["action"] == "secretary.assigned" for e in events)

    def test_orphan_patients_listed_not_auto_assigned(self, client):
        a = _make_owner("orp_pat_owner_a")
        b = _make_owner("orp_pat_owner_b")
        # Legacy unassigned rows (created before register-required-owner); simulate via SQL.
        orphan = _patient_for(a, 50337089, "بیمار یتیم")
        repo.update(SYSTEM, "patients", {"owner_id": None}, where="id = ?", params=[orphan["id"]])
        owned = _patient_for(a, 50437089, "بیمار مالک‌دار")
        _login(client, "orp_pat_owner_a", "owner-isolation-12")
        listed = client.get("/api/admin/orphan-patients").json()
        assert listed["total"] >= 1
        pub_ids = {x["public_id"] for x in listed["items"]}
        assert orphan["public_id"] in pub_ids
        assert owned["public_id"] not in pub_ids
        # Opening list must not assign.
        row = repo.select(SYSTEM, "patients", where="id = ?", params=[orphan["id"]], limit=1)[0]
        assert row["owner_id"] is None
        # Assign only the selected id — never blind-all.
        other_orphan = _patient_for(b, 50537089, "بیمار یتیم ۲")
        repo.update(
            SYSTEM,
            "patients",
            {"owner_id": None},
            where="id = ?",
            params=[other_orphan["id"]],
        )
        r = client.post(
            "/api/admin/orphan-patients/assign",
            json={"public_ids": [orphan["public_id"]]},
        )
        assert r.status_code == 200, r.text
        assert orphan["public_id"] in r.json()["assigned"]
        assert other_orphan["public_id"] not in r.json()["assigned"]
        row1 = repo.select(SYSTEM, "patients", where="id = ?", params=[orphan["id"]], limit=1)[0]
        row2 = repo.select(
            SYSTEM, "patients", where="id = ?", params=[other_orphan["id"]], limit=1
        )[0]
        assert row1["owner_id"] == a
        assert row2["owner_id"] is None
        # Owner B still sees the unassigned one, not A's newly claimed patient as orphan.
        _login(client, "orp_pat_owner_b", "owner-isolation-12")
        listed_b = client.get("/api/admin/orphan-patients").json()
        pubs_b = {x["public_id"] for x in listed_b["items"]}
        assert other_orphan["public_id"] in pubs_b
        assert orphan["public_id"] not in pubs_b
        assert b  # used


class TestBookedByIdentity:
    def test_staff_followup_records_user_and_audit(self, client):
        oid = _make_owner("book_owner")
        sec = _make_secretary(oid, "book_sec")
        p = _patient_for(oid, 50637089)
        _login(client, "book_sec", "secretary-iso-12")
        r = client.post(
            f"/api/admin/patients/{p['public_id']}/followup",
            json={"slot_date": tomorrow(), "slot_time": "17:30", "notify": False},
        )
        assert r.status_code == 201, r.text
        code = r.json()["code"]
        appt = repo.select(SYSTEM, "appointments", where="public_id = ?", params=[code], limit=1)[0]
        assert appt["booked_by"] == "staff"
        assert appt["booked_by_user_id"] == sec
        events = audit.list_for("appointment", code)
        follow = next(e for e in events if e["action"] == "appointment.followup")
        assert follow["actor_id"] == sec
        assert follow["actor_role"] == "staff"
        after = follow.get("after") or {}
        assert after.get("booked_by_user_id") == sec

    def test_portal_booking_keeps_null_user_id(self, client):
        # Sole owner already ensured by conftest; portal booker stays NULL.
        r = client.post(
            "/api/portal/register",
            data={
                "full_name": "پورتال تست",
                "national_id": nid(50737089),
                "birth_date": "۱۳۷۰/۰۵/۰۳",
                "phone": "۰۹۱۲۷۷۷۸۸۹۹",
                "mri_link": "",
                "ortho_doctor": "",
            },
            files={"med_photo": ("m.png", io.BytesIO(PNG), "image/png")},
        )
        assert r.status_code == 201, r.text
        book = client.post(
            "/api/portal/appointments",
            json={"slot_date": tomorrow(), "slot_time": "17:30"},
        )
        assert book.status_code == 201, book.text
        code = book.json()["code"]
        appt = repo.select(SYSTEM, "appointments", where="public_id = ?", params=[code], limit=1)[0]
        assert appt["booked_by"] == "patient"
        assert appt["booked_by_user_id"] is None

    def test_move_preserves_original_booker_and_audits_mover(self, client):
        oid = _make_owner("move_book_owner")
        sec = _make_secretary(oid, "move_book_sec")
        p = _patient_for(oid, 50937089)
        _login(client, "move_book_sec", "secretary-iso-12")
        booked = client.post(
            f"/api/admin/patients/{p['public_id']}/followup",
            json={"slot_date": tomorrow(), "slot_time": "17:30", "notify": False},
        )
        assert booked.status_code == 201, booked.text
        code = booked.json()["code"]
        before = repo.select(SYSTEM, "appointments", where="public_id = ?", params=[code], limit=1)[
            0
        ]
        assert before["booked_by_user_id"] == sec

        _login(client, "move_book_owner", "owner-isolation-12")
        moved = client.patch(
            f"/api/admin/appointments/{code}/move",
            json={"slot_date": tomorrow(), "slot_time": "19:00", "notify": False},
        )
        assert moved.status_code == 200, moved.text
        new_code = moved.json()["code"]
        after = repo.select(
            SYSTEM, "appointments", where="public_id = ?", params=[new_code], limit=1
        )[0]
        assert after["booked_by"] == "staff"
        assert after["booked_by_user_id"] == sec  # secretary remains original booker
        assert after["booked_by_user_id"] != oid

        events = audit.list_for("appointment", new_code)
        move_ev = next(e for e in events if e["action"] == "appointment.moved")
        assert move_ev["actor_id"] == oid
        assert move_ev["actor_role"] == "owner"
        after_json = move_ev.get("after") or {}
        assert after_json.get("booked_by_user_id") == sec
        assert after_json.get("moved_by_user_id") == oid
        assert after_json.get("moved_by_role") == "owner"
        assert after_json.get("moved_at")


class TestTwoSecretariesPermissions:
    """Same clinic, two secretaries with different caps — permission IDOR, not multi-doctor."""

    def test_limited_secretary_cannot_access_patient_or_files(self, client):
        oid = _make_owner("two_sec_owner")
        p = _patient_for(oid, 51237089, "بیمار مشترک")
        full = _make_secretary(oid, "sec_full_caps")
        limited = auth.create_user(
            "sec_limited_caps",
            "secretary-iso-12",
            role="staff",
            full_name="منشی محدود",
            created_by=oid,
        )
        auth.replace_permissions(limited, [perms.APPOINTMENTS_VIEW])
        # Full secretary can list patients
        _login(client, "sec_full_caps", "secretary-iso-12")
        assert client.get("/api/admin/patients").status_code == 200
        items = client.get("/api/admin/patients").json()["items"]
        assert any(x["public_id"] == p["public_id"] for x in items)
        # Limited cannot list or open patient resources (IDOR by public_id)
        _login(client, "sec_limited_caps", "secretary-iso-12")
        assert client.get("/api/admin/patients").status_code == 403
        assert client.get(f"/api/admin/patients/{p['public_id']}/messages").status_code == 403
        assert client.get(f"/api/admin/patients/{p['public_id']}/mri").status_code == 403
        assert client.get(f"/api/admin/patients/{p['public_id']}/photo").status_code == 403
        assert (
            client.patch(
                f"/api/admin/patients/{p['public_id']}/note", json={"staff_note": "x"}
            ).status_code
            == 403
        )
        assert client.get("/api/admin/appointments").status_code == 200
        assert full and limited

    def test_limited_cannot_followup_or_create_patient(self, client):
        oid = _make_owner("two_sec_owner2")
        p = _patient_for(oid, 51337089)
        auth.create_user(
            "sec_appt_only",
            "secretary-iso-12",
            role="staff",
            full_name="فقط نوبت",
            created_by=oid,
        )
        sid = repo.select(
            SYSTEM, "admin_users", where="username = ?", params=["sec_appt_only"], limit=1
        )[0]["id"]
        auth.replace_permissions(sid, [perms.APPOINTMENTS_VIEW])
        _login(client, "sec_appt_only", "secretary-iso-12")
        assert (
            client.post(
                f"/api/admin/patients/{p['public_id']}/followup",
                json={"slot_date": tomorrow(), "slot_time": "17:30", "notify": False},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/admin/patients",
                json={
                    "full_name": "نفوذ بیمار",
                    "national_id": nid(51437089),
                    "birth_date": "1370/05/03",
                    "phone": "09121112233",
                },
            ).status_code
            == 403
        )


class TestDisableAndSelfEscalate:
    def test_disable_revokes_active_session(self, client):
        oid = _make_owner("dis_owner")
        sid = _make_secretary(oid, "dis_sec")
        _login(client, "dis_sec", "secretary-iso-12")
        assert client.get("/api/admin/me").status_code == 200
        sec_cookie = client.cookies.get("asa_session")
        _login(client, "dis_owner", "owner-isolation-12")
        r = client.patch(f"/api/admin/secretaries/{sid}", json={"is_active": False})
        assert r.status_code == 200, r.text
        client.cookies.clear()
        if sec_cookie:
            client.cookies.set("asa_session", sec_cookie)
        me = client.get("/api/admin/me")
        assert me.status_code in (401, 403)
        # Fresh login also blocked while disabled
        denied = client.post(
            "/api/admin/login",
            json={"username": "dis_sec", "password": "secretary-iso-12"},
        )
        assert denied.status_code == 401

    def test_secretary_cannot_self_escalate(self, two_clinics):
        c = two_clinics["client"]
        _login(c, "iso_sec_a", "secretary-iso-12")
        assert c.get("/api/admin/secretaries").status_code == 403
        assert (
            c.post(
                "/api/admin/secretaries",
                json={
                    "full_name": "نفوذ",
                    "username": "evil_sec",
                    "password": "evil-password-12",
                    "permissions": list(perms.ALL),
                },
            ).status_code
            == 403
        )
        assert c.get("/api/admin/orphan-patients").status_code == 403
        assert (
            c.post(
                "/api/admin/orphan-patients/assign",
                json={"public_ids": [two_clinics["a"]["patient"]["public_id"]]},
            ).status_code
            == 403
        )


class TestPortalRequiresClinicOwner:
    def test_register_refused_when_no_sole_owner(self, client):
        from api.errors import Conflict

        _make_owner("mig_a")
        _make_owner("mig_b")
        with pytest.raises(Conflict) as exc:
            patients.register(
                {
                    "full_name": "بدون مالک",
                    "national_id": nid(50837089),
                    "birth_date": "1991-07-25",
                    "phone": "09121110000",
                    "mri_link": "",
                    "ortho_doctor": "",
                },
                med_photo="",
                ip="127.0.0.1",
            )
        assert exc.value.code == "clinic_unavailable"
        assert repo.count(SYSTEM, "patients") == 0

    def test_portal_register_http_refused_without_sole_owner(self, client):
        _make_owner("http_a")
        _make_owner("http_b")
        r = client.post(
            "/api/portal/register",
            data={
                "full_name": "پورتال مسدود",
                "national_id": nid(51037089),
                "birth_date": "۱۳۷۰/۰۵/۰۳",
                "phone": "۰۹۱۲۱۰۰۲۰۰۳",
                "mri_link": "",
                "ortho_doctor": "",
            },
            files={"med_photo": ("m.png", io.BytesIO(PNG), "image/png")},
        )
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "clinic_unavailable"
        assert repo.count(SYSTEM, "patients") == 0

    def test_book_refused_for_unassigned_patient(self, client):
        oid = _make_owner("book_block_owner")
        p = _patient_for(oid, 51137089)
        repo.update(SYSTEM, "patients", {"owner_id": None}, where="id = ?", params=[p["id"]])
        login = client.post(
            "/api/portal/login",
            json={"national_id": nid(51137089), "birth_date": "1370/05/03"},
        )
        assert login.status_code == 200, login.text
        before = repo.count(SYSTEM, "appointments")
        book = client.post(
            "/api/portal/appointments",
            json={"slot_date": tomorrow(), "slot_time": "17:30"},
        )
        assert book.status_code == 409, book.text
        assert book.json()["error"]["code"] == "patient_unassigned"
        assert repo.count(SYSTEM, "appointments") == before

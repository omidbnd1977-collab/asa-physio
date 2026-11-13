"""Owner-managed secretary accounts and permission enforcement."""

from __future__ import annotations

import pytest

from api import auth, repo
from api import permissions as perms
from api.policies import SYSTEM
from tests.test_portal import nid


@pytest.fixture()
def secretary_client(client, owner_client):
    r = owner_client.post(
        "/api/admin/secretaries",
        json={
            "full_name": "مریم منشی",
            "username": "sec_mary",
            "password": "secretary-pass-12",
            "permissions": [
                perms.PATIENTS_VIEW,
                perms.APPOINTMENTS_VIEW,
            ],
        },
    )
    assert r.status_code == 201, r.text
    client.cookies.clear()
    login = client.post(
        "/api/admin/login",
        json={"username": "sec_mary", "password": "secretary-pass-12"},
    )
    assert login.status_code == 200, login.text
    client.headers["X-CSRF-Token"] = login.json()["csrf"]
    return client, r.json()


class TestSecretaryLifecycle:
    def test_owner_creates_secretary_with_hashed_password(self, owner_client):
        r = owner_client.post(
            "/api/admin/secretaries",
            json={
                "full_name": "علی منشی",
                "username": "sec_ali",
                "password": "initial-secret-99",
                "permissions": [perms.PATIENTS_VIEW],
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["username"] == "sec_ali"
        assert body["permissions"] == [perms.PATIENTS_VIEW]
        assert "password" not in body and "password_hash" not in body
        row = repo.select(SYSTEM, "admin_users", where="username = ?", params=["sec_ali"])[0]
        assert row["password_hash"].startswith("$argon2")
        assert "initial-secret-99" not in row["password_hash"]
        assert row["created_by"] is not None

    def test_secretary_can_login(self, secretary_client):
        client, meta = secretary_client
        me = client.get("/api/admin/me")
        assert me.status_code == 200
        assert me.json()["role"] == "staff"
        assert me.json()["permissions"] == sorted(
            [perms.PATIENTS_VIEW, perms.APPOINTMENTS_VIEW]
        )
        assert me.json()["can_manage_secretaries"] is False

    def test_secretary_without_perm_cannot_list_patients(self, secretary_client):
        client, _ = secretary_client
        # Drop patients.view
        sid = repo.select(SYSTEM, "admin_users", where="username = ?", params=["sec_mary"])[0][
            "id"
        ]
        auth.replace_permissions(sid, [perms.APPOINTMENTS_VIEW])
        # Session still active; permissions reloaded on each resolve_session
        r = client.get("/api/admin/patients")
        assert r.status_code == 403

    def test_permission_change_applies_immediately(self, secretary_client):
        client, meta = secretary_client
        assert client.get("/api/admin/patients").status_code == 200
        owner_rows = repo.select(SYSTEM, "admin_users", where="role = 'owner'", limit=1)
        # Use SYSTEM to strip patients.view
        auth.replace_permissions(meta["id"], [perms.APPOINTMENTS_VIEW])
        assert client.get("/api/admin/patients").status_code == 403
        assert client.get("/api/admin/appointments").status_code == 200
        assert owner_rows

    def test_disabled_secretary_cannot_login(self, owner_client, client):
        created = owner_client.post(
            "/api/admin/secretaries",
            json={
                "full_name": "منشی غیرفعال",
                "username": "sec_off",
                "password": "disabled-pass-12",
                "permissions": [perms.PATIENTS_VIEW],
            },
        ).json()
        owner_client.patch(
            f"/api/admin/secretaries/{created['id']}", json={"is_active": False}
        )
        client.cookies.clear()
        r = client.post(
            "/api/admin/login",
            json={"username": "sec_off", "password": "disabled-pass-12"},
        )
        assert r.status_code == 401

    def test_owner_cannot_manage_other_owners_secretary(self, client):
        auth.create_user("owner_a", "owner-a-password-12", role="owner", full_name="پزشک الف")
        auth.create_user("owner_b", "owner-b-password-12", role="owner", full_name="پزشک ب")
        a = client.post(
            "/api/admin/login", json={"username": "owner_a", "password": "owner-a-password-12"}
        )
        assert a.status_code == 200
        client.headers["X-CSRF-Token"] = a.json()["csrf"]
        sec = client.post(
            "/api/admin/secretaries",
            json={
                "full_name": "منشی الف",
                "username": "sec_a_only",
                "password": "secretary-a-pass",
                "permissions": [perms.PATIENTS_VIEW],
            },
        )
        assert sec.status_code == 201, sec.text
        sec_id = sec.json()["id"]
        client.post("/api/admin/logout")
        b = client.post(
            "/api/admin/login", json={"username": "owner_b", "password": "owner-b-password-12"}
        )
        client.headers["X-CSRF-Token"] = b.json()["csrf"]
        deny = client.patch(
            f"/api/admin/secretaries/{sec_id}", json={"is_active": False}
        )
        # Cross-owner management must not succeed (404 avoids existence leak).
        assert deny.status_code in (403, 404)

    def test_secretary_cannot_manage_secretaries_api(self, secretary_client):
        client, _ = secretary_client
        r = client.get("/api/admin/secretaries")
        assert r.status_code == 403
        r2 = client.post(
            "/api/admin/secretaries",
            json={
                "full_name": "نفوذی",
                "username": "hacker_sec",
                "password": "should-not-work1",
                "permissions": list(perms.ALL),
            },
        )
        assert r2.status_code == 403


class TestPortalBackButtons:
    def test_register_page_has_step_back_controls(self, client):
        html = client.get("/booking/register").text
        assert "regBackBtn" in html
        assert "regNextBtn" in html
        assert "asa_register_draft" in html or "DRAFT_KEY" in html or "saveDraft" in html

    def test_reserve_page_has_back_controls(self, client):
        # Need a logged-in patient for reserve content; page shell still includes script.
        html = client.get("/booking/reserve").text
        assert "reserveBackBtn" in html or "reserveBackEarly" in html
        assert "goReserveBack" in html or "RESERVE_DRAFT" in html

    def test_register_draft_survives_in_page_script(self, client):
        html = client.get("/booking/register").text
        assert "sessionStorage.setItem(DRAFT_KEY" in html or "asa_register_draft" in html


class TestSecretaryPatientCreate:
    def test_create_patient_requires_permission(self, secretary_client):
        client, meta = secretary_client
        auth.replace_permissions(meta["id"], [perms.APPOINTMENTS_VIEW])
        r = client.post(
            "/api/admin/patients",
            json={
                "full_name": "بیمار تستی",
                "national_id": nid(41137089),
                "birth_date": "1370/05/03",
                "phone": "09121112233",
            },
        )
        assert r.status_code == 403

    def test_create_patient_with_permission(self, owner_client):
        sec = owner_client.post(
            "/api/admin/secretaries",
            json={
                "full_name": "منشی پذیرش",
                "username": "sec_create",
                "password": "create-patient-12",
                "permissions": [perms.PATIENTS_CREATE, perms.PATIENTS_VIEW],
            },
        ).json()
        assert sec["id"]
        # login as secretary via fresh cookies on same client after logout
        owner_client.post("/api/admin/logout")
        login = owner_client.post(
            "/api/admin/login",
            json={"username": "sec_create", "password": "create-patient-12"},
        )
        owner_client.headers["X-CSRF-Token"] = login.json()["csrf"]
        r = owner_client.post(
            "/api/admin/patients",
            json={
                "full_name": "بیمار جدید تست",
                "national_id": nid(42237089),
                "birth_date": "1370/05/03",
                "phone": "09123334455",
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["public_id"]

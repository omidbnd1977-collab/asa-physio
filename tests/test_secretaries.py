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


class TestSecretaryPasswordPolicy:
    def test_create_with_six_char_password(self, owner_client):
        r = owner_client.post(
            "/api/admin/secretaries",
            json={
                "full_name": "منشی کوتاه",
                "username": "sec_pwd6",
                "password": "abcdef",
                "permissions": [perms.PATIENTS_VIEW],
            },
        )
        assert r.status_code == 201, r.text
        row = repo.select(SYSTEM, "admin_users", where="username = ?", params=["sec_pwd6"])[0]
        assert row["password_hash"].startswith("$argon2")
        assert "abcdef" not in row["password_hash"]

    def test_reject_password_shorter_than_six(self, owner_client):
        r = owner_client.post(
            "/api/admin/secretaries",
            json={
                "full_name": "منشی کوتاه",
                "username": "sec_pwd5",
                "password": "abcde",
                "permissions": [],
            },
        )
        assert r.status_code == 422

    def test_reset_password_six_chars(self, owner_client):
        created = owner_client.post(
            "/api/admin/secretaries",
            json={
                "full_name": "منشی ریست",
                "username": "sec_reset6",
                "password": "abcdef",
                "permissions": [],
            },
        ).json()
        r = owner_client.post(
            f"/api/admin/secretaries/{created['id']}/password",
            json={"password": "xyz123"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        owner_client.post("/api/admin/logout")
        login = owner_client.post(
            "/api/admin/login",
            json={"username": "sec_reset6", "password": "xyz123"},
        )
        assert login.status_code == 200, login.text


def _relogin_owner(client):
    client.cookies.clear()
    r = client.post(
        "/api/admin/login",
        json={"username": "t_owner", "password": "a-very-long-test-password"},
    )
    assert r.status_code == 200, r.text
    client.headers["X-CSRF-Token"] = r.json()["csrf"]


def _login_as(client, username: str, password: str):
    client.cookies.clear()
    r = client.post("/api/admin/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    client.headers["X-CSRF-Token"] = r.json()["csrf"]


class TestSecretaryPermissionsStrict:
    def test_owner_can_add_and_remove_permissions(self, owner_client):
        created = owner_client.post(
            "/api/admin/secretaries",
            json={
                "full_name": "منشی پرم",
                "username": "sec_perm_flip",
                "password": "abcdef",
                "permissions": [perms.PATIENTS_VIEW],
            },
        ).json()
        sid = created["id"]
        _login_as(owner_client, "sec_perm_flip", "abcdef")
        assert owner_client.get("/api/admin/patients").status_code == 200
        assert owner_client.get("/api/admin/appointments").status_code == 403

        _relogin_owner(owner_client)
        assert owner_client.patch(
            f"/api/admin/secretaries/{sid}",
            json={"permissions": [perms.PATIENTS_VIEW, perms.APPOINTMENTS_VIEW]},
        ).status_code == 200
        _login_as(owner_client, "sec_perm_flip", "abcdef")
        assert owner_client.get("/api/admin/appointments").status_code == 200
        _relogin_owner(owner_client)
        assert owner_client.patch(
            f"/api/admin/secretaries/{sid}",
            json={"permissions": [perms.APPOINTMENTS_VIEW]},
        ).status_code == 200
        _login_as(owner_client, "sec_perm_flip", "abcdef")
        assert owner_client.get("/api/admin/patients").status_code == 403

    def test_clinical_and_files_require_explicit_perms(self, owner_client):
        p = owner_client.post(
            "/api/admin/patients",
            json={
                "full_name": "بیمار بالینی",
                "national_id": nid(43337089),
                "birth_date": "1370/05/03",
                "phone": "09124445566",
            },
        )
        assert p.status_code == 201, p.text
        pid = p.json()["public_id"]
        sec = owner_client.post(
            "/api/admin/secretaries",
            json={
                "full_name": "منشی بدون بالین",
                "username": "sec_no_clin",
                "password": "abcdef",
                "permissions": [perms.PATIENTS_VIEW, perms.PATIENTS_EDIT],
            },
        ).json()
        _login_as(owner_client, "sec_no_clin", "abcdef")
        assert owner_client.get(f"/api/admin/patients/{pid}/sessions").status_code == 403
        assert owner_client.get(f"/api/admin/patients/{pid}/mri").status_code == 403
        assert owner_client.get("/api/admin/catalog").status_code == 403
        assert owner_client.get("/api/admin/metrics").status_code == 403
        assert owner_client.get("/api/admin/feedback").status_code == 403

        _relogin_owner(owner_client)
        assert owner_client.patch(
            f"/api/admin/secretaries/{sec['id']}",
            json={
                "permissions": [
                    perms.PATIENTS_VIEW,
                    perms.CLINICAL_RECORD,
                    perms.FILES_MANAGE,
                ]
            },
        ).status_code == 200
        _login_as(owner_client, "sec_no_clin", "abcdef")
        assert owner_client.get(f"/api/admin/patients/{pid}/sessions").status_code == 200
        assert owner_client.get("/api/admin/catalog").status_code == 200

    def test_disable_revokes_session(self, owner_client):
        created = owner_client.post(
            "/api/admin/secretaries",
            json={
                "full_name": "منشی نشست",
                "username": "sec_sess_kill",
                "password": "abcdef",
                "permissions": [perms.PATIENTS_VIEW],
            },
        ).json()
        _login_as(owner_client, "sec_sess_kill", "abcdef")
        assert owner_client.get("/api/admin/me").status_code == 200
        # Keep the secretary cookie, but mutate via SYSTEM/repo as the owner would.
        # (owner_client shares the same TestClient cookies.)
        from api import staff_users
        from api.policies import Actor

        owner_row = repo.select(
            SYSTEM, "admin_users", where="username = ?", params=["t_owner"], limit=1
        )[0]
        owner_actor = Actor(role="owner", user_id=int(owner_row["id"]))
        staff_users.update_secretary(owner_actor, created["id"], is_active=False)
        assert owner_client.get("/api/admin/me").status_code == 401

    def test_soft_delete_preserves_booker(self, owner_client):
        from api import patients, scheduling

        oid = repo.select(
            SYSTEM, "admin_users", where="username = ?", params=["t_owner"], limit=1
        )[0]["id"]
        sec = owner_client.post(
            "/api/admin/secretaries",
            json={
                "full_name": "منشی با سابقه",
                "username": "sec_hist",
                "password": "abcdef",
                "permissions": [perms.APPOINTMENTS_CREATE, perms.APPOINTMENTS_VIEW],
            },
        ).json()
        pat = patients.register(
            {
                "full_name": "بیمار سابقه",
                "national_id": nid(44437089),
                "birth_date": "1991-07-25",
                "phone": "09125556677",
                "mri_link": "",
                "ortho_doctor": "",
                "website": "",
            },
            med_photo="",
            ip="127.0.0.1",
            owner_id=oid,
        )
        # Bypass slot race: stamp booked_by_user_id on an allocated row.
        days = [d for d in scheduling.open_days() if not d.get("closed")]
        day = days[-1]["date"]
        appt = scheduling.allocate(
            pat["id"], day, "20:30", kind="followup", booked_by="staff", booked_by_user_id=None
        )
        repo.update(
            SYSTEM,
            "appointments",
            {"booked_by_user_id": sec["id"]},
            where="id = ?",
            params=[appt["id"]],
        )
        deleted = owner_client.delete(f"/api/admin/secretaries/{sec['id']}")
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["mode"] == "soft"
        row = repo.select(SYSTEM, "admin_users", where="id = ?", params=[sec["id"]])[0]
        assert row["is_active"] == 0
        still = repo.select(
            SYSTEM, "appointments", where="id = ?", params=[appt["id"]]
        )[0]
        assert still["booked_by_user_id"] == sec["id"]

    def test_hard_delete_when_no_history(self, owner_client):
        created = owner_client.post(
            "/api/admin/secretaries",
            json={
                "full_name": "منشی بدون سابقه",
                "username": "sec_nohist",
                "password": "abcdef",
                "permissions": [],
            },
        ).json()
        r = owner_client.delete(f"/api/admin/secretaries/{created['id']}")
        assert r.status_code == 200, r.text
        assert r.json()["mode"] == "hard"
        assert not repo.select(
            SYSTEM, "admin_users", where="id = ?", params=[created["id"]]
        )


class TestSecretaryUiAndBack:
    def test_admin_has_secretary_wizard_and_edit_dialog(self, owner_client):
        html = owner_client.get("/admin").text
        assert "secBackBtn" in html and "secNextBtn" in html
        assert "secEditDlg" in html and "secEditBackBtn" in html
        assert "clinical.record" in html and "files.manage" in html
        assert "حداقل ۶ نویسه" in html

    def test_register_mentions_file_draft_limit(self, client):
        html = client.get("/booking/register").text
        assert "fileDraftHint" in html
        assert "regBackBtn" in html

    def test_admin_patient_create_form_and_tab_need_any(self, owner_client):
        html = owner_client.get("/admin").text
        assert 'data-tab="pts" data-need-any="patients.view,patients.create,patients.edit"' in html
        assert "patCreateForm" in html and "patCreateCard" in html
        assert "data-pane=\"noperm\"" in html
        assert "firstVisibleTab" in html and "'noperm'" in html
        assert "applyPatientsPaneVisibility" in html


class TestPermissionMatrixApi:
    """Positive/negative API checks for each staff capability."""

    def _sec(self, owner_client, username: str, caps: list[str]):
        r = owner_client.post(
            "/api/admin/secretaries",
            json={
                "full_name": f"منشی {username}",
                "username": username,
                "password": "abcdef",
                "permissions": caps,
            },
        )
        assert r.status_code == 201, r.text
        return r.json()

    def _as(self, client, username: str):
        client.cookies.clear()
        login = client.post(
            "/api/admin/login", json={"username": username, "password": "abcdef"}
        )
        assert login.status_code == 200, login.text
        client.headers["X-CSRF-Token"] = login.json()["csrf"]
        return login.json()

    def test_create_only_secretary_can_register_patient_not_list(self, owner_client):
        self._sec(owner_client, "sec_create_only", [perms.PATIENTS_CREATE])
        self._as(owner_client, "sec_create_only")
        me = owner_client.get("/api/admin/me").json()
        assert me["role"] == "staff"
        assert me["permissions"] == [perms.PATIENTS_CREATE]
        assert me["can_manage_secretaries"] is False
        assert owner_client.get("/api/admin/patients").status_code == 403
        assert owner_client.get("/api/admin/appointments").status_code == 403
        assert owner_client.get("/api/admin/secretaries").status_code == 403
        assert owner_client.get("/api/admin/metrics").status_code == 403
        created = owner_client.post(
            "/api/admin/patients",
            json={
                "full_name": "بیمار منشی فقط ثبت",
                "national_id": nid(46637089),
                "birth_date": "1370/05/03",
                "phone": "09126667788",
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["public_id"]
        # Ownership: patient belongs to clinic owner, not the secretary id.
        oid = repo.select(
            SYSTEM, "admin_users", where="username = ?", params=["t_owner"], limit=1
        )[0]["id"]
        row = repo.select(
            SYSTEM,
            "patients",
            where="public_id = ?",
            params=[created.json()["public_id"]],
            limit=1,
        )[0]
        assert row["owner_id"] == oid

    def test_view_only_cannot_create_or_edit(self, owner_client):
        self._sec(owner_client, "sec_view_only", [perms.PATIENTS_VIEW])
        # seed a patient as owner
        _relogin_owner(owner_client)
        p = owner_client.post(
            "/api/admin/patients",
            json={
                "full_name": "بیمار ویو",
                "national_id": nid(46737089),
                "birth_date": "1370/05/03",
                "phone": "09127778899",
            },
        ).json()
        self._as(owner_client, "sec_view_only")
        assert owner_client.get("/api/admin/patients").status_code == 200
        assert owner_client.post(
            "/api/admin/patients",
            json={
                "full_name": "نباید ثبت شود",
                "national_id": nid(46837089),
                "birth_date": "1370/05/03",
                "phone": "09128889900",
            },
        ).status_code == 403
        assert owner_client.patch(
            f"/api/admin/patients/{p['public_id']}/note",
            json={"staff_note": "x"},
        ).status_code == 403

    def test_edit_only_can_note_not_list_without_view(self, owner_client):
        _relogin_owner(owner_client)
        p = owner_client.post(
            "/api/admin/patients",
            json={
                "full_name": "بیمار ادیت",
                "national_id": nid(46937089),
                "birth_date": "1370/05/03",
                "phone": "09129990011",
            },
        ).json()
        self._sec(owner_client, "sec_edit_only", [perms.PATIENTS_EDIT])
        self._as(owner_client, "sec_edit_only")
        assert owner_client.get("/api/admin/patients").status_code == 403
        assert owner_client.patch(
            f"/api/admin/patients/{p['public_id']}/note",
            json={"staff_note": "یادداشت منشی"},
        ).status_code == 200

    def test_appointments_perms_positive_negative(self, owner_client):
        _relogin_owner(owner_client)
        p = owner_client.post(
            "/api/admin/patients",
            json={
                "full_name": "بیمار نوبت",
                "national_id": nid(47037089),
                "birth_date": "1370/05/03",
                "phone": "09120001122",
            },
        ).json()
        self._sec(owner_client, "sec_appt_view", [perms.APPOINTMENTS_VIEW])
        self._as(owner_client, "sec_appt_view")
        assert owner_client.get("/api/admin/appointments").status_code == 200
        assert owner_client.post(
            f"/api/admin/patients/{p['public_id']}/followup",
            json={"slot_date": "2099-01-01", "slot_time": "16:00", "notify": False},
        ).status_code == 403

        _relogin_owner(owner_client)
        self._sec(
            owner_client,
            "sec_appt_create",
            [perms.APPOINTMENTS_VIEW, perms.APPOINTMENTS_CREATE, perms.PATIENTS_VIEW],
        )
        # create still needs a valid future slot — just assert permission gate opens past 403
        self._as(owner_client, "sec_appt_create")
        r = owner_client.post(
            f"/api/admin/patients/{p['public_id']}/followup",
            json={"slot_date": "2099-01-01", "slot_time": "16:00", "notify": False},
        )
        assert r.status_code != 403

    def test_messages_clinical_files_matrix(self, owner_client):
        _relogin_owner(owner_client)
        p = owner_client.post(
            "/api/admin/patients",
            json={
                "full_name": "بیمار ماتریس",
                "national_id": nid(47137089),
                "birth_date": "1370/05/03",
                "phone": "09121112200",
            },
        ).json()
        pid = p["public_id"]
        self._sec(owner_client, "sec_msg", [perms.MESSAGES_VIEW])
        self._as(owner_client, "sec_msg")
        assert owner_client.get("/api/admin/messages").status_code == 200
        assert owner_client.get(f"/api/admin/patients/{pid}/sessions").status_code == 403
        assert owner_client.get(f"/api/admin/patients/{pid}/mri").status_code == 403

        _relogin_owner(owner_client)
        self._sec(
            owner_client,
            "sec_clin",
            [perms.PATIENTS_VIEW, perms.CLINICAL_RECORD],
        )
        self._as(owner_client, "sec_clin")
        assert owner_client.get(f"/api/admin/patients/{pid}/sessions").status_code == 200
        assert owner_client.get(f"/api/admin/patients/{pid}/mri").status_code == 403
        assert owner_client.get("/api/admin/catalog").status_code == 200

        _relogin_owner(owner_client)
        self._sec(
            owner_client,
            "sec_files",
            [perms.PATIENTS_VIEW, perms.FILES_MANAGE],
        )
        self._as(owner_client, "sec_files")
        # No file uploaded → 404 NotFound is fine; must not be 403
        r = owner_client.get(f"/api/admin/patients/{pid}/mri")
        assert r.status_code in (404, 200)
        assert owner_client.get(f"/api/admin/patients/{pid}/sessions").status_code == 403

    def test_create_only_ui_not_ops_fallback(self, owner_client):
        """Regression: patients.create alone must not land on doctor ops pane."""
        html = owner_client.get("/admin").text
        assert "return t ? t.dataset.tab : 'noperm'" in html or "return t ? t.dataset.tab : \"noperm\"" in html or "'noperm'" in html
        assert "if (name === 'ops' && ROLE !== 'owner')" in html
        assert 'data-need-any="patients.view,patients.create,patients.edit"' in html

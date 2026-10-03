"""Regression coverage for the production patient/doctor workflow additions."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_portal import reg_form, tomorrow


@pytest.fixture()
def patient(client):
    response = client.post("/api/portal/register", data=reg_form())
    assert response.status_code == 201, response.text
    return response.json()


def test_static_source_booking_links_are_github_pages_safe():
    root = Path(__file__).resolve().parent.parent
    source = "\n".join(p.read_text(encoding="utf-8") for p in (root / "site").glob("*.html"))
    assert 'href="/booking"' not in source
    assert "contact.html#bookForm" in source
    assert 'href="#bookForm"' in source
    assert 'href="/api/bookings"' not in source


def test_patient_update_and_audit_are_staff_only(owner_client, patient, client):
    from api import repo
    from api.policies import SYSTEM

    public_id = repo.select(SYSTEM, "patients")[0]["public_id"]
    r = owner_client.patch(
        f"/api/admin/patients/{public_id}",
        json={
            "full_name": "سارا کریمی",
            "phone": "09121112233",
            "ortho_doctor": "دکتر جدید",
            "mri_link": "https://example.com/mri",
            "staff_note": "پیگیری لازم است",
        },
    )
    assert r.status_code == 200, r.text
    client.cookies.clear()
    assert client.get(f"/api/admin/patients/{public_id}/audit").status_code == 401
    login = owner_client.post(
        "/api/admin/login", json={"username": "t_owner", "password": "a-very-long-test-password"}
    )
    assert login.status_code == 200
    owner_client.headers["X-CSRF-Token"] = login.json()["csrf"]
    audit = owner_client.get(f"/api/admin/patients/{public_id}/audit")
    assert audit.status_code == 200
    assert any(x["action"] == "patient.updated" for x in audit.json()["items"])


def test_status_change_is_audited(owner_client, patient):
    r = owner_client.post(
        "/api/portal/appointments",
        json={"slot_date": tomorrow(), "slot_time": "18:00"},
    )
    assert r.status_code == 201, r.text
    code = r.json()["code"]
    r = owner_client.patch(f"/api/admin/appointments/{code}/status", json={"status": "attended"})
    assert r.status_code == 200, r.text
    audit = owner_client.get(f"/api/admin/appointments/{code}/audit")
    assert audit.status_code == 200
    assert any(x["action"] == "appointment.status_changed" for x in audit.json()["items"])


def test_move_atomic_creates_one_new_booking_and_audit(owner_client, patient):
    from api import repo
    from api.policies import SYSTEM

    book = owner_client.post(
        "/api/portal/appointments",
        json={"slot_date": tomorrow(), "slot_time": "18:30"},
    )
    assert book.status_code == 201, book.text
    old_code = book.json()["code"]
    moved = owner_client.patch(
        f"/api/admin/appointments/{old_code}/move",
        json={"slot_date": tomorrow(), "slot_time": "19:00", "notify": False},
    )
    assert moved.status_code == 200, moved.text
    rows = repo.select(SYSTEM, "appointments")
    assert sum(a["status"] != "cancelled" for a in rows) == 1
    assert moved.json()["code"] != old_code
    assert owner_client.get(f"/api/admin/appointments/{moved.json()['code']}/audit").status_code == 200


def test_all_required_slot_times_are_exposed():
    from api import scheduling

    assert scheduling.SLOTS == [
        "16:00", "16:30", "17:00", "17:30", "18:00", "18:30",
        "19:00", "19:30", "20:00", "20:30", "21:00", "21:30",
    ]
    assert scheduling.DAY_CAPACITY == 120

"""Regression coverage for the production patient/doctor workflow additions."""

from __future__ import annotations

import re
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


def test_built_bundle_has_no_root_absolute_local_links():
    """The bundle is served from "/" on Render and "/asa-physio/" on GitHub Pages. A
    root-absolute href only resolves under the first. This shipped broken once: the live
    Pages site requested /style.<hash>.css, got 404, and rendered unstyled with dead nav."""
    public = Path(__file__).resolve().parent.parent / "public"
    if not public.is_dir():
        pytest.skip("public/ is not built")
    offenders = []
    for page in sorted(public.glob("**/*.html")):
        text = page.read_text(encoding="utf-8")
        for attr, url in re.findall(r'(href|src)\s*=\s*"([^"]+)"', text):
            if url.startswith("/") and not url.startswith("//"):
                offenders.append(f'{page.name}: {attr}="{url}"')
    assert not offenders, "\n".join(offenders)


def test_built_bundle_preserves_url_fragments():
    """build_static rewrites hrefs to fingerprinted names and must keep the #fragment,
    or the booking CTA lands at the top of the page instead of on the form."""
    index = Path(__file__).resolve().parent.parent / "public" / "index.html"
    if not index.exists():
        pytest.skip("public/ is not built")
    assert 'href="contact.html#bookForm"' in index.read_text(encoding="utf-8")


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
    assert (
        owner_client.get(f"/api/admin/appointments/{moved.json()['code']}/audit").status_code == 200
    )


def test_all_required_slot_times_are_exposed():
    from api import scheduling

    assert scheduling.SLOTS == [
        "16:00",
        "16:30",
        "17:00",
        "17:30",
        "18:00",
        "18:30",
        "19:00",
        "19:30",
        "20:00",
        "20:30",
        "21:00",
        "21:30",
    ]
    assert scheduling.DAY_CAPACITY == 120


def test_bootstrap_admin_is_opt_in_and_idempotent(monkeypatch):
    """A diskless deploy wipes its SQLite file on every cold start, so without a
    first-boot owner the admin panel is permanently unreachable. Stays off unless both
    variables are set, and must not create a second owner on restart."""
    from api import repo
    from api.config import settings
    from api.main import _bootstrap_admin
    from api.policies import SYSTEM

    where = "username = ?"
    params = ["boot_owner"]
    repo.delete(SYSTEM, "admin_users", where=where, params=params)

    monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_USER", "")
    monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_PASSWORD", "")
    _bootstrap_admin()
    assert repo.count(SYSTEM, "admin_users", where=where, params=params) == 0

    monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_USER", "boot_owner")
    monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_PASSWORD", "a-boot-password-12")
    _bootstrap_admin()
    _bootstrap_admin()
    assert repo.count(SYSTEM, "admin_users", where=where, params=params) == 1
    repo.delete(SYSTEM, "admin_users", where=where, params=params)

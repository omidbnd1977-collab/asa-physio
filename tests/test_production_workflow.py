"""Regression coverage for the production patient/doctor workflow additions."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.test_portal import reg_form, tomorrow


@pytest.fixture()
def patient(client):
    from api import auth, repo
    from api.policies import SYSTEM

    owners = repo.select(
        SYSTEM, "admin_users", where="role = 'owner' AND is_active = 1", limit=2
    )
    if not owners:
        auth.create_user(
            "t_owner", "a-very-long-test-password", role="owner", full_name="پزشک تست"
        )
    response = client.post("/api/portal/register", data=reg_form())
    assert response.status_code == 201, response.text
    return response.json()


def test_static_source_booking_ctas_go_to_portal():
    """Primary deploy is same-origin FastAPI (NetAram VPS / Render preview).
    Every «رزرو نوبت» CTA must open the real patient portal at /booking."""
    root = Path(__file__).resolve().parent.parent
    site = root / "site"
    for page in sorted(site.glob("*.html")):
        text = page.read_text(encoding="utf-8")
        assert 'href="contact.html#bookForm"' not in text, page.name
        # Nav / hero CTAs labeled رزرو نوبت use the portal path
        if "رزرو نوبت" in text:
            assert 'href="/booking"' in text, page.name
    contact = (site / "contact.html").read_text(encoding="utf-8")
    assert 'id="consultForm"' in contact
    assert "درخواست مشاوره و تماس" in contact
    assert "رزرو نوبت نیست" in contact
    assert 'id="feedbackForm"' in contact
    assert 'id="bookForm"' not in contact
    assert "/api/bookings" in contact  # consult form still posts leads
    assert "/api/feedback" in contact
    # Never hardcode the API as a navigation href
    source = "\n".join(p.read_text(encoding="utf-8") for p in site.glob("*.html"))
    assert 'href="/api/bookings"' not in source


def test_built_bundle_allows_portal_absolute_links_only():
    """Same-origin deploy: /booking is intentional. Other root-absolute local
    asset/nav links must stay relative so fingerprinted CSS/JS keep working."""
    public = Path(__file__).resolve().parent.parent / "public"
    if not public.is_dir():
        pytest.skip("public/ is not built")
    allowed = {"/booking"}
    offenders = []
    for page in sorted(public.glob("**/*.html")):
        text = page.read_text(encoding="utf-8")
        for attr, url in re.findall(r'(href|src)\s*=\s*"([^"]+)"', text):
            if url.startswith("/") and not url.startswith("//") and url not in allowed:
                # /booking/... deep links from marketing pages are also OK if added later
                if url.startswith("/booking"):
                    continue
                offenders.append(f'{page.name}: {attr}="{url}"')
    assert not offenders, "\n".join(offenders)


def test_built_bundle_booking_cta_and_consult_form_ids():
    """After build_static, portal CTAs and contact form ids stay intact."""
    public = Path(__file__).resolve().parent.parent / "public"
    if not public.is_dir():
        pytest.skip("public/ is not built")
    index = (public / "index.html").read_text(encoding="utf-8")
    assert 'href="/booking"' in index
    assert 'href="contact.html#bookForm"' not in index
    contact = (public / "contact.html").read_text(encoding="utf-8")
    assert 'id="consultForm"' in contact
    assert 'id="feedbackForm"' in contact
    assert 'id="bookForm"' not in contact
    assert 'href="/booking"' in contact


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
        json={"slot_date": tomorrow(), "slot_time": "16:00"},
    )
    assert r.status_code == 201, r.text
    code = r.json()["code"]
    r = owner_client.patch(f"/api/admin/appointments/{code}/status", json={"status": "attended"})
    assert r.status_code == 200, r.text
    audit = owner_client.get(f"/api/admin/appointments/{code}/audit")
    assert audit.status_code == 200
    assert any(x["action"] == "appointment.status_changed" for x in audit.json()["items"])


def test_active_appointments_only_include_booked_and_records_keep_attended(owner_client, patient):
    from api import repo
    from api.policies import SYSTEM

    book = owner_client.post(
        "/api/portal/appointments",
        json={"slot_date": tomorrow(), "slot_time": "16:00"},
    )
    assert book.status_code == 201, book.text
    code = book.json()["code"]
    patient_id = repo.select(SYSTEM, "patients")[0]["public_id"]

    completed = owner_client.patch(
        f"/api/admin/appointments/{code}/status", json={"status": "attended"}
    )
    assert completed.status_code == 200, completed.text

    active = owner_client.get("/api/admin/appointments?scope=upcoming")
    assert active.status_code == 200, active.text
    assert all(item["status"] == "booked" for item in active.json()["items"])
    assert code not in {item["public_id"] for item in active.json()["items"]}

    all_appointments = owner_client.get("/api/admin/appointments?scope=all")
    assert all_appointments.status_code == 200, all_appointments.text
    kept = next(item for item in all_appointments.json()["items"] if item["public_id"] == code)
    assert kept["status"] == "attended"

    records = owner_client.get("/api/admin/patients?per_page=100")
    assert records.status_code == 200, records.text
    record = next(item for item in records.json()["items"] if item["public_id"] == patient_id)
    assert any(
        item["public_id"] == code and item["status"] == "attended"
        for item in record["appointments"]
    )


def test_cancelled_and_no_show_stay_distinct_from_completed(owner_client, patient):
    codes = {}
    for slot_time, status in (("17:30", "cancelled"), ("19:00", "no_show")):
        booked = owner_client.post(
            "/api/portal/appointments",
            json={"slot_date": tomorrow(), "slot_time": slot_time},
        )
        assert booked.status_code == 201, booked.text
        code = booked.json()["code"]
        changed = owner_client.patch(
            f"/api/admin/appointments/{code}/status", json={"status": status}
        )
        assert changed.status_code == 200, changed.text
        codes[code] = status

    active = owner_client.get("/api/admin/appointments?scope=upcoming")
    assert active.status_code == 200, active.text
    assert not codes.keys() & {item["public_id"] for item in active.json()["items"]}

    all_appointments = owner_client.get("/api/admin/appointments?scope=all")
    assert all_appointments.status_code == 200, all_appointments.text
    statuses = {item["public_id"]: item["status"] for item in all_appointments.json()["items"]}
    assert {statuses[code] for code in codes} == {"cancelled", "no_show"}


def test_move_atomic_creates_one_new_booking_and_audit(owner_client, patient):
    from api import repo
    from api.policies import SYSTEM

    book = owner_client.post(
        "/api/portal/appointments",
        json={"slot_date": tomorrow(), "slot_time": "17:30"},
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

    assert scheduling.SLOTS == ["16:00", "17:30", "19:00", "20:30"]
    assert scheduling.DAY_CAPACITY == 40


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


def test_dockerfile_ships_every_file_pages_reads_at_request_time():
    """api/pages.py inlines assets/mark.svg while rendering the portal shell. The image
    once omitted that directory and every portal page answered 500 instead of degrading
    to no logo, so the copy is asserted rather than assumed."""
    root = Path(__file__).resolve().parent.parent
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY assets ./assets" in dockerfile
    assert (root / "assets" / "mark.svg").is_file()


def test_portal_logo_degrades_when_the_mark_is_missing(tmp_path, monkeypatch):
    """A decorative SVG must never take a whole page down with it."""
    from api import pages

    pages.logo_path.cache_clear()
    monkeypatch.setattr(pages, "ROOT", tmp_path)
    try:
        assert pages.logo_path() == ""
    finally:
        pages.logo_path.cache_clear()

    real_root = Path(__file__).resolve().parent.parent
    assert re.search(r'<path d="[^"]+"', (real_root / "assets" / "mark.svg").read_text("utf-8"))

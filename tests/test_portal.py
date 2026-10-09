"""Patient portal: registration, returning login, the slot grid, SMS, reminders."""

from __future__ import annotations

import datetime as dt
import io

import pytest


def nid(n: int) -> str:
    base = f"{n:09d}"
    s = sum(int(base[i]) * (10 - i) for i in range(9))
    r = s % 11
    return base + str(r if r < 2 else 11 - r)


NID_A = nid(49937089)
NID_B = nid(123456789)


def tomorrow() -> str:
    from api.jalali import now_tehran

    return (now_tehran().date() + dt.timedelta(days=1)).isoformat()


def reg_form(**kw) -> dict:
    base = {
        "full_name": "سارا کریمی",
        "national_id": NID_A,
        "birth_date": "۱۳۷۰/۰۵/۰۳",
        "phone": "۰۹۱۲۱۱۱۲۲۳۳",
        "mri_link": "https://drive.google.com/file/d/abc",
        "ortho_doctor": "دکتر رضایی",
    }
    base.update(kw)
    return base


PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
    b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture()
def patient(client):
    r = client.post(
        "/api/portal/register",
        data=reg_form(),
        files={"med_photo": ("m.png", io.BytesIO(PNG), "image/png")},
    )
    assert r.status_code == 201, r.text
    return r.json()


# ======================================================================
# Jalali calendar + national id
# ======================================================================
class TestJalali:
    @pytest.mark.parametrize(
        "g,j",
        [
            # 2026-10-01 is 1405/07/09. The value here used to be "1404/07/09" —
            # that was the *output of the buggy g2j*, so the test locked the bug in.
            ((2026, 10, 1), "1405/07/09"),
            ((2024, 3, 20), "1403/01/01"),
            ((1991, 7, 25), "1370/05/03"),
            # Nowruz anchors (published calendars + jdatetime agree on these)
            ((2025, 3, 21), "1404/01/01"),
            ((2026, 3, 21), "1405/01/01"),
        ],
    )
    def test_gregorian_to_jalali(self, g, j):
        from api.jalali import to_jalali_str

        assert to_jalali_str(dt.date(*g)) == j

    def test_day_366_branch_keeps_the_right_year(self):
        """Regression: the day-of-cycle branch that fires after day 366 of the
        33-year cycle used to drop a whole year (2026-10-01 → 1404/07/09 instead
        of 1405/07/09, i.e. every date the clinic currently shows was a year off).
        """
        from api.jalali import to_jalali_str

        assert to_jalali_str(dt.date(2026, 10, 1)) == "1405/07/09"
        assert to_jalali_str(dt.date(2025, 3, 21)) == "1404/01/01"
        assert to_jalali_str(dt.date(2026, 3, 21)) == "1405/01/01"
        assert to_jalali_str(dt.date(2026, 9, 23)) == "1405/07/01"

    def test_round_trip_every_month(self):
        from api.jalali import g2j, j2g

        # 1403 was the one year the old code got right, so a 1403-only loop could
        # never catch the drift; cover years on both sides of it.
        for jy in (1399, 1400, 1402, 1403, 1404, 1405, 1408, 1410):
            for jm in range(1, 13):
                for jd in (1, 15, 29):
                    g = j2g(jy, jm, jd)
                    assert g2j(*g) == (jy, jm, jd), (jy, jm, jd)

    @pytest.mark.parametrize("raw", ["1370/5/3", "۱۳۷۰-۰۵-۰۳", "1370.05.03", "70/5/3"])
    def test_parse_accepts_the_formats_people_type(self, raw):
        from api.jalali import parse_jalali

        assert parse_jalali(raw) == dt.date(1991, 7, 25)

    def test_parse_rejects_nonsense(self):
        from api.jalali import parse_jalali

        for bad in ["1370/13/01", "1370/01/32", "hello", "1370"]:
            with pytest.raises(ValueError):
                parse_jalali(bad)

    def test_national_id_checksum(self):
        from api.jalali import valid_national_id

        assert valid_national_id(NID_A)
        assert not valid_national_id("1234567890")
        assert not valid_national_id("0000000000")
        assert not valid_national_id("123")


# ======================================================================
# Registration
# ======================================================================
class TestRegistration:
    def test_happy_path_stores_everything_and_texts_the_patient(self, client):
        from api import repo
        from api.config import settings
        from api.policies import SYSTEM

        r = client.post(
            "/api/portal/register",
            data=reg_form(),
            files={"med_photo": ("m.png", io.BytesIO(PNG), "image/png")},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["ok"] and body["sms_sent"] and body["redirect"] == "/booking/reserve"

        p = repo.select(SYSTEM, "patients")[0]
        assert p["full_name"] == "سارا کریمی"
        assert p["national_id"] == NID_A
        assert p["birth_date"] == "1991-07-25"  # stored gregorian
        assert p["birth_jalali"] == "1370/05/03"  # shown jalali
        assert p["phone"] == "09121112233"  # normalised
        assert p["ortho_doctor"] == "دکتر رضایی"
        assert p["med_photo"].endswith(".png")
        assert (settings.UPLOAD_DIR / p["med_photo"]).is_file()

        sms = repo.select(SYSTEM, "sms_messages", where="kind = 'registered'")
        assert len(sms) == 1 and sms[0]["status"] == "sent"
        assert "اطلاعات شما" in sms[0]["body"]

    def test_the_session_cookie_is_set_so_the_patient_goes_straight_to_booking(self, client):
        r = client.post("/api/portal/register", data=reg_form())
        assert r.status_code == 201
        assert "asa_patient" in r.cookies or "asa_patient" in r.headers.get("set-cookie", "")
        assert client.get("/api/portal/me").status_code == 200

    def test_session_cookie_is_httponly_and_strict(self, client):
        r = client.post("/api/portal/register", data=reg_form())
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie

    @pytest.mark.parametrize(
        "bad,field",
        [
            ({"national_id": "1234567890"}, "national_id"),
            ({"full_name": "سارا"}, "full_name"),
            ({"phone": "12345"}, "phone"),
            ({"birth_date": "1370/13/40"}, "birth_date"),
            ({"mri_link": "javascript:alert(1)"}, "mri_link"),
        ],
    )
    def test_invalid_input_is_named(self, client, bad, field):
        r = client.post("/api/portal/register", data=reg_form(**bad))
        assert r.status_code == 422
        assert any(field in k for k in r.json()["error"]["fields"])

    def test_same_national_id_is_sent_to_the_login_path(self, client, patient):
        r = client.post("/api/portal/register", data=reg_form(phone="09120000000"))
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "already_registered"
        assert "قبلاً ثبت‌نام" in r.json()["error"]["message"]

    def test_a_disguised_executable_is_refused(self, client):
        r = client.post(
            "/api/portal/register",
            data=reg_form(),
            files={"med_photo": ("x.png", io.BytesIO(b"MZ\x90\x00evil"), "image/png")},
        )
        assert r.status_code == 400
        assert "med_photo" in r.json()["error"]["fields"]

    def test_oversized_upload_is_refused(self, client, monkeypatch):
        from api.config import settings

        monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", 100)
        r = client.post(
            "/api/portal/register",
            data=reg_form(),
            files={"med_photo": ("m.png", io.BytesIO(PNG + b"\x00" * 500), "image/png")},
        )
        assert r.status_code == 413

    def test_honeypot_is_absorbed(self, client):
        from api import repo
        from api.policies import SYSTEM

        r = client.post("/api/portal/register", data=reg_form(website="http://spam"))
        assert r.status_code == 201
        assert repo.count(SYSTEM, "patients") == 0


# ======================================================================
# Returning patient
# ======================================================================
class TestReturningPatient:
    def test_login_needs_both_national_id_and_birth_date(self, client, patient):
        client.cookies.clear()
        bad = client.post(
            "/api/portal/login", json={"national_id": NID_A, "birth_date": "1360/01/01"}
        )
        assert bad.status_code == 401
        ok = client.post(
            "/api/portal/login", json={"national_id": NID_A, "birth_date": "1370/05/03"}
        )
        assert ok.status_code == 200
        assert ok.json()["redirect"] == "/booking/reserve"

    def test_unknown_person_cannot_get_in(self, client, patient):
        client.cookies.clear()
        r = client.post(
            "/api/portal/login", json={"national_id": NID_B, "birth_date": "1370/05/03"}
        )
        assert r.status_code == 401

    def test_login_accepts_persian_digits_and_loose_separators(self, client, patient):
        client.cookies.clear()
        r = client.post("/api/portal/login", json={"national_id": NID_A, "birth_date": "۱۳۷۰-۵-۳"})
        assert r.status_code == 200

    def test_login_is_rate_limited(self, client, patient, monkeypatch):
        from api.config import settings

        client.cookies.clear()
        monkeypatch.setattr(settings, "RL_PATIENT_LOGIN_IP_PER_15MIN", 3)
        codes = [
            client.post(
                "/api/portal/login", json={"national_id": NID_A, "birth_date": "1300/01/01"}
            ).status_code
            for _ in range(5)
        ]
        assert 429 in codes

    def test_a_patient_only_ever_sees_their_own_appointments(self, client, patient):
        from api import repo, scheduling
        from api.policies import SYSTEM

        other = repo.insert(
            SYSTEM,
            "patients",
            {
                "public_id": "a" * 12,
                "national_id": NID_B,
                "full_name": "کس دیگر",
                "birth_date": "1980-01-01",
                "birth_jalali": "1358/10/11",
                "phone": "09125556677",
            },
        )
        scheduling.allocate(other, tomorrow(), "19:00")
        client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "20:00"}
        )
        mine = client.get("/api/portal/me").json()["appointments"]
        assert len(mine) == 1 and mine[0]["slot_time"] == "20:00"


# ======================================================================
# The slot grid
# ======================================================================
class TestSlots:
    def test_hours_are_1600_to_2200_every_thirty_minutes(self):
        from api import scheduling

        assert scheduling.SLOTS[0] == "16:00"
        assert scheduling.SLOTS[-1] == "21:30"
        assert len(scheduling.SLOTS) == 12
        assert scheduling.CABINS == 10
        assert scheduling.DAY_CAPACITY == 120

    def test_anonymous_cannot_read_the_grid(self, client):
        assert client.get("/api/portal/slots?date=" + tomorrow()).status_code == 401
        assert client.get("/api/portal/days").status_code == 401

    def test_grid_reports_free_cabins(self, client, patient):
        g = client.get("/api/portal/slots?date=" + tomorrow()).json()
        assert g["capacity"] == 120 and len(g["slots"]) == 12
        assert all(s["free"] == 10 for s in g["slots"])

    def test_cabins_are_handed_out_in_order(self, client, patient):
        from api import repo, scheduling
        from api.policies import SYSTEM

        cabins = []
        for i in range(10):
            pid = repo.insert(
                SYSTEM,
                "patients",
                {
                    "public_id": f"{i:012d}",
                    "national_id": nid(300000000 + i * 11),
                    "full_name": f"بیمار {i}",
                    "birth_date": "1980-01-01",
                    "birth_jalali": "1358/10/11",
                    "phone": f"091300000{i:02d}",
                },
            )
            cabins.append(scheduling.allocate(pid, tomorrow(), "18:00")["cabin"])
        assert sorted(cabins) == list(range(1, 11))

    def test_the_eleventh_booking_in_a_slot_is_refused(self, client, patient):
        from api import repo, scheduling
        from api.errors import Conflict
        from api.policies import SYSTEM

        for i in range(10):
            pid = repo.insert(
                SYSTEM,
                "patients",
                {
                    "public_id": f"b{i:011d}",
                    "national_id": nid(400000000 + i * 11),
                    "full_name": f"بیمار {i}",
                    "birth_date": "1980-01-01",
                    "birth_jalali": "1358/10/11",
                    "phone": f"091400000{i:02d}",
                },
            )
            scheduling.allocate(pid, tomorrow(), "18:00")
        with pytest.raises(Conflict) as exc:
            scheduling.allocate(999999, tomorrow(), "18:00")
        assert exc.value.code == "slot_full"

        r = client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "18:00"}
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "slot_full"

    def test_a_full_slot_is_marked_disabled_for_the_patient(self, client, patient):
        from api import repo, scheduling
        from api.policies import SYSTEM

        for i in range(10):
            pid = repo.insert(
                SYSTEM,
                "patients",
                {
                    "public_id": f"c{i:011d}",
                    "national_id": nid(500000000 + i * 11),
                    "full_name": f"بیمار {i}",
                    "birth_date": "1980-01-01",
                    "birth_jalali": "1358/10/11",
                    "phone": f"091500000{i:02d}",
                },
            )
            scheduling.allocate(pid, tomorrow(), "17:00")
        g = client.get("/api/portal/slots?date=" + tomorrow()).json()
        slot = next(s for s in g["slots"] if s["time"] == "17:00")
        assert slot["free"] == 0 and slot["full"] and slot["disabled"]

    @pytest.mark.parametrize("time_", ["09:00", "15:30", "22:00", "23:30"])
    def test_times_outside_clinic_hours_are_refused(self, client, patient, time_):
        r = client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": time_}
        )
        assert r.status_code == 400

    def test_the_past_is_refused(self, client, patient):
        from api.jalali import now_tehran

        past = (now_tehran().date() - dt.timedelta(days=1)).isoformat()
        r = client.post("/api/portal/appointments", json={"slot_date": past, "slot_time": "18:00"})
        assert r.status_code == 400

    def test_too_far_ahead_is_refused(self, client, patient):
        from api.jalali import now_tehran

        far = (now_tehran().date() + dt.timedelta(days=60)).isoformat()
        r = client.post("/api/portal/appointments", json={"slot_date": far, "slot_time": "18:00"})
        assert r.status_code == 400

    def test_cancelling_frees_the_cabin(self, client, patient):
        from api import repo, scheduling
        from api.policies import SYSTEM

        pid = repo.select(SYSTEM, "patients")[0]["id"]
        a = scheduling.allocate(pid, tomorrow(), "19:00")
        repo.update(
            SYSTEM, "appointments", {"status": "cancelled"}, where="id = ?", params=[a["id"]]
        )
        g = client.get("/api/portal/slots?date=" + tomorrow()).json()
        assert next(s for s in g["slots"] if s["time"] == "19:00")["free"] == 10


# ======================================================================
# Booking
# ======================================================================
class TestBooking:
    def test_booking_texts_the_patient_with_the_code(self, client, patient):
        from api import repo
        from api.policies import SYSTEM

        r = client.post(
            "/api/portal/appointments",
            json={"slot_date": tomorrow(), "slot_time": "18:30", "note": "درد زانو"},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["ok"] and body["sms_sent"] and body["cabin"] == 1
        sms = repo.select(SYSTEM, "sms_messages", where="kind = 'booked'")[0]
        assert body["code"] in sms["body"] and "18:30" in sms["body"]

    def test_the_same_patient_cannot_double_book_one_slot(self, client, patient):
        client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "18:30"}
        )
        r = client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "18:30"}
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "duplicate_booking"

    def test_booking_is_rate_limited_per_patient(self, client, patient, monkeypatch):
        from api.config import settings

        monkeypatch.setattr(settings, "RL_APPT_PATIENT_PER_DAY", 2)
        codes = [
            client.post(
                "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": t}
            ).status_code
            for t in ("16:00", "16:30", "17:00", "17:30")
        ]
        assert codes[:2] == [201, 201] and 429 in codes[2:]

    def test_anonymous_cannot_book(self, client):
        assert (
            client.post(
                "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "18:00"}
            ).status_code
            == 401
        )


# ======================================================================
# Doctor panel
# ======================================================================
class TestDoctorPanel:
    def test_staff_see_the_full_record(self, owner_client, patient):
        owner_client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "18:00"}
        )
        items = owner_client.get("/api/admin/appointments").json()["items"]
        assert items
        p = items[0]["patient"]
        assert p["full_name"] == "سارا کریمی"
        assert p["national_id"] == NID_A
        assert p["phone"] == "09121112233"
        assert p["mri_link"].startswith("https://")
        assert p["ortho_doctor"] == "دکتر رضایی"

    def test_anonymous_cannot_see_patients_or_appointments(self, client):
        for path in (
            "/api/admin/appointments",
            "/api/admin/patients",
            "/api/admin/sms",
            "/api/admin/appointments/grid",
        ):
            assert client.get(path).status_code == 401

    def test_uploads_are_not_publicly_served(self, client, patient):
        from api import repo
        from api.policies import SYSTEM

        p = repo.select(SYSTEM, "patients")[0]
        assert client.get(f"/api/admin/patients/{p['public_id']}/photo").status_code == 401
        assert client.get(f"/data/uploads/{p['med_photo']}").status_code != 200
        assert client.get(f"/uploads/{p['med_photo']}").status_code != 200

    def test_upload_path_cannot_escape_the_directory(self):
        from api.patients import upload_path

        for evil in ["../../etc/passwd", "..%2fsecret", "/etc/passwd", ".hidden", ""]:
            assert upload_path(evil) is None

    def test_reschedule_moves_the_slot_and_texts_the_patient(self, owner_client, patient):
        from api import repo
        from api.policies import SYSTEM

        book = owner_client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "18:00"}
        ).json()
        r = owner_client.patch(
            f"/api/admin/appointments/{book['code']}/move",
            json={"slot_date": tomorrow(), "slot_time": "20:00"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["sms_sent"]
        sms = repo.select(SYSTEM, "sms_messages", where="kind = 'rescheduled'")[0]
        assert "18:00" in sms["body"] and "20:00" in sms["body"]
        g = owner_client.get("/api/portal/slots?date=" + tomorrow()).json()
        assert next(s for s in g["slots"] if s["time"] == "18:00")["free"] == 10
        assert next(s for s in g["slots"] if s["time"] == "20:00")["free"] == 9

    def test_a_failed_reschedule_leaves_the_original_intact(self, owner_client, patient):
        from api import repo, scheduling
        from api.policies import SYSTEM

        book = owner_client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "18:00"}
        ).json()
        for i in range(10):  # fill the target slot
            pid = repo.insert(
                SYSTEM,
                "patients",
                {
                    "public_id": f"d{i:011d}",
                    "national_id": nid(600000000 + i * 11),
                    "full_name": f"بیمار {i}",
                    "birth_date": "1980-01-01",
                    "birth_jalali": "1358/10/11",
                    "phone": f"091600000{i:02d}",
                },
            )
            scheduling.allocate(pid, tomorrow(), "21:00")
        r = owner_client.patch(
            f"/api/admin/appointments/{book['code']}/move",
            json={"slot_date": tomorrow(), "slot_time": "21:00"},
        )
        assert r.status_code == 409
        still = repo.select(SYSTEM, "appointments", where="public_id = ?", params=[book["code"]])[0]
        assert still["status"] == "booked" and still["slot_time"] == "18:00"

    def test_next_session_is_booked_and_texted(self, owner_client, patient):
        from api import repo
        from api.policies import SYSTEM

        p = repo.select(SYSTEM, "patients")[0]
        r = owner_client.post(
            f"/api/admin/patients/{p['public_id']}/followup",
            json={"slot_date": tomorrow(), "slot_time": "19:30", "note": "جلسه دوم"},
        )
        assert r.status_code == 201, r.text
        assert r.json()["sms_sent"]
        appt = repo.select(SYSTEM, "appointments", where="kind = 'followup'")[0]
        assert appt["booked_by"] == "staff" and appt["slot_time"] == "19:30"
        assert repo.select(SYSTEM, "sms_messages", where="kind = 'followup'")

    def test_staff_writes_need_csrf(self, owner_client, patient):
        book = owner_client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "18:00"}
        ).json()
        r = owner_client.patch(
            f"/api/admin/appointments/{book['code']}/move",
            json={"slot_date": tomorrow(), "slot_time": "20:00"},
            headers={"X-CSRF-Token": "nope"},
        )
        assert r.status_code == 403

    def test_marking_attended_feeds_the_outcome_metric(self, owner_client, patient):
        from api import metrics

        book = owner_client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "18:00"}
        ).json()
        owner_client.patch(
            f"/api/admin/appointments/{book['code']}/status", json={"status": "attended"}
        )
        o = metrics.outcome(30)
        assert o["north_star"]["key"] == "session_attended"
        assert o["north_star"]["value"] == 1
        assert o["funnel"]["patient_registered"] == 1
        assert o["funnel"]["appointment_booked"] == 1


# ======================================================================
# Reminders and the inbound reply
# ======================================================================
class TestReminders:
    def test_confirmation_goes_out_before_the_appointment(self, client, patient):
        from api import reminders, repo
        from api.policies import SYSTEM

        client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "18:00"}
        )
        out = reminders.run_once(lead_min=60 * 24 * 7)
        assert out["due"] == 1 and out["sent"] == 1
        sms = repo.select(SYSTEM, "sms_messages", where="kind = 'confirm_request'")[0]
        assert "عدد 1 را ارسال کنید" in sms["body"]
        assert repo.select(SYSTEM, "appointments")[0]["confirm_sent_at"]

    def test_the_reminder_is_never_sent_twice(self, client, patient):
        from api import reminders

        client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "18:00"}
        )
        reminders.run_once(lead_min=60 * 24 * 7)
        assert reminders.run_once(lead_min=60 * 24 * 7)["sent"] == 0

    def test_nothing_is_sent_outside_the_window(self, client, patient):
        from api import reminders

        client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "21:30"}
        )
        assert reminders.run_once(lead_min=5)["due"] == 0

    @pytest.mark.parametrize("reply", ["1", "۱", "بله", "تایید", "OK"])
    def test_replying_one_marks_the_patient_as_coming(self, client, patient, reply):
        from api import reminders, repo
        from api.policies import SYSTEM

        client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "18:00"}
        )
        out = reminders.handle_inbound("09121112233", reply)
        assert out["handled"] == "confirmed"
        a = repo.select(SYSTEM, "appointments")[0]
        assert a["attendance"] == "coming" and a["confirmed_at"]

    def test_replying_no_marks_the_patient_as_not_coming(self, client, patient):
        from api import reminders, repo
        from api.policies import SYSTEM

        client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "18:00"}
        )
        assert reminders.handle_inbound("09121112233", "2")["handled"] == "declined"
        assert repo.select(SYSTEM, "appointments")[0]["attendance"] == "not_coming"

    def test_an_unknown_number_is_logged_not_crashed(self, client):
        from api import reminders, repo
        from api.policies import SYSTEM

        out = reminders.handle_inbound("09999999999", "1")
        assert out == {"matched": False, "reason": "unknown_phone"}
        assert repo.select(SYSTEM, "sms_inbound")[0]["handled"] == "unknown_phone"

    def test_the_inbound_webhook_needs_the_shared_secret(self, client, monkeypatch):
        from api.config import settings

        monkeypatch.setattr(settings, "SMS_INBOUND_SECRET", "s3cret")
        assert (
            client.post("/api/sms/inbound", json={"phone": "09121112233", "body": "1"}).status_code
            == 403
        )
        assert (
            client.post(
                "/api/sms/inbound",
                json={"phone": "09121112233", "body": "1"},
                headers={"X-SMS-Secret": "s3cret"},
            ).status_code
            == 200
        )


# ======================================================================
# SMS plumbing
# ======================================================================
class TestSms:
    def test_every_message_is_recorded_with_its_cost(self, client, patient):
        from api import budget, repo
        from api.policies import SYSTEM

        assert repo.count(SYSTEM, "sms_messages") == 1
        assert budget.spent("sms") > 0

    def test_a_gateway_failure_is_recorded_not_silently_lost(self, client, monkeypatch):
        from api import repo, sms
        from api.policies import SYSTEM

        monkeypatch.setitem(
            sms.PROVIDERS, "file", lambda p, b: (_ for _ in ()).throw(RuntimeError("gateway down"))
        )
        r = client.post("/api/portal/register", data=reg_form())
        assert r.status_code == 201
        assert r.json()["sms_sent"] is False  # the UI is told the truth
        m = repo.select(SYSTEM, "sms_messages")[0]
        assert m["status"] == "failed" and "gateway down" in m["last_error"]

    def test_a_failed_message_can_be_retried(self, client, monkeypatch):
        from api import sms

        monkeypatch.setitem(
            sms.PROVIDERS, "file", lambda p, b: (_ for _ in ()).throw(RuntimeError("down"))
        )
        client.post("/api/portal/register", data=reg_form())
        monkeypatch.setitem(sms.PROVIDERS, "file", lambda p, b: None)
        assert sms.retry_failed()["sent"] == 1

    def test_the_monthly_cap_blocks_sending(self, client, monkeypatch):
        from api import budget, repo
        from api.policies import SYSTEM

        monkeypatch.setitem(budget.CATALOG, "sms", (1.0, 1.0, "تست"))
        budget.record("sms", 1)  # cap now reached
        client.post("/api/portal/register", data=reg_form())
        m = repo.select(SYSTEM, "sms_messages")[0]
        assert m["status"] == "failed" and "سقف" in m["last_error"]

    def test_production_refuses_the_fake_sms_channel(self, monkeypatch):
        import importlib

        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("SECRET_KEY", "x" * 40)
        monkeypatch.setenv("NOTIFY_PROVIDER", "telegram")
        monkeypatch.setenv("SMS_PROVIDER", "file")
        import api.config as cfg

        with pytest.raises(RuntimeError, match="SMS_PROVIDER=file"):
            importlib.reload(cfg)
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("NOTIFY_PROVIDER", "file")
        importlib.reload(cfg)


# ======================================================================
# Pages
# ======================================================================
class TestPortalPages:
    @pytest.mark.parametrize(
        "path",
        ["/booking", "/booking/register", "/booking/login", "/booking/reserve", "/booking/done"],
    )
    def test_pages_render(self, client, path):
        r = client.get(path)
        assert r.status_code == 200
        assert "آسا فیزیو" in r.text
        assert r.headers["Cache-Control"] == "no-store"

    def test_unknown_step_redirects_home(self, client):
        assert client.get("/booking/nonsense", follow_redirects=False).status_code == 302

    def test_pages_carry_the_security_headers(self, client):
        h = client.get("/booking").headers
        assert "frame-ancestors 'none'" in h["Content-Security-Policy"]
        assert h["X-Frame-Options"] == "DENY"

    def test_no_patient_data_is_baked_into_the_html(self, client, patient):
        html = client.get("/booking/reserve").text
        assert NID_A not in html and "09121112233" not in html


# ======================================================================
# Access policy for the new tables
# ======================================================================
class TestPortalPolicies:
    def test_every_new_table_has_a_policy(self):
        from api import db
        from api.policies import missing_policies

        assert missing_policies(db.tables()) == []
        for t in ("patients", "appointments", "sms_messages", "sms_inbound", "patient_sessions"):
            assert t in db.tables()

    def test_still_no_triggers(self):
        from api import db

        assert db.assert_no_business_triggers() == []

    def test_anonymous_cannot_read_patients_through_the_repo(self):
        from api import repo
        from api.errors import Forbidden
        from api.policies import ANON

        with pytest.raises(Forbidden):
            repo.select(ANON, "patients")
        with pytest.raises(Forbidden):
            repo.select(ANON, "appointments")

    def test_the_database_itself_rejects_a_double_booked_cabin(self):
        import sqlite3

        from api import db, repo, scheduling
        from api.policies import SYSTEM

        pid = repo.insert(
            SYSTEM,
            "patients",
            {
                "public_id": "z" * 12,
                "national_id": nid(700000001),
                "full_name": "تست قفل",
                "birth_date": "1980-01-01",
                "birth_jalali": "1358/10/11",
                "phone": "09170000001",
            },
        )
        a = scheduling.allocate(pid, tomorrow(), "18:00")
        with pytest.raises(sqlite3.IntegrityError):
            db.get_conn().execute(
                "INSERT INTO appointments(public_id, patient_id, slot_date, slot_time, cabin)"
                " VALUES ('dupdupdup1', ?, ?, '18:00', ?)",
                (pid, tomorrow(), a["cabin"]),
            )

    def test_the_database_rejects_an_out_of_range_cabin(self):
        import sqlite3

        from api import db

        with pytest.raises(sqlite3.IntegrityError):
            db.get_conn().execute(
                "INSERT INTO appointments(public_id, patient_id, slot_date, slot_time, cabin)"
                " VALUES ('dupdupdup2', 1, '2026-10-05', '18:00', 11)"
            )


# ======================================================================
# Clinical record: catalogues, treatment history, MRI, welcome-back
# ======================================================================
class TestCatalog:
    def test_seeded_with_real_physiotherapy_entries(self, owner_client):
        c = owner_client.get("/api/admin/catalog").json()
        names = [i["name"] for g in c["body_parts"] for i in g["items"]]
        treats = [t["name"] for t in c["treatments"]]
        assert len(names) >= 50 and len(treats) >= 20
        for expected in ("زانو", "دیسک کمر", "سیاتیک", "شانه منجمد", "خار پاشنه"):
            assert any(expected in n for n in names), expected
        for expected in ("لیزر پرتوان", "شاک ویو", "تکار تراپی", "طب سوزنی"):
            assert any(expected in t for t in treats), expected

    def test_grouped_by_category(self, owner_client):
        c = owner_client.get("/api/admin/catalog").json()
        labels = [g["label"] for g in c["body_parts"]]
        assert "ستون فقرات" in labels and "اندام تحتانی" in labels and "عصبی" in labels

    def test_staff_can_add_a_missing_entry_once_and_it_stays(self, owner_client):
        r = owner_client.post(
            "/api/admin/catalog/body-parts",
            json={"name": "سندرم خروجی قفسه سینه", "category": "neuro"},
        )
        assert r.status_code == 201, r.text
        new_id = r.json()["id"]
        c = owner_client.get("/api/admin/catalog").json()
        found = [i for g in c["body_parts"] for i in g["items"] if i["id"] == new_id]
        assert found and found[0]["name"] == "سندرم خروجی قفسه سینه"
        assert found[0]["builtin"] is False

    def test_adding_a_treatment_works_the_same_way(self, owner_client):
        r = owner_client.post("/api/admin/catalog/treatments", json={"name": "اوزون تراپی"})
        assert r.status_code == 201
        c = owner_client.get("/api/admin/catalog").json()
        assert any(t["name"] == "اوزون تراپی" for t in c["treatments"])

    def test_duplicates_are_refused(self, owner_client):
        owner_client.post("/api/admin/catalog/treatments", json={"name": "اوزون تراپی"})
        r = owner_client.post("/api/admin/catalog/treatments", json={"name": " اوزون تراپی "})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "catalog_duplicate"

    def test_catalog_input_is_sanitised(self, owner_client):
        r = owner_client.post(
            "/api/admin/catalog/treatments", json={"name": "<script>alert(1)</script>"}
        )
        assert r.status_code == 422

    def test_anonymous_cannot_read_or_extend_the_catalog(self, client):
        assert client.get("/api/admin/catalog").status_code == 401
        assert client.post("/api/admin/catalog/treatments", json={"name": "x y"}).status_code == 401

    def test_adding_needs_csrf(self, owner_client):
        r = owner_client.post(
            "/api/admin/catalog/treatments",
            json={"name": "تست سی‌اس"},
            headers={"X-CSRF-Token": "nope"},
        )
        assert r.status_code == 403


class TestTreatmentHistory:
    def _ids(self, client):
        c = client.get("/api/admin/catalog").json()
        bp = next(i["id"] for g in c["body_parts"] for i in g["items"] if "زانو" in i["name"])
        tx = [t["id"] for t in c["treatments"] if t["name"] in ("لیزر پرتوان", "شاک ویو")]
        return bp, tx

    def test_doctor_records_what_was_done(self, owner_client, patient):
        from api import repo
        from api.policies import SYSTEM

        bp, tx = self._ids(owner_client)
        p = repo.select(SYSTEM, "patients")[0]
        book = owner_client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "18:00"}
        ).json()
        r = owner_client.post(
            f"/api/admin/patients/{p['public_id']}/sessions",
            json={
                "body_part_ids": [bp],
                "treatment_ids": tx,
                "findings": "محدودیت دامنه حرکتی زانوی راست",
                "plan": "ادامه لیزر، سه جلسه",
                "pain_before": 8,
                "pain_after": 4,
                "appointment_id": book["code"],
                "mark_attended": True,
            },
        )
        assert r.status_code == 201, r.text

        h = owner_client.get(f"/api/admin/patients/{p['public_id']}/sessions").json()
        assert h["summary"]["sessions"] == 1
        rec = h["history"][0]
        assert "زانو" in rec["body_parts"][0]
        assert set(rec["treatments"]) == {"لیزر پرتوان", "شاک ویو"}
        assert rec["pain_before"] == 8 and rec["pain_after"] == 4
        assert rec["findings"].startswith("محدودیت")

    def test_recording_marks_the_appointment_attended(self, owner_client, patient):
        from api import metrics, repo
        from api.policies import SYSTEM

        bp, tx = self._ids(owner_client)
        p = repo.select(SYSTEM, "patients")[0]
        book = owner_client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "18:00"}
        ).json()
        owner_client.post(
            f"/api/admin/patients/{p['public_id']}/sessions",
            json={
                "body_part_ids": [bp],
                "treatment_ids": tx[:1],
                "appointment_id": book["code"],
                "mark_attended": True,
            },
        )
        a = repo.select(SYSTEM, "appointments", where="public_id = ?", params=[book["code"]])[0]
        assert a["status"] == "attended"
        sessions = repo.select(SYSTEM, "treatment_sessions", where="appointment_id = ?", params=[a["id"]])
        assert len(sessions) == 1
        assert sessions[0]["patient_id"] == a["patient_id"]
        assert metrics.outcome(30)["north_star"]["value"] == 1

    def test_a_session_needs_both_an_area_and_a_treatment(self, owner_client, patient):
        from api import repo
        from api.policies import SYSTEM

        bp, tx = self._ids(owner_client)
        p = repo.select(SYSTEM, "patients")[0]
        assert (
            owner_client.post(
                f"/api/admin/patients/{p['public_id']}/sessions",
                json={"body_part_ids": [bp], "treatment_ids": []},
            ).status_code
            == 422
        )
        assert (
            owner_client.post(
                f"/api/admin/patients/{p['public_id']}/sessions",
                json={"body_part_ids": [], "treatment_ids": tx},
            ).status_code
            == 422
        )

    def test_an_unknown_catalog_id_is_refused(self, owner_client, patient):
        from api import repo
        from api.policies import SYSTEM

        p = repo.select(SYSTEM, "patients")[0]
        r = owner_client.post(
            f"/api/admin/patients/{p['public_id']}/sessions",
            json={"body_part_ids": [999999], "treatment_ids": [999999]},
        )
        assert r.status_code == 400

    def test_history_is_newest_first_and_shows_the_gap(self, owner_client, patient):
        from api import clinical, repo
        from api.policies import SYSTEM, Actor

        bp, tx = self._ids(owner_client)
        p = repo.select(SYSTEM, "patients")[0]
        actor = Actor("owner", 1)
        clinical.record_session(
            actor, p["id"], body_part_ids=[bp], treatment_ids=tx[:1], session_date="2026-01-10"
        )
        clinical.record_session(
            actor, p["id"], body_part_ids=[bp], treatment_ids=tx[:1], session_date="2026-03-02"
        )
        h = owner_client.get(f"/api/admin/patients/{p['public_id']}/sessions").json()["history"]
        assert [x["session_date"] for x in h] == ["2026-03-02", "2026-01-10"]
        assert h[0]["gap_days"] == 51

    def test_history_is_staff_only(self, client, patient):
        from api import repo
        from api.policies import SYSTEM

        p = repo.select(SYSTEM, "patients")[0]
        assert client.get(f"/api/admin/patients/{p['public_id']}/sessions").status_code == 401

    def test_optional_sms_summarises_the_session(self, owner_client, patient):
        from api import repo
        from api.policies import SYSTEM

        bp, tx = self._ids(owner_client)
        p = repo.select(SYSTEM, "patients")[0]
        owner_client.post(
            f"/api/admin/patients/{p['public_id']}/sessions",
            json={"body_part_ids": [bp], "treatment_ids": tx, "notify": True},
        )
        m = repo.select(SYSTEM, "sms_messages", where="kind = 'session_logged'")
        assert m and "لیزر پرتوان" in m[0]["body"]


class TestMriFile:
    def test_patient_can_attach_the_mri_at_registration(self, client):
        from api import repo
        from api.config import settings
        from api.policies import SYSTEM

        r = client.post(
            "/api/portal/register",
            data=reg_form(),
            files={"mri_file": ("mri.png", io.BytesIO(PNG), "image/png")},
        )
        assert r.status_code == 201, r.text
        p = repo.select(SYSTEM, "patients")[0]
        assert p["mri_file"] and (settings.UPLOAD_DIR / p["mri_file"]).is_file()

    def test_staff_can_upload_and_view_it(self, owner_client, patient):
        from api import repo
        from api.policies import SYSTEM

        p = repo.select(SYSTEM, "patients")[0]
        r = owner_client.post(
            f"/api/admin/patients/{p['public_id']}/mri",
            files={"mri_file": ("scan.png", io.BytesIO(PNG), "image/png")},
        )
        assert r.status_code == 201, r.text
        view = owner_client.get(f"/api/admin/patients/{p['public_id']}/mri")
        assert view.status_code == 200
        assert view.headers["content-type"].startswith("image/")
        assert view.headers["Cache-Control"] == "private, no-store"

    def test_the_mri_is_never_public(self, client, owner_client, patient):
        from api import repo
        from api.policies import SYSTEM

        p = repo.select(SYSTEM, "patients")[0]
        owner_client.post(
            f"/api/admin/patients/{p['public_id']}/mri",
            files={"mri_file": ("scan.png", io.BytesIO(PNG), "image/png")},
        )
        client.cookies.clear()
        assert client.get(f"/api/admin/patients/{p['public_id']}/mri").status_code == 401

    def test_a_disguised_file_is_refused(self, owner_client, patient):
        from api import repo
        from api.policies import SYSTEM

        p = repo.select(SYSTEM, "patients")[0]
        r = owner_client.post(
            f"/api/admin/patients/{p['public_id']}/mri",
            files={"mri_file": ("x.png", io.BytesIO(b"MZ\x90evil"), "image/png")},
        )
        assert r.status_code == 400


class TestWelcomeBack:
    def test_a_patient_returning_after_a_month_is_thanked(self, client, patient):
        from api import repo, scheduling
        from api.jalali import now_tehran
        from api.policies import SYSTEM

        p = repo.select(SYSTEM, "patients")[0]
        old = (now_tehran().date() - dt.timedelta(days=70)).isoformat()
        a = scheduling.allocate(p["id"], tomorrow(), "16:00")
        repo.update(SYSTEM, "appointments", {"slot_date": old}, where="id = ?", params=[a["id"]])

        r = client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "19:00"}
        )
        assert r.status_code == 201, r.text
        assert r.json()["welcomed_back"] is True
        assert r.json()["gap_days"] >= 30
        m = repo.select(SYSTEM, "sms_messages", where="kind = 'welcome_back'")
        assert m and "سپاسگزاریم" in m[0]["body"]

    def test_a_regular_weekly_patient_is_not_thanked(self, client, patient):
        from api import repo, scheduling
        from api.jalali import now_tehran
        from api.policies import SYSTEM

        p = repo.select(SYSTEM, "patients")[0]
        recent = (now_tehran().date() - dt.timedelta(days=5)).isoformat()
        a = scheduling.allocate(p["id"], tomorrow(), "16:00")
        repo.update(SYSTEM, "appointments", {"slot_date": recent}, where="id = ?", params=[a["id"]])
        r = client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "19:00"}
        )
        assert r.json()["welcomed_back"] is False
        assert repo.count(SYSTEM, "sms_messages", where="kind = 'welcome_back'") == 0

    def test_a_brand_new_patient_is_not_thanked_for_returning(self, client, patient):
        from api import repo
        from api.policies import SYSTEM

        r = client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "19:00"}
        )
        assert r.json()["gap_days"] is None
        assert repo.count(SYSTEM, "sms_messages", where="kind = 'welcome_back'") == 0

    def test_a_treatment_session_also_counts_as_a_visit(self, patient):
        from api import clinical, repo
        from api.jalali import now_tehran
        from api.policies import SYSTEM, Actor

        p = repo.select(SYSTEM, "patients")[0]
        long_ago = (now_tehran().date() - dt.timedelta(days=200)).isoformat()
        c = clinical.catalog(Actor("owner", 1))
        bp = c["body_parts"][0]["items"][0]["id"]
        tx = c["treatments"][0]["id"]
        clinical.record_session(
            Actor("owner", 1),
            p["id"],
            body_part_ids=[bp],
            treatment_ids=[tx],
            session_date=long_ago,
        )
        gap = clinical.returning_after_gap(p["id"], tomorrow())
        assert gap is not None and gap > 190

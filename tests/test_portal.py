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
    # Ensure a sole active owner exists for portal auto-attach — without logging
    # in as staff (tests that only use `patient` must stay patient-scoped).
    from api import auth, repo
    from api.policies import SYSTEM

    owners = repo.select(
        SYSTEM, "admin_users", where="role = 'owner' AND is_active = 1", limit=2
    )
    if not owners:
        auth.create_user(
            "t_owner", "a-very-long-test-password", role="owner", full_name="پزشک تست"
        )
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

    def test_mri_url_is_saved_as_private_file_when_image_download_succeeds(
        self, client, monkeypatch
    ):
        from api import repo
        from api.policies import SYSTEM

        async def fake_download(url):
            assert url == "https://cdn.example/mri.png"
            return PNG, "image/png"

        monkeypatch.setattr("api.routes_booking._download_remote_mri", fake_download)
        r = client.post(
            "/api/portal/register",
            data=reg_form(national_id=NID_B, mri_link="https://cdn.example/mri.png"),
        )
        assert r.status_code == 201, r.text
        patient = repo.select(SYSTEM, "patients", where="national_id = ?", params=[NID_B])[0]
        assert patient["mri_file"].endswith(".png")
        assert r.json()["mri_warning"] == ""

    def test_mri_download_failure_does_not_abort_registration(self, client, monkeypatch):
        from api import repo
        from api.errors import BadRequest
        from api.policies import SYSTEM

        async def failed_download(url):
            raise BadRequest("لینک MRI قابل دریافت نیست.", code="mri_download_failed")

        monkeypatch.setattr("api.routes_booking._download_remote_mri", failed_download)
        r = client.post(
            "/api/portal/register",
            data=reg_form(national_id=NID_B, mri_link="https://cdn.example/missing.png"),
        )
        assert r.status_code == 201, r.text
        assert "قابل دریافت" in r.json()["mri_warning"]
        patient = repo.select(SYSTEM, "patients", where="national_id = ?", params=[NID_B])[0]
        assert patient["mri_file"] == ""

    def test_mri_private_url_is_rejected(self):
        from api.errors import BadRequest
        from api.routes_booking import _public_mri_url

        with pytest.raises(BadRequest):
            _public_mri_url("http://127.0.0.1/mri.png")

    @pytest.mark.parametrize(
        "host,ip",
        [
            ("loop.example", "127.0.0.1"),
            ("lan.example", "10.0.0.5"),
            ("cgnat.example", "100.64.1.2"),
            ("meta.example", "169.254.169.254"),
            ("v6loop.example", "::1"),
            ("v6ula.example", "fc00::1"),
        ],
    )
    def test_mri_pin_rejects_blocked_resolved_ips(self, monkeypatch, host, ip):
        import socket

        from api.errors import BadRequest
        from api.routes_booking import _pin_public_mri_url

        def fake_gai(hostname, port, *a, **kw):
            assert hostname == host
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            sockaddr = (ip, port, 0, 0) if ":" in ip else (ip, port)
            return [(family, socket.SOCK_STREAM, 0, "", sockaddr)]

        monkeypatch.setattr("api.routes_booking.socket.getaddrinfo", fake_gai)
        with pytest.raises(BadRequest) as ei:
            _pin_public_mri_url(f"https://{host}/mri.png")
        assert ei.value.code == "blocked_mri_url"

    def test_mri_pin_accepts_public_ipv4_and_ipv6(self, monkeypatch):
        import socket

        from api.routes_booking import _pin_public_mri_url

        def fake_gai(hostname, port, *a, **kw):
            return [
                (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("2606:4700::1", port, 0, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.1.1.1", port)),
            ]

        monkeypatch.setattr("api.routes_booking.socket.getaddrinfo", fake_gai)
        url, host, pinned = _pin_public_mri_url("https://cdn.example/mri.png")
        assert url.endswith("/mri.png") and host == "cdn.example"
        assert pinned == "1.1.1.1"  # IPv4 preferred when both are public

    def test_mri_dns_rebinding_cannot_bypass_ip_pin(self, monkeypatch):
        """Resolve once to a public IP and dial that IP even if DNS later flips to loopback."""
        import asyncio
        import socket

        import httpcore

        from api.errors import BadRequest
        from api.routes_booking import _pin_public_mri_url, _PinnedIPBackend

        answers = iter([["8.8.8.8"], ["127.0.0.1"]])

        def fake_gai(hostname, port, *a, **kw):
            ip = next(answers)[0]
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, port))]

        monkeypatch.setattr("api.routes_booking.socket.getaddrinfo", fake_gai)
        _url, _host, pinned = _pin_public_mri_url("https://rebind.example/a.png")
        assert pinned == "8.8.8.8"
        dialed: list[str] = []

        class RecordingInner(httpcore.AsyncNetworkBackend):
            async def connect_tcp(
                self, host, port, timeout=None, local_address=None, socket_options=None
            ):
                dialed.append(host)
                raise OSError("stop-after-dial")

            async def connect_unix_socket(self, path, timeout=None, socket_options=None):
                raise OSError("no uds")

            async def sleep(self, seconds):
                return None

        backend = _PinnedIPBackend(pinned)
        backend._inner = RecordingInner()  # noqa: SLF001

        with pytest.raises(OSError, match="stop-after-dial"):
            asyncio.run(backend.connect_tcp("rebind.example", 443))
        assert dialed == ["8.8.8.8"]
        # Second DNS answer is loopback — validation must fail closed.
        with pytest.raises(BadRequest) as ei:
            _pin_public_mri_url("https://rebind.example/a.png")
        assert ei.value.code == "blocked_mri_url"

    def test_mri_redirect_to_private_ip_is_rejected(self, monkeypatch):
        import asyncio
        import socket

        from api.errors import BadRequest
        from api.routes_booking import _download_remote_mri

        def fake_gai(hostname, port, *a, **kw):
            ip = "1.1.1.1" if hostname == "cdn.example" else "10.1.2.3"
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, port))]
        monkeypatch.setattr("api.routes_booking.socket.getaddrinfo", fake_gai)

        class FakeStream:
            status_code = 302
            headers = {"location": "https://evil.internal/secret.png"}

            async def aiter_bytes(self, chunk_size=0):
                if False:  # pragma: no cover
                    yield b""

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def stream(self, *a, **k):
                return FakeStream()

        monkeypatch.setattr("api.routes_booking.httpx.AsyncClient", FakeClient)
        monkeypatch.setattr("api.routes_booking._pinned_transport", lambda ip: object())

        with pytest.raises(BadRequest) as ei:
            asyncio.run(_download_remote_mri("https://cdn.example/mri.png"))
        assert ei.value.code == "blocked_mri_url"

    def test_mri_download_failure_still_registers_with_warning(self, client, monkeypatch):
        from api.errors import BadRequest

        async def boom(_url: str):
            raise BadRequest(
                "دریافت از شبکه داخلی یا آدرس خصوصی مجاز نیست.", code="blocked_mri_url"
            )

        monkeypatch.setattr("api.routes_booking._download_remote_mri", boom)
        r = client.post(
            "/api/portal/register",
            data=reg_form(national_id=NID_B, mri_link="https://cdn.example/x.png"),
        )
        assert r.status_code == 201, r.text
        assert "داخلی" in r.json()["mri_warning"] or "خصوصی" in r.json()["mri_warning"]
        assert client.get("/api/portal/me").status_code == 200

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

        monkeypatch.setattr(settings, "MEDS_UPLOAD_MAX_BYTES", 100)
        r = client.post(
            "/api/portal/register",
            data=reg_form(),
            files={"med_photo": ("m.png", io.BytesIO(PNG + b"\x00" * 500), "image/png")},
        )
        assert r.status_code == 413

    def test_medication_upload_uses_exact_two_megabyte_limit(self, client, monkeypatch):
        from api.config import settings

        monkeypatch.setattr(settings, "MEDS_UPLOAD_MAX_BYTES", 2 * 1024 * 1024)
        oversized = PNG + b"\\x00" * (2 * 1024 * 1024)
        r = client.post(
            "/api/portal/register",
            data=reg_form(national_id=NID_B),
            files={"med_photo": ("m.png", io.BytesIO(oversized), "image/png")},
        )
        assert r.status_code == 413
        assert "2 مگابایت" in r.json()["error"]["message"]

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
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "20:30"}
        )
        mine = client.get("/api/portal/me").json()["appointments"]
        assert len(mine) == 1 and mine[0]["slot_time"] == "20:30"


# ======================================================================
# The slot grid
# ======================================================================
class TestSlots:
    def test_hours_are_1600_to_2200_every_ninety_minutes(self):
        from api import scheduling

        assert scheduling.SLOTS[0] == "16:00"
        assert scheduling.SLOTS == ["16:00", "17:30", "19:00", "20:30"]
        assert len(scheduling.SLOTS) == 4
        assert scheduling.CABINS == 10
        assert scheduling.DAY_CAPACITY == 40

    def test_anonymous_cannot_read_the_grid(self, client):
        assert client.get("/api/portal/slots?date=" + tomorrow()).status_code == 401
        assert client.get("/api/portal/days").status_code == 401

    def test_grid_reports_free_cabins(self, client, patient):
        g = client.get("/api/portal/slots?date=" + tomorrow()).json()
        assert g["capacity"] == 40 and len(g["slots"]) == 4
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
            cabins.append(scheduling.allocate(pid, tomorrow(), "16:00")["cabin"])
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
            scheduling.allocate(pid, tomorrow(), "16:00")
        with pytest.raises(Conflict) as exc:
            scheduling.allocate(999999, tomorrow(), "16:00")
        assert exc.value.code == "slot_full"

        r = client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "16:00"}
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
            scheduling.allocate(pid, tomorrow(), "17:30")
        g = client.get("/api/portal/slots?date=" + tomorrow()).json()
        slot = next(s for s in g["slots"] if s["time"] == "17:30")
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
        r = client.post("/api/portal/appointments", json={"slot_date": past, "slot_time": "16:00"})
        assert r.status_code == 400

    def test_too_far_ahead_is_refused(self, client, patient):
        from api.jalali import now_tehran

        far = (now_tehran().date() + dt.timedelta(days=60)).isoformat()
        r = client.post("/api/portal/appointments", json={"slot_date": far, "slot_time": "16:00"})
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

    def test_rebook_same_slot_and_cabin_after_cancel(self, client, patient):
        """C1: cancelled history stays; the same cabin can be booked again."""
        from api import repo
        from api.policies import SYSTEM

        day, slot = tomorrow(), "19:00"
        first = client.post(
            "/api/portal/appointments", json={"slot_date": day, "slot_time": slot}
        )
        assert first.status_code == 201, first.text
        cabin = first.json()["cabin"]
        code = first.json()["code"]

        cancelled = client.post(f"/api/portal/appointments/{code}/cancel")
        assert cancelled.status_code == 200, cancelled.text

        again = client.post(
            "/api/portal/appointments", json={"slot_date": day, "slot_time": slot}
        )
        assert again.status_code == 201, again.text
        assert again.json()["cabin"] == cabin
        assert again.json()["code"] != code

        rows = repo.select(
            SYSTEM,
            "appointments",
            where="slot_date = ? AND slot_time = ? AND cabin = ?",
            params=[day, slot, cabin],
        )
        assert len(rows) == 2
        by_status = {r["status"] for r in rows}
        assert by_status == {"cancelled", "booked"}
        assert {r["public_id"] for r in rows} == {code, again.json()["code"]}


# ======================================================================
# Patient cancel (portal)
# ======================================================================
class TestPatientCancel:
    def _book(self, client, slot_time: str = "19:00") -> dict:
        r = client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": slot_time}
        )
        assert r.status_code == 201, r.text
        return r.json()

    def test_cancel_sets_status_and_updated_at(self, client, patient):
        from api import repo
        from api.policies import SYSTEM

        booked = self._book(client)
        code = booked["code"]
        before = repo.select(SYSTEM, "appointments", where="public_id = ?", params=[code])[0]
        repo.update(
            SYSTEM,
            "appointments",
            {"updated_at": "2020-01-01T00:00:00Z"},
            where="id = ?",
            params=[before["id"]],
        )

        r = client.post(f"/api/portal/appointments/{code}/cancel")
        assert r.status_code == 200, r.text
        after = repo.select(SYSTEM, "appointments", where="public_id = ?", params=[code])[0]
        assert after["status"] == "cancelled"
        assert after["updated_at"] > "2020-01-01T00:00:00Z"
        assert after["updated_at"].endswith("Z")

    def test_duplicate_cancel_is_conflict(self, client, patient):
        code = self._book(client)["code"]
        assert client.post(f"/api/portal/appointments/{code}/cancel").status_code == 200
        r = client.post(f"/api/portal/appointments/{code}/cancel")
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "conflict"

    @pytest.mark.parametrize("status", ["attended", "no_show"])
    def test_non_booked_status_cannot_be_cancelled(self, client, patient, status):
        from api import repo
        from api.policies import SYSTEM

        code = self._book(client)["code"]
        repo.update(
            SYSTEM, "appointments", {"status": status}, where="public_id = ?", params=[code]
        )
        r = client.post(f"/api/portal/appointments/{code}/cancel")
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "conflict"
        assert (
            repo.select(SYSTEM, "appointments", where="public_id = ?", params=[code])[0]["status"]
            == status
        )

    def test_cancel_without_auth_is_unauthorized(self, client, patient):
        code = self._book(client)["code"]
        client.cookies.clear()
        assert client.post(f"/api/portal/appointments/{code}/cancel").status_code == 401

    def test_cannot_cancel_another_patients_appointment(self, client, patient):
        code = self._book(client)["code"]
        client.cookies.clear()
        other = client.post(
            "/api/portal/register",
            data=reg_form(national_id=NID_B, phone="۰۹۱۲۳۳۳۴۴۵۵", full_name="بیمار دیگر"),
            files={"med_photo": ("m.png", io.BytesIO(PNG), "image/png")},
        )
        assert other.status_code == 201, other.text
        assert client.post(f"/api/portal/appointments/{code}/cancel").status_code == 404

    def test_cancel_inside_three_hours_is_forbidden(self, client, patient):
        from api import repo
        from api.jalali import now_tehran
        from api.policies import SYSTEM

        code = self._book(client)["code"]
        soon = now_tehran() + dt.timedelta(hours=2)
        repo.update(
            SYSTEM,
            "appointments",
            {
                "slot_date": soon.date().isoformat(),
                "slot_time": soon.strftime("%H:%M"),
            },
            where="public_id = ?",
            params=[code],
        )
        r = client.post(f"/api/portal/appointments/{code}/cancel")
        assert r.status_code == 403
        assert (
            repo.select(SYSTEM, "appointments", where="public_id = ?", params=[code])[0]["status"]
            == "booked"
        )

    def test_cancel_does_not_send_sms(self, client, patient):
        from api import repo
        from api.policies import SYSTEM

        code = self._book(client)["code"]
        before = repo.count(SYSTEM, "sms_messages")
        assert client.post(f"/api/portal/appointments/{code}/cancel").status_code == 200
        assert repo.count(SYSTEM, "sms_messages") == before
        assert repo.count(SYSTEM, "sms_messages", where="kind = 'cancelled'") == 0


# ======================================================================
# Booking
# ======================================================================
class TestBooking:
    def test_booking_texts_the_patient_with_the_code(self, client, patient):
        from api import repo
        from api.policies import SYSTEM

        r = client.post(
            "/api/portal/appointments",
            json={"slot_date": tomorrow(), "slot_time": "17:30", "note": "درد زانو"},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["ok"] and body["sms_sent"] and body["cabin"] == 1
        sms = repo.select(SYSTEM, "sms_messages", where="kind = 'booked'")[0]
        assert body["code"] in sms["body"] and "17:30" in sms["body"]

    def test_the_same_patient_cannot_double_book_one_slot(self, client, patient):
        client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "17:30"}
        )
        r = client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "17:30"}
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
            for t in ("16:00", "17:30", "19:00", "20:30")
        ]
        assert codes[:2] == [201, 201] and 429 in codes[2:]

    def test_anonymous_cannot_book(self, client):
        assert (
            client.post(
                "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "16:00"}
            ).status_code
            == 401
        )


# ======================================================================
# Doctor panel
# ======================================================================
class TestDoctorPanel:
    def test_staff_see_the_full_record(self, owner_client, patient):
        owner_client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "16:00"}
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
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "16:00"}
        ).json()
        r = owner_client.patch(
            f"/api/admin/appointments/{book['code']}/move",
            json={"slot_date": tomorrow(), "slot_time": "20:30"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["sms_sent"]
        sms = repo.select(SYSTEM, "sms_messages", where="kind = 'rescheduled'")[0]
        assert "16:00" in sms["body"] and "20:30" in sms["body"]
        g = owner_client.get("/api/portal/slots?date=" + tomorrow()).json()
        assert next(s for s in g["slots"] if s["time"] == "16:00")["free"] == 10
        assert next(s for s in g["slots"] if s["time"] == "20:30")["free"] == 9

    def test_a_failed_reschedule_leaves_the_original_intact(self, owner_client, patient):
        from api import repo, scheduling
        from api.policies import SYSTEM

        book = owner_client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "16:00"}
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
            scheduling.allocate(pid, tomorrow(), "20:30")
        r = owner_client.patch(
            f"/api/admin/appointments/{book['code']}/move",
            json={"slot_date": tomorrow(), "slot_time": "20:30"},
        )
        assert r.status_code == 409
        still = repo.select(SYSTEM, "appointments", where="public_id = ?", params=[book["code"]])[0]
        assert still["status"] == "booked" and still["slot_time"] == "16:00"

    def test_failed_allocate_rolls_back_without_manual_restore(
        self, owner_client, patient, monkeypatch
    ):
        """Cancel+insert share one tx: a mid-flight failure must not leave cancelled."""
        from api import repo, scheduling
        from api.errors import Conflict
        from api.policies import SYSTEM

        book = owner_client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "16:00"}
        ).json()
        before = repo.select(SYSTEM, "appointments", where="public_id = ?", params=[book["code"]])[
            0
        ]

        def boom(conn, *a, **k):
            raise Conflict("پر شد", code="slot_full")

        monkeypatch.setattr(scheduling, "allocate_on_conn", boom)
        r = owner_client.patch(
            f"/api/admin/appointments/{book['code']}/move",
            json={"slot_date": tomorrow(), "slot_time": "20:30", "notify": False},
        )
        assert r.status_code == 409
        after = repo.select(SYSTEM, "appointments", where="public_id = ?", params=[book["code"]])[0]
        assert after["status"] == "booked"
        assert after["slot_time"] == before["slot_time"]
        assert after["cabin"] == before["cabin"]
        assert (
            repo.count(
                SYSTEM,
                "appointments",
                where="status = 'cancelled' AND patient_id = ?",
                params=[before["patient_id"]],
            )
            == 0
        )

    def test_dest_cabin_integrity_error_becomes_conflict(
        self, owner_client, patient, monkeypatch
    ):
        import sqlite3

        from api import repo, scheduling
        from api.policies import SYSTEM

        book = owner_client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "16:00"}
        ).json()

        def boom(conn, *a, **k):
            raise sqlite3.IntegrityError("UNIQUE constraint failed: idx_appt_active_cabin")

        monkeypatch.setattr(scheduling, "allocate_on_conn", boom)
        r = owner_client.patch(
            f"/api/admin/appointments/{book['code']}/move",
            json={"slot_date": tomorrow(), "slot_time": "19:00", "notify": False},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "slot_full"
        still = repo.select(SYSTEM, "appointments", where="public_id = ?", params=[book["code"]])[0]
        assert still["status"] == "booked" and still["slot_time"] == "16:00"

    def test_reschedule_real_unique_index_conflict_returns_409(self, owner_client, patient, monkeypatch):
        """Real SQLite partial UNIQUE hit inside allocate_on_conn → HTTP 409; source stays booked.

        Under BEGIN IMMEDIATE, two writers cannot interleave SELECT+INSERT, so a true
        check-then-act race is not reproducible. Instead we keep the real
        allocate_on_conn body and only wrap the connection so the cabin SELECT
        returns empty while cabin 1 is already taken — the subsequent INSERT
        collides with idx_appt_active_cabin on the live database.
        """
        from api import repo, scheduling
        from api.errors import Conflict
        from api.policies import SYSTEM

        class _StaleActiveCabinConn:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, parameters=()):
                text = sql if isinstance(sql, str) else str(sql)
                if (
                    "SELECT cabin FROM appointments" in text
                    and "status != 'cancelled'" in text
                ):

                    class _Empty:
                        def fetchall(self_inner):
                            return []

                    return _Empty()
                return self._conn.execute(sql, parameters)

        book = owner_client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "16:00"}
        ).json()
        source = repo.select(SYSTEM, "appointments", where="public_id = ?", params=[book["code"]])[
            0
        ]

        other_pid = repo.insert(
            SYSTEM,
            "patients",
            {
                "public_id": "z" * 12,
                "national_id": nid(700000099),
                "full_name": "رقیب کابین",
                "birth_date": "1980-01-01",
                "birth_jalali": "1358/10/11",
                "phone": "09170000099",
            },
        )
        dest = scheduling.allocate(other_pid, tomorrow(), "19:00")
        assert dest["cabin"] == 1

        real_allocate = scheduling.allocate_on_conn

        def allocate_via_stale_read(conn, *args, **kwargs):
            return real_allocate(_StaleActiveCabinConn(conn), *args, **kwargs)

        monkeypatch.setattr(scheduling, "allocate_on_conn", allocate_via_stale_read)

        # Direct reschedule path (same TX as HTTP move)
        with pytest.raises(Conflict) as raised:
            scheduling.reschedule(source, tomorrow(), "19:00", booked_by="staff")
        assert raised.value.code == "slot_full"
        import sqlite3 as _sqlite3

        assert isinstance(raised.value.__cause__, _sqlite3.IntegrityError)

        after_direct = repo.select(
            SYSTEM, "appointments", where="public_id = ?", params=[book["code"]]
        )[0]
        assert after_direct["status"] == "booked" and after_direct["slot_time"] == "16:00"
        assert after_direct["cabin"] == source["cabin"]

        r = owner_client.patch(
            f"/api/admin/appointments/{book['code']}/move",
            json={"slot_date": tomorrow(), "slot_time": "19:00", "notify": False},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "slot_full"
        assert "UNIQUE" not in r.text and "idx_appt" not in r.text
        still = repo.select(SYSTEM, "appointments", where="public_id = ?", params=[book["code"]])[0]
        assert still["status"] == "booked" and still["slot_time"] == "16:00"
        assert (
            repo.count(
                SYSTEM,
                "appointments",
                where="status = 'cancelled' AND public_id = ?",
                params=[book["code"]],
            )
            == 0
        )

    def test_partial_unique_rejects_duplicate_cabin_on_independent_connections(self, patient):
        """Two separate sqlite connections: first commits cabin 1; second INSERT hits UNIQUE."""
        import sqlite3

        from api import repo
        from api.db import connect
        from api.policies import SYSTEM

        day, slot = tomorrow(), "20:30"
        pid = repo.select(SYSTEM, "patients")[0]["id"]
        other = repo.insert(
            SYSTEM,
            "patients",
            {
                "public_id": "w" * 12,
                "national_id": nid(700000088),
                "full_name": "اتصال دوم",
                "birth_date": "1980-01-01",
                "birth_jalali": "1358/10/11",
                "phone": "09170000088",
            },
        )

        c1 = connect()
        try:
            c1.execute("BEGIN IMMEDIATE")
            c1.execute(
                "INSERT INTO appointments(public_id, patient_id, slot_date, slot_time, cabin)"
                " VALUES ('conn1cabin', ?, ?, ?, 1)",
                (pid, day, slot),
            )
            c1.execute("COMMIT")
        finally:
            c1.close()

        c2 = connect()
        try:
            c2.execute("BEGIN IMMEDIATE")
            with pytest.raises(sqlite3.IntegrityError) as hit:
                c2.execute(
                    "INSERT INTO appointments(public_id, patient_id, slot_date, slot_time, cabin)"
                    " VALUES ('conn2cabin', ?, ?, ?, 1)",
                    (other, day, slot),
                )
            assert "idx_appt_active_cabin" in str(hit.value) or "UNIQUE" in str(hit.value).upper()
            c2.execute("ROLLBACK")
        finally:
            c2.close()

    def test_begin_immediate_writers_serialize_then_second_hits_unique(self, patient):
        """Parallel BEGIN IMMEDIATE writers are serialized by SQLite (deterministic barriers).

        Limitation: they cannot interleave allocate's SELECT+INSERT to race inside one
        check-then-act window; the second blocks at BEGIN until the first commits, then
        the INSERT collides with the partial UNIQUE index.
        """
        import sqlite3
        import threading

        from api import repo
        from api.db import connect
        from api.policies import SYSTEM

        day, slot = tomorrow(), "17:30"
        pid = repo.select(SYSTEM, "patients")[0]["id"]
        other = repo.insert(
            SYSTEM,
            "patients",
            {
                "public_id": "v" * 12,
                "national_id": nid(700000077),
                "full_name": "نویسنده دوم",
                "birth_date": "1980-01-01",
                "birth_jalali": "1358/10/11",
                "phone": "09170000077",
            },
        )

        holding = threading.Event()
        release = threading.Event()
        second_result: list[object] = []

        def first_writer() -> None:
            c = connect()
            try:
                c.execute("BEGIN IMMEDIATE")
                c.execute(
                    "INSERT INTO appointments(public_id, patient_id, slot_date, slot_time, cabin)"
                    " VALUES ('immfirst01', ?, ?, ?, 1)",
                    (pid, day, slot),
                )
                holding.set()
                assert release.wait(timeout=5)
                c.execute("COMMIT")
            finally:
                c.close()

        def second_writer() -> None:
            assert holding.wait(timeout=5)
            c = connect()
            try:
                # Blocks here until first_writer COMMITs (IMMEDIATE reserved lock).
                c.execute("BEGIN IMMEDIATE")
                try:
                    c.execute(
                        "INSERT INTO appointments(public_id, patient_id, slot_date, slot_time, cabin)"
                        " VALUES ('immsecond1', ?, ?, ?, 1)",
                        (other, day, slot),
                    )
                    c.execute("COMMIT")
                    second_result.append("committed")
                except sqlite3.IntegrityError as exc:
                    second_result.append(exc)
                    c.execute("ROLLBACK")
            finally:
                c.close()

        t1 = threading.Thread(target=first_writer)
        t2 = threading.Thread(target=second_writer)
        t1.start()
        assert holding.wait(timeout=5)
        t2.start()
        release.set()
        t1.join(timeout=10)
        t2.join(timeout=10)
        assert len(second_result) == 1
        assert isinstance(second_result[0], sqlite3.IntegrityError)

    def test_source_cabin_stays_held_until_move_commits(self, owner_client, patient, monkeypatch):
        """While reschedule's IMMEDIATE tx is open, the source cabin must still look taken
        to a concurrent allocate — proving cancel is not visible until commit."""
        import threading

        from api import repo, scheduling
        from api.db import connect
        from api.policies import SYSTEM

        book = owner_client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "16:00"}
        ).json()
        old = repo.select(SYSTEM, "appointments", where="public_id = ?", params=[book["code"]])[0]
        day, slot = old["slot_date"], old["slot_time"]
        cabin = old["cabin"]
        seen_active: list[int] = []
        gate = threading.Event()
        done = threading.Event()

        real_allocate = scheduling.allocate_on_conn

        def slow_allocate(conn, *a, **k):
            gate.set()
            assert done.wait(timeout=5), "concurrent probe did not finish"
            return real_allocate(conn, *a, **k)

        monkeypatch.setattr(scheduling, "allocate_on_conn", slow_allocate)

        def probe():
            assert gate.wait(timeout=5)
            c = connect()
            try:
                n = c.execute(
                    "SELECT COUNT(*) AS n FROM appointments "
                    "WHERE slot_date = ? AND slot_time = ? AND cabin = ? "
                    "AND status != 'cancelled'",
                    (day, slot, cabin),
                ).fetchone()["n"]
                seen_active.append(int(n))
            finally:
                c.close()
                done.set()

        t = threading.Thread(target=probe)
        t.start()
        r = owner_client.patch(
            f"/api/admin/appointments/{book['code']}/move",
            json={"slot_date": tomorrow(), "slot_time": "20:30", "notify": False},
        )
        t.join(timeout=10)
        assert r.status_code == 200, r.text
        assert seen_active == [1], "source cabin must stay occupied until commit"
        # after commit the old row is cancelled and the cabin is free for reuse
        leftover = repo.select(
            SYSTEM,
            "appointments",
            where="slot_date = ? AND slot_time = ? AND cabin = ? AND status != 'cancelled'",
            params=[day, slot, cabin],
        )
        assert leftover == []
        again = scheduling.allocate(old["patient_id"], day, slot)
        assert again["cabin"] == cabin

    def test_next_session_is_booked_and_texted(self, owner_client, patient):
        from api import repo
        from api.policies import SYSTEM

        p = repo.select(SYSTEM, "patients")[0]
        r = owner_client.post(
            f"/api/admin/patients/{p['public_id']}/followup",
            json={"slot_date": tomorrow(), "slot_time": "19:00", "note": "جلسه دوم"},
        )
        assert r.status_code == 201, r.text
        assert r.json()["sms_sent"]
        appt = repo.select(SYSTEM, "appointments", where="kind = 'followup'")[0]
        assert appt["booked_by"] == "staff" and appt["slot_time"] == "19:00"
        assert repo.select(SYSTEM, "sms_messages", where="kind = 'followup'")

    def test_staff_writes_need_csrf(self, owner_client, patient):
        book = owner_client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "16:00"}
        ).json()
        r = owner_client.patch(
            f"/api/admin/appointments/{book['code']}/move",
            json={"slot_date": tomorrow(), "slot_time": "20:30"},
            headers={"X-CSRF-Token": "nope"},
        )
        assert r.status_code == 403

    def test_marking_attended_feeds_the_outcome_metric(self, owner_client, patient):
        from api import metrics

        book = owner_client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "16:00"}
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
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "16:00"}
        )
        out = reminders.run_once(lead_min=60 * 24 * 7)
        assert out["due"] == 1 and out["sent"] == 1
        sms = repo.select(SYSTEM, "sms_messages", where="kind = 'confirm_request'")[0]
        assert "عدد ۱" in sms["body"] and "عدد ۲" in sms["body"]
        assert "تأیید حضور" in sms["body"] and "عدم حضور" in sms["body"]
        assert repo.select(SYSTEM, "appointments")[0]["confirm_sent_at"]

    def test_the_reminder_is_never_sent_twice(self, client, patient):
        from api import reminders

        client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "16:00"}
        )
        reminders.run_once(lead_min=60 * 24 * 7)
        assert reminders.run_once(lead_min=60 * 24 * 7)["sent"] == 0

    def test_run_once_skips_sms_when_claim_update_changes_no_rows(self, client, patient, monkeypatch):
        from api import reminders, sms

        client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "16:00"}
        )
        sends: list[str] = []

        def _no_claim(*_a, **_k):
            return 0

        def _track_send(phone, body, kind, **kwargs):
            sends.append(kind)
            return ("", True)

        monkeypatch.setattr(reminders.repo, "update", _no_claim)
        monkeypatch.setattr(sms, "send_now", _track_send)
        out = reminders.run_once(lead_min=60 * 24 * 7)
        assert out["due"] == 1 and out["sent"] == 0 and out["failed"] == 0
        assert sends == []

    def test_nothing_is_sent_outside_the_window(self, client, patient):
        from api import reminders

        client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "20:30"}
        )
        assert reminders.run_once(lead_min=5)["due"] == 0

    def test_lifespan_starts_and_cancels_reminder_loop(self, monkeypatch):
        import asyncio
        import time

        from fastapi.testclient import TestClient

        from api import reminders
        from api.config import settings
        from api.main import app

        events: list[str] = []

        async def fake_loop():
            events.append("start")
            try:
                while True:
                    await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                events.append("cancel")
                raise

        monkeypatch.setattr(settings, "REMINDER_BACKGROUND", True)
        monkeypatch.setattr(reminders, "loop", fake_loop)
        with TestClient(app):
            deadline = time.time() + 2
            while "start" not in events and time.time() < deadline:
                time.sleep(0.02)
            assert "start" in events
        assert "cancel" in events

    @pytest.mark.parametrize("reply", ["1", "۱", "بله", "تایید", "OK"])
    def test_replying_one_marks_the_patient_as_coming(self, client, patient, reply):
        from api import reminders, repo
        from api.policies import SYSTEM

        client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "16:00"}
        )
        out = reminders.handle_inbound("09121112233", reply)
        assert out["handled"] == "confirmed"
        a = repo.select(SYSTEM, "appointments")[0]
        assert a["attendance"] == "coming" and a["confirmed_at"]

    def test_replying_no_marks_the_patient_as_not_coming(self, client, patient):
        from api import reminders, repo
        from api.policies import SYSTEM

        client.post(
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "16:00"}
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
        monkeypatch.setenv("NOTIFY_PROVIDER", "smsir")
        monkeypatch.setenv("ADMIN_NOTIFICATION_PHONE", "09121111111")
        monkeypatch.setenv("SMS_PROVIDER", "file")
        monkeypatch.setenv("SMS_API_KEY", "test-key")
        monkeypatch.setenv("SMS_SENDER", "300000000000")
        import api.config as cfg

        with pytest.raises(RuntimeError, match="SMS_PROVIDER=file"):
            importlib.reload(cfg)
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("NOTIFY_PROVIDER", "file")
        monkeypatch.setenv("SMS_PROVIDER", "file")
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
        a = scheduling.allocate(pid, tomorrow(), "16:00")
        with pytest.raises(sqlite3.IntegrityError):
            db.get_conn().execute(
                "INSERT INTO appointments(public_id, patient_id, slot_date, slot_time, cabin)"
                " VALUES ('dupdupdup1', ?, ?, '16:00', ?)",
                (pid, tomorrow(), a["cabin"]),
            )

    def test_cancelled_row_does_not_block_same_cabin_unique(self):
        from api import db, repo, scheduling
        from api.policies import SYSTEM

        pid = repo.insert(
            SYSTEM,
            "patients",
            {
                "public_id": "y" * 12,
                "national_id": nid(700000002),
                "full_name": "تست آزاد",
                "birth_date": "1980-01-01",
                "birth_jalali": "1358/10/11",
                "phone": "09170000002",
            },
        )
        a = scheduling.allocate(pid, tomorrow(), "16:00")
        repo.update(
            SYSTEM, "appointments", {"status": "cancelled"}, where="id = ?", params=[a["id"]]
        )
        db.get_conn().execute(
            "INSERT INTO appointments(public_id, patient_id, slot_date, slot_time, cabin)"
            " VALUES ('newnewnew1', ?, ?, '16:00', ?)",
            (pid, tomorrow(), a["cabin"]),
        )
        rows = db.get_conn().execute(
            "SELECT status FROM appointments WHERE slot_date = ? AND slot_time = '16:00' AND cabin = ?",
            (tomorrow(), a["cabin"]),
        ).fetchall()
        assert sorted(r["status"] for r in rows) == ["booked", "cancelled"]

    def test_migration_0009_preserves_rows_on_existing_database(self, tmp_path):
        """Apply 0001–0008, seed a cancelled row, then run 0009 and keep every column."""
        import hashlib
        import sqlite3

        from api import db

        path = tmp_path / "pre-0009.db"
        conn = sqlite3.connect(path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(db.BOOTSTRAP)

        legacy = [p for p in db._files() if p.name[:4] < "0009"]
        assert legacy and legacy[-1].name.startswith("0008")
        for path_sql in legacy:
            body = path_sql.read_text(encoding="utf-8")
            checksum = hashlib.sha256(body.encode()).hexdigest()
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN")
            try:
                for stmt in db.split_statements(body):
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO schema_migrations(version, checksum) VALUES (?,?)",
                    (path_sql.name[:4], checksum),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                conn.execute("PRAGMA foreign_keys=ON")
                raise
            conn.execute("PRAGMA foreign_keys=ON")

        conn.execute(
            "INSERT INTO patients(public_id, national_id, full_name, birth_date, birth_jalali, phone)"
            " VALUES (?, ?, 'بیمار قدیم', '1990-01-01', '1368/10/11', '09121112233')",
            ("p" * 12, nid(800000001)),
        )
        pid = conn.execute("SELECT id FROM patients").fetchone()["id"]
        conn.execute(
            "INSERT INTO appointments(public_id, patient_id, slot_date, slot_time, cabin, status, note)"
            " VALUES ('oldoldold1', ?, '2099-06-01', '16:00', 3, 'cancelled', 'keep-me')",
            (pid,),
        )
        before = dict(
            conn.execute("SELECT * FROM appointments WHERE public_id = 'oldoldold1'").fetchone()
        )

        applied = db.migrate(conn)
        assert any(name.startswith("0009") for name in applied)
        assert db.migrate(conn) == []

        after = dict(
            conn.execute("SELECT * FROM appointments WHERE public_id = 'oldoldold1'").fetchone()
        )
        assert after["note"] == "keep-me"
        assert after["status"] == "cancelled"
        assert after["cabin"] == 3
        assert after["patient_id"] == before["patient_id"]
        assert after["public_id"] == before["public_id"]
        idx = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_appt_active_cabin'"
        ).fetchone()
        assert idx and "cancelled" in idx["sql"]
        conn.execute(
            "INSERT INTO appointments(public_id, patient_id, slot_date, slot_time, cabin)"
            " VALUES ('newnewnew2', ?, '2099-06-01', '16:00', 3)",
            (pid,),
        )
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM appointments WHERE slot_date='2099-06-01' AND cabin=3"
        ).fetchone()["c"]
        assert n == 2
        conn.close()

    def test_the_database_rejects_an_out_of_range_cabin(self):
        import sqlite3

        from api import db

        with pytest.raises(sqlite3.IntegrityError):
            db.get_conn().execute(
                "INSERT INTO appointments(public_id, patient_id, slot_date, slot_time, cabin)"
                " VALUES ('dupdupdup2', 1, '2026-10-05', '16:00', 11)"
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
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "16:00"}
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
            "/api/portal/appointments", json={"slot_date": tomorrow(), "slot_time": "16:00"}
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
        sessions = repo.select(
            SYSTEM, "treatment_sessions", where="appointment_id = ?", params=[a["id"]]
        )
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
        owner_id = repo.select(
            SYSTEM, "admin_users", where="role = 'owner'", limit=1
        )[0]["id"]
        actor = Actor("owner", owner_id)
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
        owner_id = repo.select(
            SYSTEM, "admin_users", where="role = 'owner'", limit=1
        )[0]["id"]
        actor = Actor("owner", owner_id)
        c = clinical.catalog(actor)
        bp = c["body_parts"][0]["items"][0]["id"]
        tx = c["treatments"][0]["id"]
        clinical.record_session(
            actor,
            p["id"],
            body_part_ids=[bp],
            treatment_ids=[tx],
            session_date=long_ago,
        )
        gap = clinical.returning_after_gap(p["id"], tomorrow())
        assert gap is not None and gap > 190


# ======================================================================
# Clinic closure dates
# ======================================================================
def closure_future_day() -> str:
    from api.jalali import now_tehran

    return (now_tehran().date() + dt.timedelta(days=2)).isoformat()


def test_closed_day_disables_patient_slots_and_can_be_reopened(owner_client, patient):
    date = closure_future_day()
    closed = owner_client.post(
        "/api/admin/closed-days",
        json={"date": date, "reason": "تعمیرات"},
    )
    assert closed.status_code == 201, closed.text

    grid = owner_client.get(f"/api/portal/slots?date={date}")
    assert grid.status_code == 200, grid.text
    assert grid.json()["closed"] is True
    assert grid.json()["closed_reason"] == "تعمیرات"
    assert all(slot["disabled"] for slot in grid.json()["slots"])
    days = owner_client.get("/api/portal/days")
    closed_day = next(d for d in days.json()["days"] if d["date"] == date)
    assert closed_day["closed"] is True
    assert closed_day["closed_reason"] == "تعمیرات"

    booking = owner_client.post(
        "/api/portal/appointments",
        json={"slot_date": date, "slot_time": "16:00"},
    )
    assert booking.status_code == 409, booking.text
    assert booking.json()["error"]["code"] == "day_closed"

    reopened = owner_client.delete(f"/api/admin/closed-days/{date}")
    assert reopened.status_code == 200, reopened.text
    grid = owner_client.get(f"/api/portal/slots?date={date}")
    assert grid.status_code == 200
    assert grid.json()["closed"] is False


def test_staff_cannot_close_a_day_with_existing_bookings(owner_client, patient):
    date = closure_future_day()
    booking = owner_client.post(
        "/api/portal/appointments",
        json={"slot_date": date, "slot_time": "16:00"},
    )
    assert booking.status_code == 201, booking.text

    closed = owner_client.post(
        "/api/admin/closed-days",
        json={"date": date},
    )
    assert closed.status_code == 409, closed.text
    assert closed.json()["error"]["code"] == "day_has_bookings"


def test_closed_day_controls_require_staff_authentication(client):
    date = closure_future_day()
    response = client.post(
        "/api/admin/closed-days",
        json={"date": date},
    )
    assert response.status_code == 401

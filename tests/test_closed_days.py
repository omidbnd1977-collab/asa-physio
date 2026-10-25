"""Clinic closure dates: staff controls and patient-side enforcement."""

from __future__ import annotations

import datetime as dt


def future_day() -> str:
    from api.jalali import now_tehran

    return (now_tehran().date() + dt.timedelta(days=2)).isoformat()


def test_closed_day_disables_patient_slots_and_can_be_reopened(owner_client, patient):
    date = future_day()
    closed = owner_client.post(
        "/api/admin/closed-days", json={"date": date, "reason": "تعمیرات"}
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
        "/api/portal/appointments", json={"slot_date": date, "slot_time": "16:00"}
    )
    assert booking.status_code == 409, booking.text
    assert booking.json()["error"]["code"] == "day_closed"

    reopened = owner_client.delete(f"/api/admin/closed-days/{date}")
    assert reopened.status_code == 200, reopened.text
    grid = owner_client.get(f"/api/portal/slots?date={date}")
    assert grid.status_code == 200
    assert grid.json()["closed"] is False


def test_staff_cannot_close_a_day_with_existing_bookings(owner_client, patient):
    date = future_day()
    booking = owner_client.post(
        "/api/portal/appointments", json={"slot_date": date, "slot_time": "16:00"}
    )
    assert booking.status_code == 201, booking.text

    closed = owner_client.post("/api/admin/closed-days", json={"date": date})
    assert closed.status_code == 409, closed.text
    assert closed.json()["error"]["code"] == "day_has_bookings"


def test_closed_day_controls_require_staff_authentication(client):
    date = future_day()
    response = client.post("/api/admin/closed-days", json={"date": date})
    assert response.status_code == 401

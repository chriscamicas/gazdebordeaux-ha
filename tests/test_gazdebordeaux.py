"""Pure-Python tests for the Gazdebordeaux API client.

These exercise the HTTP contract by mocking aiohttp via aioresponses; no
Home Assistant fixtures are needed. We import `gazdebordeaux` as a
top-level module to avoid pulling the package's `__init__.py`, which
imports Home Assistant.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp import ClientSession
from aioresponses import aioresponses

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "gazdebordeaux")
)
from gazdebordeaux import (
    DATA_URL,
    LOGIN_URL,
    ME_URL,
    Gazdebordeaux,
)

USERNAME = "user@example.com"
PASSWORD = "secret"
TOKEN = "fake-jwt-token"
HOUSE_PATH = "/api/houses/abc"
DATA_HOST = "https://life.gazdebordeaux.fr"
RESOURCES = Path(__file__).resolve().parent / "resources"


@pytest.fixture
def http_mock():
    with aioresponses() as m:
        yield m


@pytest.fixture
async def session():
    async with ClientSession() as s:
        yield s


# ---------- async_login -----------------------------------------------------


async def test_login_stores_token(http_mock, session):
    http_mock.post(LOGIN_URL, payload={"token": TOKEN})

    api = Gazdebordeaux(session, USERNAME, PASSWORD)
    await api.async_login()

    assert api._token == TOKEN


async def test_login_html_response_raises(http_mock, session):
    http_mock.post(
        LOGIN_URL,
        status=403,
        body="<html>403 Forbidden</html>",
        headers={"Content-Type": "text/html"},
    )

    api = Gazdebordeaux(session, USERNAME, PASSWORD)
    with pytest.raises(Exception, match="Login response was not JSON"):
        await api.async_login()


async def test_login_null_token_raises(http_mock, session):
    http_mock.post(LOGIN_URL, payload={"token": None})

    api = Gazdebordeaux(session, USERNAME, PASSWORD)
    with pytest.raises(Exception, match="invalid auth"):
        await api.async_login()


# ---------- async_list_houses ---------------------------------------------


def _house_payload(category, name="Home", label="Contract", street="1 rue X", locality=""):
    return {
        "name": name,
        "addressStreet": street,
        "addressLocality": locality,
        "contractType": {"category": category, "label": label},
    }


async def test_list_houses_returns_every_contract(http_mock, session):
    elec = "/api/houses/elec-uuid"
    gas = "/houses/gas-uuid"  # missing /api prefix on some accounts

    http_mock.get(ME_URL, payload={"selectedHouse": elec, "houses": [elec, gas]})
    http_mock.get(
        f"{DATA_HOST}{elec}", payload=_house_payload("electricity", "Elec", "Elec NOVA fixe")
    )
    http_mock.get(
        f"{DATA_HOST}/api/houses/gas-uuid", payload=_house_payload("gas", "Gas", "Classique")
    )

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN)
    houses = await api.async_list_houses()

    assert [(h.path, h.category) for h in houses] == [
        (elec, "electricity"),
        ("/api/houses/gas-uuid", "gas"),
    ]
    assert houses[1].label == "Gas - Classique - 1 rue X"


async def test_list_houses_label_falls_back_to_path(http_mock, session):
    http_mock.get(ME_URL, payload={"selectedHouse": None, "houses": [HOUSE_PATH]})
    http_mock.get(f"{DATA_HOST}{HOUSE_PATH}", payload={"contractType": {"category": "gas"}})

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN)
    houses = await api.async_list_houses()

    assert houses[0].label == HOUSE_PATH


async def test_list_houses_uses_selected_house_when_list_empty(http_mock, session):
    http_mock.get(ME_URL, payload={"selectedHouse": HOUSE_PATH, "houses": []})
    http_mock.get(f"{DATA_HOST}{HOUSE_PATH}", payload={"contractType": {"category": "gas"}})

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN)
    houses = await api.async_list_houses()

    assert [h.path for h in houses] == [HOUSE_PATH]


async def test_list_houses_empty_raises(http_mock, session):
    http_mock.get(ME_URL, payload={"selectedHouse": None, "houses": []})

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN)
    with pytest.raises(Exception, match="No houses found"):
        await api.async_list_houses()


# ---------- async_find_house -----------------------------------------------


async def test_find_house_ignores_selected_house(http_mock, session):
    """selectedHouse is the web UI's last pick; it must not override the category."""
    elec = "/api/houses/elec-uuid"
    gas = "/api/houses/gas-uuid"

    http_mock.get(ME_URL, payload={"selectedHouse": elec, "houses": [elec, gas]})
    http_mock.get(f"{DATA_HOST}{elec}", payload={"contractType": {"category": "electricity"}})
    http_mock.get(f"{DATA_HOST}{gas}", payload={"contractType": {"category": "gas"}})

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN)

    assert await api.async_find_house("gas") == gas


async def test_find_house_no_match_raises(http_mock, session):
    elec1 = "/api/houses/elec1"
    elec2 = "/api/houses/elec2"

    http_mock.get(ME_URL, payload={"selectedHouse": None, "houses": [elec1, elec2]})
    http_mock.get(f"{DATA_HOST}{elec1}", payload={"contractType": {"category": "electricity"}})
    http_mock.get(f"{DATA_HOST}{elec2}", payload={"contractType": {"category": "electricity"}})

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN)
    with pytest.raises(Exception, match="No gas contract found"):
        await api.async_find_house("gas")


# ---------- async_get_data: house path normalization -----------------------


@pytest.mark.parametrize(
    "house_in",
    [
        "/api/houses/abc",  # already prefixed
        "/houses/abc",  # missing /api
        "houses/abc",  # missing both leading slash and /api
    ],
)
async def test_data_url_normalization(http_mock, session, house_in):
    expected_url = DATA_URL.format("/api/houses/abc")
    http_mock.get(
        f"{expected_url}?scale=year",
        payload={"total": {"kwh": 100, "volumeOfEnergy": 10, "price": 50}},
    )

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN)
    result = await api.async_get_total_usage(house_in)

    assert result.amountOfEnergy == 100
    assert result.volumeOfEnergy == 10
    assert result.price == 50


# ---------- electricity payload --------------------------------------------


async def test_daily_usage_parses_electricity_payload(http_mock, session):
    payload = json.loads((RESOURCES / "electricity_month.json").read_text())
    start = datetime(2026, 9, 1)
    end = datetime(2026, 9, 30)
    http_mock.get(
        f"{DATA_URL.format(HOUSE_PATH)}?scale=month&startDate=2026-09-01&endDate=2026-09-30",
        payload=payload,
    )

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN)
    reads = await api.async_get_daily_usage(HOUSE_PATH, start, end)

    assert [(r.date.day, r.amountOfEnergy, r.price, r.volumeOfEnergy) for r in reads] == [
        (29, 15, 3.4, 0),
        (30, 0, 0, 0),
    ]
    # Midnight Paris time, not Paris mean time (+00:09)
    assert reads[0].date.isoformat() == "2026-09-29T00:00:00+02:00"


# ---------- async_get_hourly_usage: Enedis load curve ----------------------

DAY_URL = re.compile(re.escape(DATA_URL.format(HOUSE_PATH)) + r"\?.*scale=day.*")


def _curve(day: date, kw: float = 0.5, step_count: int = 49) -> dict:
    """Load curve as the "day" scale returns it: average kW per half hour,
    keyed by the end of each half hour, from 00:00 to 00:00 the next day."""
    start = datetime(day.year, day.month, day.day)
    data = {
        (start + timedelta(minutes=30 * i)).strftime("%Y-%m-%d %H:%M:%S"): {
            "kwh": kw,
            "price": 0,
            "volumeOfEnergy": 0,
            "isHphc": False,
        }
        for i in range(step_count)
    }
    data["total"] = {"kwh": kw * step_count}
    return data


async def test_hourly_usage_turns_power_into_hourly_energy(http_mock, session):
    day = date(2026, 10, 20)
    http_mock.get(DAY_URL, payload=_curve(day))

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN)
    hours = await api.async_get_hourly_usage(HOUSE_PATH, day)

    assert hours is not None
    assert len(hours) == 24
    assert hours[0].date.isoformat() == "2026-10-20T00:00:00+02:00"
    assert hours[-1].date.isoformat() == "2026-10-20T23:00:00+02:00"
    # 0.5 kW over two half hours = 0.5 kWh per hour; the 00:00 point
    # belongs to the day before
    assert all(abs(h.amountOfEnergy - 0.5) < 1e-9 for h in hours)


async def test_hourly_usage_october_change_keeps_hours_distinct(http_mock, session):
    # 2026-10-25: clocks go back at 03:00; the naive keys cannot repeat
    # 02:00-03:00, but no hour may be counted twice
    day = date(2026, 10, 25)
    http_mock.get(DAY_URL, payload=_curve(day))

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN)
    hours = await api.async_get_hourly_usage(HOUSE_PATH, day)

    assert hours is not None
    starts = [h.date for h in hours]
    assert starts == sorted(starts)
    assert len({s.timestamp() for s in starts}) == len(starts) == 25
    # The 48 half hours keep their place: the summer and winter 02:00 hours
    # get one each, and no hour is filled twice
    assert all(h.amountOfEnergy <= 0.5 + 1e-9 for h in hours)
    assert abs(sum(h.amountOfEnergy for h in hours) - 12.0) < 1e-9


async def test_hourly_usage_october_change_fills_the_lost_half_hours(http_mock, session):
    # Same 49 points; the daily read says 12.5 kWh: the two half hours lost
    # in the repeated hour share the missing 0.5 kWh
    day = date(2026, 10, 25)
    http_mock.get(DAY_URL, payload=_curve(day))

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN)
    hours = await api.async_get_hourly_usage(HOUSE_PATH, day, daily_energy=12.5)

    assert hours is not None
    assert len({h.date.timestamp() for h in hours}) == len(hours) == 25
    assert all(abs(h.amountOfEnergy - 0.5) < 1e-9 for h in hours)
    assert [h.date.isoformat() for h in hours if h.date.hour == 2] == [
        "2026-10-25T02:00:00+02:00",
        "2026-10-25T02:00:00+01:00",
    ]


@pytest.mark.parametrize(
    ("day", "daily_energy"),
    [
        (date(2026, 10, 25), 20.0),  # more than the day's highest half hour
        (date(2026, 10, 25), 11.0),  # nothing lacks
        (date(2026, 10, 20), 12.5),  # a 24 hour day
    ],
)
async def test_hourly_usage_leaves_other_gaps_alone(http_mock, session, day, daily_energy):
    http_mock.get(DAY_URL, payload=_curve(day))

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN)
    hours = await api.async_get_hourly_usage(HOUSE_PATH, day, daily_energy=daily_energy)

    assert hours is not None
    assert abs(sum(h.amountOfEnergy for h in hours) - 12.0) < 1e-9


async def test_hourly_usage_march_change(http_mock, session):
    # 2026-03-29: clocks go forward at 02:00, the day has 23 hours
    day = date(2026, 3, 29)
    curve = _curve(day)
    for key in ("2026-03-29 02:30:00", "2026-03-29 03:00:00"):
        curve.pop(key)
    http_mock.get(DAY_URL, payload=curve)

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN)
    hours = await api.async_get_hourly_usage(HOUSE_PATH, day)

    assert hours is not None
    assert len(hours) == 23
    assert len({h.date.timestamp() for h in hours}) == 23


async def test_hourly_usage_rejects_daily_keys(http_mock, session):
    http_mock.get(DAY_URL, payload={"2026-10-20": {"kwh": 9.7}, "total": {}})

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN)
    assert await api.async_get_hourly_usage(HOUSE_PATH, date(2026, 10, 20)) is None


# ---------- async_get_house_details ----------------------------------------


async def test_house_details_reads_the_curve_start(http_mock, session):
    http_mock.get(
        f"{DATA_HOST}{HOUSE_PATH}",
        payload={"priceCategory": "BASE", "hourlyMeasureStartDate": "2026-02-24T00:00:00+01:00"},
    )

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN)
    details = await api.async_get_house_details("/houses/abc")

    assert details.hourly_start == date(2026, 2, 24)


async def test_house_details_without_curve(http_mock, session):
    http_mock.get(f"{DATA_HOST}{HOUSE_PATH}", payload={"priceCategory": "BASE"})

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN)
    details = await api.async_get_house_details(HOUSE_PATH)

    assert details.hourly_start is None

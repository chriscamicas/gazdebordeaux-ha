"""Pure-Python tests for the Gazdebordeaux API client.

These exercise the HTTP contract by mocking aiohttp via aioresponses; no
Home Assistant fixtures are needed. We import `gazdebordeaux` as a
top-level module to avoid pulling the package's `__init__.py`, which
imports Home Assistant.
"""

from __future__ import annotations

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
    OffPeakTimes,
)

USERNAME = "user@example.com"
PASSWORD = "secret"
TOKEN = "fake-jwt-token"
HOUSE_PATH = "/api/houses/abc"
DATA_HOST = "https://life.gazdebordeaux.fr"


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


# ---------- loadHouse: selectedHouse path ----------------------------------


async def test_loadhouse_uses_selected_house(http_mock, session):
    http_mock.get(ME_URL, payload={"selectedHouse": HOUSE_PATH, "houses": [HOUSE_PATH]})

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN)
    await api.loadHouse()

    assert api._selectedHouse == HOUSE_PATH


# ---------- loadHouse: multi-house iteration -------------------------------


async def test_loadhouse_picks_gas_house(http_mock, session):
    elec = "/api/houses/elec-uuid"
    gas = "/api/houses/gas-uuid"

    http_mock.get(ME_URL, payload={"selectedHouse": None, "houses": [elec, gas]})
    http_mock.get(f"{DATA_HOST}{elec}", payload={"contractType": {"category": "electricity"}})
    http_mock.get(f"{DATA_HOST}{gas}", payload={"contractType": {"category": "gas"}})

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN)
    await api.loadHouse()

    assert api._selectedHouse == gas


async def test_loadhouse_no_gas_raises(http_mock, session):
    elec1 = "/api/houses/elec1"
    elec2 = "/api/houses/elec2"

    http_mock.get(ME_URL, payload={"selectedHouse": None, "houses": [elec1, elec2]})
    http_mock.get(f"{DATA_HOST}{elec1}", payload={"contractType": {"category": "electricity"}})
    http_mock.get(f"{DATA_HOST}{elec2}", payload={"contractType": {"category": "electricity"}})

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN)
    with pytest.raises(Exception, match="No gas contract found"):
        await api.loadHouse()


async def test_loadhouse_empty_houses_raises(http_mock, session):
    http_mock.get(ME_URL, payload={"selectedHouse": None, "houses": []})

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN)
    with pytest.raises(Exception, match="No houses found"):
        await api.loadHouse()


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

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN, house=house_in)
    result = await api.async_get_total_usage()

    assert result.amountOfEnergy == 100
    assert result.volumeOfEnergy == 10
    assert result.price == 50


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

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN, house=HOUSE_PATH)
    hours = await api.async_get_hourly_usage(day)

    assert hours is not None
    assert len(hours) == 24
    assert hours[0].date.isoformat() == "2026-10-20T00:00:00+02:00"
    assert hours[-1].date.isoformat() == "2026-10-20T23:00:00+02:00"
    # 0.5 kW over two half hours = 0.5 kWh per hour; the 00:00 point
    # belongs to the day before
    assert all(abs(h.amountOfEnergy - 0.5) < 1e-9 for h in hours)


async def test_hourly_usage_splits_peak_and_offpeak(http_mock, session):
    day = date(2026, 10, 20)
    http_mock.get(DAY_URL, payload=_curve(day))
    offpeak = OffPeakTimes(all=[["22:00", "6:00"]], high=[], low=[])

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN, house=HOUSE_PATH)
    hours = await api.async_get_hourly_usage(day, offpeak)

    assert hours is not None
    by_hour = {h.date.hour: h for h in hours}
    assert abs(by_hour[5].consumptionHc - 0.5) < 1e-9 and by_hour[5].consumptionHp == 0
    assert abs(by_hour[6].consumptionHp - 0.5) < 1e-9 and by_hour[6].consumptionHc == 0
    assert abs(by_hour[23].consumptionHc - 0.5) < 1e-9
    assert abs(sum(h.consumptionHc for h in hours) - 4.0) < 1e-9


async def test_hourly_usage_october_change_keeps_hours_distinct(http_mock, session):
    # 2026-10-25: clocks go back at 03:00; the naive keys cannot repeat
    # 02:00-03:00, but no hour may be counted twice
    day = date(2026, 10, 25)
    http_mock.get(DAY_URL, payload=_curve(day))

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN, house=HOUSE_PATH)
    hours = await api.async_get_hourly_usage(day)

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
    offpeak = OffPeakTimes(all=[["1:00", "6:00"]], high=[], low=[])

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN, house=HOUSE_PATH)
    hours = await api.async_get_hourly_usage(day, offpeak, daily_energy=12.5)

    assert hours is not None
    assert len({h.date.timestamp() for h in hours}) == len(hours) == 25
    assert all(abs(h.amountOfEnergy - 0.5) < 1e-9 for h in hours)
    two_am = [h for h in hours if h.date.hour == 2]
    assert [h.date.isoformat() for h in two_am] == [
        "2026-10-25T02:00:00+02:00",
        "2026-10-25T02:00:00+01:00",
    ]
    assert all(abs(h.consumptionHc - 0.5) < 1e-9 for h in two_am)


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

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN, house=HOUSE_PATH)
    hours = await api.async_get_hourly_usage(day, daily_energy=daily_energy)

    assert hours is not None
    assert abs(sum(h.amountOfEnergy for h in hours) - 12.0) < 1e-9


async def test_hourly_usage_march_change(http_mock, session):
    # 2026-03-29: clocks go forward at 02:00, the day has 23 hours
    day = date(2026, 3, 29)
    curve = _curve(day)
    for key in ("2026-03-29 02:30:00", "2026-03-29 03:00:00"):
        curve.pop(key)
    http_mock.get(DAY_URL, payload=curve)

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN, house=HOUSE_PATH)
    hours = await api.async_get_hourly_usage(day)

    assert hours is not None
    assert len(hours) == 23
    assert len({h.date.timestamp() for h in hours}) == 23


async def test_hourly_usage_rejects_daily_keys(http_mock, session):
    http_mock.get(DAY_URL, payload={"2026-10-20": {"kwh": 9.7}, "total": {}})

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN, house=HOUSE_PATH)
    assert await api.async_get_hourly_usage(date(2026, 10, 20)) is None


# ---------- async_get_daily_usage: HP/HC fields ----------------------------


async def test_daily_usage_reads_peak_offpeak_and_fills_zero_days(http_mock, session):
    base = {"volumeOfEnergy": 0, "ratio": 0, "temperature": 12, "isHphc": True}
    http_mock.get(
        re.compile(re.escape(DATA_URL.format(HOUSE_PATH)) + r"\?.*scale=month.*"),
        payload={
            "2026-02-24": {
                **base,
                "kwh": 0,
                "price": 0,
                "consumptionHp": 1.0,
                "consumptionHc": 3.0,
                "priceHp": 0.18,
                "priceHc": 0.41,
                "contractPrice": 0.52,
            },
            "2026-10-20": {
                **base,
                "kwh": 9.705,
                "price": 2.04,
                "consumptionHp": 4.863,
                "consumptionHc": 4.842,
                "priceHp": 0.86,
                "priceHc": 0.66,
                "contractPrice": 0.52,
            },
            "total": {},
        },
    )

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN, house=HOUSE_PATH)
    reads = {
        r.date.date().isoformat(): r
        for r in await api.async_get_daily_usage(datetime(2026, 1, 1), datetime(2026, 10, 22))
    }

    assert reads["2026-10-20"].date.isoformat() == "2026-10-20T00:00:00+02:00"
    assert reads["2026-10-20"].consumptionHc == 4.842
    assert reads["2026-10-20"].contractPrice == 0.52
    # kwh and price at 0 while the periods are filled: the periods win
    assert reads["2026-02-24"].amountOfEnergy == 4.0
    assert abs(reads["2026-02-24"].price - 1.11) < 1e-9


# ---------- async_get_house_details ----------------------------------------


async def test_house_details_reads_seasonal_offpeak_and_curve_start(http_mock, session):
    http_mock.get(
        f"{DATA_HOST}{HOUSE_PATH}",
        payload={
            "priceCategory": "HPHC",
            "offPeakTimes": [],
            "offPeakTimesHighSeason": [["2:00", "7:00"], ["22:00", "0:00"]],
            "offPeakTimesLowSeason": [["1:00", "6:00"], ["14:00", "17:00"]],
            "hourlyMeasureStartDate": "2026-02-24T00:00:00+01:00",
        },
    )

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN, house="/houses/abc")
    details = await api.async_get_house_details()

    assert details.hourly_start == date(2026, 2, 24)
    assert details.offpeak is not None
    assert details.offpeak.is_offpeak(date(2026, 12, 1), 23 * 60 + 30)
    assert not details.offpeak.is_offpeak(date(2026, 12, 1), 15 * 60)
    assert details.offpeak.is_offpeak(date(2026, 7, 1), 15 * 60)


async def test_house_details_base_contract(http_mock, session):
    http_mock.get(f"{DATA_HOST}{HOUSE_PATH}", payload={"priceCategory": "BASE"})

    api = Gazdebordeaux(session, USERNAME, PASSWORD, token=TOKEN, house=HOUSE_PATH)
    details = await api.async_get_house_details()

    assert details.offpeak is None
    assert details.hourly_start is None


def test_offpeak_ranges_ending_at_midnight():
    offpeak = OffPeakTimes(all=[["22:00", "24:00"], ["0:00", "6:00"]], high=[], low=[])
    day = date(2026, 7, 1)
    assert offpeak.is_offpeak(day, 23 * 60 + 30)
    assert offpeak.is_offpeak(day, 0)
    assert not offpeak.is_offpeak(day, 6 * 60)

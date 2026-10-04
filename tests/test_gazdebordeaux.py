"""Pure-Python tests for the Gazdebordeaux API client.

These exercise the HTTP contract by mocking aiohttp via aioresponses; no
Home Assistant fixtures are needed. We import `gazdebordeaux` as a
top-level module to avoid pulling the package's `__init__.py`, which
imports Home Assistant.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
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

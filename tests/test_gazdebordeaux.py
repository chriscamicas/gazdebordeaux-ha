"""Pure-Python tests for the Gazdebordeaux API client.

These exercise the HTTP contract by mocking aiohttp via aioresponses; no
Home Assistant fixtures are needed. We import `gazdebordeaux` as a
top-level module to avoid pulling the package's `__init__.py`, which
imports Home Assistant.
"""

from __future__ import annotations

import sys
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

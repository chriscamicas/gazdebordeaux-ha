"""Tests for the gazdebordeaux config and options flows."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gazdebordeaux.const import (
    CONF_ELECTRICITY_HOUSE,
    CONF_GAS_HOUSE,
    DOMAIN,
    NONE,
    RESET_STATISTICS,
)
from custom_components.gazdebordeaux.gazdebordeaux import House

USERNAME = "user@example.com"
PASSWORD = "secret"
GAS = "/api/houses/gas-uuid"
ELEC = "/api/houses/elec-uuid"
HOUSES = [
    House(path=ELEC, category="electricity", name="Elec", contract_label="Elec NOVA fixe"),
    House(path=GAS, category="gas", name="Gas", contract_label="Classique"),
]

API = "custom_components.gazdebordeaux.flow_helpers.Gazdebordeaux"


def _default(schema, key):
    for marker in schema.schema:
        if marker == key:
            return marker.default()
    raise KeyError(key)


async def _start_and_login(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}

    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )


async def test_form_happy_path(hass: HomeAssistant) -> None:
    """Login, then pick both contracts: the entry stores credentials and houses."""
    with (
        patch(f"{API}.async_login", new=AsyncMock(return_value=None)),
        patch(f"{API}.async_list_houses", new=AsyncMock(return_value=HOUSES)),
        patch("custom_components.gazdebordeaux.async_setup_entry", return_value=True),
    ):
        result = await _start_and_login(hass)

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "contracts"
        # Each dropdown defaults to the first contract of its category.
        assert _default(result["data_schema"], CONF_GAS_HOUSE) == GAS
        assert _default(result["data_schema"], CONF_ELECTRICITY_HOUSE) == ELEC

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_GAS_HOUSE: GAS, CONF_ELECTRICITY_HOUSE: ELEC},
        )
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == f"({USERNAME})"
    assert result["data"] == {
        CONF_USERNAME: USERNAME,
        CONF_PASSWORD: PASSWORD,
        CONF_GAS_HOUSE: GAS,
        CONF_ELECTRICITY_HOUSE: ELEC,
    }


async def test_form_defaults_to_none_without_matching_contract(hass: HomeAssistant) -> None:
    with (
        patch(f"{API}.async_login", new=AsyncMock(return_value=None)),
        patch(f"{API}.async_list_houses", new=AsyncMock(return_value=HOUSES[1:])),
    ):
        result = await _start_and_login(hass)

    assert _default(result["data_schema"], CONF_GAS_HOUSE) == GAS
    assert _default(result["data_schema"], CONF_ELECTRICITY_HOUSE) == NONE


async def test_form_requires_one_contract(hass: HomeAssistant) -> None:
    with (
        patch(f"{API}.async_login", new=AsyncMock(return_value=None)),
        patch(f"{API}.async_list_houses", new=AsyncMock(return_value=HOUSES)),
    ):
        result = await _start_and_login(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_GAS_HOUSE: NONE, CONF_ELECTRICITY_HOUSE: NONE},
        )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "contracts"
    assert result["errors"] == {"base": "no_contract_selected"}


async def test_form_aborts_when_houses_cannot_be_listed(hass: HomeAssistant) -> None:
    with (
        patch(f"{API}.async_login", new=AsyncMock(return_value=None)),
        patch(f"{API}.async_list_houses", new=AsyncMock(side_effect=Exception("boom"))),
    ):
        result = await _start_and_login(hass)

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


async def test_form_invalid_auth(hass: HomeAssistant) -> None:
    """A failing login should re-display the form with an invalid_auth error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(f"{API}.async_login", side_effect=Exception("bad credentials")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: USERNAME, CONF_PASSWORD: "wrong"},
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_options_flow_updates_contracts(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={
            CONF_USERNAME: USERNAME,
            CONF_PASSWORD: PASSWORD,
            CONF_GAS_HOUSE: GAS,
            CONF_ELECTRICITY_HOUSE: NONE,
        },
    )
    entry.add_to_hass(hass)

    with (
        patch(f"{API}.async_login", new=AsyncMock(return_value=None)),
        patch(f"{API}.async_list_houses", new=AsyncMock(return_value=HOUSES)),
        patch("custom_components.gazdebordeaux.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] == FlowResultType.FORM
        # The current mapping is preselected, including an explicit "none".
        assert _default(result["data_schema"], CONF_GAS_HOUSE) == GAS
        assert _default(result["data_schema"], CONF_ELECTRICITY_HOUSE) == NONE

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_USERNAME: USERNAME,
                CONF_PASSWORD: PASSWORD,
                RESET_STATISTICS: False,
                CONF_GAS_HOUSE: GAS,
                CONF_ELECTRICITY_HOUSE: ELEC,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_ELECTRICITY_HOUSE] == ELEC
    assert entry.data[CONF_GAS_HOUSE] == GAS

"""Tests for the gazdebordeaux sensor platform."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gazdebordeaux.const import (
    CONF_ELECTRICITY_HOUSE,
    CONF_GAS_HOUSE,
    DOMAIN,
    NONE,
)
from custom_components.gazdebordeaux.gazdebordeaux import TotalUsageRead

USERNAME = "user@example.com"
PASSWORD = "secret"
GAS = "/api/houses/gas-uuid"
ELEC = "/api/houses/elec-uuid"

GAS_TOTAL = TotalUsageRead(amountOfEnergy=1234.0, volumeOfEnergy=110.5, price=180.42)
ELEC_TOTAL = TotalUsageRead(amountOfEnergy=3283.0, volumeOfEnergy=0, price=634.0)

API = "custom_components.gazdebordeaux.coordinator.Gazdebordeaux"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> AsyncMock:
    entry.add_to_hass(hass)
    total_usage = AsyncMock(side_effect=lambda house: {GAS: GAS_TOTAL, ELEC: ELEC_TOTAL}[house])
    with (
        patch(f"{API}.async_login", new=AsyncMock(return_value=None)),
        patch(f"{API}.async_find_house", new=AsyncMock(return_value=GAS)),
        patch(f"{API}.async_get_total_usage", new=total_usage),
        # _insert_statistics also fetches daily history; make it a no-op for this test.
        patch(
            "custom_components.gazdebordeaux.coordinator.GdbCoordinator._insert_statistics",
            new=AsyncMock(return_value=None),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return total_usage


def _state(hass: HomeAssistant, unique_id: str):
    """Look a sensor up by unique id: entity id naming differs across HA versions."""
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, unique_id)
    return hass.states.get(entity_id) if entity_id else None


async def test_sensors_expose_total_usage_values(hass: HomeAssistant) -> None:
    """A legacy (v1) entry migrates to gas-only and keeps the gas entity ids."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )
    await _setup(hass, entry)

    state_volume = _state(hass, "gazpar_gas_usage_to_date")
    state_energy = _state(hass, "gazpar_gas_energy_to_date")
    state_cost = _state(hass, "gazpar_gas_cost_to_date")

    assert state_volume is not None and float(state_volume.state) == 110.5
    assert state_energy is not None and float(state_energy.state) == 1234.0
    assert state_cost is not None and float(state_cost.state) == 180.42
    assert _state(hass, "linky_electricity_energy_to_date") is None


async def test_gas_and_electricity_sensors(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={
            CONF_USERNAME: USERNAME,
            CONF_PASSWORD: PASSWORD,
            CONF_GAS_HOUSE: GAS,
            CONF_ELECTRICITY_HOUSE: ELEC,
        },
    )
    total_usage = await _setup(hass, entry)

    assert {c.args[0] for c in total_usage.await_args_list} == {GAS, ELEC}

    state_gas = _state(hass, "gazpar_gas_energy_to_date")
    state_elec = _state(hass, "linky_electricity_energy_to_date")
    state_elec_cost = _state(hass, "linky_electricity_cost_to_date")

    assert state_gas is not None and float(state_gas.state) == 1234.0
    assert state_elec is not None and float(state_elec.state) == 3283.0
    assert state_elec_cost is not None and float(state_elec_cost.state) == 634.0


async def test_electricity_only(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={
            CONF_USERNAME: USERNAME,
            CONF_PASSWORD: PASSWORD,
            CONF_GAS_HOUSE: NONE,
            CONF_ELECTRICITY_HOUSE: ELEC,
        },
    )
    total_usage = await _setup(hass, entry)

    assert [c.args[0] for c in total_usage.await_args_list] == [ELEC]
    assert _state(hass, "gazpar_gas_energy_to_date") is None
    assert _state(hass, "linky_electricity_energy_to_date") is not None

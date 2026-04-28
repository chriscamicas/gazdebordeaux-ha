"""Tests for the gazdebordeaux sensor platform."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gazdebordeaux.const import DOMAIN
from custom_components.gazdebordeaux.gazdebordeaux import TotalUsageRead

USERNAME = "user@example.com"
PASSWORD = "secret"


async def test_sensors_expose_total_usage_values(hass: HomeAssistant) -> None:
    """The three gas sensors should reflect the TotalUsageRead the API returns."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )
    entry.add_to_hass(hass)

    canned = TotalUsageRead(amountOfEnergy=1234.0, volumeOfEnergy=110.5, price=180.42)

    with (
        patch(
            "custom_components.gazdebordeaux.coordinator.Gazdebordeaux.async_login",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.gazdebordeaux.coordinator.Gazdebordeaux.async_get_total_usage",
            new=AsyncMock(return_value=canned),
        ),
        # _insert_statistics also fetches daily history; make it a no-op for this test.
        patch(
            "custom_components.gazdebordeaux.coordinator.GdbCoordinator._insert_statistics",
            new=AsyncMock(return_value=None),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state_volume = hass.states.get("sensor.current_bill_gas_usage_to_date")
    state_energy = hass.states.get("sensor.current_energy_usage_to_date")
    state_cost = hass.states.get("sensor.current_bill_gas_cost_to_date")

    assert state_volume is not None and float(state_volume.state) == 110.5
    assert state_energy is not None and float(state_energy.state) == 1234.0
    assert state_cost is not None and float(state_cost.state) == 180.42

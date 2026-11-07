"""Tests for config entry migration."""

from __future__ import annotations

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gazdebordeaux import async_migrate_entry
from custom_components.gazdebordeaux.const import (
    AUTO,
    CONF_ELECTRICITY_HOUSE,
    CONF_GAS_HOUSE,
    DOMAIN,
    HOUSE,
    NONE,
)


@pytest.mark.parametrize(
    ("legacy_house", "expected_gas"),
    [
        ("/api/houses/abc", "/api/houses/abc"),
        ("", AUTO),
        (None, AUTO),
    ],
)
async def test_migrate_v1_to_v2(hass: HomeAssistant, legacy_house, expected_gas) -> None:
    data = {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"}
    if legacy_house is not None:
        data[HOUSE] = legacy_house
    entry = MockConfigEntry(domain=DOMAIN, version=1, data=data)
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert entry.version == 2
    assert HOUSE not in entry.data
    assert entry.data[CONF_GAS_HOUSE] == expected_gas
    assert entry.data[CONF_ELECTRICITY_HOUSE] == NONE

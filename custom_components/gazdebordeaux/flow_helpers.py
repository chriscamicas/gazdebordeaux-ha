"""Helpers shared by the config and options flows."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    AUTO,
    CONF_ELECTRICITY_HOUSE,
    CONF_GAS_HOUSE,
    ENERGY_ELECTRICITY,
    ENERGY_GAS,
    NONE,
)
from .gazdebordeaux import Gazdebordeaux, House


def create_api(hass: HomeAssistant, data: dict[str, Any]) -> Gazdebordeaux:
    return Gazdebordeaux(
        async_create_clientsession(hass),
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
    )


async def validate_login(api: Gazdebordeaux) -> dict[str, str]:
    """Validate login data and return any errors."""
    errors: dict[str, str] = {}
    try:
        await api.async_login()
    except Exception:
        errors["base"] = "invalid_auth"
    return errors


def _default_house(houses: list[House], category: str, current: str | None) -> str:
    candidates = [h.path for h in houses if h.category == category]
    if current == NONE:
        return NONE
    if current in candidates:
        return current
    # No (or unknown/AUTO) current value: preselect the first matching contract.
    return candidates[0] if candidates else NONE


def _house_selector(houses: list[House], category: str) -> SelectSelector:
    options = [SelectOptionDict(value=NONE, label="Aucun")]
    options += [
        SelectOptionDict(value=h.path, label=h.label) for h in houses if h.category == category
    ]
    return SelectSelector(SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN))


def contracts_schema_fields(
    houses: list[House],
    current_gas: str | None = None,
    current_electricity: str | None = None,
) -> dict[Any, Any]:
    """Schema fields for picking the gas and electricity contracts.

    Both fields are Required with an explicit "none" option, so the voluptuous
    default re-injection on cleared Optional fields doesn't apply here.
    """
    if current_gas == AUTO:
        current_gas = None
    return {
        vol.Required(
            CONF_GAS_HOUSE, default=_default_house(houses, ENERGY_GAS, current_gas)
        ): _house_selector(houses, ENERGY_GAS),
        vol.Required(
            CONF_ELECTRICITY_HOUSE,
            default=_default_house(houses, ENERGY_ELECTRICITY, current_electricity),
        ): _house_selector(houses, ENERGY_ELECTRICITY),
    }


def validate_contracts(user_input: dict[str, Any]) -> dict[str, str]:
    if user_input.get(CONF_GAS_HOUSE, NONE) == NONE and (
        user_input.get(CONF_ELECTRICITY_HOUSE, NONE) == NONE
    ):
        return {"base": "no_contract_selected"}
    return {}

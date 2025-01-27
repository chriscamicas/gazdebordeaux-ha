"""Config flow for Gazdebordeaux integration."""
from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigEntry, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import selector

from .const import DOMAIN, HOUSE
from .gazdebordeaux import Gazdebordeaux, House
from .option_flow import GazdebordeauxOptionFlow

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class GazdebordeauxConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Gazdebordeaux."""

    VERSION = 2
    
    data: dict[str, str | None] = {}
    gdb: Gazdebordeaux | None = None

    def __init__(self) -> None:
        """Initialize a new GazdebordeauxConfigFlow."""
        self.reauth_entry: ConfigEntry | None = None
        self.utility_info: dict[str, Any] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
                
        if user_input is not None:
            self.gdb = Gazdebordeaux(
                async_create_clientsession(self.hass),
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )
            
            errors: dict[str, str] = {}
            try:
                await self.gdb.async_login()
            except Exception:
                errors["base"] = "invalid_auth"

            if not errors:
                self.data = user_input
                self.data[HOUSE] = None
                return await self.async_step_house()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors, last_step=False
        )

    async def async_step_house(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the house choice step."""

        if self.gdb is None:
            raise Exception("API not initialized")
        
        houses = await self.gdb.async_get_houses()
        
        if houses is None:
            raise Exception("Could not get houses")

        if len(houses) == 1:
            return self._async_create_gazdebordeaux_entry(self.data, houses[0])

        if user_input is not None:
            self.data[HOUSE] = user_input[HOUSE]
            house = next((house for house in houses if house.id == user_input[HOUSE]), None)
            if house is None:
                raise Exception("Could not get selected house")
            return self._async_create_gazdebordeaux_entry(self.data, house)

 
        schema = vol.Schema({
            vol.Required(HOUSE): selector(
                {
                    "select": {
                        "options": list(map(lambda house: {"label": f"{house.address} - {house.remoteAddressId} ({house.contractCategory})"  , "value": house.id}, houses))
                    }
                }
            )
        })

        return self.async_show_form(
            step_id="house", data_schema=schema, last_step=True
        )

    @callback
    def _async_create_gazdebordeaux_entry(self, data: dict[str, Any], house: House) -> ConfigFlowResult:
        """Create the config entry."""
        return self.async_create_entry(
            title=f"({data[CONF_USERNAME]}) - {house.address} - {house.remoteAddressId} ({house.contractCategory})",
            data=data,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry):
        """Get options flow for this handler"""
        return GazdebordeauxOptionFlow(config_entry)

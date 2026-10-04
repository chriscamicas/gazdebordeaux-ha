"""Config flow for Gazdebordeaux integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback

from .const import DOMAIN
from .flow_helpers import (
    contracts_schema_fields,
    create_api,
    validate_contracts,
    validate_login,
)
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

    def __init__(self) -> None:
        """Initialize a new GazdebordeauxConfigFlow."""
        self._login_data: dict[str, Any] = {}
        self._api: Gazdebordeaux | None = None
        self._houses: list[House] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_USERNAME].lower())
            self._abort_if_unique_id_configured()

            api = create_api(self.hass, user_input)
            errors = await validate_login(api)
            if not errors:
                self._login_data = user_input
                self._api = api
                return await self.async_step_contracts()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_contracts(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user map the account's contracts to gas and electricity."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = validate_contracts(user_input)
            if not errors:
                return self._async_create_gazdebordeaux_entry({**self._login_data, **user_input})

        if not self._houses:
            assert self._api is not None
            try:
                self._houses = await self._api.async_list_houses()
            except Exception:
                _LOGGER.error("Unable to list the account's contracts", exc_info=True)
                return self.async_abort(reason="cannot_connect")

        return self.async_show_form(
            step_id="contracts",
            data_schema=vol.Schema(contracts_schema_fields(self._houses)),
            errors=errors,
        )

    @callback
    def _async_create_gazdebordeaux_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Create the config entry."""
        return self.async_create_entry(
            title=f"({data[CONF_USERNAME]})",
            data=data,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry):
        """Get options flow for this handler"""
        return GazdebordeauxOptionFlow(config_entry)

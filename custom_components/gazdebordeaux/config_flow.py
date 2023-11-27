"""Config flow for Gazdebordeaux integration."""
from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import DOMAIN
from .gazdebordeaux import Gazdebordeaux

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def _validate_login(
    hass: HomeAssistant, login_data: dict[str, str]
) -> dict[str, str]:
    """Validate login data and return any errors."""
    api = Gazdebordeaux(
        async_create_clientsession(hass),
        login_data[CONF_USERNAME],
        login_data[CONF_PASSWORD],
    )
    errors: dict[str, str] = {}
    try:
        await api.async_login()
    except Exception:
        errors["base"] = "invalid_auth"
    return errors


class GazdebordeauxConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Gazdebordeaux."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize a new GazdebordeauxConfigFlow."""
        self.reauth_entry: config_entries.ConfigEntry | None = None
        self.utility_info: dict[str, Any] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:

            errors = await _validate_login(self.hass, user_input)
            if not errors:
                return self._async_create_gazdebordeaux_entry(user_input)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )


    @callback
    def _async_create_gazdebordeaux_entry(self, data: dict[str, Any]) -> FlowResult:
        """Create the config entry."""
        return self.async_create_entry(
            title=f"({data[CONF_USERNAME]})",
            data=data,
        )

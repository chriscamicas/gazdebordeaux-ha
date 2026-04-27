"""Config flow for Gazdebordeaux integration."""
from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigEntry, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import DOMAIN, HOUSES
from .gazdebordeaux import Gazdebordeaux
from .option_flow import GazdebordeauxOptionFlow

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


class GazdebordeauxConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Gazdebordeaux."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize a new GazdebordeauxConfigFlow."""
        self.reauth_entry: ConfigEntry | None = None
        self._user_data: dict[str, Any] = {}
        self._api: Gazdebordeaux | None = None
        self._discovered_houses: list[dict] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: login."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._api = Gazdebordeaux(
                async_create_clientsession(self.hass),
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )
            try:
                await self._api.async_login()
            except Exception:
                errors["base"] = "invalid_auth"
            if not errors:
                self._user_data = user_input
                return await self.async_step_houses()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_houses(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: select houses."""
        if user_input is not None:
            selected = user_input.get("selected_houses", [])
            houses_config = [h for h in self._discovered_houses if h["path"] in selected]
            return self.async_create_entry(
                title=f"({self._user_data[CONF_USERNAME]})",
                data=self._user_data,
                options={HOUSES: houses_config},
            )

        # Discover houses
        try:
            house_paths = await self._api.async_load_all_houses()
        except Exception:
            _LOGGER.error("Failed to load houses", exc_info=True)
            return self.async_create_entry(
                title=f"({self._user_data[CONF_USERNAME]})", data=self._user_data)

        self._discovered_houses = []
        options = {}
        for path in house_paths:
            try:
                house_type = await self._api.async_detect_house_type(path)
            except Exception:
                house_type = "unknown"
            label = {"gas": "Gaz", "elec": "Électricité"}.get(house_type, house_type)
            self._discovered_houses.append({"path": path, "type": house_type})
            options[path] = label

        if not options:
            return self.async_create_entry(
                title=f"({self._user_data[CONF_USERNAME]})", data=self._user_data)

        return self.async_show_form(
            step_id="houses",
            data_schema=vol.Schema({
                vol.Required("selected_houses", default=list(options.keys())): cv.multi_select(options),
            }),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry):
        """Get options flow for this handler"""
        return GazdebordeauxOptionFlow(config_entry)
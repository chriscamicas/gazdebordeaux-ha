"""Options flow for Gazdebordeaux integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import OptionsFlow, ConfigEntry, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from homeassistant.helpers import config_validation as cv
from .const import DOMAIN, RESET_STATISTICS, HOUSE, HOUSES
from .gazdebordeaux import Gazdebordeaux

_LOGGER = logging.getLogger(__name__)

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


class GazdebordeauxOptionFlow(OptionsFlow):
    """Handle an options flow for Gazdebordeaux."""

    VERSION = 1

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        # Ne PAS faire self.config_entry = config_entry
        # HA le gère en interne via la classe parente
        self._user_inputs: dict = {}  # Attribut d'instance

    async def async_step_init(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        """Gestion de l'étape 'init'."""

        option_form = vol.Schema(
            {
                vol.Required(
                    CONF_USERNAME,
                    default=self.config_entry.data.get(CONF_USERNAME, ""),
                ): str,
                vol.Required(
                    CONF_PASSWORD,
                    default=self.config_entry.data.get(CONF_PASSWORD, ""),
                ): str,
                vol.Optional(
                    RESET_STATISTICS,
                    default=self.config_entry.data.get(RESET_STATISTICS, False),
                ): bool,
                vol.Optional(
                    HOUSE,
                    description={"suggested_value": self.config_entry.data.get(HOUSE, "")},
                ): str,
            }
        )

        if user_input is None:
            _LOGGER.debug(
                "option_flow step user (1). 1er appel : pas de user_input -> "
                "on affiche le form user_form"
            )
            return self.async_show_form(step_id="init", data_schema=option_form)

        # 2ème appel : il y a des user_input -> on stocke le résultat
        _LOGGER.debug(
            "option_flow step user (2). Valeurs reçues: %s", user_input
        )
        # On mémorise les user_input
        self._user_inputs.update(user_input)

        # On passe à la sélection des maisons
        return await self.async_step_houses()

    async def async_step_houses(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        """Select which houses to monitor."""
        if user_input is not None:
            selected = user_input.get("selected_houses", [])
            houses_config = [h for h in self._discovered_houses if h["path"] in selected]
            self._user_inputs[HOUSES] = houses_config
            return await self.async_end()

        # Discover houses
        api = Gazdebordeaux(
            async_create_clientsession(self.hass),
            self._user_inputs.get(CONF_USERNAME, self.config_entry.data.get(CONF_USERNAME, "")),
            self._user_inputs.get(CONF_PASSWORD, self.config_entry.data.get(CONF_PASSWORD, "")),
        )
        try:
            await api.async_login()
            house_paths = await api.async_load_all_houses()
        except Exception:
            _LOGGER.error("Failed to load houses", exc_info=True)
            return await self.async_end()

        self._discovered_houses = []
        options = {}
        for path in house_paths:
            try:
                house_type = await api.async_detect_house_type(path)
            except Exception:
                house_type = "unknown"
            label = {"gas": "Gaz", "elec": "Électricité"}.get(house_type, house_type)
            self._discovered_houses.append({"path": path, "type": house_type})
            options[path] = label

        if not options:
            return await self.async_end()

        current = self.config_entry.options.get(HOUSES, [])
        defaults = [h["path"] for h in current] if current else list(options.keys())

        return self.async_show_form(
            step_id="houses",
            data_schema=vol.Schema({
                vol.Required("selected_houses", default=defaults): cv.multi_select(options),
            }),
        )

    async def async_end(self) -> ConfigFlowResult:
        """Save config and options."""
        data = {
            CONF_USERNAME: self._user_inputs.get(CONF_USERNAME, self.config_entry.data.get(CONF_USERNAME)),
            CONF_PASSWORD: self._user_inputs.get(CONF_PASSWORD, self.config_entry.data.get(CONF_PASSWORD)),
            RESET_STATISTICS: self._user_inputs.get(RESET_STATISTICS, False),
            HOUSE: self._user_inputs.get(HOUSE, self.config_entry.data.get(HOUSE, "")),
        }
        self.hass.config_entries.async_update_entry(self.config_entry, data=data)

        houses = self._user_inputs.get(HOUSES, self.config_entry.options.get(HOUSES, []))
        return self.async_create_entry(title="", data={HOUSES: houses})

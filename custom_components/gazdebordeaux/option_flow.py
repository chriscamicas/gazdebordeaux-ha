"""Config flow for Gazdebordeaux integration."""
from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import OptionsFlow, ConfigEntry, ConfigFlowResult
from homeassistant.helpers import selector
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.components.sensor.const import DOMAIN as SENSOR_DOMAIN

from .const import DOMAIN, RESET_STATISTICS
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
    """Handle a config flow for Gazdebordeaux."""

    VERSION = 1
    _user_inputs: dict = {}

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Gestion de l'étape 'init'. Point d'entrée de notre
        optionsFlow. Comme pour le ConfigFlow, cette méthode est appelée 2 fois
        """

        reset_stats: Any = False
        if RESET_STATISTICS in self.config_entry.data:
            reset_stats = self.config_entry.data[RESET_STATISTICS]

        option_form = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=self.config_entry.data[CONF_USERNAME]): str,
                vol.Required(CONF_PASSWORD, default=self.config_entry.data[CONF_PASSWORD]): str,
                vol.Optional(RESET_STATISTICS, default=reset_stats): bool,
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
            "option_flow step user (2). On a reçu les valeurs: %s", user_input
        )
        # On mémorise les user_input
        self._user_inputs.update(user_input)

        # On appelle le step de fin pour enregistrer les modifications
        return await self.async_end()

    async def async_end(self):
        """Finalization of the ConfigEntry creation"""
        _LOGGER.info(
            "Recreation de l'entry %s. La nouvelle config est maintenant : %s",
            self.config_entry.entry_id,
            self._user_inputs,
        )

        # Modification de la configEntry avec nos nouvelles valeurs
        self.hass.config_entries.async_update_entry(
            self.config_entry, data=self._user_inputs
        )
        # On ne fait rien dans l'objet options dans la configEntry
        return self.async_create_entry(title=None, data=None)
"""Options flow for Gazdebordeaux integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import OptionsFlow, ConfigEntry, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import DOMAIN, RESET_STATISTICS, HOUSE
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
                    default=self.config_entry.data.get(HOUSE, ""),
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

        # On appelle le step de fin pour enregistrer les modifications
        return await self.async_end()

    async def async_end(self) -> ConfigFlowResult:
        """Finalisation et sauvegarde des modifications."""
        _LOGGER.info(
            "Recreation de l'entry %s. Nouvelle config : %s",
            self.config_entry.entry_id,
            self._user_inputs,
        )

        # Modification de la configEntry avec nos nouvelles valeurs
        self.hass.config_entries.async_update_entry(
            self.config_entry, data=self._user_inputs
        )

        return self.async_create_entry(title="", data={})

"""Options flow for Gazdebordeaux integration."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

from .const import CONF_ELECTRICITY_HOUSE, CONF_GAS_HOUSE, RESET_STATISTICS
from .flow_helpers import (
    contracts_schema_fields,
    create_api,
    validate_contracts,
    validate_login,
)
from .gazdebordeaux import House

_LOGGER = logging.getLogger(__name__)


class GazdebordeauxOptionFlow(OptionsFlow):
    """Handle an options flow for Gazdebordeaux."""

    VERSION = 1

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        # Ne PAS faire self.config_entry = config_entry
        # HA le gère en interne via la classe parente
        self._user_inputs: dict = {}  # Attribut d'instance
        self._houses: list[House] = []

    async def async_step_init(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Gestion de l'étape 'init'."""
        data = self.config_entry.data
        errors: dict[str, str] = {}

        if user_input is not None:
            # 2ème appel : il y a des user_input -> on valide puis on stocke le résultat
            _LOGGER.debug("option_flow step user (2). Valeurs reçues: %s", user_input)
            errors = validate_contracts(user_input)
            if not errors:
                errors = await validate_login(create_api(self.hass, user_input))
            if not errors:
                self._user_inputs.update(user_input)
                # On appelle le step de fin pour enregistrer les modifications
                return await self.async_end()

        if not self._houses:
            try:
                self._houses = await create_api(self.hass, dict(data)).async_list_houses()
            except Exception:
                _LOGGER.error("Unable to list the account's contracts", exc_info=True)
                return self.async_abort(reason="cannot_connect")

        option_form = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=data.get(CONF_USERNAME, "")): str,
                vol.Required(CONF_PASSWORD, default=data.get(CONF_PASSWORD, "")): str,
                vol.Optional(
                    RESET_STATISTICS,
                    default=data.get(RESET_STATISTICS, False),
                ): bool,
                **contracts_schema_fields(
                    self._houses,
                    data.get(CONF_GAS_HOUSE),
                    data.get(CONF_ELECTRICITY_HOUSE),
                ),
            }
        )

        _LOGGER.debug("option_flow step user (1). Affichage du formulaire")
        return self.async_show_form(step_id="init", data_schema=option_form, errors=errors)

    async def async_end(self) -> ConfigFlowResult:
        """Finalisation et sauvegarde des modifications."""
        _LOGGER.info(
            "Recreation de l'entry %s. Nouvelle config : %s",
            self.config_entry.entry_id,
            {k: v for k, v in self._user_inputs.items() if k != CONF_PASSWORD},
        )

        # Modification de la configEntry avec nos nouvelles valeurs
        self.hass.config_entries.async_update_entry(self.config_entry, data=self._user_inputs)

        return self.async_create_entry(title="", data={})

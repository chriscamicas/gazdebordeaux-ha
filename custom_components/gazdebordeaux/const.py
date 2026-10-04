"""Constants for the Gaz de Bordeaux integration."""

DOMAIN = "gazdebordeaux"
RESET_STATISTICS = "reset_stats"

# Legacy (config entry v1) free-text house path, only read by the migration.
HOUSE = "house"

CONF_GAS_HOUSE = "gas_house"
CONF_ELECTRICITY_HOUSE = "electricity_house"

# Sentinel values for the house selectors.
NONE = "none"
# Resolved at runtime to the first house of the matching category.
AUTO = "auto"

ENERGY_GAS = "gas"
ENERGY_ELECTRICITY = "electricity"

# Config entry key holding the house path for each energy type.
ENERGY_HOUSE_KEYS = {
    ENERGY_GAS: CONF_GAS_HOUSE,
    ENERGY_ELECTRICITY: CONF_ELECTRICITY_HOUSE,
}

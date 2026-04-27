"""Support for Opower sensors."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from typing import Optional
from .gazdebordeaux import TotalUsageRead, HouseData

from homeassistant.components.sensor.const import (
    SensorDeviceClass,
    SensorStateClass
)
from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import GdbCoordinator


@dataclass
class GdbEntityDescriptionMixin:
    """Mixin values for required keys."""

    value_fn: Callable[[dict[str, HouseData]], Optional[float]]


@dataclass
class GdbEntityDescription(SensorEntityDescription, GdbEntityDescriptionMixin):
    """Class describing Gaz de Bordeaux sensors entities."""


# suggested_display_precision=0 for all sensors since
# Opower provides 0 decimal points for all these.
# (for the statistics in the energy dashboard Opower does provide decimal points)

def _gas(data: dict[str, HouseData]) -> Optional[HouseData]:
    return data.get("gas")

def _elec(data: dict[str, HouseData]) -> Optional[HouseData]:
    return data.get("elec")

GAS_SENSORS: tuple[GdbEntityDescription, ...] = (
    GdbEntityDescription(
        key="gas_usage_to_date",
        name="Current bill gas usage to date",
        device_class=SensorDeviceClass.GAS,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        value_fn=lambda d: _gas(d).total.volumeOfEnergy if _gas(d) else None,
    ),
    GdbEntityDescription(
        key="gas_energy_to_date",
        name="Current energy usage to date",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        value_fn=lambda d: _gas(d).total.amountOfEnergy if _gas(d) else None,
    ),
    GdbEntityDescription(
        key="gas_cost_to_date",
        name="Current bill gas cost to date",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="€",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        value_fn=lambda d: _gas(d).total.price if _gas(d) else None,
    ),
)

ELEC_SENSORS: tuple[GdbEntityDescription, ...] = (
    GdbEntityDescription(
        key="elec_usage_to_date",
        name="Current elec usage to date",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        value_fn=lambda d: _elec(d).total.amountOfEnergy if _elec(d) else None,
    ),
    GdbEntityDescription(
        key="elec_cost_to_date",
        name="Current elec cost to date",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="€",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        value_fn=lambda d: _elec(d).total.price if _elec(d) else None,
    ),
)

MONTHLY_SENSORS: tuple[GdbEntityDescription, ...] = (
    GdbEntityDescription(
        key="gas_current_month_cost",
        name="GdB gas current month cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="€",
        suggested_display_precision=2,
        value_fn=lambda d: _gas(d).current_month.price if _gas(d) and _gas(d).current_month else None,
    ),
    GdbEntityDescription(
        key="gas_previous_month_cost",
        name="GdB gas previous month cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="€",
        suggested_display_precision=2,
        value_fn=lambda d: _gas(d).previous_month.price if _gas(d) and _gas(d).previous_month else None,
    ),
    GdbEntityDescription(
        key="elec_current_month_cost",
        name="GdB elec current month cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="€",
        suggested_display_precision=2,
        value_fn=lambda d: _elec(d).current_month.price if _elec(d) and _elec(d).current_month else None,
    ),
    GdbEntityDescription(
        key="elec_previous_month_cost",
        name="GdB elec previous month cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="€",
        suggested_display_precision=2,
        value_fn=lambda d: _elec(d).previous_month.price if _elec(d) and _elec(d).previous_month else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the Gdb sensor."""

    coordinator: GdbCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[GdbSensor | GdbLastUpdateSensor] = []

    device_id = f"gazpar"
    device = DeviceInfo(
        identifiers={(DOMAIN, device_id)},
        name=f"Gaz de Bordeaux",
        manufacturer="Regaz",
        model="gazpar",
        entry_type=DeviceEntryType.SERVICE,
    )
    # Gas period sensors (always, backward compat)
    for desc in GAS_SENSORS:
        entities.append(GdbSensor(coordinator, desc, "", device, device_id))

    # Elec + monthly sensors (if multi-house configured)
    houses = entry.options.get("houses", [])
    has_elec = any(h.get("type") == "elec" for h in houses)
    has_gas = any(h.get("type") == "gas" for h in houses)

    if has_elec:
        for desc in ELEC_SENSORS:
            entities.append(GdbSensor(coordinator, desc, "", device, device_id))

    if houses:
        for desc in MONTHLY_SENSORS:
            if desc.key.startswith("gas_") and not has_gas:
                continue
            if desc.key.startswith("elec_") and not has_elec:
                continue
            entities.append(GdbSensor(coordinator, desc, "", device, device_id))

    entities.append(GdbLastUpdateSensor(coordinator, device, device_id))

    async_add_entities(entities)


class GdbSensor(CoordinatorEntity[GdbCoordinator], SensorEntity):
    """Representation of an Gdb sensor."""

    entity_description: GdbEntityDescription

    def __init__(
        self,
        coordinator: GdbCoordinator,
        description: GdbEntityDescription,
        utility_account_id: str,
        device: DeviceInfo,
        device_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{device_id}_{description.key}"
        self._attr_device_info = device
        self.utility_account_id = utility_account_id

    @property
    def native_value(self) -> StateType:
        """Return the state."""
        if self.coordinator.data is not None:
            return self.entity_description.value_fn(
                self.coordinator.data
            )
        return None


# Nouveau sensor de dernière actualisation
class GdbLastUpdateSensor(CoordinatorEntity[GdbCoordinator], SensorEntity):
    """Sensor affichant la date de dernière actualisation."""

    _attr_name = "Gaz de Bordeaux Last Update"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-check"

    def __init__(
        self,
        coordinator: GdbCoordinator,
        device: DeviceInfo,
        device_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{device_id}_last_update"
        self._attr_device_info = device

    @property
    def native_value(self) -> datetime | None:
        """Return the last update datetime."""
        if self.coordinator.last_update is None:
            return None
        # HA exige un datetime avec timezone pour SensorDeviceClass.TIMESTAMP
        return dt_util.as_local(self.coordinator.last_update)

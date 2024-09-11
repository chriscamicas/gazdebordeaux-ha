"""Support for Opower sensors."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .gazdebordeaux import TotalUsageRead

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

from .const import DOMAIN
from .coordinator import GdbCoordinator


@dataclass
class GdbEntityDescriptionMixin:
    """Mixin values for required keys."""

    value_fn: Callable[[TotalUsageRead], str | float]


@dataclass
class GdbEntityDescription(SensorEntityDescription, GdbEntityDescriptionMixin):
    """Class describing Gaz de Bordeaux sensors entities."""


# suggested_display_precision=0 for all sensors since
# Opower provides 0 decimal points for all these.
# (for the statistics in the energy dashboard Opower does provide decimal points)

GAS_SENSORS: tuple[GdbEntityDescription, ...] = (
    GdbEntityDescription(
        key="gas_usage_to_date",
        name="Current bill gas usage to date",
        device_class=SensorDeviceClass.GAS,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        value_fn=lambda data: data.volumeOfEnergy,
    ),
    GdbEntityDescription(
        key="gas_energy_to_date",
        name="Current energy usage to date",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        value_fn=lambda data: data.amountOfEnergy,
    ),
    GdbEntityDescription(
        key="gas_cost_to_date",
        name="Current bill gas cost to date",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="€",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        value_fn=lambda data: data.price,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the Gdb sensor."""

    coordinator: GdbCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[GdbSensor] = []
    totalUsage = coordinator.data 

    device_id = f"gazpar"
    device = DeviceInfo(
        identifiers={(DOMAIN, device_id)},
        name=f"Gaz de Bordeaux",
        manufacturer="Regaz",
        model="gazpar",
        entry_type=DeviceEntryType.SERVICE,
    )
    sensors: tuple[GdbEntityDescription, ...] = GAS_SENSORS
    for sensor in sensors:
        entities.append(
            GdbSensor(
                coordinator,
                sensor,
                "",
                device,
                device_id,
            )
        )

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
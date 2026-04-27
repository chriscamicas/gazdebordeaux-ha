"""Coordinator to handle Gaz de Bordeaux connections."""
from datetime import datetime, timedelta
import logging
from types import MappingProxyType
from typing import Any, cast

from .const import RESET_STATISTICS, HOUSE, HOUSES
from .gazdebordeaux import Gazdebordeaux, DailyUsageRead, TotalUsageRead, HouseData

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMetaData,
    StatisticMeanType
)
from homeassistant.components.recorder.util import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    UnitOfEnergy,
    UnitOfVolume,
    CURRENCY_EURO
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class GdbCoordinator(DataUpdateCoordinator[dict[str, HouseData]]):
    """Handle fetching GazdeBordeaux data for multiple houses."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_data: MappingProxyType[str, Any],
        entry_options: MappingProxyType[str, Any] | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="gazdebordeaux",
            update_interval=timedelta(hours=12),
        )

        self.last_update: datetime | None = None

        house: Any = None
        if HOUSE in entry_data:
            house = entry_data[HOUSE]

        self.api = Gazdebordeaux(
            aiohttp_client.async_get_clientsession(hass),
            entry_data[CONF_USERNAME],
            entry_data[CONF_PASSWORD],
            None,
            house
        )

        # Houses to query (from options, or fallback to selectedHouse)
        self._configured_houses: list[dict] = []
        if entry_options and HOUSES in entry_options:
            self._configured_houses = entry_options[HOUSES]

        self.reset = False
        if RESET_STATISTICS in entry_data:
            self.reset = bool(entry_data[RESET_STATISTICS])
            if self.reset:
                _LOGGER.debug("Asked to reset all statistics...")
                entries = self.hass.config_entries.async_entries(DOMAIN)
                house = entry_data.get(HOUSE)
                _LOGGER.debug("Updating config...")
                self.hass.config_entries.async_update_entry(
                    entries[0], data={
                        CONF_USERNAME: entry_data[CONF_USERNAME],
                        CONF_PASSWORD: entry_data[CONF_PASSWORD],
                        RESET_STATISTICS: False,
                        HOUSE: house,
                    }
                )

        @callback
        def _dummy_listener() -> None:
            pass
        self.async_add_listener(_dummy_listener)

    async def _async_update_data(self) -> dict[str, HouseData]:
        """Fetch data from all configured houses."""
        try:
            await self.api.async_login()
        except Exception as err:
            raise ConfigEntryAuthFailed from err

        houses_data: dict[str, HouseData] = {}

        if self._configured_houses:
            # Multi-house mode: query each configured house
            for h in self._configured_houses:
                try:
                    data = await self.api.async_get_house_data(h["path"])
                    houses_data[data.house_type] = data
                except Exception:
                    _LOGGER.error("Error fetching house %s", h["path"], exc_info=True)
        else:
            # Legacy mode: query only selectedHouse (backward compat)
            try:
                data = await self.api.async_get_house_data(
                    self.api._selectedHouse or (await self.api.async_load_houses())[0]
                )
                houses_data[data.house_type] = data
            except Exception:
                _LOGGER.error("Error fetching default house", exc_info=True)

        # Statistics import (gas only, backward compat)
        if "gas" in houses_data:
            await self._insert_statistics()

        self.last_update = datetime.now()
        return houses_data

    async def _insert_statistics(self) -> None:
        """Insert gdb statistics."""
        cost_statistic_id = f"{DOMAIN}:energy_cost"
        consumption_statistic_id = f"{DOMAIN}:energy_consumption"
        volume_statistic_id = f"{DOMAIN}:volume"

        if self.reset:
            _LOGGER.debug("Resetting all statistics...")

        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, consumption_statistic_id, True, set()
        )
        if not last_stat:
            _LOGGER.debug("Updating statistic for the first time")
            usage_reads = await self._async_get_all_data()
            cost_sum = 0.0
            consumption_sum = 0.0
            volume_sum = 0.0
            last_stat_ts = None
        else:
            last_stat_ts = last_stat[consumption_statistic_id][0]["start"]
            last_stat_date = datetime.fromtimestamp(last_stat_ts)
            _LOGGER.debug("Last stat found for %s...", last_stat_date.strftime("%Y-%m-%d"))
            usage_reads = await self._async_get_recent_usage_reads(last_stat_ts)
            if not usage_reads:
                _LOGGER.debug("No recent usage/cost data. Skipping update")
                return

            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                usage_reads[0].date,
                None,
                {cost_statistic_id, consumption_statistic_id, volume_statistic_id},
                "day",
                None,
                {"state", "sum"},
            )
            cost_sum = cast(float, stats[cost_statistic_id][0]["sum"])
            consumption_sum = cast(float, stats[consumption_statistic_id][0]["sum"])
            volume_sum = cast(float, stats[volume_statistic_id][0]["sum"])

        cost_statistics = []
        consumption_statistics = []
        volume_statistics = []

        for usage_read in usage_reads:
            start = usage_read.date
            if last_stat_ts is not None:
                if start.timestamp() <= last_stat_ts:
                    continue
                if start.date() == last_stat_date.date():
                    continue

            cost_sum += usage_read.price
            consumption_sum += usage_read.amountOfEnergy
            volume_sum += usage_read.volumeOfEnergy

            cost_statistics.append(StatisticData(start=start, state=usage_read.price, sum=cost_sum))
            consumption_statistics.append(StatisticData(start=start, state=usage_read.amountOfEnergy, sum=consumption_sum))
            volume_statistics.append(StatisticData(start=start, state=usage_read.volumeOfEnergy, sum=volume_sum))

        name_prefix = "Gaz de Bordeaux"

        cost_metadata = StatisticMetaData(
            has_mean=False, mean_type=StatisticMeanType.NONE, unit_class=None, has_sum=True,
            name=f"{name_prefix} cost", source=DOMAIN, statistic_id=cost_statistic_id,
            unit_of_measurement=CURRENCY_EURO, device_class=SensorDeviceClass.MONETARY,
            state_class=SensorStateClass.TOTAL
        )
        consumption_metadata = StatisticMetaData(
            has_mean=False, mean_type=StatisticMeanType.NONE, unit_class="energy", has_sum=True,
            name=f"{name_prefix} consumption", source=DOMAIN, statistic_id=consumption_statistic_id,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL
        )
        volume_metadata = StatisticMetaData(
            has_mean=False, mean_type=StatisticMeanType.NONE, unit_class="volume", has_sum=True,
            name=f"{name_prefix} volume", source=DOMAIN, statistic_id=volume_statistic_id,
            unit_of_measurement=UnitOfVolume.CUBIC_METERS, device_class=SensorDeviceClass.GAS,
            state_class=SensorStateClass.TOTAL
        )

        async_add_external_statistics(self.hass, cost_metadata, cost_statistics)
        async_add_external_statistics(self.hass, consumption_metadata, consumption_statistics)
        async_add_external_statistics(self.hass, volume_metadata, volume_statistics)

    async def _async_get_all_data(self) -> list[DailyUsageRead]:
        start = datetime(datetime.today().year - 1, 1, 1)
        end = datetime.now()
        return await self.api.async_get_daily_usage(start, end)

    async def _async_get_recent_usage_reads(self, last_stat_time: float) -> list[DailyUsageRead]:
        return await self.api.async_get_daily_usage(
            datetime.fromtimestamp(last_stat_time),
            datetime.now(),
        )

"""Coordinator to handle Opower connections."""
from datetime import datetime, timedelta
import logging
from types import MappingProxyType
from typing import Any, cast

from .const import RESET_STATISTICS
from .gazdebordeaux import Gazdebordeaux, DailyUsageRead, TotalUsageRead

from homeassistant.components.recorder.util import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period
)
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class GdbCoordinator(DataUpdateCoordinator[TotalUsageRead]):
    """Handle fetching GazdeBordeaux data, updating sensors and inserting statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_data: MappingProxyType[str, Any],
    ) -> None:
        """Initialize the data handler."""
        super().__init__(
            hass,
            _LOGGER,
            name="gazdebordeaux",
            # Data is updated daily.
            # Refresh every 12h to be at most 12h behind.
            update_interval=timedelta(hours=12),
        )
        self.api = Gazdebordeaux(
            aiohttp_client.async_get_clientsession(hass),
            entry_data[CONF_USERNAME],
            entry_data[CONF_PASSWORD],
        )
        self.reset = False
        if RESET_STATISTICS in entry_data:
            _LOGGER.debug("Asked to reset all statistics...")
            self.reset = bool(entry_data[RESET_STATISTICS])
            entries=self.hass.config_entries.async_entries(DOMAIN)
            _LOGGER.debug("Updating config...")
            self.hass.config_entries.async_update_entry(
                entries[0], data={
                    CONF_USERNAME: entry_data[CONF_USERNAME],
                    CONF_PASSWORD: entry_data[CONF_PASSWORD],
                    RESET_STATISTICS: False
                }
            )

        @callback
        def _dummy_listener() -> None:
            pass

        # Force the coordinator to periodically update by registering at least one listener.
        # Needed when the _async_update_data below returns {} for utilities that don't provide
        # forecast, which results to no sensors added, no registered listeners, and thus
        # _async_update_data not periodically getting called which is needed for _insert_statistics.
        self.async_add_listener(_dummy_listener)

    async def _async_update_data(
        self,
    ) -> TotalUsageRead:
        """Fetch data from API endpoint."""
        try:
            # Login expires after a few minutes.
            # Given the infrequent updating (every 12h)
            # assume previous session has expired and re-login.
            await self.api.async_login()
        except Exception as err:
            raise ConfigEntryAuthFailed from err

        totalUsage: TotalUsageRead = await self.api.async_get_total_usage()

        # Because Opower provides historical usage/cost with a delay of a couple of days
        # we need to insert data into statistics.
        await self._insert_statistics()

        return totalUsage


    async def _insert_statistics(self) -> None:
        """Insert gdb statistics."""
        cost_statistic_id = f"{DOMAIN}:energy_cost"
        consumption_statistic_id = f"{DOMAIN}:energy_consumption"
        volume_statistic_id = f"{DOMAIN}:volume"
        _LOGGER.debug(
            "Updating Statistics for %s, %s and %s",
            cost_statistic_id,
            consumption_statistic_id,
            volume_statistic_id
        )

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
            last_stat_ts = last_stat[consumption_statistic_id][0]["start"] # type: ignore
            last_stat_date = datetime.fromtimestamp(last_stat_ts)
            _LOGGER.debug("Last stat found for %s...", last_stat_date.strftime("%Y-%m-%d"))
            usage_reads = await self._async_get_recent_usage_reads(
                last_stat_ts
            )
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
                {"sate", "sum"},
            )
            # s:StatisticsRow =stats[cost_statistic_id][0]
            
            cost_sum = cast(float, stats[cost_statistic_id][0]["sum"]) # type: ignore
            consumption_sum = cast(float, stats[consumption_statistic_id][0]["sum"])  # type: ignore
            volume_sum = cast(float, stats[volume_statistic_id][0]["sum"])  # type: ignore
            # last_stat_ts = stats[cost_statistic_id][0]["start"]  # type: ignore

        cost_statistics = []
        consumption_statistics = []
        volume_statistics = []

        for usage_read in usage_reads:
            start = usage_read.date
            start.tzinfo
            if last_stat_ts is not None:
                if start.timestamp() <= last_stat_ts:
                    _LOGGER.debug("Skipping data for %s (timestamp)", start.strftime("%Y-%m-%d"))
                    continue
                # if we are on the same day, skip it as well regarding of the time (to prevent multiple run for the same day)
                if start.date() == last_stat_date.date():
                    _LOGGER.debug("Skipping data for %s (same date)", start.strftime("%Y-%m-%d"))
                    continue
            
            _LOGGER.debug("Importing data for %s...", start.strftime("%Y-%m-%d"))

            cost_sum += usage_read.price
            consumption_sum += usage_read.amountOfEnergy
            volume_sum += usage_read.volumeOfEnergy

            cost_statistics.append(
                StatisticData(
                    start=start, state=usage_read.price, sum=cost_sum
                )
            )
            consumption_statistics.append(
                StatisticData(
                    start=start, state=usage_read.amountOfEnergy, sum=consumption_sum
                )
            )
            volume_statistics.append(
                StatisticData(
                    start=start, state=usage_read.volumeOfEnergy, sum=volume_sum
                )
            )

        name_prefix = " ".join(
            (
                "Gaz de Bordeaux",
            )
        )
        cost_metadata = StatisticMetaData(
            has_mean=False,
            has_sum=True,
            name=f"{name_prefix} cost",
            source=DOMAIN,
            statistic_id=cost_statistic_id,
            unit_of_measurement=None,
        )
        consumption_metadata = StatisticMetaData(
            has_mean=False,
            has_sum=True,
            name=f"{name_prefix} consumption",
            source=DOMAIN,
            statistic_id=consumption_statistic_id,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR
        )
        volume_metadata = StatisticMetaData(
            has_mean=False,
            has_sum=True,
            name=f"{name_prefix} volume",
            source=DOMAIN,
            statistic_id=volume_statistic_id,
            unit_of_measurement=UnitOfVolume.CUBIC_METERS
        )

        async_add_external_statistics(self.hass, cost_metadata, cost_statistics)
        async_add_external_statistics(self.hass, consumption_metadata, consumption_statistics)
        async_add_external_statistics(self.hass, volume_metadata, volume_statistics)

    async def _async_get_all_data(self) -> list[DailyUsageRead]:
        """Get all cost reads since account activation but at different resolutions depending on age.

        - month resolution for all years (since account activation)
        - day resolution for past 3 years (if account's read resolution supports it)
        - hour resolution for past 2 months (if account's read resolution supports it)
        """
        usage_reads = []

        # if start=None it will only default to beginning of current year, let's import 1 year more
        start = datetime(datetime.today().year-1, 1, 1)
        end = datetime.now()
        usage_reads = await self.api.async_get_daily_usage(start, end)
        return usage_reads

    async def _async_get_recent_usage_reads(self, last_stat_time: float) -> list[DailyUsageRead]:
        """Get cost reads within the past 30 days to allow corrections in data from utilities."""
        return await self.api.async_get_daily_usage(
            # datetime.fromtimestamp(last_stat_time) - timedelta(days=30),
            datetime.fromtimestamp(last_stat_time),
            datetime.now(),
        )
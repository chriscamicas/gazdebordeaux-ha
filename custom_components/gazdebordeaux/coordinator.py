"""Coordinator to handle Opower connections."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.components.recorder.util import get_instance
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    CURRENCY_EURO,
    UnitOfEnergy,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    AUTO,
    DOMAIN,
    ENERGY_ELECTRICITY,
    ENERGY_GAS,
    ENERGY_HOUSE_KEYS,
    NONE,
    RESET_STATISTICS,
)
from .gazdebordeaux import DailyUsageRead, Gazdebordeaux, TotalUsageRead, paris_tz

_LOGGER = logging.getLogger(__name__)


def _has_usage(usage_reads: list[DailyUsageRead]) -> bool:
    return any(read.amountOfEnergy or read.price for read in usage_reads)


@dataclass(frozen=True)
class StatisticSpec:
    """One external statistic fed from the daily usage reads."""

    statistic_id: str
    name: str
    # Must be a recorder unit converter class, or None (e.g. currencies).
    unit_class: str | None
    unit: str
    value_fn: Callable[[DailyUsageRead], float]


# The first spec of each energy is the one used to find the last imported day.
# Gas statistic ids predate electricity support: keep them unchanged so existing
# installs keep their history.
ENERGY_STATISTICS: dict[str, tuple[StatisticSpec, ...]] = {
    ENERGY_GAS: (
        StatisticSpec(
            f"{DOMAIN}:energy_consumption",
            "Gaz de Bordeaux consumption",
            "energy",
            UnitOfEnergy.KILO_WATT_HOUR,
            lambda read: read.amountOfEnergy,
        ),
        StatisticSpec(
            f"{DOMAIN}:energy_cost",
            "Gaz de Bordeaux cost",
            None,
            CURRENCY_EURO,
            lambda read: read.price,
        ),
        StatisticSpec(
            f"{DOMAIN}:volume",
            "Gaz de Bordeaux volume",
            "volume",
            UnitOfVolume.CUBIC_METERS,
            lambda read: read.volumeOfEnergy,
        ),
    ),
    ENERGY_ELECTRICITY: (
        StatisticSpec(
            f"{DOMAIN}:electricity_consumption",
            "Gaz de Bordeaux electricity consumption",
            "energy",
            UnitOfEnergy.KILO_WATT_HOUR,
            lambda read: read.amountOfEnergy,
        ),
        StatisticSpec(
            f"{DOMAIN}:electricity_cost",
            "Gaz de Bordeaux electricity cost",
            None,
            CURRENCY_EURO,
            lambda read: read.price,
        ),
    ),
}


class GdbCoordinator(DataUpdateCoordinator[dict[str, TotalUsageRead]]):
    """Handle fetching GazdeBordeaux data, updating sensors and inserting statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
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

        # Initialisation de la date de dernière actualisation
        self.last_update: datetime | None = None

        entry_data = entry.data

        # House path per configured energy; AUTO is resolved on first refresh.
        self.houses: dict[str, str] = {
            energy: entry_data[key]
            for energy, key in ENERGY_HOUSE_KEYS.items()
            if entry_data.get(key) and entry_data[key] != NONE
        }

        self.api = Gazdebordeaux(
            aiohttp_client.async_get_clientsession(hass),
            entry_data[CONF_USERNAME],
            entry_data[CONF_PASSWORD],
        )
        self.reset = False
        if RESET_STATISTICS in entry_data:
            self.reset = bool(entry_data[RESET_STATISTICS])
            if self.reset:
                _LOGGER.debug("Asked to reset all statistics...")
                _LOGGER.debug("Updating config...")
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry_data, RESET_STATISTICS: False}
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
    ) -> dict[str, TotalUsageRead]:
        """Fetch data from API endpoint."""
        try:
            # Login expires after a few minutes.
            # Given the infrequent updating (every 12h)
            # assume previous session has expired and re-login.
            await self.api.async_login()
        except Exception as err:
            raise ConfigEntryAuthFailed from err

        data: dict[str, TotalUsageRead] = {}
        for energy in self.houses:
            if self.houses[energy] == AUTO:
                self.houses[energy] = await self.api.async_find_house(energy)
            house = self.houses[energy]

            data[energy] = await self.api.async_get_total_usage(house)

            # Because Opower provides historical usage/cost with a delay of a couple of days
            # we need to insert data into statistics.
            await self._insert_statistics(energy, house)

        # Mise à jour de la date de dernière actualisation
        self.last_update = datetime.now()
        _LOGGER.debug("Last update: %s", self.last_update.strftime("%Y-%m-%d %H:%M:%S"))

        return data

    async def _insert_statistics(self, energy: str, house: str) -> None:
        """Insert gdb statistics for one energy type."""
        specs = ENERGY_STATISTICS[energy]
        statistic_ids = {spec.statistic_id for spec in specs}
        anchor_statistic_id = specs[0].statistic_id
        _LOGGER.debug("Updating Statistics for %s", ", ".join(sorted(statistic_ids)))

        if self.reset:
            _LOGGER.debug("Resetting all statistics...")

        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, anchor_statistic_id, True, set()
        )
        sums: dict[str, float]
        if not last_stat:
            _LOGGER.debug("Updating statistic for the first time")
            usage_reads = await self._async_get_all_data(house)
            if not _has_usage(usage_reads):
                await self._insert_monthly_statistics(specs, house, None)
                return
            sums = {statistic_id: 0.0 for statistic_id in statistic_ids}
            last_stat_ts = None
        else:
            last_stat_ts = last_stat[anchor_statistic_id][0]["start"]  # type: ignore
            last_stat_date = datetime.fromtimestamp(last_stat_ts)
            _LOGGER.debug("Last stat found for %s...", last_stat_date.strftime("%Y-%m-%d"))
            usage_reads = await self._async_get_recent_usage_reads(house, last_stat_ts)
            # Zero recent days can be genuine (e.g. no gas used): only fall back
            # to monthly data when the whole daily history is empty.
            if not _has_usage(usage_reads) and not _has_usage(
                await self._async_get_all_data(house)
            ):
                await self._insert_monthly_statistics(specs, house, last_stat_ts)
                return
            if not usage_reads:
                _LOGGER.debug("No recent usage/cost data. Skipping update")
                return

            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                usage_reads[0].date,
                None,
                statistic_ids,
                "day",
                None,
                {"state", "sum"},
            )
            sums = {
                statistic_id: cast(float, stats[statistic_id][0]["sum"])  # type: ignore
                for statistic_id in statistic_ids
            }

        statistics: dict[str, list[StatisticData]] = {
            statistic_id: [] for statistic_id in statistic_ids
        }

        for usage_read in usage_reads:
            start = usage_read.date
            if last_stat_ts is not None:
                if start.timestamp() <= last_stat_ts:
                    _LOGGER.debug("Skipping data for %s (timestamp)", start.strftime("%Y-%m-%d"))
                    continue
                # Same day, skip regardless of time (avoid multiple runs for the same day).
                if start.date() == last_stat_date.date():
                    _LOGGER.debug("Skipping data for %s (same date)", start.strftime("%Y-%m-%d"))
                    continue

            _LOGGER.debug("Importing data for %s...", start.strftime("%Y-%m-%d"))

            for spec in specs:
                value = spec.value_fn(usage_read)
                sums[spec.statistic_id] += value
                statistics[spec.statistic_id].append(
                    StatisticData(start=start, state=value, sum=sums[spec.statistic_id])
                )

        self._add_statistics(specs, statistics)

    async def _insert_monthly_statistics(
        self, specs: tuple[StatisticSpec, ...], house: str, last_stat_ts: float | None
    ) -> None:
        """Insert one statistic per month, for contracts without daily data.

        Months are rewritten from the one before the last imported month, so the
        current (partial) month and late corrections are picked up on each refresh.
        """
        statistic_ids = {spec.statistic_id for spec in specs}
        now = datetime.now()
        if last_stat_ts is None:
            start = datetime(now.year - 1, 1, 1)
            sums = {statistic_id: 0.0 for statistic_id in statistic_ids}
        else:
            last_stat_date = datetime.fromtimestamp(last_stat_ts, paris_tz)
            last_month = datetime(last_stat_date.year, last_stat_date.month, 1)
            start = (last_month - timedelta(days=1)).replace(day=1)
            sums = await self._async_get_sums_before(statistic_ids, start.replace(tzinfo=paris_tz))
        _LOGGER.debug("No daily data, importing monthly data since %s", start.strftime("%Y-%m"))

        usage_reads = await self.api.async_get_monthly_usage(house, start, now)

        statistics: dict[str, list[StatisticData]] = {
            statistic_id: [] for statistic_id in statistic_ids
        }
        for usage_read in usage_reads:
            if usage_read.date.replace(tzinfo=None) < start:
                continue
            _LOGGER.debug("Importing data for %s...", usage_read.date.strftime("%Y-%m"))
            for spec in specs:
                value = spec.value_fn(usage_read)
                sums[spec.statistic_id] += value
                statistics[spec.statistic_id].append(
                    StatisticData(start=usage_read.date, state=value, sum=sums[spec.statistic_id])
                )

        self._add_statistics(specs, statistics)

    async def _async_get_sums_before(
        self, statistic_ids: set[str], before: datetime
    ) -> dict[str, float]:
        """Return each statistic's running sum just before `before` (0 if none)."""
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            before - timedelta(days=62),
            before,
            statistic_ids,
            "hour",
            None,
            {"sum"},
        )
        return {
            statistic_id: float(stats[statistic_id][-1]["sum"] or 0.0)  # type: ignore
            if stats.get(statistic_id)
            else 0.0
            for statistic_id in statistic_ids
        }

    def _add_statistics(
        self,
        specs: tuple[StatisticSpec, ...],
        statistics: dict[str, list[StatisticData]],
    ) -> None:
        for spec in specs:
            metadata = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                unit_class=spec.unit_class,
                has_sum=True,
                name=spec.name,
                source=DOMAIN,
                statistic_id=spec.statistic_id,
                unit_of_measurement=spec.unit,
            )
            async_add_external_statistics(self.hass, metadata, statistics[spec.statistic_id])

    async def _async_get_all_data(self, house: str) -> list[DailyUsageRead]:
        """Get all cost reads since account activation, at different resolutions by age.

        - month resolution for all years (since account activation)
        - day resolution for past 3 years (if account's read resolution supports it)
        - hour resolution for past 2 months (if account's read resolution supports it)
        """
        usage_reads = []

        # if start=None it will only default to beginning of current year, let's import 1 year more
        start = datetime(datetime.today().year - 1, 1, 1)
        end = datetime.now()
        usage_reads = await self.api.async_get_daily_usage(house, start, end)
        return usage_reads

    async def _async_get_recent_usage_reads(
        self, house: str, last_stat_time: float
    ) -> list[DailyUsageRead]:
        """Get cost reads within the past 30 days to allow corrections in data from utilities."""
        return await self.api.async_get_daily_usage(
            house,
            # datetime.fromtimestamp(last_stat_time) - timedelta(days=30),
            datetime.fromtimestamp(last_stat_time),
            datetime.now(),
        )

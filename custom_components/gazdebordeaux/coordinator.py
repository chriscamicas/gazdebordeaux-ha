"""Coordinator to handle Opower connections."""

import dataclasses
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


def _month_of(read: DailyUsageRead) -> tuple[int, int]:
    return (read.date.year, read.date.month)


def _subtract(total: DailyUsageRead, parts: list[DailyUsageRead]) -> DailyUsageRead:
    """`total` minus the sum of `parts`, keeping `total`'s date."""
    return dataclasses.replace(
        total,
        amountOfEnergy=total.amountOfEnergy - sum(p.amountOfEnergy for p in parts),
        volumeOfEnergy=total.volumeOfEnergy - sum(p.volumeOfEnergy for p in parts),
        price=total.price - sum(p.price for p in parts),
    )


def _is_monthly_series(last_rows: list) -> bool:
    """Whether the two newest points (newest first) are a month apart, on the 1st.

    Monthly points are dated like the API parser dates them (midnight on the 1st,
    see `DailyUsageRead.date`); daily series are one day apart.
    """
    if len(last_rows) < 2:
        return False
    newest, previous = (float(row["start"]) for row in last_rows[:2])
    return newest - previous >= 27 * 86400 and all(
        _is_first_of_month(ts) for ts in (newest, previous)
    )


def _is_first_of_month(ts: float) -> bool:
    # pytz's replace() puts midnight at 23:51 CET the day before: shift by an hour
    # to land on the right day whatever the offset, then compare exactly.
    day = datetime.fromtimestamp(ts, paris_tz) + timedelta(hours=1)
    return day.day == 1 and ts in (
        datetime(day.year, day.month, 1).replace(tzinfo=paris_tz).timestamp(),
        paris_tz.localize(datetime(day.year, day.month, 1)).timestamp(),
    )


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

        last_stats = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 2, anchor_statistic_id, True, set()
        )
        # Newest first.
        last_rows = last_stats.get(anchor_statistic_id, [])

        if last_rows and _is_monthly_series(last_rows):
            all_reads = await self._async_get_all_data(house)
            if not _has_usage(all_reads):
                await self._insert_monthly_statistics(
                    specs, house, cast(float, last_rows[0]["start"])
                )
                return
            # The contract started returning daily data (e.g. the customer turned
            # on daily reporting). Continuing from the last monthly point would
            # count its month twice: rebuild the series from scratch instead.
            _LOGGER.info(
                "Daily data is now available for %s, rebuilding its statistics",
                anchor_statistic_id,
            )
            get_instance(self.hass).async_clear_statistics(sorted(statistic_ids))
            await self._insert_full_history(specs, house, all_reads)
            return

        if not last_rows:
            _LOGGER.debug("Updating statistic for the first time")
            await self._insert_full_history(specs, house, await self._async_get_all_data(house))
            return

        last_stat_ts = cast(float, last_rows[0]["start"])
        last_stat_date = datetime.fromtimestamp(last_stat_ts)
        _LOGGER.debug("Last stat found for %s...", last_stat_date.strftime("%Y-%m-%d"))
        usage_reads = await self._async_get_recent_usage_reads(house, last_stat_ts)
        # Zero recent days can be genuine (e.g. no gas used): only fall back
        # to monthly data when the whole daily history is empty.
        if not _has_usage(usage_reads) and not _has_usage(await self._async_get_all_data(house)):
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

        new_reads = []
        for usage_read in usage_reads:
            start = usage_read.date
            if start.timestamp() <= last_stat_ts:
                _LOGGER.debug("Skipping data for %s (timestamp)", start.strftime("%Y-%m-%d"))
                continue
            # Same day, skip regardless of time (avoid multiple runs for the same day).
            if start.date() == last_stat_date.date():
                _LOGGER.debug("Skipping data for %s (same date)", start.strftime("%Y-%m-%d"))
                continue
            new_reads.append(usage_read)

        self._import_reads(specs, new_reads, sums)

    async def _insert_full_history(
        self,
        specs: tuple[StatisticSpec, ...],
        house: str,
        daily_reads: list[DailyUsageRead],
    ) -> None:
        """Import the whole history into empty statistics.

        Daily reads are used wherever they exist. A contract can have daily data
        only from some point on (e.g. electricity once daily reporting is turned
        on), with monthly-only figures before that. The months before the first
        day with data then get one monthly point each, and that day's month gets
        a point on the 1st for the part of the monthly figure the days don't
        cover, so the totals match the website's.
        """
        if not _has_usage(daily_reads):
            await self._insert_monthly_statistics(specs, house, None)
            return

        statistic_ids = {spec.statistic_id for spec in specs}
        sums = {statistic_id: 0.0 for statistic_id in statistic_ids}
        first_day = next(read for read in daily_reads if _has_usage([read])).date
        first_month = (first_day.year, first_day.month)
        start = min(read.date for read in daily_reads).replace(tzinfo=None)
        if (start.year, start.month) >= first_month:
            self._import_reads(specs, daily_reads, sums)
            return

        monthly_reads = await self.api.async_get_monthly_usage(house, start, datetime.now())
        before = [read for read in monthly_reads if _month_of(read) < first_month]
        if not _has_usage(before):
            # Plain daily contract whose history starts later (e.g. a new contract).
            self._import_reads(specs, daily_reads, sums)
            return

        _LOGGER.debug(
            "Daily data starts on %s, importing monthly data before it",
            first_day.strftime("%Y-%m-%d"),
        )
        daily_from = [read for read in daily_reads if _month_of(read) >= first_month]
        transition = next((read for read in monthly_reads if _month_of(read) == first_month), None)
        if transition is not None:
            # The point on the 1st carries the monthly figure minus the other days
            # of the month, which replaces the daily read of the 1st if any.
            others = [
                read
                for read in daily_from
                if _month_of(read) == first_month and read.date != transition.date
            ]
            daily_from = [read for read in daily_from if read.date != transition.date]
            daily_from.insert(0, _subtract(transition, others))
        self._import_reads(specs, before + daily_from, sums)

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
        self._import_reads(
            specs,
            [read for read in usage_reads if read.date.replace(tzinfo=None) >= start],
            sums,
        )

    def _import_reads(
        self,
        specs: tuple[StatisticSpec, ...],
        usage_reads: list[DailyUsageRead],
        sums: dict[str, float],
    ) -> None:
        """Add `usage_reads` (in date order) on top of the running `sums`."""
        statistics: dict[str, list[StatisticData]] = {spec.statistic_id: [] for spec in specs}
        for usage_read in usage_reads:
            _LOGGER.debug("Importing data for %s...", usage_read.date.strftime("%Y-%m-%d"))
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

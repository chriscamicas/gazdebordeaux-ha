"""Coordinator to handle Opower connections."""

import asyncio
import dataclasses
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
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
from .gazdebordeaux import (
    DailyUsageRead,
    Gazdebordeaux,
    HouseDetails,
    TotalUsageRead,
    paris_tz,
)

_LOGGER = logging.getLogger(__name__)

# Life keeps the load curve for two years
HOURLY_MAX_DAYS = 730
# Pause between two hourly requests, to stay gentle with Life's edge filter
HOURLY_REQUEST_DELAY = 0.2
# Hourly data of the last days may not be published yet: wait for it
HOURLY_GRACE_DAYS = 3
# A month's daily reads may miss its monthly figure by rounding (kWh or EUR)
MONTH_ROUNDING = 0.05
# Life publishes each day's reads two days later, during the day
PUBLICATION_DELAY = timedelta(days=2)
# Usual pace, and the one while the day Life should publish next is missing
UPDATE_INTERVAL = timedelta(hours=12)
WAITING_UPDATE_INTERVAL = timedelta(hours=1)

# Offset of Paris mean time, which pytz's replace() picks: statistics stored
# before 1.3.0 are dated with it, at hh:51 in Paris time
_LMT_OFFSET = cast(timedelta, datetime(2000, 1, 1).replace(tzinfo=paris_tz).utcoffset())


def _is_whole_hour(ts: float) -> bool:
    return datetime.fromtimestamp(ts, paris_tz).minute == 0


def _whole_hour_start(ts: float) -> datetime:
    """The start of a point on the whole Paris hour, if it is dated in Paris mean time."""
    if _is_whole_hour(ts):
        return datetime.fromtimestamp(ts, paris_tz)
    wall_clock = datetime.fromtimestamp(ts, UTC).replace(tzinfo=None) + _LMT_OFFSET
    return paris_tz.localize(wall_clock)


def _first_hourly_day(hourly_start: date | None) -> date:
    """First day to import hour by hour: the load curve's, within what Life keeps.

    Without a load curve (gas, or electricity without half-hourly readings),
    that is never: everything stays daily.
    """
    today = datetime.now(paris_tz).date()
    if hourly_start is None:
        return today + timedelta(days=1)
    return max(hourly_start, today - timedelta(days=HOURLY_MAX_DAYS))


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
        consumptionHp=total.consumptionHp - sum(p.consumptionHp for p in parts),
        consumptionHc=total.consumptionHc - sum(p.consumptionHc for p in parts),
        priceHp=total.priceHp - sum(p.priceHp for p in parts),
        priceHc=total.priceHc - sum(p.priceHc for p in parts),
        contractPrice=total.contractPrice - sum(p.contractPrice for p in parts),
    )


# Fields of a read that a month's remainder adds to
_REMAINDER_FIELDS = (
    "amountOfEnergy",
    "volumeOfEnergy",
    "price",
    "consumptionHp",
    "consumptionHc",
    "priceHp",
    "priceHc",
    "contractPrice",
)


def _add_month_remainders(
    reads: list[DailyUsageRead], remainders: dict[tuple[int, int], DailyUsageRead]
) -> None:
    """Add each month's remainder to its first read; only what lacks, field by field."""
    pending = dict(remainders)
    for read in reads:
        remainder = pending.pop(_month_of(read), None)
        if remainder is None:
            continue
        for field in _REMAINDER_FIELDS:
            setattr(read, field, getattr(read, field) + max(getattr(remainder, field), 0.0))


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
    # Only written for contracts with off-peak ranges
    peak_offpeak: bool = False


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
        StatisticSpec(
            f"{DOMAIN}:electricity_consumption_hp",
            "Gaz de Bordeaux electricity peak consumption",
            "energy",
            UnitOfEnergy.KILO_WATT_HOUR,
            lambda read: read.consumptionHp,
            peak_offpeak=True,
        ),
        StatisticSpec(
            f"{DOMAIN}:electricity_consumption_hc",
            "Gaz de Bordeaux electricity off-peak consumption",
            "energy",
            UnitOfEnergy.KILO_WATT_HOUR,
            lambda read: read.consumptionHc,
            peak_offpeak=True,
        ),
        # Without the subscription, which stays in electricity_cost only
        StatisticSpec(
            f"{DOMAIN}:electricity_cost_hp",
            "Gaz de Bordeaux electricity peak cost",
            None,
            CURRENCY_EURO,
            lambda read: read.priceHp,
            peak_offpeak=True,
        ),
        StatisticSpec(
            f"{DOMAIN}:electricity_cost_hc",
            "Gaz de Bordeaux electricity off-peak cost",
            None,
            CURRENCY_EURO,
            lambda read: read.priceHc,
            peak_offpeak=True,
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
            config_entry=entry,
            name="gazdebordeaux",
            # Every 12 hours, or every hour while the next day is due:
            # see _next_update_interval
            update_interval=UPDATE_INTERVAL,
        )

        # Initialisation de la date de dernière actualisation
        self.last_update: datetime | None = None
        # Statistics import running in the background, see _async_update_data
        self._statistics_task: asyncio.Task | None = None
        self._entry = entry
        # Day of the last point of each energy's statistics, by the first
        # statistic id of the energy, once known
        self._last_days: dict[str, date] = {}

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
        # Energies whose statistics the "reset statistics" option asked to
        # import again; each one leaves the set once that is done
        self._reset_pending: set[str] = set()
        if entry_data.get(RESET_STATISTICS):
            _LOGGER.debug("Asked to reset all statistics...")
            self._reset_pending = set(self.houses)
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
        # Applies to the next update, scheduled once this one returns
        self.update_interval = self._next_update_interval()
        try:
            # Login expires after a few minutes.
            # Given the infrequent updating (hourly at most)
            # assume previous session has expired and re-login.
            await self.api.async_login()
        except Exception as err:
            raise ConfigEntryAuthFailed from err

        data: dict[str, TotalUsageRead] = {}
        for energy in self.houses:
            if self.houses[energy] == AUTO:
                self.houses[energy] = await self.api.async_find_house(energy)
            data[energy] = await self.api.async_get_total_usage(self.houses[energy])

        # Because Opower provides historical usage/cost with a delay of a couple of days
        # we need to insert data into statistics. That runs in the background,
        # so that a slow or failing import (a rebuild, Life's edge filter)
        # holds neither the config entry setup, which Home Assistant cancels
        # after a while, nor the sensors. Unloading the entry, as a reload
        # does, cancels it: the next coordinator starts its own.
        if self._statistics_task is None or self._statistics_task.done():
            self._statistics_task = self._entry.async_create_background_task(
                self.hass,
                self._async_insert_statistics_logged(dict(self.houses)),
                "gazdebordeaux statistics import",
            )

        # Mise à jour de la date de dernière actualisation
        self.last_update = datetime.now()
        _LOGGER.debug("Last update: %s", self.last_update.strftime("%Y-%m-%d %H:%M:%S"))

        return data

    async def _async_insert_statistics_logged(self, houses: dict[str, str]) -> None:
        """Insert the statistics of each energy, logging failures; the next update retries."""
        for energy, house in houses.items():
            try:
                await self._insert_statistics(energy, house)
            except Exception:
                _LOGGER.exception("Importing the Gaz de Bordeaux %s statistics failed", energy)

    async def _insert_statistics(self, energy: str, house: str) -> None:
        """Insert gdb statistics for one energy type."""
        all_specs = ENERGY_STATISTICS[energy]
        anchor_statistic_id = all_specs[0].statistic_id
        _LOGGER.debug("Updating Statistics for %s", anchor_statistic_id)

        last_stats = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 2, anchor_statistic_id, True, set()
        )
        # Newest first.
        last_rows = last_stats.get(anchor_statistic_id, [])
        if last_rows:
            last_start = datetime.fromtimestamp(float(last_rows[0]["start"]), paris_tz)
            self._last_days[anchor_statistic_id] = last_start.date()
        details = await self._async_get_house_details(house)
        specs = tuple(s for s in all_specs if details.offpeak is not None or not s.peak_offpeak)
        has_peak_offpeak = await self._async_has_peak_offpeak(all_specs)

        if rebuild := self._rebuild_reason(energy, bool(last_rows), details, has_peak_offpeak):
            since = await self._async_get_first_day(anchor_statistic_id)
            await self._insert_full_history(
                specs,
                house,
                await self._async_get_all_data(house, since),
                details,
                rebuild=rebuild,
                cleared=all_specs,
            )
            self._reset_pending.discard(energy)
            return

        if last_rows and details.offpeak is None and has_peak_offpeak:
            # Writing the other series alone would leave a hole in the peak
            # and off-peak ones for good: wait for the ranges to come back
            _LOGGER.warning(
                "No off-peak ranges for %s; skipping the update to keep the peak and "
                "off-peak statistics whole (reset the statistics if the contract changed)",
                house,
            )
            return

        if last_rows and not _is_whole_hour(float(last_rows[0]["start"])):
            # The next update goes on from the moved points
            await self._move_to_whole_hours(all_specs)
            return

        if last_rows and _is_monthly_series(last_rows):
            since = await self._async_get_first_day(anchor_statistic_id)
            all_reads = await self._async_get_all_data(house, since)
            if not _has_usage(all_reads):
                await self._insert_monthly_statistics(
                    specs, house, cast(float, last_rows[0]["start"])
                )
                return
            # The contract started returning daily data (e.g. the customer turned
            # on daily reporting). Continuing from the last monthly point would
            # count its month twice: rebuild the series from scratch instead.
            await self._insert_full_history(
                specs,
                house,
                all_reads,
                details,
                rebuild="now that daily data is available",
                cleared=all_specs,
            )
            return

        if not last_rows:
            _LOGGER.debug("Updating statistic for the first time")
            await self._insert_full_history(
                specs, house, await self._async_get_all_data(house), details
            )
            return

        await self._insert_recent_reads(specs, house, cast(float, last_rows[0]["start"]), details)

    def _rebuild_reason(
        self, energy: str, has_rows: bool, details: HouseDetails, has_peak_offpeak: bool
    ) -> str | None:
        """Why the statistics of `energy` must be imported again, if they must."""
        if energy in self._reset_pending:
            # The "reset statistics" option
            return "on request"
        if (
            has_rows
            and details.offpeak is not None
            and not has_peak_offpeak
            and any(spec.peak_offpeak for spec in ENERGY_STATISTICS[energy])
        ):
            return "with peak and off-peak series"
        return None

    async def _insert_recent_reads(
        self,
        specs: tuple[StatisticSpec, ...],
        house: str,
        last_stat_ts: float,
        details: HouseDetails,
    ) -> None:
        """Add the reads after the last imported point, on top of its sums."""
        statistic_ids = {spec.statistic_id for spec in specs}
        # Paris days whatever the time zone of Home Assistant's process
        last_stat_date = datetime.fromtimestamp(last_stat_ts, paris_tz)
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

        sums = await self._async_get_last_sums(statistic_ids)

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

        self._import_reads(specs, await self._with_hourly_reads(house, new_reads, details), sums)

    async def _insert_full_history(
        self,
        specs: tuple[StatisticSpec, ...],
        house: str,
        daily_reads: list[DailyUsageRead],
        details: HouseDetails,
        *,
        rebuild: str | None = None,
        cleared: tuple[StatisticSpec, ...] = (),
    ) -> None:
        """Import the whole history into empty statistics, or in place of them.

        Days covered by the load curve are imported hour by hour, and each
        past month is brought up to Life's monthly figure. When rebuilding,
        the existing statistics, and those of `cleared` that the contract no
        longer feeds, are only cleared once all the new data is downloaded:
        a failure on the way leaves them untouched.
        """
        reads = await self._full_history_reads(house, daily_reads)
        remainders = await self._async_get_month_remainders(house, reads)
        reads = await self._with_hourly_reads(house, reads, details)
        _add_month_remainders(reads, remainders)
        statistic_ids = sorted({spec.statistic_id for spec in specs})
        if rebuild:
            if not _has_usage(reads):
                # Treated like a failed download: keep what is there
                raise Exception(f"No usage to rebuild {', '.join(statistic_ids)} with")
            cleared_ids = sorted({spec.statistic_id for spec in (*specs, *cleared)})
            _LOGGER.info("Rebuilding %s %s", ", ".join(cleared_ids), rebuild)
            # The recorder runs the clear before the imports queued after it
            get_instance(self.hass).async_clear_statistics(cleared_ids)
        self._import_reads(specs, reads, dict.fromkeys(statistic_ids, 0.0))

    async def _full_history_reads(
        self, house: str, daily_reads: list[DailyUsageRead]
    ) -> list[DailyUsageRead]:
        """The reads that make up the whole history, oldest first.

        Daily reads are used wherever they exist. A contract can have daily data
        only from some point on (e.g. electricity once daily reporting is turned
        on), with monthly-only figures before that. The months before the first
        day with data then get one monthly point each, and that day's month gets
        a point on the 1st for the part of the monthly figure the days don't
        cover, so the totals match the website's.
        """
        if not _has_usage(daily_reads):
            # From January 1st of last year, or earlier on a rebuild of an
            # older history
            start = datetime(datetime.now().year - 1, 1, 1)
            if daily_reads:
                start = min(start, min(read.date for read in daily_reads).replace(tzinfo=None))
            _LOGGER.debug("No daily data, importing monthly data since %s", start.strftime("%Y-%m"))
            usage_reads = await self.api.async_get_monthly_usage(house, start, datetime.now())
            return [read for read in usage_reads if read.date.replace(tzinfo=None) >= start]

        first_day = next(read for read in daily_reads if _has_usage([read])).date
        first_month = (first_day.year, first_day.month)
        start = min(read.date for read in daily_reads).replace(tzinfo=None)
        if (start.year, start.month) >= first_month:
            return daily_reads

        monthly_reads = await self.api.async_get_monthly_usage(house, start, datetime.now())
        before = [read for read in monthly_reads if _month_of(read) < first_month]
        if not _has_usage(before):
            # Plain daily contract whose history starts later (e.g. a new contract).
            return daily_reads

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
        return before + daily_from

    async def _async_get_month_remainders(
        self, house: str, reads: list[DailyUsageRead]
    ) -> dict[tuple[int, int], DailyUsageRead]:
        """What each past month's daily reads lack to reach Life's monthly figure.

        Some days leave out what Life counts in its months: on the tested
        account, the first days of the contract came at 0 EUR, without the
        subscription their month counted. Only months the daily reads cover
        to the end are checked, since the month of the last read can still
        get days, and only a lack beyond rounding counts.
        """
        if not reads:
            return {}
        last_month = _month_of(reads[-1])
        months = {_month_of(r) for r in reads if not r.monthly and _month_of(r) < last_month}
        if not months:
            return {}
        start = min(r.date for r in reads if _month_of(r) in months)
        monthly_reads = await self.api.async_get_monthly_usage(
            house, datetime(start.year, start.month, 1), datetime.now()
        )
        remainders = {}
        for monthly in monthly_reads:
            month = _month_of(monthly)
            if month not in months:
                continue
            remainder = _subtract(monthly, [r for r in reads if _month_of(r) == month])
            if remainder.amountOfEnergy > MONTH_ROUNDING or remainder.price > MONTH_ROUNDING:
                _LOGGER.info(
                    "Reads of %d-%02d lack %.3f kWh and %.2f EUR of Life's monthly figure: "
                    "adding them to the month's first read",
                    *month,
                    remainder.amountOfEnergy,
                    remainder.price,
                )
                remainders[month] = remainder
        return remainders

    async def _with_hourly_reads(
        self, house: str, reads: list[DailyUsageRead], details: HouseDetails
    ) -> list[DailyUsageRead]:
        """Replace the daily reads the load curve covers by hourly reads.

        Monthly points stay as they are. A day whose hours do not add up to
        its daily read keeps the daily read. A recent day without hours yet
        stops the import there, so the next update picks it up hour by hour
        once Enedis has published it.
        """
        hourly_from = _first_hourly_day(details.hourly_start)
        offpeak = details.offpeak
        result: list[DailyUsageRead] = []
        recent = datetime.now(paris_tz).date() - timedelta(days=HOURLY_GRACE_DAYS)
        requested = False
        for daily in reads:
            day = daily.date.date()
            if daily.monthly or day < hourly_from:
                result.append(daily)
                continue
            if requested:
                await asyncio.sleep(HOURLY_REQUEST_DELAY)
            requested = True
            hourly = await self.api.async_get_hourly_usage(
                house, day, daily.amountOfEnergy, offpeak if daily.isHphc else None
            )
            if not hourly:
                if day >= recent:
                    _LOGGER.debug("No hourly data yet for %s, waiting for it", day)
                    break
                _LOGGER.debug("No hourly data for %s, keeping the daily read", day)
                result.append(daily)
                continue
            energy = sum(r.amountOfEnergy for r in hourly)
            if abs(energy - daily.amountOfEnergy) > 0.1 * daily.amountOfEnergy + 0.05:
                _LOGGER.warning(
                    "Hourly reads of %s add up to %.3f kWh, daily read says %.3f: "
                    "keeping the daily read",
                    day,
                    energy,
                    daily.amountOfEnergy,
                )
                result.append(daily)
                continue
            if daily.isHphc and offpeak is not None:
                _split_peak_offpeak(day, daily, hourly)
                result.extend(hourly)
                continue
            # The hours carry no cost, nor any gas volume: spread the day's by
            # consumption, or evenly over a day without any (it still costs
            # its subscription)
            shares = [r.amountOfEnergy / energy if energy else 1 / len(hourly) for r in hourly]
            if not any(r.price for r in hourly):
                for r, share in zip(hourly, shares, strict=True):
                    r.price = daily.price * share
            if not any(r.volumeOfEnergy for r in hourly):
                for r, share in zip(hourly, shares, strict=True):
                    r.volumeOfEnergy = daily.volumeOfEnergy * share
            result.extend(hourly)
        return result

    async def _insert_monthly_statistics(
        self, specs: tuple[StatisticSpec, ...], house: str, last_stat_ts: float
    ) -> None:
        """Insert one statistic per month, for contracts without daily data.

        Months are rewritten from the one before the last imported month, so the
        current (partial) month and late corrections are picked up on each refresh.
        """
        statistic_ids = {spec.statistic_id for spec in specs}
        now = datetime.now()
        last_stat_date = datetime.fromtimestamp(last_stat_ts, paris_tz)
        last_month = datetime(last_stat_date.year, last_stat_date.month, 1)
        start = (last_month - timedelta(days=1)).replace(day=1)
        sums = await self._async_get_sums_before(statistic_ids, paris_tz.localize(start))
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
        if usage_reads:
            self._last_days[specs[0].statistic_id] = usage_reads[-1].date.date()

    def _next_update_interval(self) -> timedelta:
        """Poll hourly while the day Life should publish next is missing.

        Life publishes each day's reads two days later, during the day. When
        more than that day is missing, Life itself is late: keep the usual
        pace rather than ask every hour for days.
        """
        expected = datetime.now(paris_tz).date() - PUBLICATION_DELAY
        for energy in self.houses:
            last_day = self._last_days.get(ENERGY_STATISTICS[energy][0].statistic_id)
            # Not known before the first import: look again soon
            if last_day is None or last_day == expected - timedelta(days=1):
                return WAITING_UPDATE_INTERVAL
        return UPDATE_INTERVAL

    async def _move_to_whole_hours(self, specs: tuple[StatisticSpec, ...]) -> None:
        """Move the points dated in Paris mean time (before 1.3.0) to whole hours.

        Only their start changes. Importing them again from Life instead would
        lose the days before the ones it returns.
        """
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            datetime(2000, 1, 1, tzinfo=UTC),
            None,
            {spec.statistic_id for spec in specs},
            "hour",
            None,
            {"state", "sum"},
        )
        moved = {
            statistic_id: [
                StatisticData(
                    start=_whole_hour_start(row["start"]),
                    state=float(row["state"] or 0),
                    sum=float(row["sum"] or 0),
                )
                for row in rows
            ]
            for statistic_id, rows in stats.items()
        }
        _LOGGER.info("Moving %s to whole hours", ", ".join(sorted(moved)))
        # The recorder runs the clear before the imports queued after it
        get_instance(self.hass).async_clear_statistics(sorted(moved))
        self._add_statistics(tuple(spec for spec in specs if spec.statistic_id in moved), moved)

    async def _async_get_house_details(self, house: str) -> HouseDetails:
        try:
            return await self.api.async_get_house_details(house)
        except Exception:
            # Without it, the import goes on daily, as before this resource was read
            _LOGGER.warning("Could not read the house details", exc_info=True)
            return HouseDetails(hourly_start=None)

    async def _async_has_peak_offpeak(self, specs: tuple[StatisticSpec, ...]) -> bool:
        """Whether the peak and off-peak series among `specs` have points."""
        statistic_id = next((spec.statistic_id for spec in specs if spec.peak_offpeak), None)
        if statistic_id is None:
            return False
        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, set()
        )
        return bool(last.get(statistic_id))

    async def _async_get_last_sums(self, statistic_ids: set[str]) -> dict[str, float]:
        """Each statistic's running sum at its last point (0 if none).

        Read from the point itself: a day period of the recorder follows Home
        Assistant's time zone, which can cut a Paris day of hourly points.
        """
        sums = {}
        for statistic_id in statistic_ids:
            last = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
            )
            rows = last.get(statistic_id)
            sums[statistic_id] = float(rows[0]["sum"] or 0) if rows else 0.0
        return sums

    async def _async_get_first_day(self, statistic_id: str) -> datetime | None:
        """When the oldest point of `statistic_id` starts, if it has any.

        A rebuild imports again from there: the first import's range (since
        January 1st of last year) can start later than the existing history.
        """
        instance = get_instance(self.hass)
        months = await instance.async_add_executor_job(
            statistics_during_period,
            self.hass,
            datetime(2000, 1, 1, tzinfo=UTC),
            None,
            {statistic_id},
            "month",
            None,
            {"sum"},
        )
        if not months.get(statistic_id):
            return None
        # The oldest month, in Home Assistant's time zone, then its oldest point
        month = datetime.fromtimestamp(months[statistic_id][0]["start"], UTC)
        points = await instance.async_add_executor_job(
            statistics_during_period,
            self.hass,
            month,
            month + timedelta(days=32),
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        first = (points.get(statistic_id) or months[statistic_id])[0]["start"]
        return datetime.fromtimestamp(first, paris_tz)

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

    async def _async_get_all_data(
        self, house: str, since: datetime | None = None
    ) -> list[DailyUsageRead]:
        """Get the daily reads since January 1st of last year, or since `since` if older."""
        usage_reads = []

        # if start=None it will only default to beginning of current year, let's import 1 year more
        start = datetime(datetime.today().year - 1, 1, 1)
        if since is not None:
            start = min(start, datetime(since.year, since.month, since.day))
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
            # Paris dates: in a process running in UTC, the last point at
            # Paris midnight falls on the day before
            datetime.fromtimestamp(last_stat_time, paris_tz),
            datetime.now(paris_tz),
        )


def _split_peak_offpeak(day: date, daily: DailyUsageRead, hourly: list[DailyUsageRead]) -> None:
    """Price each hour at its own peak / off-peak rate.

    The hours are already split by the contract's off-peak ranges; that split
    must match the daily one. Each period's daily cost is then spread over
    its own kWh, and the subscription evenly over the hours, so that the day
    adds up exactly to the daily read.
    """
    offpeak_energy = sum(r.consumptionHc for r in hourly)
    if abs(offpeak_energy - daily.consumptionHc) > 0.1 * daily.consumptionHc + 0.1:
        _LOGGER.warning(
            "Off-peak hours of %s add up to %.3f kWh, daily read says %.3f: "
            "splitting each hour by the daily ratio",
            day,
            offpeak_energy,
            daily.consumptionHc,
        )
        share = daily.consumptionHc / daily.amountOfEnergy if daily.amountOfEnergy else 0.0
        for r in hourly:
            r.consumptionHc = r.amountOfEnergy * share
            r.consumptionHp = r.amountOfEnergy - r.consumptionHc
    peak_energy = sum(r.consumptionHp for r in hourly)
    offpeak_energy = sum(r.consumptionHc for r in hourly)
    subscription = daily.contractPrice / len(hourly)
    for r in hourly:
        r.isHphc = True
        r.priceHp = daily.priceHp * r.consumptionHp / peak_energy if peak_energy else 0.0
        r.priceHc = daily.priceHc * r.consumptionHc / offpeak_energy if offpeak_energy else 0.0
        r.contractPrice = subscription
        r.price = r.priceHp + r.priceHc + subscription

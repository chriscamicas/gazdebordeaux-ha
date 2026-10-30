"""Coordinator to handle Opower connections."""

import asyncio
import logging
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Any, cast

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.components.recorder.util import get_instance
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

from .const import DOMAIN, HOUSE, RESET_STATISTICS
from .gazdebordeaux import (
    DailyUsageRead,
    Gazdebordeaux,
    HouseDetails,
    OffPeakTimes,
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
# Life publishes each day's reads two days later, during the day
PUBLICATION_DELAY = timedelta(days=2)
# Usual pace, and the one while the day Life should publish next is missing
UPDATE_INTERVAL = timedelta(hours=12)
WAITING_UPDATE_INTERVAL = timedelta(hours=1)

CONSUMPTION_ID = f"{DOMAIN}:energy_consumption"
# (statistic_id, DailyUsageRead field, name, only for peak / off-peak contracts)
SERIES = [
    (f"{DOMAIN}:energy_cost", "price", "cost", False),
    (CONSUMPTION_ID, "amountOfEnergy", "consumption", False),
    (f"{DOMAIN}:volume", "volumeOfEnergy", "volume", False),
    (f"{DOMAIN}:energy_consumption_hp", "consumptionHp", "peak consumption", True),
    (f"{DOMAIN}:energy_consumption_hc", "consumptionHc", "off-peak consumption", True),
    (f"{DOMAIN}:energy_cost_hp", "priceHp", "peak cost", True),
    (f"{DOMAIN}:energy_cost_hc", "priceHc", "off-peak cost", True),
]
# HA has no monetary unit class: costs go with unit_class None
SERIES_UNITS = {
    "price": (None, CURRENCY_EURO),
    "priceHp": (None, CURRENCY_EURO),
    "priceHc": (None, CURRENCY_EURO),
    "amountOfEnergy": ("energy", UnitOfEnergy.KILO_WATT_HOUR),
    "consumptionHp": ("energy", UnitOfEnergy.KILO_WATT_HOUR),
    "consumptionHc": ("energy", UnitOfEnergy.KILO_WATT_HOUR),
    "volumeOfEnergy": ("volume", UnitOfVolume.CUBIC_METERS),
}


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
            # Every 12 hours, or every hour while the next day is due:
            # see _next_update_interval
            update_interval=UPDATE_INTERVAL,
        )

        # Initialisation de la date de dernière actualisation
        self.last_update: datetime | None = None
        # Statistics import running in the background, see _async_update_data
        self._statistics_task: asyncio.Task | None = None
        # Day of the last imported statistic, known once an import has run
        self._last_day: date | None = None

        house: Any = None
        if HOUSE in entry_data:
            house = entry_data[HOUSE]

        self.api = Gazdebordeaux(
            aiohttp_client.async_get_clientsession(hass),
            entry_data[CONF_USERNAME],
            entry_data[CONF_PASSWORD],
            None,
            house,
        )
        self.reset = False
        if RESET_STATISTICS in entry_data:
            self.reset = bool(entry_data[RESET_STATISTICS])
            if self.reset:
                _LOGGER.debug("Asked to reset all statistics...")
                entries = self.hass.config_entries.async_entries(DOMAIN)

                _LOGGER.debug("Updating config...")
                self.hass.config_entries.async_update_entry(
                    entries[0],
                    data={
                        CONF_USERNAME: entry_data[CONF_USERNAME],
                        CONF_PASSWORD: entry_data[CONF_PASSWORD],
                        RESET_STATISTICS: False,
                        HOUSE: house,
                    },
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
        # Applies to the next update, scheduled once this one returns
        self.update_interval = self._next_update_interval()
        try:
            # Login expires after a few minutes.
            # Given the infrequent updating (hourly at most)
            # assume previous session has expired and re-login.
            await self.api.async_login()
        except Exception as err:
            raise ConfigEntryAuthFailed from err

        total_usage: TotalUsageRead = await self.api.async_get_total_usage()

        # Because Opower provides historical usage/cost with a delay of a couple of days
        # we need to insert data into statistics. A full import asks for one
        # day of load curve per request and can take minutes: run it in the
        # background so that it does not hold the config entry setup (which
        # Home Assistant cancels after a while) nor the sensors.
        if self._statistics_task is None or self._statistics_task.done():
            self._statistics_task = self.hass.async_create_background_task(
                self._async_insert_statistics_logged(), "gazdebordeaux statistics import"
            )

        # Mise à jour de la date de dernière actualisation
        self.last_update = datetime.now()
        _LOGGER.debug("Last update: %s", self.last_update.strftime("%Y-%m-%d %H:%M:%S"))

        return total_usage

    async def _async_insert_statistics_logged(self) -> None:
        """Run _insert_statistics, logging its failure; the next update retries."""
        try:
            await self._insert_statistics()
        except Exception:
            _LOGGER.exception("Importing Gaz de Bordeaux statistics failed")

    async def _insert_statistics(self) -> None:
        """Insert gdb statistics."""
        if self.reset:
            _LOGGER.debug("Resetting all statistics...")

        all_ids = [statistic_id for statistic_id, *_ in SERIES]
        last_stats: dict[str, Any] = {}
        for statistic_id in all_ids:
            last_stats.update(
                await get_instance(self.hass).async_add_executor_job(
                    get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
                )
            )

        try:
            house = await self.api.async_get_house_details()
        except Exception:
            # Without it, the import goes on daily as before this resource was read
            _LOGGER.warning("Could not read the house details", exc_info=True)
            house = HouseDetails(offpeak=None, hourly_start=None)
        offpeak = house.offpeak
        hphc_ids = [statistic_id for statistic_id, _, _, hphc_only in SERIES if hphc_only]
        has_hphc = all(i in last_stats for i in hphc_ids)

        last_stat_ts = None
        rebuild = None
        if CONSUMPTION_ID in last_stats:
            last_stat_ts = last_stats[CONSUMPTION_ID][0]["start"]
            self._last_day = datetime.fromtimestamp(last_stat_ts, paris_tz).date()
            rebuild = self._rebuild_reason(last_stat_ts, offpeak is not None and not has_hphc)
            if rebuild is None and offpeak is None and has_hphc:
                # Writing the other series alone would leave a hole in the
                # peak / off-peak ones for good: wait for the ranges to come back
                _LOGGER.warning(
                    "No off-peak ranges for this house; skipping the update to keep "
                    "the peak and off-peak statistics whole (reset them if the "
                    "contract changed)"
                )
                return
            if rebuild:
                last_stat_ts = None

        if last_stat_ts is None:
            _LOGGER.debug("Updating statistic for the first time")
            daily_reads = await self._async_get_all_data()
            sums = dict.fromkeys(all_ids, 0.0)
            hourly_from = self._first_hourly_day(house.hourly_start)
        else:
            last_stat_date = datetime.fromtimestamp(last_stat_ts, paris_tz)
            _LOGGER.debug("Last stat found for %s...", last_stat_date.strftime("%Y-%m-%d %H:%M"))
            daily_reads = await self._async_get_recent_usage_reads(last_stat_ts)
            sums = {
                statistic_id: cast(float, last_stats[statistic_id][0]["sum"])
                if statistic_id in last_stats
                else 0.0
                for statistic_id in all_ids
            }
            hourly_from = last_stat_date.date()
            if last_stat_date.hour == 0:
                # A daily read sits at midnight: that day is complete
                hourly_from += timedelta(days=1)

        usage_reads = await self._async_with_hourly_reads(
            daily_reads, hourly_from, offpeak, house.hourly_start
        )
        if not usage_reads:
            _LOGGER.debug("No recent usage/cost data. Skipping update")
            return

        if rebuild:
            # Only now that the new data is in hand: a failed download above
            # leaves the existing statistics untouched. The recorder runs the
            # clear before the imports queued below.
            _LOGGER.warning("Rebuilding Gaz de Bordeaux statistics %s", rebuild)
            get_instance(self.hass).async_clear_statistics(all_ids)

        series = [s for s in SERIES if offpeak is not None or not s[3]]
        self._write_statistics(series, usage_reads, sums, last_stat_ts, hourly_from)
        self._last_day = usage_reads[-1].date.date()
        # Cleared once the import it asked for has happened
        self.reset = False

    def _write_statistics(
        self,
        series: list[tuple[str, str, str, bool]],
        usage_reads: list[DailyUsageRead],
        sums: dict[str, float],
        last_stat_ts: float | None,
        hourly_from: date,
    ) -> None:
        """Queue the reads after the last statistic, as running sums per series."""
        statistics: dict[str, list[StatisticData]] = {s[0]: [] for s in series}

        for usage_read in usage_reads:
            start = usage_read.date
            if last_stat_ts is not None and (
                start.timestamp() <= last_stat_ts or start.date() < hourly_from
            ):
                _LOGGER.debug("Skipping data for %s (timestamp)", start.strftime("%Y-%m-%d %H:%M"))
                continue

            _LOGGER.debug("Importing data for %s...", start.strftime("%Y-%m-%d %H:%M"))

            for statistic_id, field, _, _ in series:
                value = getattr(usage_read, field)
                sums[statistic_id] += value
                statistics[statistic_id].append(
                    StatisticData(start=start, state=value, sum=sums[statistic_id])
                )

        for statistic_id, field, name, _ in series:
            unit_class, unit = SERIES_UNITS[field]
            metadata = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                unit_class=unit_class,
                has_sum=True,
                name=f"Gaz de Bordeaux {name}",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_of_measurement=unit,
            )
            async_add_external_statistics(self.hass, metadata, statistics[statistic_id])

    def _rebuild_reason(self, last_stat_ts: float, missing_hphc: bool) -> str | None:
        """Tell why the existing statistics must be cleared and imported again."""
        # Rows written before the time zone fix start at hh:51 (Paris mean
        # time): rebuild the whole series once, on the hour.
        if datetime.fromtimestamp(last_stat_ts, paris_tz).minute != 0:
            return "on whole hours"
        if missing_hphc:
            return "with peak and off-peak series"
        if self.reset:
            # The "reset statistics" option: rebuild everything, with the
            # load curve hour by hour
            return "on request"
        return None

    def _next_update_interval(self) -> timedelta:
        """Poll hourly while the day Life should publish next is missing.

        Life publishes each day's reads two days later, during the day. When
        more than that day is missing, Life itself is late: keep the usual
        pace rather than ask every hour for days.
        """
        if self._last_day is None:
            # Not known before the first import: look again soon
            return WAITING_UPDATE_INTERVAL
        expected = datetime.now(paris_tz).date() - PUBLICATION_DELAY
        if self._last_day == expected - timedelta(days=1):
            return WAITING_UPDATE_INTERVAL
        return UPDATE_INTERVAL

    @staticmethod
    def _first_hourly_day(hourly_start: date | None) -> date:
        """First day to import hour by hour on a full import.

        Without a load curve (gas, or electricity without half-hourly
        readings), that is never: everything stays daily.
        """
        today = datetime.now(paris_tz).date()
        if hourly_start is None:
            return today + timedelta(days=1)
        return max(hourly_start, today - timedelta(days=HOURLY_MAX_DAYS))

    async def _async_get_all_data(self) -> list[DailyUsageRead]:
        """Get the daily reads since January 1st of last year.

        _async_with_hourly_reads then replaces the days covered by the load
        curve with hourly reads.
        """
        usage_reads = []

        # if start=None it will only default to beginning of current year, let's import 1 year more
        start = datetime(datetime.today().year - 1, 1, 1)
        end = datetime.now()
        usage_reads = await self.api.async_get_daily_usage(start, end)
        return usage_reads

    async def _async_with_hourly_reads(
        self,
        daily_reads: list[DailyUsageRead],
        hourly_from: date,
        offpeak: OffPeakTimes | None = None,
        hourly_start: date | None = None,
    ) -> list[DailyUsageRead]:
        """Replace the daily reads from hourly_from on by hourly reads.

        Only days covered by the load curve (from hourly_start on) are asked
        hour by hour; without a curve, every day stays daily.

        A day whose hours do not add up to its daily read keeps the daily
        read. A recent day without hours yet stops the import there, so the
        next update picks it up hour by hour once Enedis has published it.
        """
        reads: list[DailyUsageRead] = []
        recent = datetime.now(paris_tz).date() - timedelta(days=HOURLY_GRACE_DAYS)
        requested = False
        for daily in sorted(daily_reads, key=lambda r: r.date):
            day = daily.date.date()
            if day < hourly_from or hourly_start is None or day < hourly_start:
                reads.append(daily)
                continue
            if requested:
                await asyncio.sleep(HOURLY_REQUEST_DELAY)
            requested = True
            hourly = await self.api.async_get_hourly_usage(
                day, offpeak if daily.isHphc else None, daily.amountOfEnergy
            )
            if not hourly:
                if day >= recent:
                    _LOGGER.debug("No hourly data yet for %s, waiting for it", day)
                    break
                _LOGGER.debug("No hourly data for %s, keeping the daily read", day)
                reads.append(daily)
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
                reads.append(daily)
                continue
            if any(r.price for r in hourly):
                reads.extend(hourly)
                continue
            if daily.isHphc and offpeak is not None:
                _split_peak_offpeak(day, daily, hourly)
            else:
                # Spread the daily cost by consumption when the hours carry none
                for r in hourly:
                    r.price = daily.price * r.amountOfEnergy / energy if energy else 0.0
            reads.extend(hourly)
        return reads

    async def _async_get_recent_usage_reads(self, last_stat_time: float) -> list[DailyUsageRead]:
        """Get the daily reads from the day of the last statistic on."""
        return await self.api.async_get_daily_usage(
            # datetime.fromtimestamp(last_stat_time) - timedelta(days=30),
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

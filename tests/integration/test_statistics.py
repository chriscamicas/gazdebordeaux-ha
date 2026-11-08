"""Tests for the external statistics fed into the Energy dashboard."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
import pytz
from homeassistant.components.recorder.statistics import (
    list_statistic_ids,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.gazdebordeaux.const import (
    CONF_ELECTRICITY_HOUSE,
    CONF_GAS_HOUSE,
    DOMAIN,
    NONE,
    RESET_STATISTICS,
)
from custom_components.gazdebordeaux.coordinator import ENERGY_STATISTICS, GdbCoordinator
from custom_components.gazdebordeaux.gazdebordeaux import DailyUsageRead, TotalUsageRead

ELEC = "/api/houses/elec-uuid"
GAS = "/api/houses/gas-uuid"
API = "custom_components.gazdebordeaux.coordinator.Gazdebordeaux"
PARIS = pytz.timezone("Europe/Paris")


def _read(day: int, kwh: float, price: float) -> DailyUsageRead:
    return DailyUsageRead(
        date=PARIS.localize(datetime(2026, 9, day)),
        amountOfEnergy=kwh,
        volumeOfEnergy=0,
        price=price,
        ratio=0,
        temperature=20,
    )


async def test_electricity_statistics_are_imported_incrementally(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_GAS_HOUSE: NONE,
            CONF_ELECTRICITY_HOUSE: ELEC,
        },
    )
    entry.add_to_hass(hass)
    coordinator = GdbCoordinator(hass, entry)

    daily = AsyncMock(return_value=[_read(1, 10, 2.0), _read(2, 5, 1.0)])
    with patch.object(coordinator.api, "async_get_daily_usage", new=daily):
        await coordinator._insert_statistics("electricity", ELEC)
        await async_wait_recording_done(hass)

        # Second run: day 2 is already imported, day 3 is new.
        daily.return_value = [_read(2, 5, 1.0), _read(3, 7, 1.5)]
        await coordinator._insert_statistics("electricity", ELEC)
        await async_wait_recording_done(hass)

    assert all(c.args[0] == ELEC for c in daily.await_args_list)

    ids = {
        s["statistic_id"]
        for s in await hass.async_add_executor_job(list_statistic_ids, hass, None, None)
    }
    assert f"{DOMAIN}:electricity_consumption" in ids
    assert f"{DOMAIN}:electricity_cost" in ids
    # Gas statistics must not be touched by the electricity import.
    assert f"{DOMAIN}:energy_consumption" not in ids
    assert f"{DOMAIN}:volume" not in ids

    stats = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        PARIS.localize(datetime(2026, 8, 31)),
        None,
        {f"{DOMAIN}:electricity_consumption", f"{DOMAIN}:electricity_cost"},
        "day",
        None,
        {"state", "sum"},
    )
    consumption = stats[f"{DOMAIN}:electricity_consumption"]
    cost = stats[f"{DOMAIN}:electricity_cost"]
    assert [(s["state"], s["sum"]) for s in consumption] == [(10, 10), (5, 15), (7, 22)]
    assert [s["sum"] for s in cost] == [2.0, 3.0, 4.5]


def _month(year: int, month: int, kwh: float, price: float) -> DailyUsageRead:
    # Same tz handling as the API client's parser.
    return DailyUsageRead(
        date=PARIS.localize(datetime(year, month, 1)),
        amountOfEnergy=kwh,
        volumeOfEnergy=0,
        price=price,
        ratio=0,
        temperature=20,
    )


async def test_monthly_fallback_when_no_daily_data(hass: HomeAssistant) -> None:
    """Contracts whose daily data is all zero are imported month by month."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_GAS_HOUSE: NONE,
            CONF_ELECTRICITY_HOUSE: ELEC,
        },
    )
    entry.add_to_hass(hass)
    coordinator = GdbCoordinator(hass, entry)

    daily = AsyncMock(return_value=[_read(1, 0, 0), _read(2, 0, 0)])
    monthly = AsyncMock(
        return_value=[
            _month(2026, 4, 39, 8.0),
            _month(2026, 5, 620, 120.0),
            _month(2026, 6, 700, 140.0),
        ]
    )
    with (
        patch.object(coordinator.api, "async_get_daily_usage", new=daily),
        patch.object(coordinator.api, "async_get_monthly_usage", new=monthly),
    ):
        await coordinator._insert_statistics("electricity", ELEC)
        await async_wait_recording_done(hass)

        # Next refresh: June got corrected and July appeared. Rewriting starts at
        # the month before the last imported one (May), on top of April's sum.
        monthly.return_value = [
            _month(2026, 5, 620, 120.0),
            _month(2026, 6, 749, 142.0),
            _month(2026, 7, 829, 155.0),
        ]
        await coordinator._insert_statistics("electricity", ELEC)
        await async_wait_recording_done(hass)

    second_start = monthly.await_args_list[1].args[1]
    assert (second_start.year, second_start.month, second_start.day) == (2026, 5, 1)

    stats = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 3, 1).replace(tzinfo=PARIS),
        None,
        {f"{DOMAIN}:electricity_consumption"},
        "hour",
        None,
        {"state", "sum"},
    )
    rows = stats[f"{DOMAIN}:electricity_consumption"]
    assert [(r["state"], r["sum"]) for r in rows] == [
        (39, 39),
        (620, 659),
        (749, 1408),
        (829, 2237),
    ]


async def test_zero_recent_days_do_not_switch_gas_to_monthly(hass: HomeAssistant) -> None:
    """A few zero days on a contract with daily history stay on the daily path."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_GAS_HOUSE: ELEC,
            CONF_ELECTRICITY_HOUSE: NONE,
        },
    )
    entry.add_to_hass(hass)
    coordinator = GdbCoordinator(hass, entry)

    daily = AsyncMock(return_value=[_read(1, 10, 2.0), _read(2, 0, 0)])
    monthly = AsyncMock(return_value=[])
    with (
        patch.object(coordinator.api, "async_get_daily_usage", new=daily),
        patch.object(coordinator.api, "async_get_monthly_usage", new=monthly),
    ):
        await coordinator._insert_statistics("gas", ELEC)
        await async_wait_recording_done(hass)

        # Recent days (starting at the last imported one) are all zero, but the
        # full history isn't.
        daily.side_effect = [
            [_read(2, 0, 0), _read(3, 0, 0)],
            [_read(1, 10, 2.0), _read(2, 0, 0), _read(3, 0, 0)],
        ]
        await coordinator._insert_statistics("gas", ELEC)
        await async_wait_recording_done(hass)

    monthly.assert_not_awaited()
    stats = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        PARIS.localize(datetime(2026, 8, 31)),
        None,
        {f"{DOMAIN}:energy_consumption"},
        "day",
        None,
        {"sum"},
    )
    assert [r["sum"] for r in stats[f"{DOMAIN}:energy_consumption"]] == [10, 10, 10]


def _day(month: int, day: int, kwh: float, price: float) -> DailyUsageRead:
    # Same tz handling as the API client's parser, so days and months line up.
    return DailyUsageRead(
        date=PARIS.localize(datetime(2026, month, day)),
        amountOfEnergy=kwh,
        volumeOfEnergy=0,
        price=price,
        ratio=0,
        temperature=20,
    )


def _electricity_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_GAS_HOUSE: NONE,
            CONF_ELECTRICITY_HOUSE: ELEC,
        },
    )
    entry.add_to_hass(hass)
    return entry


# Daily data turned on during October: Aug-Sep only have monthly figures, and
# Oct 1-4 are only in October's monthly figure (61 kWh = 39 + 10 + 12).
DAILY_SINCE_OCTOBER = [
    _day(8, 1, 0, 0),
    _day(9, 1, 0, 0),
    _day(10, 1, 0, 0),
    _day(10, 4, 0, 0),
    _day(10, 5, 10, 2.0),
    _day(10, 6, 12, 2.5),
    _day(11, 1, 9.5, 2.1),
]
MONTHLY_SINCE_AUGUST = [
    _month(2026, 8, 573, 112.6),
    _month(2026, 9, 508, 101.7),
    _month(2026, 10, 61, 13.0),
    _month(2026, 11, 9.5, 2.1),
]
# (state, sum) for consumption, then the cost sums.
EXPECTED_HYBRID = [
    (573, 573),
    (508, 1081),
    (39, 1120),  # Oct 1: October's monthly figure minus the daily reads
    (0, 1120),
    (10, 1130),
    (12, 1142),
    (9.5, 1151.5),
]
EXPECTED_HYBRID_COST = [112.6, 214.3, 222.8, 222.8, 224.8, 227.3, 229.4]


async def _hourly_rows(
    hass: HomeAssistant, statistic_id: str, since: datetime = datetime(2026, 7, 1)
) -> list:
    stats = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        since.replace(tzinfo=PARIS),
        None,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return stats.get(statistic_id, [])


async def _assert_hybrid(hass: HomeAssistant) -> None:
    consumption = await _hourly_rows(hass, f"{DOMAIN}:electricity_consumption")
    assert [(r["state"], round(r["sum"], 3)) for r in consumption] == EXPECTED_HYBRID
    cost = await _hourly_rows(hass, f"{DOMAIN}:electricity_cost")
    assert [round(r["sum"], 3) for r in cost] == EXPECTED_HYBRID_COST


async def test_first_import_mixes_monthly_and_daily(hass: HomeAssistant) -> None:
    """Months before daily data get monthly points; totals match the website."""
    coordinator = GdbCoordinator(hass, _electricity_entry(hass))
    daily = AsyncMock(return_value=DAILY_SINCE_OCTOBER)
    monthly = AsyncMock(return_value=MONTHLY_SINCE_AUGUST)
    with (
        patch.object(coordinator.api, "async_get_daily_usage", new=daily),
        patch.object(coordinator.api, "async_get_monthly_usage", new=monthly),
    ):
        await coordinator._insert_statistics("electricity", ELEC)
        await async_wait_recording_done(hass)

    await _assert_hybrid(hass)


async def test_monthly_series_is_rebuilt_when_daily_data_appears(hass: HomeAssistant) -> None:
    """A monthly series is rebuilt instead of double-counting the current month."""
    coordinator = GdbCoordinator(hass, _electricity_entry(hass))
    daily = AsyncMock(return_value=[_day(8, 1, 0, 0), _day(9, 1, 0, 0)])
    monthly = AsyncMock(return_value=MONTHLY_SINCE_AUGUST[:2])
    with (
        patch.object(coordinator.api, "async_get_daily_usage", new=daily),
        patch.object(coordinator.api, "async_get_monthly_usage", new=monthly),
    ):
        await coordinator._insert_statistics("electricity", ELEC)
        await async_wait_recording_done(hass)
        rows = await _hourly_rows(hass, f"{DOMAIN}:electricity_consumption")
        assert [r["sum"] for r in rows] == [573, 1081]

        daily.return_value = DAILY_SINCE_OCTOBER
        monthly.return_value = MONTHLY_SINCE_AUGUST
        await coordinator._insert_statistics("electricity", ELEC)
        await async_wait_recording_done(hass)

        await _assert_hybrid(hass)

        # Next refresh continues day by day from the rebuilt series.
        daily.return_value = [_day(11, 1, 9.5, 2.1), _day(11, 2, 8.5, 2.0)]
        await coordinator._insert_statistics("electricity", ELEC)
        await async_wait_recording_done(hass)

    consumption = await _hourly_rows(hass, f"{DOMAIN}:electricity_consumption")
    assert [round(r["sum"], 3) for r in consumption][-2:] == [1151.5, 1160.0]


def _gas_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_GAS_HOUSE: GAS,
            CONF_ELECTRICITY_HOUSE: NONE,
        },
    )
    entry.add_to_hass(hass)
    return entry


def _paris_mean_time(read: DailyUsageRead) -> DailyUsageRead:
    """`read` dated like the parser did before 1.3.0, with pytz's replace()."""
    return dataclasses.replace(read, date=read.date.replace(tzinfo=None).replace(tzinfo=PARIS))


def _store(coordinator: GdbCoordinator, energy: str, reads: list[DailyUsageRead]) -> None:
    specs = ENERGY_STATISTICS[energy]
    coordinator._import_reads(specs, reads, {spec.statistic_id: 0.0 for spec in specs})


async def test_paris_mean_time_points_move_to_whole_hours(hass: HomeAssistant) -> None:
    """Points stored before 1.3.0 start at hh:51: they move in place."""
    coordinator = GdbCoordinator(hass, _gas_entry(hass))
    _store(
        coordinator,
        "gas",
        [_paris_mean_time(_read(1, 10, 2.0)), _paris_mean_time(_read(2, 5, 1.0))],
    )
    await async_wait_recording_done(hass)

    daily = AsyncMock(return_value=[_read(2, 5, 1.0), _read(3, 7, 1.5)])
    with patch.object(coordinator.api, "async_get_daily_usage", new=daily):
        await coordinator._insert_statistics("gas", GAS)
        await async_wait_recording_done(hass)
        moved = await _hourly_rows(hass, f"{DOMAIN}:energy_consumption")
        # The next update goes on from the moved points
        await coordinator._insert_statistics("gas", GAS)
        await async_wait_recording_done(hass)

    assert [datetime.fromtimestamp(r["start"], PARIS).isoformat() for r in moved] == [
        "2026-09-01T00:00:00+02:00",
        "2026-09-02T00:00:00+02:00",
    ]
    assert [(r["state"], r["sum"]) for r in moved] == [(10, 10), (5, 15)]
    consumption = await _hourly_rows(hass, f"{DOMAIN}:energy_consumption")
    assert [(r["state"], r["sum"]) for r in consumption] == [(10, 10), (5, 15), (7, 22)]
    cost = await _hourly_rows(hass, f"{DOMAIN}:energy_cost")
    assert [r["sum"] for r in cost] == [2.0, 3.0, 4.5]
    assert daily.await_count == 1


async def test_paris_mean_time_monthly_series_moves_and_stays_monthly(
    hass: HomeAssistant,
) -> None:
    coordinator = GdbCoordinator(hass, _electricity_entry(hass))
    months = [_month(2026, 4, 39, 8.0), _month(2026, 5, 620, 120.0), _month(2026, 6, 700, 140.0)]
    _store(coordinator, "electricity", [_paris_mean_time(m) for m in months])
    await async_wait_recording_done(hass)

    daily = AsyncMock(return_value=[_read(1, 0, 0), _read(2, 0, 0)])
    monthly = AsyncMock(
        return_value=[
            _month(2026, 5, 620, 120.0),
            _month(2026, 6, 749, 142.0),
            _month(2026, 7, 829, 155.0),
        ]
    )
    with (
        patch.object(coordinator.api, "async_get_daily_usage", new=daily),
        patch.object(coordinator.api, "async_get_monthly_usage", new=monthly),
    ):
        await coordinator._insert_statistics("electricity", ELEC)
        await async_wait_recording_done(hass)
        await coordinator._insert_statistics("electricity", ELEC)
        await async_wait_recording_done(hass)

    rows = await _hourly_rows(hass, f"{DOMAIN}:electricity_consumption", datetime(2026, 3, 1))
    assert [datetime.fromtimestamp(r["start"], PARIS).day for r in rows] == [1, 1, 1, 1]
    # Rewritten from May on top of April's moved point, without a second May
    assert [(r["state"], r["sum"]) for r in rows] == [
        (39, 39),
        (620, 659),
        (749, 1408),
        (829, 2237),
    ]


def _reset_coordinator(hass: HomeAssistant, entry: MockConfigEntry) -> GdbCoordinator:
    """A coordinator set up after the "reset statistics" option was saved."""
    hass.config_entries.async_update_entry(entry, data={**entry.data, RESET_STATISTICS: True})
    coordinator = GdbCoordinator(hass, entry)
    assert entry.data[RESET_STATISTICS] is False
    return coordinator


async def test_reset_imports_everything_again(hass: HomeAssistant) -> None:
    entry = _electricity_entry(hass)
    daily = AsyncMock(return_value=[_read(1, 10, 2.0), _read(2, 5, 1.0)])
    with patch(f"{API}.async_get_daily_usage", new=daily):
        await GdbCoordinator(hass, entry)._insert_statistics("electricity", ELEC)
        await async_wait_recording_done(hass)

        # Life corrected the 1st since; the 3rd came out
        daily.return_value = [_read(1, 12, 2.4), _read(2, 5, 1.0), _read(3, 7, 1.5)]
        coordinator = _reset_coordinator(hass, entry)
        await coordinator._insert_statistics("electricity", ELEC)
        await async_wait_recording_done(hass)
        rows = await _hourly_rows(hass, f"{DOMAIN}:electricity_consumption")
        assert [(r["state"], r["sum"]) for r in rows] == [(12, 12), (5, 17), (7, 24)]
        assert not coordinator._reset_pending

        # Done once: the next update only adds the new day
        daily.return_value = [_read(3, 7, 1.5), _read(4, 6, 1.2)]
        await coordinator._insert_statistics("electricity", ELEC)
        await async_wait_recording_done(hass)

    rows = await _hourly_rows(hass, f"{DOMAIN}:electricity_consumption")
    assert [r["sum"] for r in rows] == [12, 17, 24, 30]


@pytest.mark.parametrize("failure", ["monthly download fails", "nothing comes back"])
async def test_failed_rebuild_keeps_the_statistics(hass: HomeAssistant, failure: str) -> None:
    entry = _electricity_entry(hass)
    daily = AsyncMock(return_value=DAILY_SINCE_OCTOBER)
    monthly = AsyncMock(return_value=MONTHLY_SINCE_AUGUST)
    with (
        patch(f"{API}.async_get_daily_usage", new=daily),
        patch(f"{API}.async_get_monthly_usage", new=monthly),
    ):
        await GdbCoordinator(hass, entry)._insert_statistics("electricity", ELEC)
        await async_wait_recording_done(hass)
        await _assert_hybrid(hass)

        coordinator = _reset_coordinator(hass, entry)
        if failure == "monthly download fails":
            monthly.side_effect = Exception("Data response was not JSON (status=403)")
        else:
            daily.return_value = []
            monthly.return_value = []
        with pytest.raises(Exception, match=r"not JSON|No usage to rebuild"):
            await coordinator._insert_statistics("electricity", ELEC)
        await async_wait_recording_done(hass)

    # The rebuild downloads everything before clearing anything
    await _assert_hybrid(hass)
    assert coordinator._reset_pending == {"electricity"}


async def test_rebuild_reaches_back_to_the_oldest_point(hass: HomeAssistant) -> None:
    """Days older than the first import's range are asked again, not dropped."""
    entry = _gas_entry(hass)
    old_day = dataclasses.replace(_read(1, 10, 2.0), date=PARIS.localize(datetime(2023, 3, 15)))
    _store(GdbCoordinator(hass, entry), "gas", [old_day, _read(1, 5, 1.0)])
    await async_wait_recording_done(hass)

    coordinator = _reset_coordinator(hass, entry)
    daily = AsyncMock(return_value=[old_day, _read(1, 5, 1.0)])
    with patch(f"{API}.async_get_daily_usage", new=daily):
        await coordinator._insert_statistics("gas", GAS)
        await async_wait_recording_done(hass)

    start = daily.await_args.args[1]
    assert start == datetime(2023, 3, 15)
    rows = await _hourly_rows(hass, f"{DOMAIN}:energy_consumption", datetime(2023, 1, 1))
    assert [r["sum"] for r in rows] == [10, 15]


async def test_reset_on_an_empty_database_runs_once(hass: HomeAssistant) -> None:
    coordinator = _reset_coordinator(hass, _electricity_entry(hass))
    daily = AsyncMock(return_value=[_read(1, 10, 2.0), _read(2, 5, 1.0)])
    with patch(f"{API}.async_get_daily_usage", new=daily):
        await coordinator._insert_statistics("electricity", ELEC)
        await async_wait_recording_done(hass)
        assert not coordinator._reset_pending

        await coordinator._insert_statistics("electricity", ELEC)
        await async_wait_recording_done(hass)

    # The second update asks from the last imported day, not the whole history
    start = daily.await_args.args[1]
    assert (start.month, start.day) == (9, 2)


async def test_setup_does_not_wait_for_the_statistics_import(hass: HomeAssistant) -> None:
    # A rebuild can take a while; Home Assistant cancels a setup that long,
    # so the import runs in the background
    entry = _gas_entry(hass)
    release = asyncio.Event()
    imported = []

    async def slow_import(self: GdbCoordinator, energy: str, house: str) -> None:
        # Bounded, so that an import holding the setup fails the test instead of hanging it
        await asyncio.wait_for(release.wait(), 5)
        imported.append(energy)

    total = TotalUsageRead(amountOfEnergy=1.0, volumeOfEnergy=0.0, price=1.0)
    with (
        patch(f"{API}.async_login", new=AsyncMock(return_value=None)),
        patch(f"{API}.async_get_total_usage", new=AsyncMock(return_value=total)),
        patch.object(GdbCoordinator, "_insert_statistics", new=slow_import),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.LOADED
        assert not imported

        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)

    assert imported == ["gas"]


async def test_unloading_the_entry_cancels_the_import(hass: HomeAssistant) -> None:
    # A reload (saving the options) must not leave the old import running
    # next to the new coordinator's one
    entry = _gas_entry(hass)
    started = asyncio.Event()

    async def endless_import(self: GdbCoordinator, energy: str, house: str) -> None:
        started.set()
        await asyncio.sleep(3600)

    total = TotalUsageRead(amountOfEnergy=1.0, volumeOfEnergy=0.0, price=1.0)
    with (
        patch(f"{API}.async_login", new=AsyncMock(return_value=None)),
        patch(f"{API}.async_get_total_usage", new=AsyncMock(return_value=total)),
        patch.object(GdbCoordinator, "_insert_statistics", new=endless_import),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await asyncio.wait_for(started.wait(), 5)
        task = hass.data[DOMAIN][entry.entry_id]._statistics_task
        assert await hass.config_entries.async_unload(entry.entry_id)

    assert task.cancelled()


async def test_failed_import_keeps_the_totals_and_the_other_energy(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_GAS_HOUSE: GAS,
            CONF_ELECTRICITY_HOUSE: ELEC,
        },
    )
    entry.add_to_hass(hass)
    coordinator = GdbCoordinator(hass, entry)
    imported = []

    async def import_or_fail(self: GdbCoordinator, energy: str, house: str) -> None:
        if energy == "gas":
            raise Exception("Data response was not JSON (status=403)")
        imported.append(energy)

    total = TotalUsageRead(amountOfEnergy=1.0, volumeOfEnergy=0.0, price=1.0)
    with (
        patch(f"{API}.async_login", new=AsyncMock(return_value=None)),
        patch(f"{API}.async_get_total_usage", new=AsyncMock(return_value=total)),
        patch.object(GdbCoordinator, "_insert_statistics", new=import_or_fail),
    ):
        data = await coordinator._async_update_data()
        await hass.async_block_till_done(wait_background_tasks=True)

    assert set(data) == {"gas", "electricity"}
    assert imported == ["electricity"]

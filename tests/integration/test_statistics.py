"""Tests for the external statistics fed into the Energy dashboard."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from datetime import date, datetime, timedelta
from typing import Any
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

from custom_components.gazdebordeaux import coordinator as coordinator_module
from custom_components.gazdebordeaux.const import (
    CONF_ELECTRICITY_HOUSE,
    CONF_GAS_HOUSE,
    DOMAIN,
    NONE,
    RESET_STATISTICS,
)
from custom_components.gazdebordeaux.coordinator import ENERGY_STATISTICS, GdbCoordinator
from custom_components.gazdebordeaux.gazdebordeaux import (
    DailyUsageRead,
    Gazdebordeaux,
    HouseDetails,
    TotalUsageRead,
)

ELEC = "/api/houses/elec-uuid"
GAS = "/api/houses/gas-uuid"
API = "custom_components.gazdebordeaux.coordinator.Gazdebordeaux"
PARIS = pytz.timezone("Europe/Paris")


@pytest.fixture(autouse=True)
def _house_without_curve(monkeypatch):
    """No load curve unless a test says otherwise, and no pause between hours."""
    monkeypatch.setattr(
        Gazdebordeaux,
        "async_get_house_details",
        AsyncMock(return_value=HouseDetails(hourly_start=None)),
    )
    monkeypatch.setattr(coordinator_module, "HOURLY_REQUEST_DELAY", 0)


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
        monthly=True,
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


@pytest.mark.parametrize("process_tz", ["UTC", "America/New_York"])
async def test_daily_sums_whatever_the_process_time_zone(
    hass: HomeAssistant, monkeypatch, process_tz: str
) -> None:
    # West of Paris, the last point at Paris midnight fell on the day before:
    # Life was asked from there, and the update started from that day's sum
    await hass.config.async_set_time_zone("Europe/Paris")
    reads = [_read(1, 10, 2.0), _read(2, 5, 1.0), _read(3, 7, 1.5)]
    published = [2]

    async def daily(
        self: object, house: str, start: datetime, end: datetime
    ) -> list[DailyUsageRead]:
        # Like Life: the days from startDate on, up to the last one out
        return [r for r in reads[: published[0]] if r.date.date() >= start.date()]

    coordinator = GdbCoordinator(hass, _electricity_entry(hass))
    monkeypatch.setenv("TZ", process_tz)
    time.tzset()
    try:
        with patch(f"{API}.async_get_daily_usage", new=daily):
            await coordinator._insert_statistics("electricity", ELEC)
            await async_wait_recording_done(hass)
            published[0] = 3
            await coordinator._insert_statistics("electricity", ELEC)
            await async_wait_recording_done(hass)
    finally:
        monkeypatch.undo()
        time.tzset()

    rows = await _hourly_rows(hass, f"{DOMAIN}:electricity_consumption")
    assert [(r["state"], r["sum"]) for r in rows] == [(10, 10), (5, 15), (7, 22)]


# ---------- hour by hour, from the load curve ------------------------------

DAY_PRICE = 2.4
CONSUMPTION = f"{DOMAIN}:electricity_consumption"
COST = f"{DOMAIN}:electricity_cost"


class FakeApi(Gazdebordeaux):
    """Serve synthetic Life responses to the real parsing code."""

    def __init__(self, hourly_start: date | None) -> None:
        super().__init__(None, "user@example.com", "secret", token="token")  # type: ignore[arg-type]
        self.today = datetime.now(PARIS).date()
        self.last_day = self.today - timedelta(days=2)
        self.hourly_start = hourly_start
        self.fail_hourly = False
        self.hourly_days: list[date] = []
        # Last day with a load curve; later days answer without points
        self.hourly_until = self.last_day

    async def async_login(self) -> None:
        return None

    async def async_get_house_details(self, house: str) -> HouseDetails:
        return HouseDetails(hourly_start=self.hourly_start)

    def daily_payload(self, day: date) -> dict[str, Any]:
        return {"kwh": 12.0, "price": DAY_PRICE, "volumeOfEnergy": 0, "ratio": 0}

    async def async_get_data(self, house: str, start: Any, end: Any, scale: str) -> Any:
        if scale == "year":
            return {"total": {"kwh": 1.0, "price": 1.0}}
        if scale == "month":
            day = start.date()
            data: dict[str, Any] = {"total": {}}
            while day <= min(end.date(), self.last_day):
                data[day.isoformat()] = self.daily_payload(day)
                day += timedelta(days=1)
            return data
        assert scale == "day"
        day = start.date()
        self.hourly_days.append(day)
        if self.fail_hourly:
            raise Exception("Data response was not JSON (status=403)")
        if day > min(self.last_day, self.hourly_until):
            return {"total": {}}
        midnight = datetime(day.year, day.month, day.day)
        return {
            (midnight + timedelta(minutes=30 * i)).strftime("%Y-%m-%d %H:%M:%S"): {
                "kwh": 0.5,
                "price": 0,
                "volumeOfEnergy": 0,
            }
            for i in range(49)
        }


def _fake_coordinator(hass: HomeAssistant, api: FakeApi) -> GdbCoordinator:
    coordinator = GdbCoordinator(hass, _electricity_entry(hass))
    coordinator.api = api
    return coordinator


async def _all_rows(hass: HomeAssistant, statistic_id: str) -> list[dict]:
    await async_wait_recording_done(hass)
    return await _hourly_rows(hass, statistic_id, datetime(2000, 1, 1))


def _days(rows: list[dict]) -> dict[date, list[dict]]:
    by_day: dict[date, list[dict]] = {}
    for row in rows:
        by_day.setdefault(datetime.fromtimestamp(row["start"], PARIS).date(), []).append(row)
    return by_day


def _hours_in(day: date) -> int:
    """23, 24 or 25: the clock changes fall in any ten-day window."""
    start = PARIS.localize(datetime(day.year, day.month, day.day))
    end = PARIS.localize(datetime.combine(day + timedelta(days=1), datetime.min.time()))
    return round((end - start).total_seconds() / 3600)


def _day_count(api: FakeApi) -> int:
    return (api.last_day - date(api.today.year - 1, 1, 1)).days + 1


async def test_first_import_hourly_since_curve_start(hass: HomeAssistant) -> None:
    api = FakeApi(hourly_start=datetime.now(PARIS).date() - timedelta(days=10))
    await _fake_coordinator(hass, api)._insert_statistics("electricity", ELEC)

    days = _days(await _all_rows(hass, CONSUMPTION))
    hourly_days = sorted(d for d, r in days.items() if len(r) > 1)
    assert hourly_days[0] == api.hourly_start
    assert hourly_days[-1] == api.last_day
    assert all(len(days[d]) == _hours_in(d) for d in hourly_days)
    assert all(len(r) == 1 for d, r in days.items() if d < api.hourly_start)
    consumption = await _all_rows(hass, CONSUMPTION)
    assert consumption[-1]["sum"] == pytest.approx(12.0 * _day_count(api))
    cost = await _all_rows(hass, COST)
    assert cost[-1]["sum"] == pytest.approx(DAY_PRICE * _day_count(api))
    # The hours carry no cost: the day's is spread by consumption
    noon = next(r for r in _days(cost)[api.last_day] if r["start"] % 3600 == 0)
    assert noon["state"] == pytest.approx(DAY_PRICE * 0.5 / 12.0)


async def test_curve_older_than_life_keeps_starts_at_the_limit(
    hass: HomeAssistant, monkeypatch
) -> None:
    monkeypatch.setattr(coordinator_module, "HOURLY_MAX_DAYS", 5)
    api = FakeApi(hourly_start=datetime.now(PARIS).date() - timedelta(days=10))
    await _fake_coordinator(hass, api)._insert_statistics("electricity", ELEC)

    assert min(api.hourly_days) == api.today - timedelta(days=5)


async def test_second_update_adds_nothing_twice(hass: HomeAssistant) -> None:
    api = FakeApi(hourly_start=datetime.now(PARIS).date() - timedelta(days=5))
    coordinator = _fake_coordinator(hass, api)
    await coordinator._insert_statistics("electricity", ELEC)
    before = await _all_rows(hass, CONSUMPTION)
    await coordinator._insert_statistics("electricity", ELEC)
    after = await _all_rows(hass, CONSUMPTION)

    assert len(after) == len(before)
    assert after[-1]["sum"] == pytest.approx(before[-1]["sum"])


async def test_next_days_come_in_hour_by_hour(hass: HomeAssistant) -> None:
    api = FakeApi(hourly_start=datetime.now(PARIS).date() - timedelta(days=8))
    api.last_day -= timedelta(days=3)
    coordinator = _fake_coordinator(hass, api)
    await coordinator._insert_statistics("electricity", ELEC)
    await async_wait_recording_done(hass)

    api.last_day += timedelta(days=3)
    api.hourly_until = api.last_day
    await coordinator._insert_statistics("electricity", ELEC)
    rows = await _all_rows(hass, CONSUMPTION)

    assert rows[-1]["sum"] == pytest.approx(12.0 * _day_count(api))
    days = _days(rows)
    for day in (api.last_day - timedelta(days=d) for d in range(3)):
        assert len(days[day]) == _hours_in(day)


async def test_failed_hourly_rebuild_keeps_the_statistics(hass: HomeAssistant) -> None:
    entry = _electricity_entry(hass)
    api = FakeApi(hourly_start=datetime.now(PARIS).date() - timedelta(days=5))
    first = GdbCoordinator(hass, entry)
    first.api = api
    await first._insert_statistics("electricity", ELEC)
    before = await _all_rows(hass, CONSUMPTION)

    coordinator = _reset_coordinator(hass, entry)
    coordinator.api = api
    api.fail_hourly = True
    with pytest.raises(Exception, match="not JSON"):
        await coordinator._insert_statistics("electricity", ELEC)
    after = await _all_rows(hass, CONSUMPTION)

    assert len(after) == len(before)
    assert after[-1]["sum"] == pytest.approx(before[-1]["sum"])
    assert coordinator._reset_pending == {"electricity"}


@pytest.mark.parametrize("process_tz", ["UTC", "Asia/Tokyo"])
async def test_day_boundaries_ignore_the_process_time_zone(
    hass: HomeAssistant, monkeypatch, process_tz: str
) -> None:
    # East of Paris, the last hour of a Paris day already falls on the next
    # date: it must not hide the next day's read
    monkeypatch.setenv("TZ", process_tz)
    time.tzset()
    try:
        api = FakeApi(hourly_start=datetime.now(PARIS).date() - timedelta(days=10))
        api.hourly_until = date(2000, 1, 1)  # curve not published yet: daily reads
        coordinator = _fake_coordinator(hass, api)
        await coordinator._insert_statistics("electricity", ELEC)
        first = await _all_rows(hass, CONSUMPTION)
        assert all(len(r) == 1 for r in _days(first).values())
        # Recent days wait for their hours rather than going in daily
        waiting = api.last_day
        assert waiting not in _days(first)

        api.hourly_until = date(3000, 1, 1)  # the curve shows up
        await coordinator._insert_statistics("electricity", ELEC)
        await async_wait_recording_done(hass)
        api.last_day += timedelta(days=1)  # and one more day is published
        await coordinator._insert_statistics("electricity", ELEC)
        rows = await _all_rows(hass, CONSUMPTION)
    finally:
        monkeypatch.undo()
        time.tzset()

    assert rows[-1]["sum"] == pytest.approx(12.0 * _day_count(api))
    days = _days(rows)
    assert len(days[waiting]) == _hours_in(waiting)
    assert len(days[api.last_day]) == _hours_in(api.last_day)
    for day, day_rows in days.items():
        assert sum(r["state"] for r in day_rows) == pytest.approx(12.0), day


async def test_hours_that_miss_the_daily_read_keep_it(hass: HomeAssistant) -> None:
    api = FakeApi(hourly_start=datetime.now(PARIS).date() - timedelta(days=6))
    short_day = api.last_day - timedelta(days=1)
    curve = api.async_get_data

    async def with_a_short_day(house: str, start: Any, end: Any, scale: str) -> Any:
        data = await curve(house, start, end, scale)
        if scale == "day" and start.date() == short_day:
            # Half the day's energy: a curve with holes, not the day itself
            data = {k: {**v, "kwh": 0.25} if k != "total" else v for k, v in data.items()}
        return data

    api.async_get_data = with_a_short_day  # type: ignore[method-assign]
    await _fake_coordinator(hass, api)._insert_statistics("electricity", ELEC)
    days = _days(await _all_rows(hass, CONSUMPTION))

    assert len(days[short_day]) == 1
    assert days[short_day][0]["state"] == pytest.approx(12.0)
    assert len(days[api.last_day]) == _hours_in(api.last_day)


async def test_a_day_without_consumption_keeps_its_cost(hass: HomeAssistant) -> None:
    # A 0 kWh day still costs its subscription: spread evenly over its hours
    api = FakeApi(hourly_start=datetime.now(PARIS).date() - timedelta(days=6))
    empty = api.last_day - timedelta(days=1)
    payload, curve = api.daily_payload, api.async_get_data

    def daily_payload(day: date) -> dict[str, Any]:
        return {**payload(day), "kwh": 0.0, "price": 0.52} if day == empty else payload(day)

    async def with_an_empty_day(house: str, start: Any, end: Any, scale: str) -> Any:
        data = await curve(house, start, end, scale)
        if scale == "day" and start.date() == empty:
            data = {k: {**v, "kwh": 0.0} if k != "total" else v for k, v in data.items()}
        return data

    api.daily_payload = daily_payload  # type: ignore[method-assign]
    api.async_get_data = with_an_empty_day  # type: ignore[method-assign]
    await _fake_coordinator(hass, api)._insert_statistics("electricity", ELEC)
    hours = _days(await _all_rows(hass, COST))[empty]

    assert len(hours) == _hours_in(empty)
    assert sum(r["state"] for r in hours) == pytest.approx(0.52)


async def test_gas_volume_is_spread_over_the_hours(hass: HomeAssistant) -> None:
    api = FakeApi(hourly_start=datetime.now(PARIS).date() - timedelta(days=3))
    api.daily_payload = lambda day: {  # type: ignore[method-assign]
        "kwh": 12.0,
        "price": DAY_PRICE,
        "volumeOfEnergy": 1.2,
    }
    coordinator = GdbCoordinator(hass, _gas_entry(hass))
    coordinator.api = api
    await coordinator._insert_statistics("gas", GAS)
    days = _days(await _all_rows(hass, f"{DOMAIN}:volume"))

    assert len(days[api.last_day]) == _hours_in(api.last_day)
    assert sum(r["state"] for r in days[api.last_day]) == pytest.approx(1.2)


async def test_account_without_load_curve_stays_daily(hass: HomeAssistant) -> None:
    # Gas, or electricity without half-hourly readings: no hourly request,
    # and the latest daily reads come in at once, as before
    api = FakeApi(hourly_start=None)
    await _fake_coordinator(hass, api)._insert_statistics("electricity", ELEC)
    days = _days(await _all_rows(hass, CONSUMPTION))

    assert api.hourly_days == []
    assert all(len(r) == 1 for r in days.values())
    assert max(days) == api.last_day


async def test_house_details_failure_falls_back_to_daily(hass: HomeAssistant) -> None:
    api = FakeApi(hourly_start=datetime.now(PARIS).date() - timedelta(days=5))
    api.async_get_house_details = AsyncMock(  # type: ignore[method-assign]
        side_effect=Exception("Data response was not JSON (status=403)")
    )
    await _fake_coordinator(hass, api)._insert_statistics("electricity", ELEC)
    days = _days(await _all_rows(hass, CONSUMPTION))

    assert api.hourly_days == []
    assert max(days) == api.last_day


async def test_hours_follow_the_monthly_points(hass: HomeAssistant, monkeypatch) -> None:
    """Monthly points and the 1st's remainder stay as they are; days go hourly."""
    monkeypatch.setattr(coordinator_module, "HOURLY_MAX_DAYS", 100_000)
    coordinator = GdbCoordinator(hass, _electricity_entry(hass))
    asked = []

    async def hours(self: Gazdebordeaux, house: str, day: date, daily_energy: float) -> list:
        asked.append(day)
        return [
            DailyUsageRead(
                date=PARIS.localize(datetime(day.year, day.month, day.day, hour)),
                amountOfEnergy=daily_energy / 24,
                volumeOfEnergy=0,
                price=0,
                ratio=0,
                temperature=0,
            )
            for hour in range(24)
        ]

    with (
        patch(f"{API}.async_get_daily_usage", new=AsyncMock(return_value=DAILY_SINCE_OCTOBER)),
        patch(f"{API}.async_get_monthly_usage", new=AsyncMock(return_value=MONTHLY_SINCE_AUGUST)),
        patch(
            f"{API}.async_get_house_details",
            new=AsyncMock(return_value=HouseDetails(hourly_start=date(2026, 10, 1))),
        ),
        patch(f"{API}.async_get_hourly_usage", new=hours),
    ):
        await coordinator._insert_statistics("electricity", ELEC)

    assert asked == [date(2026, 10, 4), date(2026, 10, 5), date(2026, 10, 6), date(2026, 11, 1)]
    rows = await _all_rows(hass, CONSUMPTION)
    days = _days(rows)
    assert [len(days[date(2026, m, 1)]) for m in (8, 9, 10)] == [1, 1, 1]
    assert len(days[date(2026, 10, 5)]) == 24
    assert rows[-1]["sum"] == pytest.approx(1151.5)

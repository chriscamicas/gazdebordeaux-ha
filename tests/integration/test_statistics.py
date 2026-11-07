"""Tests for the external statistics fed into the Energy dashboard."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytz
from homeassistant.components.recorder.statistics import (
    list_statistic_ids,
    statistics_during_period,
)
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
)
from custom_components.gazdebordeaux.coordinator import GdbCoordinator
from custom_components.gazdebordeaux.gazdebordeaux import DailyUsageRead

ELEC = "/api/houses/elec-uuid"
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
        date=datetime(year, month, 1).replace(tzinfo=PARIS),
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
        date=datetime(2026, month, day).replace(tzinfo=PARIS),
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


async def _hourly_rows(hass: HomeAssistant, statistic_id: str) -> list:
    stats = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 7, 1).replace(tzinfo=PARIS),
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

# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.11] - 2026-04-28
- Import the cost statistic with `unit_class=None`: Home Assistant has no `"monetary"` unit class and rejected the whole import with "Unsupported unit_class: 'monetary'" (#47)
- Import the consumption hour by hour from the Enedis load curve (`scale=day`, half-hourly average power in kW), from the curve's start (`hourlyMeasureStartDate`, at most two years back) on a full import, then day after day; a day whose hours do not add up to its daily read keeps the daily read
- On the October clock change, fill the repeated 02:00-03:00 hour the load curve loses (its naive keys collide, leaving 49 points for 25 hours) with what the curve lacks to reach the daily read, so the day is imported hour by hour too
- Poll Life every hour while the day it should publish next is missing, every 12 hours otherwise: Life publishes each day's reads two days later, during the day, so a 12 hour pace left Home Assistant up to 12 hours behind it
- Peak / off-peak (HP/HC) contracts: four more statistics, `energy_consumption_hp`, `energy_consumption_hc`, `energy_cost_hp` and `energy_cost_hc`, with each half hour split by the contract's off-peak ranges (seasonal ones included) and priced at its own rate
- Count `consumptionHp + consumptionHc` for HP/HC days that come with `kwh` and `price` at 0, as Life's own totals do
- Start daily statistics at local midnight: pytz's `replace(tzinfo=...)` put every day at 23:51 UTC (Paris mean time); existing series are rebuilt once, on the hour
- The "reset statistics" option now clears and rebuilds the statistics (it only logged before); a rebuild clears the old series only once the new data is downloaded
- Drop deprecated `device_class` / `state_class` / `has_mean` keys from external statistic metadata; they're no longer accepted by the recorder's `StatisticMetaData` TypedDict in modern Home Assistant
- Refactor `GdbEntityDescription` to the modern frozen+kw_only single-dataclass pattern (the old mixin shape doesn't compose with the now-frozen `EntityDescription`)

## [1.1.10] - 2026-04-25
- Pick the first gas-contract house automatically when the account has multiple contracts and no `selectedHouse` (e.g. gas + electricity customers)

## [1.1.9] - 2026-04-25
- Allow clearing the optional HOUSE field in the options flow (was reverting to the previous value because the voluptuous default was re-injected on submit)

## [1.1.8] - 2026-04-24
- Normalize the house path so accounts whose `selectedHouse` is returned as `/houses/{uuid}` (without the `/api` prefix) no longer hit the SPA HTML fallback instead of the consumption JSON

## [1.1.7] - 2026-04-24
- Log the consumption request URL, parameters, status, content-type, and body to help diagnose unexpected API responses
- Raise clearer errors when the consumption payload is `None`, not a dict, or missing the `total` key, instead of a generic `KeyError`

## [1.1.6] - 2026-04-24
- Migrate to new `life.gazdebordeaux.fr/api` endpoints (old `lifeapi` subdomain now returns 403)
- Send same-origin browser headers so login and data requests are accepted
- Log the login response body on failure to aid debugging

## [1.1.2] - 2024-09-11
- Fix Error adding entity

## [1.1.1] - 2024-09-11
- Fix Blocking calls detected

## [1.1.0] - 2024-05-19
- Fix incorrect values in Energy Dashboard

## [1.0.0] - 2023-11-27

# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.0]
- Date the daily and monthly reads at midnight Paris time instead of Paris mean time, which put every point at hh:51; existing statistics are moved to the whole hour once, in place, so no history is lost
- Make the "reset statistics" option import the statistics again (it only logged a message). Like the rebuild when daily data appears, it downloads everything before clearing anything, so a failed download leaves the statistics as they were, and it starts from the oldest existing point rather than January 1st of last year, so older history is asked again instead of dropped
- Import the statistics in the background, one import at a time, stopped when the integration is unloaded or reloaded: a slow or failing import (a rebuild, Life's edge filter) no longer holds the setup or makes the sensors unavailable, and a failure on one energy no longer stops the other one's import. Failures are logged and retried at the next update
- Keep the daily running sums whole when Home Assistant's process runs in UTC (e.g. a container without `TZ`): each update asked Life from the day before the last point and started from that day's sum, so the last imported day dropped out of the total. Statistics imported that way can be rebuilt with the "reset statistics" option
- Import the consumption hour by hour from the load curve the website shows in its day view, for contracts that have one (`hourlyMeasureStartDate` on the house), within the two years Life keeps. Other contracts, the days before the curve and the monthly points stay as they were. A day whose hours are more than 10 % off its daily read keeps the daily read, recent days wait for their hours instead of going in daily, and on the October clock change the hour the curve loses is filled from the daily read
- Split peak and off-peak hours for electricity contracts with off-peak ranges (read from the house, seasonal ones included) into four more statistics: `gazdebordeaux:electricity_consumption_hp` / `_hc` and `gazdebordeaux:electricity_cost_hp` / `_hc`, costs without the subscription. Each half hour of the load curve goes to its period and each hour is priced at its own rate, the subscription spread evenly over the day; days without hours, monthly points and the point on the 1st keep the split of their read. An existing electricity series is rebuilt once to add them, and when the house comes back without its ranges the update is skipped rather than leaving a hole in them
- Read the peak and off-peak values of daily and monthly reads, and use their sum on HP/HC days whose `kwh` and `price` come back at 0, as Life's own totals do
- Poll Life every hour while the day it should publish next is missing, every 12 hours otherwise: Life publishes each day's reads two days later, during the day, so a 12 hour pace left Home Assistant up to 12 hours behind it. When more than that day is missing, Life itself is late and the usual pace is kept

## [1.2.0] - 2026-11-07
- Add electricity consumption tracking: an electricity contract is imported into the `gazdebordeaux:electricity_consumption` and `gazdebordeaux:electricity_cost` statistics (Energy dashboard → Electricity grid consumption), with matching sensors on a new "Gaz de Bordeaux Électricité" device
- Fall back to monthly statistics (one point per month, on the 1st) for contracts whose daily data is always 0, as observed on some electricity contracts; the last two months are rewritten on each refresh so the current month stays up to date
- Mix monthly and daily statistics for contracts whose daily data only starts at some point (e.g. after turning on daily reporting on the website): monthly points before it, then daily ones, with totals matching the website's; an existing monthly series is rebuilt once when daily data appears
- Pick the gas and electricity contracts from dropdowns listing every contract on the account, both during setup and from the options flow (replaces the free-text house field)
- Migrate existing entries (config entry version 2): the gas contract is kept (or auto-detected by contract type), electricity tracking stays off until enabled in the options
- Stop following the house last selected on the website: `selectedHouse` changes whenever you switch contracts on life.gazdebordeaux.fr, which could import electricity data into the gas statistics
- Reload the integration when the options are saved
- Stop sending the credentials in the body of consumption requests

## [1.1.11] - 2026-11-07
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

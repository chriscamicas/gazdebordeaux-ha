# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-10-04
- Add electricity consumption tracking: an electricity contract is imported into the `gazdebordeaux:electricity_consumption` and `gazdebordeaux:electricity_cost` statistics (Energy dashboard → Electricity grid consumption), with matching sensors on a new "Gaz de Bordeaux Électricité" device
- Fall back to monthly statistics (one point per month, on the 1st) for contracts whose daily data is always 0, as observed on some electricity contracts; the last two months are rewritten on each refresh so the current month stays up to date
- Pick the gas and electricity contracts from dropdowns listing every contract on the account, both during setup and from the options flow (replaces the free-text house field)
- Migrate existing entries (config entry version 2): the gas contract is kept (or auto-detected by contract type), electricity tracking stays off until enabled in the options
- Stop following the house last selected on the website: `selectedHouse` changes whenever you switch contracts on life.gazdebordeaux.fr, which could import electricity data into the gas statistics
- Fix cost statistics being rejected by the recorder (`Unsupported unit_class: 'monetary'`, regression from 1.1.11); cost statistics use no unit class again
- Reload the integration when the options are saved
- Stop sending the credentials in the body of consumption requests

## [1.1.11] - 2026-04-28
- Tag the cost statistic with `unit_class="monetary"` so the energy dashboard recognizes it as a currency series (was `None` before)
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

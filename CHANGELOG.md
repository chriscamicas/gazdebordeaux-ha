# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.9] - 2026-04-25
- Allow clearing the optional HOUSE field in the options flow (was reverting to the previous value because the voluptuous default was re-injected on submit)
- Pick the first gas-contract house automatically when the account has multiple contracts and no `selectedHouse` (e.g. gas + electricity customers)

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

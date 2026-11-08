# CLAUDE.md

Notes for future sessions working on this Home Assistant custom integration.

## What this is

Custom HA integration that fetches gas consumption data from Gaz de Bordeaux's customer API and feeds it into the Energy Dashboard via the Recorder statistics tables. Domain: `gazdebordeaux`. Distributed via HACS.

## Gaz de Bordeaux API

### Base URL and endpoints

The API moved during this project's lifetime; the current host is `life.gazdebordeaux.fr` (the old `lifeapi.` subdomain returns 403). All paths are under `/api`:

- `POST /api/login_check` — JSON `{email, password}` → `{token}` (JWT, ~24h expiry).
- `GET /api/users/me` — current user, includes `selectedHouse` and `houses[]`.
- `GET /api/houses/{uuid}` — house resource with `contractType.category` (`"gas"`, `"electricity"`, …).
- `GET /api{house_path}/consumptions?scale={year|month}[&startDate=&endDate=]` — consumption payload. `scale=year` returns a wrapper with a `total` key; `scale=month` returns a dict keyed by date plus a `total` entry.

### Required browser-like headers

The edge filter rejects requests that don't look like the SPA (returns a static 403 HTML page even on POSTs with valid JSON). Every authenticated call must send the `BROWSER_HEADERS` block defined at the top of `gazdebordeaux.py` (User-Agent, Accept, Origin/Referer pointing to `https://life.gazdebordeaux.fr`, `Sec-Fetch-Site: same-origin`, `Sec-Ch-Ua-*`). Drop those and you'll see 403 HTML instead of JSON.

### House-path quirks

The `selectedHouse` value comes back in two shapes depending on the account:

- `"/api/houses/{uuid}"` — already prefixed.
- `"/houses/{uuid}"` — no `/api` prefix.

`async_get_data` normalizes both before formatting `DATA_URL` (keep that normalization if you refactor).

### Multi-contract accounts and `selectedHouse`

`/users/me` returns every contract in `houses` (plain string paths) plus a `selectedHouse`:

```json
{ "selectedHouse": "/api/houses/uuid1", "houses": ["/api/houses/uuid1", "/api/houses/uuid2"] }
```

**`selectedHouse` is just the contract last picked in the web UI.** The SPA switches it with `PUT /api/houses/{uuid}` `{"selected": true|false}`, and it can be `null`. Never use it to decide what to import, and never PUT houses from the integration: that would change what the user sees on the website.

`async_list_houses()` GETs every path in `houses` and returns `House` objects carrying `contractType.category` (`"gas"` / `"electricity"`), `name`, `contractType.label` and the address, which the flows use for the dropdown labels. `async_find_house(category)` resolves the `auto` sentinel left by the v1→v2 migration. When nothing matches it raises with the `(path, category)` list. That surface is intentional, so treat it as a real error rather than papering over it.

### Electricity payload

Electricity `/consumptions` responses have the same shape as gas (`kwh`, `price`, `volumeOfEnergy`, `ratio`, `temperature`, …) with `volumeOfEnergy`/`ratio` at 0. Days or months without data may only carry a subset of keys, so read the optional ones with `.get()`. Some electricity contracts expose **no daily data at all**: `scale=month|week|day` return every day with `kwh: 0` (the website's own day/week charts are empty too), while `scale=year` with `startDate`/`endDate` returns real per-month figures keyed `YYYY-MM`. The coordinator detects this (daily history all zero) and imports monthly points instead (`_insert_monthly_statistics`), rewriting from the month before the last imported one on top of the running sum just before it. Recent zero days alone never trigger the fallback, because gas can legitimately have zero days.

A contract can switch to daily data later, once the customer turns on daily reporting on the website. Daily history then starts on that day; it isn't backfilled. Observed in November 2026: zero every day through 4 Oct, real days from 5 Oct, and 1–4 Oct only present in October's `scale=year` figure. `_insert_full_history` handles that by importing monthly points for the months before the first day with data. It then adds a point on the 1st of that month holding the monthly figure minus the month's daily reads, followed by the daily reads, so totals match the website's. When an existing series is monthly (`_is_monthly_series`: the two newest points are a month apart, on the 1st) and daily data shows up, the series is rebuilt with `_insert_full_history(..., rebuild=...)`. The "reset statistics" option goes through it too. A rebuild downloads everything before clearing the series, so a failed download leaves them untouched, and starts from the oldest existing point (`_async_get_first_day`), which can be older than the first import's range.

`DailyUsageRead.date` uses `paris_tz.localize()`. pytz's `.replace(tzinfo=paris_tz)` picks Paris mean time (LMT, +00:09) and puts each point at hh:51 in Paris time, which is how statistics were stored before 1.3.0. When the newest point of a series is off the hour, `_move_to_whole_hours` moves every point of that energy's series to the whole hour, in place (same states and sums). Importing them again from Life instead would lose the days before the first import's range. `_is_first_of_month` accepts both offsets.

### Config entry shape (version 2)

`username`, `password`, `reset_stats`, `gas_house`, `electricity_house`. Each house value is a normalized `/api/houses/{uuid}` path, `"none"`, or (gas only, from migration) `"auto"`. The house selectors are `vol.Required` with an explicit `"none"` option, which sidesteps the Optional-default pitfall below.

### Statistic IDs

Gas keeps its pre-1.2 IDs (`gazdebordeaux:energy_consumption`, `:energy_cost`, `:volume`) so that history survives. Electricity uses `gazdebordeaux:electricity_consumption` and `:electricity_cost`. Both are declared in `ENERGY_STATISTICS` in `coordinator.py`. Cost statistics need `unit_class=None`: the recorder rejects any unit class without a unit converter, and `"monetary"` has none.

### Login response on bad credentials vs. blocked request

Distinguish carefully when debugging:

- Blocked / WAF reject → HTML body, `Content-Type: text/html`, status often 200 (SPA fallback) or 403.
- Bad creds → JSON body with `{token: null}` or an error envelope. `async_login` raises with the body included.

Always check `response.headers.get("Content-Type")` and the body before assuming the response is JSON. The integration uses `response.json(content_type=None)` plus an explicit `JSONDecodeError` branch for that reason.

## Common pitfalls

### `vol.Optional(KEY, default=...)` re-injects values on submit

Voluptuous applies `default=` whenever the key is absent from input. HA's frontend omits cleared optional fields from the submitted payload, so the schema's "default" comes back as the user's "submitted" value and you can never clear the field. Use `description={"suggested_value": ...}` for the prefill instead — that affects display only and leaves the submitted (empty) value intact. Applies to any string-valued `Optional` in `option_flow.py` / `config_flow.py`.

### Coordinator update interval vs. login expiry

The coordinator runs every 12h but the JWT expires sooner, so `_async_update_data` calls `async_login()` on every refresh. Don't add caching that skips that re-login or you'll start chasing intermittent 401s.

### Statistics import runs in the background

`_async_update_data` fetches the totals for the sensors, starts `_async_insert_statistics_logged` as a background task of the config entry (one at a time; unloading the entry, as a reload does, cancels it) and returns. A failure on one energy is logged and retried at the next update, without stopping the other one. Tests that drive `_async_update_data` must wait with `hass.async_block_till_done(wait_background_tasks=True)`.

## Local testing

There is no committed repro script any more. To hit the real API fast, write a throwaway script that puts `custom_components/gazdebordeaux` on `sys.path` and imports `gazdebordeaux` directly. That module has no `homeassistant` import, only `aiohttp` and `pytz`. Then drive `Gazdebordeaux(session, username, password)` with DEBUG logging, which shows request URLs, response status, content-type and body for every call.

## Versioning / changelog convention

- Every code change targeting users bumps `manifest.json` `version` and adds a section to `CHANGELOG.md` (Keep a Changelog format).
- Versions on open (unmerged) PRs are not yet released — if you make further changes before merging, fold them into the existing version's changelog rather than bumping again. Bump only after a release lands on `main`.
- HACS picks up releases via git tags matching the `manifest.json` version.

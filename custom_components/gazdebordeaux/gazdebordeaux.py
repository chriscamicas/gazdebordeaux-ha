import dataclasses
import logging
from datetime import date, datetime, timedelta
from json.decoder import JSONDecodeError
from typing import Any

import pytz
from aiohttp import ClientSession

DATA_URL = "https://life.gazdebordeaux.fr{0}/consumptions"
LOGIN_URL = "https://life.gazdebordeaux.fr/api/login_check"
ME_URL = "https://life.gazdebordeaux.fr/api/users/me"

INPUT_DATE_FORMAT = "%Y-%m-%d"
# Interval of the load curve returned by the "day" scale
CURVE_STEP = timedelta(minutes=30)
# Months of the high season of seasonal off-peak hours (November to March)
HIGH_SEASON_MONTHS = (11, 12, 1, 2, 3)

# Browser-like headers. The WAF on life.gazdebordeaux.fr rejects requests that
# don't look like the SPA (same-origin fetch from the web app).
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        " (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": "https://life.gazdebordeaux.fr",
    "Referer": "https://life.gazdebordeaux.fr/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
}

paris_tz = pytz.timezone("Europe/Paris")
Logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
@dataclasses.dataclass
class TotalUsageRead:
    amountOfEnergy: float
    volumeOfEnergy: float
    price: float


@dataclasses.dataclass
class DailyUsageRead:
    date: datetime
    amountOfEnergy: float
    volumeOfEnergy: float
    price: float
    ratio: float
    temperature: float
    # Peak / off-peak split, filled for HP/HC electricity contracts.
    # contractPrice is the subscription share included in price.
    isHphc: bool = False
    consumptionHp: float = 0.0
    consumptionHc: float = 0.0
    priceHp: float = 0.0
    priceHc: float = 0.0
    contractPrice: float = 0.0


# Off-peak ranges of a contract, as Life gives them: [["22:00", "6:00"], ...].
# With seasonal off-peak hours, "high" applies from November to March.
@dataclasses.dataclass
class OffPeakTimes:
    all: list
    high: list
    low: list

    def ranges(self, day: date) -> list:
        if self.high and self.low:
            return self.high if day.month in HIGH_SEASON_MONTHS else self.low
        return self.all

    def is_offpeak(self, day: date, minute: int) -> bool:
        for start, end in self.ranges(day):
            first, last = _minutes(start), _minutes(end)
            if first <= last:
                if first <= minute < last:
                    return True
            elif minute >= first or minute < last:
                return True
        return False


@dataclasses.dataclass
class HouseDetails:
    """What the house resource tells about the electricity contract."""

    offpeak: OffPeakTimes | None
    # First day of the half-hourly load curve, if the customer enabled it
    hourly_start: date | None


# ----------------------------------------------------------------------------
class Gazdebordeaux:
    def __init__(
        self,
        session: ClientSession,
        username: str,
        password: str,
        token=None,
        house=None,
    ):
        self._session = session
        self._username = username
        self._password = password
        self._token: str | None = token
        self._selectedHouse: str | None = house

    async def async_login(self):
        Logger.debug("Loging in...")
        async with self._session.post(
            LOGIN_URL,
            headers=BROWSER_HEADERS,
            json={"email": self._username, "password": self._password},
        ) as response:
            body = await response.text()
            Logger.debug(
                "Login response status=%s content-type=%s body=%s",
                response.status,
                response.headers.get("Content-Type"),
                body,
            )
            try:
                token = await response.json(content_type=None)
            except JSONDecodeError as err:
                raise Exception(
                    f"Login response was not JSON "
                    f"(status={response.status}, "
                    f"content-type={response.headers.get('Content-Type')}): {body}"
                ) from err

            if token["token"] is None:
                raise Exception("invalid auth" + body)
            Logger.debug("Login response OK")
            self._token = token["token"]

    # ------------------------------------------------------
    async def async_get_total_usage(self):
        monthly_data = await self.async_get_data(None, None, "year")
        Logger.debug("Total usage raw response: %s", monthly_data)

        if monthly_data is None:
            raise Exception("Total usage response was None (likely login/auth failure)")
        if not isinstance(monthly_data, dict):
            raise Exception(
                f"Unexpected total usage response "
                f"type={type(monthly_data).__name__} value={monthly_data!r}"
            )
        if "total" not in monthly_data:
            keys = list(monthly_data.keys())
            Logger.error(
                "Total usage response missing 'total' key. Keys: %s. Full response: %s",
                keys,
                monthly_data,
            )
            raise Exception(f"Total usage response missing 'total' key. Keys present: {keys}")

        d = monthly_data["total"]
        return TotalUsageRead(
            amountOfEnergy=d["kwh"],
            volumeOfEnergy=d["volumeOfEnergy"],
            price=d["price"],
        )

    async def async_get_daily_usage(
        self, start: datetime | None, end: datetime | None
    ) -> list[DailyUsageRead]:
        daily_data = await self.async_get_data(start, end, "month")
        Logger.debug("Daily usage raw response: %s", daily_data)

        if daily_data is None:
            raise Exception("Daily usage response was None (likely login/auth failure)")
        if not isinstance(daily_data, dict):
            raise Exception(
                f"Unexpected daily usage response "
                f"type={type(daily_data).__name__} value={daily_data!r}"
            )

        usage_reads: list[DailyUsageRead] = []

        for d in daily_data:
            if d == "total":
                continue
            usage_reads.append(
                DailyUsageRead(
                    # localize(), not replace(): pytz's replace() picks Paris
                    # mean time (+00:09) and puts every day at 23:51 UTC
                    date=paris_tz.localize(datetime.strptime(d, INPUT_DATE_FORMAT)),
                    amountOfEnergy=daily_data[d]["kwh"],
                    volumeOfEnergy=daily_data[d]["volumeOfEnergy"],
                    price=daily_data[d]["price"],
                    ratio=daily_data[d]["ratio"],
                    temperature=daily_data[d]["temperature"],
                    isHphc=bool(daily_data[d].get("isHphc")),
                    consumptionHp=daily_data[d].get("consumptionHp") or 0.0,
                    consumptionHc=daily_data[d].get("consumptionHc") or 0.0,
                    priceHp=daily_data[d].get("priceHp") or 0.0,
                    priceHc=daily_data[d].get("priceHc") or 0.0,
                    contractPrice=daily_data[d].get("contractPrice") or 0.0,
                )
            )

        # Some days of HP/HC contracts come with kwh and price at 0 while
        # their peak / off-peak values are filled; Life's own totals add the
        # two periods, so do the same.
        for read in usage_reads:
            if not read.isHphc:
                continue
            split = read.consumptionHp + read.consumptionHc
            if split and abs(read.amountOfEnergy - split) > 0.05:
                read.amountOfEnergy = split
            if not read.price and (read.priceHp or read.priceHc):
                read.price = read.priceHp + read.priceHc + read.contractPrice

        return usage_reads

    async def async_get_house_details(self) -> HouseDetails:
        """Return the off-peak ranges and load curve start of the selected house."""
        if self._selectedHouse is None:
            await self.loadHouse()
        data = await self._fetch_house(_house_api_path(self._selectedHouse))
        if not isinstance(data, dict):
            return HouseDetails(offpeak=None, hourly_start=None)

        offpeak: OffPeakTimes | None = None
        if data.get("priceCategory") == "HPHC":
            times = OffPeakTimes(
                all=data.get("offPeakTimes") or [],
                high=data.get("offPeakTimesHighSeason") or [],
                low=data.get("offPeakTimesLowSeason") or [],
            )
            if times.all or (times.high and times.low):
                offpeak = times

        hourly_start: date | None = None
        start = data.get("hourlyMeasureStartDate")
        if isinstance(start, str):
            try:
                hourly_start = date.fromisoformat(start[:10])
            except ValueError:
                Logger.warning("Unexpected hourlyMeasureStartDate %r", start)

        details = HouseDetails(offpeak=offpeak, hourly_start=hourly_start)
        Logger.debug("House details: %s", details)
        return details

    async def async_get_hourly_usage(
        self,
        day: date,
        offpeak: OffPeakTimes | None = None,
        daily_energy: float | None = None,
    ) -> list[DailyUsageRead] | None:
        """Return the hourly reads of one day, or None if none are available.

        The "day" scale is what the Life web app uses for its day view: one
        request per day. It returns the Enedis load curve: each point is the
        average power in kW over the half hour ending at its key, from 00:00
        of the day to 00:00 of the next one (49 points). Each point becomes
        kW x 0.5 h at the start of its half hour; the first one belongs to
        the day before and is dropped. With off-peak ranges, each half hour
        also goes to consumptionHp or consumptionHc.

        On the October change the day has 25 hours but the curve still has 49
        points: the naive keys of the repeated 02:00-03:00 hour collide and
        one of each pair is lost. With daily_energy, the two missing half
        hours share what the curve lacks to reach the daily read.
        """
        start = datetime(day.year, day.month, day.day)
        data = await self.async_get_data(start, start, "day")
        Logger.debug("Hourly usage raw response for %s: %s", day, data)

        if not isinstance(data, dict):
            return None

        halves: list[tuple[datetime, float, dict]] = []
        previous: datetime | None = None
        for key, value in data.items():
            if key == "total" or not isinstance(value, dict):
                continue
            point = _parse_point_time(key, day, previous)
            if point is None:
                sample = {k: data[k] for k in list(data)[:2]}
                Logger.warning(
                    "Unexpected hourly key %r for %s, keeping the daily read. Sample: %s",
                    key,
                    day,
                    sample,
                )
                return None
            previous = point
            point = paris_tz.normalize(point - CURVE_STEP)
            if point.date() != day:
                continue
            halves.append((point, _energy(value) * CURVE_STEP.total_seconds() / 3600, value))

        if daily_energy is not None:
            halves.extend(_repeated_hour_halves(day, halves, daily_energy))

        hours: dict[datetime, DailyUsageRead] = {}
        for point, energy, value in sorted(halves, key=lambda h: h[0]):
            hour = paris_tz.normalize(point.replace(minute=0))
            read = hours.setdefault(
                hour,
                DailyUsageRead(
                    date=hour,
                    amountOfEnergy=0.0,
                    volumeOfEnergy=0.0,
                    price=0.0,
                    ratio=0.0,
                    temperature=0.0,
                ),
            )
            read.amountOfEnergy += energy
            if offpeak is not None:
                read.isHphc = True
                if offpeak.is_offpeak(day, point.hour * 60 + point.minute):
                    read.consumptionHc += energy
                else:
                    read.consumptionHp += energy
            read.volumeOfEnergy += value.get("volumeOfEnergy") or 0.0
            read.price += _price(value)

        return [hours[h] for h in sorted(hours)] or None

    async def async_get_data(self, start: datetime | None, end: datetime | None, scale: str) -> Any:
        try:
            if self._token is None:
                await self.async_login()
            if self._token is None:
                return None

            if self._selectedHouse is None:
                await self.loadHouse()
                Logger.debug("Loading last selected house")

            Logger.debug("Loaded house info: %s", self._selectedHouse)

            headers = {
                **BROWSER_HEADERS,
                "Authorization": "Bearer " + self._token,
                "Connection": "keep-alive",
                "Content-Type": "application/json",
            }
            payload = {"email": self._username, "password": self._password}
            params = {"scale": scale}
            if start is not None:
                params["startDate"] = start.strftime("%Y-%m-%d")
            if end is not None:
                params["endDate"] = end.strftime("%Y-%m-%d")

            url = DATA_URL.format(_house_api_path(self._selectedHouse))
            Logger.debug("Fetching data url=%s params=%s", url, params)
            async with self._session.get(
                url, headers=headers, json=payload, params=params
            ) as response:
                body = await response.text()
                Logger.debug(
                    "Data response status=%s content-type=%s body=%s",
                    response.status,
                    response.headers.get("Content-Type"),
                    body,
                )
                try:
                    return await response.json(content_type=None)
                except JSONDecodeError as err:
                    raise Exception(
                        f"Data response was not JSON "
                        f"(status={response.status}, "
                        f"content-type={response.headers.get('Content-Type')}): {body}"
                    ) from err

        except Exception:
            Logger.error("An unexpected error occured while loading the data", exc_info=True)
            raise

    async def loadHouse(self):
        if self._token is None:
            await self.async_login()
        if self._token is None:
            return

        Logger.debug("Loading house info...")

        # querying House id
        async with self._session.get(ME_URL, headers=self._authenticated_headers()) as response:
            try:
                data = await response.json()
                Logger.debug("Loaded house info: %s", data)
            except JSONDecodeError:
                Logger.error("An unexpected error occured while loading the house", exc_info=True)
                raise

        if data.get("selectedHouse"):
            self._selectedHouse = data["selectedHouse"]
            return

        # Multi-contract accounts (e.g. gas + electricity) come back with no
        # selectedHouse. Iterate the houses list and pick the first gas one.
        houses = data.get("houses") or []
        if not houses:
            raise Exception("No houses found on this account")

        Logger.debug(
            "No selectedHouse; iterating over %d houses to find a gas contract",
            len(houses),
        )
        seen: list[tuple[str, str | None]] = []
        for path in houses:
            house = await self._fetch_house(path)
            category = (house.get("contractType") or {}).get("category")
            seen.append((path, category))
            Logger.debug("House %s category=%s", path, category)
            if category == "gas":
                Logger.debug("Selected gas house %s", path)
                self._selectedHouse = path
                return

        raise Exception(f"No gas contract found among {len(houses)} houses: {seen}")

    def _authenticated_headers(self) -> dict:
        return {
            **BROWSER_HEADERS,
            "Authorization": "Bearer " + (self._token or ""),
            "Connection": "keep-alive",
            "Content-Type": "application/json",
        }

    async def _fetch_house(self, path: str) -> Any:
        url = "https://life.gazdebordeaux.fr" + path
        Logger.debug("Fetching house %s", url)
        async with self._session.get(url, headers=self._authenticated_headers()) as response:
            return await response.json(content_type=None)


# ----------------------------------------------------------------------------
_POINT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%H:%M:%S",
    "%H:%M",
)


def _parse_point_time(key: str, day: date, previous: datetime | None = None) -> datetime | None:
    """Turn a key of the "day" scale into an aware Paris datetime.

    The format is not documented; accept the usual ones and reject a bare
    date, which would mean the API answered with daily points.

    Keys are local times in chronological order. On the October change the
    02:00-03:00 hour comes twice: a time is read as summer time first, and as
    winter time when that would not come after the previous point.
    """
    try:
        parsed = datetime.fromisoformat(key)
    except ValueError:
        parsed = None
    if parsed is not None and parsed.tzinfo is not None:
        return parsed.astimezone(paris_tz)
    if parsed is None or len(key) <= len("YYYY-MM-DD"):
        parsed = None
        for fmt in _POINT_FORMATS:
            try:
                parsed = datetime.strptime(key, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.year == 1900:
        parsed = datetime.combine(day, parsed.time())
    point = paris_tz.localize(parsed, is_dst=True)
    if previous is not None and point <= previous:
        point = paris_tz.localize(parsed, is_dst=False)
    return point


def _repeated_hour_halves(
    day: date, halves: list[tuple[datetime, float, dict]], daily_energy: float
) -> list[tuple[datetime, float, dict]]:
    """Half hours of the repeated October hour that the curve lost.

    They share what the curve lacks to reach the daily read. Only up to two
    of them, all between 02:00 and 03:00, and none above the day's highest
    half hour: anything else is a real gap, left to the daily read check.
    """
    midnight = paris_tz.localize(datetime(day.year, day.month, day.day))
    next_day = day + timedelta(days=1)
    end = paris_tz.localize(datetime(next_day.year, next_day.month, next_day.day))
    if end - midnight <= timedelta(hours=24) or not halves:
        return []
    present = {point for point, _, _ in halves}
    missing = []
    point = midnight
    while point < end:
        if point not in present:
            missing.append(paris_tz.normalize(point))
        point += CURVE_STEP
    if not missing or len(missing) > 2 or any(p.hour != 2 for p in missing):
        return []
    share = (daily_energy - sum(energy for _, energy, _ in halves)) / len(missing)
    if share <= 0 or share > max(energy for _, energy, _ in halves):
        return []
    return [(p, share, {}) for p in missing]


def _house_api_path(house: str | None) -> str:
    """selectedHouse comes as "/api/houses/{uuid}" or "/houses/{uuid}": add /api once."""
    house = house or ""
    if not house.startswith("/api/"):
        if not house.startswith("/"):
            house = "/" + house
        house = "/api" + house
    return house


def _minutes(clock: str) -> int:
    """Minutes since midnight of "H:MM"; "24:00" and "23:59" end the day."""
    hours, minutes = (int(x) for x in clock.split(":")[:2])
    total = hours * 60 + minutes
    return 1440 if total >= 1439 else total


def _energy(value: dict) -> float:
    if value.get("kwh") is not None:
        return float(value["kwh"])
    return float(value.get("consumptionHp") or 0.0) + float(value.get("consumptionHc") or 0.0)


def _price(value: dict) -> float:
    if value.get("price") is not None:
        return float(value["price"])
    return float(value.get("priceHp") or 0.0) + float(value.get("priceHc") or 0.0)

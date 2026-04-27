import logging
import dataclasses
from datetime import datetime
import pytz
from aiohttp import ClientSession
from json.decoder import JSONDecodeError
from typing import List, Any, Optional

DATA_URL = "https://life.gazdebordeaux.fr{0}/consumptions"
LOGIN_URL = "https://life.gazdebordeaux.fr/api/login_check"
ME_URL = "https://life.gazdebordeaux.fr/api/users/me"

INPUT_DATE_FORMAT = "%Y-%m-%d"

# Browser-like headers. The WAF on life.gazdebordeaux.fr rejects requests that
# don't look like the SPA (same-origin fetch from the web app).
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
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

paris_tz = pytz.timezone('Europe/Paris')
Logger = logging.getLogger(__name__)

# ------------------------------------------------------------------------------------------------------------
@dataclasses.dataclass
class TotalUsageRead:
    amountOfEnergy: float
    volumeOfEnergy: float
    price: float

@dataclasses.dataclass
class MonthlyRead:
    kwh: float
    price: float
    volumeOfEnergy: float
    contractPrice: float
    consumptionPrice: float

@dataclasses.dataclass
class HouseData:
    """All data for one house."""
    house_type: str  # "gas" or "elec"
    total: TotalUsageRead
    current_month: Optional[MonthlyRead]
    previous_month: Optional[MonthlyRead]

@dataclasses.dataclass
class DailyUsageRead:
    date: datetime
    amountOfEnergy: float
    volumeOfEnergy: float
    price: float
    ratio: float
    temperature: float
# ------------------------------------------------------------------------------------------------------------
class Gazdebordeaux:
    def __init__(self, session: ClientSession, username: str, password: str, token=None, house=None):
        self._session = session
        self._username = username
        self._password = password
        self._token: str|None = token
        self._selectedHouse: str|None = house

    async def async_login(self):
        Logger.debug("Loging in...")
        async with self._session.post(LOGIN_URL, headers=BROWSER_HEADERS, json={
            "email":self._username,
            "password":self._password
        }) as response:
            body = await response.text()
            Logger.debug("Login response status=%s content-type=%s body=%s", response.status, response.headers.get("Content-Type"), body)
            try:
                token = await response.json(content_type=None)
            except JSONDecodeError:
                raise Exception("Login response was not JSON (status=%s, content-type=%s): %s" % (response.status, response.headers.get("Content-Type"), body))

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
            raise Exception("Unexpected total usage response type=%s value=%r" % (type(monthly_data).__name__, monthly_data))
        if "total" not in monthly_data:
            Logger.error("Total usage response missing 'total' key. Keys: %s. Full response: %s", list(monthly_data.keys()), monthly_data)
            raise Exception("Total usage response missing 'total' key. Keys present: %s" % list(monthly_data.keys()))

        d = monthly_data["total"]
        return TotalUsageRead(
                amountOfEnergy = d["kwh"],
                volumeOfEnergy = d["volumeOfEnergy"],
                price = d["price"],
            )

    async def async_get_daily_usage(self, start: datetime|None, end: datetime|None) -> List[DailyUsageRead]:
        daily_data = await self.async_get_data(start, end, "month")
        Logger.debug("Daily usage raw response: %s", daily_data)

        if daily_data is None:
            raise Exception("Daily usage response was None (likely login/auth failure)")
        if not isinstance(daily_data, dict):
            raise Exception("Unexpected daily usage response type=%s value=%r" % (type(daily_data).__name__, daily_data))

        usageReads: List[DailyUsageRead] = []

        for d in daily_data:
            if d == "total":
                continue
            usageReads.append(DailyUsageRead(
                date = datetime.strptime(d, INPUT_DATE_FORMAT).replace(tzinfo=paris_tz),
                amountOfEnergy = daily_data[d]["kwh"],
                volumeOfEnergy = daily_data[d]["volumeOfEnergy"],
                price = daily_data[d]["price"],
                ratio = daily_data[d]["ratio"],
                temperature = daily_data[d]["temperature"],

            ))
        
        # Logger.debug("Data transformed: %s", usageReads)
        return usageReads


    async def async_get_data(self, start: datetime|None, end: datetime|None, scale: str) -> Any:
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
            payload = {
                "email":self._username,
                "password":self._password
            }
            params = {
                "scale": scale
            }
            if start is not None:
                params["startDate"] = start.strftime("%Y-%m-%d")
            if end is not None:
                params["endDate"] = end.strftime("%Y-%m-%d")

            # selectedHouse can be "/houses/{uuid}" or "/api/houses/{uuid}" depending
            # on the account; normalize to always include the /api prefix exactly once.
            house = self._selectedHouse or ""
            if not house.startswith("/api/"):
                if not house.startswith("/"):
                    house = "/" + house
                house = "/api" + house

            url = DATA_URL.format(house)
            Logger.debug("Fetching data url=%s params=%s", url, params)
            async with self._session.get(url, headers=headers, json=payload, params=params) as response:
                body = await response.text()
                Logger.debug("Data response status=%s content-type=%s body=%s", response.status, response.headers.get("Content-Type"), body)
                try:
                    return await response.json(content_type=None)
                except JSONDecodeError:
                    raise Exception("Data response was not JSON (status=%s, content-type=%s): %s" % (response.status, response.headers.get("Content-Type"), body))

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

        Logger.debug("No selectedHouse; iterating over %d houses to find a gas contract", len(houses))
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

        raise Exception("No gas contract found among %d houses: %s" % (len(houses), seen))

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

    # --- Multi-house support ---

    async def async_load_all_houses(self) -> List[str]:
        """Return all house paths from the user profile."""
        if self._token is None:
            await self.async_login()
        async with self._session.get(ME_URL, headers=self._authenticated_headers()) as response:
            data = await response.json()
            return data.get("houses", [])

    async def async_detect_house_type(self, house_path: str) -> str:
        """Detect if a house is gas or elec by checking volumeOfEnergy."""
        yearly = await self.async_get_data_for_house(house_path, None, None, "year")
        total = yearly.get("total", {})
        return "gas" if total.get("volumeOfEnergy", 0) > 0 else "elec"

    async def async_get_house_data(self, house_path: str) -> HouseData:
        """Fetch yearly data for a house and return structured data."""
        yearly = await self.async_get_data_for_house(house_path, None, None, "year")
        total_raw = yearly["total"]
        total = TotalUsageRead(
            amountOfEnergy=total_raw.get("kwh", 0),
            volumeOfEnergy=total_raw.get("volumeOfEnergy", 0),
            price=total_raw.get("price", 0),
        )
        house_type = "gas" if total.volumeOfEnergy > 0 else "elec"
        current, previous = self._extract_monthly(yearly)
        return HouseData(house_type=house_type, total=total,
                         current_month=current, previous_month=previous)

    async def async_get_data_for_house(self, house_path, start, end, scale) -> Any:
        """Fetch data for a specific house (not necessarily the selected one)."""
        if self._token is None:
            await self.async_login()
        house = house_path or ""
        if not house.startswith("/api/"):
            if not house.startswith("/"):
                house = "/" + house
            house = "/api" + house
        url = DATA_URL.format(house)
        params = {"scale": scale}
        if start is not None:
            params["startDate"] = start.strftime("%Y-%m-%d")
        if end is not None:
            params["endDate"] = end.strftime("%Y-%m-%d")
        async with self._session.get(url, headers=self._authenticated_headers(), params=params) as response:
            return await response.json(content_type=None)

    def _extract_monthly(self, yearly_data: dict):
        """Extract current and previous month from yearly API response."""
        now = datetime.now(paris_tz)
        current_key = now.strftime("%Y-%m")
        prev_month = now.month - 1 or 12
        prev_year = now.year if now.month > 1 else now.year - 1
        prev_key = f"{prev_year}-{prev_month:02d}"
        current = None
        previous = None
        for key, val in yearly_data.items():
            if key == "total" or not isinstance(val, dict):
                continue
            if key == current_key:
                current = self._parse_monthly(val)
            elif key == prev_key:
                previous = self._parse_monthly(val)
        return current, previous

    @staticmethod
    def _parse_monthly(data: dict) -> MonthlyRead:
        return MonthlyRead(
            kwh=data.get("kwh", 0), price=data.get("price", 0),
            volumeOfEnergy=data.get("volumeOfEnergy", 0),
            contractPrice=data.get("contractPrice", 0),
            consumptionPrice=data.get("consumptionPrice", 0),
        )

import logging
import dataclasses
from datetime import datetime
import pytz
from aiohttp import ClientSession
from json.decoder import JSONDecodeError
from typing import List, Any

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
        
        headers = {
            **BROWSER_HEADERS,
            "Authorization": "Bearer " + self._token,
            "Connection": "keep-alive",
            "Content-Type": "application/json",
        }

        # querying House id
        async with self._session.get(ME_URL, headers=headers) as response:
            try:
                data = await response.json()
                self._selectedHouse = data["selectedHouse"]
                Logger.debug("Loaded house info: %s", data)

            except JSONDecodeError:
                Logger.error("An unexpected error occured while loading the house", exc_info=True)
                raise

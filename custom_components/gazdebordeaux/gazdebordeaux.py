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


class Gazdebordeaux:
    def __init__(self, session: ClientSession, username: str, password: str, token=None, house=None):
        self._session = session
        self._username = username
        self._password = password
        self._token: str | None = token
        self._selectedHouse: str | None = house or None
        self._all_houses: List[str] = []

    async def async_login(self):
        Logger.debug("Logging in...")
        async with self._session.post(LOGIN_URL, headers=BROWSER_HEADERS, json={
            "email": self._username,
            "password": self._password
        }) as response:
            body = await response.text()
            Logger.debug("Login response status=%s", response.status)
            try:
                token = await response.json(content_type=None)
            except JSONDecodeError:
                raise Exception(
                    "Login response was not JSON (status=%s): %s"
                    % (response.status, body)
                )
            if token.get("token") is None:
                raise Exception("invalid auth: " + body)
            self._token = token["token"]

    async def async_load_houses(self) -> List[str]:
        """Load all houses from the user profile."""
        if self._token is None:
            await self.async_login()
        headers = {**BROWSER_HEADERS, "Authorization": "Bearer " + self._token}
        async with self._session.get(ME_URL, headers=headers) as response:
            data = await response.json()
            self._selectedHouse = data["selectedHouse"]
            self._all_houses = data.get("houses", [self._selectedHouse])
            Logger.debug("Houses: %s", self._all_houses)
            return self._all_houses

    async def async_detect_house_type(self, house_path: str) -> str:
        """Detect if a house is gas or elec by checking volumeOfEnergy."""
        yearly = await self._get_house_data(house_path, None, None, "year")
        total = yearly.get("total", {})
        return "gas" if total.get("volumeOfEnergy", 0) > 0 else "elec"

    async def async_get_house_data(self, house_path: str) -> HouseData:
        """Fetch yearly data for a house and return structured data."""
        yearly = await self._get_house_data(house_path, None, None, "year")
        total_raw = yearly["total"]

        total = TotalUsageRead(
            amountOfEnergy=total_raw.get("kwh", 0),
            volumeOfEnergy=total_raw.get("volumeOfEnergy", 0),
            price=total_raw.get("price", 0),
        )
        house_type = "gas" if total.volumeOfEnergy > 0 else "elec"
        current, previous = self._extract_monthly(yearly)

        return HouseData(
            house_type=house_type,
            total=total,
            current_month=current,
            previous_month=previous,
        )

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
            kwh=data.get("kwh", 0),
            price=data.get("price", 0),
            volumeOfEnergy=data.get("volumeOfEnergy", 0),
            contractPrice=data.get("contractPrice", 0),
            consumptionPrice=data.get("consumptionPrice", 0),
        )

    # --- backward compat: used by coordinator for statistics import ---

    async def async_get_total_usage(self) -> TotalUsageRead:
        yearly = await self.async_get_data(None, None, "year")
        d = yearly["total"]
        return TotalUsageRead(
            amountOfEnergy=d["kwh"],
            volumeOfEnergy=d["volumeOfEnergy"],
            price=d["price"],
        )

    async def async_get_daily_usage(self, start, end) -> List[DailyUsageRead]:
        daily_data = await self.async_get_data(start, end, "month")
        reads = []
        for key, val in daily_data.items():
            if key == "total":
                continue
            reads.append(DailyUsageRead(
                date=datetime.strptime(key, INPUT_DATE_FORMAT).replace(tzinfo=paris_tz),
                amountOfEnergy=val["kwh"],
                volumeOfEnergy=val["volumeOfEnergy"],
                price=val["price"],
                ratio=val["ratio"],
                temperature=val["temperature"],
            ))
        return reads

    async def async_get_data(self, start, end, scale) -> Any:
        if self._selectedHouse is None:
            await self.async_load_houses()
        return await self._get_house_data(self._selectedHouse, start, end, scale)

    async def _get_house_data(self, house_path, start, end, scale) -> Any:
        if self._token is None:
            await self.async_login()

        headers = {
            **BROWSER_HEADERS,
            "Authorization": "Bearer " + self._token,
            "Content-Type": "application/json",
        }
        params = {"scale": scale}
        if start is not None:
            params["startDate"] = start.strftime("%Y-%m-%d")
        if end is not None:
            params["endDate"] = end.strftime("%Y-%m-%d")

        async with self._session.get(
            DATA_URL.format(house_path), headers=headers, params=params
        ) as response:
            return await response.json()

    async def loadHouse(self):
        """Legacy compat."""
        await self.async_load_houses()

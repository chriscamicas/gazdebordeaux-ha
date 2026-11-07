import dataclasses
import logging
from datetime import datetime
from json.decoder import JSONDecodeError
from typing import Any

import pytz
from aiohttp import ClientSession

DATA_URL = "https://life.gazdebordeaux.fr{0}/consumptions"
LOGIN_URL = "https://life.gazdebordeaux.fr/api/login_check"
ME_URL = "https://life.gazdebordeaux.fr/api/users/me"

INPUT_DATE_FORMAT = "%Y-%m-%d"
INPUT_MONTH_FORMAT = "%Y-%m"

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
class House:
    path: str
    category: str | None
    name: str | None = None
    contract_label: str | None = None
    address: str | None = None

    @property
    def label(self) -> str:
        parts = [self.name, self.contract_label, self.address]
        return " - ".join(p for p in parts if p) or self.path


@dataclasses.dataclass
class DailyUsageRead:
    date: datetime
    amountOfEnergy: float
    volumeOfEnergy: float
    price: float
    ratio: float
    temperature: float


# ----------------------------------------------------------------------------
def _normalize_house_path(house: str) -> str:
    """Return the house path with the /api prefix exactly once.

    Accounts return either "/houses/{uuid}" or "/api/houses/{uuid}".
    """
    if not house.startswith("/api/"):
        if not house.startswith("/"):
            house = "/" + house
        house = "/api" + house
    return house


# ----------------------------------------------------------------------------
class Gazdebordeaux:
    def __init__(
        self,
        session: ClientSession,
        username: str,
        password: str,
        token=None,
    ):
        self._session = session
        self._username = username
        self._password = password
        self._token: str | None = token

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
    async def async_get_total_usage(self, house: str) -> TotalUsageRead:
        monthly_data = await self.async_get_data(house, None, None, "year")
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
            volumeOfEnergy=d.get("volumeOfEnergy", 0),
            price=d["price"],
        )

    async def async_get_daily_usage(
        self, house: str, start: datetime | None, end: datetime | None
    ) -> list[DailyUsageRead]:
        daily_data = await self.async_get_data(house, start, end, "month")
        Logger.debug("Daily usage raw response: %s", daily_data)
        return self._parse_usage_reads(daily_data, INPUT_DATE_FORMAT)

    async def async_get_monthly_usage(
        self, house: str, start: datetime, end: datetime
    ) -> list[DailyUsageRead]:
        """Per-month reads, dated on the 1st of each month.

        Some electricity contracts only expose monthly figures: the daily scale
        returns every day at 0 while scale=year returns real monthly values.
        """
        monthly_data = await self.async_get_data(house, start, end, "year")
        Logger.debug("Monthly usage raw response: %s", monthly_data)
        return self._parse_usage_reads(monthly_data, INPUT_MONTH_FORMAT)

    @staticmethod
    def _parse_usage_reads(daily_data: Any, date_format: str) -> list[DailyUsageRead]:
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
            # Electricity contracts return the same shape as gas but with
            # volumeOfEnergy/ratio at 0, and days without data may omit keys.
            usage_reads.append(
                DailyUsageRead(
                    date=datetime.strptime(d, date_format).replace(tzinfo=paris_tz),
                    amountOfEnergy=daily_data[d]["kwh"],
                    volumeOfEnergy=daily_data[d].get("volumeOfEnergy", 0),
                    price=daily_data[d]["price"],
                    ratio=daily_data[d].get("ratio", 0),
                    temperature=daily_data[d].get("temperature", 0),
                )
            )

        return usage_reads

    async def async_get_data(
        self, house: str, start: datetime | None, end: datetime | None, scale: str
    ) -> Any:
        try:
            if self._token is None:
                await self.async_login()
            if self._token is None:
                return None

            params = {"scale": scale}
            if start is not None:
                params["startDate"] = start.strftime("%Y-%m-%d")
            if end is not None:
                params["endDate"] = end.strftime("%Y-%m-%d")

            url = DATA_URL.format(_normalize_house_path(house))
            Logger.debug("Fetching data url=%s params=%s", url, params)
            async with self._session.get(
                url, headers=self._authenticated_headers(), params=params
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

    # ------------------------------------------------------
    async def async_list_houses(self) -> list[House]:
        """Return every house (contract) of the account with its category.

        `selectedHouse` from /users/me is deliberately ignored: it reflects the
        house last picked in the web UI, not a stable preference.
        """
        if self._token is None:
            await self.async_login()

        Logger.debug("Loading houses...")
        async with self._session.get(ME_URL, headers=self._authenticated_headers()) as response:
            try:
                data = await response.json(content_type=None)
                Logger.debug("Loaded user info: %s", data)
            except JSONDecodeError:
                Logger.error("An unexpected error occured while loading the houses", exc_info=True)
                raise

        paths = list(data.get("houses") or [])
        if not paths and data.get("selectedHouse"):
            paths = [data["selectedHouse"]]
        if not paths:
            raise Exception("No houses found on this account")

        houses: list[House] = []
        for raw_path in paths:
            path = _normalize_house_path(raw_path)
            raw = await self._fetch_house(path) or {}
            contract = raw.get("contractType") or {}
            address = " ".join(
                p for p in (raw.get("addressStreet"), raw.get("addressLocality")) if p
            ).strip()
            house = House(
                path=path,
                category=contract.get("category"),
                name=raw.get("name"),
                contract_label=contract.get("label"),
                address=address or None,
            )
            Logger.debug("House %s category=%s", path, house.category)
            houses.append(house)
        return houses

    async def async_find_house(self, category: str) -> str:
        """Return the path of the first house holding a `category` contract."""
        houses = await self.async_list_houses()
        for house in houses:
            if house.category == category:
                Logger.debug("Selected %s house %s", category, house.path)
                return house.path

        seen = [(h.path, h.category) for h in houses]
        raise Exception(f"No {category} contract found among {len(houses)} houses: {seen}")

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

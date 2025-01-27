import logging
import dataclasses
from datetime import datetime, time
import pytz
from aiohttp import ClientSession
from json.decoder import JSONDecodeError
from typing import List, Any

DATA_URL = "https://lifeapi.gazdebordeaux.fr{0}/consumptions"
LOGIN_URL = "https://lifeapi.gazdebordeaux.fr/login_check"
ME_URL = "https://lifeapi.gazdebordeaux.fr/users/me"
HOUSE_URL = "https://lifeapi.gazdebordeaux.fr${0}"
HOUSES_URL = "https://lifeapi.gazdebordeaux.fr/addresses"

INPUT_DATE_FORMAT = "%Y-%m-%d"

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

@dataclasses.dataclass
class OffPeakTime:
    startTime: time
    endTime: time

@dataclasses.dataclass
class House:
    id: str
    remoteAddressId: str
    address: str
    selected: bool
    contractCategory: str # "gas" or "electricity"
    contractCode: str
    priceCategory: str
    offPeakTimes: None|List[OffPeakTime]
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
        async with self._session.post(LOGIN_URL, json={
            "email":self._username,
            "password":self._password
        }) as response:
            token = await response.json()

            if token["token"] is None:
                raise Exception("invalid auth" + await response.text())
            Logger.debug("Login response OK")
            self._token = token["token"]

    # ------------------------------------------------------
    async def async_get_total_usage(self):
        monthly_data = await self.async_get_data(None, None, "year")
        # Logger.debug("Data retreived %s", monthly_data)

        d = monthly_data["total"]
        return TotalUsageRead(
                amountOfEnergy = d["kwh"],
                volumeOfEnergy = d["volumeOfEnergy"],
                price = d["price"],
            )
    
    async def async_get_daily_usage(self, start: datetime|None, end: datetime|None) -> List[DailyUsageRead]:
        daily_data = await self.async_get_data(start, end, "month")
        # Logger.debug("Data retreived %s", daily_data)

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
                "Authorization": "Bearer " + self._token,
                "Connection": "keep-alive",
                "Content-Type": "application/json"
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

            async with self._session.get(DATA_URL.format(self._selectedHouse), headers=headers, json=payload, params=params) as response:
                return await response.json()

        except Exception:
            Logger.error("An unexpected error occured while loading the data", exc_info=True)
            raise

    async def async_get_houses(self):
        if self._token is None:
            await self.async_login()
        if self._token is None:
            return
        
        Logger.debug("Loading houses info...")
        
        headers = {
            "Authorization": "Bearer " + self._token,
            "Connection": "keep-alive",
            "Content-Type": "application/json"
        }

        # querying House id
        async with self._session.get(HOUSES_URL, headers=headers) as response:
            try:
                data = await response.json()
                houses_data = data["hydra:member"]
                
                houses: List[House] = []

                for d in range(len(houses_data)):
                    offPeakTimeData = houses_data[d]["offPeakTimes"]
                    offPeakTimes = None
                    if offPeakTimeData is not None and len(offPeakTimeData) > 0:
                        offPeakTimes = list(map(lambda x: OffPeakTime(
                            startTime=time(hour=int(x[0].split(":")[0]), minute=int(x[0].split(":")[1])),
                            endTime=time(hour=int(x[0].split(":")[0]), minute=int(x[0].split(":")[1])),
                        ), offPeakTimeData))

                    houses.append(House(
                        id= houses_data[d]["@id"],
                        remoteAddressId= houses_data[d]["remoteAddressId"],
                        address= houses_data[d]["addressStreet"],
                        selected= houses_data[d]["selected"],
                        contractCategory= houses_data[d]["contractType"]["category"],
                        contractCode= houses_data[d]["contractType"]["code"],
                        priceCategory= houses_data[d]["priceCategory"],
                        offPeakTimes=offPeakTimes
                    ))
                
                Logger.debug("Data transformed: %s", houses)
                return houses

            except JSONDecodeError:
                Logger.error("An unexpected error occured while loading the houses list", exc_info=True)
                raise

    async def loadHouse(self):
        houses = await self.async_get_houses()

        if houses is None or len(houses) == 0:
            raise
        
        selectedHouse = next(house for house in houses if house.selected)

        if selectedHouse is None:
            raise

        self._selectedHouse = selectedHouse.id
        return selectedHouse

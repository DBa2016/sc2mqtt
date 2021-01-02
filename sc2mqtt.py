#!/usr/bin/env python3 
import time
import hashlib
from base64 import b64decode, b64encode
import requests
from requests.exceptions import InvalidSchema
from pyquery import PyQuery as pyq
import re
import json
import logging
import asyncio
from functools import partial

import paho.mqtt.client as mqtt
from pathlib import Path

import nest_asyncio
nest_asyncio.apply()

from colorlog import ColoredFormatter

import getopt
import sys

def setup_logger(name):
    """Return a logger with a default ColoredFormatter."""
    formatter = ColoredFormatter(
        "%(log_color)s[%(levelname)-8s]-%(asctime)s%(reset)s %(cyan)s%(message)s%(reset)s",
        datefmt='%Y-%m-%d %H:%M:%S',
        reset=True,
        log_colors={
            'DEBUG':    'cyan',
            'INFO':     'green',
            'WARNING':  'yellow',
            'ERROR':    'red',
            'CRITICAL': 'red',
        }
    )

    logger = logging.getLogger(name)
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    if loglevel:
        logger.setLevel(getattr(logging, loglevel.upper(), None))
    else:
        logger.setLevel(logging.DEBUG)

    if logfile:
        fileformatter = ColoredFormatter(
            "[%(levelname)-8s]-%(asctime)s %(message)s",
            datefmt='%Y-%m-%d %H:%M:%S',
            reset=False
        )
        filehandler = logging.FileHandler(logfile)
        filehandler.setFormatter(fileformatter)
        logger.addHandler(filehandler)

    return logger

STATLIMITS = [
    { "mask": r"LOCK_STATE.*DOOR", "check": "door_locked", "fail": 1 },
    { "mask": r"OPEN_STATE.*DOOR", "check": "door_closed", "fail": 0 },
    { "mask": r"OPEN_STATE.*HOOD", "check": "door_closed", "fail": 0 },
    { "mask": r"OPEN_STATE.*LID", "check": "door_closed", "fail": 0 },
    { "mask": r"STATE.*WINDOW", "check": "window_closed", "fail": 0 },
    { "mask": r"STATE.*COVER", "check": "window_closed", "fail": 0 },
]

# Defaults and Command line
logfile=""
configfile="config.json"
loglevel="DEBUG"
opts, args = getopt.getopt(sys.argv[1:], 'f:l:c:', ['logfile=', 'loglevel=', 'configfile='])
for opt, arg in opts:
    if opt in ('-f', '--logfile'):
        logfile=arg
    elif opt in ('-l', '--loglevel'):
        loglevel=arg
    elif opt in ('-c', '--configfile'):
        configfile=arg

#logging.basicConfig(level=logging.INFO)
_LOGGER = setup_logger("s2m")

async def main():

    try:
        with open(configfile, "r") as cfile:
            cfo = json.load(cfile)

        for el in ["user", "password", "broker"]:
            if el not in cfo:
                _LOGGER.critical("No %s defined in config file" % el)
                return False
        ad = SkodaAdapter(cfo["user"], cfo["password"])
        await ad.init()
        mqttc = mqtt.Client()
        mqttc.connect(cfo["broker"])
        mqttc.loop_start()
        
        loop = asyncio.get_event_loop()
        loop.run_until_complete(
            asyncio.gather(
               ad.updateValues(mqttc),
               ad.loopRefreshTokens()
            )
        )
        loop.close()

        return True


    except FileNotFoundError:
        _LOGGER.critical("Config file not found!")
        await configSample()
        return False
    except json.decoder.JSONDecodeError:
        _LOGGER.critical("Config file found and readable, invalid contents!")
        await configSample()
        return False


async def configSample():
    _LOGGER.critical("Writing sample config as config.json.sample, please adjust as needed and save as config.json!")
    with open("config.json.sample", "w") as cfile:
        json.dump({
            "user": "test@example.com",
            "password": "my_very_speciaL_passw0rd",
            "broker": "mqtt.local"
        }, cfile)



class VWThrottledException(Exception):
    # attributes:
    #   message
    #   code
    def __init__(self, message):
        self.message = message

class HTTPCodeException(Exception):
    # attributes:
    #   message
    #   code
    def __init__(self, message, code):
        self.message = message
        self.code = code

class RedirectedToSkodaException(Exception):
    # attributes:
    #   url
    def __init__(self, url):
        self.url = code

class SkodaAdapter:
    elems2tokens = {
            'state': 'state',
            '#state': 'state',
            'code': 'jwtauth_code',
            'access_token': 'jwtaccess_token',
            'id_token': 'jwtid_token'
    }


    statusValues = {
        "0x0203010001":{"statusName": "MAINTENANCE_INTERVAL_DISTANCE_TO_OIL_CHANGE", "unit_of_measurement": "km"},
        "0x0203010002":{"statusName": "MAINTENANCE_INTERVAL_TIME_TO_OIL_CHANGE", "unit_of_measurement": "days"},
        "0x0203010003":{"statusName": "MAINTENANCE_INTERVAL_DISTANCE_TO_INSPECTION", "unit_of_measurement": "km"},
        "0x0203010004":{"statusName": "MAINTENANCE_INTERVAL_TIME_TO_INSPECTION", "unit_of_measurement": "days"},
        "0x0203010005":{"statusName": "WARNING_OIL_CHANGE", "unit_of_measurement": ""},
        "0x0203010006":{"statusName": "MAINTENANCE_INTERVAL_ALARM_INSPECTION", "unit_of_measurement": ""},
        "0x0203010007":{"statusName": "MAINTENANCE_INTERVAL_MONTHLY_MILEAGE", "unit_of_measurement": ""},
        "0x02040C0001":{"statusName": "MAINTENANCE_INTERVAL_AD_BLUE_RANGE", "unit_of_measurement": ""},
        "0x0204040001":{"statusName": "OIL_LEVEL_AMOUNT_IN_LITERS", "unit_of_measurement": "l"},
        "0x0204040002":{"statusName": "OIL_LEVEL_MINIMUM_WARNING", "unit_of_measurement": ""},
        "0x0204040003":{"statusName": "OIL_LEVEL_DIPSTICK_PERCENTAGE", "unit_of_measurement": "%"},
        "0x0301010001":{"statusName": "LIGHT_STATUS", "unit_of_measurement": ""},
        "0x0301030005":{"statusName": "TOTAL_RANGE", "unit_of_measurement": "km"},
        "0x030103000A":{"statusName": "FUEL_LEVEL_IN_PERCENTAGE", "unit_of_measurement": "%"},
        "0x030103000D":{"statusName": "CNG_LEVEL_IN_PERCENTAGE", "unit_of_measurement": "%"},
        "0x0301040001":{"statusName": "LOCK_STATE_LEFT_FRONT_DOOR", "unit_of_measurement": ""},
        "0x0301040002":{"statusName": "OPEN_STATE_LEFT_FRONT_DOOR", "unit_of_measurement": ""},
        "0x0301040003":{"statusName": "SAFETY_STATE_LEFT_FRONT_DOOR", "unit_of_measurement": ""},
        "0x0301040004":{"statusName": "LOCK_STATE_LEFT_REAR_DOOR", "unit_of_measurement": ""},
        "0x0301040005":{"statusName": "OPEN_STATE_LEFT_REAR_DOOR", "unit_of_measurement": ""},
        "0x0301040006":{"statusName": "SAFETY_STATE_LEFT_REAR_DOOR", "unit_of_measurement": ""},
        "0x0301040007":{"statusName": "LOCK_STATE_RIGHT_FRONT_DOOR", "unit_of_measurement": ""},
        "0x0301040008":{"statusName": "OPEN_STATE_RIGHT_FRONT_DOOR", "unit_of_measurement": ""},
        "0x0301040009":{"statusName": "SAFETY_STATE_RIGHT_FRONT_DOOR", "unit_of_measurement": ""},
        "0x030104000A":{"statusName": "LOCK_STATE_RIGHT_REAR_DOOR", "unit_of_measurement": ""},
        "0x030104000B":{"statusName": "OPEN_STATE_RIGHT_REAR_DOOR", "unit_of_measurement": ""},
        "0x030104000C":{"statusName": "SAFETY_STATE_RIGHT_REAR_DOOR", "unit_of_measurement": ""},
        "0x030104000D":{"statusName": "LOCK_STATE_TRUNK_LID", "unit_of_measurement": ""},
        "0x030104000E":{"statusName": "OPEN_STATE_TRUNK_LID", "unit_of_measurement": ""},
        "0x030104000F":{"statusName": "SAFETY_STATE_TRUNK_LID", "unit_of_measurement": ""},
        "0x0301040010":{"statusName": "LOCK_STATE_HOOD", "unit_of_measurement": ""},
        "0x0301040011":{"statusName": "OPEN_STATE_HOOD", "unit_of_measurement": ""},
        "0x0301040012":{"statusName": "SAFETY_STATE_HOOD", "unit_of_measurement": ""},
        "0x0301050001":{"statusName": "STATE_LEFT_FRONT_WINDOW", "unit_of_measurement": ""},
        "0x0301050002":{"statusName": "POSITION_LEFT_FRONT_WINDOW", "unit_of_measurement": ""},
        "0x0301050003":{"statusName": "STATE_LEFT_REAR_WINDOW", "unit_of_measurement": ""},
        "0x0301050004":{"statusName": "POSITION_LEFT_REAR_WINDOW", "unit_of_measurement": ""},
        "0x0301050005":{"statusName": "STATE_RIGHT_FRONT_WINDOW", "unit_of_measurement": ""},
        "0x0301050006":{"statusName": "POSITION_RIGHT_FRONT_WINDOW", "unit_of_measurement": ""},
        "0x0301050007":{"statusName": "STATE_RIGHT_REAR_WINDOW", "unit_of_measurement": ""},
        "0x0301050008":{"statusName": "POSITION_RIGHT_REAR_WINDOW", "unit_of_measurement": ""},
        "0x0301050009":{"statusName": "STATE_CONVERTIBLE_TOP", "unit_of_measurement": ""},
        "0x030105000A":{"statusName": "POSITION_CONVERTIBLE_TOP", "unit_of_measurement": ""},
        "0x030105000B":{"statusName": "STATE_SUN_ROOF_MOTOR_COVER", "unit_of_measurement": ""},
        "0x030105000C":{"statusName": "POSITION_SUN_ROOF_MOTOR_COVER", "unit_of_measurement": ""},
        "0x030105000D":{"statusName": "STATE_SUN_ROOF_REAR_MOTOR_COVER_3", "unit_of_measurement": ""},
        "0x030105000E":{"statusName": "POSITION_SUN_ROOF_REAR_MOTOR_COVER_3", "unit_of_measurement": ""},
        "0x030105000F":{"statusName": "STATE_SERVICE_FLAP", "unit_of_measurement": ""},
        "0x0301050010":{"statusName": "POSITION_SERVICE_FLAP", "unit_of_measurement": ""},
        "0x0301050011":{"statusName": "STATE_SPOILER", "unit_of_measurement": ""},
        "0x0301050012":{"statusName": "POSITION_SPOILER", "unit_of_measurement": ""},
        "0x0101010001":{"statusName": "UTC_TIME_STATUS", "unit_of_measurement": ""},
        "0x0101010002":{"statusName": "KILOMETER_STATUS", "unit_of_measurement": ""},
        "0x0301030006":{"statusName": "PRIMARY_RANGE", "unit_of_measurement": "km"},
        "0x0301030007":{"statusName": "PRIMARY_DRIVE", "unit_of_measurement": ""},
        "0x0301030008":{"statusName": "SECONDARY_RANGE", "unit_of_measurement": "km"},
        "0x0301030009":{"statusName": "SECONDARY_DRIVE", "unit_of_measurement": ""},
        "0x0301030002":{"statusName": "STATE_OF_CHARGE", "unit_of_measurement": ""},
        "0x0301020001":{"statusName": "TEMPERATURE_OUTSIDE", "unit_of_measurement": "", "calc": lambda t: (int(t)-2732)/10},
        "0x0301030001":{"statusName": "PARKING_BRAKE", "unit_of_measurement": ""},
        "0x0301060001":{"statusName": "TYRE_PRESSURE_LEFT_FRONT_CURRENT_VALUE", "unit_of_measurement": ""},
        "0x0301060002":{"statusName": "TYRE_PRESSURE_LEFT_FRONT_DESIRED_VALUE", "unit_of_measurement": ""},
        "0x0301060003":{"statusName": "TYRE_PRESSURE_LEFT_REAR_CURRENT_VALUE", "unit_of_measurement": ""},
        "0x0301060004":{"statusName": "TYRE_PRESSURE_LEFT_REAR_DESIRED_VALUE", "unit_of_measurement": ""},
        "0x0301060005":{"statusName": "TYRE_PRESSURE_RIGHT_FRONT_CURRENT_VALUE", "unit_of_measurement": ""},
        "0x0301060006":{"statusName": "TYRE_PRESSURE_RIGHT_FRONT_DESIRED_VALUE", "unit_of_measurement": ""},
        "0x0301060007":{"statusName": "TYRE_PRESSURE_RIGHT_REAR_CURRENT_VALUE", "unit_of_measurement": ""},
        "0x0301060008":{"statusName": "TYRE_PRESSURE_RIGHT_REAR_DESIRED_VALUE", "unit_of_measurement": ""},
        "0x0301060009":{"statusName": "TYRE_PRESSURE_SPARE_TYRE_CURRENT_VALUE", "unit_of_measurement": ""},
        "0x030106000A":{"statusName": "TYRE_PRESSURE_SPARE_TYRE_DESIRED_VALUE", "unit_of_measurement": ""},
        "0x030106000B":{"statusName": "TYRE_PRESSURE_LEFT_FRONT_TYRE_DIFFERENCE", "unit_of_measurement": ""},
        "0x030106000C":{"statusName": "TYRE_PRESSURE_LEFT_REAR_TYRE_DIFFERENCE", "unit_of_measurement": ""},
        "0x030106000D":{"statusName": "TYRE_PRESSURE_RIGHT_FRONT_TYRE_DIFFERENCE", "unit_of_measurement": ""},
        "0x030106000E":{"statusName": "TYRE_PRESSURE_RIGHT_REAR_TYRE_DIFFERENCE", "unit_of_measurement": ""},
        "0x030106000F":{"statusName": "TYRE_PRESSURE_SPARE_TYRE_DIFFERENCE", "unit_of_measurement": ""}
    }

    curReq = ""

    vwtokens = {}

    jar = ""
    vehicles = []
    vehicleData = {}
    vehicleRights = {}
    vehicleHomeRegions = {}
    vehicleStates = {}

    throttle_wait = 0

    statesArray = [
        {
            "url": "$homeregion/fs-car/bs/departuretimer/v1/$type/$country/vehicles/$vin/timer",
            "path": "timer",
            "element": "timer",
        },
        {
            "url": "$homeregion/fs-car/bs/climatisation/v1/$type/$country/vehicles/$vin/climater",
            "path": "climater",
            "element": "climater",
        },
        {
            "url": "$homeregion/fs-car/bs/cf/v1/$type/$country/vehicles/$vin/position",
            "path": "position",
            "element": "storedPositionResponse",
            "element2": "position",
            "element3": "findCarResponse",
            "element4": "Position",
        },
        {
            "url": "$homeregion/fs-car/bs/tripstatistics/v1/$type/$country/vehicles/$vin/tripdata/$tripType?type=list",
            "path": "tripdata",
            "element": "tripDataList",
        },
        {
            "url": "$homeregion/fs-car/bs/vsr/v1/$type/$country/vehicles/$vin/status",
            "path": "status",
            "element": "StoredVehicleDataResponse",
            "element2": "vehicleData",
        },
        {
            "url": "$homeregion/fs-car/destinationfeedservice/mydestinations/v1/$type/$country/vehicles/$vin/destinations",
            "path": "destinations",
            "element": "destinations",
        },
        {
            "url": "$homeregion/fs-car/bs/batterycharge/v1/$type/$country/vehicles/$vin/charger",
            "path": "charger",
            "element": "charger",
        },
        {
            "url": "$homeregion/fs-car/bs/rs/v1/$type/$country/vehicles/$vin/status",
            "path": "remoteStandheizung",
            "element": "statusResponse",
        },
        {
            "url": "$homeregion/fs-car/bs/dwap/v1/$type/$country/vehicles/$vin/history",
            "path": "history",
        },
    ]

    configured = []
    
    HEADERS = lambda self,x: {
                    "User-Agent": "okhttp/3.7.0",
                    #"content-type": "application/x-www-form-urlencoded",
                    "X-Client-Id": self.config["xClientId"],
                    "X-App-Version": self.config["xappversion"],
                    "X-App-Name": self.config["xappname"],
                    "Accept-charset": "UTF-8",
                    "Accept": "application/json,*/*",
                } if x == "SESSION" else {
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3',
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'User-Agent': 'Mozilla/5.0 (Linux; Android 6.0.1; D5803 Build/23.5.A.1.291; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/63.0.3239.111 Mobile Safari/537.36'
                    #"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 13_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
                    #"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    #"accept-language": "de-de",
                    #"Authorization": "Bearer " + self.vwtokens["atoken"],
                    #"acccept-encoding": "gzip, deflate, br"
                }
#    # Following does not work yet really, needs adjustments for Skoda
#    session_base = 'https://www.portal.volkswagen-we.com/'
#    landing_page_url = session_base + 'portal/en_GB/web/guest/home'
#    login_page_url = session_base + 'portal/web/guest/home/-/csrftokenhandling/get-login-url'
#    extract_csrf = lambda self,req: re.compile('<meta name="_csrf" content="([^"]*)"/>').search(req).group(1)




    async def login(self):
        ## Following does not work yet really, needs adjustments for Skoda
        #_LOGGER.info("Getting landing page (%s)" % self.landing_page_url)
        #landingPage = await self.execRequest({"url": self.landing_page_url})
        #csrf = self.extract_csrf(landingPage.text)
        #_LOGGER.info("CSRF: "+csrf)
        #auth_headers = self.HEADERS("AUTH").copy()
        #auth_headers["Referer"] = self.session_base + "portal"
        #loginPage = await self.execRequest({
        #    "url": self.login_page_url,
        #    "method": "POST",
        #    "headers": auth_headers
        #})
        #loginPath = loginPage.json().get("loginURL").get("path")
        ## extract client_id
        ##self.config["client_id"] = [r.split("=")[1] for r in re.split(r"[&?]", loginPath) if re.match(r"^client_id=.*", r)][0]

        _LOGGER.info("Getting openid config...")
        getconfig = (await self.execRequest({ # get configuration
                "url": "https://identity.vwgroup.io/.well-known/openid-configuration",
                "headers": {
                    # ":authority": "identity.vwgroup.io", # not sure why, this does not work
                    "user-agent": "OneConnect/200605002 CFNetwork/1128 Darwin/19.6.0",
                    "accept": "application/json;charset=utf-8",
                    "accept-language": "de-de",
                    "accept-encoding": "gzip, deflate, br"
                }
        })).json()
        _LOGGER.info("Done!")
        await self.getauth(getconfig)
        

    async def updateValues(self, mqttc):
        while True:
            for vin in self.vehicleStates.keys():
                try:
                    await self.getVehicleStatus(vin)
                except HTTPCodeException as e:
                    if(e.code == 401):
                        await self.login()
                        await self.getVehicleStatus(vin)
                        pass


            for vin,stateDict in self.vehicleStates.items():
                publishdict = {}
                mainjtopic = "skoda2mqtt/%s/JSTATE" % vin
                mainstopic = "skoda2mqtt/%s/JSTATE"% vin
                mainctopic = "homeassistant/sensor/skoda2mqtt/%s/config" % vin
                maincpayload = '{"state_topic": "%s","json_attributes_topic": "%s", "unique_id": "s2m_%s", "name": "S2M_%s", "value_template": "{{ value_json.GENERAL_STATUS }}" }' % (
                    mainstopic, mainjtopic,
                    vin,
                    vin
                )
                #mqttc.publish(
                #    mainctopic,
                #    maincpayload
                #)
                status = 2 # locked
                for stateId,state in stateDict.items():
                    if stateId in self.statusValues and state != "" and ("textId" not in state or stateId in self.statusValues and  not re.match(r".*(?:(?:(un)|(not_)supported)|(?:invalid)).*", state["textId"])):
                        if "textId" not in state or "." in state["textId"]:
                            state["textId"] = state["value"]
                        if "calc" in self.statusValues[stateId]:
                            state["value"] = self.statusValues[stateId]["calc"](state["value"])
                        _LOGGER.info("%s -> %s(%s)" %(self.statusValues[stateId]["statusName"], state["textId"], state["value"]))
                        stopic = "skoda2mqtt/%s_%s/STATE"% (vin, self.statusValues[stateId]["statusName"])
                        spayload = "%s(%s)" %(state["textId"], state["value"]) if state["textId"] != state["value"] else state["value"]
                        if stateId not in self.configured:
                            self.configured.append(stateId)
                            ctopic = "homeassistant/sensor/skoda2mqtt/%s_%s/config" % (vin, self.statusValues[stateId]["statusName"])
                            cpayload = {
                                "state_topic": stopic,
                                "unique_id": "s2m_%s_%s" %(vin, self.statusValues[stateId]["statusName"]),
                                "name": "s2m_%s_%s" % (vin, self.statusValues[stateId]["statusName"])
                            }

                            if "unit_of_measurement" in self.statusValues[stateId] and self.statusValues[stateId]["unit_of_measurement"] != "":
                                cpayload["unit_of_measurement"] = self.statusValues[stateId]["unit_of_measurement"]

                            mqttc.publish(ctopic, json.dumps(cpayload))
                        mqttc.publish(stopic, spayload)

                        publishdict[self.statusValues[stateId]["statusName"]] = {"value": state["value"], "textId": state["textId"]};
                        for sl in STATLIMITS:
                            if(re.match(sl["mask"], self.statusValues[stateId]["statusName"]) and sl["check"] != state["textId"]):
                                status = sl["fail"] if status > sl["fail"] else status
                publishdict["GENERAL_STATUS"] = ["open", "closed", "locked"][status]

                #mqttc.publish(
                #    mainjtopic,
                #    json.dumps(publishdict)
                #)

            await asyncio.sleep(60)

    async def getVehicleStatus(self, vin):
        url = await self.replaceVarInUrl("$homeregion/fs-car/bs/vsr/v1/$type/$country/vehicles/$vin/status", vin)
        accept = "application/json"
        r = (await self.execRequest({
            "url": url,
            "headers": {
                    "User-Agent": "okhttp/3.7.0",
                    "X-App-Version": self.config["xappversion"],
                    "X-App-Name": self.config["xappname"],
                    "Authorization": "Bearer " + self.vwtokens["atoken"],
                    "Accept-charset": "UTF-8",
                    "Accept": accept,
            }
        })).json()
        if "StoredVehicleDataResponse" not in r or "vehicleData" not in r["StoredVehicleDataResponse"] or "data" not in r["StoredVehicleDataResponse"]["vehicleData"]:
            return False
        self.vehicleStates[vin] = dict([(e["id"],e if "value" in e else "") for f in [s["field"] for s in r["StoredVehicleDataResponse"]["vehicleData"]["data"]] for e in f])



    async def getVehicleStatus_orig(self, vin, url, path, element, element2, element3, element4):
        url = await self.replaceVarInUrl(url, vin)
        accept = "application/json"
        try:
            r = await self.execRequest({
                "url": url,
                "headers": {
                    "User-Agent": "okhttp/3.7.0",
                    "X-App-Version": self.config["xappversion"],
                    "X-App-Name": self.config["xappname"],
                    "Authorization": "Bearer " + self.vwtokens["atoken"],
                    "Accept-charset": "UTF-8",
                    "Accept": accept,
                },
                "method": "GET"
            })
        except HTTPCodeException as e:
            if e.code == 403:
                return
            else:
                return
        if vin not in self.vehicleStates:
            self.vehicleStates[vin] = {}
        if path == "position":
            self.vehicleStates[vin]["position.isMoving"] = ( r.status_code == 204 )
        try:
            tst = r.json()
        except: 
            return
        result = r.json()
        
        if element and element in result:
            result = result[element]
        if element2 and element2 in result:
            result = result[element2]
        if element3 and element3 in result:
            result = result[element3]
        if element4 and element4 in result:
            result = result[element4]


        if path == "tripdata":
            if self.config["tripType"] == "none":
                return
            self.vehicleStates[vin][path+".lastTrip"] = result["tripData"]["length"]

        if result:
            for v in result:
                pass




        return r


    async def replaceVarInUrl(self, url, vin = ""):
        nurl = url
        for ce in self.config:
            nurl = re.sub("\$"+ce, self.config[ce], nurl)
        if vin != "":
            nurl = re.sub("\$vin", vin, nurl)
        return nurl

    async def getVehicleData(self,vin):
        url = await self.replaceVarInUrl("https://msg.volkswagen.de/fs-car/promoter/portfolio/v1/$type/$country/vehicle/$vin/carportdata", vin)
        r = await self.execRequest({
            "url": url,
            "method": "GET",
            "headers": {
                "User-Agent": "okhttp/3.7.0",
                "X-App-Version": self.config["xappversion"],
                "X-App-Name": self.config["xappname"],
                "X-Market": "de_DE",
                "Authorization": "Bearer " + self.vwtokens["atoken"],
                "Accept": "application/json",
            },
            "followAllRedirects": True
        })
        self.vehicleData[vin] = r.json()
        return r

    async def getVehicleRights(self,vin):
        url = "https://mal-1a.prd.ece.vwg-connect.com/api/rolesrights/operationlist/v3/vehicles/" + vin
        r = await self.execRequest({
            "url": url,
            "headers": {
                "User-Agent": "okhttp/3.7.0",
                "X-App-Version": self.config["xappversion"],
                "X-App-Name": self.config["xappname"],
                "Authorization": "Bearer " + self.vwtokens["atoken"],
                "Accept": "application/json, application/vnd.vwg.mbb.operationList_v3_0_2+xml, application/vnd.vwg.mbb.genericError_v1_0_2+xml"
            },
            "method": "GET",
            "followAllRedirects": True
        })
        self.vehicleRights[vin] = r.json()
        return r

    async def saveTokens(self):
        pass # TODO

    async def loadTokens(self):
        pass # TODO

    async def requestStatusUpdate(self, vin = ""):
        
        key = "skoda2mqtt.requestStatusUpdateTS"
        tsfile = Path(key)
        if tsfile.is_file():
            tsfile = open(key, "r")
            ts = int(tsfile.read())
            tsfile.close()
        else:
            ts = 0

        if ts + RSU_INTERVAL > int(time.time()): # RSU_INTERVAL not passed yet...
            return
        else:
            await store.async_save(int(time.time()))

        if self.throttle_wait > int(time.time()):
            return
        if vin == "":
            vin = self.vehicles[0]

        if vin not in self.vehicleHomeRegions:
            await self.getHomeRegion(vin)

        url = await self.replaceVarInUrl("$homeregion/fs-car/bs/vsr/v1/$type/$country/vehicles/%s/requests"% vin)
        accept = "application/json"
        try:
            r = await self.execRequest({
                "url": url,    
                "headers": {
                    "User-Agent": "okhttp/3.7.0",
                    "X-App-Version": self.config["xappversion"],
                    "X-App-Name": self.config["xappname"],
                    "Authorization": "Bearer " + self.vwtokens["atoken"],
                    "Accept": accept,
                    "Accept-charset": "UTF-8"
                    },
                "followAllRedirects": True,
                "method": "POST"
            })
            self.throttle_wait = int(time.time()) + 30*60
            return r;
        except VWThrottledException:
            self.throttle_wait = int(time.time()) + 30*60
            pass

    async def getHomeRegion(self, vin = ""):
        if vin == "":
            vin = self.vehicles[0]
        r = await self.execRequest({ 
            "url": "https://mal-1a.prd.ece.vwg-connect.com/api/cs/vds/v1/vehicles/%s/homeRegion" % vin, 
            "method": "GET", 
            "headers": {
                "user-agent": "okhttp/3.7.0", 
                "X-App-Version": self.config["xappversion"],
                "X-App-Name": self.config["xappname"],
                "Authorization": "Bearer " + self.vwtokens["atoken"], 
                "Accept":  "application/json"
            }, 
            "followAllRedirects": True
        })
        self.config["homeregion"] = r.json()['homeRegion']['baseUri']['content'].split("/api")[0].replace("mal-", "fal-") if r.json()['homeRegion']['baseUri']['content'] != "https://mal-1a.prd.ece.vwg-connect.com/api" else "https://msg.volkswagen.de"
        return r





    async def getVehicles(self):
        url = await self.replaceVarInUrl("https://msg.volkswagen.de/fs-car/usermanagement/users/v1/$type/$country/vehicles")
        headers = {
            "User-Agent": "okhttp/3.7.0",
            "X-App-Version": self.config["xappversion"],
            "X-App-Name": self.config["xappname"],
            "Authorization": "Bearer " + self.vwtokens["atoken"],
            "Accept": "application/json",
        }
        r = await self.execRequest({
            "url": url,
            "headers": headers,
            "allowRedirects": True
        })
        self.vehicles = r.json()['userVehicles']['vehicle']
        return r.json()

    async def getCodeChallenge(self):
        chash = ""
        result = ""
        while chash == "" or "+" in chash or "/" in chash or "=" in chash or "+" in result or "/" in result:
            chars = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
            result = ''.join(random.choice(chars) for x in range(64))
            result = re.sub(r"=", "", b64encode(result.encode()).decode("utf-8"))
            sha256 = hashlib.sha256()
            sha256.update(result.encode())
            chash = b64encode(sha256.digest()).decode("utf-8")[:-1]
        return(result, chash)

    async def getTokens(self, rurl, code_verifier = ""):
        hashArray = re.split(r'[?#]',rurl)[-1].split('&')
        tokens = {}
        for s in hashArray:
            ha = s.split('=')
            try:
                tokens[self.elems2tokens[ha[0]]] = ha[1]
            except KeyError:
                pass

        body = {
                "auth_code": tokens['jwtauth_code'],
                "id_token":  tokens['jwtid_token'],
                "brand": "skoda"
        }
        url = "https://tokenrefreshservice.apps.emea.vwapps.io/exchangeAuthCode"

        _LOGGER.info("Retrieving tokens...")
        r = await self.execRequest({
            "method": "POST",
            "url": url,
            "headers": {
                "X-App-version": self.config["xappversion"],
                "content-type": "application/x-www-form-urlencoded",
                "x-app-name": self.config["xappname"],
                "accept": "application/json"
            },
            "params": body
        })

        _LOGGER.info("Done!")
        vwtok = r.json()
        await self.getVWTokens(vwtok, tokens['jwtid_token'])
        return (r.json(), tokens)

    async def loopRefreshTokens(self):
        while True:
            #await asyncio.sleep(3600 * 0.9)
            await asyncio.sleep(20)
            #await self.refreshToken()



    async def refreshToken(self):
        url = "https://tokenrefreshservice.apps.emea.vwapps.io/refreshTokens"
        data = { 
            "refresh_token": self.vwtokens["rtoken"]
        }
        rtokens = (await self.execRequest({
            "url": url,
            "headers": self.HEADERS("session"),
            "method": "POST",
            "data": data,
            "followAllRedirects": True
        })).json()
        self.vwtokens["atoken"] = rtokens["access_token"]
        if "refresh_token" in rtokens:
            self.vwtokens["rtoken"] = rtokens["refresh_token"]






    async def getVWTokens(self, tokens, jwtid_token):

        self.vwtokens["atoken"] = tokens["access_token"]
        self.vwtokens["rtoken"] = tokens["refresh_token"]
        _LOGGER.info("Retrieving VW tokens...")
        r1 = await self.execRequest({
            "url": "https://mbboauth-1d.prd.ece.vwg-connect.com/mbbcoauth/mobile/oauth2/v1/token",
            "headers": {
                "User-Agent": "okhttp/3.7.0",
                "X-App-Version": self.config["xappversion"],
                "X-App-Name": self.config["xappname"],
                "X-Client-Id": self.config["xClientId"],
                "Host": "mbboauth-1d.prd.ece.vwg-connect.com",
            },
            "params": {
                "grant_type": "id_token",
                "token": jwtid_token,
                "scope": "sc2:fal",
            },
            "method": "POST"
        })
        _LOGGER.info("Done!")
        if r1.status_code < 400:
            rtokens = r1.json()
            self.vwtokens["atoken"] = rtokens["access_token"]
            self.vwtokens["rtoken"] = rtokens["refresh_token"]
            _LOGGER.info("Tokens OK")
        else:
            _LOGGER.info("Tokens wrong...")
            pass



    async def getNonce(self):
        ts = "%d" % (time.time())
        sha256 = hashlib.sha256()
        sha256.update(ts.encode())
        return (b64encode(sha256.digest()).decode("utf-8")[:-1])

    async def tokenize(self, url):
        return dict([(s.split('=')[0], s.split('=')[1]) for s in re.split(r"[&?]", url)[1:]])


    async def execRequest(self, req):
        loop = asyncio.get_running_loop()
        if (not "method" in req) or req["method"] == "GET":
            try:
                append = "?"+"&".join([k+"="+v for k,v in req["params"].items()]) if "params" in req and len(req["params"].keys())> 0 else ""
            except:
                _LOGGER.error(json.dumps(req))
                raise
            allowRedirects = req["allowRedirects"] if "allowRedirects" in req else True
            headers = req["headers"] if "headers" in req else {}
            
            r = await loop.run_in_executor(None,
                partial(
                    requests.get,
                    url = req["url"]+append,
                    headers = headers,
                    allow_redirects = allowRedirects,
                    cookies = self.jar
                )
            )
        else:
            data = req["params"] if "params" in req and len(req["params"].keys())> 0 else {}
            allowRedirects = req["allowRedirects"] if "allowRedirects" in req else True
            headers = req["headers"] if "headers" in req else {}
            r = await loop.run_in_executor(None,
                partial(
                    requests.post,
                    url = req["url"],
                    headers = headers,
                    allow_redirects = allowRedirects,
                    data = data,
                    cookies = self.jar
                )
            )
        if r.status_code == 429: # we are throttled
            raise VWThrottledException("Polling VW too fast, got throttled!")
        if r.status_code >= 400: # ignore successful and redirected codes
            raise HTTPCodeException("Got HTTP%d; %s"%(r.status_code, r.text), r.status_code)

        if self.jar != "":
            self.jar.update(r.cookies)
        else:
            self.jar = r.cookies
        #print(r.status_code)
        return r

    async def getauth(self,getconfig):
        _LOGGER.info("Getting authorization...")
        getauth = await self.execRequest({ # 
                "url": getconfig["authorization_endpoint"], # "authorization_endpoint" from configuration
                "params": {
                    "nonce": await self.getNonce(), # does not work in python-requests
                    "response_type":"code id_token token",
                    "scope": "openid mbb",
                    "ui_locales": "de",
                    "redirect_uri": "skodaconnect://oidc.login/",
                    "client_id": self.config["client_id"],
                    # clientid = "7f045eee-7003-4379-9968-9355ed2adb06%40apps_vw-dilab_com" # unbekannt
                    # client_id:"5b33753b-957a-4a35-a8b4-6a806880d009@apps_vw-dilab_com", # MySkoda?
                    # redirect_uri: "https://login.skoda-auto.com/ipexternal/identitykit/authenticationcallback", # MySkoda
                    #state: "fec823d30dda438b915d2f55934566a8",
                    "state": await self.getNonce() # questionable...
                },
                "headers": {
                    # ":authority:": "identity.vwgroup.io", # does not work in python-requests
                    "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 13_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "accept-language": "de-de",
                    "acccept-encoding": "gzip, deflate, br"
                },
                "method": "GET",
                "allow_redirects": True
        })
        _LOGGER.info("Done!")
        _LOGGER.info("Parsing authorization...")
        pqga = pyq(getauth.text)
        mailform = dict([(t.attrib["name"],t.attrib["value"]) for t in pqga("#emailPasswordForm").find("[type='hidden']")])
        mailform["email"] = self.config["email"]
        pe_url = getconfig["issuer"]+pqga("#emailPasswordForm")[0].attrib["action"]

        _LOGGER.info("Done!")
        await self.postemail(pe_url, mailform,getconfig,getauth)


    async def postemail(self,pe_url, mailform,getconfig,getauth):
        _LOGGER.info("Sending email form...")
        postemail = await self.execRequest( {
                "url": pe_url,
                "params": mailform,
                "headers": {
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.82",
                    "origin": getconfig["issuer"], # "issuer" from config
                    "accept-language": "de-de",
                    "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 13_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
                    "referer": getauth.url
                },
                "method": "POST"
        })

        _LOGGER.info("Done!")
        _LOGGER.info("Parsing email form response...")
        pqpe = pyq(postemail.text)
        pwform = dict([(t.attrib["name"],t.attrib["value"]) for t in pqpe("#credentialsForm").find("[type='hidden']")])
        pwform["password"] = self.config["password"]

        ppwurl = getconfig["issuer"]+pqpe("#credentialsForm")[0].attrib["action"]

        _LOGGER.info("Done!")
        await self.postpw(ppwurl, pwform, getconfig, postemail)
        

    async def postpw(self, ppwurl, pwform, getconfig, postemail):
        _LOGGER.info("Sending password form...")
        excepted = False
        try:
            postpw = await self.execRequest({
                "url": ppwurl,
                "params": pwform,
                "headers": {
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "origin": getconfig["issuer"], # "issuer" from config
                    "accept-language": "de-de",
                    "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 13_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
                    "accept-encoding":  "gzip, deflate, br",
                    "referer": postemail.url
                },
                "method": "POST",
                "allowRedirects": True
            })
        except InvalidSchema as e:
            skodaURL = re.sub(r".*'(skodaconnect.*)'.*", "\\1", str(e))
            excepted = True
            pass
        _LOGGER.info("Done!")
        

        if not excepted:
            raise Exception("We should have received an exception by now, so wtf?")
            #if postpw.status_code >= 400:
            #    raise

#        userId = (await self.tokenize(skodaURL))["userId"] if "userId" in (await self.tokenize(skodaURL)) else ""
#        _LOGGER.info("Sending authentication...")
#        while postpw.status_code == 302 and re.match(r"http", postpw.headers["Location"]):
#            postpw = await self.execRequest({
#                "url": postpw.headers["Location"],
#                "method": "GET",
#                "headers": {
#                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
#                    "origin": getconfig["issuer"], # "issuer" from config
#                    "accept-language": "de-de",
#                    "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 13_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
#                    "referer": postpw.url
#                },
#                "allowRedirects": False
#
#            })
#            userId = (await self.tokenize(postpw.headers["Location"]))["userId"] if "userId" in (await self.tokenize(postpw.headers["Location"])) and userId == "" else userId
        _LOGGER.info("Done!")
        await self.getTokens(skodaURL)


        
        
        

    def __init__(self, email, password):
        self.config = {
            "country": "CZ",
            "xappversion": "3.2.6",
            "xappname": "cz.skodaauto.connect",
            "xClientId": "28cd30c6-dee7-4529-a0e6-b1e07ff90b79",
            "client_id": "7f045eee-7003-4379-9968-9355ed2adb06%40apps_vw-dilab_com",
            "type": "skoda",
            "tripType": "none",
        }
        self.config["email"] = email
        self.config["password"] = password

    async def init(self):
        if len(self.vehicles) == 0:
            await self.login()
            v = (await self.getVehicles())['userVehicles']['vehicle']
            for car in self.vehicles:
                s = await self.getVehicleData(car)
    #            t = await self.getVehicleRights(car)
                hr = await self.getHomeRegion(car)
                rq = await self.getVehicleStatus(car)





if __name__ == "__main__":
    asyncio.run(main())

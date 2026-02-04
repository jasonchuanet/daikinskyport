''' Python Code for Communication with the Daikin Skyport Thermostat.  This is taken mostly from pyecobee, so much credit to those contributors'''
import requests
import json
import os
import logging
from time import sleep

from requests.exceptions import RequestException
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

from .const import (
    DAIKIN_PERCENT_MULTIPLIER,
    DAIKIN_HVAC_MODE_OFF,
    DAIKIN_HVAC_MODE_HEAT,
    DAIKIN_HVAC_MODE_COOL,
    DAIKIN_HVAC_MODE_AUTO,
    DAIKIN_HVAC_MODE_DRY,
    WALL_UNIT_FAN_MODE_VALUES,
    WALL_UNIT_FAN_MODE_DEFAULT,
)

logger = logging.getLogger('daikinskyport')

NEXT_SCHEDULE = 1

class ExpiredTokenError(Exception):
    """Raised when Daikin Skyport API returns a code indicating expired credentials."""

    pass

def config_from_file(filename, config=None):
    ''' Small configuration file management function'''
    if config:
        # We're writing configuration
        try:
            with open(filename, 'w') as fdesc:
                fdesc.write(json.dumps(config))
        except IOError as error:
            logger.exception(error)
            return False
        return True
    else:
        # We're reading config
        if os.path.isfile(filename):
            try:
                with open(filename, 'r') as fdesc:
                    return json.loads(fdesc.read())
            except IOError as error:
                return False
        else:
            return {}


class DaikinSkyport(object):
    ''' Class for storing Daikin Skyport Thermostats and Sensors '''

    def __init__(self, config_filename=None, user_email=None, user_password=None, config=None):
        self.thermostats = list()
        self.thermostatlist = list()
        self.authenticated = False
        self.skip_next = False

        if config is None:
            self.file_based_config = True
            if config_filename is None:
                if (user_email is None) or (user_password is None):
                    logger.error("Error. No user email or password was supplied.")
                    return
                jsonconfig = {"EMAIL": user_email, "PASSWORD": user_password}
                config_filename = 'daikinskyport.conf'
                config_from_file(config_filename, jsonconfig)
            config = config_from_file(config_filename)
        else:
            self.file_based_config = False
        if 'EMAIL' in config:
            self.user_email = config['EMAIL']
        else:
            logger.error("Email missing from config.")
        if 'PASSWORD' in config: # PASSWORD is only needed during first login
            self.user_password = config['PASSWORD']

        if 'ACCESS_TOKEN' in config:
            self.access_token = config['ACCESS_TOKEN']
        else:
            self.access_token = ''

        if 'REFRESH_TOKEN' in config:
            self.refresh_token = config['REFRESH_TOKEN']
        else:
            self.refresh_token = ''
#            self.request_tokens()
#            return

#        self.update()

    @staticmethod
    def _is_wall_unit(device_info: dict) -> bool:
        return device_info.get("adptSupportedEquipment") == "RA" or "iduOperatingMode" in device_info

    @staticmethod
    def _wall_unit_mode(device_info: dict) -> int:
        op_mode = device_info.get("iduOperatingMode")
        if device_info.get("iduOnOff") is False or op_mode == DAIKIN_HVAC_MODE_OFF:
            return DAIKIN_HVAC_MODE_OFF
        if op_mode in WALL_UNIT_FAN_MODE_VALUES:
            return WALL_UNIT_FAN_MODE_DEFAULT
        return {
            1: DAIKIN_HVAC_MODE_HEAT,
            2: DAIKIN_HVAC_MODE_COOL,
            3: DAIKIN_HVAC_MODE_AUTO,
            5: DAIKIN_HVAC_MODE_DRY,
        }.get(op_mode, DAIKIN_HVAC_MODE_OFF)

    @staticmethod
    def _wall_unit_fan_speed_key(hvac_mode: int) -> str | None:
        return {
            DAIKIN_HVAC_MODE_HEAT: "iduHeatFanSpeed",
            DAIKIN_HVAC_MODE_COOL: "iduCoolFanSpeed",
            DAIKIN_HVAC_MODE_AUTO: "iduAutoFanSpeed",
            DAIKIN_HVAC_MODE_DRY: "iduDryFanSpeed",
            WALL_UNIT_FAN_MODE_DEFAULT: "iduFanModeFanSpeed",
        }.get(hvac_mode)

    def _normalize_device_info(self, device_info: dict) -> dict:
        if not device_info:
            return device_info
        if self._is_wall_unit(device_info):
            device_info["device_type"] = "wall_unit"
            if "mode" not in device_info:
                device_info["mode"] = self._wall_unit_mode(device_info)
            op_mode = device_info.get("iduOperatingMode")
            if device_info.get("iduOnOff") is True and op_mode in WALL_UNIT_FAN_MODE_VALUES:
                device_info["wallUnitFanModeValue"] = op_mode
            else:
                device_info.setdefault("wallUnitFanModeValue", WALL_UNIT_FAN_MODE_DEFAULT)
            if "tempIndoor" not in device_info and "iduRoomTemp" in device_info:
                device_info["tempIndoor"] = device_info["iduRoomTemp"]
            if "cspActive" not in device_info and "iduCoolSetpoint" in device_info:
                device_info["cspActive"] = device_info["iduCoolSetpoint"]
            if "hspActive" not in device_info and "iduHeatSetpoint" in device_info:
                device_info["hspActive"] = device_info["iduHeatSetpoint"]
            if "cspHome" not in device_info and "cspActive" in device_info:
                device_info["cspHome"] = device_info["cspActive"]
            if "hspHome" not in device_info and "hspActive" in device_info:
                device_info["hspHome"] = device_info["hspActive"]
            device_info.setdefault("geofencingAway", False)
            device_info.setdefault("schedOverride", 0)
            device_info.setdefault("schedEnabled", False)
            device_info.setdefault("fanCirculate", 0)
            device_info.setdefault("fanCirculateSpeed", 0)
            device_info.setdefault("nightModeActive", False)
            device_info.setdefault("nightModeEnabled", False)
            device_info.setdefault("displayLockPIN", 0)
            device_info.setdefault("alertMediaAirFilterDays", 0)
            device_info.setdefault("statFirmware", device_info.get("adptSWversion"))
        return device_info

    def request_tokens(self):
        ''' Method to request API tokens from skyport '''
        url = 'https://api.daikinskyport.com/users/auth/login'
        header = {'Accept': 'application/json',
                  'Content-Type': 'application/json'}
        data = {"email": self.user_email, "password": self.user_password}
        try:
            request = requests.post(url, headers=header, json=data)
        except RequestException as e:
            logger.error("Error connecting to Daikin Skyport.  Possible connectivity outage."
                        "Could not request token. %s", e)
            return False
        if request.status_code == requests.codes.ok:
            json_data = request.json()
            self.access_token = json_data['accessToken']
            self.refresh_token = json_data['refreshToken']
            if self.refresh_token is None:
                logger.error("Auth did not return a refresh token.")
            else:
                if self.file_based_config:
                    self.write_tokens_to_file()
                return json_data
        else:
            logger.error('Error while requesting tokens from daikinskyport.com.'
                        ' Status code: %s Message: %s', request.status_code, request.text)
            return False

    def refresh_tokens(self):
        ''' Method to refresh API tokens from daikinskyport.com '''
        url = 'https://api.daikinskyport.com/users/auth/token'
        header = {'Accept': 'application/json',
                  'Content-Type': 'application/json'}
        data = {'email': self.user_email,
                  'refreshToken': self.refresh_token}
        request = requests.post(url, headers=header, json=data)
        if request.status_code == requests.codes.ok:
            json_data = request.json()
            self.access_token = json_data['accessToken']
            if self.file_based_config:
                self.write_tokens_to_file()
            return True
        else:
            logger.warn("Could not refresh tokens, Trying to re-request. Status code: %s Message: %s ", request.status_code, request.text)
            result = self.request_tokens()
            if result is not None:
                return True
            return False

    def get_thermostats(self):
        ''' Set self.thermostats to a json list of thermostats from daikinskyport.com '''
        url = 'https://api.daikinskyport.com/devices'
        header = {'Content-Type': 'application/json;charset=UTF-8',
                  'Authorization': 'Bearer ' + self.access_token}
        retry_strategy = Retry(total=8, backoff_factor=0.1)
        adapter = HTTPAdapter(max_retries=retry_strategy)
        http = requests.Session()
        http.mount("https://", adapter)
        http.mount("http://", adapter)

        try:
            request = http.get(url, headers=header)
        except RequestException as e:
            logger.warn("Error connecting to Daikin Skyport.  Possible connectivity outage: %s", e)
            return None
        if request.status_code == requests.codes.ok:
            self.authenticated = True
            self.thermostatlist = request.json()
            for thermostat in self.thermostatlist:
                overwrite = False
                thermostat_info = self.get_thermostat_info(thermostat['id'])
                if thermostat_info == None:
                    continue
                thermostat_info['name'] = thermostat['name']
                thermostat_info['id'] = thermostat['id']
                thermostat_info['model'] = thermostat['model']
                for index in range(len(self.thermostats)):
                    if thermostat['id'] == self.thermostats[index]['id']:
                        overwrite = True
                        self.thermostats[index] = thermostat_info
                if not overwrite:
                    self.thermostats.append(thermostat_info)
            return self.thermostats
        else:
            self.authenticated = False
            logger.debug("Error connecting to Daikin Skyport while attempting to get "
                        "thermostat data. Status code: %s Message: %s", request.status_code, request.text)
            raise ExpiredTokenError ("Daikin Skyport token expired")
            return None

    def get_thermostat_info(self, deviceid):
        ''' Retrieve the device info for the specific device '''
        url = 'https://api.daikinskyport.com/deviceData/' + deviceid
        header = {'Content-Type': 'application/json;charset=UTF-8',
                  'Authorization': 'Bearer ' + self.access_token}
        retry_strategy = Retry(total=8, backoff_factor=0.1)
        adapter = HTTPAdapter(max_retries=retry_strategy)
        http = requests.Session()
        http.mount("https://", adapter)
        http.mount("http://", adapter)

        try:
            request = http.get(url, headers=header)
            request.raise_for_status()
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 400 and e.response.json().get("message") == "DeviceOfflineException":
                logger.warn("Device is offline: %s", deviceid)
                self.authenticated = True
                return None
            else:
                self.authenticated = False
            logger.debug("Error connecting to Daikin Skyport while attempting to get "
                        "thermostat data. Status code: %s Message: %s", request.status_code, request.text)
            raise ExpiredTokenError ("Daikin Skyport token expired")
            return None
        if request.status_code == requests.codes.ok:
            self.authenticated = True
            return self._normalize_device_info(request.json())
        else:
            self.authenticated = False
            logger.debug("Error connecting to Daikin Skyport while attempting to get "
                        "thermostat data. Status code: %s Message: %s", request.status_code, request.text)
            raise ExpiredTokenError ("Daikin Skyport token expired")
            return None

    def get_thermostat(self, index):
        ''' Return a single thermostat based on index '''
        return self.thermostats[index]

    def get_sensors(self, index):
        ''' Return sensors based on index '''
        sensors = list()
        thermostat = self.thermostats[index]
        name = thermostat['name']
        if "tempOutdoor" in thermostat:
            sensors.append({"name": f"{name} Outdoor", "value": thermostat['tempOutdoor'], "type": "temperature"})
        if "humOutdoor" in thermostat:
            sensors.append({"name": f"{name} Outdoor", "value": thermostat['humOutdoor'], "type": "humidity"})
        if "ctOutdoorFanRequestedDemandPercentage" in thermostat:
            sensors.append({"name": f"{name} Outdoor fan", "value": round(thermostat['ctOutdoorFanRequestedDemandPercentage'] / DAIKIN_PERCENT_MULTIPLIER, 1), "type": "demand"})
        if "ctOutdoorHeatRequestedDemand" in thermostat:
            sensors.append({"name": f"{name} Outdoor heat pump", "value": round(thermostat['ctOutdoorHeatRequestedDemand'] / DAIKIN_PERCENT_MULTIPLIER, 1), "type": "demand"})
        if "ctOutdoorCoolRequestedDemand" in thermostat:
            sensors.append({"name": f"{name} Outdoor cooling", "value": round(thermostat['ctOutdoorCoolRequestedDemand'] / DAIKIN_PERCENT_MULTIPLIER, 1), "type": "demand"})
        if "ctOutdoorPower" in thermostat:
            sensors.append({"name": f"{name} Outdoor", "value": thermostat['ctOutdoorPower'] * 10, "type": "power"})
        if "ctOutdoorFrequencyInPercent" in thermostat:
            sensors.append({"name": f"{name} Outdoor", "value": round(thermostat['ctOutdoorFrequencyInPercent'] / DAIKIN_PERCENT_MULTIPLIER, 1), "type": "frequency_percent"})
        if ("oduConsumedPower" in thermostat and
            (thermostat.get("device_type") == "wall_unit" or
             thermostat.get("adptSupportedEquipment") == "RA")):
            sensors.append({"name": f"{name} Outdoor consumed", "value": thermostat["oduConsumedPower"], "type": "power"})
        if "tempIndoor" in thermostat:
            sensors.append({"name": f"{name} Indoor", "value": thermostat['tempIndoor'], "type": "temperature"})
        if "humIndoor" in thermostat:
            sensors.append({"name": f"{name} Indoor", "value": thermostat['humIndoor'], "type": "humidity"})
        if "oduOutdoorTemp" in thermostat and "tempOutdoor" not in thermostat:
            sensors.append({"name": f"{name} Outdoor", "value": thermostat['oduOutdoorTemp'], "type": "temperature"})
        if "ctIFCFanRequestedDemandPercent" in thermostat:
            sensors.append({"name": f"{name} Indoor fan", "value": round(thermostat['ctIFCFanRequestedDemandPercent'] / DAIKIN_PERCENT_MULTIPLIER, 1), "type": "demand"})
        if "ctIFCCurrentFanActualStatus" in thermostat:
            sensors.append({"name": f"{name} Indoor fan", "value": round(thermostat['ctIFCCurrentFanActualStatus'] / DAIKIN_PERCENT_MULTIPLIER, 1), "type": "actual_status"})
        if "ctIFCCoolRequestedDemandPercent" in thermostat:
            sensors.append({"name": f"{name} Indoor cooling", "value": round(thermostat['ctIFCCoolRequestedDemandPercent'] / DAIKIN_PERCENT_MULTIPLIER, 1), "type": "demand"})
        if "ctIFCCurrentCoolActualStatus" in thermostat:
            sensors.append({"name": f"{name} Indoor cooling", "value": round(thermostat['ctIFCCurrentCoolActualStatus'] / DAIKIN_PERCENT_MULTIPLIER, 1), "type": "actual_status"})
        if "ctIFCHeatRequestedDemandPercent" in thermostat:
            sensors.append({"name": f"{name} Indoor furnace", "value": round(thermostat['ctIFCHeatRequestedDemandPercent'] / DAIKIN_PERCENT_MULTIPLIER, 1), "type": "demand"})
        if "ctIFCCurrentHeatActualStatus" in thermostat:
            sensors.append({"name": f"{name} Indoor furnace", "value": round(thermostat['ctIFCCurrentHeatActualStatus'] / DAIKIN_PERCENT_MULTIPLIER, 1), "type": "actual_status"})
        if "ctIFCHumRequestedDemandPercent" in thermostat:
            sensors.append({"name": f"{name} Indoor humidifier", "value": round(thermostat['ctIFCHumRequestedDemandPercent'] / DAIKIN_PERCENT_MULTIPLIER, 1), "type": "demand"})
        if "ctIFCDehumRequestedDemandPercent" in thermostat:
            sensors.append({"name": f"{name} Indoor dehumidifier", "value": round(thermostat['ctIFCDehumRequestedDemandPercent'] / DAIKIN_PERCENT_MULTIPLIER, 1), "type": "demand"})
        if "ctOutdoorAirTemperature" in thermostat:
            sensors.append({"name": f"{name} Outdoor air", "value": round(((thermostat['ctOutdoorAirTemperature'] / 10) - 32) * 5 / 9, 1), "type": "temperature"})
        if "ctIFCIndoorBlowerAirflow" in thermostat:
            sensors.append({"name": f"{name} Indoor furnace blower", "value": thermostat['ctIFCIndoorBlowerAirflow'], "type": "airflow"})
        if "ctAHCurrentIndoorAirflow" in thermostat:
            sensors.append({"name": f"{name} Indoor air handler blower", "value": thermostat['ctAHCurrentIndoorAirflow'], "type": "airflow"})

        ''' if equipment is idle, set power to zero rather than accept bogus data '''
        equipment_status = thermostat.get('equipmentStatus')
        if equipment_status == 5:
            sensors.append({"name": f"{name} Indoor", "value": 0, "type": "power"})
        elif "ctIndoorPower" in thermostat:
            sensors.append({"name": f"{name} Indoor", "value": thermostat['ctIndoorPower'], "type": "power"})

        if ("oduIntPowerConsumption" in thermostat and
            (thermostat.get("device_type") == "wall_unit" or
             thermostat.get("adptSupportedEquipment") == "RA")):
            sensors.append({"name": f"{name} Outdoor", "value": thermostat['oduIntPowerConsumption'] * 10, "type": "energy"})

        if self.thermostats[index].get('aqOutdoorAvailable'):
            sensors.append({"name": f"{name} Outdoor", "value": thermostat['aqOutdoorParticles'], "type": "particle"})
            sensors.append({"name": f"{name} Outdoor", "value": thermostat['aqOutdoorValue'], "type": "score"})
            sensors.append({"name": f"{name} Outdoor", "value": round(thermostat['aqOutdoorOzone'] * 1.96), "type": "ozone"})
        if self.thermostats[index].get('aqIndoorAvailable'):
            sensors.append({"name": f"{name} Indoor", "value": thermostat['aqIndoorParticlesValue'], "type": "particle"})
            sensors.append({"name": f"{name} Indoor", "value": thermostat['aqIndoorValue'], "type": "score"})
            sensors.append({"name": f"{name} Indoor", "value": thermostat['aqIndoorVOCValue'], "type": "VOC"})

        fault_sensors = [
            ("ctAHCriticalFault", "Air Handler Critical Fault"),
            ("ctAHMinorFault", "Air Handler Minor Fault"),
            ("ctEEVCoilCriticalFault", "EEV Coil Critical Fault"),
            ("ctEEVCoilMinorFault", "EEV Coil Minor Fault"),
            ("ctIFCCriticalFault", "Indoor Furnace Critical Fault"),
            ("ctIFCMinorFault", "Indoor Furnace Minor Fault"),
            ("ctOutdoorCriticalFault", "Outdoor Critical Fault"),
            ("ctOutdoorMinorFault", "Outdoor Minor Fault"),
            ("ctStatCriticalFault", "Thermostat Critical Fault"),
            ("ctStatMinorFault", "Thermostat Minor Fault"),
        ]

        for fault_key, fault_name in fault_sensors:
            if fault_key in thermostat:
                sensors.append({"name": f"{name} {fault_name}", "value": thermostat[fault_key], "type": "fault_code"})

        return sensors

    def write_tokens_to_file(self):
        ''' Write api tokens to a file '''
        config = dict()
        config['ACCESS_TOKEN'] = self.access_token
        config['REFRESH_TOKEN'] = self.refresh_token
        config['EMAIL'] = self.user_email
        if self.file_based_config:
            config_from_file(self.config_filename, config)
        else:
            self.config = config

    def update(self):
        ''' Get new thermostat data from daikin skyport '''
        if self.skip_next:
            logger.debug("Skipping update due to setting change")
            self.skip_next = False
            return
        result = self.get_thermostats()
        return result

    def make_request(self, index, body, log_msg_action, *, retry_count=0):
        self.skip_next = True
        deviceID = self.thermostats[index]['id']
        url = 'https://api.daikinskyport.com/deviceData/' + deviceID
        header = {'Content-Type': 'application/json;charset=UTF-8',
                  'Authorization': 'Bearer ' + self.access_token}
        logger.debug("Make Request: %s, Device: %s, Body: %s", log_msg_action, deviceID, body)
        retry_strategy = Retry(total=8, backoff_factor=0.1,)
        adapter = HTTPAdapter(max_retries=retry_strategy)
        http = requests.Session()
        http.mount("https://", adapter)
        http.mount("http://", adapter)

        try:
            request = http.put(url, headers=header, json=body)
        except RequestException as e:
            logger.warn("Error connecting to Daikin Skyport.  Possible connectivity outage: %s", e)
            return None
        if request.status_code == requests.codes.ok:
            return request
        elif (request.status_code == 401 and retry_count == 0 and
              request.json()['error'] == 'authorization_expired'):
            if self.refresh_tokens():
                return self.make_request(body, deviceID, log_msg_action,
                                         retry_count=retry_count + 1)
        else:
            logger.warn(
                "Error fetching data from Daikin Skyport while attempting to %s: %s",
                log_msg_action, request.json())
            return None

    def set_hvac_mode(self, index, hvac_mode):
        ''' possible modes are DAIKIN_HVAC_MODE_{OFF,HEAT,COOL,AUTO,AUXHEAT} '''
        body = {"mode": hvac_mode}
        log_msg_action = "set HVAC mode"
        self.thermostats[index]["mode"] = hvac_mode
        return self.make_request(index, body, log_msg_action)

    def set_thermostat_schedule(self, index, prefix, start, enable, label, heating, cooling):
        ''' Schedule to set the thermostat.
        prefix is the beginning of the JSON key to modify.  It consists of "sched" + [Mon,Tue,Wed,Thu,Fri,Sat,Sun] + "Part" + [1:6] (ex. schedMonPart1)
        start is the beginning of the schedule.  It is an integer value where every 15 minutes from 00:00 is 1 (each hour = 4)
        enable is a boolean to set whether the schedule part is active or not
        label is a name for the part (ex. wakeup, work, etc.)
        heating is the heating set point for the part
        cooling is the cooling set point for the part'''
        body = {prefix + "Time": start,
                prefix + "Enabled": enable,
                prefix + "Label": label,
                prefix + "hsp": heating,
                prefix + "csp": cooling
                }

        log_msg_action = "set thermostat schedule"
        return self.make_request(index, body, log_msg_action)

    def set_fan_mode(self, index, fan_mode):
        ''' Set fan mode. Values: auto (0), schedule (2), on (1) '''
        body = {"fanCirculate": fan_mode}
        log_msg_action = "set fan mode"
        self.thermostats[index]["fanCirculate"] = fan_mode
        return self.make_request(index, body, log_msg_action)

    def set_fan_speed(self, index, fan_speed):
        ''' Set fan speed. Values: low (0), medium (1), high (2) '''
        body = {"fanCirculateSpeed": fan_speed}
        log_msg_action = "set fan speed"
        self.thermostats[index]["fanCirculateSpeed"] = fan_speed
        return self.make_request(index, body, log_msg_action)

    def set_fan_clean(self, index, active):
        ''' Enable/disable fan clean mode.  This runs the fan at high speed to clear out the air.
        active values are true/false'''
        body = {"oneCleanFanActive": active}
        log_msg_action = "set fan clean mode"
        return self.make_request(index, body, log_msg_action)

    def set_dual_fuel_efficiency(self, index, active):
        ''' Enable/disable dual fuel efficiency mode.  This disables the use of aux heat above -5.5C/22F.
        active values are true/false'''
        body = {"ctDualFuelFurnaceLockoutEnable": active}
        log_msg_action = "set dual fuel efficiency mode"
        return self.make_request(index, body, log_msg_action)

    def set_temp_hold(self, index, cool_temp=None, heat_temp=None,
                      hold_duration=None):
        ''' Set a temporary hold '''
        if hold_duration is None:
            hold_duration = self.thermostats[index]["schedOverrideDuration"]
        if cool_temp is None:
            cool_temp = self.thermostats[index]["cspHome"]
        if heat_temp is None:
            heat_temp = self.thermostats[index]["hspHome"]
        body = {"hspHome": round(heat_temp, 1),
                "cspHome": round(cool_temp, 1),
                "schedOverride": 1,
                "schedOverrideDuration": hold_duration
                }
        log_msg_action = "set hold temp"
        self.thermostats[index]["hspHome"] = round(heat_temp, 1)
        self.thermostats[index]["cspHome"] = round(cool_temp, 1)
        self.thermostats[index]["schedOverride"] = 1
        self.thermostats[index]["schedOverrideDuration"] = hold_duration
        return self.make_request(index, body, log_msg_action)

    def set_permanent_hold(self, index, cool_temp=None, heat_temp=None):
        ''' Set a climate hold - ie enable/disable schedule. 
        active values are true/false
        hold_duration is NEXT_SCHEDULE'''
        if cool_temp is None:
            cool_temp = self.thermostats[index]["cspHome"]
        if heat_temp is None:
            heat_temp = self.thermostats[index]["hspHome"]
        body = {"hspHome": round(heat_temp, 1),
                "cspHome": round(cool_temp, 1),
                "schedOverride": 0,
                "schedEnabled": False
                }
        log_msg_action = "set permanent hold"
        self.thermostats[index]["hspHome"] = round(heat_temp, 1)
        self.thermostats[index]["cspHome"] = round(cool_temp, 1)
        self.thermostats[index]["schedOverride"] = 0
        self.thermostats[index]["schedEnabled"] = False
        return self.make_request(index, body, log_msg_action)

    def set_away(self, index, mode, heat_temp=None, cool_temp=None):
        ''' Enable/Disable the away setting and optionally set the away temps '''
        if heat_temp is None:
            heat_temp = round(self.thermostats[index]["hspAway"], 1)
        if cool_temp is None:
            cool_temp = round(self.thermostats[index]["cspAway"], 1)
        body = {"geofencingAway": mode,
                "hspAway": heat_temp,
                "cspAway": cool_temp
                }

        log_msg_action = "set away mode"
        self.thermostats[index]["geofencingAway"] = mode
        self.thermostats[index]["hspAway"] = heat_temp
        self.thermostats[index]["cspAway"] = cool_temp
        return self.make_request(index, body, log_msg_action)

    def resume_program(self, index):
        ''' Resume currently scheduled program '''
        body = {"schedEnabled": True,
                "schedOverride": 0,
                "geofencingAway": False
                }

        log_msg_action = "resume program"
        return self.make_request(index, body, log_msg_action)

    def set_fan_schedule(self, index, start, stop, interval, speed):
        ''' Schedule to run the fan.  
        start_time is the beginning of the schedule per day.  It is an integer value where every 15 minutes from 00:00 is 1 (each hour = 4)
        end_time is the end of the schedule each day.  Values are same as start_time
        interval is the run time per hour of the schedule. Options are on the full time (0), 5mins (1), 15mins (2), 30mins (3), and 45mins (4) 
        speed is low (0) medium (1) or high (2)'''
        body = {"fanCirculateStart": start,
                "fanCirculateStop": stop,
                "fanCirculateDuration": interval,
                "fanCirculateSpeed": speed
                }

        log_msg_action = "set fan schedule"
        return self.make_request(index, body, log_msg_action)

    def set_night_mode(self, index, start, stop, enable):
        ''' Set the night mode parameters '''
        body = {"nightModeStart": start,
                "nightModeStop": stop,
                "nightModeEnabled": enable,
                }

        log_msg_action = "set night mode"
        return self.make_request(index, body, log_msg_action)

    def set_econo_mode(self, index, active):
        """Enable/disable Econo mode (wall units)."""
        body = {"iduEconoModeSetting": active}
        log_msg_action = "set econo mode"
        self.thermostats[index]["iduEconoModeSetting"] = active
        return self.make_request(index, body, log_msg_action)

    def set_boost_mode(self, index, active):
        """Enable/disable Boost mode (wall units)."""
        body = {"oduPowerfulOperationRequest": active}
        log_msg_action = "set boost mode"
        self.thermostats[index]["oduPowerfulOperationRequest"] = active
        return self.make_request(index, body, log_msg_action)

    def set_humidity(self, index, humidity_low=None, humidity_high=None):
        ''' Set humidity level'''
        if humidity_low is None:
            humidity_low = self.thermostats[index]["humSP"]
        if humidity_high is None:
            humidity_high = self.thermostats[index]["dehumSP"]
        body = {"dehumSP": humidity_high,
                "humSP": humidity_low
                }

        log_msg_action = "set humidity level"
        return self.make_request(index, body, log_msg_action)

    def set_wall_unit_mode(self, index, hvac_mode):
        """Set mode for wall-unit mini-split devices."""
        if hasattr(hvac_mode, "value"):
            hvac_mode = hvac_mode.value
        body = {}
        if hvac_mode in (DAIKIN_HVAC_MODE_OFF, "off"):
            body["iduOnOff"] = False
        else:
            body["iduOnOff"] = True
            mode_map = {
                DAIKIN_HVAC_MODE_HEAT: 1,
                "heat": 1,
                DAIKIN_HVAC_MODE_COOL: 2,
                "cool": 2,
                DAIKIN_HVAC_MODE_AUTO: 3,
                "auto": 3,
                DAIKIN_HVAC_MODE_DRY: 5,
                "dry": 5,
                "fan_only": WALL_UNIT_FAN_MODE_DEFAULT,
                WALL_UNIT_FAN_MODE_DEFAULT: WALL_UNIT_FAN_MODE_DEFAULT,
            }
            body["iduOperatingMode"] = mode_map.get(
                hvac_mode,
                self.thermostats[index].get("iduOperatingMode", DAIKIN_HVAC_MODE_AUTO),
            )
            self.thermostats[index]["iduOperatingMode"] = body["iduOperatingMode"]
            if body["iduOperatingMode"] in WALL_UNIT_FAN_MODE_VALUES:
                self.thermostats[index]["wallUnitFanModeValue"] = body["iduOperatingMode"]
            fan_key = self._wall_unit_fan_speed_key(body["iduOperatingMode"])
            if fan_key is not None and fan_key in self.thermostats[index]:
                body[fan_key] = self.thermostats[index][fan_key]
        self.thermostats[index]["iduOnOff"] = body.get("iduOnOff", True)
        self.thermostats[index]["mode"] = self._wall_unit_mode(self.thermostats[index])
        log_msg_action = "set wall unit mode"
        return self.make_request(index, body, log_msg_action)

    def set_wall_unit_fan_speed(self, index, fan_speed, hvac_mode=None):
        """Set fan speed for wall-unit mini-split devices."""
        if fan_speed is None:
            return None
        is_on = self.thermostats[index].get("iduOnOff") is not False
        if hvac_mode is None:
            hvac_mode = self.thermostats[index].get("iduOperatingMode")
        if hvac_mode == DAIKIN_HVAC_MODE_OFF and not is_on:
            return None
        fan_key = self._wall_unit_fan_speed_key(hvac_mode)
        if fan_key is None:
            return None
        body = {fan_key: fan_speed}
        self.thermostats[index][fan_key] = fan_speed
        log_msg_action = "set wall unit fan speed"
        return self.make_request(index, body, log_msg_action)

    def set_wall_unit_temperature(self, index, temperature, hvac_mode=None):
        """Set target temperature for wall-unit mini-split devices."""
        if temperature is None:
            return None
        # Wall units appear to accept only 0.5C increments.
        temperature = round(temperature * 2) / 2
        temperature = round(temperature, 1)
        if hvac_mode is None:
            hvac_mode = self.thermostats[index].get("mode")
        body = {}
        if "oduPowerfulOperationRequest" in self.thermostats[index]:
            body["oduPowerfulOperationRequest"] = bool(
                self.thermostats[index].get("oduPowerfulOperationRequest")
            )
        if hvac_mode == DAIKIN_HVAC_MODE_COOL:
            body["iduCoolSetpoint"] = temperature
            self.thermostats[index]["iduCoolSetpoint"] = temperature
        elif hvac_mode == DAIKIN_HVAC_MODE_HEAT:
            body["iduHeatSetpoint"] = temperature
            self.thermostats[index]["iduHeatSetpoint"] = temperature
        elif hvac_mode == DAIKIN_HVAC_MODE_AUTO:
            body["iduAutoSetpoint"] = temperature
            self.thermostats[index]["iduAutoSetpoint"] = temperature
        elif hvac_mode == DAIKIN_HVAC_MODE_DRY:
            return None
        if hvac_mode is not None:
            self.thermostats[index]["mode"] = hvac_mode
        log_msg_action = f"{body} set wall unit temperature"

        return self.make_request(index, body, log_msg_action)
    

"""Support for Daikin Skyport Thermostats."""
import collections
from datetime import datetime
from typing import Optional

import voluptuous as vol

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
    HVACAction
)
from homeassistant.components.climate.const import (
    ATTR_TARGET_TEMP_LOW,
    ATTR_TARGET_TEMP_HIGH,
    PRESET_AWAY,
    FAN_AUTO,
    FAN_ON,
    FAN_LOW,
    FAN_MEDIUM,
    FAN_HIGH,
    PRESET_NONE,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    PRECISION_HALVES,
    PRECISION_TENTHS,
    STATE_OFF,
    STATE_ON,
    UnitOfTemperature,
)

import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo

from . import DaikinSkyportData

from .const import (
    _LOGGER,
    DOMAIN,
    DAIKIN_HVAC_MODE_OFF,
    DAIKIN_HVAC_MODE_HEAT,
    DAIKIN_HVAC_MODE_COOL,
    DAIKIN_HVAC_MODE_AUTO,
    DAIKIN_HVAC_MODE_AUXHEAT,
    DAIKIN_HVAC_MODE_DRY,
    WALL_UNIT_FAN_MODE_DEFAULT,
    WALL_UNIT_FAN_MODE_VALUES,
    COORDINATOR,
)

WEEKDAY = [ "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

#Hold settings (manual mode)
HOLD_NEXT_TRANSITION = 0
HOLD_1HR = 60
HOLD_2HR = 120
HOLD_4HR = 240
HOLD_8HR = 480

#Preset values
PRESET_AWAY = "Away"
PRESET_SCHEDULE = "Schedule"
PRESET_MANUAL = "Manual"
PRESET_TEMP_HOLD = "Temp Hold"
PRESET_ECONO = "Econo"
PRESET_BOOST = "Boost"
FAN_SCHEDULE = "Schedule"

#Fan Schedule values
ATTR_FAN_START_TIME = "start_time"
ATTR_FAN_STOP_TIME = "end_time"
ATTR_FAN_INTERVAL = "interval"
ATTR_FAN_SPEED = "fan_speed"

#Night Mode values
ATTR_NIGHT_MODE_START_TIME = "start_time"
ATTR_NIGHT_MODE_END_TIME = "end_time"
ATTR_NIGHT_MODE_ENABLE = "enable"

#Schedule Adjustment values
ATTR_SCHEDULE_DAY = "day"
ATTR_SCHEDULE_START_TIME = "start_time"
ATTR_SCHEDULE_PART = "part"
ATTR_SCHEDULE_PART_ENABLED = "enable"
ATTR_SCHEDULE_PART_LABEL = "label"
ATTR_SCHEDULE_HEATING_SETPOINT = "heat_temp_setpoint"
ATTR_SCHEDULE_COOLING_SETPOINT = "cool_temp_setpoint"
ATTR_SCHEDULE_MODE = "mode" #Unknown what this does right now
ATTR_SCHEDULE_ACTION = "action" #Unknown what this does right now

#OneClean values
ATTR_ONECLEAN_ENABLED = "enable"

#Efficiency value
ATTR_EFFICIENCY_ENABLED = "enable"

# Order matters, because for reverse mapping we don't want to map HEAT to AUX
DAIKIN_HVAC_TO_HASS = collections.OrderedDict(
    [
        (DAIKIN_HVAC_MODE_HEAT, HVACMode.HEAT),
        (DAIKIN_HVAC_MODE_COOL, HVACMode.COOL),
        (DAIKIN_HVAC_MODE_AUTO, HVACMode.AUTO),
        (DAIKIN_HVAC_MODE_DRY, HVACMode.DRY),
        (DAIKIN_HVAC_MODE_OFF, HVACMode.OFF),
        (DAIKIN_HVAC_MODE_AUXHEAT, HVACMode.HEAT),
    ]
)

DAIKIN_FAN_TO_HASS = collections.OrderedDict(
    [
        (0, FAN_AUTO),
        (1, FAN_ON),
        (2, FAN_SCHEDULE),
        (3, FAN_LOW),
        (4, FAN_MEDIUM),
        (5, FAN_HIGH),
    ]
)

DAIKIN_FAN_SPEED_TO_HASS = collections.OrderedDict(
    [
        (0, FAN_LOW),
        (1, FAN_MEDIUM),
        (2, FAN_HIGH),
    ]
)

FAN_TO_DAIKIN_FAN = collections.OrderedDict(
    [
        (FAN_AUTO, 0),
        (FAN_ON, 1),
        (FAN_SCHEDULE, 2),
        (FAN_LOW, 0),
        (FAN_MEDIUM, 1),
        (FAN_HIGH, 2),
    ]
)

DAIKIN_HVAC_ACTION_TO_HASS = {
    # Map to None if we do not know how to represent.
    1: HVACAction.COOLING,
    3: HVACAction.HEATING,
    4: HVACAction.FAN,
    2: HVACAction.DRYING,
    5: HVACAction.IDLE,
}

def _is_wall_unit(thermostat: dict) -> bool:
    return (
        thermostat.get("device_type") == "wall_unit"
        or thermostat.get("adptSupportedEquipment") == "RA"
        or "iduOperatingMode" in thermostat
    )

PRESET_TO_DAIKIN_HOLD = {
    HOLD_NEXT_TRANSITION: 0,
    HOLD_1HR: 60,
    HOLD_2HR: 120,
    HOLD_4HR: 240,
    HOLD_8HR: 480
}

SERVICE_RESUME_PROGRAM = "daikin_resume_program"
SERVICE_SET_FAN_SCHEDULE = "daikin_set_fan_schedule"
SERVICE_SET_NIGHT_MODE = "daikin_set_night_mode"
SERVICE_SET_THERMOSTAT_SCHEDULE = "daikin_set_thermostat_schedule"
SERVICE_SET_ONECLEAN = "daikin_set_oneclean"
SERVICE_PRIORITIZE_EFFICIENCY = "daikin_prioritize_efficiency"

RESUME_PROGRAM_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids
    }
)

FAN_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Optional(ATTR_FAN_START_TIME): cv.positive_int,
        vol.Optional(ATTR_FAN_STOP_TIME): cv.positive_int,
        vol.Optional(ATTR_FAN_INTERVAL): cv.positive_int,
        vol.Optional(ATTR_FAN_SPEED): cv.positive_int
    }
)

NIGHT_MODE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Optional(ATTR_NIGHT_MODE_START_TIME): cv.positive_int,
        vol.Optional(ATTR_NIGHT_MODE_END_TIME): cv.positive_int,
        vol.Optional(ATTR_NIGHT_MODE_ENABLE): cv.boolean,
    }
)

THERMOSTAT_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Optional(ATTR_SCHEDULE_DAY): cv.string,
        vol.Optional(ATTR_SCHEDULE_START_TIME): cv.positive_int,
        vol.Optional(ATTR_SCHEDULE_PART): cv.positive_int,
        vol.Optional(ATTR_SCHEDULE_PART_ENABLED): cv.boolean,
        vol.Optional(ATTR_SCHEDULE_PART_LABEL): cv.string,
        vol.Optional(ATTR_SCHEDULE_HEATING_SETPOINT): cv.positive_int,
        vol.Optional(ATTR_SCHEDULE_COOLING_SETPOINT): cv.positive_int,
    }
)

ONECLEAN_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Optional(ATTR_ONECLEAN_ENABLED): cv.boolean,
    }
)

EFFICIENCY_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Optional(ATTR_EFFICIENCY_ENABLED): cv.boolean,
    }
)

SUPPORT_FLAGS = (
    ClimateEntityFeature.TARGET_TEMPERATURE
    | ClimateEntityFeature.PRESET_MODE
    | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
    | ClimateEntityFeature.FAN_MODE
    | ClimateEntityFeature.TURN_ON
    | ClimateEntityFeature.TURN_OFF
)

WALL_UNIT_FAN_SPEED_TO_HASS = {
    10: "auto",
    11: "quiet",
    3: "low",
    4: "medium_low",
    5: "medium",
    6: "medium_high",
    7: "high",
}
WALL_UNIT_FAN_SPEED_ORDER = [10, 11, 3, 4, 5, 6, 7]
WALL_UNIT_FAN_SPEED_FROM_HASS = {value: key for key, value in WALL_UNIT_FAN_SPEED_TO_HASS.items()}

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add a Daikin Skyport Climate entity from a config_entry."""

    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: DaikinSkyportData = data[COORDINATOR]
    entities = []

    for index in range(len(coordinator.daikinskyport.thermostats)):
        thermostat = coordinator.daikinskyport.get_thermostat(index)
        entities.append(Thermostat(coordinator, index, thermostat))
    
    async_add_entities(entities, True)

    def resume_program_set_service(service: ServiceCall) -> None:
        """Resume the schedule on the target thermostats."""
        entity_ids = service.data[ATTR_ENTITY_ID]

        _LOGGER.info("Resuming program for %s", entity_ids)
        
        for entity in entity_ids:
            for thermostat in entities:
                if thermostat.entity_id == entity:
                    thermostat.resume_program()
                    _LOGGER.info("Program resumed for %s", entity)
                    thermostat.schedule_update_ha_state(True)
                    break

    def set_fan_schedule_service(service):
        """Set the fan schedule on the target thermostats."""
        
        start = service.data.get(ATTR_FAN_START_TIME)
        stop = service.data.get(ATTR_FAN_STOP_TIME)
        interval = service.data.get(ATTR_FAN_INTERVAL)
        speed = service.data.get(ATTR_FAN_SPEED)

        entity_ids = service.data[ATTR_ENTITY_ID]

        _LOGGER.info("Setting fan schedule for %s", entity_ids)
        
        for entity in entity_ids:
            for thermostat in entities:
                if thermostat.entity_id == entity:
                    thermostat.set_fan_schedule(start, stop, interval, speed)
                    _LOGGER.info("Fan schedule set for %s", entity)
                    thermostat.schedule_update_ha_state(True)
                    break

    def set_night_mode_service(service):
        """Set night mode on the target thermostats."""
        
        start = service.data.get(ATTR_NIGHT_MODE_START_TIME)
        stop = service.data.get(ATTR_NIGHT_MODE_END_TIME)
        enable = service.data.get(ATTR_NIGHT_MODE_ENABLE)

        entity_ids = service.data[ATTR_ENTITY_ID]

        _LOGGER.info("Setting night mode for %s", entity_ids)
        
        for entity in entity_ids:
            for thermostat in entities:
                if thermostat.entity_id == entity:
                    thermostat.set_night_mode(start, stop, enable)
                    _LOGGER.info("Night mode set for %s", entity)
                    thermostat.schedule_update_ha_state(True)
                    break

    def set_thermostat_schedule_service(service):
        """Set the thermostat schedule on the target thermostats."""
        day = service.data.get(ATTR_SCHEDULE_DAY)
        start = service.data.get(ATTR_SCHEDULE_START_TIME)
        part = service.data.get(ATTR_SCHEDULE_PART)
        enable = service.data.get(ATTR_SCHEDULE_PART_ENABLED)
        label = service.data.get(ATTR_SCHEDULE_PART_LABEL)
        heating = service.data.get(ATTR_SCHEDULE_HEATING_SETPOINT)
        cooling = service.data.get(ATTR_SCHEDULE_COOLING_SETPOINT)

        entity_ids = service.data[ATTR_ENTITY_ID]

        _LOGGER.info("Setting thermostat schedule for %s", entity_ids)
        
        for entity in entity_ids:
            for thermostat in entities:
                if thermostat.entity_id == entity:
                    thermostat.set_thermostat_schedule(day, start, part, enable, label, heating, cooling)
                    _LOGGER.info("Thermostat schedule set for %s", entity)
                    thermostat.schedule_update_ha_state(True)
                    break

    def set_oneclean_service(service):
        """Enable/disable OneClean."""
        enable = service.data.get(ATTR_ONECLEAN_ENABLED)

        entity_ids = service.data[ATTR_ENTITY_ID]

        _LOGGER.info("Setting OneClean for %s", entity_ids)
        
        for entity in entity_ids:
            for thermostat in entities:
                if thermostat.entity_id == entity:
                    thermostat.set_oneclean(enable)
                    _LOGGER.info("OneClean set for %s", entity)
                    thermostat.schedule_update_ha_state(True)
                    break

    def set_efficiency_service(service):
        """Enable/disable heat pump efficiency."""
        enable = service.data.get(ATTR_EFFICIENCY_ENABLED)

        entity_ids = service.data[ATTR_ENTITY_ID]

        _LOGGER.info("Setting efficiency for %s", entity_ids)
        
        for entity in entity_ids:
            for thermostat in entities:
                if thermostat.entity_id == entity:
                    thermostat.set_efficiency(enable)
                    _LOGGER.info("Efficiency set for %s", entity)
                    thermostat.schedule_update_ha_state(True)
                    break

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESUME_PROGRAM,
        resume_program_set_service,
        schema=RESUME_PROGRAM_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_FAN_SCHEDULE,
        set_fan_schedule_service,
        schema=FAN_SCHEDULE_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_NIGHT_MODE,
        set_night_mode_service,
        schema=NIGHT_MODE_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_THERMOSTAT_SCHEDULE,
        set_thermostat_schedule_service,
        schema=THERMOSTAT_SCHEDULE_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_ONECLEAN,
        set_oneclean_service,
        schema=ONECLEAN_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_PRIORITIZE_EFFICIENCY,
        set_efficiency_service,
        schema=EFFICIENCY_SCHEMA,
    )

class Thermostat(ClimateEntity):
    """A thermostat class for Daikin Skyport Thermostats."""

    _attr_precision = PRECISION_TENTHS
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_fan_modes = [FAN_AUTO, FAN_ON, FAN_LOW, FAN_MEDIUM, FAN_HIGH, FAN_SCHEDULE]
    _attr_name = None
    _attr_has_entity_name = True
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, data, thermostat_index, thermostat):
        """Initialize the thermostat."""
        self.data = data
        self.thermostat_index = thermostat_index
        self.thermostat = thermostat
        self._is_wall_unit = _is_wall_unit(self.thermostat)
        self._name = self.thermostat["name"]
        self._attr_unique_id = f"{self.thermostat['id']}-climate"
        self._cool_setpoint = None
        self._heat_setpoint = None
        self._target_temp = None
        self._hvac_mode = HVACMode.OFF
        self._fan_mode = None
        self._fan_speed = None
        self._base_supported_features = SUPPORT_FLAGS
        self._supported_features = SUPPORT_FLAGS
        self._operation_list = []
        self._preset_modes = set()
        self._fan_modes = []
        self.update_without_throttle = False
        self._configure_capabilities()
        self._apply_thermostat_state()

    def _configure_capabilities(self) -> None:
        if self._is_wall_unit:
            self._base_supported_features = (
                ClimateEntityFeature.TARGET_TEMPERATURE
                | ClimateEntityFeature.FAN_MODE
                | ClimateEntityFeature.PRESET_MODE
            )
            self._supported_features = self._base_supported_features
            self._attr_precision = PRECISION_HALVES
            self._operation_list = self._build_wall_unit_operation_list()
            self._preset_modes = {
                PRESET_ECONO,
                PRESET_NONE,
            }
            if "oduPowerfulOperationRequest" in self.thermostat:
                self._preset_modes.add(PRESET_BOOST)
            self._fan_modes = []
        else:
            self._base_supported_features = SUPPORT_FLAGS
            self._supported_features = self._base_supported_features
            self._attr_precision = PRECISION_TENTHS
            self._operation_list = []
            if self.thermostat.get("ctSystemCapHeat"):
                self._operation_list.append(HVACMode.HEAT)
            if (("ctOutdoorNoofCoolStages" in self.thermostat and self.thermostat["ctOutdoorNoofCoolStages"] > 0)
            or  ("P1P2S21CoolingCapability" in self.thermostat and self.thermostat["P1P2S21CoolingCapability"] is True)):
                self._operation_list.append(HVACMode.COOL)
            if len(self._operation_list) == 2:
                self._operation_list.insert(0, HVACMode.AUTO)
            self._operation_list.append(HVACMode.OFF)
            self._preset_modes = {
                PRESET_SCHEDULE,
                PRESET_MANUAL,
                PRESET_TEMP_HOLD,
                PRESET_AWAY,
            }
            self._fan_modes = [FAN_AUTO, FAN_ON, FAN_LOW, FAN_MEDIUM, FAN_HIGH, FAN_SCHEDULE]

    def _is_dry_mode(self) -> bool:
        return self.hvac_mode == HVACMode.DRY

    def _refresh_supported_features_for_mode(self) -> None:
        """Adjust supported features based on the active HVAC mode."""
        self._supported_features = self._base_supported_features
        if not self._fan_modes:
            self._supported_features &= ~ClimateEntityFeature.FAN_MODE
        if self._is_wall_unit and self._is_dry_mode():
            self._supported_features &= ~ClimateEntityFeature.TARGET_TEMPERATURE
            self._supported_features &= ~ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
            self._supported_features &= ~ClimateEntityFeature.FAN_MODE

    def _build_wall_unit_operation_list(self) -> list[HVACMode]:
        modes: list[HVACMode] = []
        if "iduHeatSetpoint" in self.thermostat:
            modes.append(HVACMode.HEAT)
        if "iduCoolSetpoint" in self.thermostat:
            modes.append(HVACMode.COOL)
        if "iduAutoSetpoint" in self.thermostat:
            modes.insert(0, HVACMode.AUTO)
        if "iduDryFanSpeed" in self.thermostat or self.thermostat.get("iduOperatingMode") == 5:
            modes.append(HVACMode.DRY)
        if (
            "iduFanModeFanSpeed" in self.thermostat
            or self.thermostat.get("iduOperatingMode") in WALL_UNIT_FAN_MODE_VALUES
        ):
            modes.append(HVACMode.FAN_ONLY)
        if HVACMode.OFF not in modes:
            modes.append(HVACMode.OFF)
        return modes

    def _wall_unit_hvac_mode(self) -> HVACMode:
        daikin_mode = self.thermostat.get("mode")
        if daikin_mode is None:
            daikin_mode = self.thermostat.get("iduOperatingMode")
        if self.thermostat.get("iduOnOff") is False:
            return HVACMode.OFF
        if daikin_mode in WALL_UNIT_FAN_MODE_VALUES:
            return HVACMode.FAN_ONLY
        return DAIKIN_HVAC_TO_HASS.get(daikin_mode, HVACMode.OFF)

    def _wall_unit_operating_mode(self) -> HVACMode | None:
        op_mode = self.thermostat.get("iduOperatingMode")
        if op_mode in WALL_UNIT_FAN_MODE_VALUES:
            return HVACMode.FAN_ONLY
        if op_mode == 1:
            return HVACMode.HEAT
        if op_mode == 2:
            return HVACMode.COOL
        if op_mode == 3:
            return HVACMode.AUTO
        if op_mode == 5:
            return HVACMode.DRY
        return None

    def _wall_unit_fan_speed_key(self, hvac_mode: HVACMode) -> str | None:
        return {
            HVACMode.HEAT: "iduHeatFanSpeed",
            HVACMode.COOL: "iduCoolFanSpeed",
            HVACMode.AUTO: "iduAutoFanSpeed",
            HVACMode.DRY: "iduDryFanSpeed",
            HVACMode.FAN_ONLY: "iduFanModeFanSpeed",
        }.get(hvac_mode)

    def _wall_unit_fan_speed(self) -> int | None:
        hvac_mode = self.hvac_mode
        if hvac_mode == HVACMode.OFF:
            hvac_mode = self._wall_unit_operating_mode()
        if hvac_mode is None:
            return None
        fan_key = self._wall_unit_fan_speed_key(hvac_mode)
        if fan_key is None:
            return None
        return self.thermostat.get(fan_key)

    def _apply_thermostat_state(self) -> None:
        if self._is_wall_unit:
            self._hvac_mode = self._wall_unit_hvac_mode()
            self._cool_setpoint = self.thermostat.get("iduCoolSetpoint", self.thermostat.get("cspActive"))
            self._heat_setpoint = self.thermostat.get("iduHeatSetpoint", self.thermostat.get("hspActive"))
            self._target_temp = self.thermostat.get("iduTargetTemp")
            if self._target_temp is None:
                if self._hvac_mode == HVACMode.HEAT:
                    self._target_temp = self._heat_setpoint
                elif self._hvac_mode == HVACMode.COOL:
                    self._target_temp = self._cool_setpoint
                else:
                    self._target_temp = self.thermostat.get("iduAutoSetpoint", self._cool_setpoint or self._heat_setpoint)
            if self.thermostat.get("oduPowerfulOperationRequest"):
                self._preset_mode = PRESET_BOOST
            elif self.thermostat.get("iduEconoModeSetting"):
                self._preset_mode = PRESET_ECONO
            else:
                self._preset_mode = PRESET_NONE
            fan_speed = self._wall_unit_fan_speed()
            if fan_speed is not None:
                self._fan_mode = WALL_UNIT_FAN_SPEED_TO_HASS.get(fan_speed, str(fan_speed))
            else:
                self._fan_mode = None
            fan_values: list[int] = []
            for key in (
                "iduHeatFanSpeed",
                "iduCoolFanSpeed",
                "iduAutoFanSpeed",
                "iduDryFanSpeed",
                "iduFanModeFanSpeed",
            ):
                value = self.thermostat.get(key)
                if isinstance(value, (int, float)):
                    fan_values.append(value)
            fan_values_set = set(int(value) for value in fan_values)
            fan_modes: list[str] = []
            if "iduFanModeFanSpeed" in self.thermostat:
                fan_modes = [WALL_UNIT_FAN_SPEED_TO_HASS[value] for value in WALL_UNIT_FAN_SPEED_ORDER]
            else:
                for value in WALL_UNIT_FAN_SPEED_ORDER:
                    if value in fan_values_set:
                        fan_modes.append(WALL_UNIT_FAN_SPEED_TO_HASS.get(value, str(value)))
            unknown_values = sorted(v for v in fan_values_set if v not in WALL_UNIT_FAN_SPEED_TO_HASS)
            fan_modes.extend(str(value) for value in unknown_values)
            self._fan_modes = fan_modes
            if self._is_dry_mode():
                self._fan_modes = []
                self._fan_mode = None
            self._refresh_supported_features_for_mode()
            self._fan_speed = None
            return

        self._cool_setpoint = self.thermostat.get("cspActive")
        self._heat_setpoint = self.thermostat.get("hspActive")
        daikin_mode = self.thermostat.get("mode", DAIKIN_HVAC_MODE_OFF)
        self._hvac_mode = DAIKIN_HVAC_TO_HASS.get(daikin_mode, HVACMode.OFF)
        fan_circulate = self.thermostat.get("fanCirculate", 0)
        fan_speed = self.thermostat.get("fanCirculateSpeed", 0)
        if DAIKIN_FAN_TO_HASS.get(fan_circulate) == FAN_ON:
            self._fan_mode = DAIKIN_FAN_TO_HASS.get(fan_speed + 3, FAN_ON)
        else:
            self._fan_mode = DAIKIN_FAN_TO_HASS.get(fan_circulate, FAN_AUTO)
        self._fan_speed = DAIKIN_FAN_SPEED_TO_HASS.get(fan_speed, FAN_LOW)
        if self.thermostat.get("geofencingAway"):
            self._preset_mode = PRESET_AWAY
        elif self.thermostat.get("schedOverride") == 1:
            self._preset_mode = PRESET_TEMP_HOLD
        elif self.thermostat.get("schedEnabled"):
            self._preset_mode = PRESET_SCHEDULE
        else:
            self._preset_mode = PRESET_MANUAL
        if self._is_wall_unit and self._is_dry_mode():
            self._fan_mode = None
            self._fan_speed = None
        self._refresh_supported_features_for_mode()

    def _daikin_mode_from_hass(self, hvac_mode: HVACMode) -> int:
        return next(
            (k for k, v in DAIKIN_HVAC_TO_HASS.items() if v == hvac_mode),
            DAIKIN_HVAC_MODE_OFF,
        )

    async def async_update(self):
        """Get the latest state from the thermostat."""
        if self.update_without_throttle:
            await self.data._async_update_data(no_throttle=True)
            self.update_without_throttle = False
        else:
            await self.data._async_update_data()

        self.thermostat = self.data.daikinskyport.get_thermostat(self.thermostat_index)
        self._is_wall_unit = _is_wall_unit(self.thermostat)
        self._configure_capabilities()
        self._apply_thermostat_state()

    @property
    def device_info(self) -> DeviceInfo:
        return self.data.device_info

    @property
    def available(self):
        """Return if device is available."""
        return True #TBD: Need to determine how to tell if the thermostat is available or not

    @property
    def supported_features(self):
        """Return the list of supported features."""
        return self._supported_features

    @property
    def name(self):
        """Return the name of the Daikin Thermostat."""
        return self.thermostat["name"]

    @property
    def current_temperature(self) -> float:
        """Return the current temperature."""
        return self.thermostat.get("tempIndoor", self.thermostat.get("iduRoomTemp"))

    @property
    def target_temperature_low(self):
        """Return the lower bound temperature we try to reach."""
        if self._is_wall_unit:
            return None
        if self.hvac_mode == HVACMode.AUTO:
            return self._heat_setpoint
        return None

    @property
    def target_temperature_high(self):
        """Return the upper bound temperature we try to reach."""
        if self._is_wall_unit:
            return None
        if self.hvac_mode == HVACMode.AUTO:
            return self._cool_setpoint
        return None

    @property
    def target_temperature(self):
        """Return the temperature we try to reach."""
        if self._is_wall_unit:
            if self._is_dry_mode():
                return None
            return self._target_temp
        if self.hvac_mode == HVACMode.AUTO:
            return None
        if self.hvac_mode == HVACMode.HEAT:
            return self._heat_setpoint
        if self.hvac_mode == HVACMode.COOL:
            return self._cool_setpoint
        return None

    @property
    def fan(self):
        """Return the current fan status."""
        if "ctAHFanCurrentDemandStatus" in self.thermostat and self.thermostat["ctAHFanCurrentDemandStatus"] > 0:
            return STATE_ON
        return HVACMode.OFF

    @property
    def fan_mode(self):
        """Return the fan setting."""
        return self._fan_mode

    @property
    def fan_speed(self):
        """Return the fan setting."""
        return self._fan_speed

    @property
    def fan_modes(self):
        """Return the available fan modes."""
        return self._fan_modes

    @property
    def preset_mode(self):
        """Return current preset mode."""
        return self._preset_mode

    @property
    def hvac_mode(self):
        """Return current operation."""
        return self._hvac_mode

    @property
    def hvac_modes(self):
        """Return the operation modes list."""
        return self._operation_list

    @property
    def current_humidity(self) -> Optional[int]:
        """Return the current humidity."""
        return self.thermostat.get("humIndoor")

    @property
    def hvac_action(self):
        """Return current HVAC action."""
        status = self.thermostat.get("equipmentStatus")
        if status in DAIKIN_HVAC_ACTION_TO_HASS:
            return DAIKIN_HVAC_ACTION_TO_HASS[status]
        if self._is_wall_unit:
            if self.hvac_mode == HVACMode.OFF or self.thermostat.get("iduOnOff") is False:
                return HVACAction.OFF
            if "iduThermoState" in self.thermostat:
                if self.thermostat.get("iduThermoState") is False:
                    return HVACAction.IDLE
                if self.hvac_mode == HVACMode.HEAT:
                    return HVACAction.HEATING
                if self.hvac_mode == HVACMode.COOL:
                    return HVACAction.COOLING
                if self.hvac_mode == HVACMode.DRY:
                    return HVACAction.DRYING
                if self.hvac_mode == HVACMode.FAN_ONLY:
                    return HVACAction.FAN
                return HVACAction.IDLE
            if self.hvac_mode == HVACMode.HEAT:
                return HVACAction.HEATING
            if self.hvac_mode == HVACMode.COOL:
                return HVACAction.COOLING
            if self.hvac_mode == HVACMode.DRY:
                return HVACAction.DRYING
            if self.hvac_mode == HVACMode.FAN_ONLY:
                return HVACAction.FAN
            return HVACAction.IDLE
        return None

    @property
    def extra_state_attributes(self):
        """Return device specific state attributes."""
        fan_cfm = "Unavailable"
        fan_demand = "Unavailable"
        cooling_demand = "Unavailable"
        heating_demand = "Unavailable"
        heatpump_demand = "Unavailable"
        dehumidification_demand = "Unavailable"
        humidification_demand = "Unavailable"
        indoor_mode = "Unavailable"

        if "ctAHCurrentIndoorAirflow" in self.thermostat: 
            if self.thermostat["ctAHCurrentIndoorAirflow"] == 65535:
                fan_cfm = self.thermostat["ctIFCIndoorBlowerAirflow"]
            else:
                fan_cfm = self.thermostat["ctAHCurrentIndoorAirflow"]
        
        if "ctAHFanCurrentDemandStatus" in self.thermostat:
            fan_demand = round(self.thermostat["ctAHFanCurrentDemandStatus"] / 2, 1)
            
        if "ctOutdoorCoolRequestedDemand" in self.thermostat:
            cooling_demand = round(self.thermostat["ctOutdoorCoolRequestedDemand"] / 2, 1)

        if "ctAHHeatRequestedDemand" in self.thermostat:
            heating_demand = round(self.thermostat["ctAHHeatRequestedDemand"] / 2, 1)

        if "ctOutdoorHeatRequestedDemand" in self.thermostat:
            heatpump_demand = round(self.thermostat["ctOutdoorHeatRequestedDemand"] / 2, 1)

        if "ctOutdoorDeHumidificationRequestedDemand" in self.thermostat:
            dehumidification_demand = round(self.thermostat["ctOutdoorDeHumidificationRequestedDemand"] / 2, 1)

        if "ctAHHumidificationRequestedDemand" in self.thermostat:
            humidification_demand = round(self.thermostat["ctAHHumidificationRequestedDemand"] / 2, 1)

        if "ctAHUnitType" in self.thermostat and self.thermostat["ctAHUnitType"] != 255:
            indoor_mode=self.thermostat["ctAHMode"].strip()
        elif "ctIFCUnitType" in self.thermostat and self.thermostat["ctIFCUnitType"] != 255:
            indoor_mode=self.thermostat["ctIFCOperatingHeatCoolMode"].strip()

        outdoor_mode = "Unavailable"
        if "ctOutdoorMode" in self.thermostat:
            outdoor_mode = self.thermostat["ctOutdoorMode"].strip()

        attributes = {
            "fan": self.fan,
            "schedule_mode": self.thermostat.get("schedEnabled"),
            "fan_cfm": fan_cfm,
            "fan_demand": fan_demand,
            "cooling_demand": cooling_demand,
            "heating_demand": heating_demand,
            "heatpump_demand": heatpump_demand,
            "dehumidification_demand": dehumidification_demand,
            "humidification_demand": humidification_demand,
            "indoor_mode": indoor_mode,
            "outdoor_mode": outdoor_mode,
        }

        if "statFirmware" in self.thermostat:
            attributes["thermostat_version"] = self.thermostat["statFirmware"]
        if "nightModeActive" in self.thermostat:
            attributes["night_mode_active"] = self.thermostat["nightModeActive"]
        if "nightModeEnabled" in self.thermostat:
            attributes["night_mode_enabled"] = self.thermostat["nightModeEnabled"]
        if "displayLockPIN" in self.thermostat:
            attributes["thermostat_unlocked"] = bool(self.thermostat["displayLockPIN"] == 0)
        if "alertMediaAirFilterDays" in self.thermostat:
            attributes["media_filter_days"] = self.thermostat["alertMediaAirFilterDays"]
        if "iduEconoModeSetting" in self.thermostat:
            attributes["econo_mode"] = self.thermostat["iduEconoModeSetting"]
        if "oduPowerfulOperationRequest" in self.thermostat:
            attributes["boost_mode"] = self.thermostat["oduPowerfulOperationRequest"]

        if self._is_wall_unit:
            if "iduOnOff" in self.thermostat:
                attributes["idu_on"] = self.thermostat["iduOnOff"]
            if "iduOperatingMode" in self.thermostat:
                attributes["idu_operating_mode"] = self.thermostat["iduOperatingMode"]
            if "iduTargetTemp" in self.thermostat:
                attributes["idu_target_temp"] = self.thermostat["iduTargetTemp"]
            if "iduDeltaDCommand" in self.thermostat:
                attributes["idu_delta_d_command"] = self.thermostat["iduDeltaDCommand"]
            if "oduOutdoorTemp" in self.thermostat:
                attributes["odu_outdoor_temp"] = self.thermostat["oduOutdoorTemp"]
            if "oduCompOnOff" in self.thermostat:
                attributes["odu_comp_on"] = self.thermostat["oduCompOnOff"]
            if "oduIntPowerConsumption" in self.thermostat:
                attributes["odu_int_energy_wh"] = self.thermostat["oduIntPowerConsumption"] * 10

        return attributes


    def set_preset_mode(self, preset_mode):
        """Activate a preset."""
        if self._is_wall_unit:
            if self.hvac_mode == HVACMode.FAN_ONLY and preset_mode == PRESET_ECONO:
                _LOGGER.debug("Econo mode not supported in fan-only mode.")
                return
            if preset_mode == self.preset_mode:
                return
            if preset_mode == PRESET_ECONO:
                self.data.daikinskyport.set_econo_mode(self.thermostat_index, True)
                self.data.daikinskyport.set_boost_mode(self.thermostat_index, False)
            elif preset_mode == PRESET_BOOST:
                self.data.daikinskyport.set_boost_mode(self.thermostat_index, True)
                self.data.daikinskyport.set_econo_mode(self.thermostat_index, False)
            elif preset_mode == PRESET_NONE:
                self.data.daikinskyport.set_boost_mode(self.thermostat_index, False)
                self.data.daikinskyport.set_econo_mode(self.thermostat_index, False)
            else:
                return
            self._preset_mode = preset_mode
            self.update_without_throttle = True
            return
        if preset_mode == self.preset_mode:
            return

        if preset_mode == PRESET_AWAY:
            self.data.daikinskyport.set_away(self.thermostat_index, True)

        elif preset_mode == PRESET_SCHEDULE:
            self.data.daikinskyport.set_away(self.thermostat_index, False)
            self.resume_program()

        elif preset_mode == PRESET_MANUAL:
            self.data.daikinskyport.set_away(self.thermostat_index, False)
            self.data.daikinskyport.set_permanent_hold(self.thermostat_index)
            
        elif preset_mode == PRESET_TEMP_HOLD:
            self.data.daikinskyport.set_away(self.thermostat_index, False)
            self.data.daikinskyport.set_temp_hold(self.thermostat_index)
        else:
            return
        
        self._preset_mode = preset_mode

        self.update_without_throttle = True

    @property
    def preset_modes(self):
        """Return available preset modes."""
        if self._is_wall_unit and self.hvac_mode == HVACMode.FAN_ONLY:
            return [mode for mode in self._preset_modes if mode != PRESET_ECONO]
        return list(self._preset_modes)

    def set_auto_temp_hold(self, heat_temp, cool_temp):
        """Set temperature hold in auto mode."""
        if cool_temp is not None:
            cool_temp_setpoint = cool_temp
        else:
            cool_temp_setpoint = self.thermostat["cspHome"]

        if heat_temp is not None:
            heat_temp_setpoint = heat_temp
        else:
            heat_temp_setpoint = self.thermostat["hspHome"]

        if self._preset_mode == PRESET_MANUAL:
            self.data.daikinskyport.set_permanent_hold(
                self.thermostat_index,
                cool_temp_setpoint,
                heat_temp_setpoint
        )
        else:
            self.data.daikinskyport.set_temp_hold(
                self.thermostat_index,
                cool_temp_setpoint,
                heat_temp_setpoint,
                self.hold_preference(),
        )
        
        self._cool_setpoint = cool_temp_setpoint
        self._heat_setpoint = heat_temp_setpoint
        
        _LOGGER.debug(
            "Setting Daikin Skyport hold_temp to: heat=%s, is=%s, " "cool=%s, is=%s",
            heat_temp,
            isinstance(heat_temp, (int, float)),
            cool_temp,
            isinstance(cool_temp, (int, float)),
        )

        self.update_without_throttle = True

    def set_fan_mode(self, fan_mode):
        """Set the fan mode.  Valid values are "on", "auto", or "schedule"."""
        if self._is_wall_unit and self._is_dry_mode():
            _LOGGER.debug("Fan mode is not supported in dry mode.")
            return
        if self._is_wall_unit:
            if isinstance(fan_mode, str) and fan_mode in WALL_UNIT_FAN_SPEED_FROM_HASS:
                fan_speed = WALL_UNIT_FAN_SPEED_FROM_HASS[fan_mode]
            else:
                try:
                    fan_speed = int(fan_mode)
                except (TypeError, ValueError):
                    _LOGGER.error("Invalid wall unit fan speed: %s", fan_mode)
                    return
            hvac_mode = self.hvac_mode
            if hvac_mode == HVACMode.OFF:
                hvac_mode = self._wall_unit_operating_mode() or HVACMode.AUTO
            if hvac_mode == HVACMode.FAN_ONLY:
                daikin_mode = WALL_UNIT_FAN_MODE_DEFAULT
            else:
                daikin_mode = self._daikin_mode_from_hass(hvac_mode)
            self.data.daikinskyport.set_wall_unit_fan_speed(
                self.thermostat_index, fan_speed, daikin_mode
            )
            self._fan_mode = str(fan_speed)
            self.update_without_throttle = True
            _LOGGER.debug("Setting wall unit fan speed to: %s", fan_speed)
            return
        if fan_mode in {FAN_ON, FAN_AUTO, FAN_SCHEDULE}:
            self.data.daikinskyport.set_fan_mode(
                self.thermostat_index,
                FAN_TO_DAIKIN_FAN[fan_mode]
            )
            
            self._fan_mode = fan_mode
            self.update_without_throttle = True

            _LOGGER.debug("Setting fan mode to: %s", fan_mode)
        elif fan_mode in {FAN_LOW, FAN_MEDIUM, FAN_HIGH}:
            # Start the fan if it's off.  
            if self._fan_mode == FAN_AUTO:
                self.data.daikinskyport.set_fan_mode(
                    self.thermostat_index,
                    FAN_TO_DAIKIN_FAN[FAN_ON]
                )
                
                self._fan_mode = fan_mode

                _LOGGER.debug("Setting fan mode to: %s", fan_mode)

            self.data.daikinskyport.set_fan_speed(
                self.thermostat_index,
                FAN_TO_DAIKIN_FAN[fan_mode]
            )
            
            self._fan_speed = FAN_TO_DAIKIN_FAN[fan_mode]
            self.update_without_throttle = True

            _LOGGER.debug("Setting fan speed to: %s", self._fan_speed)
        else:
            error = "Invalid fan_mode value:  Valid values are 'on', 'auto', or 'schedule'"
            _LOGGER.error(error)
            return


    def set_temp_hold(self, temp):
        """Set temperature hold in modes other than auto."""
        if self.hvac_mode == HVACMode.HEAT:
            heat_temp = temp
            cool_temp = self.thermostat["cspHome"]
        elif self.hvac_mode == HVACMode.COOL:
            cool_temp = temp
            heat_temp = self.thermostat["hspHome"]
        self.set_auto_temp_hold(heat_temp, cool_temp)

        self._cool_setpoint = cool_temp
        self._heat_setpoint = heat_temp

    def set_temperature(self, **kwargs):
        """Set new target temperature."""
        if self._is_wall_unit and self._is_dry_mode():
            _LOGGER.debug("Temperature control is not supported in dry mode.")
            return
        if self._is_wall_unit:
            temp = kwargs.get(ATTR_TEMPERATURE)
            if temp is None:
                temp = kwargs.get(ATTR_TARGET_TEMP_HIGH) or kwargs.get(ATTR_TARGET_TEMP_LOW)
            if temp is None:
                _LOGGER.error("Missing temperature for wall unit set_temperature in %s", kwargs)
                return
            daikin_mode = self._daikin_mode_from_hass(self.hvac_mode)
            self.data.daikinskyport.set_wall_unit_temperature(
                self.thermostat_index, temp, daikin_mode
            )
            self._target_temp = temp
            if self.hvac_mode == HVACMode.HEAT:
                self._heat_setpoint = temp
            elif self.hvac_mode == HVACMode.COOL:
                self._cool_setpoint = temp
            self.update_without_throttle = True
            return

        low_temp = kwargs.get(ATTR_TARGET_TEMP_LOW)
        high_temp = kwargs.get(ATTR_TARGET_TEMP_HIGH)
        temp = kwargs.get(ATTR_TEMPERATURE)

        if self.hvac_mode == HVACMode.AUTO and (
            low_temp is not None or high_temp is not None
        ):
            self.set_auto_temp_hold(low_temp, high_temp)
        elif temp is not None:
            self.set_temp_hold(temp)
        else:
            _LOGGER.error("Missing valid arguments for set_temperature in %s", kwargs)

        self._cool_setpoint = high_temp
        self._heat_setpoint = low_temp


    def set_humidity(self, humidity):
        """Set the humidity level."""
        self.data.daikinskyport.set_humidity(self.thermostat_index, humidity)

    def set_hvac_mode(self, hvac_mode):
        """Set HVAC mode (auto, auxHeatOnly, cool, heat, off)."""
        if self._is_wall_unit:
            self.data.daikinskyport.set_wall_unit_mode(self.thermostat_index, hvac_mode)
        else:
            daikin_value = self._daikin_mode_from_hass(hvac_mode)
            if daikin_value is None:
                _LOGGER.error("Invalid mode for set_hvac_mode: %s", hvac_mode)
                return
            self.data.daikinskyport.set_hvac_mode(self.thermostat_index, daikin_value)
        self._hvac_mode = hvac_mode
        self.update_without_throttle = True

    def resume_program(self):
        """Resume the thermostat schedule program."""
        self.data.daikinskyport.resume_program(
            self.thermostat_index
        )
        self.update_without_throttle = True

    def set_fan_schedule(self, start=None, stop=None, interval=None, speed=None):
        """Set the thermostat fan schedule."""
        if self._is_wall_unit:
            _LOGGER.debug("Fan schedules are not supported for wall units.")
            return
        if start is None:
            start = self.thermostat["fanCirculateStart"]
        if stop is None:
            stop = self.thermostat["fanCirculateStop"]
        if interval is None:
            interval = self.thermostat["fanCirculateDuration"]
        self.data.daikinskyport.set_fan_schedule(
            self.thermostat_index, start, stop, interval, speed
        )
        self.update_without_throttle = True

    def set_night_mode(self, start=None, stop=None, enable=None):
        """Set the thermostat night mode."""
        if self._is_wall_unit:
            _LOGGER.debug("Night mode is not supported for wall units.")
            return
        if start is None:
            start = self.thermostat["nightModeStart"]
        if stop is None:
            stop = self.thermostat["nightModeStop"]
        if enable is None:
            enable = self.thermostat["nightModeEnabled"]
        self.data.daikinskyport.set_night_mode(
            self.thermostat_index, start, stop, enable
        )
        self.update_without_throttle = True

    def set_thermostat_schedule(self, day=None, start=None, part=None, enable=None, label=None, heating=None, cooling=None):
        """Set the thermostat schedule."""
        if self._is_wall_unit:
            _LOGGER.debug("Thermostat schedules are not supported for wall units.")
            return
        if day is None:
            now = datetime.now()
            day = now.strftime("%a")
        else:
            day = day[0:3].capitalize()
            if day not in WEEKDAY:
                _LOGGER.error("Invalid weekday: %s", day)
                return None
        if part is None:
            part = 1
        prefix = "sched" + day + "Part" + str(part)
        if start is None:
            start = self.thermostat[prefix + "Time"]
        if enable is None:
            enable = self.thermostat[prefix + "Enabled"]
        if label is None:
            label = self.thermostat[prefix + "Label"]
        if heating is None:
            heating = self.thermostat[prefix + "hsp"]
        if cooling is None:
            cooling = self.thermostat[prefix + "csp"]
        self.data.daikinskyport.set_thermostat_schedule(
            self.thermostat_index, prefix, start, enable, label, heating, cooling
        )
        self.update_without_throttle = True

    def set_oneclean(self, enable):
        """Enable/disable OneClean."""
        if self._is_wall_unit:
            _LOGGER.debug("OneClean is not supported for wall units.")
            return
        self.data.daikinskyport.set_fan_clean(
            self.thermostat_index, enable
        )
        self.update_without_throttle = True

    def set_efficiency(self, enable):
        """Enable/disable heat pump efficiency."""
        if self._is_wall_unit:
            _LOGGER.debug("Efficiency mode is not supported for wall units.")
            return
        self.data.daikinskyport.set_dual_fuel_efficiency(
            self.thermostat_index, enable
        )
        self.update_without_throttle = True

    def hold_preference(self):
        """Return user preference setting for hold time."""
        default = self.thermostat.get("schedOverrideDuration", HOLD_NEXT_TRANSITION)
        if isinstance(default, int):
            return default
        return HOLD_NEXT_TRANSITION

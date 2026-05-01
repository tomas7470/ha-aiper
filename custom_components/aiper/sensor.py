"""Sensor platform — one entity per piece of state in the device record."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, SIGNAL_STRENGTH_DECIBELS_MILLIWATT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import AiperCoordinator
from .entity import AiperEntity


@dataclass(frozen=True, kw_only=True)
class AiperSensorDescription(SensorEntityDescription):
    """One row per sensor; `value_fn` extracts the value from the device record."""

    value_fn: Callable[[dict[str, Any]], Any]


# Map the device's numeric MachineStatus to a readable string.
# 0 = standby (idle), 1 = running, 2 = paused, anything else = raw int as str.
# Numeric → label mapping observed on IrriSense 2 firmware:
#   0 = standby (idle)
#   1 = running
#   2 = paused (manual or schedule pause)
#   6 = fault — usually water shortage / pump issue (alarm 4005); recovers
#       to standby once the alarm clears
_MACHINE_STATUS_LABELS = {0: "standby", 1: "running", 2: "paused", 6: "fault"}


def _map_machine_status(v: Any) -> Any:
    if isinstance(v, dict):
        v = v.get("status")
    if v is None:
        return None
    try:
        return _MACHINE_STATUS_LABELS.get(int(v), str(v))
    except (TypeError, ValueError):
        return v


def _resolve_machine_status(d: dict[str, Any]) -> Any:
    """Same as _map_machine_status but with an idle-fallback: if we have no
    live MachineStatus yet but the device is reachable (MQTT online + no
    alarms), assume `standby` — matches the app's behaviour and avoids the
    misleading 'unknown' state most of the time.
    """
    raw = d.get("mqtt_MachineStatus") if "mqtt_MachineStatus" in d else d.get("machineStatus")
    mapped = _map_machine_status(raw)
    if mapped is not None:
        return mapped
    if d.get("mqtt_online") or d.get("online"):
        codes = d.get("alarm_codes") or []
        return "standby" if not codes else "fault"
    return None


SENSORS: tuple[AiperSensorDescription, ...] = (
    AiperSensorDescription(
        key="firmware_version",
        translation_key="firmware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("version") or d.get("mainFirmwareVersion"),
    ),
    AiperSensorDescription(
        key="mcu_firmware_version",
        translation_key="mcu_firmware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("mcuFirmwareVersion"),
    ),
    AiperSensorDescription(
        key="valve_firmware_version",
        translation_key="valve_firmware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("valveFirmwareVersion"),
    ),
    AiperSensorDescription(
        key="bluetooth_firmware_version",
        translation_key="bluetooth_firmware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("bluetoothFirmwareVersion"),
    ),
    AiperSensorDescription(
        key="wifi_rssi",
        translation_key="wifi_rssi",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("wifiRssi"),
    ),
    AiperSensorDescription(
        key="wifi_ssid",
        translation_key="wifi_ssid",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("wifiName"),
    ),
    AiperSensorDescription(
        key="machine_status",
        translation_key="machine_status",
        # 0 = standby, 1 = running. Comes from realTimeProgress/setWorkMode
        # responses on aiper/things/<sn>/upChan; falls back to family-tree value.
        # If no live MachineStatus has been received yet but the device is
        # online and has no alarms, treat that as "standby" — matches what the
        # mobile app shows in the same situation.
        value_fn=lambda d: _resolve_machine_status(d),
    ),
    AiperSensorDescription(
        key="alarm_codes",
        translation_key="alarm_codes",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: ",".join(str(c) for c in d.get("alarm_codes") or []) or "none",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AiperCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[AiperSensor] = []
    for serial in coordinator.data:
        for desc in SENSORS:
            entities.append(AiperSensor(coordinator, serial, desc))
    async_add_entities(entities)


class AiperSensor(AiperEntity, SensorEntity):
    entity_description: AiperSensorDescription

    def __init__(
        self,
        coordinator: AiperCoordinator,
        serial: str,
        description: AiperSensorDescription,
    ) -> None:
        super().__init__(coordinator, serial)
        self.entity_description = description
        self._attr_unique_id = f"{serial}_{description.key}"

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.device)

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
        # Numeric op-state from the device shadow / family tree (0=idle, others TBD).
        # Only present for offline devices in the family tree; for online devices
        # we'll surface it in Phase 2 via shadow updates.
        value_fn=lambda d: d.get("machineStatus"),
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

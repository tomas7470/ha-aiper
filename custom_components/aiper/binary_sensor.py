"""Binary sensor — online status, auto-upgrade flag."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import AiperCoordinator
from .entity import AiperEntity


@dataclass(frozen=True, kw_only=True)
class AiperBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], bool | None]


BINARY_SENSORS: tuple[AiperBinarySensorDescription, ...] = (
    AiperBinarySensorDescription(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda d: bool(d.get("online")) if d.get("online") is not None else None,
    ),
    AiperBinarySensorDescription(
        key="auto_upgrade",
        translation_key="auto_upgrade",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: bool(d.get("autoUpgrade")) if d.get("autoUpgrade") is not None else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AiperCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[AiperBinarySensor] = []
    for serial in coordinator.data:
        for desc in BINARY_SENSORS:
            entities.append(AiperBinarySensor(coordinator, serial, desc))
    async_add_entities(entities)


class AiperBinarySensor(AiperEntity, BinarySensorEntity):
    entity_description: AiperBinarySensorDescription

    def __init__(
        self,
        coordinator: AiperCoordinator,
        serial: str,
        description: AiperBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator, serial)
        self.entity_description = description
        self._attr_unique_id = f"{serial}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.device)

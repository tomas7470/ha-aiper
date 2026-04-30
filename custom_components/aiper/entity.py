"""Shared entity base for Aiper devices."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AiperCoordinator


class AiperEntity(CoordinatorEntity[AiperCoordinator]):
    """One Aiper device per `serial`; entities derive their identity from that."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: AiperCoordinator, serial: str) -> None:
        super().__init__(coordinator)
        self._serial = serial

    @property
    def device(self) -> dict[str, Any]:
        return self.coordinator.data.get(self._serial, {})

    @property
    def available(self) -> bool:
        return super().available and bool(self.coordinator.data.get(self._serial))

    @property
    def device_info(self) -> DeviceInfo:
        d = self.device
        return DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            name=d.get("name") or f"Aiper {self._serial}",
            manufacturer="Aiper",
            model=d.get("deviceModel") or "IrriSense",
            sw_version=d.get("version"),
            hw_version=d.get("subver"),
            serial_number=self._serial,
        )

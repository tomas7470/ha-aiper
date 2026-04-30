"""Select platform — region picker for map-based devices (IrriSense 2.0)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import AiperCoordinator
from .entity import AiperEntity

REGION = SelectEntityDescription(
    key="region",
    translation_key="region",
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AiperCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[AiperRegionSelect] = []
    for sn, dev in coordinator.data.items():
        # Only useful for devices with map regions; WR (single-zone) skips it.
        if dev.get("regions"):
            entities.append(AiperRegionSelect(coordinator, sn))
    async_add_entities(entities)


class AiperRegionSelect(AiperEntity, SelectEntity, RestoreEntity):
    """Lets the user pick which mapped zone to use for the next run."""

    entity_description = REGION

    def __init__(self, coordinator: AiperCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_region"
        self._current_option: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in self.options:
            self._current_option = last.state
        elif self.options:
            self._current_option = self.options[0]

    @property
    def options(self) -> list[str]:
        return [r["name"] for r in self.device.get("regions", []) if isinstance(r, dict)]

    @property
    def current_option(self) -> str | None:
        # Heal stale selection if the user renamed/removed regions externally.
        if self._current_option not in self.options and self.options:
            self._current_option = self.options[0]
        return self._current_option

    async def async_select_option(self, option: str) -> None:
        if option in self.options:
            self._current_option = option
            self.async_write_ha_state()

    @callback
    def selected_region_id(self) -> int:
        """Return the numeric region id for the currently selected name."""
        for r in self.device.get("regions", []):
            if isinstance(r, dict) and r.get("name") == self._current_option:
                return int(r.get("id") or 0)
        return 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "region_id": self.selected_region_id(),
            "available_regions": self.device.get("regions", []),
        }

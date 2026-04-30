"""Button platform: refresh + Start watering (one-click run with current settings)."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import AiperCoordinator
from .entity import AiperEntity

_LOGGER = logging.getLogger(__name__)

REFRESH = ButtonEntityDescription(
    key="refresh",
    translation_key="refresh",
    entity_category=EntityCategory.DIAGNOSTIC,
)
RUN = ButtonEntityDescription(
    key="run",
    translation_key="run",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AiperCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[AiperEntity] = []
    for sn in coordinator.data:
        entities.append(AiperRefreshButton(coordinator, sn))
        entities.append(AiperRunButton(coordinator, sn))
    async_add_entities(entities)


class AiperRefreshButton(AiperEntity, ButtonEntity):
    entity_description = REFRESH

    def __init__(self, coordinator: AiperCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_refresh"

    async def async_press(self) -> None:
        await self.coordinator.async_request_refresh()


class AiperRunButton(AiperEntity, ButtonEntity):
    """Start a one-shot run using the depth + duration + region picked via the
    `number.aiper_<sn>_run_depth`, `number.aiper_<sn>_run_duration_override`,
    and `select.aiper_<sn>_region` entities.
    """

    entity_description = RUN

    def __init__(self, coordinator: AiperCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_run"

    async def async_press(self) -> None:
        # Local import: avoids circular at module-load time.
        from . import async_trigger_run  # noqa: PLC0415

        depth = self._read_number_state("run_depth", default=6.0)
        duration = int(self._read_number_state("run_duration_override", default=0.0))
        region_id = self._read_select_region_id()

        # If both ended up at 0, default to a 6mm run rather than blowing up.
        if depth <= 0 and duration <= 0:
            depth = 6.0

        _LOGGER.info(
            "AiperRunButton.press sn=%s depth=%s duration=%s region_id=%s",
            self._serial, depth, duration, region_id,
        )
        await async_trigger_run(
            self.coordinator,
            self._serial,
            depth=depth,
            duration=duration,
            region_id=region_id,
        )

    # ---- helpers: read sibling entity states ----
    def _read_number_state(self, key: str, *, default: float) -> float:
        """Resolve number.<auto>_<key> for our serial via the entity registry."""
        from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

        registry = er.async_get(self.hass)
        unique = f"{self._serial}_{key}"
        ent_id = registry.async_get_entity_id("number", DOMAIN, unique)
        if not ent_id:
            return default
        state = self.hass.states.get(ent_id)
        if state is None or state.state in (None, "unknown", "unavailable"):
            return default
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return default

    def _read_select_region_id(self) -> int:
        """Resolve the currently-picked region id from the select entity, if any."""
        from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

        registry = er.async_get(self.hass)
        ent_id = registry.async_get_entity_id("select", DOMAIN, f"{self._serial}_region")
        if not ent_id:
            return 0  # WR — single zone, async_trigger_run will pass 0
        state = self.hass.states.get(ent_id)
        if state is None:
            return 0
        return int((state.attributes or {}).get("region_id") or 0)

"""Number platform — user-set parameters for the next manual run.

Two persistent entities per device:
  * `number.aiper_<sn>_run_depth`  — millimetres (default 6)
  * `number.aiper_<sn>_run_duration_override` — minutes; 0 = use depth
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import AiperCoordinator
from .entity import AiperEntity


@dataclass(frozen=True, kw_only=True)
class AiperNumberDescription(NumberEntityDescription):
    default: float
    fallback_attr: str  # name of attribute on the entity for the in-memory value


NUMBERS: tuple[AiperNumberDescription, ...] = (
    AiperNumberDescription(
        key="run_depth",
        translation_key="run_depth",
        native_min_value=0,
        native_max_value=50,
        native_step=0.5,
        native_unit_of_measurement="mm",
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        default=6.0,
        fallback_attr="_depth",
    ),
    AiperNumberDescription(
        key="run_duration_override",
        translation_key="run_duration_override",
        native_min_value=0,
        native_max_value=120,
        native_step=1,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        default=0,
        fallback_attr="_duration",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AiperCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[AiperRunNumber] = []
    for sn in coordinator.data:
        for desc in NUMBERS:
            entities.append(AiperRunNumber(coordinator, sn, desc))
    async_add_entities(entities)


class AiperRunNumber(AiperEntity, NumberEntity, RestoreEntity):
    """A user-settable run parameter (depth, duration override). Persists across restarts."""

    entity_description: AiperNumberDescription

    def __init__(
        self,
        coordinator: AiperCoordinator,
        serial: str,
        description: AiperNumberDescription,
    ) -> None:
        super().__init__(coordinator, serial)
        self.entity_description = description
        self._attr_unique_id = f"{serial}_{description.key}"
        self._value: float = float(description.default)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state not in (None, "unknown", "unavailable"):
            try:
                self._value = float(last.state)
            except (TypeError, ValueError):
                pass

    @property
    def native_value(self) -> float:
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        self._value = float(value)
        self.async_write_ha_state()

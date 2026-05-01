"""Switch platform — `Watering` toggle per device.

A stateful control: ON while the device is irrigating, OFF in Standby.
Toggling publishes a `desired` block via the MQTT shadow.

State source: the device's reported MachineStatus when present (live via
MQTT shadow). When it's missing we fall back to "no schedule active" =
OFF, matching the "Standby" label the Aiper app shows.

`async_turn_on` hands off to the same shared `async_trigger_run` helper
the button uses, picking up the current Run depth / Run duration override
/ Region select values so the experience matches the Start watering button.
"""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import AiperCoordinator
from .entity import AiperEntity

_LOGGER = logging.getLogger(__name__)

WATERING = SwitchEntityDescription(
    key="watering",
    translation_key="watering",
)
MQTT_CAPTURE = SwitchEntityDescription(
    key="mqtt_capture",
    translation_key="mqtt_capture",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AiperCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = []
    for sn in coordinator.data:
        entities.append(AiperWateringSwitch(coordinator, sn))
    # One capture switch per integration (uses first device for grouping).
    if coordinator.data:
        first_sn = next(iter(coordinator.data))
        entities.append(AiperMqttCaptureSwitch(coordinator, first_sn))
    async_add_entities(entities)


class AiperWateringSwitch(AiperEntity, SwitchEntity):
    """ON when the device is irrigating, OFF in standby."""

    entity_description = WATERING
    _attr_should_poll = False
    # Optimistic: we flip state on press and let the next shadow update
    # ground-truth it back. Without optimism the UI lags by ~1 s.
    _attr_assumed_state = True

    def __init__(self, coordinator: AiperCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_watering"
        self._optimistic_state: bool | None = None

    @property
    def is_on(self) -> bool | None:
        d = self.device
        # MQTT shadow: MachineStatus presence/value indicates an active run.
        # The exact non-zero values are still being mapped (see
        # IRRISENSE_2_FACTS.md). For now any non-zero / non-None == running.
        ms = d.get("mqtt_MachineStatus")
        if isinstance(ms, dict):
            # Some firmwares wrap it: {"status": 1, "regionId": 1, ...}
            ms = ms.get("status")
        if ms is not None:
            return bool(ms)
        # No live data — fall back to optimistic / unknown.
        return self._optimistic_state

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        d = self.device
        return {
            "machine_status": d.get("mqtt_MachineStatus"),
            "alarm_codes": d.get("alarm_codes"),
            "last_alarm_ts": d.get("alarm_timestamp"),
        }

    async def async_turn_on(self, **kwargs: object) -> None:
        # Local import: avoid import cycle at module load time.
        from . import async_trigger_run  # noqa: PLC0415
        from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

        registry = er.async_get(self.hass)

        def _read_number(key: str, default: float) -> float:
            ent = registry.async_get_entity_id("number", DOMAIN, f"{self._serial}_{key}")
            if not ent:
                return default
            st = self.hass.states.get(ent)
            if st is None or st.state in (None, "unknown", "unavailable"):
                return default
            try:
                return float(st.state)
            except (TypeError, ValueError):
                return default

        def _read_region_id() -> int:
            ent = registry.async_get_entity_id("select", DOMAIN, f"{self._serial}_region")
            if not ent:
                return 0
            st = self.hass.states.get(ent)
            if st is None:
                return 0
            return int((st.attributes or {}).get("region_id") or 0)

        depth = _read_number("run_depth", 6.0)
        duration = int(_read_number("run_duration_override", 0))
        if depth <= 0 and duration <= 0:
            depth = 6.0
        region_id = _read_region_id()

        _LOGGER.info(
            "switch.watering ON: sn=%s depth=%.1fmm duration=%dmin region_id=%s",
            self._serial, depth, duration, region_id,
        )
        await async_trigger_run(
            self.coordinator,
            self._serial,
            depth=depth,
            duration=duration,
            region_id=region_id,
        )
        # Optimistic flip — next MQTT shadow update will overwrite.
        self._optimistic_state = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: object) -> None:
        """Stop = `WrControl{cmd: 0}` shadow desired (smali:
        ManualOperateViewModel.resetControl)."""
        mqtt = self.coordinator.mqtt
        if mqtt is None or not mqtt.is_connected:
            raise RuntimeError("Stop requires MQTT shadow (cloud_push); MQTT not connected")
        await mqtt.publish_shadow_desired(
            self._serial,
            {"WrControl": {"cmd": 0}},
        )
        self._optimistic_state = False
        self.async_write_ha_state()


class AiperMqttCaptureSwitch(AiperEntity, SwitchEntity):
    """Toggle MQTT debug capture: writes every received + sent MQTT message
    to /config/aiper_mqtt_capture.jsonl (rolling 5 MB).

    Use this when you want to learn what the Aiper app actually publishes
    when you tap "Quick Run" or "Stop": enable, do the action in the app,
    disable, then we read the file to extract the exact topic + payload.
    """

    entity_description = MQTT_CAPTURE
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: AiperCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"aiper_{coordinator.entry.entry_id}_mqtt_capture"

    @property
    def is_on(self) -> bool:
        return self.coordinator.capture_enabled

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "capture_path": str(self.coordinator._capture_path),  # noqa: SLF001
            "max_bytes": self.coordinator._capture_max_bytes,  # noqa: SLF001
        }

    async def async_turn_on(self, **kwargs: object) -> None:
        self.coordinator.capture_enabled = True
        # Pause REST polling so HA's JWT doesn't keep itself alive — that
        # frees the user to log in to the Aiper app on their phone without
        # the app's login invalidating HA's JWT (and triggering a re-login
        # that kicks the app off). MQTT runs on AWS Cognito temp creds
        # cached locally and survives independently for ~1h.
        self.coordinator.update_interval = None
        _LOGGER.info(
            "Aiper MQTT capture ENABLED — REST polling paused. "
            "Capture file: %s. Open the Aiper app and exercise Quick Run "
            "/ Stop / etc. to record what the app actually publishes.",
            self.coordinator._capture_path,  # noqa: SLF001
        )
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: object) -> None:
        from datetime import timedelta  # noqa: PLC0415
        from .const import DEFAULT_SCAN_INTERVAL  # noqa: PLC0415

        self.coordinator.capture_enabled = False
        # Resume REST polling at the same slow rate the coordinator uses
        # while MQTT is up.
        self.coordinator.update_interval = timedelta(seconds=DEFAULT_SCAN_INTERVAL * 6)
        await self.coordinator.async_request_refresh()
        _LOGGER.info("Aiper MQTT capture DISABLED — REST polling resumed")
        self.async_write_ha_state()

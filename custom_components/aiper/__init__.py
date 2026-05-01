"""Aiper IrriSense integration."""

from __future__ import annotations

import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
import homeassistant.helpers.config_validation as cv

from .api import AiperError
from .const import DOMAIN
from .coordinator import AiperCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.CAMERA,
]

SERVICE_RUN_NOW = "run_now"
RUN_NOW_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        # Either depth (preferred for IrriSense 2.0) or duration must be > 0;
        # we validate that combination in the handler so the error is friendly.
        vol.Optional("depth", default=6.0): vol.All(vol.Coerce(float), vol.Range(min=0, max=50)),
        vol.Optional("duration", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=120)),
        vol.Optional("region_id", default=0): vol.All(vol.Coerce(int), vol.Range(min=0)),
    }
)


def _resolve_serial(hass: HomeAssistant, device_id: str) -> tuple[str, AiperCoordinator]:
    """Look up the (serial, coordinator) pair behind a HA device id."""
    device = dr.async_get(hass).async_get(device_id)
    if not device:
        raise vol.Invalid(f"Unknown device id: {device_id}")
    serial: str | None = next(
        (ident for dom, ident in device.identifiers if dom == DOMAIN), None
    )
    if not serial:
        raise vol.Invalid(f"Device {device_id} is not an Aiper device")
    for entry_id in device.config_entries:
        coord: AiperCoordinator | None = hass.data.get(DOMAIN, {}).get(entry_id)
        if coord is not None:
            return serial, coord
    raise vol.Invalid(f"No active Aiper coordinator for device {device_id}")


async def async_trigger_run(
    coordinator: AiperCoordinator,
    serial: str,
    *,
    depth: float = 0.0,
    duration: int = 0,
    region_id: int = 0,
) -> None:
    """Trigger a one-shot watering run.

    Tries the MQTT shadow-desired path first (matches the app's "Quick run"
    button); falls back to the REST scheduled-task hack only if MQTT isn't
    connected. Caller must ensure exactly one of `depth`/`duration` > 0.
    """
    if depth <= 0 and duration <= 0:
        raise vol.Invalid("Set either depth (mm) > 0 or duration (min) > 0")
    use_depth = depth > 0

    # Auto-pick first map region when the user left region_id=0 and there's a map.
    if region_id == 0:
        try:
            regions = await coordinator.client.get_map_regions(serial)
            if regions:
                first = regions[0]
                region_id = int(first.get("id") or 0)
                _LOGGER.info(
                    "run_now: auto-picked region %r (id=%s) for %s",
                    first.get("name"), region_id, serial,
                )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("region auto-pick failed for %s: %s", serial, exc)

    # Path 1 (preferred): MQTT shadow-desired. The IrriSense 2.0 firmware
    # listens for a `Watering` desired-state block. Field names mirror what
    # the app's CmdManager sends (smali: editIrrisenseTaskTDevice — keys are
    # snake_case `water_depth`, `point_time`, `start_time`, `weekdays`,
    # `repeat_type`). We send a one-shot (repeat_type=0) starting now+10s.
    if coordinator.mqtt.is_connected:
        start_ts = int(time.time()) + 10
        start_local = time.localtime(start_ts)
        hh_mm = f"{start_local.tm_hour:02d}:{start_local.tm_min:02d}"
        desired: dict[str, Any] = {
            "Watering": {
                "command": "start",
                "regionId": region_id,
                "start_time": hh_mm,
                "weekdays": "",
                "repeat_type": 0,
                "trigger_ts": start_ts,
            }
        }
        if use_depth:
            desired["Watering"]["water_depth"] = depth
            desired["Watering"]["point_time"] = 0
        else:
            desired["Watering"]["point_time"] = duration
            desired["Watering"]["water_depth"] = 0.0
        _LOGGER.info("run_now via MQTT shadow: sn=%s %s", serial, desired)
        await coordinator.mqtt.publish_shadow_desired(serial, desired)
        # Coordinator will pick up the device's response on the shadow
        # update/accepted topic; nothing else to do.
        return

    # Path 2 (fallback): REST scheduled task. This is the only path the
    # cloud allows when MQTT isn't up; it works ONLY if the device is in a
    # healthy state (otherwise 6002).
    map_id = 0
    try:
        maps: Any = await coordinator.client.get_map_list(serial)
        if isinstance(maps, list) and maps and isinstance(maps[0], dict):
            map_id = int(maps[0].get("id") or 0)
    except AiperError as exc:
        _LOGGER.debug("get_map_list failed for %s: %s", serial, exc)

    start_ts = int(time.time()) + 10
    start_local = time.localtime(start_ts)
    start_time_str = f"{start_local.tm_hour:02d}:{start_local.tm_min:02d}"
    _LOGGER.info(
        "run_now via REST (MQTT not connected): sn=%s %s map_id=%s region_id=%s",
        serial,
        f"depth={depth}mm" if use_depth else f"duration={duration}min",
        map_id, region_id,
    )
    await coordinator.client.add_watering_task(
        serial,
        first_execute_ts_sec=start_ts,
        map_id=map_id,
        region_id=region_id,
        depth_mm=depth if use_depth else None,
        duration_min=None if use_depth else duration,
        start_time=start_time_str,
        repeat_type=0,
    )
    await coordinator.async_request_refresh()


async def _async_service_run_now(hass: HomeAssistant, call: ServiceCall) -> None:
    """`aiper.run_now` service handler — thin wrapper over async_trigger_run."""
    serial, coordinator = _resolve_serial(hass, call.data["device_id"])
    await async_trigger_run(
        coordinator,
        serial,
        depth=float(call.data["depth"]),
        duration=int(call.data["duration"]),
        region_id=int(call.data["region_id"]),
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = AiperCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Bring up the MQTT shadow connection once entities are ready so the first
    # state-message refresh dispatches cleanly.
    try:
        await coordinator.async_start_mqtt()
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("MQTT start failed (will keep retrying): %s", exc)

    # Register services once per HA instance (idempotent — second call is a no-op).
    if not hass.services.has_service(DOMAIN, SERVICE_RUN_NOW):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RUN_NOW,
            lambda call: _async_service_run_now(hass, call),
            schema=RUN_NOW_SCHEMA,
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coord: AiperCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coord is not None:
        await coord.async_stop_mqtt()
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    hass.data[DOMAIN].pop(entry.entry_id, None)
    # Drop services if this was the last entry.
    if not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_RUN_NOW)
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)

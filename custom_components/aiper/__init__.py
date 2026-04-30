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


async def _async_run_now(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service handler: aiper.run_now — create a one-shot watering task."""
    serial, coordinator = _resolve_serial(hass, call.data["device_id"])
    depth: float = float(call.data["depth"])
    duration: int = int(call.data["duration"])
    region_id: int = int(call.data["region_id"])

    if depth <= 0 and duration <= 0:
        raise vol.Invalid("Set either depth (mm) > 0 or duration (min) > 0")
    # If both set, prefer depth (matches the Aiper app's behaviour).
    use_depth = depth > 0

    # IrriSense 2.0 is map-based; pick the first available map. WR doesn't
    # need a map (we send 0 if there isn't one).
    map_id = 0
    try:
        maps: Any = await coordinator.client.get_map_list(serial)
        if isinstance(maps, list) and maps and isinstance(maps[0], dict):
            map_id = int(maps[0].get("id") or 0)
    except AiperError as exc:
        _LOGGER.debug("get_map_list failed for %s: %s", serial, exc)

    start_ts = int(time.time()) + 10
    _LOGGER.info(
        "aiper.run_now: sn=%s %s map_id=%s region_id=%s start_ts=%s",
        serial,
        f"depth={depth}mm" if use_depth else f"duration={duration}min",
        map_id, region_id, start_ts,
    )
    await coordinator.client.add_watering_task(
        serial,
        first_execute_ts_sec=start_ts,
        map_id=map_id,
        region_id=region_id,
        depth_mm=depth if use_depth else None,
        duration_min=None if use_depth else duration,
        repeat_type=0,
    )
    await coordinator.async_request_refresh()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = AiperCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services once per HA instance (idempotent — second call is a no-op).
    if not hass.services.has_service(DOMAIN, SERVICE_RUN_NOW):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RUN_NOW,
            lambda call: _async_run_now(hass, call),
            schema=RUN_NOW_SCHEMA,
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    hass.data[DOMAIN].pop(entry.entry_id, None)
    # Drop services if this was the last entry.
    if not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_RUN_NOW)
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)

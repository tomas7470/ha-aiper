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
    Platform.SWITCH,
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

    Tries MQTT direct-publish first (the path the v3.3.0 app actually uses
    for Quick Run, reverse-engineered + Frida-confirmed). Falls back to the
    REST scheduled-task hack if MQTT isn't connected.

    Wire format (MQTT direct, confirmed via Frida hook on live v3.3.0 app):
        topic   : aiper/things/<sn>/downChan
        payload : {"setWorkMode": <body>}        # X9 format, plain JSON, NO encryption
        body    : {"mode":0, "waterYield":<inches>, "map_id":<region.id>, "status":1}
        QoS     : 1                              # AWSIotMqttManager.publishString QOS1
    """
    if depth <= 0 and duration <= 0:
        raise vol.Invalid("Set either depth (mm) > 0 or duration (min) > 0")

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

    # ---- Path 1: MQTT direct setWorkMode (the app's Quick Run path) ----
    # Map the user's depth-mm to the app's `waterYield` field which is in
    # INCHES. The app's UI presets are 3/6/12 mm = 0.1/0.25/0.5 inches.
    if depth > 0:
        if depth <= 4:
            water_yield = 0.1
        elif depth <= 8:
            water_yield = 0.25
        else:
            water_yield = 0.5
    else:
        # Duration-based fallback — pick mid preset.
        water_yield = 0.25

    mqtt = coordinator.mqtt
    if mqtt is not None and mqtt.is_connected:
        # Field order matters for byte-for-byte parity with the app: mode, waterYield, map_id, status.
        body = {
            "mode": 0,
            "waterYield": water_yield,
            "map_id": region_id,
            "status": 1,
        }
        try:
            await mqtt.publish_aiper_cmd(serial, "setWorkMode", body)
            # The app also subscribes to progress immediately after starting.
            await mqtt.publish_aiper_cmd(serial, "realTimeProgress", {"cmd": 1})
            _LOGGER.info("run_now (MQTT setWorkMode): sn=%s body=%s", serial, body)
            await coordinator.async_request_refresh()
            return
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("MQTT setWorkMode failed (%s); falling back to REST", exc)

    # ---- Path 2: REST scheduled-task fallback ----
    # Used when MQTT isn't connected. Creates a recurring-day-of-week task
    # 70s in the future; auto-deleted after expected duration.
    use_depth = depth > 0
    map_id = int(coordinator.data.get(serial, {}).get("map_id") or 0)
    start_ts = int(time.time()) + 70
    start_local = time.localtime(start_ts)
    start_time_str = f"{start_local.tm_hour:02d}:{start_local.tm_min:02d}"
    _LOGGER.info(
        "run_now (scheduled hack): sn=%s start=%s map_id=%s region_id=%s %s",
        serial, start_time_str, map_id, region_id,
        f"depth={depth}mm" if use_depth else f"duration={duration}min",
    )
    result = await coordinator.client.add_watering_task(
        serial,
        first_execute_ts_sec=start_ts,
        map_id=map_id,
        region_id=region_id,
        depth_mm=depth if use_depth else None,
        duration_min=None if use_depth else duration,
        start_time=start_time_str,
        repeat_type=1,
        repeat_days="1,1,1,1,1,1,1",
    )
    task_id: int | None = None
    if isinstance(result, dict):
        task_id = result.get("id")
    elif isinstance(result, list) and result and isinstance(result[0], dict):
        task_id = result[0].get("id")
    if task_id is not None:
        coordinator.hass.async_create_task(
            _async_cleanup_run_task(coordinator, serial, int(task_id), duration_min=max(duration, 15))
        )
    await coordinator.async_request_refresh()


async def _async_cleanup_run_task(
    coordinator: AiperCoordinator,
    serial: str,
    task_id: int,
    *,
    duration_min: int,
) -> None:
    """Wait for the run-now scheduled task to fire and complete, then
    delete it so it doesn't recur next week.

    The task starts ~70s after creation and runs for `duration_min` minutes
    (or whatever the device's depth-based estimate ends up being). We sleep
    that long + a comfortable margin, then call batchDeleteWateringTaskV2
    with the captured id."""
    import asyncio  # noqa: PLC0415

    delay = 70 + max(duration_min, 1) * 60 + 60  # start lag + run + margin
    _LOGGER.debug("scheduling auto-delete of task %s for sn=%s in %ds", task_id, serial, delay)
    await asyncio.sleep(delay)
    try:
        await coordinator.client._post(  # noqa: SLF001
            "/wr/batchDeleteWateringTaskV2",
            {"sn": serial, "deleteTaskIdList": [task_id]},
        )
        _LOGGER.info("auto-deleted run-now task %s for sn=%s", task_id, serial)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning(
            "auto-delete of task %s for sn=%s failed: %s — please remove "
            "it manually from the Aiper app or via the schedule list",
            task_id, serial, exc,
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

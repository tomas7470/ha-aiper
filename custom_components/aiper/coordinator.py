"""DataUpdateCoordinator for the Aiper integration."""

from __future__ import annotations

import json
import logging
import ssl
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AiperAuthError, AiperClient, AiperError
from .const import (
    CONF_API_BASE,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REGION,
    CONF_TOKEN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .mqtt import AiperMqttClient

_LOGGER = logging.getLogger(__name__)


class AiperCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Polls the Aiper cloud for the user's devices and per-device state.

    Stored data shape: `{ <serial>: <merged-device-record> }` where each record
    is the union of the family tree entry and the latest /wr/getEquipmentInfo
    response.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            # Live state comes from MQTT (which uses independent AWS Cognito
            # creds and doesn't conflict with the Aiper app's JWT session).
            # REST polling is intentionally rare — every poll re-uses the
            # stored JWT, and Aiper enforces a one-session-per-account rule
            # so polling too often would cause us to wake-up & invalidate
            # whatever session the user just established in the mobile app.
            #
            # We poll every 30 min just as a safety net for fields the
            # shadow doesn't carry (firmware versions, map updates).
            update_interval=timedelta(minutes=30),
        )
        # Set when REST returns 402; while True we skip background polling
        # and entities depending on REST-only fields go unavailable. User-
        # initiated actions (button press) trigger a reauth + retry.
        self._session_invalid: bool = False
        self.entry = entry
        self.client = AiperClient(
            session=async_get_clientsession(hass),
            region=entry.data[CONF_REGION],
            api_base=entry.data.get(CONF_API_BASE),
            token=entry.data.get(CONF_TOKEN),
        )
        # ssl context is built off-loop by async_start_mqtt to avoid
        # blocking-call complaints from HA's loop guard.
        self._ssl_context: ssl.SSLContext | None = None
        self.mqtt: AiperMqttClient | None = None

        # MQTT debug capture — when enabled, every received MQTT message and
        # every published payload is written as JSON-Lines to /config so the
        # user / a future analysis script can replay & inspect the schema. The
        # toggle lives on a switch entity so users can enable when they're
        # about to do something interesting in the Aiper app.
        self.capture_enabled: bool = False
        self._capture_path: Path = Path(hass.config.path("aiper_mqtt_capture.jsonl"))
        self._capture_max_bytes: int = 5 * 1024 * 1024  # 5 MB rolling cap

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        # Background poll: NEVER auto-relogin here. If our JWT got kicked
        # off (the user logged in to the app), respect that — keep MQTT
        # running, mark REST as paused, and surface UpdateFailed so HA's
        # UI shows the entry as "unavailable" until the user explicitly
        # reclaims the session via the Reconnect button.
        if self._session_invalid:
            raise UpdateFailed(
                "Aiper session locked by another client (likely the mobile "
                "app). Press the Reconnect button on any IrriSense device "
                "to reclaim it."
            )
        try:
            devices = await self.client.list_equipment()
        except AiperAuthError as exc:
            self._session_invalid = True
            raise UpdateFailed(
                f"Aiper session no longer valid: {exc}. "
                "Press Reconnect on the device to take it back."
            ) from exc
        except AiperError as exc:
            raise UpdateFailed(str(exc)) from exc

        merged: dict[str, dict[str, Any]] = {}
        for dev in devices:
            sn = dev["sn"]
            record = dict(dev)
            try:
                info = await self.client.get_equipment_info(sn)
                if isinstance(info, dict):
                    record.update(info)
            except AiperError as exc:
                _LOGGER.debug("getEquipmentInfo failed for %s: %s", sn, exc)

            # Map regions — map JSON URL TTL is 1h so we cache, refresh once per
            # mapId change. Stored as `regions: [{id:int, name:str}, ...]` so
            # the select platform can render names without re-fetching.
            try:
                old = (self.data or {}).get(sn, {}) if self.data else {}
                if not record.get("regions") and old.get("regions"):
                    record["regions"] = old["regions"]
                    record["map_id"] = old.get("map_id")
                else:
                    map_list = await self.client.get_map_list(sn)
                    if isinstance(map_list, list) and map_list:
                        new_map_id = map_list[0].get("id")
                        if new_map_id != old.get("map_id") or not old.get("regions"):
                            regions = await self.client.get_map_regions(sn)
                            # Keep the full region dicts (id, name, points, ...).
                            # The camera platform needs the polygon geometry.
                            record["regions"] = [r for r in regions if isinstance(r, dict)]
                            record["map_id"] = new_map_id
                        else:
                            record["regions"] = old["regions"]
                            record["map_id"] = old["map_id"]
            except AiperError as exc:
                _LOGGER.debug("region fetch failed for %s: %s", sn, exc)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("region fetch error for %s: %s", sn, exc)

            merged[sn] = record
        return merged

    async def async_relogin(self) -> None:
        """Public so user-initiated actions can call it. Re-acquires the JWT
        and persists it. WARNING: this will kick any other active session
        (e.g. the mobile app) — only call when the user explicitly asks."""
        result = await self.client.login(
            self.entry.data[CONF_EMAIL], self.entry.data[CONF_PASSWORD]
        )
        new_data = {
            **self.entry.data,
            CONF_TOKEN: result.token,
            CONF_API_BASE: result.api_base,
        }
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        self._session_invalid = False
        _LOGGER.info("Aiper REST session reclaimed (api_base=%s)", result.api_base)

    async def async_user_action_with_reauth(self, fn) -> Any:
        """Run a user-triggered REST call; on 401/402 reauth once + retry."""
        try:
            return await fn()
        except AiperAuthError as exc:
            _LOGGER.info("user action hit auth fail (%s) — reclaiming session", exc)
            await self.async_relogin()
            return await fn()

    # ---- MQTT lifecycle + dispatch ----
    async def async_start_mqtt(self) -> None:
        """Start the MQTT client and subscribe to every device's topics."""
        if self._ssl_context is None:
            # Building an SSL context loads the OS trust store and reads
            # certificate files synchronously — must run in an executor.
            self._ssl_context = await self.hass.async_add_executor_job(
                ssl.create_default_context
            )
        if self.mqtt is None:
            self.mqtt = AiperMqttClient(
                self.client,
                on_message=self._on_mqtt_message,
                ssl_context=self._ssl_context,
                on_publish=lambda topic, payload: self.async_capture_publish(
                    "SEND", topic, payload
                ),
            )
        await self.mqtt.start()
        for sn in self.data:
            for topic in (
                f"$aws/things/{sn}/shadow/get/accepted",
                f"$aws/things/{sn}/shadow/update/accepted",
                f"$aws/things/{sn}/shadow/update/documents",
                f"aiper/things/{sn}/+",
                f"aiper/things/{sn}/+/+",
                f"aiper/things/{sn}/+/+/+",
            ):
                try:
                    await self.mqtt.subscribe(topic)
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug("subscribe %s failed: %s", topic, exc)
            try:
                await self.mqtt.request_shadow_get(sn)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("shadow GET %s failed: %s", sn, exc)

    async def async_stop_mqtt(self) -> None:
        if self.mqtt is not None:
            await self.mqtt.stop()

    async def _on_mqtt_message(self, topic: str, payload: Any) -> None:
        """Merge a shadow / device-report message into coordinator.data."""
        # Capture to file first (sync small write off the event loop is fine
        # for a few KB once in a while; HA's loop guard tolerates this size).
        if self.capture_enabled:
            try:
                await self.hass.async_add_executor_job(
                    self._capture_write, "RECV", topic, payload
                )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("capture write failed: %s", exc)
        if not isinstance(payload, dict):
            return
        sn = self._serial_from_topic(topic)
        if sn is None or sn not in self.data:
            return
        record = dict(self.data[sn])
        # Shadow envelope: state.reported.{NetStat,OpInfo,AlarmReport,...}
        reported = (
            payload.get("state", {}).get("reported")
            if "state" in payload
            else (
                payload.get("current", {}).get("state", {}).get("reported")
                if "current" in payload
                else payload  # device-direct report on aiper/things/<sn>/...
            )
        )
        if not isinstance(reported, dict):
            return
        # Merge the live fields we know about.
        if "NetStat" in reported and isinstance(reported["NetStat"], dict):
            ns = reported["NetStat"]
            record["mqtt_online"] = bool(ns.get("online"))
            record["mqtt_ble"] = ns.get("ble")
            record["mqtt_sta"] = ns.get("sta")
            record["mqtt_cert"] = ns.get("cert")
            record["mqtt_near_field_bind"] = ns.get("nearFieldBind")
        if "OpInfo" in reported and isinstance(reported["OpInfo"], dict):
            op = reported["OpInfo"]
            if "wifi_name" in op:
                record["wifiName"] = op["wifi_name"]
            if "wifi_rssi" in op:
                record["wifiRssi"] = op["wifi_rssi"]
        if "AlarmReport" in reported and isinstance(reported["AlarmReport"], dict):
            ar = reported["AlarmReport"]
            raw_codes = ar.get("code")
            codes = list(raw_codes) if isinstance(raw_codes, list) else []
            record["alarm_codes"] = codes
            record["alarm_timestamp"] = ar.get("timestamp")
        # MachineStatus / WorkInfo / WorkMode etc. — preserve raw for debugging
        # so we can iterate without redeploying.
        for key in ("MachineStatus", "WorkInfo", "WorkMode", "WaterYield"):
            if key in reported:
                record[f"mqtt_{key}"] = reported[key]
        self.data[sn] = record
        self.async_set_updated_data(self.data)

    @staticmethod
    def _serial_from_topic(topic: str) -> str | None:
        """Extract `<sn>` from `$aws/things/<sn>/...` or `aiper/things/<sn>/...`."""
        parts = topic.split("/")
        try:
            i = parts.index("things")
        except ValueError:
            return None
        if i + 1 < len(parts):
            return parts[i + 1] or None
        return None

    # ---- capture helpers ----
    def _capture_write(self, direction: str, topic: str, payload: Any) -> None:
        """Append one JSON-Lines record to the capture file. Roll over at cap."""
        try:
            if self._capture_path.exists() and self._capture_path.stat().st_size > self._capture_max_bytes:
                # Trim: keep the last 50% of the file.
                data = self._capture_path.read_bytes()
                cut = len(data) // 2
                # Cut at next newline so we don't break a record.
                nl = data.find(b"\n", cut)
                if nl != -1:
                    data = data[nl + 1 :]
                self._capture_path.write_bytes(data)
            with self._capture_path.open("a") as f:
                f.write(
                    json.dumps(
                        {
                            "ts": time.time(),
                            "dir": direction,  # RECV or SEND
                            "topic": topic,
                            "payload": payload,
                        },
                        default=str,
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception:  # noqa: BLE001
            # Never crash the coordinator on capture errors.
            _LOGGER.debug("capture log write failed", exc_info=True)

    async def async_capture_publish(
        self, direction: str, topic: str, payload: Any
    ) -> None:
        """Record a publish event in the capture file (call from publish helpers)."""
        if not self.capture_enabled:
            return
        try:
            await self.hass.async_add_executor_job(
                self._capture_write, direction, topic, payload
            )
        except Exception:  # noqa: BLE001
            pass

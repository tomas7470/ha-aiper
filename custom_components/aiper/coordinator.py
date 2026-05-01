"""DataUpdateCoordinator for the Aiper integration."""

from __future__ import annotations

import logging
from datetime import timedelta
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
            # Slower poll once MQTT is up (it carries live state); REST poll
            # is just a safety net + for things MQTT doesn't expose.
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL * 6),
        )
        self.entry = entry
        self.client = AiperClient(
            session=async_get_clientsession(hass),
            region=entry.data[CONF_REGION],
            api_base=entry.data.get(CONF_API_BASE),
            token=entry.data.get(CONF_TOKEN),
        )
        self.mqtt = AiperMqttClient(self.client, on_message=self._on_mqtt_message)

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            devices = await self.client.list_equipment()
        except AiperAuthError as exc:
            # Token rejected — try one re-login, then surface for reauth.
            try:
                await self._async_relogin()
                devices = await self.client.list_equipment()
            except Exception as inner:  # noqa: BLE001
                raise UpdateFailed(f"reauth failed: {inner}") from inner
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

    async def _async_relogin(self) -> None:
        result = await self.client.login(
            self.entry.data[CONF_EMAIL], self.entry.data[CONF_PASSWORD]
        )
        new_data = {
            **self.entry.data,
            CONF_TOKEN: result.token,
            CONF_API_BASE: result.api_base,
        }
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)

    # ---- MQTT lifecycle + dispatch ----
    async def async_start_mqtt(self) -> None:
        """Start the MQTT client and subscribe to every device's topics."""
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
        await self.mqtt.stop()

    async def _on_mqtt_message(self, topic: str, payload: Any) -> None:
        """Merge a shadow / device-report message into coordinator.data."""
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

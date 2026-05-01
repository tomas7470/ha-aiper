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
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.entry = entry
        self.client = AiperClient(
            session=async_get_clientsession(hass),
            region=entry.data[CONF_REGION],
            api_base=entry.data.get(CONF_API_BASE),
            token=entry.data.get(CONF_TOKEN),
        )

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

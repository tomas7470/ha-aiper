"""Aiper cloud HTTP client.

Wraps the AES+RSA envelope from `crypto.py` into typed methods that the
HA coordinator and config flow consume. Stays cleanly ignorant of HA: this
module talks raw `aiohttp` so the same client is reusable from CLI tests.

Endpoint inventory (locked in via static analysis of v2.3.7 and v3.3.0
smali under `com.aiper.base.data.http` and `com.aiper.device.i.*`):

  POST /login                                  email/password -> token + domain
  POST /equipment/list                         my devices
  POST /equipment/getEquipmentInfo             one device's full state
  POST /equipment/checkEquipmentExist
  POST /equipment/setName
  POST /equipment/unbundle
  POST /equipment/appTranspondServer           proxy device commands (cloud->device)
  POST /equipment/getAlarm
  POST /wr/addWateringTaskV2                   create watering task
  POST /wr/updateWateringTaskV2                edit task
  POST /wr/getWateringTaskListV2               list tasks
  POST /wr/local_task                          immediate run (manual)
  POST /wr/getWateringRecordHistoryDataV2      history
  POST /wr/getNozzleTypeSetting                + many other /wr/get* + /wr/update*

The exact JSON shapes for command payloads are partially known from the
prior research and partially TBD - we'll iterate as we observe server
responses against the real account.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from .const import APP_VERSION, REGION_BASES, REGION_FALLBACK_BASES, USER_AGENT
from .crypto import AiperCrypto

_LOGGER = logging.getLogger(__name__)


class AiperError(Exception):
    """Generic Aiper API failure."""


class AiperAuthError(AiperError):
    """Login failed or token rejected."""


class AiperRegionMismatch(AiperError):
    """Account exists in a different region than the one queried."""


@dataclass(slots=True)
class LoginResult:
    token: str
    api_base: str
    serial_number: str
    expires_in: int  # seconds from login until token expiry
    raw: dict[str, Any]


class AiperClient:
    """Thin async wrapper over Aiper's encrypted HTTP API.

    One client per Aiper account. Reuses a single AES session key for the
    lifetime of the instance — that mirrors the app's behaviour and keeps
    `encryptKey` rebuilds out of the hot path (RSA-PKCS1 encrypt is ~1ms).
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        region: str = "international",
        api_base: str | None = None,
        token: str | None = None,
        zone_id: str = "Europe/Berlin",
    ) -> None:
        self._session = session
        self._crypto = AiperCrypto(region=region)
        self._api_base = api_base or REGION_BASES[region]
        self._token = token
        self._zone_id = zone_id

    # ---- properties for the coordinator to persist ----
    @property
    def token(self) -> str | None:
        return self._token

    @property
    def api_base(self) -> str:
        return self._api_base

    # ---- low-level transport ----
    async def _post(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        """POST encrypted `payload` to `path`, decrypt response, return parsed JSON.

        Raises AiperAuthError on 401/expired-token, AiperRegionMismatch when
        server returns code 5050 ("Regional account does not exist").
        """
        body, headers = self._crypto.encrypt_request(payload or {})
        full_headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "version": APP_VERSION,
            "os": "android",
            "Accept-Language": "en",
            "zoneId": self._zone_id,
            "Connection": "keep-alive",
            **headers,
        }
        if self._token:
            full_headers["token"] = self._token

        url = f"{self._api_base}{path}"
        async with self._session.post(url, data=body, headers=full_headers) as resp:
            raw = await resp.read()
            if resp.status == 401:
                raise AiperAuthError(f"401 unauthorized at {path}")
            decoded = self._crypto.decrypt_response(raw)

        if not isinstance(decoded, dict):
            return decoded
        code = str(decoded.get("code", ""))
        if code == "5050":
            raise AiperRegionMismatch(decoded.get("message") or "regional account does not exist")
        # 402 = "your account is being used on another device" — Aiper rotates
        # JWTs on every fresh login; only one session per account is valid.
        # Treat it as auth failure so the coordinator transparently re-logins.
        if code == "402":
            raise AiperAuthError(decoded.get("message") or "session superseded")
        if not decoded.get("successful", code == "200"):
            # Log the full response at WARNING so we can iterate on undocumented
            # error codes by reading the HA log.
            _LOGGER.warning(
                "Aiper API error: path=%s code=%s message=%r full=%s",
                path, code, decoded.get("message"), decoded,
            )
            raise AiperError(f"{code}: {decoded.get('message') or '(no message)'} (path={path})")
        return decoded.get("data", decoded)

    # ---- public surface ----
    async def login(self, email: str, password: str) -> LoginResult:
        """Authenticate and capture token + canonical api_base.

        On a `5050 Regional account does not exist` response from the configured
        region, we fall through the REGION_FALLBACK_BASES list and re-try.
        """
        payload = {"email": email, "password": password}
        last_err: Exception | None = None

        candidates: list[str] = [self._api_base, *(b for b in REGION_FALLBACK_BASES if b != self._api_base)]

        for base in candidates:
            self._api_base = base
            try:
                data = await self._post("/login", payload)
            except AiperRegionMismatch as exc:
                last_err = exc
                _LOGGER.debug("Account not in %s, trying next region", base)
                continue
            except AiperError as exc:
                # Not a region issue — bad password, locked account, etc.
                raise AiperAuthError(str(exc)) from exc
            break
        else:
            raise AiperAuthError(str(last_err) if last_err else "login failed in all regions")

        token = data["token"]
        domain = data.get("domain") or [self._api_base]
        # `domain` is a JSON array; first entry is canonical for this account.
        api_base = domain[0].rstrip("/")
        self._token = token
        self._api_base = api_base
        return LoginResult(
            token=token,
            api_base=api_base,
            serial_number=data.get("serialNumber", ""),
            expires_in=int(data.get("tokenExpires", 0)),
            raw=data,
        )

    async def get_family_all_info(self) -> list[dict[str, Any]]:
        """Return all families with their nested places + equipments.

        Smali: DeviceApi.getFamilyAllInfo() takes no body params; response is
        BaseResp<List<FamilyData>>. This is the entry point for device discovery.
        """
        data = await self._post("/family/v1/getFamilyAllInfo")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # Sometimes wrapped in {"list": [...]} or similar
            for v in data.values():
                if isinstance(v, list):
                    return v
        return []

    async def get_family_management(self, family_id: int) -> dict[str, Any]:
        """Per-family detail (places, statistics). Smali: takes a long familyId."""
        return await self._post("/family/v1/getFamilyManagement", {"familyId": family_id})

    async def list_equipment(self) -> list[dict[str, Any]]:
        """Flatten all families/places into a flat, deduped list of devices.

        getFamilyAllInfo returns a tree where the same device can appear under
        multiple keys — we de-dupe by serial number.
        """
        family = await self.get_family_all_info()
        seen: dict[str, dict[str, Any]] = {}

        def _walk(node: Any) -> None:
            if isinstance(node, dict):
                if isinstance(node.get("sn"), str) and (
                    "deviceModel" in node or "bleName" in node
                ):
                    sn = node["sn"]
                    # First occurrence wins; later ones often have less detail.
                    seen.setdefault(sn, node)
                for v in node.values():
                    _walk(v)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)

        _walk(family)
        return list(seen.values())

    async def get_equipment_info(self, serial: str) -> dict[str, Any]:
        """Generic device info — works for most devices.

        IrriSense WR / 2.0 has its own richer variant at /wr/getEquipmentInfo;
        we try that first and fall back to the generic one.
        """
        try:
            return await self._post("/wr/getEquipmentInfo", {"sn": serial})
        except AiperError:
            return await self._post("/equipment/getEquipmentInfo", {"sn": serial})

    async def check_equipment_exists(self, serial: str) -> dict[str, Any]:
        return await self._post("/equipment/checkEquipmentExist", {"sn": serial})

    async def set_name(self, serial: str, name: str) -> dict[str, Any]:
        return await self._post("/equipment/setName", {"sn": serial, "name": name})

    # --- IrriSense WR / 2.0 commands ---

    async def list_watering_tasks(self, serial: str) -> Any:
        return await self._post("/wr/getWateringTaskListV2", {"sn": serial})

    async def get_map_list(self, serial: str) -> Any:
        return await self._post("/wr/getMapList", {"sn": serial})

    async def get_map_doc(self, serial: str) -> dict[str, Any] | None:
        """Fetch the first map's full JSON (regions + geometry).

        getMapList returns presigned S3 URLs (1-hour TTL); we follow the
        first one and parse the body. Returns None if the device has no
        saved map.
        """
        maps = await self.get_map_list(serial)
        if not (isinstance(maps, list) and maps and isinstance(maps[0], dict)):
            return None
        url = maps[0].get("mapUrl")
        if not url:
            return None
        async with self._session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def get_map_regions(self, serial: str) -> list[dict[str, Any]]:
        """Return the list of regions (zones) defined on the device's map.

        Each region has at least `id` (int) and `name` (str). Empty list if
        the device has no map.
        """
        doc = await self.get_map_doc(serial)
        if not isinstance(doc, dict):
            return []
        regions = doc.get("regions")
        return regions if isinstance(regions, list) else []

    async def add_watering_task(
        self,
        serial: str,
        *,
        first_execute_ts_sec: int,
        map_id: int,
        region_id: int = 0,
        plan_id: int = 0,
        depth_mm: float | None = None,
        duration_min: int | None = None,
        enabled: int = 1,
        repeat_days: str = "",
        repeat_type: int = 0,  # 0 = run once
        start_time: str = "00:00",
        work_type: int = 0,  # 0 = water (vs 1 = pesticide)
    ) -> Any:
        """Create a watering task — used both for scheduling and one-shot manual runs.

        Param names + types come from @JsonKey annotations on
        `WrApi.addIrrisenseTask` in v3.3.0. Both `depth` (Float) and `duration`
        (Integer) are nullable in the schema; pass exactly one. The IrriSense
        2.0 UI is depth-based (3/6/12 mm presets); the older WR may accept
        either.

        For "run now":
            * `repeat_type=0` (one-time)
            * `first_execute_ts_sec=int(time.time()) + 10` (start in ~10s)
            * either `depth_mm=N.N` or `duration_min=N` (not both)
        """
        if depth_mm is None and duration_min is None:
            raise ValueError("Pass either depth_mm or duration_min")
        if depth_mm is not None and duration_min is not None:
            raise ValueError("Pass depth_mm OR duration_min, not both")

        payload: dict[str, Any] = {
            "sn": serial,
            "enabled": enabled,
            "estimatedDuration": duration_min or 0,
            "firstExecuteUtcTimestampSecond": first_execute_ts_sec,
            "mapId": map_id,
            "planId": plan_id,
            "regionId": region_id,
            "repeatDays": repeat_days,
            "repeatType": repeat_type,
            "startTime": start_time,
            "workType": work_type,
        }
        if depth_mm is not None:
            payload["depth"] = depth_mm
        if duration_min is not None:
            payload["duration"] = duration_min
        return await self._post("/wr/addWateringTaskV2", payload)

    async def update_watering_task(self, serial: str, task: dict[str, Any]) -> Any:
        return await self._post("/wr/updateWateringTaskV2", {"sn": serial, **task})

    async def delete_watering_task(self, task_id: int) -> Any:
        return await self._post("/wr/deleteWateringTaskById", {"id": task_id})

    async def batch_set_tasks_enabled(self, serial: str, enabled: bool, task_ids: list[int]) -> Any:
        return await self._post(
            "/wr/batchUpdateWrWateringTaskEnabledV2",
            {"sn": serial, "enabled": 1 if enabled else 0, "ids": task_ids},
        )

    # --- Generic command channel (for shadow-style commands) ---
    async def transpond_to_device(self, serial: str, command: dict[str, Any]) -> dict[str, Any]:
        """Send a command via /equipment/appTranspondServer (cloud relays to device)."""
        return await self._post(
            "/equipment/appTranspondServer", {"sn": serial, "command": command}
        )

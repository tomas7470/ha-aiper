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
        if not decoded.get("successful", code == "200"):
            raise AiperError(f"{code}: {decoded.get('message')} (path={path})")
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

    async def list_watering_tasks(self, serial: str) -> dict[str, Any]:
        return await self._post("/wr/getWateringTaskListV2", {"sn": serial})

    async def add_watering_task(self, serial: str, task: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/wr/addWateringTaskV2", {"sn": serial, **task})

    async def update_watering_task(self, serial: str, task: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/wr/updateWateringTaskV2", {"sn": serial, **task})

    async def manual_run(self, serial: str, *, duration_min: int, region_id: int | None = None) -> dict[str, Any]:
        """Start a one-off manual watering. Endpoint shape from /wr/local_task in smali.

        TODO(phase-0-iter): the exact field names ("duration"? "minutes"? "regionId"?)
        will be confirmed empirically; the server's 4xx responses are descriptive.
        """
        payload: dict[str, Any] = {"sn": serial, "duration": duration_min}
        if region_id is not None:
            payload["regionId"] = region_id
        return await self._post("/wr/local_task", payload)

    async def stop_run(self, serial: str) -> dict[str, Any]:
        # Empirical — likely a flag in /wr/local_task or a separate endpoint
        return await self._post("/wr/local_task", {"sn": serial, "stop": True})

    # --- Generic command channel (for shadow-style commands) ---
    async def transpond_to_device(self, serial: str, command: dict[str, Any]) -> dict[str, Any]:
        """Send a command via /equipment/appTranspondServer (cloud relays to device)."""
        return await self._post(
            "/equipment/appTranspondServer", {"sn": serial, "command": command}
        )

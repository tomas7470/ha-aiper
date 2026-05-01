"""Aiper MQTT client — AWS IoT MQTT-over-WebSocket with Cognito SigV4 auth.

The device's live state (alarms, machine status, network info) lives only on
the AWS IoT device shadow; the REST API is for CRUD on schedules. This
client gets live updates and lets us publish shadow `desired` updates to
control the device.

Connection flow:
  1. Aiper REST `/users/getOpenIdToken` returns OpenID JWT + Cognito identity
     pool ID + iot endpoint + region.
  2. Cognito `GetCredentialsForIdentity` exchanges JWT for AWS temp creds.
  3. We sign a `wss://<endpoint>/mqtt` URL with SigV4 (service
     `iotdevicegateway`, security token appended after signing per AWS IoT
     convention).
  4. Open WSS subprotocol `mqtt`, send raw MQTT v3.1.1 frames. The
     `client_id` MUST be the Cognito `identityId` — the IoT policy enforces
     that pattern.

We implement MQTT v3.1.1 manually (no paho/aiomqtt) because both libraries'
WebSocket framing falls afoul of AWS IoT's exact handshake expectations.
The packet types we need are tiny: CONNECT/CONNACK, SUBSCRIBE/SUBACK,
PUBLISH (QoS 0 only), PINGREQ/PINGRESP, DISCONNECT.

Token refresh: the OpenID token has a `tokenDuration` (currently 86400s);
AWS Cognito creds expire ~1h. We refresh both before expiry and rebuild
the WS URL on reconnect.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as _dt
import hashlib
import hmac
import json
import logging
import ssl
import struct
import urllib.parse
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
import websockets

from .api import AiperClient

_LOGGER = logging.getLogger(__name__)


# ---- MQTT v3.1.1 constants ----
_CONNECT = 0x10
_CONNACK = 0x20
_PUBLISH = 0x30
_PUBACK = 0x40
_SUBSCRIBE = 0x82
_SUBACK = 0x90
_PINGREQ = 0xC0
_PINGRESP = 0xD0
_DISCONNECT = 0xE0


# ---- packet codecs ----
def _enc_remaining(length: int) -> bytes:
    out = bytearray()
    while True:
        b = length & 0x7F
        length >>= 7
        if length > 0:
            b |= 0x80
        out.append(b)
        if length == 0:
            return bytes(out)


def _dec_remaining(data: bytes, offset: int) -> tuple[int, int]:
    multiplier = 1
    value = 0
    while True:
        b = data[offset]
        offset += 1
        value += (b & 0x7F) * multiplier
        if b & 0x80 == 0:
            return value, offset
        multiplier *= 128


def _utf8(s: str) -> bytes:
    encoded = s.encode("utf-8")
    return struct.pack(">H", len(encoded)) + encoded


def _build_connect(client_id: str, keepalive: int = 60) -> bytes:
    body = (
        _utf8("MQTT")           # protocol name
        + bytes([4])            # protocol level (3.1.1)
        + bytes([0x02])         # flags: clean session
        + struct.pack(">H", keepalive)
        + _utf8(client_id)
    )
    return bytes([_CONNECT]) + _enc_remaining(len(body)) + body


def _build_subscribe(packet_id: int, topic: str, qos: int = 0) -> bytes:
    body = struct.pack(">H", packet_id) + _utf8(topic) + bytes([qos])
    return bytes([_SUBSCRIBE]) + _enc_remaining(len(body)) + body


def _build_publish(topic: str, payload: bytes, qos: int = 0, packet_id: int = 1) -> bytes:
    if qos == 1:
        # PUBLISH | QoS 1 = 0x32. QoS 1 needs a 2-byte packet identifier.
        body = _utf8(topic) + struct.pack(">H", packet_id) + payload
        return bytes([0x32]) + _enc_remaining(len(body)) + body
    body = _utf8(topic) + payload
    return bytes([_PUBLISH]) + _enc_remaining(len(body)) + body


# ---- Aiper command envelope (X9 format, PLAIN JSON, no encryption) ----
# Confirmed via Frida hook on the live v3.3.0 app, captured while the user
# tapped Start watering and the device DID start:
#   topic   : aiper/things/<sn>/downChan
#   payload : {"<cmdName>": <body>}            # X9 format, cmd name is the KEY
#   QoS     : 1                                 # AWSIotMqttManager.publishString QOS1
# isEncrypt is FALSE for all cmds via MqttDeviceManager.publish, so the JSON
# is sent in cleartext over the (TLS-protected) MQTT-WSS connection.
# Field order matters — Python preserves insertion order in 3.7+, so build
# the dict with keys in the order the app uses (mode, waterYield, map_id, status).


def aiper_envelope(cmd_name: str, body: dict[str, Any] | None = None) -> bytes:
    """Build the wire payload the device's firmware expects for one cmd."""
    inner = {cmd_name: body if body is not None else {}}
    return json.dumps(inner, separators=(",", ":")).encode("utf-8")


def _parse_publish(data: bytes) -> tuple[str, bytes]:
    """Parse PUBLISH variable header + payload (data starts after fixed header)."""
    topic_len = struct.unpack(">H", data[:2])[0]
    topic = data[2 : 2 + topic_len].decode("utf-8", errors="replace")
    return topic, bytes(data[2 + topic_len :])


# ---- SigV4 for AWS IoT MQTT-over-WebSocket ----
def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sigkey(secret_key: str, date: str, region: str, service: str) -> bytes:
    k = _hmac(("AWS4" + secret_key).encode("utf-8"), date)
    k = _hmac(k, region)
    k = _hmac(k, service)
    return _hmac(k, "aws4_request")


def _build_iot_ws_url(
    endpoint: str,
    region: str,
    access_key: str,
    secret_key: str,
    session_token: str,
) -> str:
    """Pre-signed wss:// URL for AWS IoT MQTT.

    AWS IoT WebSocket convention: sign WITHOUT the security token, then
    append it AFTER the signature.
    """
    service = "iotdevicegateway"
    now = _dt.datetime.now(_dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    algorithm = "AWS4-HMAC-SHA256"
    credential_scope = f"{date}/{region}/{service}/aws4_request"
    canonical_uri = "/mqtt"

    qparams = {
        "X-Amz-Algorithm": algorithm,
        "X-Amz-Credential": f"{access_key}/{credential_scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": "86400",
        "X-Amz-SignedHeaders": "host",
    }
    canonical_qs = "&".join(
        f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
        for k, v in sorted(qparams.items())
    )
    canonical_request = "\n".join(
        [
            "GET",
            canonical_uri,
            canonical_qs,
            f"host:{endpoint}\n",
            "host",
            hashlib.sha256(b"").hexdigest(),
        ]
    )
    string_to_sign = "\n".join(
        [
            algorithm,
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )
    signing_key = _sigkey(secret_key, date, region, service)
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    full_qs = (
        canonical_qs
        + f"&X-Amz-Signature={signature}"
        + f"&X-Amz-Security-Token={urllib.parse.quote(session_token, safe='-_.~')}"
    )
    return f"wss://{endpoint}{canonical_uri}?{full_qs}"


# ---- Cognito token-for-creds exchange ----
async def _cognito_get_credentials(
    session: aiohttp.ClientSession,
    region: str,
    identity_id: str,
    token: str,
) -> dict[str, Any]:
    url = f"https://cognito-identity.{region}.amazonaws.com/"
    headers = {
        "Content-Type": "application/x-amz-json-1.1",
        "X-Amz-Target": "AWSCognitoIdentityService.GetCredentialsForIdentity",
    }
    body = {
        "IdentityId": identity_id,
        "Logins": {"cognito-identity.amazonaws.com": token},
    }
    async with session.post(url, json=body, headers=headers) as resp:
        text = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"Cognito {resp.status}: {text}")
        return json.loads(text)["Credentials"]


# ---- public client ----
MessageHandler = Callable[[str, dict[str, Any] | bytes], Awaitable[None]]


class AiperMqttClient:
    """Maintains a long-lived MQTT-over-WSS connection to AWS IoT.

    Usage:
        client = AiperMqttClient(api_client, on_message=handler)
        await client.start()
        await client.subscribe("aiper/things/WRX.../#")
        await client.publish_shadow_desired("WRX...", {"Watering": {"command": "stop"}})
        ...
        await client.stop()
    """

    def __init__(
        self,
        api_client: AiperClient,
        on_message: MessageHandler,
        *,
        ssl_context: ssl.SSLContext,
        on_publish: Callable[[str, Any], Awaitable[None]] | None = None,
        keepalive: int = 60,
    ) -> None:
        self._api = api_client
        self._on_message = on_message
        self._on_publish = on_publish
        self._ssl_context = ssl_context
        self._keepalive = keepalive
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._task: asyncio.Task[None] | None = None
        self._packet_id = 0
        self._subscriptions: set[str] = set()
        self._connected = asyncio.Event()
        self._stop_evt = asyncio.Event()
        self._lock = asyncio.Lock()  # serialize WS sends

    # ---- lifecycle ----
    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_evt.clear()
        self._task = asyncio.create_task(self._run(), name="aiper-mqtt")
        # Wait briefly for first connect; don't block forever.
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=15)
        except asyncio.TimeoutError:
            _LOGGER.warning("MQTT first connect didn't finish in 15s; will keep retrying in background")

    async def stop(self) -> None:
        self._stop_evt.set()
        if self._ws is not None:
            try:
                await self._ws.send(bytes([_DISCONNECT, 0]))
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._connected.is_set()

    async def subscribe(self, topic: str, qos: int = 0) -> None:
        self._subscriptions.add(topic)
        if self.is_connected and self._ws is not None:
            self._packet_id = (self._packet_id % 65535) + 1
            async with self._lock:
                await self._ws.send(_build_subscribe(self._packet_id, topic, qos))

    async def publish(self, topic: str, payload: dict[str, Any] | bytes | str) -> None:
        if not self.is_connected or self._ws is None:
            raise RuntimeError("MQTT not connected")
        if isinstance(payload, dict):
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        elif isinstance(payload, str):
            data = payload.encode("utf-8")
        else:
            data = payload
        async with self._lock:
            await self._ws.send(_build_publish(topic, data))
        # Notify capture sink (if any). We pass the parsed JSON form when
        # possible so the JSONL stays human-readable.
        if self._on_publish is not None:
            try:
                pretty: Any
                if isinstance(payload, dict):
                    pretty = payload
                elif isinstance(payload, (bytes, bytearray)):
                    try:
                        pretty = json.loads(payload)
                    except Exception:  # noqa: BLE001
                        pretty = payload.decode("utf-8", errors="replace")
                else:
                    pretty = payload
                await self._on_publish(topic, pretty)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("on_publish capture failed", exc_info=True)

    async def publish_aiper_cmd(
        self, serial: str, cmd_name: str, body: dict[str, Any] | None = None
    ) -> None:
        """Publish a device-cmd to `aiper/things/<sn>/downChan` using the
        XOR-encrypted X9 envelope the v3.3.0 app uses (QoS 1).

        Encryption format reverse-engineered + Frida-confirmed against the
        live app: `base64(xor(json({cmd_name: body}), 0x12345678)) + "\\n"`.
        """
        if not self.is_connected or self._ws is None:
            raise RuntimeError("MQTT not connected")
        topic = f"aiper/things/{serial}/downChan"
        payload = aiper_envelope(cmd_name, body)
        self._packet_id = (self._packet_id % 65535) + 1
        pid = self._packet_id
        async with self._lock:
            await self._ws.send(_build_publish(topic, payload, qos=1, packet_id=pid))
        _LOGGER.info("aiper cmd publish: sn=%s cmd=%s body=%s pid=%s",
                     serial, cmd_name, body, pid)
        # Capture-sink mirror.
        if self._on_publish is not None:
            try:
                pretty = {cmd_name: body if body is not None else {}}
                await self._on_publish(topic, pretty)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("on_publish capture failed", exc_info=True)

    async def publish_shadow_desired(self, serial: str, desired: dict[str, Any]) -> None:
        await self.publish(
            f"$aws/things/{serial}/shadow/update",
            {"state": {"desired": desired}},
        )

    async def request_shadow_get(self, serial: str) -> None:
        await self.publish(f"$aws/things/{serial}/shadow/get", b"")

    # ---- connection management ----
    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop_evt.is_set():
            try:
                await self._connect_once()
                backoff = 1.0  # successful run, reset
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("Aiper MQTT disconnected: %s — retrying in %.1fs", exc, backoff)
            finally:
                self._connected.clear()
                self._ws = None
            try:
                await asyncio.wait_for(self._stop_evt.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60)

    async def _connect_once(self) -> None:
        # Use the AiperClient's session (don't context-manage it — closing
        # would break the REST client).
        session = self._api._session  # noqa: SLF001
        oid = await self._api._post("/users/getOpenIdToken", {})  # noqa: SLF001
        creds = await _cognito_get_credentials(
            session, oid["region"], oid["identityId"], oid["token"]
        )
        client_id = oid["identityId"]
        ws_url = _build_iot_ws_url(
            endpoint=oid["iotEndpoint"],
            region=oid["region"],
            access_key=creds["AccessKeyId"],
            secret_key=creds["SecretKey"],
            session_token=creds["SessionToken"],
        )
        # WebSocket-level ping_interval=None: AWS doesn't reply to WS pings.
        # We send MQTT PINGREQ at the protocol level instead.
        # ssl context is pre-built (off-loop) by the caller — passing it
        # explicitly avoids websockets/python triggering blocking
        # load_default_certs / set_default_verify_paths inside the event loop.
        async with websockets.connect(
            ws_url,
            subprotocols=["mqtt"],  # type: ignore[list-item]
            max_size=2**20,
            ping_interval=None,
            ping_timeout=None,
            open_timeout=20,
            ssl=self._ssl_context,
        ) as ws:
            self._ws = ws
            await ws.send(_build_connect(client_id, self._keepalive))
            connack = await asyncio.wait_for(ws.recv(), timeout=10)
            connack = bytes(connack) if isinstance(connack, (bytes, bytearray)) else b""
            if not connack or connack[0] != _CONNACK or len(connack) < 4 or connack[3] != 0:
                raise RuntimeError(f"MQTT CONNECT rejected: {connack.hex() if connack else 'no data'}")
            _LOGGER.info("Aiper MQTT connected (client_id=%s)", client_id)
            # Re-subscribe everything previously requested
            for topic in list(self._subscriptions):
                self._packet_id = (self._packet_id % 65535) + 1
                await ws.send(_build_subscribe(self._packet_id, topic))
            self._connected.set()

            # ping + receive loop
            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                await self._recv_loop(ws)
            finally:
                ping_task.cancel()
                try:
                    await ping_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    async def _ping_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        try:
            while True:
                await asyncio.sleep(self._keepalive * 0.6)  # well under keepalive window
                async with self._lock:
                    await ws.send(bytes([_PINGREQ, 0]))
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("ping loop ended: %s", exc)

    async def _recv_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        async for frame in ws:
            if not isinstance(frame, (bytes, bytearray)):
                continue
            data = bytes(frame)
            if not data:
                continue
            ptype = data[0] & 0xF0
            if ptype == _PUBLISH:
                _, hdr_end = _dec_remaining(data, 1)
                topic, payload = _parse_publish(data[hdr_end:])
                try:
                    parsed: dict[str, Any] | bytes = json.loads(payload)
                except Exception:  # noqa: BLE001
                    parsed = payload
                try:
                    await self._on_message(topic, parsed)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("on_message handler failed for %s", topic)
            elif ptype == _PINGRESP:
                continue  # heartbeat
            elif ptype == _SUBACK:
                continue  # ack — we don't track ids tightly
            elif ptype == _PUBACK:
                continue
            else:
                _LOGGER.debug("unhandled MQTT packet 0x%02x", data[0])

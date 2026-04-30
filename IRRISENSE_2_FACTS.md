# IrriSense 2.0 / WR — observed facts

Authoritative data captured live against the real account on `apieurope.aiper.com`
using v3.3.0 of the Android app's reverse-engineered envelope. This is the
ground truth for the integration's models and tests; update when the API drifts.

## Cloud envelope

* Base URL — comes from the login response's `data.domain[0]`. For this account: `https://apieurope.aiper.com`.
* Encryption: AES-128-CBC + ZeroBytePadding, key + IV are 16 random bytes each in ASCII range `0x28-0x7e` (`(`-`~`). Generated **per HTTP client** and reused for the lifetime of the process.
* Wire format: request body is `{"data": "<base64-of-AES(plaintext_with_nonce_and_timestamp_added)>"}`.
* `encryptKey` header: RSA-PKCS1 over `{"key":"...","iv":"..."}` with the international 1024-bit RSA pubkey (DER-encoded SubjectPublicKeyInfo, see [crypto.py](custom_components/aiper/crypto.py)).
* `requestIdKey` header: random 16 chars `[A-Za-z0-9!@#$%^&*()-_=+[]{}/]`.
* Auth: `token` header (NOT `Authorization`); JWT issued by `/login`.
* Other headers the iOS/Android apps send: `version: 3.3.0`, `os: android`, `zoneId: Europe/Berlin`, `Accept-Language: en`, `User-Agent: Aiper-Link-Android/3.3.0 ...`.

## Login

* `POST /login` — body `{"email","password"}` (+ envelope's nonce+timestamp).
* Response `data` contains:
    * `token` — JWT, 30 day expiry.
    * `tokenExpires` — seconds (e.g. `2592000`).
    * `serialNumber` — primary device SN; for this account `GS19Z4P05Q` (a non-IrriSense device, kept for backwards compat).
    * `domain` — array of canonical API base URLs to use for further calls; first entry wins.
* Error `code: "5050"` `message: "Regional account does not exist"` — try the other regional bases (`apiamerica.aiper.com`, `apichina.aiper.com`).

## Device discovery

* `POST /family/v1/getFamilyAllInfo` — empty body, returns `BaseResp<List<FamilyData>>`. This is the canonical entry point for device enumeration.
* The returned tree contains nested places + equipments; the same device can appear under multiple keys, so de-dup by `sn`.

### Device record shape (live)

```json
{
  "id": 1704379695729775902,
  "sn": "WRX60500946",
  "name": "IrriSense 2",
  "deviceModel": "IrriSense_2",
  "deviceType": "8",
  "deviceModelUrl": "https://prod-app-management-bucket.s3.ap-southeast-1.amazonaws.com/equipment/WR_1763951656035.png",
  "bleName": "Aiper-IrriSense_2-0500946",
  "version": "V3.8.7",
  "subver": "V3.8.5,V0.1.4,V0.0.27",
  "online": 1,
  "wifiName": "Home-Trusted",
  "wifiRssi": -58,
  "zoneId": "Europe/Berlin",
  "createTime": 1776783102000,
  "autoUpgrade": 1,
  "sceneTypeJoin": "2"
}
```

Notes:
* `deviceModel` for IrriSense 2.0 is `IrriSense_2` (NOT `IrriSense_2_Chromatic` or `IrriSense_2_SE` — those strings appear elsewhere in the APK as showroom names).
* `deviceModel` for the predecessor is `IrriSense_WR`. Both share `deviceType: "8"` and the same `/wr/*` API surface.
* Offline devices additionally include `machineStatus: 0` (running/idle code). For online devices that field is missing in the family tree — query `/wr/getEquipmentInfo` or shadow.

## Per-device state

* `POST /wr/getEquipmentInfo` — body `{"sn": "<serial>"}`, response example for the live IrriSense 2.0:

```json
{
  "sn": "WRX60500946",
  "model": "IrriSense_2",
  "bleName": "Aiper-IrriSense_2-0500946",
  "version": "V3.8.7",
  "subver": "V3.8.5,V0.1.4,V0.0.27",
  "mainFirmwareVersion": "V3.8.7",
  "mcuFirmwareVersion": "V3.8.5",
  "valveFirmwareVersion": "V0.0.27",
  "bluetoothFirmwareVersion": "V0.1.4",
  "displayOtaVersion": false
}
```

This response is firmware-only; **online/runtime state lives in the family tree** (`online`, `wifiRssi`, `wifiName`) plus, for offline devices, `machineStatus`.

## Tasks / schedules

* `POST /wr/getWateringTaskListV2` — body `{"sn": "<serial>"}`, returns the list of scheduled watering tasks. For the test device this is `[]`.
* `POST /wr/addWateringTaskV2` — schedule a new task (signature in [WrApi.smali](Downloads/aiper_v330/decompiled/smali_classes4/com/aiper/device/i/net/WrApi.smali) is `addIrrisenseTask(sn, ?, ?, int, int, long, long, long, long, str, int, str, int)` — full payload TBD).
* `POST /wr/updateWateringTaskV2` — edit task.
* `POST /wr/batchUpdateWrWateringTaskEnabledV2` — bulk enable/disable.
* `POST /wr/deleteWateringTaskById` — delete one.
* `POST /wr/batchDeleteWateringTaskV2` — bulk delete.

## Settings

* `POST /wr/getWateringSettingV2` — global watering policy.
* `POST /wr/getReminderSetting` — reminder preferences.
* `POST /wr/getNozzleTypeSetting` — current nozzle config.
* `POST /wr/getMapList` — Saved coverage maps (IrriSense 2.0 has map-based zones).

## Manual run / "start watering now"

**No dedicated REST endpoint** for one-shot manual runs. Two viable Phase-1 strategies:

1. Create a one-time task starting in the next minute via `/wr/addWateringTaskV2`, then delete after completion.
2. Defer to Phase 2: send the command directly via MQTT desired-state shadow update on `$aws/things/<sn>/shadow/update`.

Strategy (2) is cleaner once we wire AWS IoT MQTT, so the MVP **doesn't expose a `valve` entity yet** — only schedule management + observability.

## MQTT (Phase 2)

* AWS IoT endpoint, region: from prior research `cn-north-1` (Beijing); for the EU account it's likely a different regional endpoint — capture during Phase 2.
* Topic patterns (device-agnostic in v3.3.0; no `WR` segment any more):
    * `aiper/things/<sn>/app/cloud/upChan` — app → device
    * `aiper/things/<sn>/cloud/app/downChan` — device → app
    * `aiper/things/<sn>/cloudMessage/app/downChan` — alt downstream
    * `$aws/things/<sn>/shadow/get|update|update/accepted` — standard shadow
* Credential acquisition: TBD; the app must call a `/iot/...` style endpoint (or get creds inside `/family/v1/getFamilyAllInfo`) to obtain SigV4 temp creds or per-user X.509 cert + key. Capture and document during Phase 2.

## BLE (Phase 3)

* Advertising name: `Aiper-IrriSense_2-<last-7-of-serial>` (e.g. `Aiper-IrriSense_2-0500946`) for 2.0; `Aiper-IrriSense_WR-<last-5>` (e.g. `Aiper-IrriSense_WR-02921`) for WR.
* GATT service / characteristic UUIDs not yet recovered — needs HCI snoop or focused smali search at install time.

## Account on file

* Email: `tomas7470@gmail.com`
* Region: international (Europe).
* Devices:
    * `WRX60500946` — IrriSense 2.0, online, firmware `V3.8.7`, label "IrriSense 2", on Wi-Fi "Home-Trusted".
    * `WRX52102921` — IrriSense WR, offline, firmware `V0.5.29`, label "IrriSense East", last seen on Wi-Fi "FRITZ!Box_6591_Cable_DX".

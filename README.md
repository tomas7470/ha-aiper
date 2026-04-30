# Aiper IrriSense — Home Assistant integration

Cloud integration for Aiper smart-irrigation devices (IrriSense WR & IrriSense 2.0). MVP exposes status (online, firmware, Wi-Fi) — schedule + manual run land in later releases.

> Status: **early alpha (v0.1.0)**. Phase 1 only — read-only state via the Aiper cloud REST API. No control yet (manual run, schedule edit). MQTT push and BLE pairing are on the roadmap.

## Install (HACS, custom repo)

1. HACS → ⋮ → "Custom repositories" → add this repo URL → category Integration.
2. Install "Aiper IrriSense" → restart HA.
3. Settings → Devices & Services → Add integration → "Aiper IrriSense" → enter your Aiper account email + password + region.

## What you get today (v0.1.0)

Per device:
* `binary_sensor.aiper_<sn>_online`
* `binary_sensor.aiper_<sn>_auto_upgrade`
* `sensor.aiper_<sn>_firmware_version`
* `sensor.aiper_<sn>_mcu_firmware`, `valve_firmware`, `bluetooth_firmware`
* `sensor.aiper_<sn>_wifi_signal`, `wifi_network`
* `sensor.aiper_<sn>_machine_status` (only when reported)
* `button.aiper_<sn>_refresh` — force a coordinator poll.

The coordinator polls `/family/v1/getFamilyAllInfo` + `/wr/getEquipmentInfo` every 30 seconds.

## Roadmap

* **0.2** — schedule view + enable/disable. Add: `switch` per scheduled task.
* **0.3** — manual run via cloud-mediated command (creates a one-shot task).
* **0.4** — MQTT shadow push for ~real-time state. Switches `iot_class` to `cloud_push`.
* **0.5** — BLE provisioning (pair a fresh device through HA, no Aiper app required).

## Caveats

* The Aiper cloud uses static credentials per app session, but encrypts every request. We replicate the envelope cleanly — see [crypto.py](custom_components/aiper/crypto.py).
* All traffic flows through Aiper's cloud (`apieurope.aiper.com` for EU accounts, `apiamerica.aiper.com` for US, etc.). If their service is down, HA can't see/control the device.
* Aiper has not published a public API. This integration is built on reverse engineering. They may change endpoints or block third-party clients without warning.

## License

MIT.

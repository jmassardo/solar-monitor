#!/usr/bin/env python3
"""
Solar Monitor v2 — Home Assistant MQTT Service

Monitors solar equipment and publishes to Home Assistant via MQTT:
  • GoKWh 100Ah LiFePO4 Battery  (BLE, TDT protocol)
  • Renogy Wanderer 10A PWM       (RS232, Modbus — not yet implemented)
  • Solar Pi management            (BME280, door, fans — Pi-only)

Each component appears as a separate device in Home Assistant.

Usage:
    python3 solar_monitor_v2.py [--mqtt-host $MQTT_HOST] [--debug]
    python3 solar_monitor_v2.py --no-pi   # Mac testing (no GPIO/BME280)
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time

from bleak import BleakClient, BleakScanner
import paho.mqtt.client as mqtt

# Optional Pi-only hardware imports
try:
    import smbus2
    import bme280 as bme280_lib
    HAS_BME280 = True
except ImportError:
    HAS_BME280 = False

try:
    import gpiod
    from gpiod.line import Direction, Value, Bias
    HAS_GPIO = True
except ImportError:
    HAS_GPIO = False


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("solar-monitor")


# ══════════════════════════════════════════════════════════════════════
#  Configuration
# ══════════════════════════════════════════════════════════════════════

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")
DISCOVERY_PREFIX = "homeassistant"

# GoKWh BLE
BLE_SCAN_TIMEOUT = 15       # seconds
BLE_CONNECT_TIMEOUT = 20    # seconds
BLE_CMD_TIMEOUT = 5         # seconds to wait for each command response
BLE_POLL_INTERVAL = 60      # seconds between poll cycles
BLE_POLL_TIMEOUT = 45       # max seconds for entire poll (scan+connect+cmds)
BLE_DEVICE_NAME = "XDZN"   # BLE name prefix for GoKWh (TDT BMS)
BLE_RESET_THRESHOLD = 5     # consecutive fails before resetting BlueZ

# Pi management (active only when running on Pi with --no-pi omitted)
RELAY_PIN = 22              # BCM GPIO22 → relay module IN
DOOR_PIN = 27               # BCM GPIO27 → NC reed switch to GND
BME280_I2C_BUS = 3          # /dev/i2c-3 (software I²C on GPIO23/24)
BME280_I2C_ADDR = 0x76
BME280_READ_INTERVAL = 30   # seconds between BME280 reads
PI_POLL_INTERVAL = 2        # seconds between Pi sensor/door/fan updates
BATTERY_TEMP_TTL = 180      # ignore battery temps older than 3 min for fan ctrl
THERMAL_ON_C = 60.0
THERMAL_OFF_C = 55.0
KEEPALIVE_INTERVAL_S = 4 * 3600   # fan keep-alive pulse every 4h
KEEPALIVE_DURATION_S = 90         # 90s fan-on per keep-alive pulse


async def reset_bluez():
    """Escalating reset of the BlueZ adapter to clear wedged state.

    Level 1: btmgmt power cycle (clears stuck scans)
    Level 2: rfkill + bluetooth service restart (clears deeper hangs)
    Level 3: full system reboot (clears wedged UART/firmware)
    """

    async def _run(cmd, timeout=10):
        proc = await asyncio.create_subprocess_exec(
            *cmd.split(),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode == 0
        except asyncio.TimeoutError:
            proc.kill()
            return False

    async def _bt_is_up():
        proc = await asyncio.create_subprocess_exec(
            "hciconfig", "hci0",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        return b"UP RUNNING" in out

    # Level 1: btmgmt power cycle
    log.warning("BlueZ reset — level 1: btmgmt power cycle")
    await _run("btmgmt power off")
    await asyncio.sleep(1)
    await _run("btmgmt power on")
    await asyncio.sleep(2)
    if await _bt_is_up():
        log.info("BlueZ recovered (level 1)")
        return

    # Level 2: rfkill + bluetooth service restart
    log.warning("BlueZ reset — level 2: rfkill + service restart")
    await _run("rfkill block bluetooth")
    await asyncio.sleep(2)
    await _run("rfkill unblock bluetooth")
    await asyncio.sleep(2)
    await _run("systemctl restart bluetooth", timeout=15)
    await asyncio.sleep(3)
    await _run("hciconfig hci0 up")
    await asyncio.sleep(1)
    if await _bt_is_up():
        log.info("BlueZ recovered (level 2)")
        return

    # Level 3: reboot
    log.error("BlueZ unrecoverable — rebooting system")
    await _run("systemctl reboot")


# ══════════════════════════════════════════════════════════════════════
#  HA Device Definitions
# ══════════════════════════════════════════════════════════════════════

GOKWH_PREFIX = "solar_gokwh"
GOKWH_DEVICE = {
    "identifiers": ["solar_gokwh_battery"],
    "name": "GoKWh Battery",
    "manufacturer": "GoKWh",
    "model": "100Ah LiFePO4",
}

WANDERER_PREFIX = "solar_wanderer"
WANDERER_DEVICE = {
    "identifiers": ["solar_renogy_wanderer"],
    "name": "Renogy Wanderer",
    "manufacturer": "Renogy",
    "model": "Wanderer 10A PWM",
}

PI_PREFIX = "solar_pi"
PI_DEVICE = {
    "identifiers": ["solar_pi_management"],
    "name": "Solar Pi",
    "manufacturer": "Raspberry Pi",
    "model": "3 Model B+",
    "sw_version": "2.0.0",
}


# ══════════════════════════════════════════════════════════════════════
#  TDT BMS Protocol
# ══════════════════════════════════════════════════════════════════════
#
# Frame format (command):
#   HEAD VER 01 03 00 CMD LEN_HI LEN_LO [DATA] CRC_HI CRC_LO 0D
#
# Frame format (response):
#   7E VER 01 03 ERR CMD LEN_HI LEN_LO [PAYLOAD] CRC_HI CRC_LO 0D
#
# Auth: write b"HiLink" to char fffa, read back 0x01 = unlocked
# Commands: 0x8C (battery data), 0x8D (protection/MOSFETs), 0x92 (device info)
# HEAD alternatives: 0x7E (standard), 0x1E (some firmware variants)
# CRC: Modbus CRC-16 over all bytes before the 3-byte trailer

def crc_modbus(data: bytes) -> int:
    """Modbus CRC-16."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def tdt_cmd(cmd_id: int, head: int = 0x7E) -> bytes:
    """Build a TDT BMS command frame (no payload, read-only commands)."""
    frame = bytearray([head, 0x00, 0x01, 0x03, 0x00, cmd_id, 0x00, 0x00])
    frame += crc_modbus(frame).to_bytes(2, "big") + bytes([0x0D])
    return bytes(frame)


def validate_tdt_frame(frame: bytes) -> bool:
    """Validate a TDT BMS response frame (length, version, error, CRC)."""
    if len(frame) < 10 or frame[-1] != 0x0D:
        return False
    if frame[1] not in (0x00, 0x04):
        return False
    if frame[4] != 0x00:
        log.warning("BMS error code: 0x%02X", frame[4])
        return False
    expected = crc_modbus(frame[:-3])
    actual = int.from_bytes(frame[-3:-1], "big")
    if expected != actual:
        log.warning("CRC mismatch: 0x%04X != 0x%04X", expected, actual)
        return False
    return True


def decode_battery_data(msg_8c: bytes, msg_8d: bytes | None) -> dict:
    """
    Decode 0x8C (battery data) and optional 0x8D (status) responses.

    0x8C payload layout (starting at offset 8):
      [cell_count] [cell_1_hi cell_1_lo] ... [cell_N_hi cell_N_lo]
      [temp_count] [temp_1_hi temp_1_lo] ... [temp_M_hi temp_M_lo]
      [current_hi current_lo]       — bit 15 = sign, bits 0–13 = mag/10
      [voltage_hi voltage_lo]       — pack voltage × 100
      [capacity_hi capacity_lo]     — remaining Ah × 10
      [??] [??]
      [cycles_hi cycles_lo]
      [??] [??]
      [soc_hi soc_lo]
    """
    P = 8  # payload start ("cell position" in aiobmsble)

    # Cell voltages (millivolts)
    cell_count = msg_8c[P]
    cells = [int.from_bytes(msg_8c[P + 1 + i * 2 : P + 3 + i * 2], "big")
             for i in range(cell_count)]

    # Temperature sensors (raw = Kelvin × 10, offset by 2731)
    tp = P + cell_count * 2 + 1
    temp_count = msg_8c[tp]
    temps = [round((int.from_bytes(msg_8c[tp + 1 + i * 2 : tp + 3 + i * 2], "big")
                     - 2731) / 10, 1)
             for i in range(temp_count)]

    # Main fields (after variable-length cell + temp arrays)
    fs = P + (cell_count + temp_count) * 2 + 2

    i_raw = int.from_bytes(msg_8c[fs : fs + 2], "big")
    current = round((i_raw & 0x3FFF) / 10 * (-1 if i_raw >> 15 else 1), 1)
    voltage = round(int.from_bytes(msg_8c[fs + 2 : fs + 4], "big") / 100, 2)

    data = {
        "voltage": voltage,
        "current": current,
        "power": round(voltage * current, 1),
        "remaining_ah": round(int.from_bytes(msg_8c[fs + 4 : fs + 6], "big") / 10, 1),
        "cycles": int.from_bytes(msg_8c[fs + 8 : fs + 10], "big"),
        "soc": int.from_bytes(msg_8c[fs + 12 : fs + 14], "big"),
        "cell_count": cell_count,
        "cell_voltages": cells,
        "cell_delta": max(cells) - min(cells) if cells else 0,
        "temp_count": temp_count,
        "temperatures": temps,
    }

    # 0x8D: protection status and MOSFET switches
    if msg_8d and len(msg_8d) > P + (cell_count + temp_count) + 9:
        idx = cell_count + temp_count
        prob_off = P + idx + 6
        data["problem_code"] = int.from_bytes(
            msg_8d[prob_off : prob_off + 2], "big"
        )
        mosfets = msg_8d[P + idx + 8]
        data["charge_mosfet"] = bool(mosfets & 0x04)
        data["discharge_mosfet"] = bool(mosfets & 0x02)

    return data


def decode_device_info(msg_92: bytes) -> dict:
    """Decode 0x92 response — firmware version, manufacturer, serial."""
    def clean(b):
        return b.decode("ascii", errors="replace").strip("\x00").strip()
    return {
        "sw_version": clean(msg_92[8:28]),
        "manufacturer": clean(msg_92[28:48]),
        "serial_number": clean(msg_92[48:68]),
    }


# ══════════════════════════════════════════════════════════════════════
#  GoKWh BLE Battery Polling
# ══════════════════════════════════════════════════════════════════════
#
# Each poll is a full cycle: scan → connect → auth → commands → disconnect.
# The battery disconnects after ~30 s of inactivity, so with 60 s polls
# we'd lose the connection anyway.  Connect-per-poll is cleaner and
# avoids fighting with the GPKWD phone app for the BLE link.

UUID_FFF1 = "0000fff1-0000-1000-8000-00805f9b34fb"  # Notify (BMS → client)
UUID_FFF2 = "0000fff2-0000-1000-8000-00805f9b34fb"  # Write  (client → BMS)
UUID_FFFA = "0000fffa-0000-1000-8000-00805f9b34fb"  # Config / auth


async def poll_battery(
    name_prefix: str = BLE_DEVICE_NAME,
    mac: str | None = None,
) -> tuple[dict | None, dict | None]:
    """
    Full BLE poll cycle.  Returns (battery_data, device_info) on success,
    (None, None) on any failure.  Always disconnects before returning.
    """

    # ── Scan ──────────────────────────────────────────────────────
    log.debug("BLE scan (%d s)…", BLE_SCAN_TIMEOUT)
    devices = await BleakScanner.discover(
        timeout=BLE_SCAN_TIMEOUT, return_adv=True,
    )
    device = None
    for addr, (dev, adv) in devices.items():
        name = adv.local_name or dev.name or ""
        if mac and addr.upper() == mac.upper():
            device = dev
            break
        if name.upper().startswith(name_prefix.upper()):
            device = dev
            break

    if not device:
        log.warning("Battery not found in BLE scan")
        return None, None

    log.debug("Found %s (%s)", device.name or "?", device.address)

    # ── Connect ───────────────────────────────────────────────────
    client = BleakClient(device, timeout=BLE_CONNECT_TIMEOUT)
    try:
        await client.connect()
    except Exception as e:
        log.error("BLE connect failed: %s", e)
        return None, None

    try:
        # Grab characteristic objects right away (bleak macOS cache quirk)
        svcs = client.services
        await asyncio.sleep(0.5)

        write_char = svcs.get_characteristic(UUID_FFF2)
        notify_char = svcs.get_characteristic(UUID_FFF1)
        cfg_char = svcs.get_characteristic(UUID_FFFA)
        if not all([write_char, notify_char, cfg_char]):
            log.error("Missing required BLE characteristics")
            return None, None

        # ── Auth handshake ────────────────────────────────────────
        await client.write_gatt_char(cfg_char, b"HiLink", response=False)
        await asyncio.sleep(0.3)
        auth = await client.read_gatt_char(cfg_char)
        if auth != b"\x01":
            log.warning("Auth response: 0x%s (expected 01)", auth.hex())

        # ── Notifications ─────────────────────────────────────────
        responses: dict[int, bytes] = {}
        frame_buf = bytearray()
        exp_len = 0
        msg_event = asyncio.Event()

        def on_notify(_sender, data: bytearray):
            nonlocal frame_buf, exp_len
            # Detect start of a new frame
            if (len(data) > 10 and data[0] == 0x7E
                    and len(frame_buf) >= exp_len):
                exp_len = 10 + int.from_bytes(data[6:8], "big")
                frame_buf = bytearray()
            frame_buf.extend(data)
            # Frame complete?
            if len(frame_buf) >= max(10, exp_len) and frame_buf[-1] == 0x0D:
                frame = bytes(frame_buf)
                if validate_tdt_frame(frame):
                    responses[frame[5]] = frame
                    msg_event.set()
                else:
                    log.debug("Invalid frame discarded (%d B)", len(frame))
                frame_buf.clear()
                exp_len = 0

        await client.start_notify(notify_char, on_notify)

        # ── Send commands ─────────────────────────────────────────
        async def send_cmd(cmd_id: int) -> bytes | None:
            for head in (0x7E, 0x1E):
                msg_event.clear()
                cmd = tdt_cmd(cmd_id, head=head)
                await client.write_gatt_char(write_char, cmd, response=False)
                # Wait for our specific cmd_id (msg_event fires for any
                # frame — a late/duplicate from a prior command could wake
                # us before our response arrives).
                deadline = asyncio.get_event_loop().time() + BLE_CMD_TIMEOUT
                while cmd_id not in responses:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        break
                    msg_event.clear()
                    try:
                        await asyncio.wait_for(msg_event.wait(), remaining)
                    except asyncio.TimeoutError:
                        break
                if cmd_id in responses:
                    return responses[cmd_id]
            log.warning("No response for cmd 0x%02X", cmd_id)
            return None

        # Small delays between commands — BMS needs time to process
        msg_8c = await send_cmd(0x8C)
        await asyncio.sleep(0.3)
        msg_8d = await send_cmd(0x8D)
        await asyncio.sleep(0.3)
        msg_92 = await send_cmd(0x92)

        if not msg_8c:
            log.warning("No battery data (0x8C) — skipping cycle")
            return None, None

        battery_data = decode_battery_data(msg_8c, msg_8d)
        device_info = decode_device_info(msg_92) if msg_92 else None
        return battery_data, device_info

    except Exception as e:
        log.error("BLE poll error: %s", e)
        return None, None

    finally:
        if client.is_connected:
            try:
                await client.disconnect()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════
#  MQTT — Discovery and State Publishing
# ══════════════════════════════════════════════════════════════════════

def _pub(client, prefix, obj_id, state):
    """Publish a retained state value."""
    client.publish(f"{prefix}/{obj_id}/state", str(state), qos=1, retain=True)


def _disco_sensor(client, prefix, device, obj_id, name, unit=None,
                  dev_cls=None, state_cls="measurement", entity_cat=None):
    """Publish HA sensor auto-discovery config."""
    config = {
        "name": name,
        "unique_id": f"{prefix}_{obj_id}",
        "state_topic": f"{prefix}/{obj_id}/state",
        "availability_topic": f"{prefix}/availability",
        "device": device,
    }
    if unit:
        config["unit_of_measurement"] = unit
    if dev_cls:
        config["device_class"] = dev_cls
    if state_cls:
        config["state_class"] = state_cls
    if entity_cat:
        config["entity_category"] = entity_cat
    topic = f"{DISCOVERY_PREFIX}/sensor/{prefix}/{obj_id}/config"
    client.publish(topic, json.dumps(config), qos=1, retain=True)


def _disco_binary(client, prefix, device, obj_id, name,
                  dev_cls=None, entity_cat=None):
    """Publish HA binary sensor auto-discovery config."""
    config = {
        "name": name,
        "unique_id": f"{prefix}_{obj_id}",
        "state_topic": f"{prefix}/{obj_id}/state",
        "availability_topic": f"{prefix}/availability",
        "device": device,
        "payload_on": "ON",
        "payload_off": "OFF",
    }
    if dev_cls:
        config["device_class"] = dev_cls
    if entity_cat:
        config["entity_category"] = entity_cat
    topic = f"{DISCOVERY_PREFIX}/binary_sensor/{prefix}/{obj_id}/config"
    client.publish(topic, json.dumps(config), qos=1, retain=True)


# ── GoKWh Battery ──────────────────────────────────────────────────

def publish_gokwh_discovery(client, device_info=None):
    """Register all GoKWh battery entities with HA."""
    p = GOKWH_PREFIX
    d = dict(GOKWH_DEVICE)
    if device_info:
        if device_info.get("sw_version"):
            d["sw_version"] = device_info["sw_version"]
        if device_info.get("serial_number"):
            d["serial_number"] = device_info["serial_number"]

    _disco_sensor(client, p, d, "pack_voltage", "Pack Voltage", "V", "voltage")
    _disco_sensor(client, p, d, "current", "Current", "A", "current")
    _disco_sensor(client, p, d, "power", "Power", "W", "power")
    _disco_sensor(client, p, d, "soc", "State of Charge", "%", "battery")
    _disco_sensor(client, p, d, "remaining_capacity", "Remaining Capacity", "Ah")
    _disco_sensor(client, p, d, "cycles", "Charge Cycles",
                  state_cls="total_increasing")
    for i in range(1, 5):
        _disco_sensor(client, p, d, f"cell_{i}", f"Cell {i} Voltage", "V", "voltage")
    _disco_sensor(client, p, d, "cell_delta", "Cell Voltage Delta", "mV", "voltage")
    for i in range(1, 4):
        _disco_sensor(client, p, d, f"temp_{i}", f"Temperature {i}", "°C",
                      "temperature")

    # Text sensor (no unit/device_class/state_class)
    _disco_sensor(client, p, d, "status", "Status", state_cls=None)

    # Binary sensors
    _disco_binary(client, p, d, "charge_mosfet", "Charge MOSFET")
    _disco_binary(client, p, d, "discharge_mosfet", "Discharge MOSFET")
    _disco_binary(client, p, d, "ble_connected", "BLE Connected",
                  "connectivity", "diagnostic")
    _disco_binary(client, p, d, "protection_active", "Protection Active",
                  "problem")

    log.info("GoKWh discovery published (19 entities)")


def publish_gokwh_state(client, data: dict):
    """Push all GoKWh battery sensor values to MQTT."""
    p = GOKWH_PREFIX

    _pub(client, p, "pack_voltage", f"{data['voltage']:.2f}")
    _pub(client, p, "current", f"{data['current']:.1f}")
    _pub(client, p, "power", f"{data['power']:.1f}")
    _pub(client, p, "soc", data["soc"])
    _pub(client, p, "remaining_capacity", f"{data['remaining_ah']:.1f}")
    _pub(client, p, "cycles", data["cycles"])

    for i, mv in enumerate(data.get("cell_voltages", []), 1):
        _pub(client, p, f"cell_{i}", f"{mv / 1000:.3f}")
    _pub(client, p, "cell_delta", data.get("cell_delta", 0))

    for i, t in enumerate(data.get("temperatures", []), 1):
        _pub(client, p, f"temp_{i}", f"{t:.1f}")

    if "charge_mosfet" in data:
        _pub(client, p, "charge_mosfet",
             "ON" if data["charge_mosfet"] else "OFF")
        _pub(client, p, "discharge_mosfet",
             "ON" if data["discharge_mosfet"] else "OFF")
        _pub(client, p, "protection_active",
             "ON" if data.get("problem_code", 0) != 0 else "OFF")

    if data["current"] > 0.5:
        status = "Charging"
    elif data["current"] < -0.5:
        status = "Discharging"
    else:
        status = "Idle"
    _pub(client, p, "status", status)


# ── Renogy Wanderer (placeholder — RS232 not yet wired) ───────────

def publish_wanderer_discovery(client):
    """Register Renogy Wanderer entities with HA (placeholder)."""
    p, d = WANDERER_PREFIX, WANDERER_DEVICE
    _disco_sensor(client, p, d, "solar_voltage", "Solar Voltage", "V", "voltage")
    _disco_sensor(client, p, d, "solar_current", "Solar Current", "A", "current")
    _disco_sensor(client, p, d, "solar_power", "Solar Power", "W", "power")
    _disco_sensor(client, p, d, "battery_voltage", "Battery Voltage", "V", "voltage")
    _disco_sensor(client, p, d, "charge_current", "Charge Current", "A", "current")
    _disco_sensor(client, p, d, "charge_power", "Charge Power", "W", "power")
    _disco_sensor(client, p, d, "controller_temp", "Controller Temp", "°C",
                  "temperature")
    _disco_sensor(client, p, d, "battery_temp", "Battery Temp", "°C", "temperature")
    _disco_sensor(client, p, d, "soc", "State of Charge", "%", "battery")
    _disco_sensor(client, p, d, "charge_state", "Charge State", state_cls=None)
    log.info("Wanderer discovery published (10 entities, data pending RS232)")


# ── Solar Pi Management ────────────────────────────────────────────

def publish_pi_discovery(client):
    """Register Solar Pi management entities with HA."""
    p, d = PI_PREFIX, PI_DEVICE
    _disco_sensor(client, p, d, "enclosure_temp", "Enclosure Temperature",
                  "°C", "temperature")
    _disco_sensor(client, p, d, "enclosure_humidity", "Enclosure Humidity",
                  "%", "humidity")
    _disco_sensor(client, p, d, "enclosure_pressure", "Enclosure Pressure",
                  "hPa", "atmospheric_pressure")
    _disco_binary(client, p, d, "enclosure_door", "Enclosure Door", "door")
    _disco_binary(client, p, d, "fans_active", "Cooling Fans", "running")

    # Fan mode select
    config = {
        "name": "Fan Mode",
        "unique_id": f"{p}_fan_mode",
        "state_topic": f"{p}/fan_mode/state",
        "command_topic": f"{p}/fan_mode/set",
        "availability_topic": f"{p}/availability",
        "device": d,
        "options": ["Auto", "On", "Off"],
    }
    topic = f"{DISCOVERY_PREFIX}/select/{p}/fan_mode/config"
    client.publish(topic, json.dumps(config), qos=1, retain=True)

    log.info("Solar Pi discovery published (6 entities)")


# ══════════════════════════════════════════════════════════════════════
#  Pi Hardware Manager (BME280, door, fans)
# ══════════════════════════════════════════════════════════════════════

class PiManager:
    """Manages Pi-local hardware: BME280, door switch, fan relay."""

    def __init__(self, mqtt_client):
        self.mqtt = mqtt_client
        self.fans_on = False
        self.fan_mode = "Auto"
        self.last_keepalive = time.time()
        self.keepalive_off_time = 0
        self.enclosure_temp = None
        self.last_bme_read = 0

        # GPIO
        self.gpio_chip = None
        self.relay_line = None
        self.door_line = None
        if HAS_GPIO:
            try:
                self.gpio_chip = gpiod.Chip("/dev/gpiochip0")
                self.relay_line = self.gpio_chip.request_lines(
                    config={RELAY_PIN: gpiod.LineSettings(
                        direction=Direction.OUTPUT,
                        output_value=Value.INACTIVE,
                    )},
                    consumer="solar-monitor-relay",
                )
                self.door_line = self.gpio_chip.request_lines(
                    config={DOOR_PIN: gpiod.LineSettings(
                        direction=Direction.INPUT,
                        bias=Bias.PULL_UP,
                    )},
                    consumer="solar-monitor-door",
                )
                log.info("GPIO initialized (relay=%d, door=%d)", RELAY_PIN, DOOR_PIN)
            except Exception as e:
                log.error("GPIO init failed: %s", e)

        # BME280
        self.i2c_bus = None
        self.bme_cal = None
        if HAS_BME280:
            try:
                self.i2c_bus = smbus2.SMBus(BME280_I2C_BUS)
                self.bme_cal = bme280_lib.load_calibration_params(
                    self.i2c_bus, BME280_I2C_ADDR,
                )
                log.info("BME280 initialized on bus %d", BME280_I2C_BUS)
            except Exception as e:
                log.error("BME280 init failed: %s", e)

    def handle_mqtt_command(self, object_id, payload):
        """Handle incoming MQTT commands for Pi entities."""
        if object_id == "fan_mode" and payload in ("Auto", "On", "Off"):
            self.fan_mode = payload
            _pub(self.mqtt, PI_PREFIX, "fan_mode", payload)
            log.info("Fan mode → %s", payload)

    def update(self, battery_temps=None, battery_temps_age=0):
        """Run one update cycle: read sensors, control fans, publish."""
        now = time.time()
        p = PI_PREFIX

        # Discard stale battery temps for fan safety
        if battery_temps_age > BATTERY_TEMP_TTL:
            battery_temps = None

        # BME280
        if self.i2c_bus and (now - self.last_bme_read) >= BME280_READ_INTERVAL:
            try:
                sample = bme280_lib.sample(self.i2c_bus, BME280_I2C_ADDR, self.bme_cal)
                self.enclosure_temp = sample.temperature
                _pub(self.mqtt, p, "enclosure_temp", f"{sample.temperature:.1f}")
                _pub(self.mqtt, p, "enclosure_humidity", f"{sample.humidity:.1f}")
                _pub(self.mqtt, p, "enclosure_pressure", f"{sample.pressure:.1f}")
            except Exception as e:
                log.warning("BME280 read error: %s", e)
            self.last_bme_read = now

        # Door switch
        if self.door_line:
            try:
                door_open = self.door_line.get_value(DOOR_PIN) == Value.ACTIVE
                _pub(self.mqtt, p, "enclosure_door", "ON" if door_open else "OFF")
            except Exception as e:
                log.warning("Door read error: %s", e)

        # Fan control
        want_fans = False
        temps = []
        if self.fan_mode == "On":
            want_fans = True
        elif self.fan_mode == "Off":
            want_fans = False
        else:  # Auto
            if self.enclosure_temp is not None:
                temps.append(self.enclosure_temp)
            if battery_temps:
                temps.extend(battery_temps)
            if temps:
                if any(t >= THERMAL_ON_C for t in temps):
                    want_fans = True
                elif self.fans_on and not all(t <= THERMAL_OFF_C for t in temps):
                    want_fans = True
            # Keep-alive pulse
            if not want_fans:
                if (now - self.last_keepalive) >= KEEPALIVE_INTERVAL_S:
                    want_fans = True
                    self.keepalive_off_time = now + KEEPALIVE_DURATION_S
                    self.last_keepalive = now
                elif now < self.keepalive_off_time:
                    want_fans = True

        if self.relay_line:
            if want_fans and not self.fans_on:
                self.relay_line.set_value(RELAY_PIN, Value.ACTIVE)
                self.fans_on = True
                log.info("Fans ON (mode=%s)", self.fan_mode)
            elif not want_fans and self.fans_on:
                self.relay_line.set_value(RELAY_PIN, Value.INACTIVE)
                self.fans_on = False
                log.info("Fans OFF (mode=%s)", self.fan_mode)

        _pub(self.mqtt, p, "fans_active", "ON" if self.fans_on else "OFF")

    def shutdown(self):
        """Ensure fans are off on exit."""
        if self.relay_line:
            try:
                self.relay_line.set_value(RELAY_PIN, Value.INACTIVE)
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════

async def main():
    parser = argparse.ArgumentParser(
        description="Solar Monitor v2 — GoKWh + Renogy + Pi → Home Assistant",
    )
    parser.add_argument("--mac", help="GoKWh battery MAC (optional, for Linux)")
    parser.add_argument("--mqtt-host", default=MQTT_HOST)
    parser.add_argument("--mqtt-port", type=int, default=MQTT_PORT)
    parser.add_argument("--poll-interval", type=int, default=BLE_POLL_INTERVAL,
                        help="BLE poll interval in seconds (default: 60)")
    parser.add_argument("--no-wanderer", action="store_true",
                        help="Skip Renogy Wanderer discovery")
    parser.add_argument("--no-pi", action="store_true",
                        help="Skip Pi management (for Mac testing)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # ── Shared state ──────────────────────────────────────────────
    gokwh_device_info = None   # populated on first successful poll

    # ── MQTT client ───────────────────────────────────────────────
    mqtt_client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="solar_monitor_v2",
    )
    if MQTT_USER:
        mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
    mqtt_client.will_set(
        f"{GOKWH_PREFIX}/availability", "offline", qos=1, retain=True,
    )

    def on_connect(client, userdata, flags, rc, props):
        if rc.is_failure:
            log.error("MQTT connection failed: %s", rc)
            return
        log.info("MQTT connected to %s:%d", args.mqtt_host, args.mqtt_port)

        publish_gokwh_discovery(client, gokwh_device_info)
        client.publish(f"{GOKWH_PREFIX}/availability", "online",
                       qos=1, retain=True)

        if not args.no_wanderer:
            publish_wanderer_discovery(client)
            client.publish(f"{WANDERER_PREFIX}/availability", "offline",
                           qos=1, retain=True)

        if not args.no_pi:
            publish_pi_discovery(client)
            client.publish(f"{PI_PREFIX}/availability", "online",
                           qos=1, retain=True)
            client.subscribe(f"{PI_PREFIX}/+/set")

    def on_message(client, userdata, message):
        parts = message.topic.split("/")
        if len(parts) == 3 and parts[0] == PI_PREFIX and parts[2] == "set":
            payload = message.payload.decode("utf-8", errors="replace").strip()
            if pi_mgr:
                pi_mgr.handle_mqtt_command(parts[1], payload)

    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message

    log.info("Connecting to MQTT %s:%d…", args.mqtt_host, args.mqtt_port)
    mqtt_client.connect_async(args.mqtt_host, args.mqtt_port, keepalive=60)
    mqtt_client.loop_start()
    # Give paho's thread a moment to establish the connection
    await asyncio.sleep(2)

    # ── Pi hardware manager ───────────────────────────────────────
    pi_mgr = None
    if not args.no_pi and (HAS_GPIO or HAS_BME280):
        pi_mgr = PiManager(mqtt_client)
        _pub(mqtt_client, PI_PREFIX, "fan_mode", "Auto")

    log.info("Starting poll loops: battery=%ds, pi=%ds",
             args.poll_interval, PI_POLL_INTERVAL if pi_mgr else 0)

    # ── Shared state across tasks ────────────────────────────────
    # Written atomically by battery_loop, read by pi_loop
    shared = {"battery_temps": None, "battery_temps_at": 0.0}

    # ── Battery polling task ──────────────────────────────────────
    async def battery_loop():
        nonlocal gokwh_device_info
        consecutive_fails = 0
        ev_loop = asyncio.get_event_loop()

        while True:
            t0 = ev_loop.time()
            try:
                try:
                    data, info = await asyncio.wait_for(
                        poll_battery(mac=args.mac),
                        timeout=BLE_POLL_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    log.warning("BLE poll timed out after %ds", BLE_POLL_TIMEOUT)
                    data, info = None, None
                except Exception as e:
                    log.warning("BLE poll error: %s", e)
                    data, info = None, None

                if data:
                    consecutive_fails = 0
                    _pub(mqtt_client, GOKWH_PREFIX, "ble_connected", "ON")
                    publish_gokwh_state(mqtt_client, data)
                    shared["battery_temps"] = data.get("temperatures")
                    shared["battery_temps_at"] = time.time()

                    if info and info != gokwh_device_info:
                        gokwh_device_info = info
                        publish_gokwh_discovery(mqtt_client, info)
                        log.info("Device info: %s", info)

                    log.info(
                        "Battery: %.2fV  %.1fA  %d%%  %.1f°C  Δ%dmV",
                        data["voltage"], data["current"], data["soc"],
                        data["temperatures"][0] if data["temperatures"] else 0,
                        data["cell_delta"],
                    )
                else:
                    consecutive_fails += 1
                    _pub(mqtt_client, GOKWH_PREFIX, "ble_connected", "OFF")
                    if consecutive_fails >= BLE_RESET_THRESHOLD:
                        log.error("%d consecutive BLE failures — resetting BlueZ",
                                  consecutive_fails)
                        await reset_bluez()
                        consecutive_fails = 0
                        shared["battery_temps"] = None

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("battery_loop iteration error: %s", e, exc_info=True)

            # Sleep remainder of interval (measured from poll start)
            elapsed = ev_loop.time() - t0
            remaining = max(0, args.poll_interval - elapsed)
            await asyncio.sleep(remaining)

    # ── Pi hardware polling task ──────────────────────────────────
    async def pi_loop():
        while True:
            try:
                now = time.time()
                temps_age = now - shared["battery_temps_at"]
                pi_mgr.update(
                    battery_temps=shared["battery_temps"],
                    battery_temps_age=temps_age,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("pi_loop iteration error: %s", e, exc_info=True)
            await asyncio.sleep(PI_POLL_INTERVAL)

    # ── Wanderer polling task (placeholder) ───────────────────────
    async def wanderer_loop():
        # Will be implemented when RS232 hardware arrives
        while True:
            await asyncio.sleep(60)

    # ── Task supervisor ───────────────────────────────────────────
    managed_tasks: dict[str, asyncio.Task] = {}

    def _start_task(name, coro_fn):
        task = asyncio.create_task(coro_fn(), name=name)
        managed_tasks[name] = task
        task.add_done_callback(lambda t: _on_task_done(name, coro_fn, t))
        return task

    def _on_task_done(name, coro_fn, task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            log.error("Task '%s' died: %s — restarting in 5s", name, exc)
            asyncio.get_event_loop().call_later(
                5, lambda: _start_task(name, coro_fn))

    # ── Run all loops concurrently ────────────────────────────────
    shutdown = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.set)

    _start_task("battery", battery_loop)
    if pi_mgr:
        _start_task("pi", pi_loop)
    if not args.no_wanderer:
        _start_task("wanderer", wanderer_loop)

    # Wait for shutdown signal, then cancel all tasks
    await shutdown.wait()
    for t in managed_tasks.values():
        t.cancel()
    await asyncio.gather(*managed_tasks.values(), return_exceptions=True)

    # ── Cleanup ───────────────────────────────────────────────────
    log.info("Shutting down…")
    mqtt_client.publish(f"{GOKWH_PREFIX}/availability", "offline",
                        qos=1, retain=True)
    if not args.no_pi:
        mqtt_client.publish(f"{PI_PREFIX}/availability", "offline",
                            qos=1, retain=True)
    if pi_mgr:
        pi_mgr.shutdown()
    mqtt_client.loop_stop()
    mqtt_client.disconnect()
    log.info("Stopped.")


if __name__ == "__main__":
    asyncio.run(main())

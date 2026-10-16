#!/usr/bin/env python3
"""
Solar Monitor v2 — Home Assistant MQTT Service

Monitors solar equipment and publishes to Home Assistant via MQTT:
  • GoKWh 100Ah LiFePO4 Batteries (×2, BLE, TDT protocol)
  • Renogy Wanderer Li 30A PWM   (read-only Modbus via BT-1 BLE)
  • Solar Pi management            (BME280, door, fans — Pi-only)

Each component appears as a separate device in Home Assistant.

Usage:
    python3 solar_monitor_v2.py [--mqtt-host $MQTT_HOST] [--debug]
    python3 solar_monitor_v2.py --mac1 AA:BB:CC:DD:EE:FF --mac2 11:22:33:44:55:66
    python3 solar_monitor_v2.py --no-pi   # Mac testing (no GPIO/BME280)
"""

import argparse
import asyncio
import json
import logging
import os
import re
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
BLE_CONNECT_RETRIES = 3     # connect attempts per poll cycle
BLE_DEVICE_NAME = "XDZN"   # BLE name prefix for GoKWh (TDT BMS)
BLE_RESET_THRESHOLD = 5     # consecutive fails before resetting BlueZ
WANDERER_ADDRESS = 0xFF     # Renogy standalone address, not a broadcast

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


async def _run(cmd, timeout=10):
    """Run a shell command; return True on success, False on failure/timeout."""
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


async def reset_bluez():
    """Escalating reset of the BlueZ adapter to clear wedged state.

    Level 1: btmgmt power cycle (clears stuck scans)
    Level 2: rfkill + bluetooth service restart (clears deeper hangs)
    Level 3: full system reboot (clears wedged UART/firmware)
    """

    async def _bt_is_up():
        proc = await asyncio.create_subprocess_exec(
            "hciconfig", "hci0",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        return b"UP RUNNING" in out

    # Level 1: HCI adapter reset (clears firmware-level wedges)
    log.warning("BlueZ reset — level 1: HCI adapter reset")
    await _run("hciconfig hci0 reset")
    await asyncio.sleep(2)
    await _run("hciconfig hci0 up")
    await asyncio.sleep(1)
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
    "name": "GoKWh Battery - 482F",
    "manufacturer": "GoKWh",
    "model": "100Ah LiFePO4",
}

GOKWH_2_PREFIX = "solar_gokwh_2"
GOKWH_2_DEVICE = {
    "identifiers": ["solar_gokwh_battery_2"],
    "name": "GoKWh Battery - C7DD",
    "manufacturer": "GoKWh",
    "model": "100Ah LiFePO4",
}

WANDERER_PREFIX = "solar_wanderer"
WANDERER_DEVICE = {
    "identifiers": ["solar_renogy_wanderer"],
    "name": "Renogy Wanderer",
    "manufacturer": "Renogy",
    "model": "Wanderer Li 30A PWM",
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
    devices = None
    for scan_attempt in range(2):
        try:
            devices = await BleakScanner.discover(
                timeout=BLE_SCAN_TIMEOUT, return_adv=True,
            )
            break
        except Exception as e:
            if scan_attempt == 0 and "InProgress" in str(e):
                log.warning("Stale BLE scan detected — resetting HCI adapter")
                await _run("hciconfig hci0 reset")
                await asyncio.sleep(2)
                await _run("hciconfig hci0 up")
                await asyncio.sleep(1)
            else:
                log.error("BLE scan failed: %s", e)
                return None, None
    if devices is None:
        return None, None
    device = None
    for addr, (dev, adv) in devices.items():
        name = adv.local_name or dev.name or ""
        if mac and addr.upper() == mac.upper():
            device = dev
            break
        if not mac and name.upper().startswith(name_prefix.upper()):
            device = dev
            break

    if not device:
        log.warning("Battery not found in BLE scan")
        return None, None

    log.debug("Found %s (%s)", device.name or "?", device.address)

    # ── Connect (with retry) ─────────────────────────────────────
    # Fresh BleakClient per attempt — a failed connect can leave the
    # client/BlueZ in a dirty state, so reusing the same object risks
    # compounding the problem.
    client = None
    try:
        for attempt in range(1, BLE_CONNECT_RETRIES + 1):
            client = BleakClient(device, timeout=BLE_CONNECT_TIMEOUT)
            try:
                await client.connect()
                break
            except Exception as e:
                # Best-effort cleanup of partial connection state
                try:
                    await asyncio.wait_for(client.disconnect(), timeout=3)
                except Exception:
                    pass
                client = None
                if attempt < BLE_CONNECT_RETRIES:
                    log.warning("BLE connect attempt %d/%d failed: %s",
                                attempt, BLE_CONNECT_RETRIES, e)
                    await asyncio.sleep(2)
                else:
                    log.error("BLE connect failed after %d attempts: %s",
                              BLE_CONNECT_RETRIES, e)
                    return None, None

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
        if client:
            try:
                await asyncio.wait_for(client.disconnect(), timeout=5)
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════
#  Wanderer BT-1 Protocol
# ══════════════════════════════════════════════════════════════════════

BT1_WRITE_UUID = "0000ffd1-0000-1000-8000-00805f9b34fb"
BT1_NOTIFY_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
CHARGE_STATES = {
    0: "Deactivated", 1: "Activated", 2: "MPPT", 3: "Equalize",
    4: "Boost", 5: "Float", 6: "Current limiting",
}


def wanderer_request(address: int, start: int, count: int) -> bytes:
    """Build a function-03 register read; never emit controller writes."""
    if not 1 <= address <= 255:
        raise ValueError("Wanderer address must be between 1 and 255")
    if not 0 <= start <= 0xFFFF or not 1 <= count <= 125:
        raise ValueError("Invalid Modbus register range")
    body = bytes([address, 3]) + start.to_bytes(2, "big") + count.to_bytes(2, "big")
    return body + crc_modbus(body).to_bytes(2, "little")


def decode_wanderer_response(frame: bytes, address: int, count: int) -> list[int]:
    """Validate a complete Modbus response, including exceptions and wire CRC."""
    if len(frame) < 5:
        raise ValueError("Truncated Wanderer response")
    if crc_modbus(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
        raise ValueError("Wanderer response CRC mismatch")
    if frame[0] != address:
        raise ValueError("Unexpected Wanderer response address")
    if frame[1] == 0x83 and len(frame) == 5:
        raise ValueError(f"Wanderer Modbus exception 0x{frame[2]:02X}")
    if frame[1] != 3 or frame[2] != count * 2 or len(frame) != count * 2 + 5:
        raise ValueError("Unexpected Wanderer response function or length")
    return [int.from_bytes(frame[i:i + 2], "big")
            for i in range(3, len(frame) - 2, 2)]


def decode_wanderer_data(registers: list[int], state: int) -> dict:
    if len(registers) != 10 or not 0 <= registers[0] <= 100:
        raise ValueError("Invalid Wanderer telemetry registers")

    def temperature(raw):
        return -(raw & 0x7F) if raw & 0x80 else raw

    voltage = registers[1] / 10
    current = registers[2] / 100
    charge_state = state & 0xFF
    return {
        "solar_voltage": registers[7] / 10,
        "solar_current": registers[8] / 100,
        "solar_power": registers[9],
        "battery_voltage": voltage,
        "charge_current": current,
        "charge_power": round(voltage * current, 2),
        "controller_temp": temperature(registers[3] >> 8),
        "battery_temp": temperature(registers[3] & 0xFF),
        "soc": registers[0],
        "charge_state": CHARGE_STATES.get(charge_state, f"Unknown ({charge_state})"),
    }


async def poll_wanderer(mac: str, address: int = WANDERER_ADDRESS) -> dict:
    """One bounded scan/connect/read/disconnect cycle, serialized with batteries."""
    device = await BleakScanner.find_device_by_address(mac, timeout=BLE_SCAN_TIMEOUT)
    if device is None:
        raise RuntimeError("Wanderer BT-1 not found in BLE scan")
    client = BleakClient(device, timeout=BLE_CONNECT_TIMEOUT)
    try:
        await client.connect()
        write = client.services.get_characteristic(BT1_WRITE_UUID)
        notify = client.services.get_characteristic(BT1_NOTIFY_UUID)
        if write is None or notify is None:
            raise RuntimeError("Wanderer BT-1 missing FFD1/FFF1 characteristics")
        queue = asyncio.Queue()

        def on_notify(_sender, data):
            queue.put_nowait(bytes(data))

        await client.start_notify(notify, on_notify)

        async def read_registers(start, count):
            while not queue.empty():
                queue.get_nowait()
            await client.write_gatt_char(
                write, wanderer_request(address, start, count), response=False,
            )
            frame = bytearray()

            async def receive():
                while True:
                    frame.extend(await queue.get())
                    if len(frame) < 3:
                        continue
                    if frame[0] != address or frame[1] not in (3, 0x83):
                        raise ValueError("Unexpected Wanderer response header")
                    length = 5 if frame[1] == 0x83 else frame[2] + 5
                    if len(frame) >= length:
                        return decode_wanderer_response(bytes(frame), address, count)

            return await asyncio.wait_for(receive(), timeout=BLE_CMD_TIMEOUT)

        registers = await read_registers(0x0100, 10)
        await asyncio.sleep(0.3)
        state, = await read_registers(0x0120, 1)
        return decode_wanderer_data(registers, state)
    finally:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=5)
        except Exception as exc:
            log.warning("Wanderer BT-1 disconnect failed: %s", exc)


async def update_wanderer(client, mac, address):
    """Publish a complete validated sample, or explicitly mark it unavailable."""
    try:
        data = await asyncio.wait_for(
            poll_wanderer(mac, address), timeout=BLE_POLL_TIMEOUT,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("Wanderer poll failed (%s): %s", type(exc).__name__, exc)
        client.publish(f"{WANDERER_PREFIX}/availability", "offline", qos=1, retain=True)
        return False
    for object_id, value in data.items():
        _pub(client, WANDERER_PREFIX, object_id, value)
    client.publish(f"{WANDERER_PREFIX}/availability", "online", qos=1, retain=True)
    log.info("Wanderer: battery=%.1fV charge=%.2fA solar=%.1fV/%.2fA/%dW %s",
             data["battery_voltage"], data["charge_current"], data["solar_voltage"],
             data["solar_current"], data["solar_power"], data["charge_state"])
    return True


# ══════════════════════════════════════════════════════════════════════
#  MQTT — Discovery and State Publishing
# ══════════════════════════════════════════════════════════════════════

def _pub(client, prefix, obj_id, state):
    """Publish a retained state value."""
    client.publish(f"{prefix}/{obj_id}/state", str(state), qos=1, retain=True)


def _disco_sensor(client, prefix, device, obj_id, name, unit=None,
                  dev_cls=None, state_cls="measurement", entity_cat=None,
                  expire_after=None):
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
    if expire_after is not None:
        config["expire_after"] = expire_after
        del config["availability_topic"]
        config["availability_mode"] = "all"
        config["availability"] = [
            {"topic": f"{prefix}/availability"},
            {"topic": f"{GOKWH_PREFIX}/availability"},
        ]
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

def publish_gokwh_discovery(client, prefix, device, device_info=None, mac=None):
    """Register GoKWh battery entities with HA."""
    p = prefix
    d = dict(device)
    if mac:
        d["connections"] = [["mac", mac.upper()]]
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

    log.info("%s discovery published (19 entities)", d["name"])


def publish_gokwh_state(client, prefix, data: dict):
    """Push GoKWh battery sensor values to MQTT."""
    p = prefix

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


# ── Renogy Wanderer ──────────────────────────────────────────────

def publish_wanderer_discovery(client, expire_after=180):
    """Register controller sensors, gated by poll health and the Pi process LWT."""
    p, d = WANDERER_PREFIX, WANDERER_DEVICE
    sensors = [
        ("solar_voltage", "Solar Voltage", "V", "voltage"),
        ("solar_current", "Solar Current", "A", "current"),
        ("solar_power", "Solar Power", "W", "power"),
        ("battery_voltage", "Battery Voltage", "V", "voltage"),
        ("charge_current", "Charge Current", "A", "current"),
        ("charge_power", "Charge Power", "W", "power"),
        ("controller_temp", "Controller Temp", "°C", "temperature"),
        ("battery_temp", "Battery Temp", "°C", "temperature"),
        ("soc", "Controller Estimated SOC", "%", "battery"),
        ("charge_state", "Charge State", None, None),
    ]
    for obj_id, name, unit, dev_cls in sensors:
        _disco_sensor(client, p, d, obj_id, name, unit, dev_cls,
                      state_cls=None if obj_id == "charge_state" else "measurement",
                      expire_after=expire_after)
    log.info("Wanderer discovery published (10 entities)")


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
    parser.add_argument("--mac", "--mac1", dest="mac1",
                        help="GoKWh battery 1 MAC address")
    parser.add_argument("--mac2",
                        help="GoKWh battery 2 MAC address")
    parser.add_argument("--mqtt-host", default=MQTT_HOST)
    parser.add_argument("--mqtt-port", type=int, default=MQTT_PORT)
    parser.add_argument("--poll-interval", type=int, default=BLE_POLL_INTERVAL,
                        help="BLE poll interval in seconds (default: 60)")
    parser.add_argument("--no-wanderer", action="store_true",
                        help="Disable Renogy Wanderer discovery and polling")
    parser.add_argument("--wanderer-mac", default=os.environ.get("WANDERER_MAC"),
                        help="BT-1 MAC address (or WANDERER_MAC environment variable)")
    parser.add_argument("--wanderer-address", type=lambda value: int(value, 0),
                        default=os.environ.get("WANDERER_ADDRESS", "0xFF"),
                        help="Modbus device address (default: 0xFF)")
    parser.add_argument("--no-pi", action="store_true",
                        help="Skip Pi management (for Mac testing)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    if args.poll_interval <= 0:
        parser.error("--poll-interval must be positive")
    if not 1 <= args.wanderer_address <= 255:
        parser.error("--wanderer-address must be between 1 and 255")
    if args.wanderer_mac and not re.fullmatch(
            r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", args.wanderer_mac):
        parser.error("--wanderer-mac must be a colon-separated BLE MAC address")

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # ── Resolve battery MACs ──────────────────────────────────────
    mac1 = args.mac1 or os.environ.get("BLE_MAC_1") or os.environ.get("BLE_MAC")
    mac2 = args.mac2 or os.environ.get("BLE_MAC_2")

    # Build list of battery configs: (prefix, device_dict, mac)
    batteries = [(GOKWH_PREFIX, GOKWH_DEVICE, mac1)]
    if mac2:
        batteries.append((GOKWH_2_PREFIX, GOKWH_2_DEVICE, mac2))
    wanderer_enabled = bool(args.wanderer_mac) and not args.no_wanderer
    if wanderer_enabled and any(
            mac and mac.upper() == args.wanderer_mac.upper() for _, _, mac in batteries):
        parser.error("Wanderer and battery MAC addresses must be different")
    if not args.no_wanderer and not args.wanderer_mac:
        log.warning("WANDERER_MAC not configured; controller will remain unavailable")
    wanderer_expiry = max(180, args.poll_interval * 3)
    pi_mgr = None

    # ── Shared state ──────────────────────────────────────────────
    # Per-battery device info, populated on first successful poll
    gokwh_device_infos = {prefix: None for prefix, _, _ in batteries}

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

        for prefix, device, _mac in batteries:
            publish_gokwh_discovery(client, prefix, device,
                                    gokwh_device_infos[prefix], mac=_mac)
            client.publish(f"{prefix}/availability", "online",
                           qos=1, retain=True)

        if not args.no_wanderer:
            publish_wanderer_discovery(client, wanderer_expiry)
        client.publish(f"{WANDERER_PREFIX}/availability", "offline",
                       qos=1, retain=True)
        client.subscribe(f"{DISCOVERY_PREFIX}/status")

        if not args.no_pi:
            publish_pi_discovery(client)
            client.publish(f"{PI_PREFIX}/availability", "online",
                           qos=1, retain=True)
            client.subscribe(f"{PI_PREFIX}/+/set")

    def on_message(client, userdata, message):
        if message.topic == f"{DISCOVERY_PREFIX}/status":
            if message.payload == b"online" and not args.no_wanderer:
                publish_wanderer_discovery(client, wanderer_expiry)
            return
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

    log.info("Starting poll loops: BLE=%ds (%d batteries, Wanderer=%s), pi=%ds",
             args.poll_interval, len(batteries),
             args.wanderer_mac if wanderer_enabled else "disabled",
             PI_POLL_INTERVAL if pi_mgr else 0)

    # ── Shared state across tasks ────────────────────────────────
    # Per-battery temps, written by battery_loop, read by pi_loop
    shared = {
        "battery_temps": {},      # prefix → temp list
        "battery_temps_at": {},   # prefix → timestamp
    }

    # ── Battery polling task ──────────────────────────────────────
    async def battery_loop():
        # Per-battery tracking
        consecutive_fails = {prefix: 0 for prefix, _, _ in batteries}
        if wanderer_enabled:
            consecutive_fails[WANDERER_PREFIX] = 0
        ev_loop = asyncio.get_event_loop()

        while True:
            t0 = ev_loop.time()
            try:
                for prefix, device, mac in batteries:
                    try:
                        data, info = await asyncio.wait_for(
                            poll_battery(mac=mac),
                            timeout=BLE_POLL_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        log.warning("%s: BLE poll timed out after %ds",
                                    device["name"], BLE_POLL_TIMEOUT)
                        data, info = None, None
                    except Exception as e:
                        log.warning("%s: BLE poll error: %s", device["name"], e)
                        data, info = None, None

                    if data:
                        consecutive_fails[prefix] = 0
                        _pub(mqtt_client, prefix, "ble_connected", "ON")
                        publish_gokwh_state(mqtt_client, prefix, data)
                        shared["battery_temps"][prefix] = data.get("temperatures")
                        shared["battery_temps_at"][prefix] = time.time()

                        if info and info != gokwh_device_infos[prefix]:
                            gokwh_device_infos[prefix] = info
                            publish_gokwh_discovery(mqtt_client, prefix,
                                                    device, info, mac=mac)
                            log.info("%s device info: %s", device["name"], info)

                        log.info(
                            "%s: %.2fV  %.1fA  %d%%  %.1f°C  Δ%dmV",
                            device["name"],
                            data["voltage"], data["current"], data["soc"],
                            data["temperatures"][0] if data.get("temperatures") else 0,
                            data["cell_delta"],
                        )
                    else:
                        consecutive_fails[prefix] += 1
                        _pub(mqtt_client, prefix, "ble_connected", "OFF")

                    # Small gap between polling each battery
                    if len(batteries) > 1:
                        await asyncio.sleep(2)

                if wanderer_enabled:
                    if await update_wanderer(
                            mqtt_client, args.wanderer_mac, args.wanderer_address):
                        consecutive_fails[WANDERER_PREFIX] = 0
                    else:
                        consecutive_fails[WANDERER_PREFIX] += 1

                # Reset only when ALL configured BLE devices are failing.
                if all(f >= BLE_RESET_THRESHOLD
                       for f in consecutive_fails.values()):
                    total = sum(consecutive_fails.values())
                    log.error("%d total consecutive BLE failures — resetting BlueZ",
                              total)
                    await reset_bluez()
                    for p in consecutive_fails:
                        consecutive_fails[p] = 0
                    shared["battery_temps"].clear()

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
                # Combine fresh temps from all batteries
                all_temps = []
                oldest_age = 0.0
                for prefix in shared["battery_temps"]:
                    age = now - shared["battery_temps_at"].get(prefix, 0)
                    temps = shared["battery_temps"].get(prefix)
                    if temps and age <= BATTERY_TEMP_TTL:
                        all_temps.extend(temps)
                    oldest_age = max(oldest_age, age)
                pi_mgr.update(
                    battery_temps=all_temps or None,
                    battery_temps_age=oldest_age,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("pi_loop iteration error: %s", e, exc_info=True)
            await asyncio.sleep(PI_POLL_INTERVAL)

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

    # Wait for shutdown signal, then cancel all tasks
    await shutdown.wait()
    for t in managed_tasks.values():
        t.cancel()
    await asyncio.gather(*managed_tasks.values(), return_exceptions=True)

    # ── Cleanup ───────────────────────────────────────────────────
    log.info("Shutting down…")
    for prefix, _, _ in batteries:
        mqtt_client.publish(f"{prefix}/availability", "offline",
                            qos=1, retain=True)
    if not args.no_pi:
        mqtt_client.publish(f"{PI_PREFIX}/availability", "offline",
                            qos=1, retain=True)
    controller_offline = mqtt_client.publish(
        f"{WANDERER_PREFIX}/availability", "offline", qos=1, retain=True,
    )
    if mqtt_client.is_connected():
        controller_offline.wait_for_publish(timeout=3)
    if pi_mgr:
        pi_mgr.shutdown()
    mqtt_client.loop_stop()
    mqtt_client.disconnect()
    log.info("Stopped.")


if __name__ == "__main__":
    asyncio.run(main())

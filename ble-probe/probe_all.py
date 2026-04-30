#!/usr/bin/env python3
"""
All-in-one BLE BMS probe — scan, enumerate, and test JBD protocol.

Scans for a BMS device (by fff0 service UUID or name pattern), connects
immediately when found, dumps the GATT profile, and tries JBD commands.
Single script avoids the macOS CoreBluetooth UUID rotation problem.

Usage:
    python3 probe_all.py                  # auto-find BMS, enumerate + probe
    python3 probe_all.py --monitor 60     # also watch notifications for 60s
    python3 probe_all.py --name 125F      # match by partial name
"""

import argparse
import asyncio
import struct
from bleak import BleakClient, BleakScanner

# ── JBD Protocol ────────────────────────────────────────────────────

def jbd_build_read_cmd(cmd: int) -> bytes:
    length = 0x00
    checksum = 0x10000 - (cmd + length)
    return bytes([0xDD, 0xA5, cmd, length,
                  (checksum >> 8) & 0xFF, checksum & 0xFF, 0x77])


def jbd_parse_frame(data: bytes) -> dict | None:
    if len(data) < 7 or data[0] != 0xDD or data[-1] != 0x77:
        return None
    cmd = data[1]
    status = data[2]
    length = data[3]
    payload = data[4:4 + length]
    chk_bytes = data[4 + length:4 + length + 2]
    if len(chk_bytes) < 2:
        return None
    received_chk = (chk_bytes[0] << 8) | chk_bytes[1]
    calc_chk = 0x10000 - ((sum(payload) + cmd + status + length) & 0xFFFF)
    return {
        "cmd": cmd,
        "status": "OK" if status == 0x00 else f"ERROR(0x{status:02X})",
        "length": length,
        "payload": payload,
        "checksum_ok": (received_chk & 0xFFFF) == (calc_chk & 0xFFFF),
    }


def decode_basic_info(payload: bytes) -> dict:
    if len(payload) < 23:
        return {"error": f"payload too short ({len(payload)} bytes, need ≥23)"}
    info = {}
    info["total_voltage"] = struct.unpack(">H", payload[0:2])[0] / 100.0
    info["current"] = struct.unpack(">h", payload[2:4])[0] / 100.0
    info["remaining_capacity"] = struct.unpack(">H", payload[4:6])[0] / 100.0
    info["nominal_capacity"] = struct.unpack(">H", payload[6:8])[0] / 100.0
    info["cycle_count"] = struct.unpack(">H", payload[8:10])[0]
    info["production_date"] = struct.unpack(">H", payload[10:12])[0]
    info["balance_status_1"] = struct.unpack(">H", payload[12:14])[0]
    info["balance_status_2"] = struct.unpack(">H", payload[14:16])[0]
    info["protection_status"] = struct.unpack(">H", payload[16:18])[0]
    info["software_version"] = payload[18]
    info["remaining_soc"] = payload[19]
    info["fet_status"] = payload[20]
    info["cell_count"] = payload[21]
    info["ntc_count"] = payload[22]
    temps = []
    for i in range(info["ntc_count"]):
        offset = 23 + i * 2
        if offset + 2 <= len(payload):
            raw = struct.unpack(">H", payload[offset:offset + 2])[0]
            temps.append((raw - 2731) / 10.0)
    info["temperatures"] = temps
    prot = info["protection_status"]
    prot_names = [
        "Cell OVP", "Cell UVP", "Pack OVP", "Pack UVP",
        "Charge OT", "Charge UT", "Discharge OT", "Discharge UT",
        "Charge OC", "Discharge OC", "Short Circuit", "IC Error",
        "FET Lock", "Res13", "Res14", "Res15",
    ]
    info["protections"] = [n for bit, n in enumerate(prot_names) if prot & (1 << bit)]
    info["charge_fet"] = "ON" if info["fet_status"] & 0x01 else "OFF"
    info["discharge_fet"] = "ON" if info["fet_status"] & 0x02 else "OFF"
    return info


def decode_cell_voltages(payload: bytes) -> list[float]:
    count = len(payload) // 2
    return [struct.unpack(">H", payload[i*2:(i+1)*2])[0] / 1000.0
            for i in range(count)]


def decode_hw_version(payload: bytes) -> str:
    try:
        return payload.decode("ascii", errors="replace").strip("\x00")
    except Exception:
        return payload.hex()


# ── BLE helpers ─────────────────────────────────────────────────────

KNOWN_UUIDS = {
    "00002a19": "Battery Level", "00002a00": "Device Name",
    "00002a29": "Manufacturer", "00002a24": "Model Number",
    "00002a26": "Firmware Rev", "0000fff0": "JBD BMS Service",
    "0000fff1": "JBD Write", "0000fff2": "JBD Notify",
    "0000fff4": "JBD Notify (alt)", "0000ffe0": "BMS Service (alt)",
    "0000ffe1": "BMS Char (alt)",
}

WRITE_HINTS = ["0000fff2", "0000fff1", "0000ffe1", "0000ff01"]
NOTIFY_HINTS = ["0000fff1", "0000fff4", "0000fff2", "0000ffe1", "0000ff02"]
BMS_SERVICE_HINTS = {"0000fff0", "0000ffe0", "0000ff00"}
BMS_NAME_HINTS = ["bms", "battery", "jbd", "smart", "xdzn", "gpkwd", "gokwh",
                  "xiaoxiang", "overkill"]


def uuid_hint(uuid_str: str) -> str:
    return KNOWN_UUIDS.get(uuid_str[:8].lower(), "")


def props_str(props: list[str]) -> str:
    m = {"read": "R", "write": "W", "write-without-response": "Wn",
         "notify": "N", "indicate": "I"}
    return " ".join(m.get(p.lower(), p) for p in props)


class BLEResponse:
    def __init__(self):
        self.buffer = bytearray()
        self.event = asyncio.Event()

    def handle(self, sender, data: bytearray):
        self.buffer.extend(data)
        if len(self.buffer) >= 7 and self.buffer[-1] == 0x77:
            self.event.set()


# ── Main logic ──────────────────────────────────────────────────────

async def find_bms(name_filter: str | None, timeout: float = 15.0):
    """Scan and return the first likely BMS device."""
    print(f"Scanning for BMS devices ({timeout}s)...")

    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)

    for addr, (device, adv) in devices.items():
        name = adv.local_name or device.name or ""
        uuids = adv.service_uuids or []

        # If name filter specified, match on that
        if name_filter and name_filter.lower() not in name.lower():
            continue

        # Check service UUIDs or name for BMS signatures
        is_bms = any(uuid.lower().startswith(tuple(BMS_SERVICE_HINTS)) for uuid in uuids)
        if not is_bms:
            is_bms = any(h in name.lower() for h in BMS_NAME_HINTS)
        if is_bms or (name_filter and name_filter.lower() in name.lower()):
            print(f"  ★ Found: {name} [{addr}] RSSI: {adv.rssi}")
            if uuids:
                print(f"    Services: {', '.join(uuids)}")
            return device

    return None


async def enumerate_gatt(client):
    """Dump the full GATT profile and return (write_uuid, notify_uuid)."""
    write_uuid = None
    notify_uuid = None

    print("\n" + "=" * 60)
    print("  GATT SERVICE MAP")
    print("=" * 60)

    for service in client.services:
        hint = uuid_hint(service.uuid)
        label = f"  ({hint})" if hint else ""
        print(f"\nService: {service.uuid}{label}")

        for char in service.characteristics:
            chint = uuid_hint(char.uuid)
            clabel = f"  ({chint})" if chint else ""
            flags = props_str(char.properties)
            print(f"  ├─ {char.uuid}  [{flags}]{clabel}")

            lprops = [p.lower() for p in char.properties]
            uuid_lower = char.uuid.lower()

            # Auto-detect write/notify chars (by actual properties, not UUID assumptions)
            has_write = "write" in lprops or "write-without-response" in lprops
            has_notify = "notify" in lprops
            is_known = any(uuid_lower.startswith(h) for h in WRITE_HINTS + NOTIFY_HINTS)

            if is_known:
                if has_write and not has_notify and not write_uuid:
                    write_uuid = char.uuid
                elif has_notify and not has_write and not notify_uuid:
                    notify_uuid = char.uuid
                # If char has both, check descriptor names or defer
                elif has_write and has_notify:
                    pass  # ambiguous, skip

    # Second pass: if we still need chars, pick best from what we saw
    if not write_uuid or not notify_uuid:
        for service in client.services:
            for char in service.characteristics:
                lprops = [p.lower() for p in char.properties]
                uuid_lower = char.uuid.lower()
                is_known = any(uuid_lower.startswith(h) for h in WRITE_HINTS + NOTIFY_HINTS)
                if not is_known:
                    continue
                has_write = "write" in lprops or "write-without-response" in lprops
                has_notify = "notify" in lprops
                if has_write and not write_uuid:
                    write_uuid = char.uuid
                if has_notify and not notify_uuid:
                    notify_uuid = char.uuid

            # Read if possible
            if "read" in lprops:
                try:
                    value = await client.read_gatt_char(char)
                    hex_val = value.hex()
                    try:
                        text = value.decode("utf-8", errors="replace")
                        if text and all(32 <= ord(c) < 127 for c in text):
                            print(f"  │  Value: {hex_val} = \"{text}\"")
                        else:
                            print(f"  │  Value: {hex_val}")
                    except Exception:
                        print(f"  │  Value: {hex_val}")
                except Exception as e:
                    print(f"  │  Value: (read failed: {e})")

            for desc in char.descriptors:
                try:
                    dv = await client.read_gatt_descriptor(desc.handle)
                    print(f"  │  └─ Desc: {desc.uuid} = {dv.hex()}")
                except Exception:
                    print(f"  │  └─ Desc: {desc.uuid}")

    return write_uuid, notify_uuid


async def send_jbd_cmd(client, write_uuid, notify_uuid, cmd, timeout=5.0):
    """Send a JBD command and wait for the response."""
    resp = BLEResponse()
    await client.start_notify(notify_uuid, resp.handle)

    frame = jbd_build_read_cmd(cmd)
    print(f"  TX → {frame.hex()}")

    try:
        await client.write_gatt_char(write_uuid, frame, response=False)
    except Exception:
        await client.write_gatt_char(write_uuid, frame, response=True)

    try:
        await asyncio.wait_for(resp.event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        print(f"  ⏱  No response after {timeout}s")
        await client.stop_notify(notify_uuid)
        return None

    await client.stop_notify(notify_uuid)
    print(f"  RX ← {resp.buffer.hex()}")
    return bytes(resp.buffer)


async def probe_jbd(client, write_uuid, notify_uuid):
    """Send JBD commands and decode responses."""

    print("\n" + "=" * 60)
    print("  JBD PROTOCOL PROBE")
    print("=" * 60)
    print(f"  Write:  {write_uuid}")
    print(f"  Notify: {notify_uuid}\n")

    # CMD 0x03: Basic Info
    print("── CMD 0x03: Basic Info ──────────────────────────────────")
    raw = await send_jbd_cmd(client, write_uuid, notify_uuid, 0x03)
    if raw:
        parsed = jbd_parse_frame(raw)
        if parsed:
            chk = "✓" if parsed["checksum_ok"] else "✗"
            print(f"  Status: {parsed['status']}  Checksum: {chk}")
            if parsed["status"] == "OK":
                info = decode_basic_info(parsed["payload"])
                if "error" not in info:
                    print(f"\n  ┌─ Battery Pack ───────────────────────────────────┐")
                    print(f"  │  Voltage:      {info['total_voltage']:>8.2f} V                  │")
                    print(f"  │  Current:      {info['current']:>+8.2f} A                  │")
                    print(f"  │  SOC:          {info['remaining_soc']:>5d}   %                  │")
                    print(f"  │  Remaining:    {info['remaining_capacity']:>8.2f} Ah                 │")
                    print(f"  │  Nominal:      {info['nominal_capacity']:>8.2f} Ah                 │")
                    print(f"  │  Cycles:       {info['cycle_count']:>5d}                        │")
                    print(f"  │  Cells:        {info['cell_count']:>5d}                        │")
                    print(f"  │  Charge FET:   {info['charge_fet']:>5s}                        │")
                    print(f"  │  Discharge FET:{info['discharge_fet']:>5s}                        │")
                    if info["temperatures"]:
                        t = "  ".join(f"{t:.1f}°C" for t in info["temperatures"])
                        print(f"  │  Temps:        {t:<30s}│")
                    if info["protections"]:
                        p = ", ".join(info["protections"])
                        print(f"  │  ⚠ Active:    {p:<30s}│")
                    else:
                        print(f"  │  Protections:  None active ✓                    │")
                    print(f"  └──────────────────────────────────────────────────┘")
                else:
                    print(f"  {info['error']}")
            else:
                print(f"  Payload: {parsed['payload'].hex()}")
        else:
            print("  Could not parse as JBD frame")
    print()

    # CMD 0x04: Cell Voltages
    print("── CMD 0x04: Cell Voltages ──────────────────────────────")
    raw = await send_jbd_cmd(client, write_uuid, notify_uuid, 0x04)
    if raw:
        parsed = jbd_parse_frame(raw)
        if parsed and parsed["status"] == "OK":
            cells = decode_cell_voltages(parsed["payload"])
            chk = "✓" if parsed["checksum_ok"] else "✗"
            print(f"  Status: OK  Checksum: {chk}\n")
            for i, v in enumerate(cells):
                bar = "█" * int(v / 3.65 * 20)
                print(f"    Cell {i+1:2d}: {v:.3f}V  {bar}")
            if cells:
                print(f"\n    Max: {max(cells):.3f}V  Min: {min(cells):.3f}V  Δ: {max(cells)-min(cells):.3f}V")
        elif parsed:
            print(f"  Status: {parsed['status']}  Payload: {parsed['payload'].hex()}")
    print()

    # CMD 0x05: Hardware Version
    print("── CMD 0x05: Hardware Version ───────────────────────────")
    raw = await send_jbd_cmd(client, write_uuid, notify_uuid, 0x05)
    if raw:
        parsed = jbd_parse_frame(raw)
        if parsed and parsed["status"] == "OK":
            hw = decode_hw_version(parsed["payload"])
            chk = "✓" if parsed["checksum_ok"] else "✗"
            print(f"  Status: OK  Checksum: {chk}")
            print(f"  Version: {hw}")
        elif parsed:
            print(f"  Status: {parsed['status']}  Payload: {parsed['payload'].hex()}")
    print()


async def monitor_notifications(client, duration: float):
    """Subscribe to all notify characteristics and print traffic."""
    notify_chars = []
    for service in client.services:
        for char in service.characteristics:
            if "notify" in [p.lower() for p in char.properties]:
                notify_chars.append(char)

    if not notify_chars:
        print("No notify characteristics found.")
        return

    print("=" * 60)
    print(f"  Monitoring {len(notify_chars)} characteristics for {duration}s...")
    print("  (Open GPKWD app on your phone to capture its commands)")
    print("=" * 60)

    def make_handler(uuid):
        def handler(sender, data: bytearray):
            print(f"  [{uuid[-8:]}] ← {data.hex()}")
        return handler

    for char in notify_chars:
        try:
            await client.start_notify(char, make_handler(char.uuid))
            print(f"  Subscribed: {char.uuid}")
        except Exception as e:
            print(f"  Failed: {char.uuid} — {e}")

    await asyncio.sleep(duration)

    for char in notify_chars:
        try:
            await client.stop_notify(char)
        except Exception:
            pass


async def main(name_filter: str | None, monitor_duration: float | None):
    device = await find_bms(name_filter)
    if not device:
        print("\nNo BMS device found. Is the battery powered on?")
        print("Try: python3 scan.py --all  to see all nearby BLE devices.")
        return

    for attempt in range(3):
        try:
            print(f"\nConnecting (attempt {attempt + 1}/3)...")
            async with BleakClient(device, timeout=30) as client:
                print(f"Connected! MTU: {client.mtu_size if hasattr(client, 'mtu_size') else '?'}")

                # Step 1: Enumerate GATT
                write_uuid, notify_uuid = await enumerate_gatt(client)

                # Step 2: Try JBD protocol
                if write_uuid and notify_uuid:
                    await probe_jbd(client, write_uuid, notify_uuid)
                    print("✓ If all commands returned data, this battery speaks JBD!")
                else:
                    print(f"\n  Write char: {write_uuid or 'NOT FOUND'}")
                    print(f"  Notify char: {notify_uuid or 'NOT FOUND'}")
                    print("  Could not auto-detect BMS characteristics.")
                    print("  Check the GATT map above for write+notify chars.")

                # Step 3: Optional monitoring
                if monitor_duration:
                    await monitor_notifications(client, monitor_duration)

                print("\nDone.")
                return

        except TimeoutError:
            print(f"  Connection timed out.")
        except Exception as e:
            print(f"  Failed: {e}")
        if attempt < 2:
            print("  Retrying in 3s...")
            await asyncio.sleep(3)

    print("\nFailed to connect after 3 attempts.")
    print("Move closer to the battery and try again.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="All-in-one BLE BMS probe")
    parser.add_argument("--name", help="Filter by device name (partial match)")
    parser.add_argument("--monitor", type=float, metavar="SECS",
                        help="Monitor notifications for N seconds after probing")
    args = parser.parse_args()
    asyncio.run(main(args.name, args.monitor))

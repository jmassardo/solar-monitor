#!/usr/bin/env python3
"""
JBD BMS Protocol Probe — Try talking to the GoKWh battery.

Sends standard JBD/Xiaoxiang BMS commands over BLE and decodes the
responses. This is the "does it speak JBD?" test.

The JBD protocol frame format:
    TX: DD  A5  CMD  LEN  [DATA]  CHECKSUM_HI  CHECKSUM_LO  77
    RX: DD  CMD  STATUS  LEN  [DATA]  CHECKSUM_HI  CHECKSUM_LO  77

Commands:
    0x03 = Basic info (voltage, current, SOC, temps, protection status)
    0x04 = Cell voltages
    0x05 = Hardware version / name

Usage:
    python3 jbd_probe.py XX:XX:XX:XX:XX:XX
    python3 jbd_probe.py XX:XX:XX:XX:XX:XX --write-uuid 0000fff1-... --notify-uuid 0000fff4-...
"""

import argparse
import asyncio
import struct
from bleak import BleakClient, BleakScanner

# ── JBD Protocol ────────────────────────────────────────────────────

JBD_CMD_BASIC_INFO = 0x03
JBD_CMD_CELL_VOLTAGES = 0x04
JBD_CMD_HW_VERSION = 0x05

# Common BLE characteristic UUIDs for JBD BMS
DEFAULT_WRITE_HINTS = ["0000fff1", "0000ffe1", "0000ff01"]
DEFAULT_NOTIFY_HINTS = ["0000fff4", "0000fff2", "0000ffe1", "0000ff02"]


def jbd_build_read_cmd(cmd: int) -> bytes:
    """Build a JBD read request frame."""
    length = 0x00
    checksum = 0x10000 - (cmd + length)
    return bytes([0xDD, 0xA5, cmd, length,
                  (checksum >> 8) & 0xFF, checksum & 0xFF, 0x77])


def jbd_parse_frame(data: bytes) -> dict | None:
    """Parse a JBD response frame."""
    if len(data) < 7:
        return None
    if data[0] != 0xDD or data[-1] != 0x77:
        return None

    cmd = data[1]
    status = data[2]  # 0x00 = OK, 0x80 = error
    length = data[3]
    payload = data[4:4 + length]

    chk_bytes = data[4 + length:4 + length + 2]
    if len(chk_bytes) < 2:
        return None
    received_chk = (chk_bytes[0] << 8) | chk_bytes[1]
    calc_sum = sum(payload) + cmd + status + length
    calc_chk = 0x10000 - (calc_sum & 0xFFFF)

    return {
        "cmd": cmd,
        "status": "OK" if status == 0x00 else f"ERROR(0x{status:02X})",
        "length": length,
        "payload": payload,
        "checksum_ok": (received_chk & 0xFFFF) == (calc_chk & 0xFFFF),
    }


def decode_basic_info(payload: bytes) -> dict:
    """Decode cmd 0x03 basic info response."""
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
    info["protections"] = []
    prot_names = [
        "Cell OVP", "Cell UVP", "Pack OVP", "Pack UVP",
        "Charge OT", "Charge UT", "Discharge OT", "Discharge UT",
        "Charge OC", "Discharge OC", "Short Circuit", "IC Error",
        "FET Lock", "Reserved13", "Reserved14", "Reserved15",
    ]
    for bit, name in enumerate(prot_names):
        if prot & (1 << bit):
            info["protections"].append(name)

    info["charge_fet"] = "ON" if info["fet_status"] & 0x01 else "OFF"
    info["discharge_fet"] = "ON" if info["fet_status"] & 0x02 else "OFF"

    return info


def decode_cell_voltages(payload: bytes) -> list[float]:
    """Decode cmd 0x04 cell voltages response."""
    count = len(payload) // 2
    return [struct.unpack(">H", payload[i*2:(i+1)*2])[0] / 1000.0
            for i in range(count)]


def decode_hw_version(payload: bytes) -> str:
    """Decode cmd 0x05 hardware version string."""
    try:
        return payload.decode("ascii", errors="replace").strip("\x00")
    except Exception:
        return payload.hex()


# ── BLE Communication ───────────────────────────────────────────────

class BLEResponse:
    """Collects fragmented BLE notifications into a complete frame."""
    def __init__(self):
        self.buffer = bytearray()
        self.event = asyncio.Event()

    def handle(self, sender, data: bytearray):
        self.buffer.extend(data)
        if len(self.buffer) >= 7 and self.buffer[-1] == 0x77:
            self.event.set()

    def reset(self):
        self.buffer = bytearray()
        self.event.clear()


async def find_working_chars(client):
    """Auto-detect write and notify characteristic UUIDs."""
    write_char = None
    notify_char = None

    for service in client.services:
        for char in service.characteristics:
            props = [p.lower() for p in char.properties]
            uuid = char.uuid.lower()

            if not write_char and ("write" in props or "write-without-response" in props):
                if any(uuid.startswith(hint) for hint in DEFAULT_WRITE_HINTS):
                    write_char = char.uuid
            if not notify_char and "notify" in props:
                if any(uuid.startswith(hint) for hint in DEFAULT_NOTIFY_HINTS):
                    notify_char = char.uuid

    return write_char, notify_char


async def send_command(client, write_uuid: str, notify_uuid: str,
                       cmd: int, timeout: float = 5.0) -> bytes | None:
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


async def probe(address: str, write_uuid: str | None, notify_uuid: str | None):
    print(f"Scanning for {address}...")
    device = await BleakScanner.find_device_by_address(address, timeout=15)
    if not device:
        device = await BleakScanner.find_device_by_name(address, timeout=15)
    if not device:
        print("Device not found. Make sure it's powered on and nearby.")
        return

    print(f"Found: {device.name or '(unnamed)'} [{device.address}]")
    print(f"Connecting...")

    async with BleakClient(device, timeout=20) as client:
        print(f"Connected!\n")

        if not write_uuid or not notify_uuid:
            print("Auto-detecting write/notify characteristics...")
            w, n = await find_working_chars(client)
            write_uuid = write_uuid or w
            notify_uuid = notify_uuid or n

        if not write_uuid or not notify_uuid:
            print("ERROR: Could not find write/notify characteristics.")
            print("Run enumerate.py first, then pass --write-uuid / --notify-uuid.")
            return

        print(f"  Write:  {write_uuid}")
        print(f"  Notify: {notify_uuid}\n")

        # ── CMD 0x03: Basic Info ────────────────────────────────────
        print("=" * 60)
        print("  CMD 0x03 — BASIC INFO")
        print("=" * 60)

        raw = await send_command(client, write_uuid, notify_uuid, JBD_CMD_BASIC_INFO)
        if raw:
            parsed = jbd_parse_frame(raw)
            if parsed:
                print(f"  Status: {parsed['status']}  Checksum: {'✓' if parsed['checksum_ok'] else '✗'}")
                if parsed["status"] == "OK":
                    info = decode_basic_info(parsed["payload"])
                    if "error" in info:
                        print(f"  {info['error']}")
                    else:
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
                        if info['temperatures']:
                            temps_str = "  ".join(f"{t:.1f}°C" for t in info['temperatures'])
                            print(f"  │  Temps:        {temps_str:<30s}│")
                        if info['protections']:
                            print(f"  │  ⚠ Active:    {', '.join(info['protections']):<30s}│")
                        else:
                            print(f"  │  Protections:  None active ✓                    │")
                        print(f"  └──────────────────────────────────────────────────┘")
                else:
                    print(f"  Payload: {parsed['payload'].hex()}")
            else:
                print("  Failed to parse response frame")
        print()

        # ── CMD 0x04: Cell Voltages ─────────────────────────────────
        print("=" * 60)
        print("  CMD 0x04 — CELL VOLTAGES")
        print("=" * 60)

        raw = await send_command(client, write_uuid, notify_uuid, JBD_CMD_CELL_VOLTAGES)
        if raw:
            parsed = jbd_parse_frame(raw)
            if parsed and parsed["status"] == "OK":
                cells = decode_cell_voltages(parsed["payload"])
                print(f"  Status: OK  Checksum: {'✓' if parsed['checksum_ok'] else '✗'}")
                print(f"\n  Cell voltages ({len(cells)} cells):")
                for i, v in enumerate(cells):
                    bar = "█" * int(v / 3.65 * 20)
                    print(f"    Cell {i+1:2d}: {v:.3f}V  {bar}")
                if cells:
                    delta = max(cells) - min(cells)
                    print(f"\n    Max: {max(cells):.3f}V  Min: {min(cells):.3f}V  Δ: {delta:.3f}V")
            elif parsed:
                print(f"  Status: {parsed['status']}")
                print(f"  Payload: {parsed['payload'].hex()}")
        print()

        # ── CMD 0x05: Hardware Version ──────────────────────────────
        print("=" * 60)
        print("  CMD 0x05 — HARDWARE VERSION")
        print("=" * 60)

        raw = await send_command(client, write_uuid, notify_uuid, JBD_CMD_HW_VERSION)
        if raw:
            parsed = jbd_parse_frame(raw)
            if parsed and parsed["status"] == "OK":
                hw = decode_hw_version(parsed["payload"])
                print(f"  Status: OK  Checksum: {'✓' if parsed['checksum_ok'] else '✗'}")
                print(f"  Version: {hw}")
            elif parsed:
                print(f"  Status: {parsed['status']}")
                print(f"  Payload: {parsed['payload'].hex()}")

        print()
        print("Done! If all three commands returned data, this battery speaks JBD.")
        print("If not, sniff GPKWD traffic with: enumerate.py <addr> --monitor")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="JBD BMS BLE protocol probe")
    parser.add_argument("address", help="BLE MAC address of the battery")
    parser.add_argument("--write-uuid", help="Override write characteristic UUID")
    parser.add_argument("--notify-uuid", help="Override notify characteristic UUID")
    args = parser.parse_args()
    asyncio.run(probe(args.address, args.write_uuid, args.notify_uuid))

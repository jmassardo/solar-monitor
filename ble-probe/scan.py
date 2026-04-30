#!/usr/bin/env python3
"""
BLE Scanner — Find the GoKWh battery.

Scans for nearby BLE devices and prints name, MAC, RSSI, and any
service UUIDs advertised. Look for anything with "BMS", "battery",
"JBD", "Smart", or the GPKWD-style service UUIDs (fff0, ffe0).

Usage:
    python3 scan.py              # 10-second scan
    python3 scan.py --duration 30  # longer scan
    python3 scan.py --all        # show ALL devices (noisy)
"""

import argparse
import asyncio
from bleak import BleakScanner

# UUIDs commonly used by JBD / Chinese BMS devices
BMS_SERVICE_HINTS = {"0000fff0", "0000ffe0", "0000ff00"}

# Name substrings that suggest a BMS
BMS_NAME_HINTS = [
    "bms", "battery", "jbd", "smart", "gpkwd", "gokwh",
    "xiaoxiang", "overkill", "sp1", "sp2",
]


def is_likely_bms(name: str, uuids: list[str]) -> bool:
    lower = (name or "").lower()
    if any(hint in lower for hint in BMS_NAME_HINTS):
        return True
    for uuid in uuids:
        if any(uuid.lower().startswith(h) for h in BMS_SERVICE_HINTS):
            return True
    return False


async def main(duration: float, show_all: bool):
    print(f"Scanning for BLE devices ({duration}s)...\n")

    devices = await BleakScanner.discover(
        timeout=duration,
        return_adv=True,
    )

    bms_hits = []
    other = []

    for addr, (device, adv) in devices.items():
        name = adv.local_name or device.name or ""
        uuids = adv.service_uuids or []
        rssi = adv.rssi
        entry = (name, addr, rssi, uuids)

        if is_likely_bms(name, uuids):
            bms_hits.append(entry)
        else:
            other.append(entry)

    # Print likely BMS devices first
    if bms_hits:
        print("=" * 70)
        print("  LIKELY BMS DEVICES")
        print("=" * 70)
        for name, addr, rssi, uuids in sorted(bms_hits, key=lambda x: -x[2]):
            print(f"  ★ {name or '(unnamed)':30s}  {addr}  RSSI: {rssi}")
            if uuids:
                print(f"    Services: {', '.join(uuids)}")
        print()
    else:
        print("  No obvious BMS devices found. Try --all to see everything,")
        print("  or make sure the battery is powered on and BLE is active.\n")

    if show_all and other:
        print("=" * 70)
        print("  ALL OTHER DEVICES")
        print("=" * 70)
        for name, addr, rssi, uuids in sorted(other, key=lambda x: -x[2]):
            print(f"    {name or '(unnamed)':30s}  {addr}  RSSI: {rssi}")
            if uuids:
                print(f"      Services: {', '.join(uuids)}")
        print()

    total = len(bms_hits) + len(other)
    print(f"Found {total} devices total, {len(bms_hits)} likely BMS.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BLE scanner for GoKWh battery")
    parser.add_argument("--duration", type=float, default=10, help="Scan duration in seconds")
    parser.add_argument("--all", action="store_true", help="Show all devices")
    args = parser.parse_args()
    asyncio.run(main(args.duration, args.all))

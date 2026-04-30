#!/usr/bin/env python3
"""
BLE Service Enumerator — Map out the GoKWh battery's GATT profile.

Connects to a BLE device by MAC address and dumps every service,
characteristic, and descriptor with properties. This is the key step
to figure out which characteristic to write commands to and which
one sends notifications back.

Usage:
    python3 enumerate.py XX:XX:XX:XX:XX:XX
    python3 enumerate.py XX:XX:XX:XX:XX:XX --monitor   # also subscribe to all notify chars
"""

import argparse
import asyncio
from bleak import BleakClient, BleakScanner

# Well-known 16-bit UUID descriptions (BMS-relevant subset)
KNOWN_UUIDS = {
    "00002a19": "Battery Level",
    "00002a00": "Device Name",
    "00002a01": "Appearance",
    "00002a29": "Manufacturer Name",
    "00002a24": "Model Number",
    "00002a25": "Serial Number",
    "00002a26": "Firmware Revision",
    "00002a27": "Hardware Revision",
    "0000fff0": "JBD/BMS UART Service",
    "0000fff1": "JBD/BMS Write Char",
    "0000fff2": "JBD/BMS Read/Notify Char",
    "0000fff4": "JBD/BMS Notify Char (alt)",
    "0000ffe0": "BMS Service (alt)",
    "0000ffe1": "BMS Char (alt)",
    "0000ff00": "BMS Service (alt2)",
    "0000ff01": "BMS Write (alt2)",
    "0000ff02": "BMS Notify (alt2)",
}


def uuid_hint(uuid_str: str) -> str:
    short = uuid_str[:8].lower()
    return KNOWN_UUIDS.get(short, "")


def props_str(props: list[str]) -> str:
    flag_map = {
        "read": "R",
        "write": "W",
        "write-without-response": "Wn",
        "notify": "N",
        "indicate": "I",
    }
    return " ".join(flag_map.get(p.lower(), p) for p in props)


async def enumerate_services(address: str, monitor: bool, monitor_duration: float):
    print(f"Scanning for {address}...")
    device = await BleakScanner.find_device_by_address(address, timeout=15)
    if not device:
        # Also try matching by name (the advertised local name)
        print(f"  Not found by address, scanning by name...")
        device = await BleakScanner.find_device_by_name(address, timeout=15)
    if not device:
        print(f"  Device not found. Make sure it's powered on and nearby.")
        return

    print(f"Found: {device.name or '(unnamed)'} [{device.address}]")

    for attempt in range(3):
        try:
            print(f"Connecting (attempt {attempt + 1}/3, 30s timeout)...")
            async with BleakClient(device, timeout=30) as client:
                print(f"Connected: {client.is_connected}")
                print(f"MTU: {client.mtu_size if hasattr(client, 'mtu_size') else 'unknown'}\n")
                await _do_enumerate(client, monitor, monitor_duration)
                return
        except TimeoutError:
            print(f"  Connection timed out.")
        except Exception as e:
            print(f"  Connection failed: {e}")
        if attempt < 2:
            print(f"  Retrying in 2s...")
            await asyncio.sleep(2)

    print("Failed to connect after 3 attempts.")


async def _do_enumerate(client, monitor: bool, monitor_duration: float):

    notify_chars = []

    for service in client.services:
        hint = uuid_hint(service.uuid)
        label = f"  ({hint})" if hint else ""
        print(f"Service: {service.uuid}{label}")
        print(f"  Handle: 0x{service.handle:04X}")

        for char in service.characteristics:
            chint = uuid_hint(char.uuid)
            clabel = f"  ({chint})" if chint else ""
            flags = props_str(char.properties)
            print(f"  ├─ Char: {char.uuid}  [{flags}]{clabel}")
            print(f"  │  Handle: 0x{char.handle:04X}")

            # Try to read if readable
            if "read" in [p.lower() for p in char.properties]:
                try:
                    value = await client.read_gatt_char(char)
                    hex_val = value.hex()
                    try:
                        text = value.decode("utf-8", errors="replace")
                        if all(32 <= ord(c) < 127 or c in "\n\r\t" for c in text):
                            print(f"  │  Value: {hex_val} = \"{text}\"")
                        else:
                            print(f"  │  Value: {hex_val}")
                    except Exception:
                        print(f"  │  Value: {hex_val}")
                except Exception as e:
                    print(f"  │  Value: (read failed: {e})")

            if "notify" in [p.lower() for p in char.properties]:
                notify_chars.append(char)

            for desc in char.descriptors:
                print(f"  │  └─ Desc: {desc.uuid} (0x{desc.handle:04X})")
                try:
                    dv = await client.read_gatt_descriptor(desc.handle)
                    print(f"  │     Value: {dv.hex()}")
                except Exception:
                    pass

        print()

    # Monitor mode: subscribe to all notify characteristics
    if monitor and notify_chars:
        print("=" * 60)
        print(f"Monitoring {len(notify_chars)} notify characteristics for {monitor_duration}s...")
        print("(Open the GPKWD app on your phone to see traffic)")
        print("=" * 60)

        def make_handler(char_uuid):
            def handler(sender, data: bytearray):
                print(f"  [{char_uuid[-8:]}] ← {data.hex()}")
            return handler

        for char in notify_chars:
            try:
                await client.start_notify(char, make_handler(char.uuid))
                print(f"  Subscribed to {char.uuid}")
            except Exception as e:
                print(f"  Failed to subscribe {char.uuid}: {e}")

        await asyncio.sleep(monitor_duration)

        for char in notify_chars:
            try:
                await client.stop_notify(char)
            except Exception:
                pass

    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BLE GATT service enumerator")
    parser.add_argument("address", help="BLE MAC address (XX:XX:XX:XX:XX:XX)")
    parser.add_argument("--monitor", action="store_true",
                        help="Subscribe to notify chars and print incoming data")
    parser.add_argument("--duration", type=float, default=30,
                        help="Monitor duration in seconds (default: 30)")
    args = parser.parse_args()
    asyncio.run(enumerate_services(args.address, args.monitor, args.duration))

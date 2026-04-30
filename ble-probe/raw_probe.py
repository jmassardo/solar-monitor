#!/usr/bin/env python3
"""
Raw BLE probe — try everything, log everything.

Connects to the XDZN BMS device and:
1. Subscribes to ALL notify characteristics
2. Sends JBD commands on fff2 and the alt service write char
3. Dumps all raw traffic

This is the "throw everything at the wall" approach.
"""

import asyncio
import sys
from bleak import BleakClient, BleakScanner

# JBD read commands
JBD_BASIC  = bytes.fromhex("dda50300fffd77")
JBD_CELLS  = bytes.fromhex("dda50400fffc77")
JBD_HW     = bytes.fromhex("dda50500fffb77")

# Some BMS use a simpler request format
ALT_CMD_1  = bytes.fromhex("5aa50300")      # alternate start byte
ALT_CMD_2  = bytes.fromhex("3a030000000032")  # PACE/XDZN style?


async def main():
    name_match = sys.argv[1] if len(sys.argv) > 1 else "XDZN"

    print(f"Scanning for '{name_match}'...")
    device = None
    devices = await BleakScanner.discover(timeout=15, return_adv=True)
    for addr, (dev, adv) in devices.items():
        name = adv.local_name or dev.name or ""
        if name_match.lower() in name.lower():
            print(f"  Found: {name} [{addr}] RSSI: {adv.rssi}")
            device = dev
            break

    if not device:
        print("Not found!")
        return

    print("Connecting...")
    async with BleakClient(device, timeout=30) as client:
        print(f"Connected, MTU: {client.mtu_size}")

        # Force service property access and give CoreBluetooth time to settle
        svcs = client.services
        print(f"Services discovered: {len(list(svcs))}")
        await asyncio.sleep(2)
        print()

        # Collect all characteristics by type
        write_chars = []
        notify_chars = []
        all_chars = []

        for service in client.services:
            print(f"Service: {service.uuid}")
            for char in service.characteristics:
                props = [p.lower() for p in char.properties]
                pstr = "/".join(props)
                print(f"  {char.uuid}  [{pstr}]")
                all_chars.append((char, props))
                if "write" in props or "write-without-response" in props:
                    write_chars.append(char)
                if "notify" in props:
                    notify_chars.append(char)

        print(f"\n{len(write_chars)} writable, {len(notify_chars)} notifiable\n")

        # Subscribe to ALL notify characteristics
        print("=" * 60)
        print("  SUBSCRIBING TO ALL NOTIFICATIONS")
        print("=" * 60)

        def make_handler(uuid):
            short = uuid.split("-")[0]
            def handler(sender, data: bytearray):
                print(f"  ◀ [{short}] {data.hex()}  ({len(data)} bytes)")
                # Also try to decode as ASCII
                try:
                    text = data.decode("ascii", errors="replace")
                    if any(32 <= ord(c) < 127 for c in text):
                        printable = "".join(c if 32 <= ord(c) < 127 else "." for c in text)
                        print(f"           ASCII: {printable}")
                except Exception:
                    pass
            return handler

        for char in notify_chars:
            try:
                await client.start_notify(char, make_handler(char.uuid))
                print(f"  ✓ Subscribed: {char.uuid}")
            except Exception as e:
                print(f"  ✗ Failed: {char.uuid} — {e}")

        # Wait a moment to see if BMS sends anything unsolicited
        print(f"\n  Listening for unsolicited data (5s)...")
        await asyncio.sleep(5)

        # Try writing JBD commands to each writable characteristic
        print("\n" + "=" * 60)
        print("  SENDING COMMANDS")
        print("=" * 60)

        commands = [
            ("JBD basic info", JBD_BASIC),
            ("JBD cell voltages", JBD_CELLS),
            ("JBD hw version", JBD_HW),
            ("ALT cmd 1", ALT_CMD_1),
            ("ALT cmd 2", ALT_CMD_2),
        ]

        for char in write_chars:
            for cmd_name, cmd_bytes in commands:
                try:
                    print(f"\n  ▶ [{char.uuid.split('-')[0]}] {cmd_name}: {cmd_bytes.hex()}")
                    props = [p.lower() for p in char.properties]
                    if "write-without-response" in props:
                        await client.write_gatt_char(char, cmd_bytes, response=False)
                    else:
                        await client.write_gatt_char(char, cmd_bytes, response=True)
                    print(f"    Write OK, waiting 3s for response...")
                    await asyncio.sleep(3)
                except Exception as e:
                    print(f"    Write failed: {e}")

        # Also try reading all readable characteristics
        print("\n" + "=" * 60)
        print("  READING ALL READABLE CHARACTERISTICS")
        print("=" * 60)

        for char, props in all_chars:
            if "read" in props:
                try:
                    value = await client.read_gatt_char(char)
                    if value:
                        print(f"  {char.uuid}: {value.hex()} ({len(value)} bytes)")
                        try:
                            text = value.decode("ascii", errors="replace")
                            printable = "".join(c if 32 <= ord(c) < 127 else "." for c in text)
                            if any(c != "." for c in printable):
                                print(f"    ASCII: {printable}")
                        except Exception:
                            pass
                    else:
                        print(f"  {char.uuid}: (empty)")
                except Exception as e:
                    print(f"  {char.uuid}: (error: {e})")

        # Cleanup
        for char in notify_chars:
            try:
                await client.stop_notify(char)
            except Exception:
                pass

        print("\nDone.")


asyncio.run(main())

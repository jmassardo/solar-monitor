import asyncio
import json
import signal
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import solar_monitor_v2 as app


MAC = "10:CA:BF:AA:BA:3D"
REGISTERS = [97, 131, 0, 0x1819, 0, 0, 0, 45, 0, 0]
LIVE_FRAME = bytes.fromhex("ff03140061008300001819000000000000002d00000000c1a0")


def response(registers, address=255, function=3):
    data = b"".join(value.to_bytes(2, "big") for value in registers)
    body = bytes([address, function, len(data)]) + data
    return body + app.crc_modbus(body).to_bytes(2, "little")


class ProtocolTests(unittest.TestCase):
    def test_requests_match_hardware_verified_read_only_frames(self):
        self.assertEqual(app.wanderer_request(255, 0x0100, 10).hex(),
                         "ff030100000ad1ef")
        self.assertEqual(app.wanderer_request(255, 0x0120, 1).hex(),
                         "ff030120000191e2")
        for address, start, count in [(0, 256, 10), (256, 256, 10),
                                      (255, -1, 1), (255, 0, 126)]:
            with self.subTest(address=address, start=start, count=count):
                with self.assertRaises(ValueError):
                    app.wanderer_request(address, start, count)

    def test_decode_live_crc_checked_frame_and_nighttime_zeroes(self):
        registers = app.decode_wanderer_response(LIVE_FRAME, 255, 10)
        self.assertEqual(registers, REGISTERS)
        self.assertEqual(app.decode_wanderer_data(registers, 0), {
            "solar_voltage": 4.5, "solar_current": 0.0, "solar_power": 0,
            "battery_voltage": 13.1, "charge_current": 0.0, "charge_power": 0.0,
            "controller_temp": 24, "battery_temp": 25, "soc": 97,
            "charge_state": "Deactivated",
        })

    def test_charge_scaling_signed_temperatures_and_state_low_byte(self):
        registers = [75, 136, 1234, 0x858A, 0, 0, 0, 185, 907, 168]
        data = app.decode_wanderer_data(registers, 0x8005)
        self.assertEqual(data["charge_current"], 12.34)
        self.assertEqual(data["charge_power"], 167.82)
        self.assertEqual(data["solar_voltage"], 18.5)
        self.assertEqual(data["solar_current"], 9.07)
        self.assertEqual(data["controller_temp"], -5)
        self.assertEqual(data["battery_temp"], -10)
        self.assertEqual(data["charge_state"], "Float")
        self.assertEqual(app.decode_wanderer_data(registers, 99)["charge_state"],
                         "Unknown (99)")
        registers[0] = 101
        with self.assertRaises(ValueError):
            app.decode_wanderer_data(registers, 0)

    def test_reject_bad_crc_address_function_count_and_truncation(self):
        frames = [LIVE_FRAME[:-1], LIVE_FRAME + b"\x00",
                  LIVE_FRAME[:-1] + bytes([LIVE_FRAME[-1] ^ 1]),
                  response(REGISTERS, address=1),
                  response(REGISTERS, function=4), response(REGISTERS[:9]), b""]
        for frame in frames:
            with self.subTest(frame=frame.hex()):
                with self.assertRaises(ValueError):
                    app.decode_wanderer_response(frame, 255, 10)

    def test_modbus_exception_is_explicit(self):
        body = bytes.fromhex("ff8302")
        frame = body + app.crc_modbus(body).to_bytes(2, "little")
        with self.assertRaisesRegex(ValueError, "exception 0x02"):
            app.decode_wanderer_response(frame, 255, 10)

    def test_discovery_preserves_ids_and_gates_stale_or_dead_process(self):
        client = Mock()
        app.publish_wanderer_discovery(client, 240)
        self.assertEqual(client.publish.call_count, 10)
        objects = set()
        for call in client.publish.call_args_list:
            topic, payload = call.args
            config = json.loads(payload)
            object_id = topic.split("/")[-2]
            objects.add(object_id)
            self.assertEqual(topic, f"homeassistant/sensor/solar_wanderer/{object_id}/config")
            self.assertEqual(config["unique_id"], f"solar_wanderer_{object_id}")
            self.assertEqual(config["device"]["identifiers"], ["solar_renogy_wanderer"])
            self.assertEqual(config["device"]["model"], "Wanderer Li 30A PWM")
            self.assertEqual(config["state_topic"], f"solar_wanderer/{object_id}/state")
            self.assertEqual(config["availability_mode"], "all")
            self.assertEqual(config["availability"], [
                {"topic": "solar_wanderer/availability"},
                {"topic": "solar_gokwh/availability"},
            ])
            self.assertEqual(config["expire_after"], 240)
            self.assertEqual(call.kwargs, {"qos": 1, "retain": True})
            if object_id == "charge_state":
                self.assertNotIn("state_class", config)
        self.assertEqual(objects, set(app.decode_wanderer_data(REGISTERS, 0)))

    def test_existing_battery_discovery_not_changed(self):
        client = Mock()
        app.publish_gokwh_discovery(client, app.GOKWH_PREFIX, app.GOKWH_DEVICE)
        config = json.loads(client.publish.call_args_list[0].args[1])
        self.assertEqual(config["availability_topic"], "solar_gokwh/availability")
        self.assertNotIn("expire_after", config)


class PollTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self, frames=None, fragments=True):
        client = Mock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.services.get_characteristic.side_effect = lambda uuid: uuid
        handler = None
        frames = iter(frames if frames is not None else [LIVE_FRAME, response([0])])

        async def subscribe(_char, callback):
            nonlocal handler
            handler = callback

        async def write(_char, command, response):
            frame = next(frames)
            if fragments:
                for byte in frame:
                    handler(None, bytes([byte]))
            else:
                handler(None, frame)

        client.start_notify = AsyncMock(side_effect=subscribe)
        client.write_gatt_char = AsyncMock(side_effect=write)
        return client

    async def test_fragmented_notifications_and_only_two_function03_requests(self):
        client = self.make_client()
        with patch.object(app.BleakScanner, "find_device_by_address", new=AsyncMock(return_value=MAC)), \
                patch.object(app, "BleakClient", return_value=client):
            data = await app.poll_wanderer(MAC)
        self.assertEqual(data["battery_voltage"], 13.1)
        client.start_notify.assert_awaited_once()
        client.disconnect.assert_awaited_once()
        requests = [call.args[1] for call in client.write_gatt_char.await_args_list]
        self.assertEqual(requests, [
            app.wanderer_request(255, 0x0100, 10),
            app.wanderer_request(255, 0x0120, 1),
        ])

    async def test_crc_failure_disconnects_without_second_read(self):
        client = self.make_client([LIVE_FRAME[:-1] + b"\x00"])
        with patch.object(app.BleakScanner, "find_device_by_address", new=AsyncMock(return_value=MAC)), \
                patch.object(app, "BleakClient", return_value=client):
            with self.assertRaisesRegex(ValueError, "CRC"):
                await app.poll_wanderer(MAC)
        client.disconnect.assert_awaited_once()
        self.assertEqual(client.write_gatt_char.await_count, 1)

    async def test_missing_device_or_characteristic_is_error(self):
        with patch.object(app.BleakScanner, "find_device_by_address", new=AsyncMock(return_value=None)):
            with self.assertRaisesRegex(RuntimeError, "not found"):
                await app.poll_wanderer(MAC)
        client = self.make_client()
        client.services.get_characteristic.side_effect = None
        client.services.get_characteristic.return_value = None
        with patch.object(app.BleakScanner, "find_device_by_address", new=AsyncMock(return_value=MAC)), \
                patch.object(app, "BleakClient", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "characteristics"):
                await app.poll_wanderer(MAC)
        client.disconnect.assert_awaited_once()

    async def test_response_timeout_and_cancellation_disconnect(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                client = self.make_client([b""])
                with patch.object(app.BleakScanner, "find_device_by_address", new=AsyncMock(return_value=MAC)), \
                        patch.object(app, "BleakClient", return_value=client), \
                        patch.object(app, "BLE_CMD_TIMEOUT", 0.01):
                    task = asyncio.create_task(app.poll_wanderer(MAC))
                    await asyncio.sleep(0.001)
                    if cancel:
                        task.cancel()
                    with self.assertRaises(asyncio.CancelledError if cancel else TimeoutError):
                        await task
                client.disconnect.assert_awaited_once()

    async def test_full_sample_published_before_online(self):
        client = Mock()
        data = app.decode_wanderer_data(REGISTERS, 0)
        with patch.object(app, "poll_wanderer", new=AsyncMock(return_value=data)):
            self.assertTrue(await app.update_wanderer(client, MAC, 255))
        self.assertEqual(client.publish.call_count, 11)
        client.publish.assert_called_with("solar_wanderer/availability", "online",
                                          qos=1, retain=True)
        for call in client.publish.call_args_list:
            self.assertEqual(call.kwargs, {"qos": 1, "retain": True})

    async def test_failures_publish_only_offline_never_zeroes(self):
        for error in (ValueError("CRC mismatch"), TimeoutError(), RuntimeError("not found")):
            with self.subTest(error=error), \
                    patch.object(app, "poll_wanderer", new=AsyncMock(side_effect=error)):
                client = Mock()
                with self.assertLogs(app.log, level="WARNING"):
                    self.assertFalse(await app.update_wanderer(client, MAC, 255))
                client.publish.assert_called_once_with(
                    "solar_wanderer/availability", "offline", qos=1, retain=True)

    async def test_whole_poll_is_bounded(self):
        async def stalled(*_args):
            await asyncio.Event().wait()

        with patch.object(app, "poll_wanderer", stalled), \
                patch.object(app, "BLE_POLL_TIMEOUT", 0.01):
            client = Mock()
            with self.assertLogs(app.log, level="WARNING"):
                self.assertFalse(await app.update_wanderer(client, MAC, 255))
            client.publish.assert_called_once_with(
                "solar_wanderer/availability", "offline", qos=1, retain=True)

    async def test_invalid_configuration_fails_before_mqtt_or_ble(self):
        for options in [
                ["--wanderer-mac", "not-a-mac"], ["--wanderer-address", "0"],
                ["--wanderer-address", "256"], ["--poll-interval", "0"],
                ["--mac1", MAC, "--wanderer-mac", MAC]]:
            with self.subTest(options=options), \
                    patch("sys.argv", ["solar_monitor_v2.py"] + options), \
                    patch.dict("os.environ", {}, clear=True), \
                    patch("sys.stderr"), \
                    patch.object(app.mqtt, "Client") as client:
                with self.assertRaises(SystemExit) as raised:
                    await app.main()
                self.assertEqual(raised.exception.code, 2)
                client.assert_not_called()

    async def test_main_serializes_all_ble_and_keeps_pi_loop_independent(self):
        await self.run_main(wanderer=True, rounds=6)

    async def test_main_disable_skips_controller_and_marks_offline(self):
        await self.run_main(wanderer=False, rounds=1)

    async def run_main(self, wanderer, rounds):
        loop = asyncio.get_running_loop()
        real_sleep = asyncio.sleep
        handlers = {}
        order = []
        controller_calls = 0
        client = Mock()
        pi = Mock()
        pi_updates_during_ble = []

        async def sleep(_delay):
            await real_sleep(0.001)

        async def battery(mac):
            order.append(mac)
            await real_sleep(0.003)
            pi_updates_during_ble.append(pi.update.call_count)
            if not wanderer and mac == "B2":
                handlers[signal.SIGTERM]()
            return None, None

        async def controller(*_args):
            nonlocal controller_calls
            order.append("BT1")
            controller_calls += 1
            if controller_calls == rounds:
                handlers[signal.SIGTERM]()
            return True

        argv = ["solar_monitor_v2.py", "--mac1", "B1", "--mac2", "B2",
                "--wanderer-mac", MAC, "--poll-interval", "1"]
        if not wanderer:
            argv.append("--no-wanderer")
        with patch("sys.argv", argv), patch.dict("os.environ", {}, clear=True), \
                patch.object(loop, "add_signal_handler", side_effect=lambda sig, fn: handlers.update({sig: fn})), \
                patch.object(app.mqtt, "Client", return_value=client), \
                patch.object(app, "PiManager", return_value=pi), \
                patch.object(app, "HAS_GPIO", True), \
                patch.object(app, "poll_battery", side_effect=battery), \
                patch.object(app, "update_wanderer", side_effect=controller) as update, \
                patch.object(app, "reset_bluez", new=AsyncMock()) as reset, \
                patch.object(app.asyncio, "sleep", side_effect=sleep):
            await asyncio.wait_for(app.main(), 2)
            client.on_connect(client, None, None, SimpleNamespace(is_failure=False), None)
            client.subscribe.assert_any_call("homeassistant/status")
            if wanderer:
                self.assertEqual(order, ["B1", "B2", "BT1"] * rounds)
                reset.assert_not_awaited()
                before = client.publish.call_count
                client.on_message(client, None, SimpleNamespace(
                    topic="homeassistant/status", payload=b"online"))
                self.assertEqual(client.publish.call_count - before, 10)
            else:
                update.assert_not_called()
        self.assertGreater(pi.update.call_count, 0)
        self.assertTrue(all(count > 0 for count in pi_updates_during_ble))
        client.publish.assert_any_call("solar_wanderer/availability", "offline",
                                       qos=1, retain=True)
        pi.shutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()

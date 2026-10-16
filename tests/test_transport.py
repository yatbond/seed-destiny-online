"""Real TCP tests of the room relay and byte-exact upstream emulator framing.

These tests do not boot an emulator or prove a game match / Internet latency.
"""
import asyncio
import json
from pathlib import Path
import shutil
import ssl
import struct
import subprocess
import tempfile
import unittest

from seedrooms.client import EmulatorBridge, RoomClient, RoomError
from seedrooms.transport import FRAME, HEADER, JSON, MAX_JSON, RelayServer


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Source files reside on a network drive; traceback lookups can be slow.
        asyncio.get_running_loop().slow_callback_duration = 1.0
        self.server = await RelayServer(auth_attempts=40).start()
        self.clients = []
        self.bridges = []
        self.emulators = []

    async def asyncTearDown(self):
        for _, writer in self.emulators:
            writer.close()
            await writer.wait_closed()
        for bridge in self.bridges:
            await bridge.close()
        for client in self.clients:
            await client.close()
        await self.server.close()
        self.assertEqual(self.server.peers, set())
        self.assertEqual(self.server.rooms, {})

    async def connect(self):
        client = await RoomClient.connect("127.0.0.1", self.server.port)
        self.clients.append(client)
        return client

    async def room(self, count=4):
        creator = await self.connect()
        result = await creator.create("play-test-pass", "Host")
        self.assertNotIn("session_token", result)
        members = [creator]
        for i in range(1, count):
            peer = await self.connect()
            await peer.join(creator.room_code, "play-test-pass", f"Player {i + 1}")
            members.append(peer)
        return members

    async def wait_members(self, client, count):
        async with asyncio.timeout(3):
            while len((await client.status())["members"]) != count:
                await asyncio.sleep(.01)

    async def test_four_slots_and_ready_start_are_synchronized(self):
        members = await self.room()
        self.assertEqual([c.slot for c in members], ["A1", "A2", "B1", "B2"])
        self.assertEqual([c.cabinet_id for c in members], [1, 2, 3, 4])
        with self.assertRaisesRegex(RoomError, "Four ready"):
            await members[0].start_match()
        await asyncio.gather(*(c.ready() for c in members))
        with self.assertRaisesRegex(RoomError, "Only the room host"):
            await members[1].start_match()
        state = await members[0].start_match()
        self.assertTrue(state["started"])
        for member in members:
            self.assertEqual(await member.status(), state)
            self.assertTrue(any(event["event"] == "state" for event in list(member.events._queue)))
        serialized = json.dumps(state)
        self.assertNotIn("play-test-pass", serialized)
        self.assertNotIn(members[0].session_token, serialized)
        reset = await members[0].reset_lobby()
        self.assertFalse(reset["started"])
        self.assertFalse(any(p["ready"] for p in reset["members"]))

    async def test_wrong_passcode_and_fifth_member_rejected(self):
        members = await self.room()
        wrong = await self.connect()
        with self.assertRaisesRegex(RoomError, "Invalid room code or passcode"):
            await wrong.join(members[0].room_code, "wrong-pass")
        fifth = await self.connect()
        with self.assertRaisesRegex(RoomError, "Room is full"):
            await fifth.join(members[0].room_code, "play-test-pass")
        self.assertEqual(len((await members[0].status())["members"]), 4)

    async def test_room_isolation_and_no_self_echo_before_ready(self):
        members = await self.room(2)
        outsider = await self.connect()
        await outsider.create("another-pass", "Other room")
        payload = bytes(range(256)) * 8
        await members[0].send_frame(payload)
        self.assertEqual(await asyncio.wait_for(members[1].recv_frame(), 1), payload)
        for client in (members[0], outsider):
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(client.recv_frame(), .05)

    async def test_bridge_translates_exact_frames_and_concurrent_broadcast(self):
        members = await self.room()
        for member in members:
            bridge = await EmulatorBridge(member).start(port=0)
            self.bridges.append(bridge)
            emulator = await asyncio.open_connection("127.0.0.1", bridge.port)
            self.emulators.append(emulator)
            await asyncio.wait_for(bridge.connected.wait(), 1)

        payloads = [bytes([i + 1]) * size for i, size in enumerate((14, 64, 1500, 2048))]
        # Sending all four sockets before reading detects framing interleaving.
        for (_, writer), frame in zip(self.emulators, payloads):
            wire = struct.pack("!I", len(frame)) + frame
            writer.write(wire[:2])
            await writer.drain()
            writer.write(wire[2:])
            await writer.drain()
        for i, (reader, _) in enumerate(self.emulators):
            received = []
            for _ in range(3):
                size = struct.unpack("!I", await asyncio.wait_for(reader.readexactly(4), 2))[0]
                received.append(await asyncio.wait_for(reader.readexactly(size), 2))
            self.assertCountEqual(received, [p for n, p in enumerate(payloads) if n != i])
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(reader.readexactly(4), .05)
        for bridge in self.bridges:
            self.assertEqual(bridge.frames_to_relay, 1)
            self.assertEqual(bridge.frames_to_emulator, 3)

    async def test_disconnect_frees_slot_and_resets_ready(self):
        members = await self.room()
        await asyncio.gather(*(c.ready() for c in members))
        await members[0].start_match()
        await members[2].close()
        await self.wait_members(members[0], 3)
        state = await members[0].status()
        self.assertFalse(state["started"])
        self.assertFalse(any(member["ready"] for member in state["members"]))
        replacement = await self.connect()
        await replacement.join(members[0].room_code, "play-test-pass", "Replacement")
        self.assertEqual(replacement.slot, "B1")
        self.assertNotEqual(replacement.session_token, members[2].session_token)
        await members[0].close()
        await self.wait_members(members[1], 3)
        self.assertEqual((await members[1].status())["owner_id"], members[1].member_id)

    async def test_invalid_lengths_are_rejected_without_payload_allocation(self):
        for packet in (HEADER.pack(JSON, MAX_JSON + 1), HEADER.pack(FRAME, 2049), HEADER.pack(FRAME, 13)):
            reader, writer = await asyncio.open_connection("127.0.0.1", self.server.port)
            writer.write(packet)
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
            writer.close()
            await writer.wait_closed()

    async def test_bridge_rejects_oversize_and_second_emulator(self):
        client = (await self.room(1))[0]
        bridge = await EmulatorBridge(client).start(port=0)
        self.bridges.append(bridge)
        reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
        self.emulators.append((reader, writer))
        await asyncio.wait_for(bridge.connected.wait(), 1)
        second_reader, second_writer = await asyncio.open_connection("127.0.0.1", bridge.port)
        self.assertEqual(await asyncio.wait_for(second_reader.read(), 1), b"")
        second_writer.close()
        await second_writer.wait_closed()
        writer.write(struct.pack("!I", 2**31))
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        self.assertEqual(bridge.error, "Invalid emulator frame length")
        self.assertTrue(client.connected)

    async def test_auth_throttling_is_shared_across_connections(self):
        self.server.auth_attempts = 2
        first = await self.connect()
        await first.create("play-test-pass")
        wrong = await self.connect()
        with self.assertRaisesRegex(RoomError, "Invalid room code"):
            await wrong.join(first.room_code, "wrong-pass")
        throttled = await self.connect()
        with self.assertRaisesRegex(RoomError, "Too many authentication"):
            await throttled.join(first.room_code, "play-test-pass")

    async def test_tls_mandatory_for_public_listener(self):
        server = RelayServer()
        with self.assertRaisesRegex(ValueError, "TLS"):
            await server.start("0.0.0.0", 0)
        self.assertIsNone(server.server)

    async def test_connection_limit(self):
        self.server.max_per_ip = 1
        existing = await self.connect()
        await existing.create("play-test-pass")
        reader, writer = await asyncio.open_connection("127.0.0.1", self.server.port)
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        writer.close()
        await writer.wait_closed()
        self.assertTrue(existing.connected)

    async def test_malformed_unicode_is_rejected_and_server_stays_usable(self):
        malformed = await self.connect()
        with self.assertRaisesRegex(RoomError, "invalid Unicode"):
            await malformed.create("secretxxx\ud800")
        normal = (await self.room(1))[0]
        correct_token = normal.session_token
        normal.session_token = "\u2603"
        with self.assertRaisesRegex(RoomError, "Invalid session token"):
            await normal.status()
        normal.session_token = correct_token
        self.assertEqual(len((await normal.status())["members"]), 1)

    async def test_tls_roundtrip_trust_and_hostname_validation(self):
        executable = shutil.which("openssl")
        if executable is None:
            candidate = Path("C:/Program Files/Git/usr/bin/openssl.exe")
            if candidate.is_file():
                executable = str(candidate)
        if executable is None:
            self.skipTest("OpenSSL required only to generate this test's temporary TLS certificate")
        with tempfile.TemporaryDirectory(prefix="seedrooms-tls-test-") as temporary:
            cert, key = Path(temporary) / "cert.pem", Path(temporary) / "key.pem"
            await asyncio.to_thread(
                subprocess.run,
                [executable, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", str(key), "-out", str(cert), "-days", "2",
                 "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                check=True, capture_output=True, timeout=30,
            )
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.minimum_version = ssl.TLSVersion.TLSv1_2
            server_context.load_cert_chain(cert, key)
            trusted = ssl.create_default_context(cafile=str(cert))
            tls_server = await RelayServer().start(ssl_context=server_context)
            try:
                with self.assertRaises(ssl.SSLCertVerificationError):
                    await RoomClient.connect("127.0.0.1", tls_server.port,
                                             ssl_context=ssl.create_default_context())
                with self.assertRaises(ssl.SSLCertVerificationError):
                    await RoomClient.connect("127.0.0.1", tls_server.port,
                                             ssl_context=trusted, server_hostname="wrong.invalid")
                first = await RoomClient.connect("127.0.0.1", tls_server.port,
                                                 ssl_context=trusted, server_hostname="localhost")
                second = await RoomClient.connect("127.0.0.1", tls_server.port, ssl_context=trusted)
                self.clients.extend((first, second))
                await first.create("tls-test-pass")
                await second.join(first.room_code, "tls-test-pass")
                payload = b"tls-roundtrip!"
                await first.send_frame(payload)
                self.assertEqual(await asyncio.wait_for(second.recv_frame(), 2), payload)
                self.assertIn(first.writer.get_extra_info("ssl_object").version(), ("TLSv1.2", "TLSv1.3"))
                await first.close()
                await second.close()
            finally:
                await tls_server.close()


if __name__ == "__main__":
    unittest.main()

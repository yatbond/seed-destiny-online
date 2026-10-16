"""Real local sockets: health checks must not join, mutate, or weaken TLS."""
import asyncio
from pathlib import Path
import ssl
import tempfile
import time
import unittest

from seedrooms.client import RoomClient, RoomError
from seedrooms.connection_health import check_connection
from seedrooms.hosting import host_identity
from seedrooms.transport import ProtocolError, RelayServer


class LegacyRelay(RelayServer):
    async def _control(self, peer, message):
        if message.get("op") == "probe":
            raise ProtocolError("Create or join a room first")
        await super()._control(peer, message)


class ConnectionHealthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1
        self.server = await RelayServer().start()
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        await self.server.close()

    async def join_room(self, count=1):
        for i in range(count):
            client = await RoomClient.connect("127.0.0.1", self.server.port)
            self.clients.append(client)
            if i == 0:
                await client.create("health-test-pass")
            else:
                await client.join(self.clients[0].room_code, "health-test-pass")
        return self.clients

    async def test_probe_does_not_allocate_room_or_mutate_existing_room(self):
        client = (await self.join_room())[0]
        before = await client.status()
        result = await check_connection("127.0.0.1", self.server.port)
        self.assertTrue(result["relay_confirmed"])
        self.assertFalse(result["tls_verified"])
        self.assertFalse(result["room_joined"])
        self.assertFalse(result["internet_match_verified"])
        self.assertEqual(await client.status(), before)
        self.assertEqual(len(self.server.rooms), 1)
        self.assertEqual(len(self.server.auth_history["127.0.0.1"]), 1)

    async def test_legacy_probe_does_not_join_as_fallback(self):
        legacy = await LegacyRelay().start()
        try:
            result = await check_connection("127.0.0.1", legacy.port)
            self.assertTrue(result["reachable"])
            self.assertFalse(result["probe_supported"])
            self.assertFalse(result["relay_confirmed"])
            self.assertEqual(legacy.rooms, {})
            self.assertEqual(legacy.auth_history, {})
        finally:
            await legacy.close()

    async def test_tls_probe_verifies_certificate_and_hostname(self):
        with tempfile.TemporaryDirectory() as temporary:
            cert, key = host_identity(Path(temporary), "localhost")
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            server = await RelayServer().start(ssl_context=context)
            trusted = ssl.create_default_context(cafile=str(cert))
            try:
                result = await check_connection("127.0.0.1", server.port,
                                                ssl_context=trusted, server_hostname="localhost")
                self.assertTrue(result["tls_verified"])
                self.assertTrue(result["relay_confirmed"])
                self.assertEqual(server.rooms, {})
                with self.assertRaises(ssl.SSLCertVerificationError):
                    await check_connection("127.0.0.1", server.port, ssl_context=trusted,
                                           server_hostname="wrong.invalid")
                with self.assertRaises(ssl.SSLCertVerificationError):
                    await check_connection("127.0.0.1", server.port,
                                           ssl_context=ssl.create_default_context())
                insecure = ssl._create_unverified_context()
                with self.assertRaisesRegex(ValueError, "verify TLS"):
                    await check_connection("127.0.0.1", server.port, ssl_context=insecure)
            finally:
                await server.close()

    async def test_probe_timeout_cleans_up_connection(self):
        closed = asyncio.Event()
        async def silent(reader, writer):
            try:
                await reader.read()
                closed.set()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(silent, "127.0.0.1", 0)
        try:
            started = time.monotonic()
            with self.assertRaises(asyncio.TimeoutError):
                await check_connection("127.0.0.1", server.sockets[0].getsockname()[1], timeout=.1)
            self.assertLess(time.monotonic() - started, 1)
            await asyncio.wait_for(closed.wait(), 1)
        finally:
            server.close()
            await server.wait_closed()

    async def test_latency_requires_authentication_and_preserves_lobby(self):
        client = (await self.join_room())[0]
        before = await client.status()
        report = await client.measure_latency(samples=3)
        self.assertEqual(report["responses"], 3)
        self.assertEqual(report["timeouts"], 0)
        self.assertGreaterEqual(report["rtt_ms"], 0)
        self.assertEqual(await client.status(), before)
        client.session_token = "wrong-token"
        with self.assertRaisesRegex(RoomError, "Invalid session token"):
            await client.measure_latency(samples=1)
        self.assertEqual(client._pending, {})

    async def test_latency_timeout_does_not_poison_later_requests(self):
        client = (await self.join_room())[0]
        original = self.server._control
        async def delayed(peer, message):
            if message.get("op") == "status":
                await asyncio.sleep(.1)
            await original(peer, message)
        self.server._control = delayed
        report = await client.measure_latency(samples=1, timeout=.05)
        self.assertEqual(report["timeouts"], 1)
        self.assertIsNone(report["rtt_ms"])
        self.assertEqual(client._pending, {})
        self.assertEqual(len((await client.status())["members"]), 1)

    async def test_member_status_self_only_and_team_preferences_do_not_reassign(self):
        clients = await self.join_room(4)
        before = [(p.slot, p.cabinet_id) for p in clients]
        await clients[0].member_status(launch_state="loading", team_preference="A")
        with self.assertRaisesRegex(RoomError, "Wait for the game"):
            await clients[0].ready()
        await clients[0].member_status(launch_state="connected")
        await clients[0].ready()
        state = await clients[1].member_status(team_preference="A")
        self.assertFalse(any(p["ready"] for p in state["members"]))
        with self.assertRaisesRegex(RoomError, "Two players"):
            await clients[2].member_status(team_preference="A")
        await clients[2].member_status(team_preference="B")
        await clients[3].member_status(team_preference="B")
        self.assertEqual([(p.slot, p.cabinet_id) for p in clients], before)
        state = await clients[1]._request("member_status", launch_state="error",
                                          launch_error="Game stopped", member_id=clients[0].member_id)
        self.assertEqual(state["state"]["members"][0]["launch_state"], "connected")
        self.assertEqual(state["state"]["members"][1]["launch_state"], "error")
        for client in clients:
            await client.member_status(launch_state="connected")
            await client.ready()
        await clients[0].start_match()
        with self.assertRaisesRegex(RoomError, "Reset the lobby"):
            await clients[0].member_status(team_preference="Auto")

    async def test_member_status_rejects_forgery_and_invalid_values_atomically(self):
        client = (await self.join_room())[0]
        before = await client.status()
        for kwargs in ({"launch_state": "won"}, {"team_preference": "C"},
                       {"launch_state": "error", "launch_error": "x" * 161},
                       {"launch_state": "error", "launch_error": "line\nbreak"}):
            with self.assertRaises(RoomError):
                await client.member_status(**kwargs)
            self.assertEqual(await client.status(), before)
        token = client.session_token
        client.session_token = "forged"
        with self.assertRaisesRegex(RoomError, "Invalid session token"):
            await client.member_status(team_preference="B")
        client.session_token = token
        self.assertEqual(await client.status(), before)
        client.state.pop("capabilities")
        with self.assertRaisesRegex(RoomError, "host needs an update"):
            await client.member_status(launch_state="loading")


if __name__ == "__main__":
    unittest.main()

"""Room client and loopback bridge for the upstream UE PCB TCP transport.

Each emulator must join 127.0.0.1:<bridge port> using its assigned cabinet ID.
Room readiness/start are coordination signals, not emulator frame lockstep.
Reconnection is explicit: create a new RoomClient and join again; no stale slot
or authentication token is reused. A disconnect resets all lobby readiness.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import ssl
import statistics
import struct
import time

from .transport import (FRAME, JSON, MAX_FRAME, MIN_FRAME, ProtocolError,
                        encode_frame, encode_json, is_loopback, parse_json,
                        read_packet)


class RoomError(Exception):
    """A room request was rejected or the relay disconnected."""


class RoomClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader, self.writer = reader, writer
        self.state: dict = {}
        self.events: asyncio.Queue[dict] = asyncio.Queue(64)
        self.frames: asyncio.Queue[bytes | Exception] = asyncio.Queue(256)
        self.session_token = ""
        self.member_id = ""
        self.slot = ""
        self.cabinet_id = 0
        self.room_code = ""
        self.connected = True
        self._pending: dict[int, asyncio.Future] = {}
        self._sequence = 0
        self._write_lock = asyncio.Lock()
        self._reader_task = asyncio.create_task(self._read())

    @classmethod
    async def connect(cls, host: str, port: int, *,
                      ssl_context: ssl.SSLContext | None = None,
                      server_hostname: str | None = None) -> RoomClient:
        # An omitted SSL context is convenient only for a local test relay.
        if not is_loopback(host) and ssl_context is None:
            ssl_context = ssl.create_default_context()
        if not is_loopback(host) and ssl_context is not None:
            if not ssl_context.check_hostname or ssl_context.verify_mode != ssl.CERT_REQUIRED:
                raise ValueError("Internet relay TLS must verify certificate and hostname")
        options = {}
        if ssl_context:
            options["server_hostname"] = server_hostname or host
            options["ssl_handshake_timeout"] = 10.0
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ssl_context, limit=16384, **options), 15)
        return cls(reader, writer)

    async def _send(self, packet: bytes) -> None:
        if not self.connected:
            raise RoomError("Relay disconnected")
        async with self._write_lock:
            self.writer.write(packet)
            try:
                await asyncio.wait_for(self.writer.drain(), 10)
            except (OSError, asyncio.TimeoutError) as exc:
                raise RoomError("Relay write failed") from exc

    async def _request(self, operation: str, *, _timeout: float = 20, **data) -> dict:
        if len(self._pending) >= 32:
            raise RoomError("Too many pending room requests")
        self._sequence += 1
        request_id = self._sequence
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        request = {"id": request_id, "op": operation, **data}
        if self.session_token:
            request["session_token"] = self.session_token
        try:
            async with asyncio.timeout(_timeout):
                await self._send(encode_json(request))
                result = await future
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
        if not result.get("ok"):
            raise RoomError(result.get("error", "Room request rejected"))
        if "state" in result:
            self._update_state(result["state"], event=False)
        return result

    async def create(self, passcode: str, name: str = "Player") -> dict:
        return self._remember(await self._request("create", passcode=passcode, name=name))

    async def join(self, room_code: str, passcode: str, name: str = "Player") -> dict:
        return self._remember(await self._request("join", room_code=room_code.upper().strip(),
                                                 passcode=passcode, name=name))

    def _remember(self, result: dict) -> dict:
        for key in ("session_token", "member_id", "slot", "cabinet_id", "room_code"):
            setattr(self, key, result[key])
        # Keep the private token internal, even when callers print a join result.
        return {key: value for key, value in result.items() if key != "session_token"}

    async def status(self) -> dict:
        return (await self._request("status"))["state"]

    async def ready(self, ready: bool = True) -> dict:
        return (await self._request("ready", ready=ready))["state"]

    async def start_match(self) -> dict:
        return (await self._request("start"))["state"]

    async def reset_lobby(self) -> dict:
        return (await self._request("reset"))["state"]

    async def member_status(self, *, launch_state: str | None = None,
                            team_preference: str | None = None,
                            launch_error: str = "") -> dict:
        """Report this player's launch progress and intended team, not game state.

        Older hosts remain usable, but cannot share this optional information.
        Cabinet IDs and the original game's team selection are never changed.
        """
        if "member_status" not in self.state.get("capabilities", []):
            raise RoomError("This host needs an update to share game status and team preferences")
        data = {"launch_error": launch_error}
        if launch_state is not None:
            data["launch_state"] = launch_state
        if team_preference is not None:
            data["team_preference"] = team_preference
        return (await self._request("member_status", **data))["state"]

    async def measure_latency(self, *, samples: int = 3, timeout: float = 2) -> dict:
        """Measure relay request RTT, not game latency or network packet loss.

        Uses authenticated status requests supported by older hosts. A bounded
        sample avoids adding continuous load to the arcade Ethernet relay.
        """
        if type(samples) is not int or not 1 <= samples <= 5:
            raise ValueError("Use one through five latency samples")
        if not 0.05 <= timeout <= 5:
            raise ValueError("Latency timeout must be 0.05 through 5 seconds")
        if not self.session_token:
            raise RoomError("Join a room before measuring relay latency")
        elapsed = []
        timeouts = 0
        for _ in range(samples):
            started = time.perf_counter()
            try:
                await self._request("status", _timeout=timeout)
            except asyncio.TimeoutError:
                timeouts += 1
            else:
                elapsed.append((time.perf_counter() - started) * 1000)
        return {"rtt_ms": round(statistics.median(elapsed), 2) if elapsed else None,
                "jitter_ms": round(max(elapsed) - min(elapsed), 2) if len(elapsed) > 1 else None,
                "samples": samples, "responses": len(elapsed), "timeouts": timeouts,
                "connected": self.connected, "measurement": "relay_request_round_trip"}

    async def send_frame(self, frame: bytes) -> None:
        if not self.session_token:
            raise RoomError("Join a room before sending Ethernet frames")
        await self._send(encode_frame(frame))

    async def recv_frame(self) -> bytes:
        if not self.connected and self.frames.empty():
            raise RoomError("Relay disconnected")
        result = await self.frames.get()
        if isinstance(result, Exception):
            raise result
        return result

    def _event(self, event: dict) -> None:
        if self.events.full():
            self.events.get_nowait()
        self.events.put_nowait(event)

    def _update_state(self, state: dict, *, event: bool = True) -> None:
        if state.get("revision", -1) < self.state.get("revision", -1):
            return
        self.state = state
        if event:
            self._event({"event": "state", "state": state})

    async def _read(self) -> None:
        error = RoomError("Relay disconnected")
        try:
            while True:
                kind, payload = await read_packet(self.reader)
                if kind == FRAME:
                    if self.frames.full():
                        raise RoomError("Emulator cannot keep up with incoming frames")
                    self.frames.put_nowait(payload)
                    continue
                message = parse_json(payload)
                if message.get("event") == "state":
                    self._update_state(message["state"])
                elif message.get("id") in self._pending:
                    future = self._pending[message["id"]]
                    if not future.done():
                        future.set_result(message)
        except asyncio.CancelledError:
            pass
        except (OSError, asyncio.IncompleteReadError, ProtocolError, RoomError, KeyError) as exc:
            if isinstance(exc, RoomError):
                error = exc
        finally:
            self.connected = False
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            if self.frames.full():
                self.frames.get_nowait()
            self.frames.put_nowait(error)
            self._event({"event": "disconnected", "error": str(error)})
            self.writer.close()

    async def close(self) -> None:
        self.writer.close()
        self._reader_task.cancel()
        await asyncio.gather(self._reader_task, return_exceptions=True)
        try:
            await asyncio.wait_for(self.writer.wait_closed(), 2)
        except (OSError, asyncio.TimeoutError):
            pass


class EmulatorBridge:
    """One emulator per room member; only local connections are accepted.

    Frames received while no emulator is attached are discarded rather than
    replaying stale setup packets to a newly started cabinet. Existing emulator
    connections may reconnect locally; relay reconnection requires a new client.
    """
    def __init__(self, client: RoomClient):
        self.client = client
        self.server: asyncio.Server | None = None
        self.emulator_writer: asyncio.StreamWriter | None = None
        self.connected = asyncio.Event()
        self.error: str | None = None
        self.frames_to_relay = 0
        self.frames_to_emulator = 0
        self._tasks: set[asyncio.Task] = set()
        self._receive_task: asyncio.Task | None = None
        self._closing = False

    @property
    def port(self) -> int:
        if self.server is None or not self.server.sockets:
            raise RuntimeError("Bridge is not listening")
        return self.server.sockets[0].getsockname()[1]

    async def start(self, port: int = 7500) -> EmulatorBridge:
        if self.server is not None:
            raise RuntimeError("Bridge already started")
        if not self.client.session_token or not self.client.connected:
            raise RoomError("Join a room before starting the emulator bridge")
        self._closing = False
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", port, limit=8192)
        self._receive_task = asyncio.create_task(self._relay_to_emulator())
        return self

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._closing or self.emulator_writer is not None or not self.client.connected:
            writer.close()
            return
        self.emulator_writer = writer
        self.connected.set()
        task = asyncio.create_task(self._emulator_to_relay(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _emulator_to_relay(self, reader, writer) -> None:
        try:
            while True:
                length = struct.unpack("!I", await reader.readexactly(4))[0]
                if not MIN_FRAME <= length <= MAX_FRAME:
                    raise ProtocolError("Invalid emulator frame length")
                frame = await reader.readexactly(length)
                await self.client.send_frame(frame)
                self.frames_to_relay += 1
        except (OSError, asyncio.IncompleteReadError, ProtocolError, RoomError) as exc:
            if isinstance(exc, (ProtocolError, RoomError)):
                self.error = str(exc)
        finally:
            if self.emulator_writer is writer:
                self.emulator_writer = None
                self.connected.clear()
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except (OSError, asyncio.TimeoutError):
                pass

    async def _relay_to_emulator(self) -> None:
        try:
            while True:
                frame = await self.client.recv_frame()
                writer = self.emulator_writer
                if writer is not None:
                    try:
                        writer.write(struct.pack("!I", len(frame)) + frame)
                        await asyncio.wait_for(writer.drain(), 10)
                        self.frames_to_emulator += 1
                    except (OSError, asyncio.TimeoutError):
                        writer.close()
        except RoomError as exc:
            self.error = str(exc)
            if self.emulator_writer:
                self.emulator_writer.close()

    async def close(self) -> None:
        self._closing = True
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        if self.emulator_writer:
            self.emulator_writer.close()
        for task in list(self._tasks):
            task.cancel()
        if self._receive_task:
            self._receive_task.cancel()
        pending = list(self._tasks) + ([self._receive_task] if self._receive_task else [])
        await asyncio.gather(*pending, return_exceptions=True)
        self.connected.clear()


async def _main(args) -> None:
    context = ssl.create_default_context(cafile=args.ca) if args.tls or args.ca else None
    # Prompt before opening the socket so human input cannot expire authentication.
    passcode = await asyncio.to_thread(getpass.getpass, "Room passcode (8-128 characters): ")
    client = await RoomClient.connect(args.host, args.port, ssl_context=context)
    bridge = EmulatorBridge(client)
    try:
        await (client.join(args.room, passcode, args.name) if args.room else client.create(passcode, args.name))
        del passcode
        await bridge.start(args.bridge_port)
        print(f"Room {client.room_code}; slot {client.slot}; cabinet {client.cabinet_id}")
        print(f"Emulator PeerIP=127.0.0.1 PeerPort={bridge.port}")
        print("Lobby signals only; emulator synchronization must be qualified separately.")
        print("Commands: status, ready, unready, start, reset, quit")

        async def events():
            while True:
                event = await client.events.get()
                print(json.dumps(event))

        event_task = asyncio.create_task(events())
        try:
            while client.connected:
                command = (await asyncio.to_thread(input, "> ")).strip().lower()
                try:
                    if command == "quit":
                        break
                    elif command == "ready":
                        await client.ready()
                    elif command == "unready":
                        await client.ready(False)
                    elif command == "start":
                        await client.start_match()
                    elif command == "reset":
                        await client.reset_lobby()
                    elif command == "status":
                        print(json.dumps(await client.status()))
                except RoomError as exc:
                    print(str(exc))
        finally:
            event_task.cancel()
            await asyncio.gather(event_task, return_exceptions=True)
    finally:
        await bridge.close()
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7443)
    parser.add_argument("--room", help="Join this code; omit to create a room")
    parser.add_argument("--name", default="Player")
    parser.add_argument("--bridge-port", type=int, default=7500)
    parser.add_argument("--tls", action="store_true", help="Use TLS even for a loopback relay")
    parser.add_argument("--ca", help="Trusted relay certificate / CA PEM")
    try:
        asyncio.run(_main(parser.parse_args()))
    except (KeyboardInterrupt, EOFError):
        pass


if __name__ == "__main__":
    main()

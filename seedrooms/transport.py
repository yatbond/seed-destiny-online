"""Authenticated four-cabinet Ethernet relay. Python standard library only.

Internet protocol: a one-byte kind and four-byte unsigned big-endian length,
followed by JSON (kind 1) or an Ethernet frame (kind 2). This is deliberately
different from the emulator's four-byte length framing; client.py translates.
TLS is mandatory on non-loopback listeners. No discovery or port forwarding.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from dataclasses import dataclass, field
import hashlib
import hmac
import ipaddress
import json
import secrets
import ssl
import struct
import time

JSON, FRAME = 1, 2
MAX_JSON, MAX_FRAME, MIN_FRAME = 8192, 2048, 14
SLOTS = ("A1", "A2", "B1", "B2")
CAPABILITIES = ("member_status", "connection_probe")
LAUNCH_STATES = ("idle", "loading", "connected", "error")
TEAM_PREFERENCES = ("Auto", "A", "B")
QUEUE_LIMIT = 256
HEADER = struct.Struct("!BI")


class ProtocolError(Exception):
    """A peer sent invalid or unauthorized protocol data."""


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def encode_json(value: dict) -> bytes:
    payload = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()
    if len(payload) > MAX_JSON:
        raise ProtocolError("JSON message too large")
    return HEADER.pack(JSON, len(payload)) + payload


def encode_frame(frame: bytes) -> bytes:
    if not MIN_FRAME <= len(frame) <= MAX_FRAME:
        raise ProtocolError("Ethernet frame must contain 14 through 2048 bytes")
    return HEADER.pack(FRAME, len(frame)) + frame


async def read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    kind, length = HEADER.unpack(await reader.readexactly(HEADER.size))
    if kind == JSON:
        if not 2 <= length <= MAX_JSON:
            raise ProtocolError("Invalid JSON length")
    elif kind == FRAME:
        if not MIN_FRAME <= length <= MAX_FRAME:
            raise ProtocolError("Invalid Ethernet frame length")
    else:
        raise ProtocolError("Unknown packet kind")
    return kind, await reader.readexactly(length)


def parse_json(payload: bytes) -> dict:
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("Invalid JSON") from exc
    if not isinstance(value, dict):
        raise ProtocolError("JSON object required")
    return value


def password_hash(passcode: str, salt: bytes) -> bytes:
    return hashlib.scrypt(passcode.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)


@dataclass(eq=False)
class Peer:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    ip: str
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(QUEUE_LIMIT))
    member_id: str = field(default_factory=lambda: secrets.token_urlsafe(12))
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    room: Room | None = None
    slot: int = 0
    name: str = "Player"
    ready: bool = False
    launch_state: str = "idle"
    launch_error: str = ""
    team_preference: str = "Auto"
    writer_task: asyncio.Task | None = None
    control_times: deque = field(default_factory=deque)
    frame_window: float = 0
    frame_count: int = 0


@dataclass
class Room:
    code: str
    salt: bytes
    pass_hash: bytes
    owner_id: str
    members: dict[int, Peer] = field(default_factory=dict)
    revision: int = 0
    started: bool = False

    def state(self) -> dict:
        return {
            "room_code": self.code,
            "revision": self.revision,
            "started": self.started,
            "owner_id": self.owner_id,
            "capacity": 4,
            "capabilities": list(CAPABILITIES),
            "members": [
                {"member_id": p.member_id, "name": p.name, "slot": SLOTS[i],
                 "cabinet_id": i + 1, "ready": p.ready,
                 "launch_state": p.launch_state, "launch_error": p.launch_error,
                 "team_preference": p.team_preference}
                for i, p in sorted(self.members.items())
            ],
        }


class RelayServer:
    def __init__(self, *, max_connections: int = 256, max_per_ip: int = 32,
                 max_rooms: int = 64, auth_attempts: int = 12,
                 auth_window: float = 60.0):
        self.max_connections, self.max_per_ip = max_connections, max_per_ip
        self.max_rooms = max_rooms
        self.auth_attempts, self.auth_window = auth_attempts, auth_window
        self.rooms: dict[str, Room] = {}
        self.peers: set[Peer] = set()
        self.tasks: set[asyncio.Task] = set()
        self.auth_history: dict[str, deque] = {}
        self.hash_slots = asyncio.Semaphore(4)
        self.server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        if self.server is None or not self.server.sockets:
            raise RuntimeError("Relay is not listening")
        return self.server.sockets[0].getsockname()[1]

    async def start(self, host: str = "127.0.0.1", port: int = 0,
                    ssl_context: ssl.SSLContext | None = None) -> RelayServer:
        if not is_loopback(host) and ssl_context is None:
            raise ValueError("TLS certificate and key required for a non-loopback listener")
        if self.server is not None:
            raise RuntimeError("Relay already started")
        options = {"ssl_handshake_timeout": 10.0} if ssl_context else {}
        self.server = await asyncio.start_server(self._accept, host, port,
                                                 ssl=ssl_context, limit=16384, **options)
        return self

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for peer in list(self.peers):
            peer.writer.close()
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        self.rooms.clear()

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        ip = str(writer.get_extra_info("peername")[0])
        if len(self.peers) >= self.max_connections or sum(p.ip == ip for p in self.peers) >= self.max_per_ip:
            writer.close()
            return
        peer = Peer(reader, writer, ip)
        self.peers.add(peer)
        task = asyncio.create_task(self._serve(peer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _queue(self, peer: Peer, packet: bytes) -> None:
        try:
            peer.queue.put_nowait(packet)
        except asyncio.QueueFull:
            # Losing Ethernet frames silently would make game behavior misleading.
            peer.writer.close()

    def _reply(self, peer: Peer, request_id: int, **data) -> None:
        self._queue(peer, encode_json({"id": request_id, **data}))

    def _publish(self, room: Room) -> None:
        room.revision += 1
        packet = encode_json({"event": "state", "state": room.state()})
        for member in room.members.values():
            self._queue(member, packet)

    async def _write(self, peer: Peer) -> None:
        try:
            while True:
                packet = await peer.queue.get()
                try:
                    peer.writer.write(packet)
                    await asyncio.wait_for(peer.writer.drain(), 10.0)
                finally:
                    peer.queue.task_done()
        except (OSError, asyncio.TimeoutError, asyncio.CancelledError):
            peer.writer.close()

    def _allow_auth(self, ip: str) -> bool:
        now = time.monotonic()
        if ip not in self.auth_history:
            # Bound IP-keyed bookkeeping without permitting active buckets to reset.
            expired = [key for key, values in self.auth_history.items()
                       if not values or values[-1] <= now - self.auth_window]
            for key in expired:
                del self.auth_history[key]
            if len(self.auth_history) >= 4096:
                return False
            self.auth_history[ip] = deque()
        history = self.auth_history[ip]
        while history and history[0] <= now - self.auth_window:
            history.popleft()
        if len(history) >= self.auth_attempts:
            return False
        history.append(now)
        return True

    async def _authenticate(self, peer: Peer, message: dict) -> None:
        operation = message.get("op")
        if operation not in ("create", "join"):
            raise ProtocolError("Create or join a room first")
        if not self._allow_auth(peer.ip):
            raise ProtocolError("Too many authentication attempts; retry later")
        passcode = message.get("passcode")
        name = message.get("name", "Player")
        if not isinstance(passcode, str) or not 8 <= len(passcode) <= 128:
            raise ProtocolError("Passcode must contain 8 through 128 characters")
        try:
            passcode.encode("utf-8")
        except UnicodeError as exc:
            raise ProtocolError("Passcode contains invalid Unicode") from exc
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 32 or any(ord(c) < 32 for c in name):
            raise ProtocolError("Player name must contain 1 through 32 printable characters")
        if operation == "create":
            if len(self.rooms) >= self.max_rooms:
                raise ProtocolError("Room limit reached")
            salt = secrets.token_bytes(16)
            async with self.hash_slots:
                digest = await asyncio.to_thread(password_hash, passcode, salt)
            # Authentication yields; check limits again before mutating room state.
            if len(self.rooms) >= self.max_rooms:
                raise ProtocolError("Room limit reached")
            code = secrets.token_hex(4).upper()
            while code in self.rooms:
                code = secrets.token_hex(4).upper()
            room = Room(code, salt, digest, peer.member_id)
            self.rooms[code] = room
        else:
            code = message.get("room_code")
            if not isinstance(code, str) or len(code) != 8:
                raise ProtocolError("Invalid room code or passcode")
            room = self.rooms.get(code.upper())
            # Unknown rooms perform equivalent expensive work and share the error.
            salt = room.salt if room else secrets.token_bytes(16)
            async with self.hash_slots:
                digest = await asyncio.to_thread(password_hash, passcode, salt)
            if room is None or not hmac.compare_digest(room.pass_hash, digest) or self.rooms.get(room.code) is not room:
                raise ProtocolError("Invalid room code or passcode")
            if len(room.members) >= 4:
                raise ProtocolError("Room is full")
            if room.started:
                raise ProtocolError("Room has started; ask the host to reset the lobby")
        peer.room, peer.name = room, name.strip()
        peer.slot = next(i for i in range(4) if i not in room.members)
        room.members[peer.slot] = peer
        self._publish(room)
        self._reply(peer, message["id"], ok=True, session_token=peer.token,
                    member_id=peer.member_id, slot=SLOTS[peer.slot],
                    cabinet_id=peer.slot + 1, room_code=room.code, state=room.state())

    async def _control(self, peer: Peer, message: dict) -> None:
        request_id = message.get("id")
        if type(request_id) is not int or not 1 <= request_id <= 2**53:
            raise ProtocolError("Invalid request ID")
        now = time.monotonic()
        while peer.control_times and peer.control_times[0] < now - 1:
            peer.control_times.popleft()
        if len(peer.control_times) >= 60:
            raise ProtocolError("Control message rate limit reached")
        peer.control_times.append(now)
        if peer.room is None:
            if message.get("op") == "probe":
                # Server identity comes from verified TLS. No room/password or
                # membership is exposed; the connection closes after one reply.
                self._reply(peer, request_id, ok=True, protocol="seed-room-v1",
                            capabilities=list(CAPABILITIES))
                return
            await self._authenticate(peer, message)
            return
        token = message.get("session_token")
        if not isinstance(token, str) or not token.isascii() or not hmac.compare_digest(token, peer.token):
            raise ProtocolError("Invalid session token")
        room = peer.room
        operation = message.get("op")
        if operation == "status":
            pass
        elif operation == "member_status":
            launch = message.get("launch_state", peer.launch_state)
            team = message.get("team_preference", peer.team_preference)
            error = message.get("launch_error", "")
            if launch not in LAUNCH_STATES or team not in TEAM_PREFERENCES:
                raise ProtocolError("Invalid launch state or team preference")
            if not isinstance(error, str) or len(error) > 160 or any(ord(c) < 32 for c in error):
                raise ProtocolError("Launch error must be at most 160 printable characters")
            if team != peer.team_preference:
                if room.started:
                    raise ProtocolError("Reset the lobby before changing team preference")
                if team != "Auto" and sum(p is not peer and p.team_preference == team
                                           for p in room.members.values()) >= 2:
                    raise ProtocolError("Two players already prefer that team")
                # Preferences never reassign cabinet IDs or game-side teams.
                for member in room.members.values():
                    member.ready = False
            peer.team_preference = team
            peer.launch_state = launch
            peer.launch_error = error if launch == "error" else ""
            if launch != "connected":
                peer.ready = False
            self._publish(room)
        elif operation == "ready":
            if type(message.get("ready")) is not bool:
                raise ProtocolError("Ready must be true or false")
            if room.started:
                raise ProtocolError("Reset the lobby before changing readiness")
            if message["ready"] and peer.launch_state in ("loading", "error"):
                raise ProtocolError("Wait for the game connection before marking ready")
            peer.ready = message["ready"]
            self._publish(room)
        elif operation == "start":
            if peer.member_id != room.owner_id:
                raise ProtocolError("Only the room host can start")
            if len(room.members) != 4 or not all(p.ready for p in room.members.values()):
                raise ProtocolError("Four ready players are required")
            room.started = True
            self._publish(room)
        elif operation == "reset":
            if peer.member_id != room.owner_id:
                raise ProtocolError("Only the room host can reset")
            room.started = False
            for member in room.members.values():
                member.ready = False
            self._publish(room)
        else:
            raise ProtocolError("Unknown operation")
        self._reply(peer, request_id, ok=True, state=room.state())

    async def _serve(self, peer: Peer) -> None:
        peer.writer_task = asyncio.create_task(self._write(peer))
        try:
            # Authentication timeout includes expensive passcode verification.
            async with asyncio.timeout(15):
                kind, payload = await read_packet(peer.reader)
                if kind != JSON:
                    raise ProtocolError("Authentication required")
                message = parse_json(payload)
                del payload
                try:
                    await self._control(peer, message)
                except ProtocolError as exc:
                    self._reply(peer, message.get("id", 0), ok=False, error=str(exc))
                    await asyncio.wait_for(peer.queue.join(), 2)
                    return
                finally:
                    # Avoid retaining plaintext in the long-lived read loop.
                    message.pop("passcode", None)
            if peer.room is None:
                await asyncio.wait_for(peer.queue.join(), 2)
                return
            while True:
                kind, payload = await read_packet(peer.reader)
                if kind == FRAME:
                    now = time.monotonic()
                    if now - peer.frame_window >= 1:
                        peer.frame_window, peer.frame_count = now, 0
                    peer.frame_count += 1
                    if peer.frame_count > 5000:
                        raise ProtocolError("Frame rate limit reached")
                    packet = encode_frame(payload)
                    # Ethernet setup packets are allowed before lobby readiness.
                    for member in peer.room.members.values():
                        if member is not peer:
                            self._queue(member, packet)
                else:
                    message = parse_json(payload)
                    try:
                        await self._control(peer, message)
                    except ProtocolError as exc:
                        self._reply(peer, message.get("id", 0), ok=False, error=str(exc))
                        if str(exc) == "Control message rate limit reached":
                            await asyncio.wait_for(peer.queue.join(), 2)
                            return
        except (asyncio.IncompleteReadError, OSError, asyncio.TimeoutError, ProtocolError):
            pass
        finally:
            room = peer.room
            if room:
                room.members.pop(peer.slot, None)
                room.started = False
                for member in room.members.values():
                    member.ready = False
                if not room.members:
                    self.rooms.pop(room.code, None)
                else:
                    if peer.member_id == room.owner_id:
                        room.owner_id = room.members[min(room.members)].member_id
                    self._publish(room)
            self.peers.discard(peer)
            peer.writer_task.cancel()
            await asyncio.gather(peer.writer_task, return_exceptions=True)
            peer.writer.close()
            try:
                await asyncio.wait_for(peer.writer.wait_closed(), 2)
            except (OSError, asyncio.TimeoutError):
                pass


async def _main(args) -> None:
    context = None
    if args.cert or args.key:
        if not args.cert or not args.key:
            raise ValueError("Both --cert and --key are required")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.cert, args.key)
    server = await RelayServer().start(args.host, args.port, context)
    print(f"Room relay listening on {args.host}:{server.port}; TLS {'enabled' if context else 'off (loopback only)'}")
    try:
        await asyncio.Event().wait()
    finally:
        await server.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7443)
    parser.add_argument("--cert", help="TLS server certificate PEM")
    parser.add_argument("--key", help="TLS private key PEM")
    args = parser.parse_args()
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

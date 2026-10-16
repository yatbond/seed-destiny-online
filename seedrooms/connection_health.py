"""Bounded relay checks; no room creation, slot consumption, or game claims."""
from __future__ import annotations

import asyncio
import ssl
import time

from .client import RoomClient, RoomError


async def check_connection(host: str, port: int, *,
                           ssl_context: ssl.SSLContext | None = None,
                           server_hostname: str | None = None,
                           timeout: float = 12) -> dict:
    """Verify server TLS identity and, where supported, the relay protocol.

    An existing invitation supplies the trusted public certificate. This checks
    server authentication only; a room passcode is still required to join.
    Legacy relays can reject the new probe without making a verified TLS
    connection failure. The result explicitly distinguishes those outcomes.
    Exceptions report DNS/TCP/TLS/deadline failure and must not be called success.
    """
    if not 0.1 <= timeout <= 20:
        raise ValueError("Connection check timeout must be 0.1 through 20 seconds")
    if ssl_context is not None and (not ssl_context.check_hostname or
                                    ssl_context.verify_mode != ssl.CERT_REQUIRED):
        raise ValueError("Connection checks must verify TLS certificate and hostname")
    client = None
    try:
        async with asyncio.timeout(timeout):
            started = time.perf_counter()
            client = await RoomClient.connect(host, port, ssl_context=ssl_context,
                                               server_hostname=server_hostname)
            connect_ms = round((time.perf_counter() - started) * 1000, 2)
            tls = client.writer.get_extra_info("ssl_object")
            result = {"reachable": True, "tls_verified": tls is not None,
                      "tls_version": tls.version() if tls else None,
                      "connect_ms": connect_ms, "relay_confirmed": False,
                      "capabilities": [], "room_joined": False,
                      "internet_match_verified": False}
            try:
                reply = await client._request("probe", _timeout=min(3, timeout))
            except RoomError as exc:
                # Known older relays reject before authentication. Never retry
                # by creating a room, bypassing TLS, or consuming a player slot.
                if str(exc) != "Create or join a room first":
                    raise
                result["probe_supported"] = False
            else:
                result["probe_supported"] = True
                result["relay_confirmed"] = reply.get("protocol") == "seed-room-v1"
                result["capabilities"] = reply.get("capabilities", [])
            return result
    finally:
        if client is not None:
            await client.close()

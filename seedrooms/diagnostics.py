"""Offline package qualification using real TLS sockets and four room clients."""
import asyncio
import json
from pathlib import Path
import ssl
import tempfile

from .client import RoomClient, EmulatorBridge
from .hosting import host_identity, make_invitation, import_invitation
from .transport import RelayServer


async def qualify():
    clients, bridges, sockets = [], [], []
    server = None
    with tempfile.TemporaryDirectory(prefix='seed-room-check-') as temp:
        root = Path(temp)
        cert, key = host_identity(root / 'host', 'localhost')
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        try:
            server = await RelayServer().start('127.0.0.1', 0, ssl_context=context)
            trusted = ssl.create_default_context(cafile=str(cert))
            first = await RoomClient.connect('127.0.0.1', server.port, ssl_context=trusted)
            clients.append(first)
            await first.create('diagnostic-only-passcode', 'Host')
            invitation = make_invitation('localhost', server.port, first.room_code, cert.read_text())
            received = import_invitation(invitation, root / 'guest')
            for number in range(3):
                client = await RoomClient.connect(received['host'], received['port'],
                    ssl_context=ssl.create_default_context(cafile=received['ca']))
                clients.append(client)
                await client.join(received['room'], 'diagnostic-only-passcode', f'Guest {number + 1}')
            for client in clients:
                bridges.append(await EmulatorBridge(client).start(port=0))
            for bridge in bridges:
                sockets.append(await asyncio.open_connection('127.0.0.1', bridge.port))
                await asyncio.wait_for(bridge.connected.wait(), 5)
            for origin, (_, writer) in enumerate(sockets):
                payload = bytes([origin]) * 128
                writer.write(len(payload).to_bytes(4, 'big') + payload)
                await writer.drain()
                for target, (reader, _) in enumerate(sockets):
                    if target == origin:
                        continue
                    size = int.from_bytes(await asyncio.wait_for(reader.readexactly(4), 5), 'big')
                    actual = await asyncio.wait_for(reader.readexactly(size), 5)
                    assert actual == payload, 'Ethernet frame changed during relay'
            return {'passed': True, 'tls': True, 'invitation_import': True,
                    'clients': 4, 'emulator_bridges': 4, 'frame_deliveries_verified': 12,
                    'game_boot_verified': False, 'internet_match_verified': False}
        finally:
            for _, writer in sockets:
                writer.close()
                await writer.wait_closed()
            for bridge in bridges:
                await bridge.close()
            for client in clients:
                await client.close()
            if server:
                await server.close()


def run(output):
    try:
        result = asyncio.run(qualify())
    except Exception as exc:
        result = {'passed': False, 'error': str(exc)}
    Path(output).write_text(json.dumps(result, indent=2), encoding='utf-8')
    return 0 if result['passed'] else 1

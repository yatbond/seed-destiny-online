import json
from pathlib import Path
import tempfile
import unittest

from seedrooms.hosting import host_identity, import_invitation, make_invitation, parse_invitation


class HostingTests(unittest.TestCase):
    def test_invitation_contains_public_certificate_only_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cert, key = host_identity(root / 'host', 'rooms.example.test')
            text = make_invitation('rooms.example.test', 7443, 'ABC123', cert.read_text())
            self.assertNotIn('PRIVATE KEY', text)
            self.assertNotIn('passcode', text)
            result = import_invitation(text, root / 'guest')
            self.assertEqual(Path(result['ca']).read_text(), cert.read_text())
            self.assertEqual(result['host'], 'rooms.example.test')
            old_key = key.read_bytes()
            self.assertEqual(host_identity(root / 'host', 'rooms.example.test'), (cert, key))
            self.assertEqual(key.read_bytes(), old_key)

    def test_reject_malformed_invitations_before_writing(self):
        base = json.loads(make_invitation('localhost', 7443, 'ABC123', tls=False))
        for field, value in [('host', 'https://example.test'), ('host', None), ('host', '../escape'),
                             ('port', True), ('port', 65536), ('room', '../path'),
                             ('certificate', '-----BEGIN PRIVATE KEY-----'), ('tls', 'false')]:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / 'certificates'
                with self.assertRaises(ValueError):
                    import_invitation(json.dumps({**base, field: value}), target)
                self.assertFalse(target.exists())

    def test_reject_unencrypted_remote_and_oversized_invitation(self):
        with self.assertRaises(ValueError):
            make_invitation('192.168.1.2', 7443, 'ABC123', tls=False)
        with self.assertRaises(ValueError):
            parse_invitation(' ' * 16385)


if __name__ == '__main__':
    unittest.main()

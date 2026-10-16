import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from unittest.mock import patch

from seedrooms.play_profiles import cabinet_initialized, configure_cabinet, generate_play_profile
from seedrooms.profiles import ProfileError


class PlayProfilesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.exe = self.root / "Play.exe"
        self.exe.write_bytes(b"MZ\0SEED_UEPCB_BRIDGE_PORT\0prepare-cabinet\0SEED_GL_SWAP_INTERVAL\0SEED_GL_DETACHED")
        self.iso = self.root / "game.iso"
        disc = bytearray(17 * 2048)
        disc[16 * 2048:16 * 2048 + 7] = b"\x01CD001\x01"
        disc[16 * 2048 + 40:16 * 2048 + 44] = b"GSD1"
        self.iso.write_bytes(disc)
        self.card = self.root / "dongle.bin"
        with self.card.open("wb") as stream:
            stream.write(b"Sony PS2 Memory Card Format ")
            stream.truncate(8388608)

    def profile(self, seat=1, **extra):
        return generate_play_profile(self.root / "players", seat=seat,
            emulator_path=self.exe, iso_path=self.iso, dongle_path=self.card,
            bridge_port=24000 + seat, **extra)

    def test_seat_settings_and_raw_dongle_are_isolated(self):
        before = hashlib.sha256(self.card.read_bytes()).hexdigest()
        one, two = self.profile(1), self.profile(2)
        self.assertNotEqual(one.data_dir, two.data_dir)
        self.assertEqual(two.environment["SEED_UEPCB_SEAT"], "2")
        self.assertEqual(two.environment["SEED_UEPCB_BRIDGE_PORT"], "24002")
        archive = two.data_dir / "Play Data Files/arcaderoms/seed_destiny_nm00034.zip"
        with zipfile.ZipFile(archive) as package:
            self.assertEqual(hashlib.sha256(package.read("gsd1vera.bin")).hexdigest(), before)
        self.assertEqual(hashlib.sha256(self.card.read_bytes()).hexdigest(), before)
        self.assertFalse(two.backup_ram.exists())
        prefs = {x.attrib["Name"]: x.attrib["Value"] for x in ET.parse(two.data_dir / "Play Data Files/config.xml").getroot()}
        self.assertEqual(prefs["ui.pausewhenfocuslost"], "false")
        self.assertTrue((two.data_dir / "Play Data Files/config.xml").read_bytes().startswith(b"<Config>"))

    def test_cabinet_update_preserves_all_other_generated_bytes(self):
        profile = self.profile(4)
        data = bytearray((i * 7) % 256 for i in range(32768))
        for offset, value in [(0, b"CAPCOM CO.,LTD\0"), (0x40, b"GUNDAM SEED DESTINY\0"), (0x80, b"Ver.0.99A\0")]:
            data[offset:offset + len(value)] = value
        profile.backup_ram.write_bytes(data)
        self.assertTrue(cabinet_initialized(profile.backup_ram))
        configure_cabinet(profile)
        after = profile.backup_ram.read_bytes()
        self.assertEqual(after[:0x1618], data[:0x1618])
        self.assertEqual(after[0x1620:], data[0x1620:])
        self.assertEqual(struct.unpack_from("<II", after, 0x1618), (3, 3))
        self.profile(4)
        self.assertEqual(profile.backup_ram.read_bytes(), after)

    def test_unrecognized_cabinet_and_invalid_graphics_are_rejected(self):
        profile = self.profile()
        profile.backup_ram.write_bytes(b"x" * 32768)
        with self.assertRaises(ProfileError):
            configure_cabinet(profile)
        self.assertEqual(profile.backup_ram.read_bytes(), b"x" * 32768)
        with self.assertRaises(ProfileError):
            self.profile(renderer="Vulkan", scale=3)

    def test_enhanced_sync_and_old_runtime_guard(self):
        with patch.dict("os.environ", {"SEED_GL_SWAP_INTERVAL": "0", "SEED_GL_DETACHED": "1"}):
            enhanced = self.profile(renderer="OpenGL", scale=3)
            self.assertEqual(enhanced.environment["SEED_GL_SWAP_INTERVAL"], "1")
            self.assertEqual(enhanced.environment["SEED_GL_DETACHED"], "1")
            self.assertNotIn("SEED_GL_SWAP_INTERVAL", self.profile().environment)
            self.assertNotIn("SEED_GL_DETACHED", self.profile().environment)
        self.exe.write_bytes(b"MZ\0SEED_UEPCB_BRIDGE_PORT\0prepare-cabinet\0SEED_GL_SWAP_INTERVAL")
        with self.assertRaisesRegex(ProfileError, "updated bundled emulator"):
            self.profile(renderer="OpenGL", scale=3)


if __name__ == "__main__":
    unittest.main()

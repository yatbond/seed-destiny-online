import configparser
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest

from seedrooms.profiles import ProfileError, generate_profile, inspect_bios, inspect_profile


class ProfilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inputs = {}
        payloads = {"emulator": b"MZ\0UePcb\0UePCB: netplay", "boot": bytearray(64),
                    "bios": bytearray(512 * 1024), "iso": bytearray(17 * 2048),
                    "dongle": bytearray(8_650_752)}
        payloads["boot"][:6] = b"\x7fELF\x01\x01"
        struct.pack_into("<H", payloads["boot"], 18, 8)
        payloads["boot"][52:59] = b"SE2LOAD"
        payloads["bios"][:16] = struct.pack("<10sHI", b"RESET", 0, 64)
        payloads["bios"][16:32] = struct.pack("<10sHI", b"ROMVER", 0, 14)
        payloads["bios"][64:78] = b"0100TZ20000306"
        payloads["iso"][16*2048:16*2048+7] = b"\x01CD001\x01"
        payloads["iso"][16*2048+40:16*2048+52] = b"GSD1_DVD0A  "
        magic = b"Sony PS2 Memory Card Format "
        payloads["dongle"][:len(magic)] = magic
        for key, content in payloads.items():
            path = self.root / key
            path.write_bytes(content)
            self.inputs[f"{key}_path"] = path
        self.before = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in self.inputs.items()}

    def generate(self, seat=1, **options):
        return generate_profile(self.root / "profiles", seat=seat, bridge_port=7501, **self.inputs, **options)

    def test_four_seats_are_isolated_and_keep_originals(self):
        profiles = [self.generate(seat) for seat in range(1, 5)]
        macs = set()
        for profile in profiles:
            settings = configparser.ConfigParser()
            settings.read(profile.settings_path)
            macs.add(settings["USB1"]["UePcb_MacHex"])
            self.assertEqual(profile.command[1:3], ["-datapath", str(profile.data_dir.parent)])
            self.assertEqual(profile.data_dir.name, "PCSX2x6")
            self.assertTrue(inspect_profile(profile)["configured"])
            self.assertFalse(inspect_profile(profile)["runtime_verified"])
            self.assertTrue((profile.data_dir / "memcards" / "NM00034.ps2").is_file())
        self.assertEqual(len(macs), 4)
        for key, path in self.inputs.items():
            self.assertEqual(self.before[key], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_regenerate_preserves_sram_controls_and_mac(self):
        profile = self.generate()
        sram = profile.game_path.parent / "sram.bin"
        sram.write_bytes(b"user network id")
        old = configparser.ConfigParser()
        old.optionxform = str
        old.read(profile.settings_path)
        old["JVS"]["CustomControl"] = "Keyboard/A"
        old["JVS"]["Gundam_Shoot_P1"] = "Keyboard/Z"
        del old["JVS"]["Coin1"]
        with profile.settings_path.open("w", encoding="utf-8") as out:
            old.write(out)
        self.generate(scale=6)
        new = configparser.ConfigParser()
        new.read(profile.settings_path)
        self.assertEqual(new["USB1"]["UePcb_MacHex"], old["USB1"]["UePcb_MacHex"])
        self.assertEqual(new["JVS"]["CustomControl"], "Keyboard/A")
        self.assertEqual(new["JVS"]["Gundam_Shoot_P1"], "Keyboard/Z")
        self.assertNotIn("Coin1", new["JVS"])
        self.assertEqual(new["EmuCore/GS"]["upscale_multiplier"], "6")
        self.assertEqual(sram.read_bytes(), b"user network id")

    def test_new_profile_has_arcade_keyboard_controls(self):
        profile = self.generate()
        settings = configparser.ConfigParser()
        settings.read(profile.settings_path)
        self.assertEqual(settings["JVS"]["Coin1"], "Keyboard/5")
        self.assertEqual(settings["JVS"]["ToggleTestMode"], "Keyboard/F2")
        self.assertEqual(settings["JVS"]["Gundam_Shoot_P1"], "Keyboard/J")
        self.assertEqual(settings["JVS"]["Gundam_Target_P1"], "Keyboard/I")
        self.assertNotIn("NextSaveStateSlot", settings["Hotkeys"])

    def test_rejects_stock_emulator_before_writing(self):
        self.inputs["emulator_path"].write_bytes(b"MZ ordinary emulator")
        with self.assertRaisesRegex(ProfileError, "netplay device"):
            self.generate()
        self.assertFalse((self.root / "profiles").exists())

    def test_rejects_retail_bios(self):
        path = self.inputs["bios_path"]
        data = bytearray(path.read_bytes())
        data[68:70] = b"AC"
        path.write_bytes(data)
        with self.assertRaisesRegex(ProfileError, "retail PS2"):
            inspect_bios(path)

    def test_rejects_wrong_game_bootloader(self):
        path = self.inputs["boot_path"]
        path.write_bytes(path.read_bytes().replace(b"SE2LOAD", b"SEDLOAD"))
        with self.assertRaisesRegex(ProfileError, "SE2LOAD"):
            self.generate()

    def test_rejects_inputs_inside_mutable_profile(self):
        path = self.root / "profiles" / "seat-1" / "source.exe"
        path.parent.mkdir(parents=True)
        path.write_bytes(self.inputs["emulator_path"].read_bytes())
        self.inputs["emulator_path"] = path
        with self.assertRaisesRegex(ProfileError, "outside"):
            self.generate()


if __name__ == "__main__":
    unittest.main()

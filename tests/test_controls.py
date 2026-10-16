import ctypes
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from seedrooms.controls import (apply_preset, sync_controls, copy_controls, XInputReader,
                               _State, _Gamepad, XINPUT_PROVIDER, KEYBOARD_PROVIDER)
from seedrooms.profiles import ProfileError


def values(path):
    return {item.get("Name"): item.get("Value") for item in ET.parse(path).getroot().findall("Preference")}


class ControlsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_xbox_profile_matches_native_schema_and_keeps_other_pads(self):
        data = self.root / "Play Data Files"
        profile = data / "inputprofiles" / "my-pad.xml"
        profile.parent.mkdir(parents=True)
        (data / "config.xml").write_text('<Config><Preference Name="input.pad1.profile" Type="string" Value="my-pad" /></Config>')
        old = b'<Config><Preference Name="input.pad2.cross.bindingtype" Type="integer" Value="3" /><Preference Name="custom.keep" Type="string" Value="yes" /></Config>'
        profile.write_bytes(old)
        result = apply_preset(self.root, "xbox", 2)
        self.assertEqual(result.profile_path, profile)
        self.assertEqual(result.backup_paths[0].read_bytes(), old)
        prefs = values(profile)
        self.assertEqual(prefs["input.pad1.square.bindingtarget1.providerId"], str(int.from_bytes(b"xinp", "big")))
        self.assertEqual(prefs["input.pad1.square.bindingtarget1.keyId"], "17")
        self.assertEqual(prefs["input.pad1.cross.bindingtarget1.keyId"], "18")
        self.assertEqual(prefs["input.pad1.circle.bindingtarget1.keyId"], "19")
        self.assertEqual(prefs["input.pad1.triangle.bindingtarget1.keyId"], "16")
        self.assertEqual(prefs["input.pad1.square.bindingtarget1.deviceId"], "2:0:0:0:0:0")
        self.assertEqual(prefs["input.pad1.select.bindingtarget1.keyId"], "11")
        self.assertEqual(prefs["input.pad1.analog_left_y.bindingtarget1.keyType"], "1")
        # Native XInput thresholds triggers and reports them as buttons.
        self.assertEqual(prefs["input.pad1.l2.bindingtarget1.keyId"], "4")
        self.assertEqual(prefs["input.pad1.r2.bindingtarget1.keyId"], "5")
        self.assertEqual(prefs["input.pad1.l2.bindingtarget1.keyType"], "0")
        self.assertEqual(prefs["input.pad1.r2.bindingtarget1.keyType"], "0")
        self.assertEqual(prefs["input.pad2.cross.bindingtype"], "3")
        self.assertEqual(prefs["custom.keep"], "yes")
        self.assertFalse(profile.read_bytes().startswith(b"<?xml"))

    def test_keyboard_reset_uses_qt_keys_and_paired_analog_keys(self):
        result = apply_preset(self.root, "keyboard")
        prefs = values(result.profile_path)
        self.assertEqual(prefs["input.pad1.start.bindingtarget1.keyId"], str(0x1000004))
        self.assertEqual(prefs["input.pad1.select.bindingtarget1.keyId"], str(0x1000003))
        self.assertEqual(prefs["input.pad1.cross.bindingtarget1.providerId"], "1366575993")
        self.assertEqual(prefs["input.pad1.cross.bindingtarget1.keyId"], "90")
        self.assertEqual(prefs["input.pad1.analog_left_x.bindingtype"], "2")
        self.assertEqual(prefs["input.pad1.analog_left_x.bindingtarget2.keyId"], str(ord("H")))

    def test_sync_preserves_later_f1_edits_until_explicit_reapply(self):
        source, seat = self.root / "shared", self.root / "seat"
        apply_preset(source, "xbox")
        first = sync_controls(source, seat)
        self.assertTrue(first.changed)
        edited = first.profile_path.read_bytes().replace(b'Value="17"', b'Value="18"')
        first.profile_path.write_bytes(edited)
        self.assertFalse(sync_controls(source, seat).changed)
        self.assertEqual(first.profile_path.read_bytes(), edited)
        apply_preset(source, "xbox")
        reapplied = sync_controls(source, seat)
        self.assertTrue(reapplied.changed)
        self.assertEqual(reapplied.backup_paths[0].read_bytes(), edited)
        self.assertEqual(values(first.profile_path)["input.pad1.square.bindingtarget1.keyId"], "17")

    def test_sync_does_not_touch_other_config_or_profile(self):
        source, seat = self.root / "shared", self.root / "seat"
        apply_preset(source, "keyboard")
        data = seat / "Play Data Files"
        data.mkdir(parents=True)
        cfg = b'<Config><Preference Name="input.pad1.profile" Type="string" Value="custom" /><Preference Name="video.gshandler" Type="integer" Value="1" /></Config>'
        (data / "config.xml").write_bytes(cfg)
        out = copy_controls(source, seat)
        self.assertEqual(out.profile_path.name, "custom.xml")
        self.assertEqual((data / "config.xml").read_bytes(), cfg)
        self.assertFalse(sync_controls(source, source).changed)

    def test_missing_source_is_noop_and_bad_xml_is_never_replaced(self):
        self.assertFalse(sync_controls(self.root / "missing", self.root / "seat").changed)
        self.assertFalse((self.root / "seat").exists())
        profile = self.root / "Play Data Files/inputprofiles/default.xml"
        profile.parent.mkdir(parents=True)
        profile.write_bytes(b"broken xml")
        with self.assertRaises(ProfileError):
            apply_preset(self.root)
        self.assertEqual(profile.read_bytes(), b"broken xml")

    def test_validation_blocks_path_escape_and_invalid_controller(self):
        for index in (-1, 4, True):
            with self.assertRaises(ProfileError):
                apply_preset(self.root, controller_index=index)
        with self.assertRaises(ProfileError):
            apply_preset(self.root, "unknown")
        data = self.root / "Play Data Files"
        data.mkdir()
        (data / "config.xml").write_text('<Config><Preference Name="input.pad1.profile" Value="../../outside" /></Config>')
        with self.assertRaises(ProfileError):
            apply_preset(self.root)

    def test_xinput_layout_and_decode_disconnected_devices(self):
        self.assertEqual(ctypes.sizeof(_Gamepad), 12)
        self.assertEqual(ctypes.sizeof(_State), 16)
        reader = XInputReader.__new__(XInputReader)
        reader.error = ""
        def get_state(index, output):
            if index != 2:
                return 1167
            state = ctypes.cast(output, ctypes.POINTER(_State)).contents
            state.gamepad.buttons = 0x1000 | 0x20 | 0x1
            state.gamepad.left_trigger = 177
            state.gamepad.right_trigger = 255
            state.gamepad.left_x = -32768
            state.gamepad.left_y = 32767
            return 0
        reader._get_state = get_state
        states = reader.poll()
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0].index, 2)
        self.assertEqual(states[0].buttons, ("D-pad Up", "View / Back", "A"))
        self.assertEqual(states[0].left_stick, (-32768, 32767))
        self.assertEqual(states[0].right_trigger, 255)
        reader._get_state = None
        self.assertFalse(reader.available)
        self.assertEqual(reader.poll(), [])


if __name__ == "__main__":
    unittest.main()

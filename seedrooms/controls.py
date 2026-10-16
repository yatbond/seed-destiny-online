"""Controller presets and read-only XInput testing for the bundled Play! runtime.

Schema: Play! InputBindingManager.cpp, InputConfig.cpp, InputProviderXInput.cpp,
InputProviderQtKey.cpp and ControllerConfig/controllerconfigdialog.cpp. Call
writers only while the destination emulator is stopped (it saves on exit).
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import tempfile
import xml.etree.ElementTree as ET

from .profiles import ProfileError

XINPUT_PROVIDER = int.from_bytes(b"xinp", "big")
KEYBOARD_PROVIDER = int.from_bytes(b"QtKy", "big")
PROFILE_PREFERENCE = "input.pad1.profile"
BUTTONS = ("dpad_up", "dpad_down", "dpad_left", "dpad_right", "select", "start",
           "square", "triangle", "circle", "cross", "l1", "l2", "l3", "r1", "r2", "r3")
# Arcade actions differ from a PS2 game's default face-button layout.
# X shoots, Y melees, A boosts, B changes target (verified in-game).
XBOX_KEYS = dict(zip(BUTTONS, (6, 7, 8, 9, 11, 10, 17, 16, 19, 18, 14, 4, 12, 15, 5, 13)))
KEYBOARD_KEYS = dict(zip(BUTTONS, (0x1000013, 0x1000015, 0x1000012, 0x1000014,
    0x1000003, 0x1000004, 65, 83, 88, 90, 49, 50, 51, 56, 57, 48)))
CONTROL_ROWS = (
    ("Move / menu", "DPAD", "Arrow keys", "D-pad"),
    ("Credit", "SELECT", "Backspace", "View / Back"),
    ("Start", "START", "Enter", "Menu / Start"),
    ("Shoot", "CROSS", "Z", "X"),
    ("Melee / confirm", "CIRCLE", "X", "Y"),
    ("Jump / boost", "TRIANGLE", "S", "A"),
    ("Change target", "SQUARE", "A", "B"),
)
CONTROLS_GUIDE = """Connect your Windows controller before starting the game.
Choose an Xbox preset for XInput controllers, or Keyboard to restore the default keys.
The live tester shows XInput devices only; DirectInput controllers can still be mapped in the game.

For custom mapping, press F1 in the game, select Pad1, and double-click a row.
Hold the desired button until accepted (about three seconds), then Apply and OK.
Config All guides you through the rows. Profile(s) lets you save separate layouts.
Each online player uses Pad1 on their own PC. Use the D-pad for arcade movement.

Keyboard: Backspace inserts credit; Enter starts; arrow keys move.
Z shoots (CROSS); X performs melee or confirms a selection (CIRCLE).
S jumps/boosts (TRIANGLE); A changes target (SQUARE).
Xbox preset: View inserts credit; Menu starts; the D-pad moves.
X shoots; Y performs melee or confirms; A jumps/boosts; B changes target.
These actions were checked in an original-game battle. Suit-specific weapons and combos vary.
Alt+Enter toggles fullscreen; Escape returns to a window; Ctrl+R pauses/resumes.

Apply presets with the game closed. Existing mappings are backed up before replacement.
A newly selected launcher preset is copied to each player's settings on the next launch.
Later F1 custom edits are kept until you select another launcher preset.
"""


@dataclass(frozen=True)
class ControlsResult:
    profile_path: Path
    changed: bool
    backup_paths: tuple[Path, ...] = ()


def _read_config(path: Path) -> ET.ElementTree:
    try:
        tree = ET.parse(path) if path.exists() else ET.ElementTree(ET.Element("Config"))
    except (ET.ParseError, OSError) as exc:
        raise ProfileError(f"Cannot read controller settings: {path.name}. Keep the original file before resetting it.") from exc
    if tree.getroot().tag != "Config":
        raise ProfileError("Controller settings have an unsupported format")
    return tree


def _profile_path(root: str | Path) -> Path:
    data = Path(root) / "Play Data Files"
    config = _read_config(data / "config.xml")
    selected = "default"
    for item in config.getroot().findall("Preference"):
        if item.get("Name") == PROFILE_PREFERENCE:
            selected = item.get("Value", "default")
    if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]*", selected) or selected in (".", ".."):
        raise ProfileError("The selected input profile name is invalid")
    # Match CInputConfig::LoadProfile's replace_extension rather than appending.
    return (data / "inputprofiles" / selected).with_suffix(".xml")


def _encode(tree: ET.ElementTree) -> bytes:
    ET.indent(tree, space="  ")
    return ET.tostring(tree.getroot(), encoding="utf-8") + b"\n"


def _write(path: Path, content: bytes) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(path.name + ".before-" + stamp + ".bak")
        with backup.open("xb") as output:
            output.write(path.read_bytes())
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    return backup


def _put(root: ET.Element, name: str, value: object, kind: str = "integer") -> None:
    matches = [item for item in root.findall("Preference") if item.get("Name") == name]
    item = matches[0] if matches else ET.SubElement(root, "Preference")
    for duplicate in matches[1:]:
        root.remove(duplicate)
    item.attrib.update(Name=name, Type=kind, Value=str(value))


def apply_preset(profile_root: str | Path, preset: str = "xbox", controller_index: int = 0) -> ControlsResult:
    """Replace Pad1 in its selected profile; retain other pads and unrelated data."""
    if preset not in ("xbox", "keyboard"):
        raise ProfileError("Choose the Xbox or Keyboard preset")
    if type(controller_index) is not int or controller_index not in range(4):
        raise ProfileError("Choose an Xbox controller from 1 to 4")
    path = _profile_path(profile_root)
    tree = _read_config(path)
    root = tree.getroot()
    for item in list(root.findall("Preference")):
        if item.get("Name", "").startswith("input.pad1."):
            root.remove(item)
    provider = XINPUT_PROVIDER if preset == "xbox" else KEYBOARD_PROVIDER
    device = f"{controller_index:x}:0:0:0:0:0" if preset == "xbox" else "0:0:0:0:0:0"

    def target(base: str, key: int, key_type: int = 0) -> None:
        for suffix, value in (("providerId", provider), ("keyId", key), ("keyType", key_type)):
            _put(root, base + "." + suffix, value)
        _put(root, base + ".deviceId", device, "string")

    for button, key in (XBOX_KEYS if preset == "xbox" else KEYBOARD_KEYS).items():
        base = "input.pad1." + button
        _put(root, base + ".bindingtype", 1)
        target(base + ".bindingtarget1", key)
    for index, (axis, keys) in enumerate(zip(("left_x", "left_y", "right_x", "right_y"), ("FH", "TG", "JL", "IK"))):
        base = "input.pad1.analog_" + axis
        _put(root, base + ".bindingtype", 1 if preset == "xbox" else 2)
        if preset == "xbox":
            target(base + ".bindingtarget1", index, 1)
        else:
            target(base + ".bindingtarget1", ord(keys[0]))
            target(base + ".bindingtarget2", ord(keys[1]))
    _put(root, "input.pad1.analog.sensitivity", "1.000000", "float")
    backup = _write(path, _encode(tree))
    # Explicitly reapplying the same preset should also supersede F1 edits at seats.
    revision = Path(profile_root) / "Play Data Files" / "controls-revision.txt"
    revision.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    return ControlsResult(path, True, (backup,) if backup else ())


def sync_controls(source_profile_root: str | Path, target_profile_root: str | Path, *, force: bool = False) -> ControlsResult:
    """Propagate a changed central preset once; preserve subsequent F1 edits.

    Only Pad1 from the source's selected profile is merged into the target's
    selected profile. A source hash/revision marker avoids repeated overwrites.
    No source means no change. Call only with the destination game stopped.
    """
    source = _profile_path(source_profile_root)
    target = _profile_path(target_profile_root)
    if source.resolve() == target.resolve() or not source.exists():
        return ControlsResult(target, False)
    tree = _read_config(source)
    revision_file = Path(source_profile_root) / "Play Data Files" / "controls-revision.txt"
    revision = revision_file.read_bytes() if revision_file.exists() else b""
    digest = hashlib.sha256(source.read_bytes() + b"\0" + revision).hexdigest()
    marker = Path(target_profile_root) / "Play Data Files" / "controls-applied.sha256"
    if not force and target.exists() and marker.exists() and marker.read_text(encoding="utf-8").strip() == digest:
        return ControlsResult(target, False)
    incoming = [item for item in tree.getroot().findall("Preference") if item.get("Name", "").startswith("input.pad1.")]
    if not incoming:
        raise ProfileError("The shared controller profile has no Pad1 mappings")
    destination = _read_config(target)
    for item in list(destination.getroot().findall("Preference")):
        if item.get("Name", "").startswith("input.pad1."):
            destination.getroot().remove(item)
    for item in incoming:
        destination.getroot().append(ET.fromstring(ET.tostring(item)))
    backup = _write(target, _encode(destination))
    marker.write_text(digest + "\n", encoding="utf-8")
    return ControlsResult(target, True, (backup,) if backup else ())


def copy_controls(source_profile_root: str | Path, target_profile_root: str | Path) -> ControlsResult:
    """Explicit copy, with a backup; normal launches should use sync_controls."""
    return sync_controls(source_profile_root, target_profile_root, force=True)


class _Gamepad(ctypes.Structure):
    _fields_ = [("buttons", ctypes.c_uint16), ("left_trigger", ctypes.c_uint8),
                ("right_trigger", ctypes.c_uint8), ("left_x", ctypes.c_int16),
                ("left_y", ctypes.c_int16), ("right_x", ctypes.c_int16), ("right_y", ctypes.c_int16)]


class _State(ctypes.Structure):
    _fields_ = [("packet", ctypes.c_uint32), ("gamepad", _Gamepad)]


@dataclass(frozen=True)
class ControllerState:
    index: int
    buttons: tuple[str, ...]
    left_trigger: int
    right_trigger: int
    left_stick: tuple[int, int]
    right_stick: tuple[int, int]

    @property
    def name(self) -> str:
        return f"Xbox controller {self.index + 1}"


class XInputReader:
    """Read-only polling. No vibration, keyboard injection, or device changes."""
    def __init__(self) -> None:
        self._get_state = None
        self.error = ""
        if os.name != "nt":
            self.error = "The controller tester needs Windows"
            return
        for dll in ("xinput1_4.dll", "xinput9_1_0.dll", "xinput1_3.dll"):
            try:
                library = ctypes.WinDLL(dll, winmode=0x00000800)  # System32 only.
                function = library.XInputGetState
                function.argtypes = [ctypes.c_uint32, ctypes.POINTER(_State)]
                function.restype = ctypes.c_uint32
                self._get_state = function
                break
            except (OSError, AttributeError):
                continue
        if self._get_state is None:
            self.error = "Windows XInput is unavailable; use F1 in the game to map your controller"

    @property
    def available(self) -> bool:
        return self._get_state is not None

    def poll(self) -> list[ControllerState]:
        if self._get_state is None:
            return []
        masks = ((1, "D-pad Up"), (2, "D-pad Down"), (4, "D-pad Left"), (8, "D-pad Right"),
                 (16, "Menu / Start"), (32, "View / Back"), (64, "Left stick"), (128, "Right stick"),
                 (256, "LB"), (512, "RB"), (4096, "A"), (8192, "B"), (16384, "X"), (32768, "Y"))
        states = []
        for index in range(4):
            state = _State()
            if self._get_state(index, ctypes.byref(state)) != 0:
                continue
            pad = state.gamepad
            states.append(ControllerState(index, tuple(name for mask, name in masks if pad.buttons & mask),
                pad.left_trigger, pad.right_trigger, (pad.left_x, pad.left_y), (pad.right_x, pad.right_y)))
        return states

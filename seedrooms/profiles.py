"""Isolated SEED Destiny profiles for PCSX2x6's experimental UE PCB build.

Source contract: boostedmans-hub/pcsx2x6 at
743953a3905b4fe482cd62e72bd724015ea28880, QtHost.cpp, VMManager.cpp,
USB/usb-eth/usb-uepcb.cpp, Pcsx2Config.cpp and ps2/BiosTools.cpp.
Profiles are configuration, not evidence of a successful boot or netplay match.
"""
from __future__ import annotations

import configparser
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
from dataclasses import dataclass

NETPLAY_COMMIT = "743953a3905b4fe482cd62e72bd724015ea28880"
PROVERB_URL = "https://github.com/PS2Homebrew-arcade/proverb/releases/download/nightly/proverb.zip"
PROVERB_ENTRY = "bin/NM00034/boot.elf"
# Verified from the publisher's NM00034 entry on 2026-10-15. The release
# bootloader is ps2-packer compressed, so its target string is not visible.
PROVERB_NM00034_SHA256 = "9f5f3c8c0284333f38090b8ff033fa52aebe45310696fc4c679e2bcb913fe226"


class ProfileError(ValueError):
    """A required local runtime or game artifact is missing or unsuitable."""


@dataclass(frozen=True)
class Profile:
    seat: int
    data_dir: Path
    settings_path: Path
    game_path: Path
    command: list[str]
    manual_steps: tuple[str, ...]


def _file(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file() or path.stat().st_size == 0:
        raise ProfileError(f"{label} is missing or empty: {path}")
    if any(c in str(path) for c in "\r\n\x00"):
        raise ProfileError(f"{label} path contains invalid characters")
    return path


def inspect_bios(path: str | Path) -> dict[str, str]:
    """Read ROMDIR rather than guessing from a filename or arbitrary strings."""
    bios = _file(path, "Arcade BIOS")
    size = bios.stat().st_size
    if not 512 * 1024 <= size <= 8 * 1024 * 1024:
        raise ProfileError("Arcade BIOS must be a complete 512 KiB to 8 MiB ROM image")
    data = bios.read_bytes()
    for start in range(0, min(len(data) - 16, 512 * 1024), 16):
        if data[start:start + 10].rstrip(b"\0") != b"RESET":
            continue
        offset = 0
        for pos in range(start, min(len(data) - 15, start + 16 * 4096), 16):
            name, _extsize, length = struct.unpack_from("<10sHI", data, pos)
            name = name.rstrip(b"\0")
            if not name:
                break
            if offset + length > len(data):
                break
            if name == b"ROMVER" and length >= 14:
                version = data[offset:offset + 14]
                if version[4:6] != b"TZ":
                    raise ProfileError("This is not a COH-H arcade BIOS; a retail PS2 BIOS cannot boot this game")
                return {"type": "COH-H", "romver": version.decode("ascii", errors="replace")}
            offset += (length + 15) & ~15
    raise ProfileError("Could not identify a COH-H arcade ROMVER in this BIOS")


def validate_inputs(*, emulator_path: str | Path, iso_path: str | Path,
                    dongle_path: str | Path, bios_path: str | Path,
                    boot_path: str | Path) -> dict[str, Path]:
    paths = {key: _file(value, label) for key, value, label in (
        ("emulator", emulator_path, "PCSX2x6 netplay executable"),
        ("iso", iso_path, "GSD1 DVD image"),
        ("dongle", dongle_path, "GSD1 dongle dump"),
        ("bios", bios_path, "COH-H arcade BIOS"),
        ("boot", boot_path, "NM00034 Proverb boot.elf"),
    )}
    with paths["emulator"].open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ProfileError("PCSX2x6 executable is not a Windows executable")
    # The stock release cannot link cabinets; these strings are emitted by the
    # experimental device and survive release compilation. This is a capability
    # check, not a cryptographic assertion of the binary's build provenance.
    binary = paths["emulator"].read_bytes()
    if b"UePcb" not in binary or b"UePCB: netplay" not in binary:
        raise ProfileError("This emulator does not contain the experimental UE PCB netplay device")
    with paths["iso"].open("rb") as stream:
        stream.seek(16 * 2048)
        pvd = stream.read(2048)
    if len(pvd) != 2048 or pvd[0:7] != b"\x01CD001\x01":
        raise ProfileError("Game image must be an ISO9660 DVD ISO; compressed archives and CHDs need separate preparation")
    if not pvd[40:72].strip(b" \0").startswith(b"GSD1"):
        raise ProfileError("DVD volume is not identified as GSD1")
    with paths["dongle"].open("rb") as stream:
        magic = stream.read(28)
    if not magic.startswith(b"Sony PS2 Memory Card Format "):
        raise ProfileError("Dongle is not a PS2 memory-card image")
    if paths["dongle"].stat().st_size not in (8_388_608, 8_650_752):
        raise ProfileError("Expected an 8 MiB PS2 dongle image, with or without ECC")
    with paths["boot"].open("rb") as stream:
        header = stream.read(52)
    if len(header) < 52 or header[:6] != b"\x7fELF\x01\x01" or struct.unpack_from("<H", header, 18)[0] != 8:
        raise ProfileError("Bootloader must be a little-endian ELF32 MIPS image")
    boot_data = paths["boot"].read_bytes()
    if b"SE2LOAD" not in boot_data and hashlib.sha256(boot_data).hexdigest() != PROVERB_NM00034_SHA256:
        raise ProfileError("Bootloader does not target SE2LOAD; use bin/NM00034/boot.elf from the official Proverb package")
    inspect_bios(paths["bios"])
    return paths


def _parser() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    return parser


def _write_ini(path: Path, sections: dict[str, dict[str, str]], *, preserve: bool = False) -> None:
    parser = _parser()
    if preserve and path.exists():
        try:
            parser.read(path, encoding="utf-8-sig")
        except configparser.Error as exc:
            raise ProfileError(f"Existing settings could not be read: {path}") from exc
    for name, values in sections.items():
        if not parser.has_section(name):
            parser.add_section(name)
        for key, value in values.items():
            parser.set(name, key, value)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        parser.write(stream, space_around_delimiters=False)
    temporary.replace(path)


def generate_profile(output_root: str | Path, *, seat: int,
                     emulator_path: str | Path, iso_path: str | Path,
                     dongle_path: str | Path, bios_path: str | Path,
                     boot_path: str | Path, bridge_port: int,
                     scale: int = 3) -> Profile:
    """Prepare a seat; all emulators JOIN the authenticated local bridge.

    Does not launch processes, change a firewall, download files, edit source
    artifacts or set the game's network ID. Repeated calls preserve controls and
    SRAM, while refreshing managed graphics/network settings and dongle copies.
    """
    if type(seat) is not int or seat not in range(1, 5):
        raise ProfileError("Seat must be 1, 2, 3 or 4")
    if type(bridge_port) is not int or not 1 <= bridge_port <= 65535:
        raise ProfileError("Bridge port must be between 1 and 65535")
    if type(scale) is not int or scale not in (1, 3, 6):
        raise ProfileError("Graphics scale must be 1, 3 or 6")
    paths = validate_inputs(emulator_path=emulator_path, iso_path=iso_path,
                            dongle_path=dongle_path, bios_path=bios_path,
                            boot_path=boot_path)
    root = Path(output_root).expanduser().resolve()
    # The pinned Windows build appends PCSX2x6 to CustomDataPath.
    base_dir = root / f"seat-{seat}"
    data_dir = base_dir / "PCSX2x6"
    # Never use an input artifact as mutable profile output, even when the user
    # supplied a previous generated file as their original.
    for path in paths.values():
        if path.is_relative_to(base_dir):
            raise ProfileError("Source artifacts must be outside the generated seat directory")
    game_dir = data_dir / "game"
    for directory in (game_dir, data_dir / "inis", data_dir / "memcards", data_dir / "logs"):
        directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(paths["boot"], game_dir / "boot.elf")
    shutil.copyfile(paths["dongle"], game_dir / "NM00034.ps2")
    shutil.copyfile(paths["dongle"], data_dir / "memcards" / "NM00034.ps2")
    # The same output root yields stable identities between launches; seats are
    # distinct. Deployments on different machines should have different roots.
    ident = hashlib.sha256((str(root).casefold() + f"|{seat}").encode()).digest()[:3]
    mac = "00902e" + ident.hex()
    settings_path = data_dir / "inis" / "PCSX2.ini"
    if not settings_path.exists():
        # Exact keys from the pinned ACJV.cpp Gundam layout and InputManager.
        # Seed once only: later launches must preserve remaps and unbound keys.
        _write_ini(settings_path, {
            "JVS": {
                "P1_Up": "Keyboard/Up", "P1_Down": "Keyboard/Down",
                "P1_Left": "Keyboard/Left", "P1_Right": "Keyboard/Right",
                "P1_Start": "Keyboard/Return", "P1_Service": "Keyboard/Backspace",
                "P1_Button1": "Keyboard/J", "Coin1": "Keyboard/5",
                "ToggleTestMode": "Keyboard/F2",
                "Gundam_Shoot_P1": "Keyboard/J", "Gundam_Melee_P1": "Keyboard/K",
                "Gundam_Jump_P1": "Keyboard/L", "Gundam_Target_P1": "Keyboard/I",
            },
            "Hotkeys": {"ToggleFullscreen": "Keyboard/Alt & Keyboard/Return",
                        "OpenPauseMenu": "Keyboard/Escape"},
        })
    _write_ini(settings_path, {
        "UI": {"SettingsVersion": "1", "SetupWizardIncomplete": "false"},
        "Folders": {"Bios": str(paths["bios"].parent), "MemoryCards": str(data_dir / "memcards"),
                    "Logs": str(data_dir / "logs"), "Savestates": str(data_dir / "sstates")},
        "Filenames": {"BIOS": paths["bios"].name},
        "MemoryCards": {"Slot1_Enable": "true", "Slot1_Filename": "NM00034.ps2", "Slot2_Enable": "false"},
        "USB1": {"Type": "UePcb", "UePcb_PeerIP": "127.0.0.1", "UePcb_Port": str(bridge_port),
                 "UePcb_BindIP": "127.0.0.1", "UePcb_MacHex": mac},
        "USB2": {"Type": "None"},
        "EmuCore/GS": {"Renderer": "14", "upscale_multiplier": str(scale), "MaxAnisotropy": "16",
                       "AspectRatio": "4:3", "LoadTextureReplacements": "false"},
    }, preserve=True)
    game_path = game_dir / "NM00034.acgame"
    # ACATA combines the base directory and media path. Relative paths across a
    # volume are used where possible; Windows absolute paths handle other drives.
    try:
        media = os.path.relpath(paths["iso"], game_dir)
    except ValueError:
        media = str(paths["iso"])
    _write_ini(game_path, {
        "game": {"name": "SEED Destiny Rengou vs. Z.A.F.T. II", "gameid": "NM00034", "platform": "256"},
        "data": {"elf": "boot.elf", "dongle": "NM00034.ps2", "mediasrc": media,
                 "media": "DVD", "usb": "UePcb", "sram": "sram.bin"},
    })
    manual = (f"In the game's Test/Service menu, set NetworkID to {seat}; save settings for this seat.",
              "Keyboard: arrows move; J shoots/enters; K melee; L jump; I target; Enter start; 5 coin; F2 test mode; Backspace service. Remap JVS controls in the emulator as needed.",
              "Do not load savestates during a linked match. Confirm all four cabinets reach NETWORK SETUP.")
    metadata = {"seat": seat, "netplay_commit": NETPLAY_COMMIT, "mac": mac,
                "manual_network_id_required": True, "runtime_verified": False,
                "original_iso": str(paths["iso"]), "original_dongle": str(paths["dongle"])}
    (data_dir / "profile.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    command = [str(paths["emulator"]), "-datapath", str(base_dir), "-batch", "--", str(game_path)]
    return Profile(seat, data_dir, settings_path, game_path, command, manual)


def inspect_profile(profile: Profile) -> dict[str, object]:
    """Verify saved managed settings; this deliberately does not imply gameplay."""
    settings = _parser()
    game = _parser()
    try:
        if not settings.read(profile.settings_path, encoding="utf-8-sig"):
            raise ProfileError("Profile settings are missing")
        if not game.read(profile.game_path, encoding="utf-8-sig"):
            raise ProfileError("Game configuration is missing")
        if settings.get("USB1", "Type") != "UePcb" or settings.get("USB1", "UePcb_PeerIP") != "127.0.0.1":
            raise ProfileError("Profile must use the local UE PCB bridge")
        if game.get("data", "usb") != "UePcb" or game.get("game", "gameid") != "NM00034":
            raise ProfileError("Game configuration does not retain the NM00034 UE PCB device")
    except (configparser.Error, KeyError) as exc:
        raise ProfileError("Saved profile is incomplete or invalid") from exc
    return {"configured": True, "runtime_verified": False, "seat": profile.seat,
            "manual_network_id_required": True, "bridge_port": settings.getint("USB1", "UePcb_Port")}

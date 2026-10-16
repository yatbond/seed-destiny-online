"""Per-player Play! profiles using each player's original disc and dongle.

No game, BIOS, or saved machine state is shipped. First-run cabinet settings
are initialized by the original game, then the two verified service-menu
fields are set to the room's cabinet number and four linked cabinets.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import xml.etree.ElementTree as ET
import zipfile

from .profiles import ProfileError, _file

GAME_ID = "seed_destiny_nm00034"


@dataclass(frozen=True)
class PlayProfile:
    seat: int
    data_dir: Path
    command: list[str]
    environment: dict[str, str]
    backup_ram: Path

    @property
    def preparation_command(self) -> list[str]:
        return self.command + ["--prepare-cabinet"]


def cabinet_initialized(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size != 32768:
        return False
    data = path.read_bytes()
    return (data.startswith(b"CAPCOM CO.,LTD\0")
            and data.startswith(b"GUNDAM SEED DESTINY\0", 0x40)
            and data.startswith(b"Ver.0.99A\0", 0x80))


def configure_cabinet(profile: PlayProfile) -> None:
    if not cabinet_initialized(profile.backup_ram):
        raise ProfileError("The game has not finished preparing its cabinet settings. Please retry.")
    data = bytearray(profile.backup_ram.read_bytes())
    # Compared with all four original Exit & Save service-menu results.
    struct.pack_into("<I", data, 0x1618, profile.seat - 1)
    struct.pack_into("<I", data, 0x161c, 3)
    temporary = profile.backup_ram.with_suffix(".tmp")
    temporary.write_bytes(data)
    temporary.replace(profile.backup_ram)


def generate_play_profile(output_root: str | Path, *, seat: int,
                          emulator_path: str | Path, iso_path: str | Path,
                          dongle_path: str | Path, bridge_port: int,
                          renderer: str = "Vulkan", scale: int = 1) -> PlayProfile:
    if type(seat) is not int or seat not in range(1, 5):
        raise ProfileError("Player number must be 1–4")
    if type(bridge_port) is not int or not 1 <= bridge_port <= 65535:
        raise ProfileError("Invalid local room connection")
    if renderer not in ("Vulkan", "OpenGL") or scale not in (1, 2, 3, 4, 6):
        raise ProfileError("Unsupported graphics setting")
    if renderer == "Vulkan" and scale != 1:
        raise ProfileError("This Vulkan build supports native internal resolution only")
    exe = _file(emulator_path, "Bundled Play! emulator")
    iso = _file(iso_path, "Game disc")
    dongle = _file(dongle_path, "Original dongle")
    binary = exe.read_bytes()
    if binary[:2] != b"MZ" or b"SEED_UEPCB_BRIDGE_PORT" not in binary or b"prepare-cabinet" not in binary:
        raise ProfileError("Select the Play! emulator supplied with this version of SEED Destiny Online")
    if renderer == "OpenGL" and b"SEED_GL_SWAP_INTERVAL" not in binary:
        raise ProfileError("Enhanced graphics needs the updated bundled emulator with display synchronization")
    with iso.open("rb") as stream:
        stream.seek(16 * 2048)
        pvd = stream.read(2048)
    if pvd[:7] != b"\x01CD001\x01" or not pvd[40:72].strip(b" \0").startswith(b"GSD1"):
        raise ProfileError("Select the original GSD1 DVD ISO")
    card = dongle.read_bytes()
    if len(card) not in (8388608, 8650752) or not card.startswith(b"Sony PS2 Memory Card Format "):
        raise ProfileError("Select the original 8 MiB PS2 dongle dump, with or without ECC")
    root = Path(output_root).expanduser().resolve() / f"seat-{seat}" / "Play"
    if any(p.is_relative_to(root) for p in (exe, iso, dongle)):
        raise ProfileError("Keep original game files and the emulator outside the player settings folder")
    data = root / "Play Data Files"
    roms = data / "arcaderoms"
    saves = data / "arcadesaves"
    for directory in (root / "arcadedefs", roms, saves):
        directory.mkdir(parents=True, exist_ok=True)
    (root / "portable.txt").touch()
    game_config = exe.parent / "GameConfig.xml"
    if game_config.is_file():
        shutil.copy2(game_config, root / "GameConfig.xml")
    digest = hashlib.sha256(card).hexdigest()
    archive = roms / (GAME_ID + ".zip")
    marker = roms / (GAME_ID + ".sha256")
    if not archive.is_file() or not marker.is_file() or marker.read_text().strip() != digest:
        temporary = archive.with_suffix(".tmp")
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as output:
            output.writestr("gsd1vera.bin", card)
        temporary.replace(archive)
        marker.write_text(digest + "\n")
    definition = {"id": GAME_ID, "name": "SEED Destiny — Rengou vs. Z.A.F.T. II",
                  "driver": "sys246", "dongle": {"name": "gsd1vera.bin"},
                  "cdvd": {"name": str(iso)}, "boot": "ac0:SE2LOAD", "patches": []}
    (root / "arcadedefs" / (GAME_ID + ".arcadedef")).write_text(json.dumps(definition, indent=2), encoding="utf-8")
    config_path = data / "config.xml"
    try:
        config = ET.parse(config_path) if config_path.is_file() else ET.ElementTree(ET.Element("Config"))
    except ET.ParseError as exc:
        raise ProfileError("Existing player settings are unreadable; preserve them before resetting this player") from exc
    preferences = {item.attrib.get("Name"): item for item in config.getroot().findall("Preference")}
    managed = {
        "video.gshandler": ("integer", 1 if renderer == "Vulkan" else 0),
        "renderer.opengl.resfactor": ("integer", scale),
        "renderer.presentationmode": ("integer", 1),
        "renderer.widescreen": ("boolean", "false"),
        "renderer.ramreads.enabled": ("boolean", "true"),
        "ps2.limitframerate": ("boolean", "true"),
        "ui.pausewhenfocuslost": ("boolean", "false"),
        "ps2.arcade.ioserver.enabled": ("boolean", "false"),
        "iop.fileio.stdlogging": ("boolean", "false"),
        "ps2.arcaderoms.directory": ("path", str(roms)),
        "ps2.cdrom0.path.v2": ("path", str(iso)),
    }
    for name, (kind, value) in managed.items():
        item = preferences.get(name)
        if item is None:
            item = ET.SubElement(config.getroot(), "Preference")
        item.attrib.update(Name=name, Type=kind, Value=str(value))
    temporary = config_path.with_suffix(".tmp")
    # Play!'s small XML reader only accepts double-quoted attribute values.
    # ElementTree's declaration uses single quotes, so match Play!'s own writer.
    config.write(temporary, encoding="utf-8", xml_declaration=False)
    temporary.replace(config_path)
    env = os.environ.copy()
    env.update(SEED_UEPCB_ENABLE="1", SEED_UEPCB_SEAT=str(seat), SEED_UEPCB_BRIDGE_PORT=str(bridge_port))
    env.pop("SEED_UEPCB_NETIF_FIX", None)
    env.pop("SEED_UEPCB_FORCE_DRIVER", None)
    # Match completed OpenGL frames to display refresh. The bundled runtime
    # implements this before creating its double-buffered window surface.
    if renderer == "OpenGL":
        env["SEED_GL_SWAP_INTERVAL"] = "1"
    else:
        env.pop("SEED_GL_SWAP_INTERVAL", None)
    return PlayProfile(seat, root, [str(exe), "--arcade", GAME_ID], env, saves / (GAME_ID + ".backupram"))

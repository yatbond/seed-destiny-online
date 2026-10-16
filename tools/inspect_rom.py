"""Read-only PS2 memory-card filesystem inspection, with bounded local extraction.

Format references: Play! Source/saves/McDumpReader.{h,cpp} and ps2mcfs.
This is ordinary format inspection, not REA decompiler output.
"""
from pathlib import Path
import argparse
import hashlib
import json
import re
import struct
import subprocess


class Card:
    def __init__(self, path):
        self.raw = Path(path).read_bytes()
        if not self.raw.startswith(b"Sony PS2 Memory Card Format "):
            raise ValueError("Not a PS2 memory-card image")
        self.page, self.ppc = struct.unpack_from("<HH", self.raw, 40)
        self.clusters, self.alloc, self.end, self.root = struct.unpack_from("<IIII", self.raw, 48)
        if self.page != 512 or self.ppc != 2 or self.clusters > 131072:
            raise ValueError("Unsupported card geometry")
        pages = self.clusters * self.ppc
        if len(self.raw) % pages:
            raise ValueError("Truncated memory-card image")
        self.stride = len(self.raw) // pages
        if self.stride not in (512, 528):
            raise ValueError("Unsupported spare-page geometry")
        self.ifc = struct.unpack_from("<32I", self.raw, 80)

    def cluster(self, number):
        if not 0 <= number < self.clusters:
            raise ValueError("Cluster outside card")
        off = number * self.ppc * self.stride
        return self.raw[off:off + 512] + self.raw[off + self.stride:off + self.stride + 512]

    def chain(self, number, size):
        if size < 0 or size > len(self.raw):
            raise ValueError("Invalid file size")
        chunks, seen = [], set()
        while size > 0:
            if number in seen or number >= self.end:
                raise ValueError("Invalid or cyclic FAT chain")
            seen.add(number)
            chunk = self.cluster(self.alloc + number)
            chunks.append(chunk[:size])
            size -= min(size, 1024)
            if size:
                indirect = self.cluster(self.ifc[number // 65536])
                fat = self.cluster(struct.unpack_from("<I", indirect, ((number // 256) % 256) * 4)[0])
                nxt = struct.unpack_from("<I", fat, (number % 256) * 4)[0]
                if not nxt & 0x80000000 or nxt == 0xffffffff:
                    raise ValueError("FAT chain ended before file size")
                number = nxt & 0x7fffffff
        return b"".join(chunks)

    def directory(self, number=None, count=None):
        number = self.root if number is None else number
        if count is None:
            count = struct.unpack_from("<I", self.chain(number, 512), 4)[0]
        if not 2 <= count <= 4096:
            raise ValueError("Invalid directory size")
        data = self.chain(number, count * 512)
        entries = []
        for off in range(0, len(data), 512):
            mode, _, length = struct.unpack_from("<HHI", data, off)
            cluster = struct.unpack_from("<I", data, off + 16)[0]
            name = data[off + 64:off + 96].split(b"\0", 1)[0].decode("ascii", "replace")
            if mode & 0x8000 and name not in (".", ".."):
                entries.append(dict(name=name, size=length, cluster=cluster, directory=bool(mode & 0x20)))
        return entries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rom_folder", type=Path)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--extract", type=Path)
    ap.add_argument("--hash-disc", action="store_true")
    a = ap.parse_args()
    iso = next(a.rom_folder.glob("*.iso"))
    dongle = next(a.rom_folder.glob("*.bin"))
    card = Card(dongle)
    report = {"method": "Read-only filesystem parser; not REA decompilation", "disc": {"path": str(iso), "bytes": iso.stat().st_size}, "dongle": {"path": str(dongle), "bytes": len(card.raw), "sha256": hashlib.sha256(card.raw).hexdigest(), "page_bytes": card.page, "raw_page_bytes": card.stride}, "files": []}
    if a.hash_disc:
        with iso.open("rb") as stream:
            report["disc"]["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    for entry in card.directory():
        if entry["directory"]:
            report["files"].append(entry)
            continue
        data = card.chain(entry["cluster"], entry["size"])
        entry["sha256"] = hashlib.sha256(data).hexdigest()
        entry["magic_hex"] = data[:16].hex()
        if data.startswith(b"\x7fELF"):
            entry["elf"] = {"class": data[4], "endian": data[5], "machine": struct.unpack_from("<H", data, 18)[0], "entry": hex(struct.unpack_from("<I", data, 24)[0])}
        if a.extract and re.fullmatch(r"[A-Za-z0-9_.-]+", entry["name"]):
            a.extract.mkdir(parents=True, exist_ok=True)
            dest = a.extract / entry["name"]
            if dest.exists() and dest.read_bytes() != data:
                raise ValueError("Extraction would overwrite a different file")
            dest.write_bytes(data)
        report["files"].append(entry)
    listing = subprocess.run(["7z", "l", "-slt", str(iso)], capture_output=True, text=True, check=True)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.with_suffix(".iso-listing.txt").write_text(listing.stdout, encoding="utf-8")
    report["disc"]["inventory"] = a.output.with_suffix(".iso-listing.txt").name
    a.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

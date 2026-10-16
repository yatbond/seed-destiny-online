"""Collect unmodified dependency sources from the exact upstream build recipe.

Reads recipe text as data; does not execute downloaded files or the recipe.
Downloads are accepted only when their SHA-256 matches the pinned recipe.
"""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import re
import sys
import urllib.request


def main(recipe_path, destination):
    recipe = Path(recipe_path).read_text(encoding='utf-8')
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    variables = dict(re.findall(r'^set ([A-Z0-9_]+)=([^\r\n]+)', recipe, re.M))
    expanded = re.sub(r'%([A-Z0-9_]+)%', lambda m: variables.get(m[1], m[0]), recipe)
    entries = []
    prefixes = ('qt', 'QtApng-', 'freetype-', 'harfbuzz-', 'lpng', 'libjpeg-',
                'libwebp-', 'SDL3-', 'lz4-', 'zlib', 'zstd-', 'KDDockWidgets-',
                'plutovg-', 'plutosvg-', 'rapidyaml-', 'shaderc-', 'DirectX-Headers-')
    for match in re.finditer(r'^call :downloadfile "([^"]+)" "?(https://[^"\s]+)"? ([0-9a-f]{64})', expanded, re.M):
        name, url, digest = match.groups()
        if name.startswith(prefixes):
            entries.append({'file': name, 'url': url, 'sha256': digest})

    def fetch(entry):
        path = destination / entry['file']
        try:
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
                req = urllib.request.Request(entry['url'], headers={'User-Agent': 'SeedDestiny-source-packager/1'})
                with urllib.request.urlopen(req, timeout=45) as response, path.open('wb') as output:
                    while block := response.read(1024 * 1024):
                        output.write(block)
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != entry['sha256']:
                raise ValueError('Downloaded source hash does not match upstream recipe')
            entry.update(verified=True, bytes=path.stat().st_size)
        except Exception as exc:
            entry.update(verified=False, error=str(exc))
        print(json.dumps(entry), flush=True)
        return entry

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(fetch, entries))
    (destination / 'manifest.json').write_text(json.dumps({
        'dependency_build_run': 'https://github.com/PCSX2/pcsx2-windows-dependencies/actions/runs/37680131286',
        'recipe_commit': '904bd9da01c68bb31ae86f4ea6e0e9a1fb0743e2',
        'archives': results,
        'excluded': 'FFmpeg codecs and build-tool binaries are not shipped in the installer.'
    }, indent=2), encoding='utf-8')
    return 0 if results and all(item['verified'] for item in results) else 1


if __name__ == '__main__':
    raise SystemExit(main(*sys.argv[1:]))

"""Package the exact patched Play! checkout and matching dependency sources."""
from pathlib import Path
import hashlib, json, os, shutil, subprocess, sys, tarfile, urllib.request, zipfile

base = Path(os.environ['LOCALAPPDATA']) / 'SeedDestinyOnline'
source = base / 'play-probe/source'
destination = base / 'release-0.3-sources'
destination.mkdir(exist_ok=True)
project = Path(__file__).resolve().parents[1]
qt_name = 'qtbase-everywhere-src-5.15.2.tar.xz'
qt_url = 'https://download.qt.io/archive/qt/5.15/5.15.2/submodules/' + qt_name
qt_hash = '909fad2591ee367993a75d7e2ea50ad4db332f05e1c38dd7a5a274e156a4e0f8'
qt = destination / qt_name
if not qt.exists() or hashlib.sha256(qt.read_bytes()).hexdigest() != qt_hash:
    temporary = qt.with_suffix('.partial')
    with urllib.request.urlopen(qt_url, timeout=60) as response, temporary.open('wb') as output:
        shutil.copyfileobj(response, output)
    if hashlib.sha256(temporary.read_bytes()).hexdigest() != qt_hash:
        raise ValueError('Qt source checksum mismatch')
    temporary.replace(qt)
print('Qt source verified', flush=True)
licenses = destination / 'licenses'
licenses.mkdir(exist_ok=True)
with tarfile.open(qt, 'r:xz') as archive:
    for member in archive.getmembers():
        parts = Path(member.name).parts
        if member.isfile() and len(parts) == 2 and parts[-1].startswith('LICENSE'):
            (licenses / parts[-1]).write_bytes(archive.extractfile(member).read())
shutil.copy2(base/'release-sources/zlib132.zip', destination/'zlib132.zip')
shutil.copy2(source/'License.txt', licenses/'Play-BSD-2-Clause.txt')
gpl = base/'build/pcsx2x6/COPYING.GPLv3'
if not gpl.is_file():
    candidates = list((base/'build/pcsx2x6').glob('*COPY*'))
    raise RuntimeError('Locate PCSX2 GPL license: '+str(candidates))
shutil.copy2(gpl, licenses/'PCSX2-GPL-3.0.txt')
files = subprocess.check_output(['git','ls-files','--recurse-submodules','-z'], cwd=source).decode().split('\0')
manifest = {'play_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True).strip(),
            'submodules':subprocess.check_output(['git','submodule','status','--recursive'],cwd=source,text=True),
            'qt_source':{'url':qt_url,'sha256':qt_hash}, 'files':{}}
patch = subprocess.check_output(['git','diff','--binary'],cwd=source)
(project/'evidence/play-probe-local.patch').write_bytes(patch)
archive_path = destination/'play-patched-source-0.4.0.zip'
with zipfile.ZipFile(archive_path,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
    for name in files:
        file = source/name
        if not name or not file.is_file():continue
        data=file.read_bytes(); archive.writestr('Play/'+name,data)
        manifest['files'][name]=hashlib.sha256(data).hexdigest()
    archive.writestr('patches/Play.patch',patch)
    for sub in ('deps/Dependencies','deps/libchdr'):
        archive.writestr('patches/'+sub.replace('/','-')+'.patch',subprocess.check_output(['git','diff','--binary'],cwd=source/sub))
    for name in ('BuildPlay.cmd','PlayBuild.txt'):
        archive.write(project/'release'/name,name)
    headers = base/'build/pcsx2x6/3rdparty/vulkan'
    for p in headers.rglob('*'):
        if p.is_file():archive.write(p,'Vulkan-Headers/'+p.relative_to(headers).as_posix())
    archive.writestr('source-manifest.json',json.dumps(manifest,indent=2))
(destination/'manifest.json').write_text(json.dumps({
    'play_commit':manifest['play_commit'], 'qt':manifest['qt_source'],
    'archives':{p.name:{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
                for p in destination.iterdir() if p.is_file() and p.suffix in ('.zip','.xz')}
},indent=2)+'\n')
print('Source package ready: '+str(archive_path),flush=True)

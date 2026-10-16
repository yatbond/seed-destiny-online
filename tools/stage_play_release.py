"""Assemble an allowlisted release; never copy local game/test folders."""
from pathlib import Path
import hashlib, json, os, shutil, sys, zipfile

project=Path(__file__).resolve().parents[1]
base=Path(os.environ['LOCALAPPDATA'])/'SeedDestinyOnline'
package=base/'release-0.3-dist/SeedDestinyOnline'
assert (package/'SeedDestinyOnline.exe').is_file(), 'Build launcher first'
runtime=base/'runtime-play'
target=package/'emulator';target.mkdir(exist_ok=True)
for name in ('Play.exe','Qt5Core.dll','Qt5Gui.dll','Qt5Widgets.dll','z.dll','GameConfig.xml','platforms/qwindows.dll'):
    dst=target/name;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(runtime/name,dst)
crt=Path(r'C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Redist\MSVC\14.50.35710\x64\Microsoft.VC145.CRT')
for dll in crt.glob('*.dll'):shutil.copy2(dll,target/dll.name)
sources=package/'sources';sources.mkdir(exist_ok=True)
source_stage=base/'release-0.3-sources'
for name in ('play-patched-source-0.3.0.zip','qtbase-everywhere-src-5.15.2.tar.xz','zlib132.zip','manifest.json'):
    shutil.copy2(source_stage/name,sources/name)
shutil.copytree(source_stage/'licenses',package/'licenses',dirs_exist_ok=True)
for name in ('README.md',):shutil.copy2(project/name,package/name)
for name in ('PlayBuild.txt','BuildPlay.cmd'):shutil.copy2(project/'release'/name,sources/name)
for name in ('HostSetup.txt','ReleaseNotes.txt','Validation.json'):
    shutil.copy2(project/'release'/name,package/name)
shutil.copy2(project/'installer/GettingStarted.txt',package/'GettingStarted.txt')
with zipfile.ZipFile(sources/'launcher-source-0.3.1.zip','w',zipfile.ZIP_DEFLATED) as archive:
    for name in ('launch.py','relay.py','requirements.txt','Play SEED Destiny.cmd','Start room host.ps1','README.md'):
        archive.write(project/name,name)
    for folder in ('seedrooms','tests','tools','installer'):
        for p in (project/folder).rglob('*'):
            if p.is_file() and '__pycache__' not in p.parts and p.suffix in ('.py','.txt','.iss','.ps1','.cmd'):
                archive.write(p,p.relative_to(project).as_posix())
notices='''SEED Destiny Online 0.3.1 - component notices

The modified Play! executable incorporates PCSX2 GPL-3.0-or-later code.
Its corresponding patched source and submodules are in sources. Original
Play! and dependency copyright/license notices remain in those sources.
Qt Base 5.15.2 is dynamically linked; corresponding source and LGPLv3/GPLv3
texts are supplied. Compatible modified Qt DLLs may replace the originals.
z.dll is zlib 1.3.2, whose complete source and notice are included.

Python, Tcl/Tk, cryptography, OpenSSL and PyInstaller components are provided
with their distribution notices under _internal and licenses. PyInstaller's
bootloader exception permits redistribution of the packaged application.
Microsoft C/C++ runtime DLLs are Microsoft redistributable components.
They are not part of the GPL emulator source.

No original game disc, dongle, BIOS, extracted game program, screenshot,
or saved machine state is included. Keep sources and notices with copies.
'''
(package/'licenses/COMPONENTS.txt').write_text(notices)
python_base=Path(sys.base_prefix)
for name in ('LICENSE.txt','LICENSE'):
    if (python_base/name).is_file():shutil.copy2(python_base/name,package/'licenses/Python-LICENSE.txt')
site=Path(sys.prefix)/'Lib/site-packages'
for component in ('PyInstaller','cryptography'):
    for p in (site/component).glob('*COPY*'):
        if p.is_file():shutil.copy2(p,package/'licenses'/(component+'-'+p.name))
    for p in (site/component).glob('*LICENSE*'):
        if p.is_file():shutil.copy2(p,package/'licenses'/(component+'-'+p.name))
for p in package.rglob('*'):
    if p.is_file() and (p.suffix.lower() in ('.iso','.chd','.backupram','.bmp') or p.name in ('gsd1vera.bin','SE2LOAD','private-key.pem','launcher.json')):
        raise RuntimeError('Forbidden release data: '+str(p))
manifest={p.relative_to(package).as_posix():{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
          for p in package.rglob('*') if p.is_file() and p.name!='release-manifest.json'}
(package/'release-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
print(json.dumps({'package':str(package),'files':len(manifest),'bytes':sum(v['bytes'] for v in manifest.values())}),flush=True)

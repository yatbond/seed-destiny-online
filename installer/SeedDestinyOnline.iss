#ifndef PackageDir
  #define PackageDir "..\dist\SeedDestinyOnline"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

[Setup]
AppId={{6C6DB74B-7324-4972-AF0E-D8D2AB7DB829}
AppName=SEED Destiny Online
AppVersion=0.3.2
AppVerName=SEED Destiny Online 0.3.2 (experimental)
AppPublisher=SEED Destiny Online community project
DefaultDirName={localappdata}\Programs\SeedDestinyOnline
DefaultGroupName=SEED Destiny Online
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=SeedDestinyOnline-Setup-0.3.2
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\SeedDestinyOnline.exe
CloseApplications=no
RestartApplications=no
InfoBeforeFile=BeforeInstall.txt

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "{#PackageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "GettingStarted.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\SEED Destiny Online"; Filename: "{app}\SeedDestinyOnline.exe"; WorkingDir: "{app}"
Name: "{group}\Getting started"; Filename: "{app}\GettingStarted.txt"
Name: "{autodesktop}\SEED Destiny Online"; Filename: "{app}\SeedDestinyOnline.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\SeedDestinyOnline.exe"; Description: "Open SEED Destiny Online"; Flags: nowait postinstall skipifsilent

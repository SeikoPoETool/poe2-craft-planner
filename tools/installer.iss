; Windows installer (Inno Setup 6): built by the release workflow from the prepared package folder.
;   iscc /DSrc=<package folder> /DVer=<version> tools\installer.iss   ->  dist\poe2-craft-planner-setup.exe
; Installs per user (no admin rights) into %LOCALAPPDATA%\Programs, adds Start menu and desktop shortcuts and an
; uninstaller. The cache folder (the user's saved craft links, icons) is never removed by an update.

#ifndef Ver
  #define Ver "0.0.0"
#endif

[Setup]
AppId={{6E7E2B0C-5D7B-4C55-9C1F-2B5E3C9A8F41}
AppName=PoE2 Craft Planner
AppVersion={#Ver}
AppPublisher=SeikoPoETool
AppPublisherURL=https://github.com/SeikoPoETool/poe2-craft-planner
DefaultDirName={localappdata}\Programs\PoE2 Craft Planner
DefaultGroupName=PoE2 Craft Planner
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=poe2-craft-planner-setup
SetupIconFile=app.ico
UninstallDisplayIcon={app}\app.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
CloseApplications=yes

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#Src}\*"; DestDir: "{app}"; Excludes: "cache\*,smoke_server.log"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "app.ico"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
Name: "{app}\cache"; Flags: uninsneveruninstall

[Icons]
Name: "{group}\PoE2 Craft Planner"; Filename: "{app}\start.bat"; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"; Flags: runminimized
Name: "{group}\{cm:UninstallProgram,PoE2 Craft Planner}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\PoE2 Craft Planner"; Filename: "{app}\start.bat"; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"; Flags: runminimized; Tasks: desktopicon

[Run]
Filename: "{app}\start.bat"; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,PoE2 Craft Planner}"; Flags: nowait postinstall skipifsilent runminimized

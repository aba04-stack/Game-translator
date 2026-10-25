#define MyAppName "PatchLingo"
#define MyAppVersion "5.0"

[Setup]
AppId={{3CAAA5D2-364C-49EB-93EE-43B8DB2FA6A6}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
DefaultDirName={localappdata}\Programs\PatchLingo
DefaultGroupName=PatchLingo
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\PatchLingo.exe
OutputBaseFilename=PatchLingo-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Дополнительные значки:"

[Files]
Source: "..\dist\PatchLingo.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\PatchLingo"; Filename: "{app}\PatchLingo.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\PatchLingo"; Filename: "{app}\PatchLingo.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\PatchLingo.exe"; Description: "Запустить PatchLingo"; Flags: postinstall nowait skipifsilent

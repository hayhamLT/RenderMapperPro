; Inno Setup script for Render Mapper Pro (Windows).
; Build:  iscc /DMyAppVersion=1.7.3 installer\windows.iss
; Produces installer\Output\RenderMapperPro-Windows-x64-Setup.exe
;
; Installs the PyInstaller onedir build to Program Files (a stable location, so
; in-app updates aren't fighting a running .exe in Downloads), with Start-Menu /
; optional desktop shortcuts and a proper uninstaller. Unsigned — SmartScreen
; shows a one-time "unknown publisher" prompt until a code-signing cert is added.

#define MyAppName "Render Mapper Pro"
#define MyAppExeName "Render Mapper Pro.exe"
#define MyAppPublisher "Toy Robot Media"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
; A stable AppId keeps upgrades/uninstall consistent across versions.
AppId={{8E5C2F1A-3B7D-4E9A-9C21-0A1B2C3D4E5F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=Output
OutputBaseFilename=RenderMapperPro-Windows-x64-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Allow a per-user install if not elevated (so it never hard-fails on a locked machine).
PrivilegesRequiredOverridesAllowed=dialog
; The in-app updater quits the running app before launching this, but be safe:
; auto-close any lingering instance so its .exe can be replaced (no "file in use").
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
; The whole PyInstaller onedir output (exe + _internal/).
Source: "..\dist\Render Mapper Pro\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

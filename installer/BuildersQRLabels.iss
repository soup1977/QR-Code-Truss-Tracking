; BuildersQRLabels.iss — Inno Setup 6 installer script for Builders Truss QR
;
; Prerequisites:
;   1. Run PyInstaller first:  pyinstaller BuildersQRLabels.spec --clean --noconfirm
;   2. This script expects the PyInstaller output at:  ..\dist\BuildersQRLabels\
;
; Usage (from project root):
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\BuildersQRLabels.iss
;
; Output: Output\BuildersTrussQR-0.9.0-Setup.exe

#define AppName      "Builders Truss QR"
#define AppVersion   "0.9.0"
#define AppPublisher "Builders Inc."
#define AppExeName   "BuildersQRLabels.exe"
#define AppDir       "BuildersTrussQR"

[Setup]
AppId={{A3F8B2C1-4D7E-4A9F-B6C2-1E5D8F3A7B9C}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppDir}
DefaultGroupName={#AppName}
AllowNoIcons=yes
; Per-user install — no admin rights required
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\Output
OutputBaseFilename=BuildersTrussQR-{#AppVersion}-Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
; Minimum Windows 10
MinVersion=10.0
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} Installer

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
; Include the entire PyInstaller bundle (recurse all files and subdirs)
Source: "..\dist\BuildersQRLabels\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Start Menu shortcut
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
; Optional desktop shortcut (only if user selected the task above)
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
; Offer to launch the app after install finishes
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Clean up auto-generated runtime files on uninstall (config is left intact)
Type: files; Name: "{app}\*.log"

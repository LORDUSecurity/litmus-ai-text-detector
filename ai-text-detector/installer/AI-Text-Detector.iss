; ---------------------------------------------------------------------------
; Litmus — Windows installer (Inno Setup)
;
; NOTE ON BUILDING THIS FILE:
;   Inno Setup 6 is installed here, but NOT where a Program Files sweep would look:
;   a per-user winget install (JRSoftware.InnoSetup) puts it under %LOCALAPPDATA%, so on
;   this machine the compiler is
;
;       C:\Users\adam\AppData\Local\Programs\Inno Setup 6\ISCC.exe
;
;   Build from the repo root:
;
;       "<that path>" installer/AI-Text-Detector.iss
;
;   (or open it in the "Inno Setup Compiler" GUI and press Ctrl+F9). It emits
;   installer\Output\AI-Text-Detector-Setup-<version>.exe.
;
;   Why SourceDir=.. is below: Inno resolves a relative [Files] Source path against the
;   directory holding the .iss, not the working directory. Without it this script asks for
;   installer\.localbuild\out\AI-Text-Detector.exe and aborts on the first line of [Files] —
;   which is what the original version of this file did, and why the paths here are written
;   relative to the repo root.
;
; What the installer ships:
;   The single-file PyInstaller build of backend.serve_local (built by the
;   repo-root `pyinstaller ... run_local.py` command recorded in
;   installer/BUILD.md), which serves the built Angular SPA + the JSON API from
;   one process on 127.0.0.1 and opens the default browser. The SQLite history
;   and the Fernet key live in %LOCALAPPDATA%\AITextDetector (created on first
;   run), NOT inside the install folder — so a per-user, no-admin install works
;   and the user's data survives upgrades and uninstalls.
;
;   The installer does NOT bundle any API key. The app ships with no .env, so it
;   seeds providers + the measured model ranking only; the signed-in admin adds a
;   key at runtime through the BYOK screen, exactly like the hosted page.
; ---------------------------------------------------------------------------

; The product is Litmus; the filenames are not. MyAppExeName, MySetupBase and the data directory
; below keep the original "AI-Text-Detector" / "AITextDetector" spellings on purpose: renaming the
; binary would orphan every existing install's Start-Menu entry and uninstall key, and renaming
; %LOCALAPPDATA%\AITextDetector would leave a user's detection history and Fernet key behind in the
; old folder with nothing pointing at it. AppId is the GUID that ties an upgrade to an install, so
; it must never change either — a new GUID side-installs Litmus next to the old app.
#define MyAppName      "Litmus"
#define MyAppVersion   "1.0.0"
#define MyAppPublisher "Litmus"
#define MyAppExeName   "AI-Text-Detector.exe"
; Filename base — deliberately not {#MyAppName}, which contains a space and would emit
; "AI Text Detector-Setup-1.0.0.exe".
#define MySetupBase    "AI-Text-Detector-Setup"
; Source produced by the PyInstaller onefile build:
#define MyAppSourceExe ".localbuild\out\AI-Text-Detector.exe"

[Setup]
; A stable GUID makes upgrades replace this install instead of side-installing.
; Generate your own once (Tools > Generate GUID in the Inno IDE) if you fork.
AppId={{7A2F6C41-3E5B-4D17-9C0E-5B1D4F2A8E63}
; [Files] Source paths below are written relative to the repo root, so anchor them there.
SourceDir=..
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; OutputDir is resolved against SourceDir too (measured: "Output" put the installer in the repo
; root, not in installer\), so this path is repo-root-relative like [Files].
OutputDir=installer\Output
OutputBaseFilename={#MySetupBase}-{#MyAppVersion}
; Per-user install by default: no UAC prompt, works for "user upload" on locked
; machines. The user can still choose "Install for anyone" (admin) at runtime.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The setup wizard and the compiled installer file take the same tile the favicon and the header
; mark are drawn from (tools/make_icon.py writes it; --check fails if the three ever disagree).
; Without this the installer is a generic window in Downloads, which is the first thing a user
; sees of the product.
SetupIconFile=installer\app-icon.ico
; "x64" is accepted but deprecated and ISCC warns on every compile, substituting x64os.
; x64compatible is what the 43 MB PyInstaller onefile actually needs (it runs on ARM64 too).
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#MyAppSourceExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "installer\README.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Launch the local server after install. It opens the browser on its own; the
; console window it runs in is what the user closes to stop the server.
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Leave %LOCALAPPDATA%\AITextDetector (history + encryption key) untouched on
; uninstall so an accidental reinstall keeps the user's saved detections. Add
; the two lines below only if you DO want data removed on uninstall:
; Type: filesandordirs; Name: "{localappdata}\AITextDetector"

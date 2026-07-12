; HERMES installer (roadmap 7.2 / SPEC_dist.3): per-user, no admin, clean uninstall.
;
; Wraps dist.2's onedir tree (build/dist/HERMES/). Version + publisher are injected by the
; build driver via ISCC /D... (single source of truth = shared/version.py). Compile with:
;     python build/build.py --installer
; or directly: ISCC.exe /DMyAppVersion=0.1.0 /DMyAppPublisher=Saltydayn build/hermes.iss
;
; The installed app uses %LOCALAPPDATA%\HERMES for user data (dist.1), SEPARATE from the
; {app} install dir, so uninstalling {app} never touches the user's clips/config. The
; launch-on-boot Run-key value name "HERMES" is the contract dist.4's autostart.py matches.

#define MyAppName "HERMES"
#define MyAppExeName "HERMES.exe"
; both defines are overridden by the driver via /DMyAppVersion=... /DMyAppPublisher=...
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#ifndef MyAppPublisher
  #define MyAppPublisher "Saltydayn"
#endif

[Setup]
AppId={{0141F00E-DB9E-4583-9850-D92A866524FC}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; per-user (PrivilegesRequired=lowest) => {localappdata}\Programs\HERMES
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
; no admin, no UAC, no Program Files write
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=HERMES-{#MyAppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\assets\hermes.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon";  Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"
Name: "launchonboot"; Description: "Launch {#MyAppName} when Windows starts"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
; The whole dist.2 onedir tree. portable.txt is NOT shipped; the installed app runs in
; installed mode (%LOCALAPPDATA%\HERMES), per dist.1.
Source: "dist\HERMES\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}";        Filename: "{app}\{#MyAppExeName}"
Name: "{userdesktop}\{#MyAppName}";  Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; Launch-on-boot. Value name "HERMES" + quoted path == shared/autostart.py's contract, so
; the installer checkbox and the in-app Home toggle are the SAME switch. Removed on uninstall.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
  ValueName: "HERMES"; ValueData: """{app}\{#MyAppExeName}"""; Tasks: launchonboot; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
{ Opt-in user-data wipe. [UninstallDelete] is unconditional, so do it in Pascal. Default is
  KEEP (MB_DEFBUTTON2 -> No): a default uninstall must NEVER delete the user's clips/config.
  Silent uninstalls skip the prompt entirely and keep the data: /SUPPRESSMSGBOXES does not
  suppress [Code] MsgBox calls, so an unguarded prompt would hang a /VERYSILENT uninstall. }
var
  WipeData: Boolean;

function InitializeUninstall(): Boolean;
begin
  WipeData := False;
  if not UninstallSilent() then
    WipeData := MsgBox(
      'Remove your HERMES clips, Shorts, and settings too?' + #13#10 +
      'Choose No to keep them (default). Choose Yes to delete everything in' + #13#10 +
      ExpandConstant('{localappdata}\HERMES'),
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
  Result := True;
end;

procedure CurUninstallStepChanged(CurStep: TUninstallStep);
begin
  if (CurStep = usPostUninstall) and WipeData then
    DelTree(ExpandConstant('{localappdata}\HERMES'), True, True, True);
end;

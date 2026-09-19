; Presence tracker — Windows installer (Inno Setup 6)
;
; Build the executable first (build.ps1), then compile this script:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DServerUrl="https://presence.yourcompany.com" installer.iss
;
; Produces PresenceTracker-Setup.exe, which can be pushed with Group Policy,
; Intune or any deployment tool, and installs silently with /VERYSILENT.

#ifndef ServerUrl
  #define ServerUrl "https://presence.yourcompany.com"
#endif
#define AppName "Presence Tracker"
#define AppVersion "1.0.0"
#define AppPublisher "Your Company"
#define ExeName "PresenceTracker.exe"

[Setup]
AppId={{8B3F1C42-5E7A-4D19-9A2B-6F4C8E1D3A50}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\Presence
DefaultGroupName=Presence
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=PresenceTracker-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Machine-wide install: the agent must be present for every user of the laptop.
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#ExeName}
; Employees should not be able to remove the tracker themselves.
UninstallDisplayName={#AppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "dist\{#ExeName}";        DestDir: "{app}"; Flags: ignoreversion
Source: "watchdog-task.xml";      DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\docs\PRIVACY.md";  DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Dirs]
; Machine-wide config, writable only by administrators. An employee cannot
; repoint the agent at a server of their own.
Name: "{commonappdata}\Presence"; Permissions: users-readexec admins-full

[Icons]
Name: "{group}\{#AppName}";           Filename: "{app}\{#ExeName}"
Name: "{autostartup}\{#AppName}";     Filename: "{app}\{#ExeName}"

[Run]
; Install the watchdog scheduled task, which restarts the agent if it stops.
Filename: "schtasks.exe"; \
  Parameters: "/Create /XML ""{app}\watchdog-task.xml"" /TN ""Presence Tracker"" /F"; \
  Flags: runhidden waituntilterminated; \
  StatusMsg: "Installing the tracker watchdog..."

Filename: "{app}\{#ExeName}"; \
  Description: "Start {#AppName} now"; \
  Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "schtasks.exe"; Parameters: "/Delete /TN ""Presence Tracker"" /F"; \
  Flags: runhidden; RunOnceId: "RemoveWatchdog"
Filename: "taskkill.exe"; Parameters: "/IM {#ExeName} /F"; \
  Flags: runhidden; RunOnceId: "StopAgent"

[Code]
procedure WriteMachineConfig();
var
  ConfigPath: String;
  Config: String;
begin
  { The server URL is baked in at install time so employees never type it. }
  ConfigPath := ExpandConstant('{commonappdata}\Presence\config.json');
  Config := '{' + #13#10 +
            '  "server_url": "{#ServerUrl}",' + #13#10 +
            '  "verify_tls": true' + #13#10 +
            '}';
  ForceDirectories(ExpandConstant('{commonappdata}\Presence'));
  SaveStringToFile(ConfigPath, Config, False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    WriteMachineConfig();
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  { Installing over a running agent leaves a locked file behind. }
  Exec('taskkill.exe', '/IM {#ExeName} /F', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

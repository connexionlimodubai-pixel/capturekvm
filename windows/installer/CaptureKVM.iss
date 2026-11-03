; Inno Setup script for CaptureKVM: one installer for both ends of the link.
;
;   cd windows
;   .\build.ps1 -Installer
;
; build.ps1 builds the app, fetches ViGEm's files, and runs ISCC with /DAppVersion read from
; capturekvm/__init__.py, the one place the Windows version is set. Inno Setup 6.
;
; Per user, like Beamer's own installer: no administrator permission to install or to run. Two
; things a PC being watched may need do take administrator permission, and are asked for only on
; that PC, only when ticked, and each through Windows's own prompt:
;
;   * firewall rules, so the laptop or tablet can reach host mode (installer\host-firewall.ps1)
;   * the ViGEmBus driver, when none is installed, for game controllers (Nefarius's signed MSI)
;
; Which end the machine is gets asked here, on a page of its own, and handed to the app with
; `CaptureKVM.exe --set-mode`. The zip could only ask when the app first started.
;
; Silent installs take /ROLE=host|receiver|ask, plus /FIREWALL=0 and /DRIVER=0 to skip those.

#ifndef AppVersion
  #error Pass the version: ISCC /DAppVersion=0.4.2 CaptureKVM.iss (build.ps1 -Installer does)
#endif

#define Dist "..\dist\CaptureKVM"
#define Vendor "..\vendor"

[Setup]
AppId={{71A012F8-E86B-46B0-8339-E474B01CB7D1}
AppName=CaptureKVM
AppVersion={#AppVersion}
AppVerName=CaptureKVM {#AppVersion}
VersionInfoVersion={#AppVersion}.0
AppPublisher=CaptureKVM
AppPublisherURL=https://github.com/connexionlimodubai-pixel/capturekvm
AppSupportURL=https://github.com/connexionlimodubai-pixel/capturekvm/issues
DefaultDirName={localappdata}\Programs\CaptureKVM
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=CaptureKVM-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; CaptureKVM.exe is x64. ARM Windows runs x64 apps under emulation, so it is allowed.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\CaptureKVM.exe
UninstallDisplayName=CaptureKVM
; A running CaptureKVM holds its own files; Windows's Restart Manager asks to close it first.
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
; The one-folder build: CaptureKVM.exe and _internal\, ViGEmClient.dll among it.
Source: "{#Dist}\*"; Excludes: "Change mode.cmd"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "host-firewall.ps1"; DestDir: "{app}"; Flags: ignoreversion
; Unpacked to a temporary folder and run only if the driver is wanted and missing.
Source: "{#Vendor}\ViGEmBusSetup_x64.msi"; Flags: dontcopy

[InstallDelete]
; The zip's way back to the picker. The Start menu has its own now.
Type: files; Name: "{app}\Change mode.cmd"

[Icons]
Name: "{autoprograms}\CaptureKVM"; Filename: "{app}\CaptureKVM.exe"
Name: "{autoprograms}\CaptureKVM - change mode"; Filename: "{app}\CaptureKVM.exe"; Parameters: "--pick"; \
  Comment: "Ask again which end of the link this machine is"
Name: "{autodesktop}\CaptureKVM"; Filename: "{app}\CaptureKVM.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\CaptureKVM.exe"; Description: "Start CaptureKVM now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Recorded only when this install added the rules, so an uninstall that has nothing of the
; firewall's to take back does not ask for administrator permission to do it.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
  Parameters: "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""{app}\host-firewall.ps1"" -ExePath ""{app}\CaptureKVM.exe"" -Remove"; \
  Flags: shellexec runhidden waituntilterminated; Verb: "runas"; RunOnceId: "CaptureKVMFirewall"; \
  Check: FirewallWanted

[Code]
const
  RoleReceiver = 0;
  RoleHost = 1;
  RoleAsk = 2;
  DriverKey = 'SYSTEM\CurrentControlSet\Services\ViGEmBus';

var
  RolePage: TInputOptionWizardPage;
  HostPage: TInputOptionWizardPage;
  FirewallItem: Integer;
  DriverItem: Integer;

function DriverInstalled: Boolean;
begin
  Result := RegKeyExists(HKLM, DriverKey);
end;

function OldBeamerInstalled: Boolean;
begin
  Result := FileExists(ExpandConstant('{localappdata}\Beamer\Beamer.exe'));
end;

function ChosenRole: Integer;
var
  I: Integer;
begin
  Result := RoleAsk;
  for I := 0 to 2 do
    if RolePage.Values[I] then
      Result := I;
end;

function RoleArgument: String;
begin
  case ChosenRole of
    RoleReceiver: Result := 'receiver';
    RoleHost: Result := 'host';
  else
    Result := 'ask';
  end;
end;

function HostItemChosen(const Item: Integer): Boolean;
begin
  Result := (ChosenRole = RoleHost) and (Item >= 0) and HostPage.Values[Item];
end;

function FirewallWanted: Boolean;
begin
  Result := HostItemChosen(FirewallItem);
end;

function DriverWanted: Boolean;
begin
  Result := HostItemChosen(DriverItem);
end;

{ A silent install's answers come on the command line; an interactive one's from last time. }
function InitialRole: Integer;
var
  Given: String;
begin
  Given := Lowercase(ExpandConstant('{param:ROLE|}'));
  if Given = '' then
    Given := GetPreviousData('Role', 'receiver');
  if Given = 'host' then
    Result := RoleHost
  else if Given = 'ask' then
    Result := RoleAsk
  else
    Result := RoleReceiver;
end;

procedure InitializeWizard;
var
  Note: String;
begin
  RolePage := CreateInputOptionPage(wpSelectDir,
    'Which end is this machine?',
    'CaptureKVM is both ends of the link; this decides which one starts here.',
    'You can change it later from the Start menu: "CaptureKVM - change mode".',
    True, False);
  RolePage.Add('This machine watches another PC: the capture card plugs in here (receiver)');
  RolePage.Add('This is the PC being watched: the games run here (host)');
  RolePage.Add('Ask me when CaptureKVM first starts');
  RolePage.Values[InitialRole] := True;

  Note := 'The laptop or tablet sends its keyboard, mouse and controller to this PC. ' +
          'Each item below asks for administrator permission through Windows''s own prompt.';
  if DriverInstalled then
    Note := Note + #13#10#13#10 + 'The ViGEmBus controller driver is already installed.';
  if OldBeamerInstalled then
    Note := Note + #13#10#13#10 + 'An older Beamer is installed on this PC. Close it before ' +
            'starting CaptureKVM, or the two will both try to drive the controller.';
  HostPage := CreateInputOptionPage(RolePage.ID,
    'Set up the PC being watched', 'What host mode needs from Windows.', Note, False, False);

  FirewallItem := HostPage.Add('Let the laptop or tablet through Windows Firewall');
  HostPage.Values[FirewallItem] := ExpandConstant('{param:FIREWALL|1}') <> '0';
  DriverItem := -1;
  if not DriverInstalled then
  begin
    DriverItem := HostPage.Add('Install the ViGEmBus driver, for game controllers');
    HostPage.Values[DriverItem] := ExpandConstant('{param:DRIVER|1}') <> '0';
  end;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = HostPage.ID) and (ChosenRole <> RoleHost);
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'Role', RoleArgument);
end;

{ Shown, not thrown: the app is installed whatever happens here, and each of these has a way to
  be done later. A failed step must not roll back a working install. }
procedure Warn(const Text: String);
begin
  Log(Text);
  if not WizardSilent then
    MsgBox(Text, mbInformation, MB_OK);
end;

procedure RecordRole;
var
  Code: Integer;
begin
  if not Exec(ExpandConstant('{app}\CaptureKVM.exe'), '--set-mode ' + RoleArgument, '',
              SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then
    Warn('CaptureKVM could not record which end this machine is, so it will ask when it starts.');
end;

procedure OpenFirewall;
var
  Code: Integer;
  Arguments: String;
begin
  Arguments := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
               ExpandConstant('{app}\host-firewall.ps1') + '" -ExePath "' +
               ExpandConstant('{app}\CaptureKVM.exe') + '"';
  if not ShellExec('runas', ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Arguments,
                   '', SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then
    Warn('The firewall rules were not added. Windows will ask whether to allow CaptureKVM the ' +
         'first time a laptop or tablet connects; answer Allow, on private networks.');
end;

procedure InstallDriver;
var
  Code: Integer;
  Msi: String;
begin
  ExtractTemporaryFile('ViGEmBusSetup_x64.msi');
  Msi := ExpandConstant('{tmp}\ViGEmBusSetup_x64.msi');
  { The MSI is per machine and asks for administrator permission itself. 3010: done, restart
    needed, which Windows will say. }
  if not ShellExec('', ExpandConstant('{sys}\msiexec.exe'), '/i "' + Msi + '" /passive /norestart',
                   '', SW_SHOW, ewWaitUntilTerminated, Code) or ((Code <> 0) and (Code <> 3010)) then
    Warn('The ViGEmBus driver was not installed, so game controllers will not work on this PC ' +
         'yet. Install it from https://github.com/nefarius/ViGEmBus/releases and restart CaptureKVM.');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep <> ssPostInstall then
    Exit;
  RecordRole;
  if FirewallWanted then
    OpenFirewall;
  if DriverWanted then
    InstallDriver;
end;

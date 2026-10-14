#define MyAppName "Phone Controller"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Phone Controller"
#define MyAppExeName "PhoneController.exe"

[Setup]
AppId={{79250189-8252-4772-BDA7-A82411E181F2}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\installer
OutputBaseFilename=PhoneControllerSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no
RestartApplications=no
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "..\dist\PhoneController\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
function StopPhoneController(): Boolean;
var
  ResultCode: Integer;
  Executable: String;
begin
  Result := True;
  Executable := ExpandConstant('{app}\{#MyAppExeName}');
  if FileExists(Executable) then
  begin
    Result := Exec(Executable, '--shutdown', '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  if StopPhoneController() then
    Result := ''
  else
    Result := 'Phone Controller could not be stopped. Close it and try again.';
end;

function InitializeUninstall(): Boolean;
begin
  Result := StopPhoneController();
  if not Result then
    MsgBox('Phone Controller could not be stopped. Close it and try again.',
      mbError, MB_OK);
end;

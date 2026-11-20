#define AppVersion "21.1.2"
#define AppExe "NYC-Gestao.exe"
[Setup]
AppId={{D8A38156-B8B7-4865-949B-67D801C48CA1}
AppName=NYC Gestão Desktop
AppVersion={#AppVersion}
AppPublisher=NYC Gestão
DefaultDirName={localappdata}\Programs\NYC Gestao
DefaultGroupName=NYC Gestão
PrivilegesRequired=lowest
UsePreviousAppDir=yes
OutputDir=release
OutputBaseFilename=NYC-Gestao-Setup-21.1.2-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
UninstallDisplayIcon={app}\{#AppExe}
DisableProgramGroupPage=yes
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
[Files]
Source: "dist\NYC-Gestao\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{autodesktop}\NYC Gestão"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{autoprograms}\NYC Gestão"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
[Run]
Filename: "{app}\{#AppExe}"; Description: "Abrir NYC Gestão"; Flags: nowait postinstall skipifsilent
[Code]
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then begin
    { Remover apenas o Python virtual desnecessário da instalação antiga.
      O banco de dados em %APPDATA% NÃO é apagado. }
    if DirExists(ExpandConstant('{app}\.venv')) then
      DelTree(ExpandConstant('{app}\.venv'), True, True, True);
    RegDeleteKeyIncludingSubkeys(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\NYC-Gestao-21.1');
  end;
end;

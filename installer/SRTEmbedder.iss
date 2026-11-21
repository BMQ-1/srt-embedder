; Inno Setup Script for SRT Embedder

#define MyAppName "SRT Embedder"
#define MyAppVersion "1.2.0"
#define MyAppPublisher "SRT Embedder Contributors"
#define MyAppExeName "SRT-Embedder.exe"
#define MyCliExeName "srt-embedder-cli.exe"

[Setup]
AppId={{8D4F62C3-9E41-4A1D-9A77-4A2B1D55C011}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\SRT Embedder
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=SRT-Embedder-Setup-{#MyAppVersion}
SetupIconFile=..\srt_embedder\assets\icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ChangesEnvironment=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "contextmenu"; Description: "Add 'Embed Subtitles with SRT Embedder' to Windows Explorer right-click menu"; GroupDescription: "Integration"

[Files]
Source: "..\dist\SRT-Embedder\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyCliExeName}"; Parameters: "shell install"; Flags: runhidden; Tasks: contextmenu
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\{#MyCliExeName}"; Parameters: "shell uninstall"; Flags: runhidden

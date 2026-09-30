; 当前用户安装，升级不覆盖用户配置，卸载不删除用户模型或索引。
#ifndef AppVersion
  #define AppVersion "1.1.0"
#endif
#ifndef SourceDir
  #error SourceDir is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif

[Setup]
AppId={{D9739D28-31DA-4078-A55F-7EF09D443038}
AppName=Local Model Search
AppVersion={#AppVersion}
AppPublisher=pipi123456798
AppPublisherURL=https://github.com/pipi123456798/local-model-search
DefaultDirName={localappdata}\Programs\LocalModelSearch
DefaultGroupName=Local Model Search
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=LocalModelSearch-{#AppVersion}-Windows-x64-Setup
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\LocalModelSearch.exe
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Local Model Search"; Filename: "{app}\LocalModelSearch.exe"
Name: "{group}\Configuration and logs"; Filename: "{app}\LocalModelSearch.exe"; Parameters: "--open-data"
Name: "{autodesktop}\Local Model Search"; Filename: "{app}\LocalModelSearch.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\LocalModelSearch.exe"; Description: "Launch Local Model Search"; Flags: nowait postinstall skipifsilent

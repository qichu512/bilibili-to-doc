; B站视频转文档 安装脚本 (Inno Setup 6)
#define MyAppName "B站视频转文档"
#define MyAppVersion "1.0.2"
#define MyAppExeName "bili2doc.exe"

[Setup]
AppId={{8F1C7B3A-9E2D-4C6F-A5B0-B11D2D0C0001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=bili2doc
DefaultDirName={localappdata}\Programs\bili2doc
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\app.ico
OutputDir=..\installer
OutputBaseFilename=bili2doc-setup-1.0.2
SetupIconFile=..\app.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式（双击即可使用）"; GroupDescription: "附加任务："

[Files]
Source: "..\dist\bili2doc\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; DestName: "README.md"; Flags: ignoreversion

[Icons]
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
// 从旧开发目录迁移用户配置，避免重新导入 Cookies 和 API Key
const
  LegacyDir = 'C:\common\bili2doc';

procedure CopyFileIfMissing(const Src, Dst: string);
begin
  if FileExists(Src) and not FileExists(Dst) then
    FileCopy(Src, Dst, False);
end;

procedure MigrateLegacyConfig;
var
  AppDir, DstData: string;
begin
  AppDir := ExpandConstant('{app}');
  if not DirExists(LegacyDir) then
    Exit;
  CopyFileIfMissing(LegacyDir + '\config.json', AppDir + '\config.json');
  if DirExists(LegacyDir + '\data') then
  begin
    DstData := AppDir + '\data';
    ForceDirectories(DstData);
    CopyFileIfMissing(LegacyDir + '\data\cookies.txt', DstData + '\cookies.txt');
    CopyFileIfMissing(LegacyDir + '\data\cookies_raw.txt', DstData + '\cookies_raw.txt');
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    MigrateLegacyConfig;
end;

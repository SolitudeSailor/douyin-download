; ============================================================
;  抖音视频下载器 —— Windows 安装程序脚本（Inno Setup 6）
;
;  编译：
;    ISCC.exe build\installer.iss
;    或直接跑 build\build_installer.bat
;
;  产物：
;    release\抖音视频下载器-安装程序.exe
;
;  ---- 三个关键设计（别顺手改掉）----
;
;  1) 默认装到 %LOCALAPPDATA%，不要管理员权限。
;     程序运行时要在自己旁边生成 config.yaml / browser_profile / Download，
;     装进 C:\Program Files 会因普通用户无写权限而运行失败。
;     脚本用 IsDirWritable() 在「选择目录」页做了拦截，防止用户误选。
;
;  2) 安装包内**只含 exe 与使用说明** —— [Files] 里写死两行，绝不扫描目录。
;     主程序直接取 ..\dist\douyin-tool.exe，说明取 ..\build\使用说明.txt。
;     （2026-09-29 起 dist\ 只放构建产物：exe / README.md / data_dir.txt（数据指针），
;      运行时数据 config.yaml / browser_profile / Download 已全部收归 ..\data\。
;      即便如此，[Files] 仍必须写死路径、绝不扫目录 —— 这是防泄露的硬约束，别改。）
;     不含任何 Cookie、登录态、下载记录、视频 —— 这些由程序首次运行自行生成。
;
;  3) AppPublisher / AppCopyright / AppSupportURL 等字段一律留空，
;     不写入任何可识别的个人信息。
; ============================================================

#define AppName "抖音视频下载器"
#define AppVersion "1.0.0"
#define AppExeName "douyin-tool.exe"
#define DocName "使用说明.txt"

[Setup]
; AppId 是卸载/升级识别的唯一标识，一旦发布就不要再改
AppId={{7C4E2B90-1F3A-4D6E-9B21-A5C8D3E07F64}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
DefaultDirName={localappdata}\DouyinDownloader
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\release
OutputBaseFilename=抖音视频下载器-安装程序
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}
SetupIconFile=icon.ico
AllowNoIcons=yes
ShowLanguageDialog=no
; 安装前展示使用说明（只读，不要求“同意”）
InfoBeforeFile=..\build\{#DocName}

[Languages]
Name: "chs"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："

[Files]
Source: "..\dist\{#AppExeName}";  DestDir: "{app}"; Flags: ignoreversion
Source: "..\build\{#DocName}";    DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}";        Filename: "{app}\{#AppExeName}"
Name: "{group}\{#DocName}";        Filename: "{app}\{#DocName}"
Name: "{group}\卸载 {#AppName}";   Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";  Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "立即运行 {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
{ ------------------------------------------------------------------
  目录可写性检测：
  程序必须能在自己所在目录写文件，否则登录态存不下、视频没处放。

  注意：Inno 的 Pascal Script 不支持异常处理（没有 try/except），
  所以这里只用「建目录 / 删目录」这类不抛异常的 API 来探测，
  不能用 TFileStream。

  逻辑：试着在目标下建一个临时子目录，建成即说明有写权限。
        建不成就看目录是否本来就存在（此前已装过 → 必然可写）。
  ------------------------------------------------------------------ }
function IsDirWritable(const Dir: string): Boolean;
var
  TestDir: string;
begin
  TestDir := AddBackslash(Dir) + '.write_test';
  if ForceDirectories(TestDir) then
  begin
    RemoveDir(TestDir);
    Result := True;
  end
  else
    Result := DirExists(Dir);
end;

{ 常见系统保护目录，普通用户即使建得出目录，后续也容易出权限问题 }
function IsProtectedLocation(const Dir: string): Boolean;
var
  D: string;
begin
  D := LowerCase(AddBackslash(Dir));
  Result :=
    (Pos('\program files', D) > 0) or
    (Pos('\programdata', D) > 0) or
    (Pos('\windows\', D) > 0);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    if IsProtectedLocation(WizardDirValue) or (not IsDirWritable(WizardDirValue)) then
    begin
      MsgBox('这个位置当前用户没有写入权限，程序无法在这里保存登录信息和下载的视频。' + #13#10 + #13#10 +
             '请换一个位置，例如：' + #13#10 +
             '  ·  C:\Users\你的用户名\DouyinDownloader' + #13#10 +
             '  ·  D:\抖音下载' + #13#10 + #13#10 +
             '（不要装到 C:\Program Files 下）',
             mbError, MB_OK);
      Result := False;
    end;
  end;
end;

{ ------------------------------------------------------------------
  卸载：问一句要不要连数据一起删。
  默认（选“否”）保留登录态与已下载视频，方便以后重装接着用。
  ------------------------------------------------------------------ }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  R: Integer;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    { 静默卸载（/SILENT、/VERYSILENT）下不能弹框，否则会永远卡住。
      此时一律保守处理：保留用户数据，只卸载程序本体。 }
    if UninstallSilent then
      Exit;

    R := MsgBox('是否一并删除程序目录里的登录信息和已下载的视频？' + #13#10 + #13#10 +
                '【是】彻底清空，包括 Download 文件夹里的所有视频' + #13#10 +
                '【否】只卸载程序，保留数据（以后重装可直接接着用）',
                mbConfirmation, MB_YESNO);
    if R = IDYES then
      DelTree(ExpandConstant('{app}'), True, True, True);
  end;
end;

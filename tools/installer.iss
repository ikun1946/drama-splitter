; AI 短剧智能分集器 —— Windows 安装程序（Inno Setup 6）
;
; 用法：
;     "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" tools\installer.iss
; 或
;     python tools/build_installer.py
;
; 前提：先跑 `python tools/build_package.py` 生成 dist\drama-splitter\。
;
; 为什么限定为「按用户安装」而不是装进 Program Files
; --------------------------------------------------
; 本程序把配置、缓存、模型目录都写在**exe 同级**（config.json / cache/ /
; models/）——这是有意的：打包后源码位于 `_internal/` 的只读安装目录里，
; 写在那里会直接 PermissionError（见 app/core/app_config.py::app_dir）。
; 但反过来，exe 同级也意味着：若装进 C:\Program Files，普通（非管理员）
; 用户运行时同样写不进去配置。
; 所以这里用 PrivilegesRequired=lowest 固定装到 %LOCALAPPDATA%\Programs\，
; 且**不提供**「为所有用户安装」的选项——那会把程序放到普通用户不可写的目录。
;
; FFmpeg 为什么不随包分发
; ---------------------
; 1) 体积：GPL 全量构建里 ffmpeg.exe + ffprobe.exe 各约 194 MB；
; 2) 许可：GPLv2+ 二进制随安装包对外分发，会让整个分发物承担 GPL 义务。
; 改为安装时检测，缺失则给出明确的下一步（见 [Code] 段的 CurStepChanged）。
;
; FFprobe 是必需的，ffplay 是可选的（只用于预览播放出声，见 ffmpeg.py 第 61 行），
; 所以提示里只要求前两个。

[Setup]
AppId={{8F3C2A17-6D94-4E2B-9C51-7A0D3E68B412}
AppName=AI 短剧智能分集器
AppVersion=1.0.0
AppVerName=AI 短剧智能分集器 1.0.0
AppPublisher=ikun1946
AppPublisherURL=https://github.com/ikun1946/drama-splitter
AppSupportURL=https://github.com/ikun1946/drama-splitter/issues
AppUpdatesURL=https://github.com/ikun1946/drama-splitter/releases
VersionInfoVersion=1.0.0.0
VersionInfoDescription=AI 短剧智能分集器 安装程序

; 按用户安装，免管理员
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\drama-splitter

DefaultGroupName=AI 短剧智能分集器
DisableProgramGroupPage=yes
DisableWelcomePage=no
ShowLanguageDialog=no

UninstallDisplayName=AI 短剧智能分集器
UninstallDisplayIcon={app}\drama-splitter.exe
UninstallFilesDir={app}\uninstall
CloseApplications=yes
CloseApplicationsFilter=drama-splitter.exe

OutputDir=..\dist
OutputBaseFilename=drama-splitter-1.0.0-setup
SetupIconFile=..\assets\icon.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
ArchitecturesInstallIn64BitMode=x64compatible
DisableDirPage=no
MinVersion=10.0

[Languages]
; 用仓库自带的精简翻译：Inno Setup 6.7.3 的默认发行不含简体中文
; （属「额外翻译」，需另行下载，而 GitHub raw 在国内经常拉不动）。
; 自带一份好处是离线可复现——打包安装程序不必先联网。
Name: "chinesesimplified"; MessagesFile: "i18n\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; onedir 打包产物（204 个文件）。ignoreversion：PyInstaller 每次构建时间戳都变，
; 否则同一版本重装会被误判为「更新」而覆盖正在使用的目录。
Source: "..\dist\drama-splitter\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\AI 短剧智能分集器"; Filename: "{app}\drama-splitter.exe"
Name: "{group}\{cm:UninstallProgram,AI 短剧智能分集器}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\AI 短剧智能分集器"; Filename: "{app}\drama-splitter.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\drama-splitter.exe"; Description: "{cm:LaunchProgram,AI 短剧智能分集器}"; Flags: nowait postinstall skipifsilent

[Code]
// 安装后检测 FFmpeg。
//
// 判据是「用户装完能不能直接干活」，不是「程序能不能启动」：程序缺 FFmpeg 照样
// 能开窗口（启动时只弹一次警告），但导入、导出、镜头检测全部不可用。所以要在
// 安装向导里就把这件事说清楚，而不是让人等到第一次导入失败。

const
  FFmpegHint =
    '未检测到 FFmpeg。' + #13#10 + #13#10 +
    '导入、导出与镜头检测都依赖它，缺少时相关功能不可用。' + #13#10 + #13#10 +
    '两种方式任选其一：' + #13#10 +
    '  1. 装好后把 FFmpeg 的 bin 目录加入系统 PATH；' + #13#10 +
    '  2. 或设环境变量 DRAMA_FFMPEG_DIR 指向其 bin 目录。' + #13#10 + #13#10 +
    '下载地址：https://www.gyan.dev/ffmpeg/builds/' + #13#10 + #13#10 +
    '本程序需要 ffmpeg.exe 与 ffprobe.exe（ffplay 可选，仅用于预览播放出声）。';

const
  ModelHint =
    '模型不随安装包分发（体积过大）。' + #13#10 +
    '首次使用请在「模型设置」界面里直接下载，无需另行配置。';

// 注意：Inno 的 Pascal Script 用自己的 TFindRec（不是 Delphi 的 TSearchRec），
// 且不需要 FindClose——脚本引擎自行收尾。
function FileInDir(const Dir, Name: String): Boolean;
var
  FindRec: TFindRec;
begin
  Result := False;
  if Dir = '' then
    Exit;
  Result := FindFirst(Dir + '\' + Name, FindRec);
end;

// 手写 PATH 拆分：Inno 的 Pascal Script 没有现成的字符串分割函数。
// 逐字符扫，遇到 ';' 收一段。
function PATHHasFFmpeg(const DirList: String): Boolean;
var
  I, Start: Integer;
  Dir: String;
begin
  Result := False;
  Start := 1;
  for I := 1 to Length(DirList) + 1 do
  begin
    if (I > Length(DirList)) or (DirList[I] = ';') then
    begin
      Dir := Trim(Copy(DirList, Start, I - Start));
      if (Dir <> '') and FileInDir(Dir, 'ffmpeg.exe') then
      begin
        Result := True;
        Exit;
      end;
      Start := I + 1;
    end;
  end;
end;

function FFmpegAvailable: Boolean;
begin
  // 优先级与 app/core/ffmpeg.py 的探测顺序一致：先 exe 同级，再 PATH。
  Result := FileInDir(ExpandConstant('{app}'), 'ffmpeg.exe')
            or PATHHasFFmpeg(GetEnv('PATH'));
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    // 静默安装时**必须**跳过提示框：/VERYSILENT 下 Inno 不会自动关掉 [Code]
    // 里的 MsgBox，装完文件就会永久卡在这里。自动化部署（CI、企业批量装）
    // 走的正是静默模式。ssPostInstall 只在安装阶段触发，卸载不会走到这里。
    if WizardSilent then
      Exit;
    if FFmpegAvailable then
    begin
      // 只在缺模型时提一句：模型能自己下，不算阻塞。
      MsgBox('关于模型' + #13#10 + #13#10 + ModelHint, mbInformation, MB_OK);
    end
    else
    begin
      MsgBox('需要 FFmpeg' + #13#10 + #13#10 + FFmpegHint + #13#10 + #13#10 + ModelHint,
             mbInformation, MB_OK);
    end;
  end;
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  // 产物不在就别让人装出一个空壳：报明确原因并中止。
  if not FileExists(ExpandConstant('{src}\..\dist\drama-splitter\drama-splitter.exe')) then
  begin
    MsgBox('未找到打包产物 dist\drama-splitter\。' + #13#10 + #13#10 +
           '请先运行：python tools\build_package.py', mbError, MB_OK);
    Result := False;
  end;
end;

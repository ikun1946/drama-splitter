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
AppVersion=1.0.30
AppVerName=AI 短剧智能分集器 1.0.30
AppPublisher=ikun1946
AppPublisherURL=https://github.com/ikun1946/drama-splitter
AppSupportURL=https://github.com/ikun1946/drama-splitter/issues
AppUpdatesURL=https://github.com/ikun1946/drama-splitter/releases
VersionInfoVersion=1.0.30.0
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
OutputBaseFilename=drama-splitter-1.0.30-setup
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

[InstallDelete]
; 覆盖升级时清掉旧运行库，防止新旧 Qt/Shiboken DLL 混用。
;
; v1.0.29 从 PySide6 6.12 切到 6.8.3 时，安装器只覆盖同名新文件：
; 6.12 的 _internal\shiboken6\shiboken6.abi3.dll 不在新清单里，因此留了下来。
; Windows 加载器会先找到这份旧 DLL，再与新 Shiboken.pyd 搭配，启动时报
; “DLL load failed while importing Shiboken: 找不到指定的程序”。
;
; _internal 只放随包程序与运行库；config.json / models / cache / deps 都在
; 它的上一级，删除这里不会碰用户数据。
Type: filesandordirs; Name: "{app}\_internal"

[Files]
; onedir 打包产物（204 个文件）。ignoreversion：PyInstaller 每次构建时间戳都变，
; 否则同一版本重装会被误判为「更新」而覆盖正在使用的目录。
Source: "..\dist\drama-splitter\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\AI 短剧智能分集器"; Filename: "{app}\drama-splitter.exe"
Name: "{group}\{cm:UninstallProgram,AI 短剧智能分集器}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\AI 短剧智能分集器"; Filename: "{app}\drama-splitter.exe"; Tasks: desktopicon

[Run]
; 交互安装：勾选后启动新版。skipifsilent = 静默安装不弹界面（托管部署不该被
; 突然拉出一个窗口）。
Filename: "{app}\drama-splitter.exe"; Description: "{cm:LaunchProgram,AI 短剧智能分集器}"; Flags: nowait postinstall skipifsilent
; 应用内更新：更新器用 /RESTARTAPP=1 启动安装器，这一条负责把新版拉起来。
; 少了它，用户看到的就是「窗口突然关掉，然后什么都没有」——更新等于没做完。
Filename: "{app}\drama-splitter.exe"; Flags: nowait; Check: RelaunchRequested

[Code]
// 应用内更新的收尾：更新器用 /VERYSILENT /RESTARTAPP=1 启动安装器。
//
// 为什么单独来一条 [Run]：交互安装那条带 skipifsilent（静默安装不弹界面），
// 于是应用内更新装完什么都不会启动，用户看到的就是「窗口突然关掉，然后什么都
// 没有」。这里只在更新器显式传了 /RESTARTAPP=1 时重启，托管静默部署不受影响。
function RelaunchRequested(): Boolean;
begin
  Result := ExpandConstant('{param:RESTARTAPP|0}') = '1';
end;

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

// ===========================================================================
// 卸载时的可选清理
// ===========================================================================
//
// 为什么需要它：安装程序只登记了随包分发的文件，卸载器照单删除，但程序运行时
// 自己在 exe 同级生成的三样东西不在那份清单里——config.json、cache/、models/。
// 不显式处理的话，卸载完安装目录会留着一个空壳（目录非空，连 {app} 都删不掉），
// 模型动辄几个 GB 更是白占空间。
//
// 三样东西的价值差别很大，所以**默认状态不同**，不是一刀切：
//   cache/      分析缓存，纯可重建 → 默认勾选（删了没有任何损失）
//   models/     下载的模型，动辄数 GB 且受限于网速 → 默认不勾（删了要重下）
//   config.json 设置与云端 API Key       → 默认不勾（删了要重新配置）
// 破坏性操作不给"默认全选"，宁可让用户少清理一次，也不要让人误删几个 GB。
//
// 静默卸载（/VERYSILENT，自动化部署走这条）一律**不删**任何数据——无人值守时
// 不该销毁数据。要清理得用命令行开关显式声明：
//     unins000.exe /VERYSILENT /DELCACHE /DELMODELS /DELCONFIG
// 开关与界面复选框走同一组变量，两条路径的行为不会分叉。

var
  DelModels: Boolean;
  DelCache: Boolean;
  DelConfig: Boolean;

// 递归求目录字节数。Inno 的 TFindRec 没有 Size 字段，只有 SizeLow/SizeHigh
// 两个 32 位分量，必须自己拼回 64 位——单个模型文件超过 4 GB 是常态，
// 只用 SizeLow 会把体积报成错的（甚至"负"的）。
function DirSizeBytes(const Dir: String): Int64;
var
  FindRec: TFindRec;
  Sub: String;
begin
  Result := 0;
  if not FindFirst(Dir + '\*', FindRec) then
    Exit;
  try
    repeat
      if (FindRec.Name <> '.') and (FindRec.Name <> '..') then
      begin
        Sub := Dir + '\' + FindRec.Name;
        if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
          Result := Result + DirSizeBytes(Sub)
        else
          Result := Result + (Int64(FindRec.SizeHigh) * 4294967296) + FindRec.SizeLow;
      end;
    until not FindNext(FindRec);
  finally
    FindClose(FindRec);
  end;
end;

function FormatBytes(Value: Int64): String;
begin
  if Value >= 1073741824 then
    Result := Format('%.1f GB', [Value / 1073741824.0])
  else if Value >= 1048576 then
    Result := Format('%.0f MB', [Value / 1048576.0])
  else if Value >= 1024 then
    Result := Format('%.0f KB', [Value / 1024.0])
  else
    Result := IntToStr(Value) + ' B';
end;

// Inno 没有「命令行里有没有某个开关」的现成函数（CmdLineParamExists 在 6.7
// 里并不存在），只有 GetCmdTail 返回整条命令行尾部，所以这里自己扫一遍。
// 大小写不敏感，与 Windows 命令行惯例一致。
function CmdLineHasSwitch(const Name: String): Boolean;
var
  Tail, UpperName: String;
begin
  Tail := UpperCase(GetCmdTail);
  UpperName := UpperCase(Name);
  Result := Pos(UpperName, Tail) > 0;
end;

function DataCachePath: String;
begin
  Result := ExpandConstant('{app}') + '\cache';
end;

function DataModelsPath: String;
begin
  Result := ExpandConstant('{app}') + '\models';
end;

function DataConfigPath: String;
begin
  Result := ExpandConstant('{app}') + '\config.json';
end;

// 三样里有没有哪样真的存在。都没有就别弹窗打扰用户。
function HasDeletableData: Boolean;
begin
  Result := DirExists(DataCachePath)
            or DirExists(DataModelsPath)
            or FileExists(DataConfigPath);
end;

// 交互式卸载的清理选项框。点「取消」返回 False，由调用方中止整个卸载——
// 半途退出卸载会让人以为"卸载了其实没卸"，不如整件事重来。
function AskWhatToDelete(): Boolean;
var
  Form: TSetupForm;
  Intro, Note, Verdict: TNewStaticText;
  CBModels, CBCache, CBConfig: TNewCheckBox;
  DeleteButton, CancelButton: TNewButton;
  Y: Integer;
begin
  Result := True;
  Form := CreateCustomForm(ScaleX(530), ScaleY(330), False, False);
  try
    Form.Caption := '卸载 AI 短剧智能分集器';

    Intro := TNewStaticText.Create(Form);
    Intro.Parent := Form;
    Intro.Left := ScaleX(18);
    Intro.Top := ScaleY(16);
    Intro.Caption := '程序即将卸载。以下数据由程序在运行中生成，是否一并删除？';

    Y := ScaleY(48);

    CBCache := TNewCheckBox.Create(Form);
    CBCache.Parent := Form;
    CBCache.Left := ScaleX(18);
    CBCache.Top := Y;
    CBCache.Width := Form.ClientWidth - ScaleX(36);
    CBCache.Caption := '分析缓存（' + FormatBytes(DirSizeBytes(DataCachePath))
                       + '）——删了下次重新分析即可重建';
    CBCache.Checked := True;   // 可重建，默认删

    Y := Y + ScaleY(46);

    CBModels := TNewCheckBox.Create(Form);
    CBModels.Parent := Form;
    CBModels.Left := ScaleX(18);
    CBModels.Top := Y;
    CBModels.Width := Form.ClientWidth - ScaleX(36);
    CBModels.Caption := '下载的模型（' + FormatBytes(DirSizeBytes(DataModelsPath))
                        + '）——默认保留，删了要重新下载';
    CBModels.Checked := False;

    Y := Y + ScaleY(46);

    CBConfig := TNewCheckBox.Create(Form);
    CBConfig.Parent := Form;
    CBConfig.Left := ScaleX(18);
    CBConfig.Top := Y;
    CBConfig.Width := Form.ClientWidth - ScaleX(36);
    CBConfig.Caption := '应用配置 config.json（含界面设置与云端 API Key）——默认保留';
    CBConfig.Checked := False;

    Y := Y + ScaleY(52);

    Note := TNewStaticText.Create(Form);
    Note.Parent := Form;
    Note.Left := ScaleX(18);
    Note.Top := Y;
    Note.Width := Form.ClientWidth - ScaleX(36);
    Note.WordWrap := True;
    Note.AutoSize := False;
    Note.Height := ScaleY(48);
    Note.Caption := '项目文件（*.drama-project）与导出的视频都保存在你自己选择的位置，'
                    + '不在安装目录内，卸载不会删除它们。'
                    + '模型若被改到安装目录之外，也不会被删除。';

    Verdict := TNewStaticText.Create(Form);
    Verdict.Parent := Form;
    Verdict.Left := ScaleX(18);
    Verdict.Top := Form.ClientHeight - ScaleY(54);
    Verdict.Caption := '勾选的数据删除后不可恢复。';

    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.Caption := '取消';
    CancelButton.ModalResult := mrCancel;
    CancelButton.Cancel := True;
    CancelButton.Width := ScaleX(88);
    CancelButton.Height := ScaleY(26);
    CancelButton.Left := Form.ClientWidth - ScaleX(18 + 88);
    CancelButton.Top := Form.ClientHeight - ScaleY(26 + 16);

    DeleteButton := TNewButton.Create(Form);
    DeleteButton.Parent := Form;
    DeleteButton.Caption := '继续卸载';
    DeleteButton.ModalResult := mrOk;
    DeleteButton.Default := True;
    DeleteButton.Width := ScaleX(88);
    DeleteButton.Height := ScaleY(26);
    DeleteButton.Left := CancelButton.Left - ScaleX(88 + 8);
    DeleteButton.Top := CancelButton.Top;

    Form.ActiveControl := DeleteButton;

    if Form.ShowModal() <> mrOk then
    begin
      Result := False;
      Exit;
    end;

    DelCache := CBCache.Checked;
    DelModels := CBModels.Checked;
    DelConfig := CBConfig.Checked;
  finally
    Form.Free();
  end;
end;

// 卸载开始时决定删什么。静默模式只看命令行开关（默认全不删）；交互模式弹选项框。
function InitializeUninstall(): Boolean;
begin
  DelCache := False;
  DelModels := False;
  DelConfig := False;
  Result := True;

  if UninstallSilent() then
  begin
    // 无人值守不删数据。要清理得显式给开关。
    DelCache := CmdLineHasSwitch('/DELCACHE');
    DelModels := CmdLineHasSwitch('/DELMODELS');
    DelConfig := CmdLineHasSwitch('/DELCONFIG');
    Exit;
  end;

  if not HasDeletableData() then
    Exit;   // 没有可清理的东西，不打扰

  Result := AskWhatToDelete();
end;

// 在 usUninstall（文件删除**之前**）动手：这样被清掉的目录不再占用 {app}，
// 卸载器随后能把随包文件删净、并把空掉的 {app} 一并移除。放在 usPostUninstall
// 会晚一步——那时 {app} 已因非空被判为"留着"，反而留下一个空目录。
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    if DelCache and DirExists(DataCachePath) then
      DelTree(DataCachePath, True, True, True);
    if DelModels and DirExists(DataModelsPath) then
      DelTree(DataModelsPath, True, True, True);
    if DelConfig and FileExists(DataConfigPath) then
      DeleteFile(DataConfigPath);
  end;
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

    // 先确认主程序确实落地了——这才是运行期**能**验、也该验的事。
    //
    // 不要试图在 InitializeSetup 里检查源码目录里的 dist\：安装包的全部文件
    // 来自 [Files] 段，路径在编译期就解析好并打进 setup.exe 内部了，运行时是
    // 自包含的。{src} 指的是"正在运行的安装程序自己所在的目录"，它随用户把
    // setup.exe 放在哪里而变——用户从 Downloads 双击时，{src}\..\dist\... 必然
    // 不存在，于是安装被误判为"缺产物"而中止。编译期该做的检查由
    // tools/build_installer.py 负责（跑 ISCC 之前就查）。
    if not FileExists(ExpandConstant('{app}\drama-splitter.exe')) then
    begin
      MsgBox('安装似乎未完成：未找到主程序 drama-splitter.exe。' + #13#10 + #13#10 +
             '请重新运行安装程序；若反复出现，请到项目主页反馈。',
             mbError, MB_OK);
      Exit;
    end;

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

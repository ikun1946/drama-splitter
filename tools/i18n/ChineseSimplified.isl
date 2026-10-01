; AI 短剧智能分集器 —— 安装向导简体中文
;
; 为什么自己写而不是下载官方翻译
; --------------------------
; Inno Setup 6.7.3 的默认发行包里**不含**简体中文（它属于「额外翻译」，需另外下；
; jrsoftware.org 的下载链接已 404，GitHub raw 在国内又常拉不动）。装一个中文程序
; 却给英文安装向导，第一印象就废了，所以这里自带一份。
;
; 三个必须知道的坑（都踩过）
; ------------------------
; 1) **占位符是位置式的 %1**，不是 printf 的 %s。写成 %s 会让开始菜单里出现一个
;    名字就叫「卸载%s」的快捷方式。
; 2) **键名必须与 Default.isl 完全一致**：按钮是 ButtonNext / ButtonCancel /
;    ButtonInstall / ButtonFinish / ButtonOK / ButtonYes / ButtonNo，
;    不是 NextButton / CancelButton。Inno 对**多余**的键不报警告——写错了会
;    静默失效，安装向导仍然是全英文按钮。
; 3) 未定义的键会自动回退英文，所以本文件可以是「部分翻译」，不会编译失败。
;    但回退的英文和中文混排很难看，改 installer.iss 的 Disable*Page 之后记得
;    回来补对应条目（改完用 ISCC 编译，看有没有 "has not been defined" 警告）。
;
; 编码：UTF-8，故 LanguageCodePage=0。

[LangOptions]
LanguageName=简体中文
LanguageID=$0804
LanguageCodePage=0

[Messages]

; *** 应用标题
; 注意标题类键用 %1（应用名参数），**不是 [name]**——写成 [name] 会在标题栏
; 原样显示 "{name} 安装程序"，不会替换。Default.isl 的模板同样是 Setup - %1。
SetupAppTitle=%1 安装程序
SetupWindowTitle=%1 安装程序
UninstallAppTitle=%1 卸载
UninstallAppFullTitle=%1 卸载

; *** 向导页面标题（modern 样式显示在页面顶部）
WizardSelectDir=选择安装位置
WizardReady=准备安装
WizardPreparing=正在准备
WizardInstalling=正在安装
WizardInfoBefore=安装前
WizardInfoAfter=安装后
WizardSelectTasks=选择附加任务
WizardSelectComponents=选择组件
WizardSelectProgramGroup=选择开始菜单文件夹
WizardLicense=许可协议
WizardPassword=密码
YesRadio=是(&Y)
NoRadio=否(&N)
WizardUninstalling=正在卸载
WizardUserInfo=安装信息

; *** 按钮（键名必须与 Default.isl 一致）
ButtonBack=< 上一步
ButtonNext=下一步(&N) >
ButtonCancel=取消
ButtonInstall=安装(&I)
ButtonFinish=完成(&F)
ButtonBrowse=浏览(&B)...
ButtonOK=确定
ButtonYes=是(&Y)
ButtonNo=否(&N)
ButtonYesToAll=全部是(&A)
ButtonNoToAll=全部否(&A)
ButtonNewFolder=新建文件夹(&F)...
ButtonWizardBrowse=浏览...

; *** 欢迎页
WelcomeLabel1=欢迎安装 [name]
WelcomeLabel2=即将把 [name/ver] 安装到这台电脑上。%n%n建议先关闭其他正在运行的程序。%n%n本程序按当前用户安装，无需管理员权限。配置、缓存与模型会存放在安装目录内。
ClickNext=点「下一步」继续，点「取消」退出安装程序。

; *** 许可协议页（本项目未附带许可文件，实际不会出现，留着以防将来加）
LicenseLabel=请先阅读以下重要信息再继续。
LicenseLabel3=若不接受协议，请点击「取消」退出安装程序。
LicenseAccepted=我接受协议中的条款(&A)
LicenseNotAccepted=我不接受协议中的条款(&A)

; *** 选择安装目录页
SelectDirDesc=[name] 要安装到哪个文件夹？
SelectDirLabel3=将把 [name] 安装到下面选定的文件夹里。
SelectDirBrowseLabel=要继续安装，请点「下一步」。想换个文件夹，请点「浏览」。
DirDoesntExistTitle=文件夹不存在
DirDoesntExist=文件夹 %n%n%1 %n%n不存在。要创建它吗？
InvalidDirName=这个文件夹名无效。
DirNameTooLong=这个文件夹名太长。
CannotInstallToNetworkDrive=不能安装到网络驱动器。
CannotInstallToUNCPath=不能安装到 UNC 路径。
BrowseDialogLabel=选择要安装 [name] 的文件夹
BrowseDialogTitle=浏览文件夹
NewFolderName=新建文件夹
InvalidPath=这个路径无效。

; *** 选择附加任务页（本向导只提供「创建桌面快捷方式」一项）
SelectTasksDesc=要执行哪些附加任务？
SelectTasksLabel2=选择安装程序在安装 [name] 时要执行的附加任务，然后点「下一步」。

; *** 准备安装页
ReadyLabel1=[name] 现在可以安装到这台电脑上了。
ReadyMemoDir=安装位置：
ReadyMemoGroup=开始菜单文件夹：
ReadyMemoTasks=附加任务：
ReadyMemoUserInfo=用户信息：
DiskSpaceMBLabel=至少需要 [mb] MB 可用磁盘空间。
DiskSpaceGBLabel=至少需要 [gb] GB 可用磁盘空间。
CompactInstallation=安装程序会尽量把文件压缩，以节省磁盘空间。

; *** 安装过程
ExtractingLabel=正在解压文件...
StatusExtractFiles=正在解压文件...
StatusCreateDirs=正在创建文件夹...
StatusCreateIcons=正在创建快捷方式...
StatusCreateIniEntries=正在写入设置...
StatusCreateRegistryEntries=正在写入注册表项...
StatusSavingUninstall=正在保存卸载信息...
StatusRunProgram=正在运行安装后的程序...
ClickFinish=点「完成」关闭这个向导。

; *** 安装完成页
FinishedHeadingLabel=[name] 安装完成
FinishedLabel=[name] 已经装到下面这个文件夹了：%n%n%1%n%n可以通过已创建的快捷方式启动它。
FinishedLabelNoIcons=[name] 已经装到下面这个文件夹了：%n%n%1
FinishedRestartLabel=要完成 [name] 的安装，必须重启计算机。现在重启吗？

; *** 卸载过程
UninstallStatusLabel=状态：
StatusUninstalling=正在卸载...
StatusRollback=正在回滚更改...

; *** 卸载确认
ConfirmUninstall=你确定要卸载 [name] 吗？%n%n%1
UninstallNotFound=未找到 [name] 的卸载信息。要继续吗？
UninstallUnsupportedVer=已安装的 [name] 版本不支持卸载。
UninstallDataCorrupted=卸载信息已损坏，是否仍要卸载 [name]？
UninstalledAll=[name] 已卸载成功。
UninstalledMost=[name] 已卸载，但仍有一些项目留在原处。
UninstalledAndNeedsRestart=[name] 已卸载完成，但需要重启计算机才能完成。
OnlyAdminCanUninstall=只有管理员才能卸载 [name]。
NoUninstallWarningTitle=没有找到卸载程序
NoUninstallWarning=[name] 的卸载信息缺失，可能无法正常卸载。要继续吗？

; *** 错误对话框
ErrorTitle=错误
CannotContinue=安装无法继续。请点「取消」退出。
ErrorFileSize=文件大小不对：%n%n%1%n%n请重新下载安装程序。
ErrorCreatingDir=安装程序无法创建文件夹：%n%n%1
ErrorCreatingTemp=安装程序无法创建临时文件夹：%n%n%1
ErrorReadingSource=安装程序无法读取源文件：%n%n%1
ErrorReadingExistingDest=安装程序无法读取目标位置的现有文件：%n%n%1
ErrorReplacingExistingFile=安装程序无法覆盖文件：%n%n%1%n%n请关闭可能正在使用它的程序。
ErrorTooManyFilesInDir=目标文件夹 %1 里的文件过多，无法完成安装。
ErrorExtracting=安装程序遇到错误，无法解压文件。
ErrorCopying=安装程序遇到错误，无法复制文件：%n%n%1
ErrorRegCreateKey=安装程序无法创建注册表项：%n%n%1
ErrorRegOpenKey=安装程序无法打开注册表项：%n%n%1
ErrorIniEntry=安装程序无法写入设置文件：%n%n%1
ErrorRestartingComputer=安装程序无法重启计算机。
ErrorCloseApplications=安装程序无法关闭某个应用。请关闭后重试。
ErrorInternal2=安装程序遇到内部错误：%n%n%1
LastErrorMessage=安装过程中出现未知错误：%n%n%1
SetupAppRunningError=[name] 正在运行，安装无法继续。%n%n请先关掉它再重试本安装程序。
UninstallAppRunningError=[name] 正在运行，卸载无法继续。%n%n请先关掉它再重试卸载。
UninstallOpenError=安装程序无法启动卸载程序：%n%n%1
SetupFileMissing=安装程序找不到文件 %1。请重新下载后运行。
SetupFileCorrupt=安装文件已损坏。请重新下载后运行。
SetupFileCorruptOrWrongVer=安装文件已损坏或版本不对。请重新下载后运行。
InvalidParameter=安装参数无效：%n%n%1
LdrCannotExecTemp=安装程序无法解压到临时文件夹。

; *** 警告与信息
InformationTitle=信息
StatusClosingApplications=正在关闭可以关闭的应用...
CloseApplications=关闭可以关闭的应用
DontCloseApplications=不关闭应用
ApplicationsFound=以下应用仍在运行，需要关闭后才能继续：%n%n%1
ApplicationsFound2=以下应用仍在运行，需要关闭后才能继续：%n%n%1
PrepareToInstallNeedsRestart=[name] 需要重启计算机才能完成安装。现在重启吗？
PreviousInstallNotCompleted=上一次的安装没有完成。建议先重启计算机，然后再运行本安装程序。%n%n要重启后继续吗？
SetupAborted=安装已中止，没有做任何更改。%n%n要退出吗？
OnlyOnThisPlatform=本程序只能在 Windows 上安装。
OnlyOnTheseArchitectures=本程序只能安装到 %1 架构上。
NotOnThisPlatform=本程序不能在当前系统上运行。
WindowsVersionNotSupported=当前 Windows 版本太旧，无法安装本程序。
WinVersionTooLowError=需要 Windows 10 或更高版本才能安装本程序。%n%n当前系统版本过低，请升级后再试。
WinVersionTooHighError=本程序不支持当前这个过新的 Windows 版本。
InvalidDrive=找不到 %1 盘：。
WindowsServicePackRequired=需要安装 %1 Service Pack。
UserInfoName=用户名：
UserInfoOrg=组织：
UserInfoSerial=序列号：
UserInfoNameRequired=必须填写用户名。
UserInfoDesc=请填写本程序的信息。%n%n只有本程序的用户信息页用到这些内容。
AboutSetupTitle=关于本安装程序
AboutSetupMessage=关于 %1 安装程序
AboutSetupNote=本安装程序由 %2 用 Inno Setup 制作。

; *** 组菜单项
AboutSetupMenuItem=关于安装程序(&A)

[CustomMessages]

; 位置占位符是 %1，不是 %s——用 %s 会生成字面量「卸载%s」的快捷方式。
AdditionalIcons=附加快捷方式：
CreateDesktopIcon=创建桌面快捷方式(&D)
CreateQuickLaunchIcon=创建快速启动栏快捷方式(&Q)
CreateStartMenuShortcut=创建开始菜单快捷方式(&S)
LaunchProgram=启动%1
UninstallProgram=卸载%1
SelectLanguage=选择语言
SelectDir=选择安装位置
FinishedHeaderLabel=安装完成
AdditionalSetupFilesTitle=附加安装文件
AddRemoveAdditionalFiles=选择要安装的附加文件。
SelectSetupLanguage=选择安装程序语言
PreparingToInstall=正在准备安装
CannotCheckForUpdates=无法检查更新
CheckingForUpdates=正在检查更新
ModifySetup=修改
Modify=修改安装
Repair=修复
Uninstall=卸载
ChangeInstall=修改
Reinstall=重新安装
MenuExtras=附加
MenuInstall=安装
MenuWelcome=欢迎
MenuFinish=完成
MenuLicense=许可协议
MenuInformation=信息
MenuReady=准备
MenuUninstallConfirm=确认卸载
MenuUninstallReady=准备卸载
MenuUninstallComplete=卸载完成
ClickFinish=点「完成」关闭这个向导。

; Reproducible Inno Setup definition for the Windows JoyRead onedir release.
; ``scripts/build_windows_inno.py`` supplies the source tree, project root,
; and version. Do not replace these with a developer-specific absolute path.

#ifndef MyProjectRoot
  #error MyProjectRoot must be passed by scripts/build_windows_inno.py.
#endif
#ifndef MyAppSourceDir
  #error MyAppSourceDir must be passed by scripts/build_windows_inno.py.
#endif
#ifndef MyAppVersion
  #error MyAppVersion must be passed by scripts/build_windows_inno.py.
#endif
#ifndef MyAppFileVersion
  #error MyAppFileVersion must be passed by scripts/build_windows_inno.py.
#endif

#define MyAppName "JoyRead"
#define MyAppPublisher "CtfTnk"
#define MyAppURL "https://github.com/CtfTnk/JoyRead"
#define MyAppExeName "JoyRead.exe"
#define MyFileType "JoyRead.Document"
#define MyFileTypeName "JoyRead Document"
#define MyExplicitOpenWithTasks "openwith_cbz or openwith_cbr or openwith_cb7 or openwith_pdf"
#define DoubleAmp(Value) StringChange(Value, "&", "&&")

[Setup]
; This is the Wizard-generated product identity. Keep it stable so Inno Setup
; recognizes upgrades and removes the matching installation on uninstall.
AppId={{4D820D9A-7542-4C7A-A0C0-AE02FDA0AF61}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
AppCopyright=Copyright (C) 2026 JoyRead contributors. Licensed under GPL-3.0.
DefaultDirName={autopf}\{#MyAppName}
DisableProgramGroupPage=yes
LicenseFile={#MyProjectRoot}\LICENSE
SetupIconFile={#MyProjectRoot}\src\joyread\ui\resources\icons\JoyRead.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
; The JoyRead runtime is x64. Inno Setup 7's x64 Setup binary also runs under
; Windows 11 Arm's x64 emulation, matching the existing Windows target.
SetupArchitecture=x64
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
ChangesAssociations=yes
VersionInfoVersion={#MyAppFileVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Setup
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppFileVersion}
SolidCompression=yes
WizardStyle=modern dynamic

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[CustomMessages]
english.OpenWithChoices=Add JoyRead as an Open With option for these additional types (ZIP, RAR, and 7z archives are always offered):
chinesesimplified.OpenWithChoices=选择要额外添加 JoyRead 到“打开方式”的格式（ZIP、RAR 和 7z 压缩包始终作为候选项提供）：
japanese.OpenWithChoices=JoyRead を「プログラムから開く」に追加する形式を選択してください（ZIP、RAR、7z は常に候補になります）：
english.OpenWithCbz=CBZ comic books (.cbz)
chinesesimplified.OpenWithCbz=CBZ 漫画文件 (.cbz)
japanese.OpenWithCbz=CBZ 漫画 (.cbz)
english.OpenWithCbr=CBR comic books (.cbr)
chinesesimplified.OpenWithCbr=CBR 漫画文件 (.cbr)
japanese.OpenWithCbr=CBR 漫画 (.cbr)
english.OpenWithCb7=CB7 comic books (.cb7)
chinesesimplified.OpenWithCb7=CB7 漫画文件 (.cb7)
japanese.OpenWithCb7=CB7 漫画 (.cb7)
english.OpenWithPdf=PDF documents (.pdf)
chinesesimplified.OpenWithPdf=PDF 文档 (.pdf)
japanese.OpenWithPdf=PDF 文書 (.pdf)

[Tasks]
; A normal Windows install creates the desktop shortcut unless the user opts out.
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
; Each checkbox is a candidate registration only. None writes an extension's
; default ProgID. Generic archives are always candidates (see [Registry]).
Name: "openwith_cbz"; Description: "{cm:OpenWithCbz}"; GroupDescription: "{cm:OpenWithChoices}"; Flags: unchecked
Name: "openwith_cbr"; Description: "{cm:OpenWithCbr}"; GroupDescription: "{cm:OpenWithChoices}"; Flags: unchecked
Name: "openwith_cb7"; Description: "{cm:OpenWithCb7}"; GroupDescription: "{cm:OpenWithChoices}"; Flags: unchecked
Name: "openwith_pdf"; Description: "{cm:OpenWithPdf}"; GroupDescription: "{cm:OpenWithChoices}"; Flags: unchecked

[Files]
; JoyRead is an onedir application. Copy the complete tree: the EXE alone
; cannot load Python, Qt, native extensions, resources, or the bundled 7-Zip.
Source: "{#MyAppSourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Keep the shell document icon beside the EXE instead of inside a backend-
; specific resource tree. This keeps the DefaultIcon path independent from
; PyInstaller's internal onedir layout.
Source: "{#MyProjectRoot}\src\joyread\ui\resources\icons\JoyReadDocument.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; HKA resolves to HKLM for this per-machine install. The Applications class
; gives Windows a stable, named candidate instead of forcing the user to browse
; to Program Files. No entry below sets an extension's default value.
Root: HKA; Subkey: "Software\Classes\{#MyFileType}"; ValueType: string; ValueData: "{#MyFileTypeName}"; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKA; Subkey: "Software\Classes\{#MyFileType}\DefaultIcon"; ValueType: string; ValueData: "{app}\JoyReadDocument.ico"; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKA; Subkey: "Software\Classes\{#MyFileType}\shell"; Flags: uninsdeletekeyifempty
Root: HKA; Subkey: "Software\Classes\{#MyFileType}\shell\open"; Flags: uninsdeletekeyifempty
Root: HKA; Subkey: "Software\Classes\{#MyFileType}\shell\open\command"; ValueType: string; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Flags: uninsdeletevalue uninsdeletekeyifempty

; This app identity and command are always installed. Generic archive types
; below are intentional Open With alternatives, never default associations.
Root: HKA; Subkey: "Software\Classes\Applications\{#MyAppExeName}"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "{#MyAppName}"; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKA; Subkey: "Software\Classes\Applications\{#MyAppExeName}\shell"; Flags: uninsdeletekeyifempty
Root: HKA; Subkey: "Software\Classes\Applications\{#MyAppExeName}\shell\open"; Flags: uninsdeletekeyifempty
Root: HKA; Subkey: "Software\Classes\Applications\{#MyAppExeName}\shell\open\command"; ValueType: string; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKA; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; ValueType: string; ValueName: ".zip"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKA; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; ValueType: string; ValueName: ".rar"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKA; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; ValueType: string; ValueName: ".7z"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKA; Subkey: "Software\Classes\.zip\OpenWithProgids"; ValueType: string; ValueName: "{#MyFileType}"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKA; Subkey: "Software\Classes\.rar\OpenWithProgids"; ValueType: string; ValueName: "{#MyFileType}"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKA; Subkey: "Software\Classes\.7z\OpenWithProgids"; ValueType: string; ValueName: "{#MyFileType}"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty

; Explicit checkboxes control the less-generic reader types. Registering each
; one in both SupportedTypes and OpenWithProgids makes it visible in Explorer's
; chooser without replacing the user's existing default handler.
Root: HKA; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; ValueType: string; ValueName: ".cbz"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty; Tasks: openwith_cbz
Root: HKA; Subkey: "Software\Classes\.cbz\OpenWithProgids"; ValueType: string; ValueName: "{#MyFileType}"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty; Tasks: openwith_cbz
Root: HKA; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; ValueType: string; ValueName: ".cbr"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty; Tasks: openwith_cbr
Root: HKA; Subkey: "Software\Classes\.cbr\OpenWithProgids"; ValueType: string; ValueName: "{#MyFileType}"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty; Tasks: openwith_cbr
Root: HKA; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; ValueType: string; ValueName: ".cb7"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty; Tasks: openwith_cb7
Root: HKA; Subkey: "Software\Classes\.cb7\OpenWithProgids"; ValueType: string; ValueName: "{#MyFileType}"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty; Tasks: openwith_cb7
Root: HKA; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; ValueType: string; ValueName: ".pdf"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty; Tasks: openwith_pdf
Root: HKA; Subkey: "Software\Classes\.pdf\OpenWithProgids"; ValueType: string; ValueName: "{#MyFileType}"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty; Tasks: openwith_pdf

; The Default Apps surface needs Capabilities/RegisteredApplications. Keep
; generic archives out of it; they are Open With alternatives only. The task
; expression keeps this surface absent when the user chose no extra format.
Root: HKA; Subkey: "Software\JoyRead"; Flags: uninsdeletekeyifempty; Tasks: {#MyExplicitOpenWithTasks}
Root: HKA; Subkey: "Software\JoyRead\Capabilities"; ValueType: string; ValueName: "ApplicationName"; ValueData: "{#MyAppName}"; Flags: uninsdeletevalue uninsdeletekeyifempty; Tasks: {#MyExplicitOpenWithTasks}
Root: HKA; Subkey: "Software\JoyRead\Capabilities"; ValueType: string; ValueName: "ApplicationDescription"; ValueData: "Read local manga archives and PDF documents."; Flags: uninsdeletevalue uninsdeletekeyifempty; Tasks: {#MyExplicitOpenWithTasks}
Root: HKA; Subkey: "Software\JoyRead\Capabilities\FileAssociations"; ValueType: string; ValueName: ".cbz"; ValueData: "{#MyFileType}"; Flags: uninsdeletevalue uninsdeletekeyifempty; Tasks: openwith_cbz
Root: HKA; Subkey: "Software\JoyRead\Capabilities\FileAssociations"; ValueType: string; ValueName: ".cbr"; ValueData: "{#MyFileType}"; Flags: uninsdeletevalue uninsdeletekeyifempty; Tasks: openwith_cbr
Root: HKA; Subkey: "Software\JoyRead\Capabilities\FileAssociations"; ValueType: string; ValueName: ".cb7"; ValueData: "{#MyFileType}"; Flags: uninsdeletevalue uninsdeletekeyifempty; Tasks: openwith_cb7
Root: HKA; Subkey: "Software\JoyRead\Capabilities\FileAssociations"; ValueType: string; ValueName: ".pdf"; ValueData: "{#MyFileType}"; Flags: uninsdeletevalue uninsdeletekeyifempty; Tasks: openwith_pdf
Root: HKA; Subkey: "Software\RegisteredApplications"; ValueType: string; ValueName: "{#MyAppName}"; ValueData: "Software\JoyRead\Capabilities"; Flags: uninsdeletevalue; Tasks: {#MyExplicitOpenWithTasks}

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#DoubleAmp(MyAppName)}}"; Flags: nowait postinstall skipifsilent

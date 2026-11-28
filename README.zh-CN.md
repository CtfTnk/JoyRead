<p align="center"><a href="README.md">English</a> · <strong>简体中文</strong> · <a href="README.ja.md">日本語</a></p>

<p align="center"><img src="src/joyread/ui/resources/icons/JoyRead.png" width="112" alt="JoyRead 软件图标"></p>
<h1 align="center">欣阅 · JoyRead</h1>
<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0--only-blue" alt="License: GPL-3.0-only"></a>
  <a href="https://github.com/CtfTnk/JoyRead/releases/tag/v1.4.1"><img src="https://img.shields.io/badge/release-1.4.1-39846a" alt="Release 1.4.1"></a>
  <a href="https://github.com/CtfTnk/JoyRead/releases/tag/v1.4.1"><img src="https://img.shields.io/badge/macOS-Apple_Silicon-222222?logo=apple&amp;logoColor=white" alt="macOS Apple Silicon"></a>
  <a href="https://github.com/CtfTnk/JoyRead/releases/tag/v1.4.1"><img src="https://img.shields.io/badge/Windows-x64-0078D4" alt="Windows x64"></a>
  <a href="https://github.com/CtfTnk/JoyRead/releases/tag/v1.4.1"><img src="https://img.shields.io/badge/Ubuntu-amd64_%7C_arm64-E95420?logo=ubuntu&amp;logoColor=white" alt="Ubuntu amd64 and arm64"></a>
</p>
<p align="center"><strong>让本地书籍管理更加便捷，也是随时打开、即时阅读的选择。</strong></p>
<p align="center"><a href="https://github.com/CtfTnk/JoyRead/releases/latest">下载</a> · <a href="docs/MANUAL.zh-CN.md">使用手册</a> · <a href="https://github.com/CtfTnk/JoyRead/releases/tag/v1.4.1">1.4.1 更新</a> · <a href="https://github.com/CtfTnk/JoyRead/issues">问题反馈</a></p>

欣阅是一款面向 **macOS、Windows 和 Ubuntu/Linux** 的本地漫画与 PDF 阅读器，也是一座属于你的桌面书库。把喜爱的书籍导入后慢慢整理，或者直接打开文件开始阅读，无需账户或云端服务。

![欣阅中文书库：封面卡片、最近阅读、收藏与隐藏书单](docs/assets/readme/library-zh-CN.png)

## 下载与安装

| 系统 | 1.4.1 安装包 | 安装方式 |
| --- | --- | --- |
| macOS 26+ · Apple Silicon | [DMG](https://github.com/CtfTnk/JoyRead/releases/download/v1.4.1/JoyRead-1.4.1-macos-arm64.dmg) | 打开磁盘映像，将 JoyRead 拖入 Applications。 |
| Windows · x64 | [EXE](https://github.com/CtfTnk/JoyRead/releases/download/v1.4.1/JoyRead-1.4.1-windows-x86_64-setup.exe) | 运行安装程序，按提示安装。 |
| Ubuntu 22.04+ / Linux · amd64 | [DEB](https://github.com/CtfTnk/JoyRead/releases/download/v1.4.1/JoyRead-1.4.1-linux-amd64.deb) | 在终端运行 `sudo apt install ./JoyRead-1.4.1-linux-amd64.deb`。 |
| Ubuntu 22.04+ / Linux · arm64 | [DEB](https://github.com/CtfTnk/JoyRead/releases/download/v1.4.1/JoyRead-1.4.1-linux-arm64.deb) | 在终端运行 `sudo apt install ./JoyRead-1.4.1-linux-arm64.deb`。 |

**Ubuntu 请仅通过命令行安装。** 图形安装器会卡在 **“Preparing”**，此问题尚未修复。请在下载目录打开终端，运行上表中对应安装包的命令。

安装包与校验值见 [Release 页面](https://github.com/CtfTnk/JoyRead/releases/tag/v1.4.1)。macOS 包未经 Apple 公证，Windows 包无受信任的代码签名，首次运行可能出现系统提示；首次启动说明见发布说明。暂不提供 Intel Mac 安装包。

**macOS 升级：** 请先退出 JoyRead，再把新版本拖入「应用程序」，Finder 询问时选择**替换**。选择**保留两者**会留下名为「JoyRead 2」的第二份副本。

欣阅 1.4.1 可以直接使用 **1.4.0** 的书库和设置。

从 **1.3.1 或更早版本**升级前，请先备份书库。升级会将书库迁移到第 15 版数据库架构。若要回退到 1.3.1 或更早版本，请恢复备份。

## 从阅读到整理

- **常见漫画格式与 PDF：** ZIP / CBZ、7z / CB7、RAR / CBR、PDF。EPUB / 轻小说阅读尚未启用。
- **卡片与列表自由切换：** 用封面浏览藏书，或切换到紧凑列表；配合搜索、排序，以及格式、语言与标签筛选快速定位。
- **标签与书单管理：** 按题材、系列或阅读计划整理，结合收藏、最近阅读和进度继续上次的旅程。
- **隐藏空间与隐藏书单：** 将不想在普通书架展示的书籍和书单隐藏。隐藏书籍及其封面在磁盘上加密保存，用密码解锁，并有恢复码作为备用。
- **即时阅读：** 支持拖入文件阅读，也可在系统的“打开方式”中选择 JoyRead，无需先导入书库。系统关联取决于安装与桌面环境。
- **专注阅读：** 单页 / 双页、从右向左或纵向阅读、缩放与适应模式、书签、缩略图导航和阅读进度。
- **三语界面：** 英文、简体中文、日文；新配置默认跟随系统，不支持的语言回退到英文。

## 使用示例：拖进来，选择阅读或导入

把支持的文件拖入书库窗口：放到左侧 **阅读** 区域直接打开，放到右侧 **导入** 区域加入书库。也可以使用左上角操作菜单打开文件、打开并导入，或批量导入文件与文件夹。

![拖放文件时选择直接阅读或导入书库](docs/assets/readme/drag-drop-zh-CN.png)

**直接阅读**适合临时查看文件；默认不复制文件，也不添加书库记录。**导入**会创建由 JoyRead 管理的副本，保留原文件，方便长期管理。若启用了“打开书籍时导入”，打开文件也会尝试导入。不支持带密码的压缩包。

## 使用示例：补全书籍资料

点击书籍的详情按钮，或从更多菜单进入详情页：

| 操作 | 结果 |
| --- | --- |
| 双击书名或作者 | 手动编辑，按 Enter 保存；按 Esc 或移开焦点取消。 |
| 双击语言 | 选择书籍的语言。 |
| 双击封面 | 打开封面编辑器，调整封面；不修改原书籍文件。 |
| 使用标签区的添加按钮 | 为书籍添加标签，方便后续筛选。 |

导入漫画包附带受支持的 **`meta.json` 或 `ComicInfo.xml` 元信息**时，JoyRead 会预处理并填入可识别的书名、作者、标签和语言。结果取决于元信息中实际提供的字段；缺少或无法解析的信息会回退到文件名或默认值，也可以在详情页手动修正。

![书籍详情：书名、作者、语言、封面与页面预览](docs/assets/readme/details-zh-CN.png)

## 使用示例：用标签整理书库

在详情页给书籍加标签，通过书库工具栏按标签筛选；在 **设置 → 标签** 中统一搜索、新建、重命名或删除标签。删除标签不会删除书籍。需要独立分组时，可创建普通书单或使用隐藏书单。

![设置中的中文标签管理](docs/assets/readme/tags-zh-CN.png)

## 使用示例：打开即读

阅读器提供单页和双页布局，支持阅读方向、书签、页面缩略图与进度导航。可以从书库继续阅读，也可以把 JoyRead 作为本地文件的可选打开方式。

将鼠标悬停在进度条上，即可预览对应页面，再决定跳转位置。

![JoyRead 双页阅读器与进度条悬停页面预览](docs/assets/readme/reader.png)

## 隐藏空间与带密码的压缩包

- **隐藏空间会加密隐藏的书籍。** 隐藏书籍及其封面在书库中加密保存，用至少 4 个字符的密码解锁。设置时请保存恢复码：密码和恢复码都丢失后，任何人都无法恢复隐藏的书籍。
- **书库数据库本身不加密。** 隐藏书籍的书名、标签、书单名和阅读进度仍可读取，日志也会记录你打开的书籍名称。详见[隐藏空间](docs/MANUAL.zh-CN.md#隐藏空间)。
- **不支持带密码的压缩包。** 欣阅不会打开或导入带密码的 ZIP、7z、RAR 压缩包，也不会询问密码；请先用其他工具解压。内部嵌套了带密码压缩包的普通压缩包仍可打开，其余页面照常显示。详见[加密压缩包](docs/MANUAL.zh-CN.md#加密压缩包)。

## 开源与开发

JoyRead 采用 **[GNU GPL v3.0 only](LICENSE)**。第三方组件许可见 [THIRD_PARTY_NOTICES.txt](packaging/THIRD_PARTY_NOTICES.txt)。

截图中的漫画为《Give My Regards to Black Jack》，作者 **SHUHO SATO**。截图使用官方电子版，适用[出版方的二次使用条款](https://densho810.com/free/)。

源码运行步骤见英文版的 [Development](README.md#development)，完整控件说明见[使用手册](docs/MANUAL.zh-CN.md)，构建步骤见[打包指南](docs/PACKAGING.md)。

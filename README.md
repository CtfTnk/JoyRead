<p align="center"><strong>English (default)</strong> · 中文与日本語请展开下方选项 / 下の項目を開くと言語を選べます</p>

<details>
<summary><strong>简体中文 · 欣阅</strong></summary>

<p align="center"><img src="src/joyread/ui/resources/icons/JoyRead.png" width="112" alt="JoyRead 软件图标"></p>
<h1 align="center">欣阅 · JoyRead</h1>
<p align="center"><strong>让本地书籍管理更加便捷，也是随时打开、即时阅读的选择。</strong></p>
<p align="center"><a href="https://github.com/CtfTnk/JoyRead/releases/latest">下载</a> · <a href="docs/MANUAL.md">使用手册（英文）</a> · <a href="https://github.com/CtfTnk/JoyRead/releases/tag/v1.0.2">1.0.2 更新</a> · <a href="https://github.com/CtfTnk/JoyRead/issues">问题反馈</a></p>

欣阅是一款面向 **macOS、Windows 和 Ubuntu/Linux** 的本地漫画与 PDF 阅读器，也是一座属于你的桌面书库。把喜爱的书籍导入后慢慢整理，或者直接打开文件开始阅读，无需账户或云端服务。

![欣阅中文书库：封面卡片、最近阅读、收藏与隐藏书单](docs/assets/readme/library-zh-CN.png)

### 下载与安装

| 系统 | 1.0.2 安装包 | 安装方式 |
| --- | --- | --- |
| macOS 13+ · Apple Silicon | [DMG](https://github.com/CtfTnk/JoyRead/releases/download/v1.0.2/JoyRead-1.0.2-macos-arm64.dmg) | 打开磁盘映像，将 JoyRead 拖入 Applications。 |
| Windows · x64 | [EXE](https://github.com/CtfTnk/JoyRead/releases/download/v1.0.2/JoyRead-1.0.2-windows-x86_64-setup.exe) | 运行安装程序，按提示安装。 |
| Ubuntu/Linux · amd64 | [DEB](https://github.com/CtfTnk/JoyRead/releases/download/v1.0.2/JoyRead-1.0.2-linux-amd64.deb) | 使用系统软件安装器，或 `sudo apt install ./JoyRead-1.0.2-linux-amd64.deb`。 |
| Ubuntu/Linux · arm64 | [DEB](https://github.com/CtfTnk/JoyRead/releases/download/v1.0.2/JoyRead-1.0.2-linux-arm64.deb) | 使用系统软件安装器，或 `sudo apt install ./JoyRead-1.0.2-linux-arm64.deb`。 |

安装包与校验值见 [Release 页面](https://github.com/CtfTnk/JoyRead/releases/tag/v1.0.2)。macOS 包未经 Apple 公证，Windows 包无受信任的代码签名，首次运行可能出现系统提示；详情及实机验证状态见发布说明。暂不提供 Intel Mac 安装包。

### 从阅读到整理

- **常见漫画格式与 PDF：** ZIP / CBZ、7z / CB7、RAR / CBR、PDF。EPUB / 轻小说阅读尚未启用。
- **卡片与列表自由切换：** 用封面浏览藏书，或切换到紧凑列表；配合搜索、排序、格式与标签筛选快速定位。
- **标签与书单管理：** 按题材、系列或阅读计划整理，结合收藏、最近阅读和进度继续上次的旅程。
- **隐藏空间与隐藏书单：** 将不想在普通书架展示的书籍和书单隐藏，并通过应用内密码入口访问。
- **即时阅读：** 支持拖入文件阅读，也可在系统的“打开方式”中选择 JoyRead，无需先导入书库。系统关联取决于安装与桌面环境。
- **专注阅读：** 单页 / 双页、从右向左或纵向阅读、缩放与适应模式、书签、缩略图导航和阅读进度。
- **三语界面：** 英文、简体中文、日文；新配置默认跟随系统，不支持的语言回退到英文。

### 使用示例：拖进来，选择阅读或导入

把支持的文件拖入书库窗口：放到左侧 **阅读** 区域直接打开，放到右侧 **导入** 区域加入书库。也可以使用左上角操作菜单打开文件、打开并导入，或批量导入文件与文件夹。

![拖放文件时选择直接阅读或导入书库](docs/assets/readme/drag-drop-zh-CN.png)

**直接阅读**适合临时查看文件；默认不复制文件，也不添加书库记录。**导入**会创建由 JoyRead 管理的副本，保留原文件，方便长期管理。若启用了“打开书籍时导入”，打开文件也会尝试导入；加密漫画仍不可导入。

### 使用示例：补全书籍资料

点击书籍的详情按钮，或从更多菜单进入详情页：

| 操作 | 结果 |
| --- | --- |
| 双击书名或作者 | 手动编辑，按 Enter 保存；按 Esc 或移开焦点取消。 |
| 双击语言 | 选择书籍的语言。 |
| 双击封面 | 打开封面编辑器，调整封面；不修改原书籍文件。 |
| 使用标签区的添加按钮 | 为书籍添加标签，方便后续筛选。 |

导入漫画包附带受支持的 **`meta.json` 或 `ComicInfo.xml` 元信息**时，JoyRead 会预处理并填入可识别的书名、作者、标签和语言。结果取决于元信息中实际提供的字段；缺少或无法解析的信息会回退到文件名或默认值，也可以在详情页手动修正。

![书籍详情：书名、作者、语言、封面与页面预览](docs/assets/readme/details-zh-CN.png)

### 使用示例：用标签整理书库

在详情页给书籍加标签，通过书库工具栏按标签筛选；在 **设置 → 标签** 中统一搜索、新建、重命名或删除标签。删除标签不会删除书籍。需要独立分组时，可创建普通书单或使用隐藏书单。

![设置中的中文标签管理](docs/assets/readme/tags-zh-CN.png)

### 使用示例：打开即读

阅读器提供单页和双页布局，支持阅读方向、书签、页面缩略图与进度导航。可以从书库继续阅读，也可以把 JoyRead 作为本地文件的可选打开方式。

![JoyRead 双页阅读器](docs/assets/readme/reader.png)

### 加密漫画与隐藏功能的边界

- **加密漫画可以阅读，但不能导入。** 支持的密码保护 ZIP/CBZ、7z/CB7、RAR/CBR 会在打开时请求密码，密码仅用于当前会话。
- **隐藏只作用于 GUI，不是文件加密。** 隐藏书籍、书单和应用内访问密码不会加密磁盘上的文件。
- 加密漫画解出的页面缓存以明文保存；**设置 → 隐私 → 关闭时删除页面缓存**默认开启。部分格式使用 7-Zip 辅助程序解压时，密码可能短暂出现在同用户进程可读取的命令行中；AES ZIP 使用进程内解密。详见[加密漫画说明](docs/MANUAL.md#encrypted-archives)。

### 开源与开发

JoyRead 采用 **[GNU GPL v3.0 only](LICENSE)**；第三方组件许可见 [THIRD_PARTY_NOTICES.txt](packaging/THIRD_PARTY_NOTICES.txt)。示例截图中的书籍封面和页面版权归各自权利人所有，不随软件附赠，也不受 JoyRead 软件许可授权。

源码运行步骤见下方 [Development](#development)，完整控件说明见[使用手册](docs/MANUAL.md)，构建步骤见[打包指南](docs/PACKAGING.md)。

</details>

<details>
<summary><strong>日本語 · ジョイヨミ</strong></summary>

<p align="center"><img src="src/joyread/ui/resources/icons/JoyRead.png" width="112" alt="JoyRead アプリアイコン"></p>
<h1 align="center">ジョイヨミ · JoyRead</h1>
<p align="center"><strong>ローカルの蔵書管理をもっと手軽に。読みたいときに、すぐ読める。</strong></p>
<p align="center"><a href="https://github.com/CtfTnk/JoyRead/releases/latest">ダウンロード</a> · <a href="docs/MANUAL.md">使い方（英語）</a> · <a href="https://github.com/CtfTnk/JoyRead/releases/tag/v1.0.2">1.0.2 の更新内容</a> · <a href="https://github.com/CtfTnk/JoyRead/issues">不具合の報告</a></p>

JoyRead は **macOS、Windows、Ubuntu/Linux** 向けのローカル漫画・PDF リーダー兼ライブラリ管理アプリです。お気に入りの本をインポートして整理することも、ファイルをそのまま開いて読み始めることもできます。アカウントやクラウドサービスは必要ありません。

![日本語のライブラリ：表紙カード、最近、お気に入り、非表示のコレクション](docs/assets/readme/library-ja.png)

### ダウンロードとインストール

| OS | 1.0.2 パッケージ | インストール方法 |
| --- | --- | --- |
| macOS 13+ · Apple Silicon | [DMG](https://github.com/CtfTnk/JoyRead/releases/download/v1.0.2/JoyRead-1.0.2-macos-arm64.dmg) | ディスクイメージを開き、JoyRead を Applications にドラッグします。 |
| Windows · x64 | [EXE](https://github.com/CtfTnk/JoyRead/releases/download/v1.0.2/JoyRead-1.0.2-windows-x86_64-setup.exe) | インストーラーの案内に従います。 |
| Ubuntu/Linux · amd64 | [DEB](https://github.com/CtfTnk/JoyRead/releases/download/v1.0.2/JoyRead-1.0.2-linux-amd64.deb) | ソフトウェア管理アプリ、または `sudo apt install ./JoyRead-1.0.2-linux-amd64.deb` を使います。 |
| Ubuntu/Linux · arm64 | [DEB](https://github.com/CtfTnk/JoyRead/releases/download/v1.0.2/JoyRead-1.0.2-linux-arm64.deb) | ソフトウェア管理アプリ、または `sudo apt install ./JoyRead-1.0.2-linux-arm64.deb` を使います。 |

パッケージとチェックサムは[リリースページ](https://github.com/CtfTnk/JoyRead/releases/tag/v1.0.2)にあります。macOS 版は Apple の公証を受けておらず、Windows 版にも信頼されたコード署名はありません。初回起動時の案内と実機検証状況はリリースノートをご確認ください。Intel Mac 用のパッケージはありません。

### 読むことも、整理することも

- **漫画アーカイブと PDF：** ZIP / CBZ、7z / CB7、RAR / CBR、PDF に対応。EPUB・ライトノベル機能は未有効化です。
- **カード / リスト切り替え：** 表紙で探す表示とコンパクトな一覧を選択。検索、並べ替え、形式・タグでの絞り込みにも対応します。
- **タグとコレクション：** ジャンル、シリーズ、読書予定などで整理。お気に入り、最近読んだ本、読書進捗も管理できます。
- **非表示スペース：** 通常の本棚に表示したくない本やコレクションを隠し、アプリ内のパスワード画面からアクセスできます。
- **すぐに読める：** ファイルのドラッグ＆ドロップや OS の「このアプリケーションで開く」に対応。先にインポートする必要はありません。ファイル関連付けはインストール方法とデスクトップ環境に依存します。
- **読みやすい表示：** 単ページ・見開き、右から左・縦方向の読み進め、ズーム、表示サイズ調整、しおり、サムネイル、読書進捗。
- **3 言語の UI：** 英語、簡体字中国語、日本語。新規設定ではシステムに従い、未対応言語では英語を使用します。

### 使い方：ドロップして、読むか整理するかを選ぶ

対応ファイルをライブラリにドラッグし、左の **読む** にドロップすると直接開き、右の **インポート** にドロップするとライブラリへ追加します。左上の操作メニューから、ファイルを開く、開いてインポートする、ファイルやフォルダーをまとめて取り込むこともできます。

![ドラッグ＆ドロップで読むかインポートするかを選択](docs/assets/readme/drag-drop-ja.png)

**直接開く**場合、初期設定ではファイルをコピーせず、本棚にも追加しません。**インポート**では元ファイルを残して管理用コピーを作成します。「開くときにインポート」を有効にすると、開いたファイルの取り込みも試みます。ただし、暗号化された漫画はインポートできません。

### 使い方：本の情報を整える

本の詳細ボタン、またはその他の操作メニューから詳細画面を開きます。

| 操作 | 内容 |
| --- | --- |
| タイトルや著者をダブルクリック | 手動編集し、Enter で保存。Esc またはフォーカス移動で取り消し。 |
| 言語をダブルクリック | 本の言語を選択。 |
| 表紙をダブルクリック | 表紙エディターで調整。元の書籍ファイルは変更しません。 |
| タグ欄の追加ボタン | 絞り込みに使うタグを付与。 |

漫画アーカイブに対応する **`meta.json` または `ComicInfo.xml`** が含まれている場合、JoyRead は読み取れたタイトル、著者、タグ、言語をインポート時に取り込みます。取得できる内容はメタデータの記載に依存します。情報がない場合や解析できない場合はファイル名や既定値を使い、詳細画面から修正できます。

![本の詳細：タイトル、著者、言語、表紙とページプレビュー](docs/assets/readme/details-ja.png)

### 使い方：タグで蔵書を整理する

詳細画面でタグを付け、ライブラリのツールバーで絞り込みます。**設定 → タグ** ではタグの検索、作成、名前変更、削除をまとめて管理できます。タグを削除しても本は削除されません。別のまとまりが必要なら、通常のコレクションや非表示のコレクションも使えます。

![設定画面の日本語タグ管理](docs/assets/readme/tags-ja.png)

### 使い方：すぐに読み始める

リーダーでは単ページと見開き表示を切り替え、読む方向、しおり、ページサムネイル、進捗バーを使えます。本棚から続きを読むことも、ローカルファイルを開くアプリとして JoyRead を選ぶこともできます。

![JoyRead の見開きリーダー](docs/assets/readme/reader.png)

### 暗号化アーカイブと非表示機能について

- **暗号化された漫画は読めますが、インポートできません。** 対応するパスワード付き ZIP/CBZ、7z/CB7、RAR/CBR を開く際にパスワードを求めます。パスワードはそのセッションだけで使います。
- **非表示は GUI 上の機能であり、ファイルの暗号化ではありません。** 本やコレクションを隠しても、ディスク上のファイルは暗号化されません。
- 復号したページキャッシュは平文で保存されます。**設定 → プライバシー → 閉じるときにページキャッシュを削除**は初期設定で有効です。一部の形式では 7-Zip 補助プログラムのコマンドラインにパスワードが一時的に現れ、同じユーザーのプロセスから見える場合があります。AES ZIP はアプリ内で復号します。[詳しい説明](docs/MANUAL.md#encrypted-archives)をご覧ください。

### オープンソースと開発

JoyRead は **[GNU GPL v3.0 only](LICENSE)** で公開しています。第三者コンポーネントのライセンスは [THIRD_PARTY_NOTICES.txt](packaging/THIRD_PARTY_NOTICES.txt) を参照してください。スクリーンショット内の表紙・本文の権利は各権利者に帰属し、書籍はアプリに含まれず、JoyRead のソフトウェアライセンスの対象にもなりません。

ソースからの起動は下の [Development](#development)、操作の詳細は[マニュアル](docs/MANUAL.md)、ビルド手順は[パッケージガイド](docs/PACKAGING.md)を参照してください。

</details>

<p align="center">
  <img src="src/joyread/ui/resources/icons/JoyRead.png" width="128" alt="JoyRead app icon">
</p>
<h1 align="center">JoyRead</h1>
<p align="center"><strong>Your local library, easier to manage. Your next book, ready to read.</strong></p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0--only-blue" alt="License: GPL-3.0-only"></a>
  <a href="https://github.com/CtfTnk/JoyRead/releases/tag/v1.0.2"><img src="https://img.shields.io/badge/release-1.0.2-39846a" alt="Release 1.0.2"></a>
  <a href="#download"><img src="https://img.shields.io/badge/macOS-Apple_Silicon-222222?logo=apple&amp;logoColor=white" alt="macOS Apple Silicon"></a>
  <a href="#download"><img src="https://img.shields.io/badge/Windows-x64-0078D4" alt="Windows x64"></a>
  <a href="#download"><img src="https://img.shields.io/badge/Ubuntu-amd64_%7C_arm64-E95420?logo=ubuntu&amp;logoColor=white" alt="Ubuntu amd64 and arm64"></a>
</p>
<p align="center">
  <a href="#download"><strong>Download</strong></a> ·
  <a href="#getting-started">Get started</a> ·
  <a href="#a-closer-look">Screenshots</a> ·
  <a href="docs/MANUAL.md">User manual</a> ·
  <a href="https://github.com/CtfTnk/JoyRead/releases/tag/v1.0.2">What's new</a> ·
  <a href="https://github.com/CtfTnk/JoyRead/issues">Feedback</a>
</p>

JoyRead is a local manga, comic, and PDF reader with a desktop library for **macOS, Windows, and Ubuntu/Linux**. Import your favourites for a collection you can keep organised, or open a file and start reading immediately. No account or cloud service required.

![JoyRead library with cover cards, collections, favourites, and Hidden Space](docs/assets/readme/library-en.png)

## Download

| Platform | JoyRead 1.0.2 | Installation |
| --- | --- | --- |
| macOS 13+ · Apple Silicon | [Download DMG](https://github.com/CtfTnk/JoyRead/releases/download/v1.0.2/JoyRead-1.0.2-macos-arm64.dmg) | Open the disk image and drag JoyRead into Applications. |
| Windows · x64 | [Download EXE](https://github.com/CtfTnk/JoyRead/releases/download/v1.0.2/JoyRead-1.0.2-windows-x86_64-setup.exe) | Run the installer and follow the setup prompts. |
| Ubuntu/Linux · amd64 | [Download DEB](https://github.com/CtfTnk/JoyRead/releases/download/v1.0.2/JoyRead-1.0.2-linux-amd64.deb) | Use your software installer, or `sudo apt install ./JoyRead-1.0.2-linux-amd64.deb`. |
| Ubuntu/Linux · arm64 | [Download DEB](https://github.com/CtfTnk/JoyRead/releases/download/v1.0.2/JoyRead-1.0.2-linux-arm64.deb) | Use your software installer, or `sudo apt install ./JoyRead-1.0.2-linux-arm64.deb`. |

Find all packages and checksums on the [release page](https://github.com/CtfTnk/JoyRead/releases/tag/v1.0.2), or browse [previous releases](https://github.com/CtfTnk/JoyRead/releases). There is no Intel Mac package. macOS builds are not Apple-notarized, and Windows installers do not carry a trusted code signature; see the release notes for first-launch guidance and outstanding desktop validation.

## Made for your local books

| Capability | What you can do |
| --- | --- |
| **Manga and PDF** | Read ZIP / CBZ, 7z / CB7, RAR / CBR, and PDF. EPUB / light novel reading is not enabled. |
| **Cards or lists** | Browse by cover or switch to a compact list. Search, sort, and filter by format or tag. |
| **Tags and collections** | Organise by genre, series, or reading plans, with favourites, recent books, and progress. |
| **Editable book details** | Correct titles, authors, and languages, adjust covers, and reuse embedded archive metadata. |
| **Hidden Space** | Keep books and collections out of the ordinary shelf behind an in-app password entry. This is GUI hiding, not encryption. |
| **Open and read** | Drag in a file or choose JoyRead through your operating system's **Open With** menu. Importing first is optional. |
| **A flexible reader** | Single pages or spreads, right-to-left and vertical reading, fit and zoom controls, bookmarks, thumbnails, and saved progress. |
| **Three interface languages** | English, Simplified Chinese, and Japanese. New profiles follow the system language, with English fallback. |

## Getting started

1. **Install and launch JoyRead.** A normal launch opens your library.
2. **Drop a supported file onto the window.** Choose **Read** on the left to open it, or **Import** on the right to keep it in your library. The action menu also offers Open, Open & Import, and file/folder import.
3. **Make the shelf yours.** Switch between cards and lists, create collections, add tags, and mark favourites.
4. **Start reading.** Open a book from the shelf, or use **Open With → JoyRead** in your file manager. File associations depend on installation and desktop integration.

**Read** opens a file in place: by default, it does not copy the file or add a library entry. **Import** makes a managed copy while preserving the original. If **Import book when opening** is enabled in Settings, opening also attempts an import; encrypted comics still cannot be imported.

## A closer look

### Drop a book. Read now or keep it.

A file you only want to glance through does not need a permanent place on your shelf. The drop overlay makes the choice explicit, while file and folder import lets you build a lasting library.

![Drag-and-drop overlay with Read and Import destinations](docs/assets/readme/drag-drop-en.png)

### Book details you can make your own

Open **Detail** from a book's detail button or More menu.

| Gesture | Action |
| --- | --- |
| Double-click the **title** or **author** | Edit the text and press Enter to save. Escape or moving focus away cancels. |
| Double-click the **language** | Select the book's language. |
| Double-click the **cover** | Open the cover editor and adjust the cover without changing the source book. |
| Use the **add tag** button | Attach tags for browsing and filtering. |

When an imported comic archive includes supported **`meta.json` or `ComicInfo.xml` metadata**, JoyRead preprocesses the available title, author, tags, and language for the library. Fields depend on what the archive actually supplies. Missing or unreadable metadata falls back to the filename or defaults, and you can correct the result in the detail view.

![Book details with editable metadata, cover, progress, and page previews](docs/assets/readme/details-en.png)

### Give your library a useful vocabulary

Add tags in book details, then filter by them from the shelf toolbar. **Settings → Tags** brings search, creation, renaming, and deletion together. Deleting a tag does not delete its books. Collections provide another way to group a series or reading list, including collections you want to hide.

![English tag management with search, alphabetical indexing, rename, and delete](docs/assets/readme/tags-en.png)

### Settle into the page

Choose single-page or spread layouts, set your reading direction, and move through a book using thumbnails, bookmarks, or the progress bar. Resume from the library or use JoyRead as an immediate reader for a local file.

![JoyRead two-page reader with navigation and reading controls](docs/assets/readme/reader.png)

## Encrypted comics and Hidden Space

- **Encrypted comics can be read, but cannot be imported.** Supported password-protected ZIP/CBZ, 7z/CB7, and RAR/CBR archives request a password when opened. Passwords are used for the current session only.
- **Hidden Space is GUI hiding only.** Hidden books, hidden collections, and the in-app password do not encrypt files on disk.
- Decrypted page caches are stored **unencrypted**. **Settings → Privacy → Delete cached pages when closing** is enabled by default. Some archive formats pass the password to the 7-Zip helper's command line, briefly exposing it to same-user processes; AES ZIP is decrypted in-process. Read the [encrypted archive notes](docs/MANUAL.md#encrypted-archives) for details.

## Development

JoyRead is built with Python, PySide6, and SQLite. Use the repository's conda environment by path:

```bash
git clone https://github.com/CtfTnk/JoyRead.git
cd JoyRead
# Create once; skip this command if the environment already exists.
conda env create --prefix .conda/joyread-py312 -f environment-release.yml
conda activate ./.conda/joyread-py312
python -m joyread.app.main
```

Run tests with `python -m pytest -q`. To use a disposable library during development, set `JOYREAD_RUNTIME_DIR=/tmp/joyread-smoke` before launching. Dependencies live in `pyproject.toml`; refresh them with `python -m pip install -e '.[dev,release]'`.

The novel reader remains disabled and separate from the shipping app. Its optional `epub` extra adds dependencies for novel-reader development; installing it does not enable EPUB access.

[User manual](docs/MANUAL.md) · [Packaging](docs/PACKAGING.md) · [Changelog](CHANGELOG.md) · [Report an issue](https://github.com/CtfTnk/JoyRead/issues)

## License

JoyRead is free and open-source software under **[GNU GPL v3.0 only](LICENSE)**. See [THIRD_PARTY_NOTICES.txt](packaging/THIRD_PARTY_NOTICES.txt) for bundled component licenses.

Book covers and pages in the supplied screenshots belong to their respective rights holders. These books are not bundled with JoyRead and are not licensed under JoyRead's software license.

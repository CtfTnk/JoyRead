<p align="center"><a href="README.md">English</a> · <a href="README.zh-CN.md">简体中文</a> · <strong>日本語</strong></p>

<p align="center"><img src="src/joyread/ui/resources/icons/JoyRead.png" width="112" alt="JoyRead アプリアイコン"></p>
<h1 align="center">ジョイヨミ · JoyRead</h1>
<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0--only-blue" alt="License: GPL-3.0-only"></a>
  <a href="https://github.com/CtfTnk/JoyRead/releases/tag/v1.4.0"><img src="https://img.shields.io/badge/release-1.4.0-39846a" alt="Release 1.4.0"></a>
  <a href="https://github.com/CtfTnk/JoyRead/releases/tag/v1.4.0"><img src="https://img.shields.io/badge/macOS-Apple_Silicon-222222?logo=apple&amp;logoColor=white" alt="macOS Apple Silicon"></a>
  <a href="https://github.com/CtfTnk/JoyRead/releases/tag/v1.4.0"><img src="https://img.shields.io/badge/Windows-x64-0078D4" alt="Windows x64"></a>
  <a href="https://github.com/CtfTnk/JoyRead/releases/tag/v1.4.0"><img src="https://img.shields.io/badge/Ubuntu-amd64_%7C_arm64-E95420?logo=ubuntu&amp;logoColor=white" alt="Ubuntu amd64 and arm64"></a>
</p>
<p align="center"><strong>ローカルの蔵書管理をもっと手軽に。読みたいときに、すぐ読める。</strong></p>
<p align="center"><a href="https://github.com/CtfTnk/JoyRead/releases/latest">ダウンロード</a> · <a href="docs/MANUAL.ja.md">使い方</a> · <a href="https://github.com/CtfTnk/JoyRead/releases/tag/v1.4.0">1.4.0 の更新内容</a> · <a href="https://github.com/CtfTnk/JoyRead/issues">不具合の報告</a></p>

JoyRead は **macOS、Windows、Ubuntu/Linux** 向けのローカル漫画・PDF リーダー兼ライブラリ管理アプリです。お気に入りの本をインポートして整理することも、ファイルをそのまま開いて読み始めることもできます。アカウントやクラウドサービスは必要ありません。

![日本語のライブラリ：表紙カード、最近、お気に入り、非表示のコレクション](docs/assets/readme/library-ja.png)

## ダウンロードとインストール

| OS | 1.4.0 パッケージ | インストール方法 |
| --- | --- | --- |
| macOS 26+ · Apple Silicon | [DMG](https://github.com/CtfTnk/JoyRead/releases/download/v1.4.0/JoyRead-1.4.0-macos-arm64.dmg) | ディスクイメージを開き、JoyRead を Applications にドラッグします。 |
| Windows · x64 | [EXE](https://github.com/CtfTnk/JoyRead/releases/download/v1.4.0/JoyRead-1.4.0-windows-x86_64-setup.exe) | インストーラーの案内に従います。 |
| Ubuntu 22.04+ / Linux · amd64 | [DEB](https://github.com/CtfTnk/JoyRead/releases/download/v1.4.0/JoyRead-1.4.0-linux-amd64.deb) | ターミナルで `sudo apt install ./JoyRead-1.4.0-linux-amd64.deb` を使います。 |
| Ubuntu 22.04+ / Linux · arm64 | [DEB](https://github.com/CtfTnk/JoyRead/releases/download/v1.4.0/JoyRead-1.4.0-linux-arm64.deb) | ターミナルで `sudo apt install ./JoyRead-1.4.0-linux-arm64.deb` を使います。 |

**Ubuntu ではターミナルからインストールしてください。** GUI インストーラーは **「Preparing」** で停止するため使用できません。この問題は未修正です。ダウンロード先のフォルダーでターミナルを開き、上の表にあるパッケージに対応するコマンドを実行してください。

パッケージとチェックサムは[リリースページ](https://github.com/CtfTnk/JoyRead/releases/tag/v1.4.0)にあります。macOS 版は Apple の公証を受けておらず、Windows 版にも信頼されたコード署名はありません。初回起動時の案内はリリースノートをご確認ください。Intel Mac 用のパッケージはありません。

**macOS での更新：** 先にジョイヨミを終了してから新しい版を Applications にドラッグし、Finder に尋ねられたら**置き換える**を選んでください。両方を残すと「JoyRead 2」という 2 つ目のコピーができます。

**1.3.1 以前からの更新：** 先にライブラリをバックアップしてください。ジョイヨミ 1.4.0 で一度開いたライブラリは、以前のバージョンでは開けなくなります。

## 読むことも、整理することも

- **漫画アーカイブと PDF：** ZIP / CBZ、7z / CB7、RAR / CBR、PDF に対応。EPUB・ライトノベル機能は未有効化です。
- **カード / リスト切り替え：** 表紙で探す表示とコンパクトな一覧を選択。検索、並べ替え、形式・言語・タグでの絞り込みにも対応します。
- **タグとコレクション：** ジャンル、シリーズ、読書予定などで整理。お気に入り、最近読んだ本、読書進捗も管理できます。
- **非表示スペース：** 通常の本棚に表示したくない本やコレクションを隠します。非表示の本と表紙はディスク上で暗号化され、パスフレーズでロックを解除し、リカバリーコードで復旧できます。
- **すぐに読める：** ファイルのドラッグ＆ドロップや OS の「このアプリケーションで開く」に対応。先にインポートする必要はありません。ファイル関連付けはインストール方法とデスクトップ環境に依存します。
- **読みやすい表示：** 単ページ・見開き、右から左・縦方向の読み進め、ズーム、表示サイズ調整、しおり、サムネイル、読書進捗。
- **3 言語の UI：** 英語、簡体字中国語、日本語。新規設定ではシステムに従い、未対応言語では英語を使用します。

## 使い方：ドロップして、読むか整理するかを選ぶ

対応ファイルをライブラリにドラッグし、左の **読む** にドロップすると直接開き、右の **インポート** にドロップするとライブラリへ追加します。左上の操作メニューから、ファイルを開く、開いてインポートする、ファイルやフォルダーをまとめて取り込むこともできます。

![ドラッグ＆ドロップで読むかインポートするかを選択](docs/assets/readme/drag-drop-ja.png)

**直接開く**場合、初期設定ではファイルをコピーせず、本棚にも追加しません。**インポート**では元ファイルを残して管理用コピーを作成します。「開くときにインポート」を有効にすると、開いたファイルの取り込みも試みます。パスワード付き圧縮ファイルには対応していません。

## 使い方：本の情報を整える

本の詳細ボタン、またはその他の操作メニューから詳細画面を開きます。

| 操作 | 内容 |
| --- | --- |
| タイトルや著者をダブルクリック | 手動編集し、Enter で保存。Esc またはフォーカス移動で取り消し。 |
| 言語をダブルクリック | 本の言語を選択。 |
| 表紙をダブルクリック | 表紙エディターで調整。元の書籍ファイルは変更しません。 |
| タグ欄の追加ボタン | 絞り込みに使うタグを付与。 |

漫画アーカイブに対応する **`meta.json` または `ComicInfo.xml`** が含まれている場合、JoyRead は読み取れたタイトル、著者、タグ、言語をインポート時に取り込みます。取得できる内容はメタデータの記載に依存します。情報がない場合や解析できない場合はファイル名や既定値を使い、詳細画面から修正できます。

![本の詳細：タイトル、著者、言語、表紙とページプレビュー](docs/assets/readme/details-ja.png)

## 使い方：タグで蔵書を整理する

詳細画面でタグを付け、ライブラリのツールバーで絞り込みます。**設定 → タグ** ではタグの検索、作成、名前変更、削除をまとめて管理できます。タグを削除しても本は削除されません。別のまとまりが必要なら、通常のコレクションや非表示のコレクションも使えます。

![設定画面の日本語タグ管理](docs/assets/readme/tags-ja.png)

## 使い方：すぐに読み始める

リーダーでは単ページと見開き表示を切り替え、読む方向、しおり、ページサムネイル、進捗バーを使えます。本棚から続きを読むことも、ローカルファイルを開くアプリとして JoyRead を選ぶこともできます。

進捗バーにマウスを合わせると、移動先のページをプレビューできます。

![JoyRead の見開きリーダーと進捗バーのページプレビュー](docs/assets/readme/reader.png)

## 非表示スペースとパスワード付き圧縮ファイル

- **非表示スペースは本を暗号化します。** 非表示の本と表紙はライブラリ内で暗号化して保存され、4 文字以上のパスフレーズでロックを解除します。設定時にリカバリーコードを保存してください。両方を失うと、非表示の本は誰にも復元できません。
- **ライブラリのデータベースは暗号化されません。** 非表示の本のタイトル、タグ、コレクション名、読書位置は読める状態のままで、ログにも開いた本の名前が記録されます。詳しくは[非表示スペース](docs/MANUAL.ja.md#非表示スペース)をご覧ください。
- **パスワード付き圧縮ファイルには対応していません。** ジョイヨミはパスワード付きの ZIP、7z、RAR を開いたりインポートしたりせず、パスワードも求めません。先に別のツールで展開してください。パスワード付き圧縮ファイルを含む通常の圧縮ファイルは開け、その他のページを表示します。詳しくは[暗号化アーカイブ](docs/MANUAL.ja.md#暗号化アーカイブ)をご覧ください。

## オープンソースと開発

JoyRead は **[GNU GPL v3.0 only](LICENSE)** で公開しています。第三者コンポーネントのライセンスは [THIRD_PARTY_NOTICES.txt](packaging/THIRD_PARTY_NOTICES.txt) を参照してください。

スクリーンショットでは、佐藤秀峰『ブラックジャックによろしく』の公式電子版を[二次利用規約](https://densho810.com/free/)に基づいて使用しています。

ソースからの起動は英語版の [Development](README.md#development)、操作の詳細は[マニュアル](docs/MANUAL.ja.md)、ビルド手順は[パッケージガイド](docs/PACKAGING.md)を参照してください。

# Slide2Video AI Generator

**スライド（PDF）と台本（テキスト）を入れるだけで、プロ品質のナレーション付き動画（MP4）が完成する**自動制作システムです。

ナレーション音声の生成、音声に同期したテロップ、スライドの動き（Ken Burns）、场面転換、イントロ/アウトロカード、BGMの自動選曲とダッキング、音量のプロ基準正規化——すべて自動で行われます。

## 出来上がる動画の品質

| 機能 | 内容 |
|------|------|
| 🎙️ ナレーション | 台本から自然な日本語音声を自動生成（Fish Audio / 無料のedge-tts） |
| 💬 テロップ | **実際の発話タイミングに同期**（whisper解析）。放送基準の文字数（1行16文字×2行）・座布団付き |
| ✨ キーワード強調 | 台本で `**こう書く**` と、その言葉だけ**黄色ハイライト** |
| 🎬 スライドの動き | ゆっくりズーム/パン（Ken Burns）をスライドごとに方向を変えて自動適用 |
| 🔄 場面転換 | クロスフェード＋転換効果音（whoosh） |
| 🎨 イントロ/アウトロ | タイトルカードとエンドカードを自動生成 |
| 🖼️ 黒帯なし | 縦横比が合わないスライドは「ぼかし拡大背景」で美しく全画面化 |
| 🎵 BGM | 台本の雰囲気から自動選曲、ナレーション中は自動で音量ダウン（ダッキング） |
| 🔊 音量 | YouTube標準の **-14 LUFS** に自動正規化（音が小さい/大きい問題が起きない） |

---

## 必要なもの（最初に1回だけ）

### Mac の場合

ターミナルで以下をコピペ実行：

```bash
brew install ffmpeg poppler node python3
```

### Windows の場合

**管理者権限**でPowerShellを開き、以下を順番にコピペ実行：

```powershell
# 1. パッケージマネージャー Chocolatey を入れる（未導入の場合）
Set-ExecutionPolicy Bypass -Scope Process -Force; [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072; iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))
```

```powershell
# 2. 必要ツールを一括インストール
choco install python nodejs ffmpeg poppler -y
```

インストール後、PowerShellを一度閉じて開き直してください（環境変数の反映のため）。

---

## セットアップ（最初に1回だけ）

1. このフォルダを開いてセットアップスクリプトを実行：
   - **Mac**: ターミナルで `./setup.sh`
   - **Windows**: `setup.bat` をダブルクリック
2. （任意）高品質TTSを使う場合は、`.env.example` をコピーして `.env` を作り、Fish AudioのAPIキーを設定：
   ```env
   FISH_AUDIO_API_KEY=あなたのAPIキー
   ```
   **APIキーがなくても動きます**（無料のedge-tts音声に自動フォールバック）。

---

## 使い方（毎回3ステップ）

### 1. 素材を入れる

`inbox/` の中に動画1本ぶんのフォルダを作り、**PDF**と**台本（.md）**を入れます：

```
inbox/
└── 新商品紹介/          ← フォルダ名は自由
    ├── slides.pdf       ← スライド（PDF）
    └── script.md        ← 台本（下記の書き方参照）
```

### 2. 実行する

- **Mac**: `./run.sh`
- **Windows**: `run.bat` をダブルクリック

※ 初回だけ、テロップ同期用のAIモデル（約470MB）のダウンロードが走るため時間がかかります。

### 3. 完成

`output/新商品紹介/final.mp4` が出来上がります。

---

## 台本の書き方（script.md）

```markdown
# Title: 新商品のご紹介
# Style: pop

# Slide 1
こんにちは。今日は**新商品**をご紹介します。

# Slide 2
この商品の特徴は3つあります。まず、**価格が従来の半分**であること。
```

| 記法 | 意味 |
|------|------|
| `# Title: ...` | イントロカードに表示するタイトル（省略時はフォルダ名） |
| `# Style: ...` | 動画の雰囲気プリセット（下記から選択。省略時は modern） |
| `# Slide 1` | ここから下がスライド1のナレーション |
| `**強調**` | テロップで黄色ハイライトされる（音声には影響しない） |

## 雰囲気プリセット（# Style: で選択）

| プリセット | 雰囲気 | 向いている動画 |
|-----------|--------|--------------|
| `modern`（既定） | ダークブルー×光。クールで先進的 | 会社紹介・サービス紹介・技術解説 |
| `pop` | ピンク×オレンジ。明るく元気、テンポ速め | 商品PR・キャンペーン・楽しい話題 |
| `calm` | グリーン系。ゆったり落ち着いた転換 | 医療・教育・丁寧な解説 |
| `serious` | ネイビー×グレー。控えめな効果音 | 報告書・データ解説・堅い話題 |

さらに細かく変えたい場合は、動画フォルダに `style.json` を置くと個別に上書きできます（例：テロップの文字サイズだけ変える）：

```json
{ "design": { "subtitle_font_size": 64 } }
```

全体の既定値は `src/video-style.json` にまとまっています（色・速度・音量・すべてここが本体）。

## BGMを増やす

`assets/bgm/` の中に雰囲気別フォルダがあります。**お手持ちのmp3を入れるだけ**で選曲対象になります：

```
assets/bgm/
├── upbeat/     ← 明るい曲
├── relaxing/   ← 落ち着いた曲
└── serious/    ← 真面目な曲
```

台本の内容から雰囲気を自動判定して選曲されます（`# Style:` のプリセットで固定も可能）。

---

## よくあるつまずき

| 症状 | 対処 |
|------|------|
| 「仮想環境が見つかりません」 | 先に `setup.sh` / `setup.bat` を実行してください |
| PDFが読めないエラー | popplerが未インストール。上記「必要なもの」を実行 |
| 音声が無音になる | ネット接続を確認（TTSはオンライン生成）。`.env` のAPIキーも確認 |
| 初回がとても遅い | whisperモデル（約470MB）のダウンロード中です。2回目からは速くなります |
| Windowsで文字化け | PowerShell/コマンドプロンプトで `chcp 65001` を実行してから再実行 |

---

## 仕組み（開発者向け）

```
inbox/（PDF+台本）
  → src/main.py           パース → TTS生成 → whisperで発話タイミング解析
  → src/template.html     コンポジションテンプレート（デザイン・アニメの本体）
  → hyperframes-app/      HyperFramesが1920×1080でレンダリング
  → FFmpeg                SFX合成 → BGMダッキング → -14 LUFS正規化 → final.mp4
```

- 動画の見た目を変える → `src/template.html` と `src/video-style.json`
- 生成ロジックを変える → `src/main.py`
- `hyperframes-app/index.html` は毎回自動生成されるので直接編集しない

詳細は `CLAUDE.md`（AIエージェント向け指示書）を参照。

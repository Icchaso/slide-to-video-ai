# Slide2Video AI Generator

**スライド（PDF）と台本（テキスト）を入れるだけで、プロ品質のナレーション付き動画（MP4）が完成する**自動制作システムです。
AntiGravity などの AI エージェントに「inbox の素材で動画を作って」と頼むだけで、生成から**品質チェック（品質ゲート）**まで自動で行われます。

## 出来上がる動画の品質

| 機能 | 内容 |
|------|------|
| 🎙️ ナレーション | 台本から自然な日本語音声を自動生成（Fish Audio / 無料の edge-tts） |
| 💬 テロップ | **文ごとに音声を生成して結合**するので、発話とテロップが文単位で同期。放送基準の文字数（1行16文字×2行）・座布団付き |
| ✨ キーワード強調 | 台本で `**こう書く**` と、その言葉だけ**黄色ハイライト** |
| 🎬 スライドの動き | ゆっくりズーム/パン（Ken Burns）をスライドごとに方向を変えて自動適用 |
| 🔄 場面転換 | クロスフェード＋転換効果音（whoosh） |
| 🎨 イントロ/アウトロ | タイトルカードとエンドカードを自動生成 |
| 🖼️ 黒帯なし | 縦横比が合わないスライドは「ぼかし拡大背景」で美しく全画面化 |
| 🎵 BGM | 台本の雰囲気から自動選曲。ナレーション中は自動で音量ダウン（ダッキング）。短い曲は継ぎ目をクロスフェードして自動ループ |
| 🔊 音量 | YouTube 標準の **-14 LUFS** に自動正規化 |
| ✅ 品質ゲート | 尺・音量・BGM・テロップ・台本の整合を毎回機械チェックし、`build_summary.json` と確認用画像 `contact_sheet.jpg` を出力 |

---

## 必要なもの（最初に1回だけ）

### Mac

```bash
brew install ffmpeg poppler node python3
```

### Windows

**管理者権限**で PowerShell を開き、順番に実行：

```powershell
# 1. パッケージマネージャー Chocolatey（未導入の場合）
Set-ExecutionPolicy Bypass -Scope Process -Force; [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072; iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))
```

```powershell
# 2. 必要ツールを一括インストール
choco install python nodejs ffmpeg poppler -y
```

インストール後、PowerShell を一度閉じて開き直してください。

---

## セットアップ（最初に1回だけ）

1. セットアップスクリプトを実行：
   - **Mac**: `./setup.sh`
   - **Windows**: `setup.bat` をダブルクリック
2. **BGM を入れる**（重要）：`assets/bgm/relaxing/` `upbeat/` `serious/` に、商用利用可の音楽ファイル（mp3 など）を各2〜3曲置く。条件と入手先は [`assets/bgm/README.md`](assets/bgm/README.md)。入れないと BGM なしの動画になります。
   ※ 音楽ファイルは git に上がりません（公開リポジトリのため再配布を避ける）。**動画を作る PC ごとに置いてください**
3. （任意）高品質 TTS を使う場合は `.env.example` をコピーして `.env` を作り、Fish Audio の API キーを設定：
   ```env
   FISH_AUDIO_API_KEY=あなたのAPIキー
   ```
   **キーがなくても動きます**（無料の edge-tts に自動フォールバック）。キーが失効すると自動で edge-tts に切り替わり、品質ゲートに WARN が出ます
4. 環境チェック：`./run.sh --check`（Windows: `run.bat --check`）で「環境OK」が出れば準備完了

---

## 使い方（毎回3ステップ）

### 1. 素材を入れる

```
inbox/
└── 新商品紹介/          ← フォルダ名は自由（動画名になる）
    ├── slides.pdf       ← スライド（PDF。ファイル名は自由）
    └── script.md        ← 台本（下記の書き方参照）
```

### 2. 実行する

- **Mac**: `./run.sh`
- **Windows**: `run.bat` をダブルクリック
- AI エージェントに頼む場合: 「inbox の素材で動画を作って、品質ゲートの結果を教えて」

### 3. 結果を確認する

`output/新商品紹介/` に以下ができます：

| ファイル | 用途 |
|---|---|
| `final.mp4` | 完成動画 |
| `contact_sheet.jpg` | 12コマの確認用画像。**開くだけで全体の見た目が分かる** |
| `build_summary.json` | 品質ゲートの結果（`status`: PASS / WARN / FAIL と各チェックの詳細） |

終了コード: `0`=OK（PASS または WARN）/ `1`=実行エラー / `2`=環境不足 / `3`=品質ゲート FAIL。
WARN・FAIL の意味と対処は [`AGENTS.md`](AGENTS.md) の対処表を参照。

---

## 台本の書き方（script.md）

```markdown
# Title: 新商品のご紹介
# Style: pop
# BGM: upbeat

# Slide 1
こんにちは。今日は**新商品**をご紹介します。

# Slide 2
この商品の特徴は3つあります。まず、**価格が従来の半分**であること。
```

| 記法 | 意味 |
|------|------|
| `# Title: ...` | イントロカードに表示するタイトル（省略時はフォルダ名） |
| `# Style: ...` | 雰囲気プリセット（下記。省略時は modern） |
| `# BGM: ...` | BGM の指定。`relaxing` / `upbeat` / `serious`（フォルダ）、ファイル名、`none`（BGM なし）。省略時は台本から自動判定 |
| `# Slide 1` | ここから下がスライド1のナレーション。**文は「。！？」で区切る**（文ごとに音声化・テロップ同期） |
| `**強調**` | テロップで黄色ハイライト（音声には影響しない） |

台本に無いスライドは動画に入りません（品質ゲートが WARN で教えてくれます）。

## 雰囲気プリセット（# Style: で選択）

| プリセット | 雰囲気 | 向いている動画 |
|-----------|--------|--------------|
| `modern`（既定） | ダークブルー×光。クールで先進的 | 会社紹介・サービス紹介・技術解説 |
| `pop` | ピンク×オレンジ。明るく元気、テンポ速め | 商品PR・キャンペーン・楽しい話題 |
| `calm` | グリーン系。ゆったり落ち着いた転換 | 医療・教育・丁寧な解説 |
| `serious` | ネイビー×グレー。控えめな効果音 | 報告書・データ解説・堅い話題 |

さらに細かく変えたい場合は、動画フォルダに `style.json` を置くと個別に上書きできます：

```json
{ "design": { "subtitle_font_size": 64, "subtitles_enabled": false } }
```

全体の既定値は `src/video-style.json` にまとまっています（色・速度・音量・品質ゲートの閾値、すべてここが本体）。

---

## 品質担保の項目一覧（毎回自動で守られる基準）

| 項目 | 基準 | 根拠 |
|------|------|------|
| 音量 | 統合ラウドネス -14 LUFS ±0.7（±1.5 超で FAIL）/ トゥルーピーク -1.5 dBTP | YouTube 等の配信基準 |
| BGM | ナレーションより約 20dB 下（事前 -18dB ＋ サイドチェインダッキング 8:1）。フェードイン 2秒 / アウト 4秒。継ぎ目はクロスフェード | 映像音響の実務値 |
| BGM 素材 | 抑揚のない持続音（LRA < 1.5 LU）は警告 | 合成音が混入した過去の事故の再発防止 |
| テロップ | 1行16文字 × 2行以内、白文字＋黒縁＋半透明帯、音声より 80ms 先行して即時表示、改行は読点＞助詞境界 | 放送・映像翻訳業界の基準 |
| 映像 | Ken Burns 6%（方向ローテーション）、クロスフェード 0.6秒、黒帯なし（ぼかし背景） | 文字スライドが破綻しない上限 |
| 尺 | 期待値 ±1.5 秒 | レンダ欠落の検知 |
| 決定性 | 同じ入力なら同じ出力（TTS キャッシュ・決定的選曲・乱数不使用） | 再現性 |
| 環境 | `requirements.txt` バージョン固定、HyperFrames CLI ピン、実行前 preflight | 環境差による品質ブレの排除 |

---

## よくあるつまずき

| 症状 | 対処 |
|------|------|
| 「仮想環境が見つかりません」 | 先に `setup.sh` / `setup.bat` を実行 |
| 終了コード 2（環境不足） | 画面に出た `brew install ...` / `choco install ...` を実行して再実行 |
| BGM が入っていない | `assets/bgm/<mood>/` に音楽ファイルを追加（`assets/bgm/README.md`） |
| BGM が「ブーン」と鳴る | 置いた素材が音楽ではなく合成音・環境音。品質ゲートの `BGM: 持続音の可能性` 警告が目印。曲を差し替える |
| 音声が edge-tts になった（WARN） | `.env` の Fish Audio キーが失効または残高不足。新しいキーを発行して差し替え |
| 音声が無音（FAIL） | ネット接続を確認（TTS はオンライン生成） |
| Windows で文字化け | `chcp 65001` を実行してから再実行 |
| フォントが崩れる | Google Fonts をレンダ時に取得するため**オフラインでは Noto Sans JP が使えません**（既知の制約） |

---

## 仕組み（開発者・AI エージェント向け）

```
inbox/（PDF+台本）
  → src/main.py           preflight → パース → 文単位TTS → テロップ同期 → コンポジション生成
  → src/template.html     コンポジションテンプレート（デザイン・アニメの本体）
  → hyperframes-app/      HyperFrames が 1920×1080 でレンダリング（index.html は自動生成）
  → FFmpeg                BGMベッド → ダッキング → SFX → -14 LUFS 正規化 → final.mp4
  → 品質ゲート            build_summary.json / contact_sheet.jpg
```

- 動画の見た目を変える → `src/template.html` と `src/video-style.json`
- 生成ロジックを変える → `src/main.py`
- `hyperframes-app/index.html` は毎回自動生成されるので直接編集しない
- 変更後は `cd hyperframes-app && npm run check` がエラー 0 になることを確認

運用手順・WARN/FAIL 対処表・やってはいけないことは [`AGENTS.md`](AGENTS.md)（AI エージェント向け指示書）にまとめています。

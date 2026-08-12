# Slide2Video AI Generator

スライド（PDF）と台本（Markdown）を入力するだけで、AIを活用して「音声合成」「テロップの自動配置」「BGMの自動挿入とダッキング」までを完全自動で行い、プロフェッショナルなプレゼン動画（MP4）を生成する自動制作システムです。

## 概要 (Overview)
このシステムは以下の技術を組み合わせて完全自動のパイプラインを構築しています：
* **Python**: PDFの画像化（`pdf2image`）、Markdown台本のパース、BGMの自動選定、文字数に基づくテロップの自動分割・表示時間計算。
* **Fish Audio API**: スライドごとの台本テキストから、極めて自然で高品質な日本語音声を生成（TTS）。
* **HyperFrames (HTML/CSS/JS)**: 生成されたスライド画像、音声、テロップテキストを受け取り、Web技術を用いて動画のコンポジション（レイアウトやテロップのアニメーションなど）を構築・レンダリング。
* **FFmpeg**: レンダリングされた動画に対して、台本の雰囲気に合わせたBGMを挿入。人が話している間は自動でBGMの音量を下げる「ダッキング」処理を実行。

---

## 前提条件 (Requirements)
本システムをローカルで動かすには以下のインストールが必要です。

### Mac の場合 (Homebrew使用)
```bash
brew install poppler
brew install ffmpeg
brew install imagemagick
```
* その他、**Python 3.10+** と **Node.js (npm)** が必要です。

### Windows の場合
* **Python 3.10+**
* **Node.js (npm)**
* **FFmpeg**: 環境変数 `PATH` に追加されていること。
* **Poppler**: `pdf2image` を使うために必要。Windows用のバイナリをダウンロードして `PATH` に追加するか、WSL（Windows Subsystem for Linux）上で動かすことを推奨します。

---

## セットアップ (Setup)

1. **環境変数の設定**
   リポジトリのルートに `.env` ファイルを作成し、APIキーを設定してください（`.env.example` を参考にしてください）。
   ```env
   FISH_AUDIO_API_KEY=your_fish_audio_api_key_here
   ```

2. **依存関係のインストール**
   初期化スクリプトを実行して、PythonパッケージとNodeパッケージをインストールします。
   ```bash
   ./setup.sh
   ```
   *(Windows環境で bash が使えない場合は、`python -m venv venv`, `pip install -r requirements.txt`, および `cd hyperframes-app && npm install` を手動またはAIエージェントに実行させてください)*

---

## 使い方 (Usage)

### 👨‍💻 AntiGravity (AIエージェント) を使った全自動運用 【推奨】
本システムは **AntiGravity IDE** などのAIエージェントと組み合わせて使用することを前提に設計されています。MacでもWindowsでも、AIに指示を出すだけで完結します。

1. **素材の配置**
   `inbox/` フォルダの中に、動画化したい **PDFファイル（スライド）** と **Markdownファイル（台本）** を配置します。
   *(※ファイル名は何でも構いません。1つのPDFと1つの.mdファイルがあれば自動で認識します)*
   
2. **AIへの指示**
   AntiGravityのチャット欄に以下のように指示を出してください：
   > 「inboxに入れたPDFと台本を使って動画を作成して」

3. **自動実行**
   AntiGravityが自動的に `run.sh`（またはそれに相当する各コマンド）を実行し、以下のプロセスを完了させます：
   - 台本の読み込みと音声（TTS）の生成
   - テロップの最適な秒数配分と文字分割
   - HyperFramesによる動画レンダリング
   - BGMの自動合成（ダッキング処理）

4. **完成**
   処理が完了すると、`output/` フォルダの中に `final.mp4` という名前で完成した動画が出力されます。

### 🖥️ 手動での実行方法
CLIから直接実行する場合は以下のコマンドを使用します。

```bash
# inbox/ フォルダに PDFとMD を入れた状態で実行
./run.sh
```
これにより、`work/` ディレクトリ内で中間ファイル（画像や音声）が生成され、最終的な動画が `output/` フォルダに保存されます。

---

## 📂 フォルダ構成
* `inbox/` : 入力用フォルダ（ここにPDFと台本を入れます）
* `src/` : Pythonのコアスクリプト（メインロジック、テキスト処理、音声生成など）
* `hyperframes-app/` : HyperFramesのプロジェクト（動画のレイアウト、テロップのアニメーション、CSSデザイン）
* `assets/` : システムが使用するBGMなどの静的アセット
* `work/` : 処理中に一時的に生成される中間ファイル（スライド画像やTTS音声など）
* `output/` : 最終的に完成した動画（MP4）の出力先

# Slide2Video AI Generator — AIエージェント向け運用手順書

> `AGENTS.md`（AntiGravity 等が読む）と `CLAUDE.md`（Claude Code が読む）は**同一内容**。片方を直したら必ずもう片方にコピーする。

このリポジトリは **AntiGravity などの AI エージェントが無人で実行する前提** で作られています。
人間は `inbox/` に素材を置いて「動画を作って」と言うだけ。エージェントの仕事は、この手順書どおりに実行し、**品質ゲートの結果を人間に報告する**ところまでです。「動いた」ではなく「品質ゲートが PASS した」を完了の基準にしてください。

## 0. 何をするリポジトリか

```
inbox/<動画名>/slides.pdf + script.md
  → src/main.py            0.preflight → 1.パース → 2.TTS（文単位） → 3.テロップ同期
  → src/template.html      4.コンポジション生成（見た目の本体。ここを編集する）
  → hyperframes-app/index.html   （自動生成。手で編集しない）
  → npx hyperframes render 5.レンダリング（1920×1080 / 30fps）
  → FFmpeg                 6.BGMベッド → ダッキング → SFX → 2パス loudnorm -14 LUFS
  → 品質ゲート             7.閾値で機械判定 → PASS / WARN / FAIL
  → output/<動画名>/final.mp4 + contact_sheet.jpg + build_summary.json
```

## 1. 動画を作る手順（毎回この順で）

1. **環境確認**: `./run.sh --check`（Windows: `run.bat --check`）
   → 終了コードが 0 以外なら、画面に出た「導入コマンド」を人間に案内して止まる（自分で `brew` / `choco` を実行してよいか確認する）
2. **素材確認**: `inbox/<動画名>/` に PDF が1つ・台本（`.md`）が1つあるか。無ければ人間に依頼する（台本の書き方は §7）
3. **BGM を選んでもらう（台本に `# BGM:` が無いとき・または「BGM 候補出して」と言われたとき）**
   1. `./run.sh --bgm-candidates --project <動画名>` → 台本のムードに合う **3曲** を Openverse（CC BY / CC0 の無料音源）＋手持ちライブラリから集め、冒頭20秒の「ナレーション＋BGM」プレビューを `output/<動画名>/bgm_candidates/1_*.mp3 … 3_*.mp3` に書き出す
   2. 人間に **候補表（曲名 / 作者 / ライセンス / 長さ）と3本のプレビューのパス** を提示し、番号を選んでもらう。自分で決めない
   3. 「N番で」と言われたら `./run.sh --bgm-choose N --project <動画名>` → 曲が `assets/bgm/<mood>/` に保存され、台本先頭に `# BGM: <ファイル名>` が書かれ、`output/<動画名>/credits.txt`（概要欄用クレジット）ができる
   4. 気に入る曲が無ければ `--bgm-query "<英語の検索語>"`（例: `"ukulele"` `"lofi"`）で出し直す
4. **実行**: `./run.sh`（1本だけなら `./run.sh --project <動画名>`）
   → 数分かかる。途中で止めない。ログは `work/<動画名>/run.log` にも残る
5. **結果確認（必須・省略禁止）**
   - 終了コード: `0`=PASS/WARN, `1`=実行エラー, `2`=環境不足, `3`=品質ゲート FAIL
   - `output/<動画名>/build_summary.json` の `status` と `checks[]`（PASS 以外の行を全部読む）
   - `output/<動画名>/contact_sheet.jpg` を**開いて目視**: テロップの切れ・黒帯・強調色・イントロ/アウトロが出ているか
6. **報告**: 人間に次を短く伝える — `status` / 尺 / LUFS / BGM の曲名 / TTS エンジン / WARN と FAIL の一覧 / 動画のパス。
   BGM が CC BY なら **`output/<動画名>/credits.txt` の1行を動画の概要欄に貼る**ことを必ず伝える。
   FAIL なら §2 の対処表に従って直し、再実行してから報告する

### 「リポジトリを更新して確認して」と言われたとき

1. `git pull origin main`（ローカル変更があれば人間に確認してから）
2. `./run.sh --check` → 環境不足があれば導入コマンドを案内
3. §1 の手順で BGM 候補を出し、人間が選んだら本番生成 → `build_summary.json` の `BGM` と `BGMバランス` が PASS であることを報告する（「ブーン」問題の修正確認はこの2行で判断できる）

## 2. WARN / FAIL の対処表

| 表示（checks の name / detail） | 意味 | 対処 |
|---|---|---|
| `BGM: BGM なし` (WARN) | この動画に BGM が割り当てられていない | §1-3 の手順で候補を出して選んでもらう（`./run.sh --bgm-candidates`）。曲なしでも納品は可能 |
| `--bgm-candidates` が「候補が見つかりません」（終了コード 3） | 検索語に合う CC BY/CC0 曲が無い、または Openverse が応答しない | `--bgm-query "<別の英語>"` で再検索。それでも駄目ならネット接続を確認し、手持ちライブラリ（`assets/bgm/<mood>/`）から選ぶ |
| `Openverse 検索失敗 … TimeoutError / HTTPError` (WARNING) | Openverse API が遅い・一時的に落ちている（よくある） | 自動で1回リトライ済み。数分待って再実行。手持ちライブラリに曲があればそれだけで候補が出る |
| `BGM: … 持続音の可能性（LRA < 1.2）` (WARN) | 音楽ではなく合成音・環境音が置かれている | その曲を外して別の曲を入れる。**合成音を BGM にしない** |
| `TTS: … edge-tts にフォールバック` (WARN) | Fish Audio が失敗（401=キー失効 / 402=残高不足 / ネット） | `.env` の `FISH_AUDIO_API_KEY` 更新を人間に依頼。無料の edge-tts 音声のまま納品してよいか確認する |
| `TTS: … 無音で代替` (FAIL) | TTS が両方失敗（ネット不通） | 接続を直して再実行 |
| `台本: スライド N は台本がない…` (WARN) | `# Slide N` の本文が無い | 意図的か人間に確認（そのスライドは動画に入らない） |
| `ラウドネス` (WARN/FAIL) | -14 LUFS から乖離 | 再実行。続くなら `work/<動画名>/run.log` の測定値を添えて報告 |
| `トゥルーピーク` (FAIL) | ピークが -1.0 dBTP 超 | 再実行。続くなら報告（`src/video-style.json` の `audio.loudness_true_peak`） |
| `尺` (FAIL) | レンダ結果の長さが期待と違う | `run.log` のレンダ部分を確認して再実行 |
| `テロップ: 0枚` (FAIL) | テロップが1枚も生成されない | 台本の本文が空でないか確認 |
| `lint: エラー N件` (WARN) | コンポジションの不整合 | `npm run check` で詳細を見て `src/template.html` / `src/main.py` の生成部を直す |
| 終了コード `2` | ffmpeg / poppler / node 等が無い | 表示された導入コマンドを案内 |
| 終了コード `1` | 例外で停止 | `work/<動画名>/run.log` の `[ERROR]` と traceback を読む |

## 3. 品質が毎回担保される仕組み（壊さないこと）

| 仕組み | 内容 |
|---|---|
| preflight | 実行前に ffmpeg / ffprobe / pdftoppm / node / npx / edge-tts / Fish キー / BGM の有無を確認 |
| 文単位 TTS | 台本を文で区切って音声化し結合 → 文境界の実タイムスタンプでテロップを同期（whisper 不要） |
| 音響チェーン | BGM -18dB 事前レベル → sidechaincompress(0.03 / 8:1 / 20ms / 400ms) → SFX → `amix normalize=0` → 2パス loudnorm **-14 LUFS / TP -1.5** |
| BGM ベッド | 曲が短ければ `acrossfade` 2秒で連結（継ぎ目のプツッを防止）。フェードイン2秒 / アウト4秒。LRA<1.2 の持続音は警告 |
| テロップ | 放送基準: 1行16文字 × 2行、白文字＋黒縁＋座布団、即時表示、音声より 80ms 先行、読点＞助詞境界で改行 |
| 映像 | Ken Burns 6%（in→left→out→right ローテーション）、クロスフェード 0.6秒、ぼかし拡大背景で黒帯なし |
| 決定性 | TTS はテキスト＋エンジン＋声のハッシュでキャッシュ、BGM はタイトルのハッシュで選曲、乱数不使用。同じ入力→同じ `index.html` |
| バージョン固定 | `requirements.txt` は `==` 固定、HyperFrames CLI は `hyperframes-app/package.json` にピン（`get_pinned_cli` が使う） |
| 品質ゲート | 尺 / ストリーム / ラウドネス / トゥルーピーク / BGM / TTS / テロップ / lint / 台本整合 を毎回判定し `build_summary.json` に記録 |

## 4. 見た目・設定を変えたいとき

| 変えたいもの | 場所 |
|---|---|
| 動画ごとの雰囲気・BGM・タイトル | `inbox/<動画名>/script.md` の先頭行: `# Title:` / `# Style: modern\|pop\|calm\|serious` / `# BGM: <ムード or ファイル名 or none>`（`--bgm-choose` が自動で書く） |
| 動画ごとの細かい上書き | `inbox/<動画名>/style.json`（`src/video-style.json` と同じ構造で一部だけ書く） |
| 全動画の既定値（色・字幕サイズ・音量・閾値） | `src/video-style.json`（単一の情報源） |
| デザイン・アニメーション | `src/template.html` と `src/main.py` の `generate_hyperframes_config` |
| 字幕を出さない | `design.subtitles_enabled: false` |

変更後は必ず: 1本生成 → `cd hyperframes-app && npm run check` がエラー 0 → `contact_sheet.jpg` を目視。

## 5. HyperFrames コンポジションのルール（`src/template.html` と生成クリップ）

Claude Code で作業するときは、コンポジションを触る前に `/hyperframes-core` スキルを読む（`data-*` 契約が一般の Web 知識と異なる）。

1. 時間を持つ要素は `data-start` / `data-duration` / `data-track-index` と `class="clip"` が必須
2. タイムラインは `gsap.timeline({ paused: true })` を1本だけ `window.__timelines["main"]` に登録
3. クリップ本体の opacity はフレームワーク所有 → アニメは内側ラッパー（`.scene-inner` / `.kb-wrap`）にかける
4. 決定的ロジックのみ（`Date.now()` / `Math.random()` / ネットワーク取得 禁止）
5. `tl.fromTo()` で開始状態を明示（seek 安全）。裸の `tl.from()` は使わない
6. `id` は生成側で一意に付ける（`scene-N` / `slide-N` / `voice-N` / `sub-N-K` / `intro-card` / `outro-card`）
7. 音声は `<audio class="clip">`。BGM / SFX は FFmpeg 段で合成する（コンポジションには入れない）

## 6. コマンド

```bash
./run.sh --check                 # 環境チェックのみ（Windows: run.bat --check）
./run.sh                         # inbox/ 全部を処理
./run.sh --project <動画名>      # 1本だけ
./run.sh --strict                # lint エラーがあればレンダ前に停止
./run.sh --bgm-candidates --project <動画名> [--bgm-query "英語"] [--count 3]   # BGM 候補3曲＋プレビュー
./run.sh --bgm-choose N --project <動画名>                                    # 候補 N を採用（台本に # BGM: を書く）
cd hyperframes-app && npm run check    # lint + runtime + layout + motion + contrast
cd hyperframes-app && npm run dev      # プレビューサーバ（常駐。Claude Code では run_in_background）
./setup.sh                       # 初回セットアップ（Windows: setup.bat）
```

## 7. 台本フォーマット（`inbox/<動画名>/script.md`）

```markdown
# Title: 動画タイトル（イントロカード用。省略時はフォルダ名）
# Style: pop
# BGM: upbeat

# Slide 1
ナレーション本文。**強調したい語**はテロップで黄色になる（音声には影響しない）。

# Slide 2
文は「。！？」で区切る。1文ごとに音声化されテロップが同期する。
```

## 8. やってはいけないこと

- 合成音（`sine` / `anullsrc` 等で作った音）を `assets/bgm/` に置かない → 動画全体が「ブーン」になる事故が過去にあった。BGM は必ず音楽ファイル（`assets/bgm/README.md`）
- `hyperframes-app/index.html` を手で編集しない（毎回上書きされる）
- `.env`・`work/`・`output/`・`inbox/` の中身・ルートの `.mp4`・**`assets/bgm/` の音楽ファイル**をコミットしない（`.gitignore` 済み）。このリポジトリは**公開**で、フリー素材の再配布はライセンス違反になる。BGM は PC ごとに置き、`assets/bgm/LICENSES.md` だけコミットする
- `npx hyperframes` をピン無しで実行しない（`hyperframes-app/package.json` のピンを使う。更新は `npx hyperframes@latest upgrade --project hyperframes-app --check` で差分確認してから）
- 品質ゲートが FAIL のまま「完成」と報告しない
- BGM を人間に選ばせずに勝手に決めない（`--bgm-candidates` → 提示 → 「N番で」→ `--bgm-choose N`）。CC BY の曲を使った動画は概要欄に `credits.txt` を貼るよう必ず伝える
- Openverse 以外の素材サイト（DOVA 等）をスクリプトで自動取得しない（規約で自動ダウンロード禁止のことが多い）

## 9. 参考

- 目指す完成イメージ: ルートの `Video Project 3.mp4`（git 管理外・参考動画）
- 引き継ぎ: `HANDOFF.md`（目的 → 現状 → 次 → 注意 → ファイル → 検証）
- 数値基準の出典: 放送テロップ基準・YouTube ラウドネス基準（`README.md` の「品質担保の項目一覧」）

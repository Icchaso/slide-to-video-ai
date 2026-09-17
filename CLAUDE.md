# Slide2Video — Claude Code 用の地図とルール

スライド（PDF）と台本（`.md`）を `inbox/<動画名>/` に置くと、ナレーション・テロップ・BGM 付きの動画ができる。
**このリポジトリは Claude Code 専用。** 動画を作る手順は `make-video` スキル（`.claude/skills/make-video/SKILL.md`）にある。「動画作って」と言われたらまずそれを読む。

台本とスライドは **このツールでは作らない**（別スキルの担当）。持ち込まれたものを一番分かりやすい動画にするのが仕事。

## 役割分担
| 担当 | 何をするか |
|---|---|
| エンジン `./run.sh` → `src/main.py` | 決まった処理を毎回同じように回す（TTS・テロップ同期・レンダ・音の処理・品質ゲート） |
| Claude（`make-video` スキル） | スライドを見て判断する・BGM 候補を人に選ばせる・書き出し後にコマを見て直す・報告する |
| `video-reviewer` エージェント | コマ画像と台本を見て指摘だけ返す（読み取り専用） |

## 地図
```
inbox/<動画名>/slides.pdf + script.md                   ← 入力（git 管理外。作らない・書き換えない）
        + storyboard.json（Claude が書く絵コンテ）+ reading.json（読み方辞書）+ style.json
  → src/main.py            preflight → パース → 文単位TTS → テロップ同期
  → src/template.html      コンポジション（見た目の本体。ここを編集する）
  → hyperframes-app/index.html   自動生成（手で編集しない）
  → npx hyperframes render 1920×1080 / 30fps
  → FFmpeg                 BGMベッド → ダッキング → SFX → 2パス loudnorm -14 LUFS
  → 品質ゲート             PASS / WARN / FAIL
  → output/<動画名>/final.mp4 + contact_sheet.jpg + build_summary.json
```

| 変えたいもの | 場所 |
|---|---|
| 声の読み方（テロップは変えない） | `inbox/<動画名>/reading.json`（make-video スキル ②'） |
| 場面ごとの演出（テロップ位置・文の間・囲み/下線/スポットライト/寄り） | `inbox/<動画名>/storyboard.json`（書式は make-video スキル ③） |
| 全動画の既定値（色・字幕・音量・閾値） | `src/video-style.json`（単一の情報源） |
| 動画ごとの上書き | `inbox/<動画名>/style.json`、台本先頭の `# Title:` `# Style:` `# BGM:` |
| デザイン・アニメーション | `src/template.html` と `src/main.py` の `generate_hyperframes_config` |
| BGM 検索 | `src/bgm_search.py` |

## コマンド
```bash
./run.sh --check                                  # 環境チェック（終了コード 2 = 不足）
./run.sh --draft --project <動画名>                # 下書き: テロップごとのコマを work/<動画名>/review/ に撮る（数十秒）
./run.sh --voice-check --project <動画名>          # 読み上げチェック: 文字起こしと台本を読みで照合 → work/<動画名>/voice_check.md
./run.sh --project <動画名>                        # 1本作る（数分。run_in_background で待つ）
./run.sh --bgm-candidates --project <動画名> [--bgm-query "英語1語"]   # BGM を最大8曲集め、おすすめ3曲（理由つき）＋全曲プレビュー
./run.sh --bgm-choose N --project <動画名>         # 候補 N を採用
./run.sh --strict                                 # lint エラーでレンダ前に停止
cd hyperframes-app && npm run check               # lint + runtime + layout + motion + contrast
```
終了コード: `0`=PASS/WARN / `1`=実行エラー / `2`=環境不足 / `3`=品質ゲート FAIL

## 絶対ルール
- **リポジトリは公開**。`.env`・`work/`・`output/`・`inbox/` の中身・ルートの `.mp4`・`assets/bgm/` の音楽ファイルはコミットしない（`.gitignore` 済み。フリー素材の再配布はライセンス違反）。BGM の出典は `assets/bgm/LICENSES.md` だけコミットする
- 合成音（`sine` / `anullsrc` 等）を `assets/bgm/` に置かない（動画全体が「ブーン」になる事故が過去にあった）
- BGM は人間に番号で選ばせる。CC BY の曲は `credits.txt` を概要欄に貼るよう伝える
- 品質ゲート FAIL・自己レビュー未実施のまま「完成」と報告しない
- `npx hyperframes` はピン無しで実行しない（`hyperframes-app/package.json` のピンを使う）
- 決定性を壊さない（`Date.now()` / `Math.random()` / レンダ中のネット取得を使わない。同じ入力なら `index.html` の md5 が一致）

## HyperFrames コンポジションを触るとき
着手前に `/hyperframes-core`（必要なら `/hyperframes-animation`）を読む。`data-*` の決まりが一般の Web と違う。
1. 時間を持つ要素は `data-start` / `data-duration` / `data-track-index` と `class="clip"` が必須
2. タイムラインは `gsap.timeline({ paused: true })` を1本だけ `window.__timelines["main"]` に登録
3. クリップ本体の opacity はフレームワーク所有 → アニメは内側ラッパー（`.scene-inner` / `.kb-wrap`）にかける
4. `tl.fromTo()` で開始状態を明示（裸の `tl.from()` は使わない）
5. `id` は生成側で一意に付ける（`scene-N` / `slide-N` / `voice-N` / `sub-N-K` / `intro-card` / `outro-card`）
6. 音声は `<audio class="clip">`。BGM / SFX は FFmpeg 段で合成する
7. 音声の `data-duration` は ms 切り上げ（切り捨てると `duplicate_audio_track` が再発する）

変更後は必ず: 1本生成 → `npm run check` エラー 0 → コマを目視（自己レビュー）。

## 参考
- 目指す完成イメージ: ルートの `Video Project 3.mp4`（git 管理外）
- 引き継ぎ: `HANDOFF.md`
- AntiGravity から乗り換える人向け: `docs/AntiGravityからClaudeCodeへ.md`

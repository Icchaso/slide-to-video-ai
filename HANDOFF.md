# HANDOFF — Slide2Video 全体改善

## 0. 進行中: Claude Code 専用化（2026-09-17〜 プラン `~/.claude/plans/glowing-stargazing-fog.md`）

**目的**: AntiGravity 向けの「手順を回すだけ」から、Claude がスライドとコマを見て直す作りへ。台本・スライドの生成は別スキルの担当で、このツールは取り込むだけ（いっちゃん決定）。

| Phase | 内容 | 状態 |
|---|---|---|
| 0 | CLAUDE.md（地図とルール）・`make-video` スキル・`video-reviewer` エージェント・`.claude/settings.json`・AGENTS.md を切り替え案内に・`docs/AntiGravityからClaudeCodeへ.md` | 完了 c339255 |
| 1 | `storyboard.json`（場面ごとの subtitle: bottom/top/band/off）・`--draft`（全尺を書き出さず `hyperframes snapshot --at` でテロップごとのコマ → `work/<名前>/review/`）・「という」の途中で改行しない | 完了 2611d02 |
| 2 | 声: 読み方辞書 `reading.json`（音声だけ置換・テロップ不変）・`--voice-check`（文ごとに文字起こし→台本と**ひらがなの読み**で照合、⚠️=一致率<0.9 か2文字以上のずれ）・storyboard の `gap`（場面ごとの文間） | 完了 b6aab39。**自分の声は未**: Fish の新キー＋録音1〜2分待ち → `POST /model`（private）で登録 → `.env` の `FISH_AUDIO_VOICE_ID` |
| 2.5 | レイアウト標準（いっちゃん指定）: スライドは画面いっぱい、テロップは下20%（216px）の中に重ねる `band` が既定・文字52px・Ken Burns 既定オフ（全面スライドの端が切れるため）。スライドの中身は上80%に置く約束（スライドを作る側） | 完了 b6aab39 |
| 2.9 | Fable 品質レビュー（11件）の反映: 「もっ\|と」改行の退行・100コマ超でコマと字幕の対応ずれ・辞書を入れると照合が常に⚠️・preflight に pykakasi・draft/voice-check のログ分離・SKILL/README の食い違い・`voice-sample/` を gitignore。**未反映 #10**: settings.json に `--voice-check` / `--bgm-candidates` の許可を足す案（許可範囲の拡大なのでいっちゃん判断待ち） | 完了（下のコミット） |
| 3 | 演出: zoom / spotlight / marker（Claude がスライドを見て storyboard に書く）。全面スライドでも端が切れず下20%に食い込まない動きにする（Ken Burns は既定オフにした） | 未着手 |
| 4 | README・判断記録・メモリ更新・共有相手への切り替え案内文 | 未着手 |

**Phase 1 の検証結果**: storyboard 無しで従来と見た目一致（スナップショット差 最大1/255）・index.html 決定性 md5 一致・storyboard 誤りは項目つきでエラー・test-project でスライド3を band にしてグラフの項目名が見えることをレビュー役と目視で確認・本番 PASS（-14.01 LUFS）・`npm run check` エラー0。
**残課題**: `npm run check` の contrast 警告1件（イントロ→スライド切替の t=3.2s でタイトル 1.3:1。以前からあるかは未確認）。
**注意**: セッション途中で作ったエージェントは次のセッションから読み込まれる。**`.env` の Fish Audio キーは失効中（401 Invalid Token）**。品質ゲートの `TTS: fish` PASS はキャッシュ再利用で新規生成0文だったため。新しい文は edge-tts にフォールバックする。声の録音は `assets/voice/`（.gitignore 済み・個人の声なので絶対にコミットしない）。

---

## 以下、2026-09-02 時点（BGM根治・品質ゲート・AntiGravity無人実行対応）

最終更新: 2026-09-02

## 1. 目的

「PDFスライド＋台本 → 動画」パイプラインを、**AntiGravity 等の AI エージェントが無人で実行しても毎回同じ品質が出る**状態にする。
発端は、いっちゃんの「BGM が入らず背後でブーンと鳴る」報告。原因は BGM 素材そのものが ffmpeg 合成の持続音だったこと（音楽ファイルが1つも無かった）。

## 2. 現状（完了）

- **リポジトリ統合**: 8/14 に `~/プロジェクト/Git編集ルーム/slide-to-video-ai` で行われた4コミット（未push）を GitHub に push し、正本 `ICHI DESIGN/06_ツール/動画編集ツール` に pull。以後はこのフォルダだけで作業する
- **BGM 根治**: 合成ドローン `ambient_chord.mp3` を削除。`assets/bgm/<mood>/` に音楽ファイルを置く運用（公開リポのため git 管理外・`LICENSES.md` に出典記録）。`build_bgm_bed()` が曲長に応じて `acrossfade` 連結・フェード・-18dB 事前レベルを適用。台本の `# BGM: <mood|ファイル名|none>` で指定可。LRA<1.5 LU の持続音は品質ゲートが警告
- **文単位 TTS**: 台本を文で区切って音声化→結合し、文境界の実タイムスタンプでテロップ同期（whisper 不要・既定 off）。実測: 境界誤差 0.1 秒以内（`silencedetect` の無音区間内）。TTS キャッシュキーにエンジン/声/モデル/速度を含めるバグ修正
- **Fish Audio**: ペイロードを公式仕様に合わせた（`format: wav` / `model` ヘッダ / `prosody`）。**ただし `.env` のキーが失効**（API が `Token expired`）。現状は edge-tts に自動フォールバックし、品質ゲートに WARN が出る
- **品質ゲート**: 尺 / ストリーム / ラウドネス(-14±0.7) / TP / BGM / TTS / テロップ / lint / 台本整合 を毎回判定 → `output/<proj>/build_summary.json`（PASS/WARN/FAIL）＋ `contact_sheet.jpg`（12コマ）。終了コード 0/1/2/3
- **preflight**: `./run.sh --check` で ffmpeg/poppler/node/edge-tts/キー/BGM を確認し、不足は導入コマンド付きで表示
- **テロップ改行**: 読点 → ひらがな→非ひらがな遷移（文節境界近似）→ 助詞 → 文字種遷移 の4段階候補、「」内・敬語接頭辞・カタカナ語の途中では切らない
- **lint 対応**: 安定 id（scene-N / slide-N / voice-N / sub-N-K）、音声クリックの ms 切り上げ＋交互トラック、ぼかし背景は 1/4 縮小 JPEG の別ファイル → `npm run check` エラー 0
- **HyperFrames CLI** 0.7.108 → 0.8.24、`requirements.txt` バージョン固定
- **ドキュメント**: `AGENTS.md`（= `CLAUDE.md`）を AntiGravity 向け運用手順書に全面改訂（手順・WARN/FAIL 対処表・やってはいけないこと）。README も実態に更新
- **検証**: 2プロジェクト（test-project 4枚 / テスト動画 8枚中4枚に台本）で E2E 通過、-14.0 LUFS、決定性（同一入力で index.html の md5 一致）確認済み
- **BGM「3候補 → 選ぶ」ワークフロー（2026-09-02 追加）**: `./run.sh --bgm-candidates --project X` が Openverse API（CC BY / CC0 のみ・無料・キー不要）＋手持ちライブラリから3曲を集め、冒頭20秒の「ナレーション＋BGM」プレビューを `output/X/bgm_candidates/` に書き出す。`./run.sh --bgm-choose N --project X` で採用 → `assets/bgm/<mood>/` に保存・sidecar JSON・`LICENSES.md` 追記・台本先頭に `# BGM:`・`output/X/credits.txt`（概要欄用クレジット）。本番の品質ゲートに `クレジット` 行を追加。HeyGen 無料枠は規約で商用不可のため不採用。持続音閾値 `qa.bgm_lra_min` は 1.5→1.2（本物のループ曲が 1.3〜1.7 のため）

## 3. 次にやること

- [ ] **いっちゃん**: Fish Audio の新 API キーを発行し `.env` の `FISH_AUDIO_API_KEY` を差し替え（fish.audio → Settings → API Keys）。差し替え後 `./run.sh --project test-project` で `TTS: fish` の PASS を確認
- [ ] **いっちゃん**: test-project の BGM 候補3曲（送付済みプレビュー）から番号を決める（検証用に仮で 2 番を採用済み。変更は `./run.sh --bgm-choose N --project test-project` → `./run.sh --project test-project`）
- [ ] （任意）手持ちのフリー BGM を `assets/bgm/<mood>/` に置くと候補に混ざる（`LICENSES.md` に記録）
- [ ] Git編集ルームのクローン `~/プロジェクト/Git編集ルーム/slide-to-video-ai` は統合済みなので削除候補（いっちゃん判断）
- [ ] （任意）TOALU 不動産ショート動画生成ツールの BGM 5曲も同じ合成ドローン → 別途対応
- [ ] （任意）Google Fonts のローカル同梱（オフライン環境向け。現状はレンダ時にネット取得）

## 4. 注意

- `hyperframes-app/index.html` は**毎回自動生成**。見た目は `src/template.html` と `src/video-style.json` で変える
- `AGENTS.md` と `CLAUDE.md` は同一内容。片方を直したら `cp -p AGENTS.md CLAUDE.md`
- リポジトリは**公開**。`.env` / BGM 音楽ファイル / `work` / `output` / `inbox` の中身 / ルート `.mp4` はコミットしない（`.gitignore` 済み）
- 参考動画 `Video Project 3.mp4`（目指す完成イメージ）はルートに git 管理外で保持。`git pull` で消えることはもう無い（追跡解除済み）
- 動画に台本の無いスライドは入らない（WARN で通知）。テスト動画は 8枚中 4枚のみ台本あり
- lint は wav の**実長**でクリップ重なりを判定する → 音声の `data-duration` は ms 切り上げ（`math.ceil`）にしてある。切り捨てに戻すと `duplicate_audio_track` が再発する
- Openverse API は**遅い・不安定**（18秒かかる／504・500 が出ることがある）。`search_openverse` は 45秒タイムアウト＋1回リトライ、失敗しても手持ちライブラリだけで続行する。検索語は**単語1つ**が有効（2語以上の AND 検索は 0 件になりやすい）。`pdm` をライセンス指定に含めると 0 件
- `--bgm-choose` は `inbox/<X>/script.md` の先頭 `# BGM:` 行だけを書き換える（本文は触らない）
- 候補検索は毎回結果が変わりうるが、選んだ曲はローカル保存されるので本番生成は決定的

## 5. 主要ファイル

| ファイル | 役割 |
|---------|------|
| `src/main.py` | パイプライン本体（preflight → parse → TTS → compose → render → mix → quality_gate）＋ `bgm_candidates` / `bgm_choose` |
| `src/bgm_search.py` | Openverse 検索・スコアリング（vocal 除外・速度タグ・作者重複なし）・DL 検証・sidecar JSON・クレジット文 |
| `src/template.html` | コンポジション（CSS / GSAP） |
| `src/video-style.json` | 全既定値・プリセット・品質ゲート閾値 |
| `AGENTS.md` = `CLAUDE.md` | AI 向け運用手順書（対処表つき） |
| `assets/bgm/README.md` `LICENSES.md` | BGM 投入ルールと出典台帳 |
| `run.sh` / `run.bat` | 実行入口（終了コードを表示） |
| `work/<proj>/run.log` | 実行ログ（AntiGravity 上で原因を追う用） |
| `output/<proj>/` | `final.mp4` / `contact_sheet.jpg` / `build_summary.json` |

## 6. 検証方法

```bash
./run.sh --check                                   # 環境OK / BGM 曲数 / TTS エンジンが出る
./run.sh --project test-project                    # E2E。終了コード 0、build_summary.json の status
cd hyperframes-app && npm run check                # エラー 0（警告は timeline_track_too_dense のみ許容）
ffmpeg -i output/test-project/final.mp4 -af ebur128 -f null - 2>&1 | tail -8   # I: -14.0 LUFS
open output/test-project/contact_sheet.jpg         # テロップ切れ・黒帯・強調色・イントロ/アウトロ
./run.sh --bgm-candidates --project test-project    # 3本の 20s プレビュー（-14 LUFS 付近）と candidates.md
./run.sh --bgm-choose 2 --project test-project      # assets/bgm/<mood>/ に保存・LICENSES.md・# BGM: 行・credits.txt
```

決定性: 同じ入力で2回コンポジション生成し `md5 -q hyperframes-app/index.html` が一致すること（TTS はキャッシュされるため2回目以降は完全一致）。

## 7. 完了コミット（2026-09-02）

| ハッシュ | 内容 |
|---|---|
| 5a4ec57 | BGM: 合成ドローン削除・フリー音楽運用 |
| d66f792 | HyperFrames CLI 0.8.24 |
| 130a8ab | パイプライン改修（文単位TTS・BGMベッド・品質ゲート・preflight） |
| 7e8f256 | AGENTS.md / README / run.sh / HANDOFF 更新 |
| 891be83 | BGM「3候補→選ぶ」（Openverse・プレビュー・credits・BGMバランス計測） |
| b8f6fda | AGENTS/README/HANDOFF 更新（候補ワークフロー・共有相手向け確認手順） |

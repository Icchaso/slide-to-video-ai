# HANDOFF — Slide2Video 全体改善（BGM根治・品質ゲート・AntiGravity無人実行対応）

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

## 3. 次にやること

- [ ] **いっちゃん**: Fish Audio の新 API キーを発行し `.env` の `FISH_AUDIO_API_KEY` を差し替え（fish.audio → Settings → API Keys）。差し替え後 `./run.sh --project test-project` で `TTS: fish` の PASS を確認
- [ ] **いっちゃん**: フリー BGM（DOVA-SYNDROME 等・商用可・ボーカルなし・15秒以上）を `assets/bgm/relaxing/` `upbeat/` `serious/` に各2〜3曲投入し `assets/bgm/LICENSES.md` に記録。投入後に1本生成して耳で確認（品質ゲートの `BGMバランス` が声より 6dB 以上下がっていれば OK）
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

## 5. 主要ファイル

| ファイル | 役割 |
|---------|------|
| `src/main.py` | パイプライン本体（preflight → parse → TTS → compose → render → mix → quality_gate） |
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
```

決定性: 同じ入力で2回コンポジション生成し `md5 -q hyperframes-app/index.html` が一致すること（TTS はキャッシュされるため2回目以降は完全一致）。

## 7. 完了コミット（2026-09-02）

| ハッシュ | 内容 |
|---|---|
| 5a4ec57 | BGM: 合成ドローン削除・フリー音楽運用 |
| d66f792 | HyperFrames CLI 0.8.24 |
| 130a8ab | パイプライン改修（文単位TTS・BGMベッド・品質ゲート・preflight） |
| 7e8f256 | AGENTS.md / README / run.sh / HANDOFF 更新 |

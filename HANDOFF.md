# HANDOFF — Slide2Video 品質大幅アップグレード

最終更新: 2026-08-14

## 1. 目的

「PDFスライド＋台本 → 動画」パイプラインの編集クオリティをプロ水準へ引き上げる。プロの映像編集・音響仕上げをリサーチ（外交くん2本）し、その数値基準（放送テロップ基準・-14 LUFS・クロスフェード0.5〜1s等）をシステムに実装した。加えて「動画ごとの雰囲気プリセット切替」「Mac/Windows両対応のわかりやすいセットアップ」に対応。

## 2. 現状（完了）

- **コンポジション刷新** (`src/template.html` + `src/main.py`):
  - Ken Burns（1.06倍・スライドごとに in→left→out→right ローテーション）
  - クロスフェード転換（0.6s・z-index管理で dip-to-black なし）
  - ぼかし拡大背景（レターボックス黒帯の解消、blur 22px）
  - 座布団付きテロップ（1行16文字×2行・白文字黒縁・半透明黒帯・即時表示=放送標準）
  - whisper同期テロップ（`hyperframes transcribe --json` 文単位セグメント＋文内は文字数比例補間。失敗時は文字数比推定にフォールバック）
  - `**強調**` 記法 → 金色ハイライト、改行は読点＞助詞境界を優先
  - イントロ/アウトロカード（タイトル自動、躍動系ダーク×グラデ×グロー）
- **音響チェーン** (`src/main.py` の FFmpeg 4段):
  SFXミックス（whoosh転換・impactイントロ）→ BGM事前-18dB＋sidechaincompress(threshold 0.03/ratio 8/attack 20ms/release 400ms) → 2パスloudnorm **-14 LUFS/TP-1.5** → 映像copy+AAC384k+faststart
- **プリセット機構**: 台本に `# Style: pop` 等の1行で切替（modern/pop/calm/serious）。`inbox/<proj>/style.json` で個別上書き、全既定値は `src/video-style.json`
- **信頼性**: TTSキャッシュ（テキストhash）・BGM決定的選択（タイトルhash）・レンダはピン版CLI使用・edge-tts依存追加（未インストールバグ修正）
- **検証結果**: `npm run check` 合格（エラー0・コントラスト8/8 AA）。最終ラウドネス実測 -13.99 LUFS。テロップ同期 whisper モード動作。フレーム目視でイントロ/座布団/ハイライト/クロスフェード/ぼかし背景を確認
- **リポジトリ整理**: 16MBサンプル動画をgit除外、rootの空HyperFramesスケルトンを `_archive/` へ、README全面刷新（Mac/Win並記）、CLIピン 0.7.106→0.7.108

## 3. 次にやること（任意・未完了）

- [ ] **BGMライブラリ拡充**: 現状 `assets/bgm/relaxing/ambient_chord.mp3` の1曲のみ。upbeat/serious は空フォルダ。media-use skill の resolve（要HeyGen CLI認証）か手持ちmp3の投入で各ムード2〜3曲へ
- [ ] Fish Audio APIキーでの本番TTS検証（今回はedge-ttsフォールバックで検証済み。Fish経路の新パラメータ speed/temperature は**未検証**）
- [ ] 長尺・多スライド（10枚超）での負荷確認
- [ ] BGMループの継ぎ目 acrossfade 処理（現状は -stream_loop、環境音系BGMなら問題なし）

## 4. 注意

- `hyperframes-app/index.html` は **毎回自動生成**。見た目の変更は `src/template.html` と `src/video-style.json` で行う
- テロップ同期は日本語では「文単位セグメント」（whisperのword分割はスペース区切り言語のみ）。文内は比例補間で実用精度
- 初回実行時に whisper small モデル（約466MB）が自動ダウンロードされる
- クリップ本体のopacityはHyperFrames所有 → アニメは必ず内側ラッパー（`.scene-inner`/`.kb-wrap`）に
- テロップの `data-start/duration` はミリ秒丸めの整合を取ってから出力（1ms重複でlintに落ちる）

## 5. 主要ファイル

| ファイル | 役割 |
|---------|------|
| `src/main.py` | パイプライン本体（パース→TTS→transcribe→コンポジション生成→レンダ→音響仕上げ） |
| `src/template.html` | コンポジションテンプレート（CSS/GSAP、デザインの本体） |
| `src/video-style.json` | 全スタイル既定値＋プリセット定義（単一情報源） |
| `assets/sfx/` | 同梱SFX（whoosh-short/impact-bass-1等6種） |
| `inbox/test-project/` | E2E検証用テスト素材（PDF4枚＋台本） |
| `work/<proj>/` | 中間生成物（音声キャッシュ・transcribe キャッシュ・render.mp4・ログ） |

## 6. 検証方法

```bash
./run.sh                              # E2E: output/test-project/final.mp4 が生成される
cd hyperframes-app && npm run check   # コンポジション検証（エラー0が合格）
ffmpeg -i output/test-project/final.mp4 -af ebur128 -f null - 2>&1 | tail -8  # ラウドネス実測
```

決定性: 同じ入力で2回実行し `md5 -q hyperframes-app/index.html` が一致すること（TTS/transcribeはキャッシュされるため2回目以降は完全一致）。

#!/usr/bin/env python3
"""
Slide2Video AI Generator — パイプライン本体

inbox/<project>/(slides.pdf + script.md)
  → 0. preflight（環境チェック）
  → 1. パース（PDF→PNG / 台本→ブロック）
  → 2. TTS（文単位で生成 → 結合。文境界の実タイムスタンプを得る）
  → 3. テロップ分割・タイミング（文境界に同期）
  → 4. HyperFrames コンポジション生成（hyperframes-app/index.html）
  → 5. レンダリング（lint → render）
  → 6. 音響仕上げ（BGMベッド生成 → ダッキング → SFX → 2パス loudnorm -14 LUFS）
  → 7. 品質ゲート（閾値で機械判定 → build_summary.json に PASS/WARN/FAIL）
  → output/<project>/final.mp4 + contact_sheet.jpg + build_summary.json

Claude Code（make-video スキル）が判断役として回す前提:
  - storyboard.json（Claude がスライドを見て書く演出指示）を読み、無ければ従来どおりの出力
  - --draft で全尺を書き出さずにテロップごとのコマを撮り、Claude が見て直してから本番を書き出す
  - 環境不足は preflight が「導入コマンド」付きで即座に報告する（終了コード 2）
  - 品質ゲートは毎回同じ基準で判定し、FAIL は終了コード 3 で失敗扱いにする
  - 同じ入力からは同じ出力（決定性）: ハッシュキャッシュ・決定的選曲・乱数不使用

終了コード: 0 = 全て PASS/WARN / 1 = 実行エラー / 2 = 環境不足 / 3 = 品質ゲート FAIL
"""

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv
from pdf2image import convert_from_path
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bgm_search  # noqa: E402  Openverse 検索・sidecar・クレジット

load_dotenv()

# --- パス定義 ---
BASE_DIR = Path(__file__).resolve().parent.parent
INBOX_DIR = BASE_DIR / "inbox"
OUTPUT_DIR = BASE_DIR / "output"
WORK_DIR = BASE_DIR / "work"
ASSETS_DIR = BASE_DIR / "assets"
STYLE_FILE = BASE_DIR / "src" / "video-style.json"
TEMPLATE_FILE = BASE_DIR / "src" / "template.html"
APP_DIR = BASE_DIR / "hyperframes-app"

BGM_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")
MOODS = ("relaxing", "upbeat", "serious")
FISH_TTS_URL = "https://api.fish.audio/v1/tts"
EDGE_VOICE = "ja-JP-NanamiNeural"

EXIT_OK, EXIT_ERROR, EXIT_PREFLIGHT, EXIT_QA_FAIL = 0, 1, 2, 3

# TTS 1文ごとの前後無音を刈る（先頭 60ms / 末尾 150ms だけ残す）。文境界のタイムスタンプ精度に直結する
TRIM_FILTER = ("silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.06,"
               "areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.15,areverse")


class PreflightError(Exception):
    pass


# =====================================================================
# ログ・コマンド・計測ユーティリティ
# =====================================================================

_LOG_FILE = None


def set_log_file(path):
    """プロジェクトごとの run.log にも同じ内容を書く（AntiGravity 上で後から原因を追えるように）"""
    global _LOG_FILE
    _LOG_FILE = Path(path) if path else None
    if _LOG_FILE:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _LOG_FILE.write_text("", encoding="utf-8")


def log(msg):
    line = f"[slide-video] {msg}"
    print(line, flush=True)
    if _LOG_FILE:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def run_command(cmd, cwd=None, capture=False, timeout=None):
    log(f"Running command: {' '.join(str(c) for c in cmd)}")
    if capture:
        return subprocess.run(cmd, check=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return subprocess.run(cmd, check=True, cwd=cwd, timeout=timeout)


def run_ffmpeg(args):
    """ffmpeg を静かに実行し、失敗時は stderr を例外メッセージに含める"""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + [str(a) for a in args]
    log(f"Running command: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 失敗 (exit {result.returncode}): {result.stderr[-600:]}")
    return result


def ffprobe_duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True)
    out = result.stdout.strip()
    return float(out) if out else 0.0


def ffprobe_stream_types(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return set(result.stdout.split())


def measure_loudness(path):
    """loudnorm 1パス目: 測定値JSON（input_i / input_tp / input_lra / input_thresh / target_offset）"""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af",
         "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True)
    m = re.search(r'\{[^{}]*"input_i"[^{}]*\}', result.stderr, re.DOTALL)
    if not m:
        raise RuntimeError("loudnorm測定値の取得に失敗")
    return json.loads(m.group(0))


def measure_ebur128(path):
    """EBU R128 の統合ラウドネス(I)・ラウドネスレンジ(LRA)を返す。LRA が極端に小さい = 持続音（ブーン）の疑い"""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn",
         "-af", "ebur128=peak=true", "-f", "null", "-"],
        capture_output=True, text=True)
    # 途中経過行（t: ... I: -70.0 LUFS）ではなく末尾の "Summary:" ブロックだけを読む
    err = result.stderr.split("Summary:")[-1]
    mi = re.search(r'I:\s+(-?[\d.]+) LUFS', err)
    ml = re.search(r'LRA:\s+([\d.]+) LU', err)
    mp = re.search(r'Peak:\s+(-?[\d.]+) dBFS', err)
    return {
        "I": float(mi.group(1)) if mi else None,
        "LRA": float(ml.group(1)) if ml else None,
        "peak": float(mp.group(1)) if mp else None,
    }


def rms_db(path, start, length):
    """区間 [start, start+length] の RMS レベル(dB)。BGM とナレーションのバランス実測に使う"""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
         "-i", str(path), "-vn", "-af", "astats=metadata=1:reset=0", "-f", "null", "-"],
        capture_output=True, text=True)
    vals = re.findall(r'RMS level dB: (-?[\d.]+|-inf)', result.stderr)
    if not vals:
        return None
    v = vals[-1]
    return -120.0 if v == "-inf" else float(v)


def sha1_short(text, n=12):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]


# =====================================================================
# スタイル（既定値 → プリセット → プロジェクト個別）
# =====================================================================

def deep_merge(base, override):
    """辞書を再帰マージ（overrideが勝つ）"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def resolve_style(base_style, preset_name, project_style):
    presets = base_style.get("presets", {})
    style = {k: v for k, v in base_style.items() if k != "presets"}
    if preset_name:
        preset = presets.get(preset_name)
        if preset:
            log(f"   - スタイルプリセット適用: {preset_name}")
            style = deep_merge(style, preset)
        else:
            log(f"   [WARNING] 未定義のプリセット '{preset_name}'（利用可能: {', '.join(presets)}）")
    if project_style:
        log("   - プロジェクト個別スタイル (style.json) を適用")
        style = deep_merge(style, project_style)
    return style


def get_pinned_cli():
    """hyperframes-app/package.json のピンからCLIコマンドを組み立てる（未ピン実行による乖離を防ぐ）"""
    try:
        pkg = json.loads((APP_DIR / "package.json").read_text(encoding="utf-8"))
        m = re.search(r"hyperframes@([\d.]+)", pkg["scripts"]["render"])
        if m:
            return ["npx", "--yes", f"hyperframes@{m.group(1)}"]
    except Exception as e:
        log(f"   [WARNING] ピンの解決に失敗: {e}。hyperframes@latest を使用します。")
    return ["npx", "--yes", "hyperframes@latest"]


def fish_api_key():
    key = os.environ.get("FISH_AUDIO_API_KEY", "").strip().strip('"')
    if not key or key == "your_api_key_here":
        return ""
    return key


def list_bgm_files(directory):
    if not directory.exists():
        return []
    return sorted(p for p in directory.iterdir()
                  if p.is_file() and p.suffix.lower() in BGM_EXTS and not p.name.startswith("."))


# =====================================================================
# 0. preflight（実行前チェック）
# =====================================================================

def preflight_check(style):
    """必要ツール・キー・BGM の有無を確認。不足は導入コマンド付きで列挙し PreflightError"""
    log("0. 実行前チェック (preflight)...")
    problems = []
    tools = {
        "ffmpeg": ("brew install ffmpeg", "choco install ffmpeg -y"),
        "ffprobe": ("brew install ffmpeg", "choco install ffmpeg -y"),
        "pdftoppm": ("brew install poppler", "choco install poppler -y"),
        "node": ("brew install node", "choco install nodejs -y"),
        "npx": ("brew install node", "choco install nodejs -y"),
    }
    for tool, (mac, win) in tools.items():
        if not shutil.which(tool):
            problems.append(f"{tool} が見つかりません → Mac: `{mac}` / Windows(管理者PowerShell): `{win}`")
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        problems.append("edge-tts が未インストール → venv を有効化して `pip install -r requirements.txt`")
    try:
        import pykakasi  # noqa: F401  読み上げチェック（--voice-check）用
    except ImportError:
        problems.append("pykakasi が未インストール（git pull 後に増えた部品）→ `venv/bin/pip install -r requirements.txt`（Windows: `venv\\Scripts\\pip install -r requirements.txt`）")
    if not (APP_DIR / "package.json").exists():
        problems.append("hyperframes-app/package.json がありません → `./setup.sh`（Windows: `setup.bat`）を実行")
    if sys.version_info < (3, 10):
        problems.append(f"Python 3.10 以上が必要です（現在 {sys.version.split()[0]}）")

    key = fish_api_key()
    if key:
        log(f"   - TTS: Fish Audio（model={os.environ.get('FISH_AUDIO_MODEL', style['audio'].get('fish_model', 's2.1-pro'))}）")
    else:
        log("   - TTS: edge-tts（無料）。高品質にするなら .env に FISH_AUDIO_API_KEY を設定")

    counts = {m: len(list_bgm_files(ASSETS_DIR / "bgm" / m)) for m in MOODS}
    if sum(counts.values()) == 0:
        log("   [WARNING] BGM が1曲もありません → assets/bgm/<mood>/ に音楽ファイルを置いてください"
            "（assets/bgm/README.md 参照）。BGM なしで出力します")
    else:
        log("   - BGM: " + " / ".join(f"{m}={c}曲" for m, c in counts.items()))

    if problems:
        for p in problems:
            log(f"   [ERROR] {p}")
        raise PreflightError("環境が不足しています。上記を導入してから再実行してください")
    log("   - 環境OK")
    return {"tts_engine": "fish" if key else "edge-tts", "bgm_counts": counts}


# =====================================================================
# 1. 入力パース
# =====================================================================

def determine_mood(script_blocks):
    """台本テキストから動画のムード（upbeat / relaxing / serious）を判定する"""
    full_text = "".join(b.get('text', '') for b in script_blocks)
    serious_keywords = ["課題", "問題", "深刻", "減少", "対策", "リスク", "注意"]
    upbeat_keywords = ["最高", "おすすめ", "新製品", "新登場", "大ヒット", "突破", "達成", "嬉しい"]
    serious_score = sum(full_text.count(kw) for kw in serious_keywords)
    upbeat_score = sum(full_text.count(kw) for kw in upbeat_keywords)
    if serious_score > upbeat_score and serious_score > 0:
        return "serious"
    if upbeat_score > serious_score and upbeat_score > 0:
        return "upbeat"
    return "relaxing"


def _extract_directive(content, name):
    """「# Title: ...」形式の1行指示を取り出し、本文から除去する"""
    m = re.search(rf'^#+\s*{name}\s*[:：]\s*(.+?)\s*$', content, re.MULTILINE | re.IGNORECASE)
    if not m:
        return None, content
    return m.group(1).strip(), content.replace(m.group(0), '')


SUBTITLE_POSITIONS = ("bottom", "top", "band", "off")
FOCUS_EFFECTS = ("zoom", "spotlight", "box", "underline")


def load_storyboard(project_inbox, slide_count):
    """inbox/<動画名>/storyboard.json（Claude がスライドを見て書く演出指示）を読んで検証する。
    無ければ空（今までと同じ出力）。書式の誤りは黙って無視せず、全部まとめて例外にする。
      {"default": {"subtitle": "bottom"}, "slides": {"3": {"subtitle": "band"}}}
    """
    path = project_inbox / "storyboard.json"
    if not path.exists():
        return {"default": {}, "slides": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"storyboard.json が JSON として読めません: {e}")
    errors = []
    if not isinstance(data, dict):
        raise ValueError("storyboard.json の一番外側は {} にしてください")
    for key in data:
        if key not in ("version", "default", "slides"):
            errors.append(f"不明なキー '{key}'（使えるのは version / default / slides）")

    def check_scene(where, scene):
        if not isinstance(scene, dict):
            errors.append(f"{where} は {{}} にしてください")
            return
        for k, v in scene.items():
            if k == "focus" and where == "default":
                errors.append("default に focus は書けません（スライドごとに書いてください）")
            elif k == "subtitle":
                if v not in SUBTITLE_POSITIONS:
                    errors.append(f"{where}.subtitle = '{v}' は使えません（{' / '.join(SUBTITLE_POSITIONS)}）")
            elif k == "focus":
                check_focus(where, v)
            elif k == "gap":
                if not isinstance(v, (int, float)) or isinstance(v, bool) or not (0 <= v <= 2):
                    errors.append(f"{where}.gap = {v!r} は 0〜2 の秒数にしてください")
            elif k != "note":
                errors.append(f"{where} の不明なキー '{k}'")

    def check_focus(where, focus):
        if not isinstance(focus, list):
            errors.append(f"{where}.focus は [{{...}}, ...] の配列にしてください")
            return
        for j, f in enumerate(focus, start=1):
            w = f"{where}.focus[{j}]"
            if not isinstance(f, dict):
                errors.append(f"{w} は {{}} にしてください")
                continue
            for k in f:
                if k not in ("sentence", "effect", "box", "span", "note"):
                    errors.append(f"{w} の不明なキー '{k}'（使えるのは sentence / effect / box / span / note）")
            sn = f.get("sentence")
            if not isinstance(sn, int) or isinstance(sn, bool) or sn < 1:
                errors.append(f"{w}.sentence は 1 以上の整数（そのスライドの何文目か）にしてください")
            if f.get("effect") not in FOCUS_EFFECTS:
                errors.append(f"{w}.effect = {f.get('effect')!r} は使えません（{' / '.join(FOCUS_EFFECTS)}）")
            box = f.get("box")
            nums = isinstance(box, list) and len(box) == 4 and all(
                isinstance(x, (int, float)) and not isinstance(x, bool) for x in box)
            if not nums or not (0 <= box[0] < 1 and 0 <= box[1] < 1 and 0 < box[2] <= 1 and 0 < box[3] <= 1
                                and box[0] + box[2] <= 1.001 and box[1] + box[3] <= 1.001):
                errors.append(f"{w}.box は [左, 上, 幅, 高さ]（スライドに対する 0〜1 の割合。はみ出さない）にしてください: {box!r}")
            span = f.get("span", [0, 1])
            if not (isinstance(span, list) and len(span) == 2 and all(
                    isinstance(x, (int, float)) and not isinstance(x, bool) for x in span) and 0 <= span[0] < span[1] <= 1):
                errors.append(f"{w}.span は [開始, 終了]（その文の長さに対する 0〜1 の割合。開始 < 終了）にしてください: {span!r}")
        keys = [(f.get("sentence"), f.get("effect"), tuple(f.get("span", [0, 1])))
                for f in focus if isinstance(f, dict)]
        if len(keys) != len(set(keys)):
            errors.append(f"{where}.focus に同じ文・同じ効果・同じ区間の指定が重複しています")

    check_scene("default", data.get("default", {}))
    slides = data.get("slides", {})
    if not isinstance(slides, dict):
        errors.append("slides は {\"スライド番号\": {...}} の形にしてください")
        slides = {}
    for n, scene in slides.items():
        if not str(n).isdigit() or not (1 <= int(n) <= slide_count):
            errors.append(f"slides の '{n}' はスライド番号（1〜{slide_count}）ではありません")
        check_scene(f"slides.{n}", scene)
    if errors:
        raise ValueError("storyboard.json の誤り:\n  - " + "\n  - ".join(errors))
    log(f"   - storyboard.json を読み込みました（個別指定 {len(slides)} 枚）")
    return {"default": data.get("default", {}), "slides": {int(n): s for n, s in slides.items()}}


def load_readings(project_inbox):
    """inbox/<動画名>/reading.json（読み方辞書）: {"表記": "読み"}。音声に渡す文字だけを置き換え、テロップは変えない"""
    path = project_inbox / "reading.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"reading.json が JSON として読めません: {e}")
    if not isinstance(data, dict):
        raise ValueError('reading.json は {"表記": "読み"} の形にしてください')
    bad = [k for k, v in data.items() if not isinstance(k, str) or not k or not isinstance(v, str) or not v]
    if bad:
        raise ValueError(f"reading.json の誤り: 表記と読みは空でない文字列にしてください → {bad}")
    log(f"   - reading.json を読み込みました（{len(data)}語）")
    return data


def apply_readings(text, readings):
    """長い表記から順に置き換える（「AI活用」を「AI」より先に）"""
    for k in sorted(readings, key=len, reverse=True):
        text = text.replace(k, readings[k])
    return text


def scene_setting(storyboard, slide_no, key, fallback):
    """スライド個別 → default → 既定値 の順で1項目を引く"""
    scene = (storyboard or {}).get("slides", {}).get(slide_no, {})
    if key in scene:
        return scene[key]
    return (storyboard or {}).get("default", {}).get(key, fallback)


def parse_input(video_title):
    log(f"1. [{video_title}] スライドと台本のパースを開始します...")
    project_inbox = INBOX_DIR / video_title
    project_work = WORK_DIR / video_title
    slides_dir = project_work / "slides"
    slides_dir.mkdir(parents=True, exist_ok=True)
    warnings = []

    pdf_path = project_inbox / "slides.pdf"
    pptx_path = project_inbox / "slides.pptx"
    if not pdf_path.exists():
        pdfs = sorted(project_inbox.glob("*.pdf"))
        if pdfs:
            pdf_path = pdfs[0]
        elif pptx_path.exists():
            raise FileNotFoundError(
                f"PDFが見つかりません。{pptx_path.name} を PDF 形式に書き出して同じフォルダに置いてください。")
        else:
            raise FileNotFoundError(f"PDFが見つかりません: {project_inbox}")

    log("   - PDFを画像に変換中...")
    images = convert_from_path(pdf_path, dpi=150)
    slide_images = []
    for i, image in enumerate(images):
        img_path = slides_dir / f"slide_{i+1:03d}.png"
        image.save(img_path, 'PNG')
        slide_images.append(str(img_path))
    log(f"   - {len(slide_images)}枚のスライド画像を生成しました。")

    script_path = project_inbox / "script.md"
    if not script_path.exists():
        mds = sorted(project_inbox.glob("*.md"))
        if not mds:
            raise FileNotFoundError(f"台本（.md）が見つかりません: {project_inbox}")
        script_path = mds[0]

    log("   - 台本(Markdown)を解析中...")
    content = script_path.read_text(encoding='utf-8')

    title, content = _extract_directive(content, "Title")
    video_display_title = title or video_title
    preset_name, content = _extract_directive(content, "Style")
    preset_name = preset_name.lower() if preset_name else None
    bgm_hint, content = _extract_directive(content, "BGM")

    project_style = None
    style_json = project_inbox / "style.json"
    if style_json.exists():
        project_style = json.loads(style_json.read_text(encoding='utf-8'))

    blocks = []
    current_slide = 1
    current_text = []
    for line in content.split('\n'):
        match = re.match(r'^#+\s*Slide\s*(\d+)', line, re.IGNORECASE)
        if match:
            text_str = '\n'.join(current_text).strip()
            if text_str:
                blocks.append({"slide": current_slide, "text": text_str})
            current_slide = int(match.group(1))
            current_text = []
        else:
            current_text.append(line)
    text_str = '\n'.join(current_text).strip()
    if text_str:
        blocks.append({"slide": current_slide, "text": text_str})
    if not blocks:
        raise ValueError("台本にナレーションがありません（「# Slide 1」の下に本文を書いてください）")

    # 台本と枚数の整合（AIが無人実行しても「何が動画に入らなかったか」が分かるように）
    narrated = {b["slide"] for b in blocks}
    missing = [n for n in range(1, len(slide_images) + 1) if n not in narrated]
    if missing:
        msg = f"スライド {', '.join(map(str, missing))} は台本がないため動画に含まれません"
        log(f"   [WARNING] {msg}")
        warnings.append(msg)
    over = sorted(n for n in narrated if n > len(slide_images))
    if over:
        msg = f"台本の Slide {', '.join(map(str, over))} に対応するスライド画像がありません（PDFは{len(slide_images)}枚）"
        log(f"   [WARNING] {msg}")
        warnings.append(msg)
        blocks = [b for b in blocks if b["slide"] <= len(slide_images)]

    storyboard = load_storyboard(project_inbox, len(slide_images))
    readings = load_readings(project_inbox)

    log(f"   - {len(blocks)}個の台本ブロックを抽出しました。タイトル: {video_display_title}")
    return {
        "storyboard": storyboard,
        "readings": readings,
        "slides": slide_images,
        "script_blocks": blocks,
        "title": video_display_title,
        "preset": preset_name,
        "bgm_hint": bgm_hint,
        "project_style": project_style,
        "warnings": warnings,
    }


def parse_emphasis(text):
    """**強調** 記法を除去した平文と、強調範囲 [(start, end), ...] を返す"""
    plain = []
    spans = []
    i = 0
    pos = 0
    for m in re.finditer(r'\*\*(.+?)\*\*', text, re.DOTALL):
        plain.append(text[i:m.start()])
        pos += len(text[i:m.start()])
        inner = m.group(1)
        spans.append((pos, pos + len(inner)))
        plain.append(inner)
        pos += len(inner)
        i = m.end()
    plain.append(text[i:])
    return ''.join(plain), spans


# =====================================================================
# 2. TTS（文単位生成 → 結合。Fish Audio → edge-tts フォールバック）
# =====================================================================

NORM_RE = re.compile(r'[^0-9A-Za-zぁ-んァ-ヶ一-龯ー々]')


def norm_chars(s):
    return NORM_RE.sub('', s)


def tts_text_of(block_text):
    """TTSに渡す平文（強調記法を除去）"""
    plain, _ = parse_emphasis(block_text)
    return re.sub(r'\n+', '\n', plain).strip()


def split_sentences(text, min_chars=6):
    """文（。！？改行）に分割。極端に短い文は隣と結合してTTSの不自然さを防ぐ"""
    parts = []
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue
        for m in re.finditer(r'[^。！？]+[。！？]*', line):
            s = m.group(0).strip()
            if s:
                parts.append(s)
    merged = []
    for s in parts:
        if merged and (len(norm_chars(s)) < min_chars or len(norm_chars(merged[-1])) < min_chars):
            merged[-1] += s
        else:
            merged.append(s)
    return merged or [text.replace('\n', '')]


def synth_sentence(text, out_stem, tts):
    """1文を音声化して (rawファイル, 使用エンジン) を返す"""
    if tts["engine"] == "fish":
        raw = Path(f"{out_stem}.wav")
        try:
            payload = {
                "text": text,
                "format": "wav",
                "normalize": True,
                "latency": "normal",
                "temperature": tts["temperature"],
                "top_p": 0.7,
                "prosody": {"speed": tts["speed"], "volume": 0},
            }
            if tts["voice_id"]:
                payload["reference_id"] = tts["voice_id"]
            r = requests.post(
                FISH_TTS_URL,
                headers={"Authorization": f"Bearer {tts['key']}",
                         "Content-Type": "application/json",
                         "model": tts["model"]},
                json=payload, timeout=180)
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            if len(r.content) < 1000 or "json" in ctype:
                raise RuntimeError(f"音声でない応答 ({ctype}): {r.content[:160]!r}")
            raw.write_bytes(r.content)
            return raw, "fish"
        except Exception as e:
            log(f"   [WARNING] Fish Audio 失敗: {e} → edge-tts にフォールバック")

    raw = Path(f"{out_stem}.mp3")
    speed = tts["speed"]
    rate = f"{'+' if speed >= 1.0 else ''}{round((speed - 1.0) * 100)}%"
    text_clean = text.replace('"', '').replace("'", "")
    cmd = [sys.executable, "-m", "edge_tts", "--voice", tts["edge_voice"],
           f"--rate={rate}", "--text", text_clean, "--write-media", str(raw)]
    try:
        run_command(cmd, capture=True, timeout=180)
        return raw, "edge-tts"
    except Exception as e:
        log(f"   [ERROR] edge-tts も失敗: {e} → 1.5秒の無音で代替します（要確認）")
        run_ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1.5", raw])
        return raw, "silence"


def normalize_segment(raw, seg_path):
    """前後の無音を刈り、48kHz stereo PCM に統一。無音になった場合は1秒の無音で代替"""
    run_ffmpeg(["-i", raw, "-af", TRIM_FILTER, "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", seg_path])
    if ffprobe_duration(seg_path) < 0.2:
        log("   [WARNING] 音声がほぼ無音のため1秒の無音で代替します")
        run_ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1.0",
                    "-c:a", "pcm_s16le", seg_path])


def concat_block(seg_paths, gap, lead, tail, out_path):
    """文の音声を「文間ギャップ」付きで結合し、前後に無音パディングを付ける"""
    n = len(seg_paths)
    inputs = []
    for p in seg_paths:
        inputs += ["-i", str(p)]
    parts = []
    for k in range(n):
        if k < n - 1:
            parts.append(f"[{k}:a]apad=pad_dur={gap}[a{k}]")
        else:
            parts.append(f"[{k}:a]anull[a{k}]")
    chain = "".join(f"[a{k}]" for k in range(n))
    parts.append(f"{chain}concat=n={n}:v=0:a=1,adelay={int(lead * 1000)}:all=1,apad=pad_dur={tail}[out]")
    run_ffmpeg(inputs + ["-filter_complex", ";".join(parts), "-map", "[out]",
                         "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", out_path])


def generate_tts(script_blocks, video_title, style, storyboard=None, readings=None):
    log("2. ナレーション音声を生成します...")
    audio_cfg = style["audio"]
    lead = float(audio_cfg.get("lead_silence", 0.25))
    tail = float(audio_cfg.get("tail_silence", 0.6))
    default_gap = float(audio_cfg.get("sentence_gap", 0.18))
    readings = readings or {}
    granularity = audio_cfg.get("tts_granularity", "sentence")
    min_chars = int(audio_cfg.get("min_sentence_chars", 6))

    key = fish_api_key()
    tts = {
        "engine": "fish" if key else "edge-tts",
        "key": key,
        "voice_id": os.environ.get("FISH_AUDIO_VOICE_ID", "").strip().strip('"'),
        "model": os.environ.get("FISH_AUDIO_MODEL", "").strip().strip('"') or audio_cfg.get("fish_model", "s2.1-pro"),
        "speed": float(audio_cfg.get("tts_speed", 1.0)),
        "temperature": float(audio_cfg.get("tts_temperature", 0.5)),
        "edge_voice": audio_cfg.get("edge_voice", EDGE_VOICE),
    }
    if tts["engine"] == "fish":
        log(f"   - Fish Audio で生成します（model={tts['model']}, voice={tts['voice_id'] or 'default'}, 文単位={granularity == 'sentence'}）")
    else:
        log(f"   - FISH_AUDIO_API_KEY 未設定のため edge-tts ({tts['edge_voice']}) で生成します")
    # キャッシュキーにエンジン・声・速度を含める（キー設定後も古い edge-tts 音声が再利用されるバグを防ぐ）
    cache_sig = f"{tts['engine']}|{tts['voice_id']}|{tts['model']}|{tts['speed']}|{tts['temperature']}|{tts['edge_voice']}"

    audio_dir = WORK_DIR / video_title / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    results = []
    engines_used = []
    for i, block in enumerate(script_blocks):
        text = tts_text_of(block['text'])
        slide_num = block['slide']
        gap = float(scene_setting(storyboard, slide_num, "gap", default_gap))
        sentences = split_sentences(text, min_chars) if granularity == "sentence" else [text.replace('\n', '')]

        seg_paths = []
        seg_durs = []
        generated_any = False
        for s in sentences:
            # 読み方辞書は音声に渡す文字だけに当てる（辞書が無ければ従来と同じハッシュ → キャッシュもそのまま使える）
            spoken = apply_readings(s, readings)
            h = sha1_short(f"{cache_sig}|{spoken}")
            seg = audio_dir / f"seg_{h}.wav"
            if not seg.exists():
                if not generated_any:
                    log(f"   - スライド {slide_num} の音声を生成中... ({len(sentences)}文 / {len(text)}文字)")
                    generated_any = True
                raw, used = synth_sentence(spoken, audio_dir / f"seg_{h}_raw", tts)
                engines_used.append(used)
                normalize_segment(raw, seg)
                raw.unlink(missing_ok=True)
            else:
                engines_used.append("cache")
            seg_paths.append(seg)
            seg_durs.append(ffprobe_duration(seg))

        block_hash = sha1_short("|".join(p.name for p in seg_paths) + f"|{lead}|{tail}|{gap}")
        padded = audio_dir / f"block_{slide_num:03d}_{i:03d}_{block_hash}.wav"
        meta_path = padded.with_suffix(".json")
        if not padded.exists() or not meta_path.exists():
            concat_block(seg_paths, gap, lead, tail, padded)
            t = lead
            sentence_times = []
            for s, d in zip(sentences, seg_durs):
                sentence_times.append({"text": s, "start": round(t, 3), "end": round(t + d, 3)})
                t += d + gap
            meta_path.write_text(json.dumps(sentence_times, ensure_ascii=False, indent=1), encoding="utf-8")
        else:
            log(f"   - スライド {slide_num}: キャッシュ音声を再利用")
        sentence_times = json.loads(meta_path.read_text(encoding="utf-8"))

        results.append({
            "slide": slide_num,
            "segments": [{"text": s, "spoken": apply_readings(s, readings), "path": str(p)}
                         for s, p in zip(sentences, seg_paths)],
            "audio_path": str(padded),
            "duration": ffprobe_duration(padded),
            "sentence_times": sentence_times,
        })

    fresh = [e for e in engines_used if e != "cache"]
    tts_info = {
        "engine_expected": tts["engine"],
        "model": tts["model"] if tts["engine"] == "fish" else tts["edge_voice"],
        "generated": len(fresh),
        "cached": engines_used.count("cache"),
        "fallbacks": sum(1 for e in fresh if e in ("edge-tts", "silence") and tts["engine"] == "fish"),
        "silences": fresh.count("silence"),
    }
    log(f"   - 生成 {tts_info['generated']}文 / キャッシュ {tts_info['cached']}文"
        + (f" / フォールバック {tts_info['fallbacks']}文" if tts_info["fallbacks"] else ""))
    return results, tts_info


# =====================================================================
# 3. テロップ分割・タイミング
# =====================================================================

def transcribe_words(audio_path, style):
    """hyperframes transcribe（whisper）で発話タイミングを取得。既定では無効（文単位TTSで十分な精度が出るため）"""
    tcfg = style.get("transcribe", {})
    if not tcfg.get("enabled", False):
        return None
    cache = Path(str(audio_path) + ".words.json")
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass
    cli = get_pinned_cli()
    cmd = cli + ["transcribe", str(audio_path),
                 "--model", tcfg.get("model", "small"),
                 "--language", tcfg.get("language", "ja"),
                 "--json"]
    try:
        result = subprocess.run(cmd, cwd=str(APP_DIR), capture_output=True, text=True, timeout=900)
        if result.returncode != 0:
            log(f"   [WARNING] transcribe 失敗 (exit {result.returncode}): {result.stderr[-300:]}")
            return None
        envelope = json.loads(result.stdout.strip())
        if not envelope.get("ok"):
            log(f"   [WARNING] transcribe NG: {envelope}")
            return None
        words = json.loads(Path(envelope["transcriptPath"]).read_text(encoding="utf-8"))
        if words:
            cache.write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
            log(f"   - 発話タイミング取得: {len(words)}セグメント")
        return words or None
    except Exception as e:
        log(f"   [WARNING] transcribe 実行エラー: {e}")
        return None


PARTICLE_CHARS = 'をはがでにとへも'
# この文字の直前では改行しない（句読点・閉じ括弧・拗音促音長音・助詞「の」等が行頭に来るのを防ぐ）
NO_BREAK_NEXT = '。、！？」）ゃゅょんっーの' + PARTICLE_CHARS
NO_BREAK_PREV = '「（('


def _is_hira(ch):
    return 'ぁ' <= ch <= 'ゖ'


def _is_kata(ch):
    return ('ァ' <= ch <= 'ヺ') or ch == 'ー'


def _char_class(ch):
    if _is_hira(ch):
        return 'hira'
    if _is_kata(ch):
        return 'kata'
    if '一' <= ch <= '鿿' or ch == '々':
        return 'kanji'
    if ch.isascii() and ch.isalnum():
        return 'latin'
    return 'other'


def _quotative_at(text, p):
    """位置 p から引用の「という／といった／といって」が始まるか。
    促音の直後（「もっ|といっぱい」「ちょっ|といった」）や「といっしょ」「といっても」は除く"""
    return (p > 0 and text[p - 1] != 'っ'
            and text.startswith(('という', 'といった', 'といって'), p)
            and not text.startswith('といっても', p))


def break_candidates(text):
    """改行候補を優先順のティアで返す（各ティアは位置のリスト）。
    1) 読点直後  2) ひらがな→非ひらがな遷移（文節境界の近似。「〜あたり|6時間」「〜によって|バラバラ」）
    3) 助詞直後  4) 文字種の切り替わり（漢字語・カタカナ語・数字の途中では切らない）
    いずれも「」（）の内側では切らない。語の途中（「バ|ラバラ」「動画制|作」）で切れるのを避けるための順序"""
    n = len(text)
    inside = []
    depth = 0
    for ch in text:
        inside.append(depth > 0)
        if ch in '「（(':
            depth += 1
        elif ch in '」）)':
            depth = max(0, depth - 1)

    def ok(p):
        if not (0 < p < n) or text[p - 1] in NO_BREAK_PREV or inside[p]:
            return False
        # 「という」「といった」はひとかたまり: 「増加と|いう」は切らず、「増加|という」は許す
        quotative = _quotative_at(text, p)
        if text[p] in NO_BREAK_NEXT and not quotative:
            return False
        if text[p - 1] == 'と' and text.startswith(('いう', 'いっ'), p):
            return False
        # 敬語接頭辞「ご紹介」「お好み」の途中で切らない
        if text[p - 1] in 'ごお' and _char_class(text[p]) == 'kanji':
            return False
        return True

    t1 = [m.end() for m in re.finditer('、', text)] + [p for p in range(1, n) if text[p - 1] in '」）)']
    t2 = [p for p in range(1, n) if _is_hira(text[p - 1]) and not _is_hira(text[p])]
    t2 += [p for p in range(1, n) if _quotative_at(text, p)]   # 「増加|という」
    t3 = [i + 1 for i, ch in enumerate(text[:-1]) if ch in PARTICLE_CHARS]
    t4 = [p for p in range(1, n) if _char_class(text[p - 1]) != _char_class(text[p])]
    return [[p for p in sorted(set(tier)) if ok(p)] for tier in (t1, t2, t3, t4)]


def find_break_pos(text, max_len, min_pos, target=None):
    """[min_pos, max_len] 内で最も自然な分割位置（build_chunks の超長文分割用）。
    target（理想の分割位置）に最も近い候補をティア順に選ぶ。無ければ max_len"""
    if target is None:
        target = max_len
    for tier in break_candidates(text):
        c = [p for p in tier if min_pos <= p <= max_len]
        if c:
            return min(c, key=lambda p: abs(p - target))
    return max_len


def build_chunks(plain_text, kw_spans, max_per_line, max_lines):
    """台本平文を放送基準（1行max_per_line文字×max_lines行）のテロップチャンクに分割"""
    max_screen = max_per_line * max_lines
    parts = re.split(r'([。！？\n]+)', plain_text)
    sentences = []
    for j in range(0, len(parts) - 1, 2):
        s = (parts[j] + parts[j + 1]).replace('\n', '').strip()
        if s:
            sentences.append(s)
    if len(parts) % 2 == 1 and parts[-1].strip():
        sentences.append(parts[-1].replace('\n', '').strip())

    chunks = []
    for s in sentences:
        if len(s) <= max_screen:
            chunks.append(s)
            continue
        pieces = [p for p in re.split(r'(?<=、)', s) if p]
        cur = ""
        for p in pieces:
            while len(p) > max_screen:
                if cur:
                    chunks.append(cur)
                    cur = ""
                # 残りを均等に割った理想位置に最も近い自然な境界で切る（末尾に極端に短い塊を残さない）
                n_parts = math.ceil(len(p) / max_screen)
                target = len(p) / n_parts
                bp = find_break_pos(p, max_screen, int(max_screen * 0.4), target)
                chunks.append(p[:bp])
                p = p[bp:]
            if len(cur) + len(p) <= max_screen:
                cur += p
            else:
                chunks.append(cur)
                cur = p
        if cur:
            chunks.append(cur)

    if not chunks:
        chunks = [plain_text[:max_screen]]

    ranges = []
    cursor = 0
    for c in chunks:
        idx = plain_text.find(c[:8] if len(c) >= 8 else c, cursor)
        if idx < 0:
            idx = cursor
        ranges.append((idx, idx + len(c)))
        cursor = idx + len(c)
    return chunks, ranges


def split_lines(chunk, max_per_line):
    """チャンクを2行に分割。まず両行 max_per_line 以内で候補ティア順に探し、
    無ければ +4 文字まで許容（座布団幅には余裕があるため、語の途中で切るより長い行のほうが読みやすい）"""
    n = len(chunk)
    if n <= max_per_line:
        return chunk
    mid = n / 2
    tiers = break_candidates(chunk)
    for cap in (max_per_line, max_per_line + 4):
        for tier in tiers:
            c = [p for p in tier if max(p, n - p) <= cap]
            if c:
                bp = min(c, key=lambda p: abs(p - mid))
                return chunk[:bp] + '\n' + chunk[bp:]
    bp = min(max_per_line, int(mid + 0.5))
    return chunk[:bp] + '\n' + chunk[bp:]


def chunk_html(chunk_text, chunk_range, kw_spans, max_per_line):
    """キーワード強調spanを埋め込んだテロップHTMLを生成"""
    c0, _ = chunk_range
    text = split_lines(chunk_text, max_per_line)
    out = []
    plain_i = c0
    for ch in text:
        if ch == '\n':
            out.append(('\n', None))
        else:
            out.append((ch, plain_i))
            plain_i += 1
    html = ""
    in_kw = False
    for ch, pi in out:
        kw = pi is not None and any(s <= pi < e for s, e in kw_spans)
        if kw and not in_kw:
            html += '<span class="kw">'
            in_kw = True
        elif not kw and in_kw:
            html += '</span>'
            in_kw = False
        html += ch
    if in_kw:
        html += '</span>'
    return html


def time_chunks(chunks, ranges, plain_text, words, duration, lead_time, lead_silence, tail_silence, source="sentence"):
    """各チャンクの表示時間を決定。words（文単位 or whisper の {text,start,end}）があれば実タイミング、なければ文字数比"""
    n_total = len(norm_chars(plain_text))
    timings = []

    if words:
        spans = []
        c = 0
        for w in words:
            text = w.get('text', w.get('word', ''))
            ln = len(norm_chars(text))
            if ln == 0:
                continue
            spans.append((c, c + ln, float(w['start']), float(w['end'])))
            c += ln
        n_trans = c

        def time_at(script_char_idx):
            if not spans or n_total == 0:
                return None
            t_idx = script_char_idx / n_total * n_trans
            for (c0, c1, s, e) in spans:
                if t_idx < c1:
                    if t_idx <= c0:
                        return s
                    return s + (t_idx - c0) / (c1 - c0) * (e - s)
            return spans[-1][3]

        if spans and n_trans >= n_total * 0.5:
            cursor_norm = 0
            starts = []
            ends = []
            for (r0, r1) in ranges:
                seg = norm_chars(plain_text[r0:r1])
                starts.append(time_at(cursor_norm))
                cursor_norm += len(seg)
                ends.append(time_at(cursor_norm))
            t0s = [max(0.0, (starts[i] or 0) - lead_time) for i in range(len(chunks))]
            for i in range(1, len(chunks)):
                if t0s[i] <= t0s[i - 1]:
                    t0s[i] = t0s[i - 1] + 0.15
            for i in range(len(chunks)):
                if i + 1 < len(chunks):
                    t1 = t0s[i + 1]
                else:
                    t1 = min(duration, (ends[i] or duration) + 0.45)
                timings.append((t0s[i], max(t0s[i] + 0.15, t1)))
            return timings, source
        log("   [INFO] タイミング情報が台本と乖離しているため文字数比推定にフォールバック")

    speech_start = lead_silence
    speech_dur = max(0.5, duration - lead_silence - tail_silence)
    total_chars = sum(len(c) for c in chunks) or 1
    bounds = []
    t = speech_start
    for c in chunks:
        bounds.append(t)
        t += (len(c) / total_chars) * speech_dur
    t0s = [max(0.0, b - lead_time) for b in bounds]
    for i in range(len(chunks)):
        t1 = t0s[i + 1] if i + 1 < len(chunks) else min(duration, t + 0.3)
        timings.append((t0s[i], max(t0s[i] + 0.15, t1)))
    return timings, "estimate"


# =====================================================================
# 4. HyperFrames コンポジション生成
# =====================================================================

KB_ROTATION = ["in", "left", "out", "right"]


def build_focus(focus_list, slide_no, scene_start, scene_end, sentence_times, image_size, anim, band_h):
    """storyboard の focus を、スライドと一緒に動く演出要素（HTML）と zoom の指示に変換する。
    位置はスライド画像に対する割合 → 画面上の px（object-fit: contain の実表示領域）に直す。
    zoom は囲んだ場所が「テロップ帯を除いた上側」の中央に来るよう、拡大率と移動量を Python で決める（決定性）"""
    W, H = 1920, 1080
    iw, ih = image_size
    fit = min(W / iw, H / ih)
    dw, dh = iw * fit, ih * fit
    ox, oy = (W - dw) / 2, (H - dh) / 2
    pad = float(anim.get("focus_padding", 14))
    max_zoom = float(anim.get("focus_max_zoom", 1.8))
    lead = float(anim.get("focus_lead", 0.15))
    area_h = H - band_h
    html, cues, labels = [], [], []
    for j, f in enumerate(sorted(focus_list, key=lambda f: (f["sentence"], f.get("span", [0, 1])[0])), start=1):
        n = f["sentence"]
        if n > len(sentence_times):
            raise ValueError(f"storyboard.json: スライド {slide_no} の focus は {n} 文目を指していますが、"
                             f"このスライドの台本は {len(sentence_times)} 文です")
        st = sentence_times[n - 1]
        a, b = f.get("span", [0, 1])
        seg = st["end"] - st["start"]
        t0 = max(scene_start, scene_start + st["start"] + seg * a - lead)
        t1 = min(scene_end, scene_start + st["start"] + seg * b)
        bx, by, bw, bh = f["box"]
        x = ox + bx * dw - pad
        y = oy + by * dh - pad
        w = bw * dw + pad * 2
        h = bh * dh + pad * 2
        eff = f["effect"]
        labels.append({"effect": eff, "start": round(t0, 3), "end": round(t1, 3), "box": f["box"], "sentence": n})
        if eff == "zoom":
            sc = min(max_zoom, (W * 0.9) / w, (area_h * 0.9) / h)
            sc = max(1.0, sc)
            tx = W / 2 - sc * (x + w / 2)
            ty = area_h / 2 - sc * (y + h / 2)
            tx = min(0.0, max(W - W * sc, tx))     # 左右・上に隙間を作らない
            # スライドの下の端が画面に入ると、帯の奥にぼかし背景との継ぎ目が見えるため H で止める
            ty = min(0.0, max(H - H * sc, ty))
            cues.append(f'<div class="zoom-cue" data-t0="{t0:.3f}" data-t1="{t1:.3f}" '
                        f'data-s="{sc:.4f}" data-x="{tx:.1f}" data-y="{ty:.1f}"></div>')
        else:
            html.append(f'<div id="focus-{slide_no}-{j}" class="focus-el focus-{eff}" data-t0="{t0:.3f}" data-t1="{t1:.3f}" '
                        f'style="left:{x:.0f}px;top:{y:.0f}px;width:{w:.0f}px;height:{h:.0f}px"></div>')
    return "".join(html), "".join(cues), labels


def generate_hyperframes_config(parsed_data, audio_data, style, video_title):
    log("4. HyperFrames用のHTMLコンポジションを生成します...")
    html_path = APP_DIR / "index.html"
    assets_out = APP_DIR / "assets"
    assets_out.mkdir(parents=True, exist_ok=True)

    design = style["design"]
    anim = style["animation"]
    intro_cfg = style.get("intro", {})
    outro_cfg = style.get("outro", {})
    audio_cfg = style["audio"]

    overlap = float(anim.get("transition_duration", 0.6))
    kb_on = bool(anim.get("ken_burns", True))
    kb_zoom = float(anim.get("ken_burns_zoom", 1.06))
    lead_time = float(anim.get("subtitle_lead_time", 0.08))
    subtitles_on = bool(design.get("subtitles_enabled", True))
    max_per_line = int(design.get("subtitle_max_chars_per_line", 16))
    max_lines = int(design.get("subtitle_max_lines", 2))
    lead_silence = float(audio_cfg.get("lead_silence", 0.25))
    tail_silence = float(audio_cfg.get("tail_silence", 0.6))

    slides = parsed_data['slides']
    storyboard = parsed_data.get("storyboard")
    default_position = design.get("subtitle_position", "bottom")
    clips = []
    sfx_events = []
    subtitle_modes = []
    subtitle_count = 0
    timeline = []   # 自己レビュー用: 場面とテロップの時刻表

    intro_dur = float(intro_cfg.get("duration", 3.0)) if intro_cfg.get("enabled", True) else 0.0
    content_t0 = intro_dur
    current = content_t0

    for i, item in enumerate(audio_data):
        seq = i + 1
        slide_idx = item['slide'] - 1
        # ms 単位に切り上げ: lint/レンダラは wav の実長で判定するため、切り捨てると次クリップと 1ms 未満の重なりが出る
        duration = math.ceil(max(item['duration'], 1.5) * 1000) / 1000
        start = round(current, 3)
        is_last = (i == len(audio_data) - 1)

        bg_rel = ""
        blur_rel = ""
        if slide_idx < len(slides):
            src = Path(slides[slide_idx])
            shutil.copy2(src, assets_out / src.name)
            bg_rel = f"assets/{src.name}"
            # ぼかし背景は 1/4 サイズの別ファイル（同一 src の二重参照を避け、blur の描画コストも下げる）
            blur_name = f"{src.stem}_blur.jpg"
            blur_path = assets_out / blur_name
            if not blur_path.exists():
                with Image.open(src) as im:
                    im = im.convert("RGB")
                    im.thumbnail((480, 480))
                    im.save(blur_path, "JPEG", quality=80)
            blur_rel = f"assets/{blur_name}"
        asrc = Path(item['audio_path'])
        shutil.copy2(asrc, assets_out / asrc.name)
        audio_rel = f"assets/{asrc.name}"

        # スライドシーン（クロスフェード分だけ次のスライドの下に延長）。安定 id は Studio 編集・lint 用
        clip_dur = duration + (0 if is_last else overlap)
        focus_list = ((storyboard or {}).get("slides", {}).get(item['slide'], {}) or {}).get("focus") or []
        has_zoom = any(f["effect"] == "zoom" for f in focus_list)
        # zoom とゆっくりズーム（Ken Burns）は同じ transform を奪い合うので、zoom のある場面では Ken Burns を止める
        kb = KB_ROTATION[i % len(KB_ROTATION)] if (kb_on and not has_zoom) else "none"
        fade_in = overlap if (i > 0 or intro_dur > 0) else 0
        track = i % 2
        z = 11 + i
        position = scene_setting(storyboard, item['slide'], "subtitle", default_position)
        sub_cls = {"top": " pos-top", "band": " pos-band"}.get(position, "")
        focus_html, zoom_cues, focus_labels = "", "", []
        if focus_list and slide_idx < len(slides):
            with Image.open(slides[slide_idx]) as im:
                img_size = im.size
            focus_html, zoom_cues, focus_labels = build_focus(
                focus_list, item['slide'], start, round(start + duration, 3), item.get("sentence_times") or [],
                img_size, anim, int(design.get("subtitle_band_height", 216)) if position == "band" else 0)
        scene_cls = " has-zoom" if has_zoom else ""
        timeline.append({"kind": "scene", "slide": item['slide'], "start": start,
                         "end": round(start + duration, 3), "position": position, "focus": focus_labels})
        clips.append(f'''
        <!-- Slide {slide_idx + 1} -->
        <div id="scene-{seq}" class="clip slide-scene{scene_cls}" data-start="{start:.3f}" data-duration="{clip_dur:.3f}" data-track-index="{track}"
             data-kb="{kb}" data-kb-zoom="{kb_zoom}" data-fade-in="{fade_in:.2f}" style="z-index:{z}">
          <div class="scene-inner">
            <div class="bg-blur" data-layout-allow-overflow><img id="bg-{seq}" src="{blur_rel}" /></div>
            <div class="kb-wrap"><img id="slide-{seq}" class="slide-img" src="{bg_rel}" />{focus_html}</div>
          </div>{zoom_cues}
        </div>
        <audio id="voice-{seq}" class="clip" data-start="{start:.3f}" data-duration="{duration:.3f}" data-track-index="{2 if i % 2 == 0 else 5}" src="{audio_rel}"></audio>''')

        if subtitles_on and position != "off":
            block_text = next((b['text'] for b in parsed_data['script_blocks']
                               if b['slide'] == item['slide']), "")
            plain, kw_spans = parse_emphasis(block_text)
            plain = plain.replace('\n', '')
            chunks, ranges = build_chunks(plain, kw_spans, max_per_line, max_lines)
            words = item.get("sentence_times") or None
            source = "sentence"
            if not words:
                words = transcribe_words(item['audio_path'], style)
                source = "whisper"
            timings, mode = time_chunks(chunks, ranges, plain, words, duration,
                                        lead_time, lead_silence, tail_silence, source)
            subtitle_modes.append(mode)
            for k, (chunk, rng, (t0, t1)) in enumerate(zip(chunks, ranges, timings)):
                html = chunk_html(chunk, rng, kw_spans, max_per_line)
                sa = round(start + t0, 3)
                ea = round(start + t1, 3)
                subtitle_count += 1
                timeline.append({"kind": "subtitle", "id": f"sub-{seq}-{k + 1}", "slide": item['slide'],
                                 "start": sa, "end": ea, "position": position,
                                 "text": re.sub(r"<[^>]+>", "", html)})
                clips.append(f'''
        <div id="sub-{seq}-{k + 1}" class="clip subtitle-wrapper{sub_cls}" data-start="{sa:.3f}" data-duration="{max(0.15, round(ea - sa, 3)):.3f}" data-track-index="3" style="z-index:500">
          <div class="subtitle-box">{html}</div>
        </div>''')

        if not is_last:
            sfx_events.append(("transition", start + duration - 0.1))
        current = round(start + duration, 3)

    content_end = current

    if intro_dur > 0:
        title = parsed_data.get("title", video_title)
        clips.append(f'''
        <!-- Intro -->
        <div id="intro-card" class="clip card-scene" data-start="0" data-duration="{intro_dur + overlap:.3f}" data-track-index="4" style="z-index:10">
          <div class="scene-inner">
            <div class="card-bg"></div>
            <div class="card-glow"></div>
            <div class="card-content">
              <div class="intro-label">{design.get("intro_label", "VIDEO GUIDE")}</div>
              <h1 class="intro-title">{title}</h1>
              <div class="intro-bar"></div>
            </div>
          </div>
        </div>''')
        sfx_events.append(("intro", 0.12))
        sfx_events.append(("transition", content_t0 - 0.15))

    total = content_end
    if outro_cfg.get("enabled", True):
        outro_dur = float(outro_cfg.get("duration", 4.0))
        outro_fade = 0.8
        outro_text = outro_cfg.get("text", "ご視聴ありがとうございました")
        title = parsed_data.get("title", video_title)
        clips.append(f'''
        <!-- Outro -->
        <div id="outro-card" class="clip card-scene" data-start="{content_end - outro_fade:.3f}" data-duration="{outro_dur + outro_fade:.3f}" data-track-index="4"
             data-fade-in="{outro_fade}" style="z-index:900">
          <div class="scene-inner">
            <div class="card-bg"></div>
            <div class="card-glow"></div>
            <div class="card-content">
              <div class="outro-title">{outro_text}</div>
              <div class="outro-sub">{title}</div>
            </div>
          </div>
        </div>''')
        sfx_events.append(("transition", content_end - outro_fade))
        total = content_end + outro_dur

    template = TEMPLATE_FILE.read_text(encoding='utf-8')
    subtitle_bg = "rgba(0, 0, 0, 0.55)" if design.get("subtitle_background", "band") == "band" else "transparent"
    card = design.get("card", {})
    replacements = {
        "<!-- TOTAL_DURATION -->": f"{total:.3f}",
        "<!-- INSERT_CLIPS_HERE -->": "\n".join(clips),
        "__FONT_FAMILY__": design.get("font_family", "'Noto Sans JP', sans-serif").replace("Noto Sans JP", "'Noto Sans JP'"),
        "__SUBTITLE_FONT_SIZE__": str(design.get("subtitle_font_size", 58)),
        "__TEXT_COLOR__": design.get("text_color", "#FFFFFF"),
        "__STROKE_COLOR__": design.get("stroke_color", "#000000"),
        "__HIGHLIGHT_COLOR__": design.get("highlight_color", "#FFD700"),
        "__SUBTITLE_BG__": subtitle_bg,
        "__BAND_HEIGHT__": str(int(design.get("subtitle_band_height", 210))),
        "__CARD_BG_A__": card.get("bg_a", "#0b1020"),
        "__CARD_BG_B__": card.get("bg_b", "#101830"),
        "__ACCENT_A__": card.get("accent_a", "#5a8cff"),
        "__ACCENT_B__": card.get("accent_b", "#00c8ff"),
    }
    for k, v in replacements.items():
        template = template.replace(k, v)
    html_path.write_text(template, encoding='utf-8')

    modes = "+".join(sorted(set(subtitle_modes))) if subtitle_modes else "off"
    log(f"   - {html_path} を生成しました。(合計尺: {total:.1f}秒 / テロップ {subtitle_count}枚 / 同期: {modes})")
    if intro_dur > 0:
        timeline.insert(0, {"kind": "intro", "start": 0.0, "end": intro_dur})
    if outro_cfg.get("enabled", True):
        timeline.append({"kind": "outro", "start": round(content_end, 3), "end": round(total, 3)})
    return {"html_path": html_path, "sfx_events": sfx_events, "total": total,
            "subtitle_count": subtitle_count, "subtitle_mode": modes, "subtitles_enabled": subtitles_on,
            "timeline": timeline}


# =====================================================================
# 5. レンダリング
# =====================================================================

def _lint_findings(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("findings", "results", "issues", "diagnostics"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def render_hyperframes(video_title, strict=False):
    log("5. HyperFramesで映像をレンダリングします...")
    project_work = WORK_DIR / video_title
    output_video = project_work / "render.mp4"
    cli = get_pinned_cli()

    lint_errors = []
    try:
        result = subprocess.run(cli + ["lint", "--json"], cwd=str(APP_DIR),
                                capture_output=True, text=True, timeout=300)
        data = json.loads(result.stdout) if result.stdout.strip() else {}
        findings = _lint_findings(data)
        lint_errors = [f for f in findings if isinstance(f, dict) and f.get("severity") == "error"]
        warns = [f for f in findings if isinstance(f, dict) and f.get("severity") == "warning"]
        log(f"   - lint: エラー {len(lint_errors)}件 / 警告 {len(warns)}件")
        for e in lint_errors[:5]:
            log(f"     [ERROR] {e.get('rule', e.get('id', ''))}: {e.get('message', e)}")
    except Exception as e:
        log(f"   [INFO] lint スキップ: {e}")
    if strict and lint_errors:
        raise RuntimeError(f"lint エラー {len(lint_errors)}件のためレンダリングを中止しました（--strict）")

    run_command(cli + ["render", "--quality", "high", "--output", str(output_video.absolute())],
                cwd=str(APP_DIR))
    if not output_video.exists():
        raise FileNotFoundError(f"レンダリングに失敗しました: {output_video}")
    return output_video, len(lint_errors)


# =====================================================================
# 6. 音響仕上げ（BGMベッド → ダッキング → SFX → 2パス loudnorm）
# =====================================================================

def select_bgm(video_title, mood, style, bgm_hint=None):
    """BGM を決定的に選ぶ。優先順: 台本の `# BGM:` 指示 > プリセットの bgm_mood > 台本ムード判定。
    ファイル名指定ならそのファイル、ムード名ならそのフォルダから タイトルhash で1曲"""
    audio_cfg = style["audio"]
    min_len = float(audio_cfg.get("bgm_min_length_sec", 15))
    bgm_root = ASSETS_DIR / "bgm"

    if bgm_hint:
        hint = bgm_hint.strip()
        if hint.lower() in ("none", "off", "なし"):
            log("   - 台本指示により BGM なし")
            return None, "none"
        if hint.lower() in MOODS:
            mood = hint.lower()
        else:
            for m in MOODS:
                for f in list_bgm_files(bgm_root / m):
                    if f.name.lower() == hint.lower() or f.stem.lower() == hint.lower():
                        return f, m
            log(f"   [WARNING] `# BGM: {hint}` に一致するファイルが assets/bgm/ にありません → 自動選曲します")
    else:
        override = audio_cfg.get("bgm_mood", "auto")
        if override and override != "auto":
            mood = override

    def usable(files):
        ok = []
        for f in files:
            d = ffprobe_duration(f)
            if d >= min_len:
                ok.append(f)
            else:
                log(f"   [WARNING] {f.name} は {d:.0f}秒 と短すぎるため除外（{min_len:.0f}秒以上を推奨）")
        return ok

    files = usable(list_bgm_files(bgm_root / mood))
    used_mood = mood
    if not files:
        for m in MOODS:
            if m == mood:
                continue
            files = usable(list_bgm_files(bgm_root / m))
            if files:
                log(f"   [INFO] {mood} に曲がないため {m} から選曲します")
                used_mood = m
                break
    if not files:
        return None, mood
    idx = int(hashlib.sha256(video_title.encode('utf-8')).hexdigest(), 16) % len(files)
    return files[idx], used_mood


def build_bgm_bed(bgm_path, total_duration, style, work_dir):
    """動画尺ぶんの BGM ベッド（wav）を作る。曲が短ければ acrossfade でつなぎ、レベル・フェードを適用"""
    audio_cfg = style["audio"]
    bgm_db = float(audio_cfg.get("bgm_volume_db", -18))
    fi = float(audio_cfg.get("fade_in_duration", 2.0))
    fo = float(audio_cfg.get("fade_out_duration", 4.0))
    xf = float(audio_cfg.get("bgm_crossfade", 2.0))
    bed = work_dir / "bgm_bed.wav"

    length = ffprobe_duration(bgm_path)
    need = total_duration + 0.5
    if length >= need:
        n = 1
    else:
        n = math.ceil(need / max(length - xf, 1.0)) + 1
        n = min(n, 60)
    inputs = []
    for _ in range(n):
        inputs += ["-i", str(bgm_path)]
    filters = []
    for k in range(n):
        filters.append(f"[{k}:a]aformat=sample_rates=48000:channel_layouts=stereo[b{k}]")
    prev = "[b0]"
    for k in range(1, n):
        filters.append(f"{prev}[b{k}]acrossfade=d={xf}:c1=tri:c2=tri[x{k}]")
        prev = f"[x{k}]"
    fade_out_start = max(0.0, total_duration - fo)
    filters.append(
        f"{prev}atrim=0:{total_duration:.3f},asetpts=PTS-STARTPTS,"
        f"volume={bgm_db}dB,afade=t=in:st=0:d={fi},afade=t=out:st={fade_out_start:.3f}:d={fo}[bed]")
    run_ffmpeg(inputs + ["-filter_complex", ";".join(filters), "-map", "[bed]",
                         "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", bed])
    meas = measure_ebur128(bed)
    info = {"file": bgm_path.name, "source_length": round(length, 1), "loops": n,
            "bed_lufs": meas["I"], "bed_lra": meas["LRA"]}
    log(f"   - BGMベッド生成: {bgm_path.name}（曲長 {length:.0f}s × {n}本 / LRA {meas['LRA']} LU）")
    return bed, info


def apply_ffmpeg_processing(video_title, render_video, style, parsed_data, sfx_events, total_duration):
    log("6. FFmpegでBGM・SFX・ラウドネス仕上げを行います...")
    project_output = OUTPUT_DIR / video_title
    project_output.mkdir(parents=True, exist_ok=True)
    project_work = WORK_DIR / video_title
    final_video = project_output / "final.mp4"

    audio_cfg = style["audio"]
    sfx_cfg = style.get("sfx", {})
    mood = determine_mood(parsed_data.get('script_blocks', []))
    bgm_path, used_mood = select_bgm(video_title, mood, style, parsed_data.get("bgm_hint"))
    bgm_info = {"mood_detected": mood, "mood_used": used_mood, "file": None, "credit": ""}
    log(f"   - ムード判定: {mood} → 使用: {used_mood} / BGM: {bgm_path.name if bgm_path else 'なし'}")
    if bgm_path:
        sidecar = bgm_search.read_sidecar(bgm_path)
        if sidecar:
            bgm_info["credit"] = bgm_search.credits_text(sidecar)
            bgm_info["license"] = bgm_search.license_label(sidecar)

    bed = None
    if bgm_path:
        try:
            bed, bed_info = build_bgm_bed(bgm_path, total_duration, style, project_work)
            bgm_info.update(bed_info)
        except Exception as e:
            log(f"   [WARNING] BGMベッド生成に失敗: {e} → BGM なしで続行します")
            bed = None
    else:
        log("   [WARNING] BGM なしで出力します。assets/bgm/<mood>/ に音楽ファイルを置いてください（assets/bgm/README.md）")

    th = float(audio_cfg.get("duck_threshold", 0.03))
    ratio = float(audio_cfg.get("duck_ratio", 8))
    atk = float(audio_cfg.get("duck_attack_ms", 20))
    rel = float(audio_cfg.get("duck_release_ms", 400))

    inputs = ["-i", str(render_video)]
    filters = []
    if bed:
        inputs += ["-i", str(bed)]
        filters.append("[0:a]asplit=2[vo][sc]")
        filters.append(
            f"[1:a][sc]sidechaincompress=threshold={th}:ratio={ratio}:attack={atk}:release={rel}[duck]")
        mix_srcs = ["[vo]", "[duck]"]
        n_input = 2
    else:
        filters.append("[0:a]anull[vo]")
        mix_srcs = ["[vo]"]
        n_input = 1

    sfx_dir = ASSETS_DIR / "sfx"
    sfx_used = 0
    if sfx_cfg.get("enabled", True):
        for kind, t in sfx_events:
            fname = sfx_cfg.get(f"{kind}_file", "")
            vol = float(sfx_cfg.get(f"{kind}_volume", 0.25))
            fpath = sfx_dir / fname
            if not fname or not fpath.exists() or t < 0:
                continue
            inputs += ["-i", str(fpath)]
            label = f"sfx{n_input}"
            filters.append(
                f"[{n_input}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                f"adelay={int(t * 1000)}:all=1,volume={vol}[{label}]")
            mix_srcs.append(f"[{label}]")
            n_input += 1
            sfx_used += 1

    mixed_wav = project_work / "mixed.wav"
    filters.append(f"{''.join(mix_srcs)}amix=inputs={len(mix_srcs)}:duration=first:dropout_transition=0:normalize=0[mix]")
    run_ffmpeg(inputs + ["-filter_complex", ";".join(filters), "-map", "[mix]",
                         "-ar", "48000", "-c:a", "pcm_s16le", mixed_wav])

    target_i = float(audio_cfg.get("loudness_target_lufs", -14))
    target_tp = float(audio_cfg.get("loudness_true_peak", -1.5))
    log("   - ラウドネス測定中 (1パス目)...")
    meas = measure_loudness(mixed_wav)
    log(f"   - 測定値: I={meas['input_i']} LUFS / TP={meas['input_tp']} dBTP → {target_i} LUFSへ正規化")
    normalized_wav = project_work / "normalized.wav"
    ln = (f"loudnorm=I={target_i}:TP={target_tp}:LRA=11:"
          f"measured_I={meas['input_i']}:measured_TP={meas['input_tp']}:"
          f"measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}:"
          f"offset={meas['target_offset']}:linear=true")
    run_ffmpeg(["-i", mixed_wav, "-af", ln, "-ar", "48000", "-c:a", "pcm_s16le", normalized_wav])

    run_ffmpeg(["-i", render_video, "-i", normalized_wav,
                "-map", "0:v", "-map", "1:a",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "384k", "-ar", "48000",
                "-movflags", "+faststart", "-shortest", final_video])
    if not final_video.exists():
        raise FileNotFoundError(f"最終動画の出力に失敗しました: {final_video}")
    log(f"   - 最終動画を出力しました: {final_video}")
    return final_video, {"bgm": bgm_info, "bgm_used": bed is not None, "sfx_used": sfx_used}


# =====================================================================
# 7. 品質ゲート（毎回同じ基準で機械判定）
# =====================================================================

def make_contact_sheet(video, out_path, frames=12):
    """動画全体から等間隔に frames 枚を抜いて1枚に並べる（AI・人が一目で確認するため）"""
    dur = max(ffprobe_duration(video), 1.0)
    cols = 4
    rows = math.ceil(frames / cols)
    interval = dur / frames
    run_ffmpeg(["-i", video, "-vf", f"fps=1/{interval:.4f},scale=480:-1,tile={cols}x{rows}",
                "-frames:v", "1", "-q:v", "3", out_path])
    return out_path


def quality_gate(ctx):
    """閾値付きの検品。戻り値 {"status": PASS|WARN|FAIL, "checks": [...]} """
    log("7. 品質ゲート（尺・ストリーム・ラウドネス・BGM・テロップ）...")
    qa = ctx["style"].get("qa", {})
    audio_cfg = ctx["style"]["audio"]
    final_video = ctx["final_video"]
    checks = []

    def add(name, status, detail):
        checks.append({"name": name, "status": status, "detail": detail})
        log(f"   [{status}] {name}: {detail}")

    dur = ffprobe_duration(final_video)
    tol = float(qa.get("duration_tolerance_sec", 1.5))
    add("尺", "PASS" if abs(dur - ctx["total"]) <= tol else "FAIL",
        f"実測 {dur:.1f}s / 期待 {ctx['total']:.1f}s")

    types = ffprobe_stream_types(final_video)
    add("ストリーム", "PASS" if {"video", "audio"} <= types else "FAIL", f"{sorted(types)}")

    meas = measure_loudness(final_video)
    li = float(meas["input_i"])
    tp = float(meas["input_tp"])
    target_i = float(audio_cfg.get("loudness_target_lufs", -14))
    warn_tol = float(qa.get("loudness_tolerance_warn", 0.7))
    fail_tol = float(qa.get("loudness_tolerance_fail", 1.5))
    diff = abs(li - target_i)
    add("ラウドネス", "PASS" if diff <= warn_tol else ("WARN" if diff <= fail_tol else "FAIL"),
        f"{li:.2f} LUFS（目標 {target_i}）")
    tp_fail = float(qa.get("true_peak_fail_dbtp", -1.0))
    add("トゥルーピーク", "PASS" if tp <= tp_fail else "FAIL", f"{tp:.2f} dBTP（上限 {tp_fail}）")

    bgm = ctx["audio_info"]["bgm"]
    if ctx["audio_info"]["bgm_used"]:
        lra_min = float(qa.get("bgm_lra_min", 1.5))
        lra = bgm.get("bed_lra")
        if lra is not None and lra < lra_min:
            add("BGM", "WARN", f"{bgm['file']} は抑揚がほぼ無い持続音の可能性（LRA {lra} LU < {lra_min}）。音楽ファイルか確認してください")
        else:
            add("BGM", "PASS", f"{bgm['file']}（{bgm['mood_used']} / LRA {lra} LU）")
        # ナレーション区間と無声区間のレベル差（ダッキングの実効確認）
        st = ctx.get("sentence_probe")
        if st:
            v = rms_db(final_video, st["voice_t"], 0.8)
            g = rms_db(final_video, st["gap_t"], 0.35)
            if v is not None and g is not None:
                add("BGMバランス", "PASS" if (v - g) >= 6 else "WARN",
                    f"ナレーション中 {v:.1f} dB / 無声区間 {g:.1f} dB（差 {v - g:.1f} dB）")
        if bgm.get("credit"):
            add("クレジット", "PASS", f"{bgm.get('license')} → output の credits.txt を動画の概要欄に貼ってください")
        else:
            add("クレジット", "PASS", "出典情報なし（手持ち曲）。assets/bgm/LICENSES.md で条件を確認してください")
    else:
        add("BGM", "WARN" if bgm.get("mood_used") != "none" else "PASS",
            "BGM なし" + ("" if bgm.get("mood_used") == "none" else "（./run.sh --bgm-candidates で候補を出すか assets/bgm/ に音楽を追加してください）"))

    tts = ctx["tts_info"]
    if tts["fallbacks"]:
        add("TTS", "WARN", f"Fish Audio 指定ですが {tts['fallbacks']}文が edge-tts にフォールバック（.env のキー / 残高を確認）")
    elif tts["silences"]:
        add("TTS", "FAIL", f"{tts['silences']}文が無音で代替されました（ネット接続を確認）")
    else:
        add("TTS", "PASS", f"{tts['engine_expected']} ({tts['model']}) 生成 {tts['generated']}文 / キャッシュ {tts['cached']}文")

    comp = ctx["comp"]
    scenes = [e for e in comp.get("timeline", []) if e["kind"] == "scene"]
    if scenes and all(e["position"] == "off" for e in scenes):
        add("テロップ", "PASS", "無効（storyboard.json で全場面 off）")
    elif comp["subtitles_enabled"]:
        add("テロップ", "PASS" if comp["subtitle_count"] > 0 else "FAIL",
            f"{comp['subtitle_count']}枚 / 同期: {comp['subtitle_mode']}")
    else:
        add("テロップ", "PASS", "無効（design.subtitles_enabled=false）")

    if ctx["lint_errors"]:
        add("lint", "WARN", f"コンポジションの lint エラー {ctx['lint_errors']}件（npm run check で詳細）")
    else:
        add("lint", "PASS", "エラー 0")

    for w in ctx["parse_warnings"]:
        add("台本", "WARN", w)

    sheet = None
    try:
        sheet = make_contact_sheet(final_video, ctx["work_dir"] / "contact_sheet.jpg",
                                   int(qa.get("contact_sheet_frames", 12)))
        add("コンタクトシート", "PASS", str(sheet))
    except Exception as e:
        add("コンタクトシート", "WARN", f"生成失敗: {e}")

    statuses = {c["status"] for c in checks}
    status = "FAIL" if "FAIL" in statuses else ("WARN" if "WARN" in statuses else "PASS")
    log(f"   => 品質ゲート: {status}")
    return {"status": status, "checks": checks, "duration": round(dur, 2),
            "loudness_i": li, "loudness_tp": tp, "contact_sheet": str(sheet) if sheet else None}


# =====================================================================
# 8. BGM 候補（3曲プレビュー → 選択）
# =====================================================================

def resolve_mood(parsed, style):
    """select_bgm と同じ優先順でムードを決める: 台本 `# BGM: <mood>` > プリセット bgm_mood > 台本判定"""
    hint = (parsed.get("bgm_hint") or "").strip().lower()
    if hint in MOODS:
        return hint
    override = style["audio"].get("bgm_mood", "auto")
    if override and override != "auto" and override in MOODS:
        return override
    return determine_mood(parsed.get("script_blocks", []))


def _probe_track(path):
    return ffprobe_duration(path), measure_ebur128(path)["LRA"]


def bgm_candidates(video_title, base_style, query=None, count=3):
    """台本のムードに合う BGM を手持ち＋Openverse から集め、冒頭プレビュー（ナレーション＋BGM）を書き出す"""
    project_work = WORK_DIR / video_title
    project_work.mkdir(parents=True, exist_ok=True)
    set_log_file(project_work / "bgm_candidates.log")
    log(f"--- BGM 候補出し: {video_title} ---")
    parsed = parse_input(video_title)
    style = resolve_style(base_style, parsed.get("preset"), parsed.get("project_style"))
    cfg = style.get("bgm_search", {})
    mood = resolve_mood(parsed, style)
    min_sec = float(cfg.get("min_seconds", 30))
    min_lra = float(style.get("qa", {}).get("bgm_lra_min", 1.2))
    log(f"   - ムード: {mood} / 候補数: {count}")

    candidates = []
    # 手持ちライブラリから最大1曲（タイトルhashで決定的）
    local = [f for f in list_bgm_files(ASSETS_DIR / "bgm" / mood) if ffprobe_duration(f) >= min_sec]
    if local:
        f = local[int(hashlib.sha256(video_title.encode("utf-8")).hexdigest(), 16) % len(local)]
        sc = bgm_search.read_sidecar(f) or {}
        candidates.append({
            "source": "local", "path": str(f), "slug": f.stem, "mood": mood,
            "title": sc.get("title") or f.stem, "creator": sc.get("creator") or "（手持ち）",
            "license": sc.get("license") or "", "license_label": bgm_search.license_label(sc) if sc else "手持ち（LICENSES.md 参照）",
            "duration_s": round(ffprobe_duration(f), 1), "provider": sc.get("provider") or "local",
            "source_url": sc.get("source_url") or "", "attribution": sc.get("attribution") or "",
        })
        log(f"   - 手持ちライブラリから: {f.name}")

    # Openverse で残りを充足
    need = count - len(candidates)
    if need > 0 and cfg.get("enabled", True):
        # 検索語は最大3つ分をまとめて集めてからスコア上位を選ぶ（1語だけだと曲調が偏る）
        queries = [query] if query else list(cfg.get("queries", {}).get(mood) or bgm_search.MOOD_QUERIES[mood])
        queries = queries[:int(cfg.get("max_queries", 3))]
        tracks = []
        for q in queries:
            found = bgm_search.search_openverse(q, cfg, log)
            if found is None:  # 接続不可（残りの検索語は試しても無駄）
                log("   [WARNING] Openverse に接続できないため、手持ちライブラリだけで候補を作ります")
                break
            tracks += found
        picked = bgm_search.pick_candidates(tracks, need + 4, mood, cfg)
        picked_paths = []
        for t in picked:
            p = bgm_search.download_track(t, WORK_DIR / "_bgm_cache", _probe_track, log, min_lra, min_sec)
            if p:
                picked_paths.append((t, p))
            if len(picked_paths) >= need:
                break
        for t, p in picked_paths[:need]:
            candidates.append({
                "source": "openverse", "path": str(p), "cache_path": str(p), "slug": bgm_search.slug_for(t), "mood": mood,
                "title": t["title"], "creator": t["creator"], "license": t["license"],
                "license_label": bgm_search.license_label(t), "duration_s": t["duration_s"], "lra": t.get("lra"),
                "provider": t["provider"], "source_url": t["source_url"], "attribution": t["attribution"],
                "url": t["url"], "id": t["id"], "license_version": t["license_version"], "license_url": t["license_url"],
                "tags": t["tags"][:8],
            })
    if not candidates:
        log("   [ERROR] 候補が見つかりません。--bgm-query で検索語を変えるか、assets/bgm/<mood>/ に曲を追加してください")
        return EXIT_QA_FAIL

    # プレビュー用ナレーション: 先頭ブロックから preview_seconds ぶん（足りなければ次のブロックも結合。TTS はキャッシュ再利用）
    target = float(cfg.get("preview_seconds", 20))
    audio_data, _ = generate_tts(parsed["script_blocks"][:4], video_title, style,
                                 parsed.get("storyboard"), parsed.get("readings"))
    parts, total = [], 0.0
    for a in audio_data:
        parts.append(a)
        total += a["duration"]
        if total >= target:
            break
    if len(parts) == 1:
        voice = Path(parts[0]["audio_path"])
    else:
        voice = project_work / "preview_voice.wav"
        inputs = []
        for a in parts:
            inputs += ["-i", a["audio_path"]]
        chain = "".join(f"[{k}:a]" for k in range(len(parts)))
        run_ffmpeg(inputs + ["-filter_complex", f"{chain}concat=n={len(parts)}:v=0:a=1[out]",
                             "-map", "[out]", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", voice])
    preview_len = min(target, max(5.0, total))
    a = style["audio"]
    out_dir = OUTPUT_DIR / video_title / "bgm_candidates"
    if out_dir.exists():
        for old in out_dir.glob("*"):
            old.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, c in enumerate(candidates, 1):
        bed, _info = build_bgm_bed(Path(c["path"]), preview_len, style, project_work)
        preview = out_dir / f"{i}_{c['slug']}.mp3"
        run_ffmpeg(["-i", voice, "-i", bed, "-filter_complex",
                    f"[0:a]atrim=0:{preview_len:.3f},asetpts=PTS-STARTPTS,asplit=2[vo][sc];"
                    f"[1:a][sc]sidechaincompress=threshold={a.get('duck_threshold', 0.03)}:ratio={a.get('duck_ratio', 8)}:"
                    f"attack={a.get('duck_attack_ms', 20)}:release={a.get('duck_release_ms', 400)}[duck];"
                    f"[vo][duck]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
                    f"loudnorm=I={a.get('loudness_target_lufs', -14)}:TP={a.get('loudness_true_peak', -1.5)}[out]",
                    "-map", "[out]", "-ar", "48000", "-c:a", "libmp3lame", "-b:a", "160k", preview])
        c["number"] = i
        c["preview"] = str(preview)

    (out_dir / "candidates.json").write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# BGM 候補（{video_title} / ムード: {mood}）", "",
             "| # | 曲名 | 作者 | ライセンス | 長さ | 出典 | プレビュー |", "|---|---|---|---|---|---|---|"]
    for c in candidates:
        lines.append(f"| {c['number']} | {c['title']} | {c['creator']} | {c['license_label']} | {c['duration_s']:.0f}s | "
                     f"{c['provider']} {c['source_url']} | {c['preview']} |")
    lines += ["", f"選ぶ: `./run.sh --bgm-choose <番号> --project {video_title}`"]
    (out_dir / "candidates.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log("=== BGM 候補 ===")
    for c in candidates:
        log(f"  [{c['number']}] {c['title']} / {c['creator']} / {c['license_label']} / {c['duration_s']:.0f}s / {c['provider']}")
        log(f"      プレビュー: {c['preview']}")
    log(f"  一覧: {out_dir / 'candidates.md'}")
    log(f"  次: ./run.sh --bgm-choose <番号> --project {video_title}")
    set_log_file(None)
    return EXIT_OK


def bgm_choose(video_title, n):
    """候補 n を採用: assets/bgm/<mood>/ に保存・LICENSES.md 追記・台本に # BGM: 行・credits.txt"""
    import datetime
    cands_path = OUTPUT_DIR / video_title / "bgm_candidates" / "candidates.json"
    if not cands_path.exists():
        log(f"[ERROR] 候補がありません。先に ./run.sh --bgm-candidates --project {video_title} を実行してください")
        return EXIT_ERROR
    cands = json.loads(cands_path.read_text(encoding="utf-8"))
    if not (1 <= n <= len(cands)):
        log(f"[ERROR] 番号は 1〜{len(cands)} で指定してください")
        return EXIT_ERROR
    c = cands[n - 1]
    mood = c["mood"]
    if c["source"] == "local":
        dest = Path(c["path"])
    else:
        dest = ASSETS_DIR / "bgm" / mood / f"{c['slug']}.mp3"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.copy2(c["path"], dest)
        bgm_search.write_sidecar(dest, c)
        lic_md = ASSETS_DIR / "bgm" / "LICENSES.md"
        existing = lic_md.read_text(encoding="utf-8") if lic_md.exists() else ""
        if dest.name not in existing:
            row = bgm_search.licenses_md_row(dest.name, mood, c, datetime.date.today().isoformat())
            with open(lic_md, "a", encoding="utf-8") as f:
                f.write(row + "\n")
    # 台本の先頭 `# BGM:` 行だけを更新（本文は触らない）
    script = None
    for cand in [INBOX_DIR / video_title / "script.md"] + sorted((INBOX_DIR / video_title).glob("*.md")):
        if cand.exists():
            script = cand
            break
    content = script.read_text(encoding="utf-8")
    directive = f"# BGM: {dest.name}"
    if re.search(r'^#+\s*BGM\s*[:：].*$', content, re.MULTILINE | re.IGNORECASE):
        content = re.sub(r'^#+\s*BGM\s*[:：].*$', directive, content, count=1, flags=re.MULTILINE | re.IGNORECASE)
    else:
        content = directive + "\n" + content
    script.write_text(content, encoding="utf-8")
    sidecar = bgm_search.read_sidecar(dest)
    credit = bgm_search.credits_text(sidecar) if sidecar else ""
    out_dir = OUTPUT_DIR / video_title
    out_dir.mkdir(parents=True, exist_ok=True)
    if credit:
        (out_dir / "credits.txt").write_text(credit + "\n", encoding="utf-8")
    log(f"=== BGM 決定: [{n}] {c['title']} / {c['creator']} ({c['license_label']}) ===")
    log(f"  保存先: {dest}")
    log(f"  台本に追記: {directive}（{script.name}）")
    if credit:
        log(f"  概要欄用クレジット: {credit}")
        log(f"  → {out_dir / 'credits.txt'}")
    log(f"  次: ./run.sh --project {video_title}")
    return EXIT_OK


# =====================================================================
# main
# =====================================================================

_KANA_DIGITS = "れい いち に さん よん ご ろく なな はち きゅう".split()


def _num_to_kana(num_str):
    """整数・小数をおおまかな読みに（照合用。音便は代表的なものだけ）"""
    if "." in num_str:
        a, b = num_str.split(".", 1)
        return _num_to_kana(a or "0") + "てん" + "".join(_KANA_DIGITS[int(c)] for c in b)
    n = int(num_str)
    if n == 0:
        return "ぜろ"
    special = {3: {100: "さんびゃく", 1000: "さんぜん"}, 6: {100: "ろっぴゃく"}, 8: {100: "はっぴゃく", 1000: "はっせん"}}
    out = ""
    for unit, name in ((10 ** 8, "おく"), (10 ** 4, "まん")):
        if n >= unit:
            out += _num_to_kana(str(n // unit)) + name
            n %= unit
    for unit, name in ((1000, "せん"), (100, "ひゃく"), (10, "じゅう")):
        d = n // unit
        if d:
            out += special.get(d, {}).get(unit) or (("" if d == 1 else _KANA_DIGITS[d]) + name)
        n %= unit
    if n:
        out += _KANA_DIGITS[n]
    return out


def to_kana(text, kks):
    """照合用に「読み」へそろえる: 記号除去 → 数字と % を読みに → 漢字かな交じりをひらがなに"""
    import unicodedata
    t = unicodedata.normalize("NFKC", text)
    t = t.replace("%", "パーセント")
    t = re.sub(r"\d+(?:\.\d+)?", lambda m: _num_to_kana(m.group(0).replace(",", "")), t.replace(",", ""))
    t = re.sub(r"[\s、。，．,.!?！？「」『』（）()・:：;；\-ー―~〜…\"']", lambda m: "ー" if m.group(0) == "ー" else "", t)
    hira = "".join(x["hira"] for x in kks.convert(t))
    return hira.lower()


def voice_check(video_title, base_style):
    """読み間違いの検出: 文ごとの音声を文字起こしし、台本と「ひらがなの読み」で照合する。
    漢字どうしで比べると同音異字（制作/政策）で誤検出するため、両方を読みに直してから比べる。
    出力: work/<動画名>/voice_check.md / .json（一致率の低い順）"""
    import difflib
    try:
        import pykakasi
    except ImportError:
        raise RuntimeError("pykakasi がありません。./setup.sh を実行するか venv/bin/pip install -r requirements.txt を実行してください")
    kks = pykakasi.kakasi()

    project_work = WORK_DIR / video_title
    project_work.mkdir(parents=True, exist_ok=True)
    set_log_file(project_work / "voice_check.log")   # 本番の run.log を上書きしない
    log(f"--- 読み上げチェック: {video_title} ---")
    parsed = parse_input(video_title)
    style = resolve_style(base_style, parsed.get("preset"), parsed.get("project_style"))
    audio_data, tts_info = generate_tts(parsed['script_blocks'], video_title, style,
                                        parsed.get("storyboard"), parsed.get("readings"))
    tcfg = style.get("transcribe", {})
    threshold = float(style.get("qa", {}).get("voice_match_min", 0.9))
    cli = get_pinned_cli()
    rows = []
    for block in audio_data:
        for seg in block["segments"]:
            wav = Path(seg["path"])
            cache = wav.with_suffix(".asr.json")
            if cache.exists():
                heard = json.loads(cache.read_text(encoding="utf-8"))["text"]
            else:
                tmp_dir = project_work / "asr_tmp"
                tmp_dir.mkdir(exist_ok=True)
                tmp_wav = tmp_dir / wav.name     # transcribe は入力の隣に transcript.json を書くので、専用フォルダで回す
                shutil.copy2(wav, tmp_wav)
                result = subprocess.run(cli + ["transcribe", str(tmp_wav), "--model", tcfg.get("model", "small"),
                                               "--language", tcfg.get("language", "ja"), "--json"],
                                        cwd=str(APP_DIR), capture_output=True, text=True, timeout=900)
                envelope = json.loads(result.stdout.strip() or "{}") if result.returncode == 0 else {}
                if not envelope.get("ok"):
                    raise RuntimeError(f"文字起こしに失敗しました: {result.stderr[-300:] or result.stdout[-300:]}")
                words = json.loads(Path(envelope["transcriptPath"]).read_text(encoding="utf-8"))
                heard = "".join(w.get("text", "") for w in words)
                cache.write_text(json.dumps({"text": heard}, ensure_ascii=False), encoding="utf-8")
                shutil.rmtree(tmp_dir)
            expected_kana = to_kana(seg["spoken"], kks)
            # 聞き取り側にも同じ辞書を当てる（「1本」→「いっぽん」を辞書に書いたとき、文字起こしの「1本」を「いちほん」と読んで誤検出しないため）
            heard_kana = to_kana(apply_readings(heard, parsed.get("readings") or {}), kks)
            sm = difflib.SequenceMatcher(None, expected_kana, heard_kana, autojunk=False)
            diffs = [f"「{expected_kana[i1:i2]}」→「{heard_kana[j1:j2]}」"
                     for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal"]
            # 長い文の中の1語の読み違いは一致率に表れにくいので、2文字以上のずれがあれば一致率に関係なく要確認にする
            big = any(max(i2 - i1, j2 - j1) >= 2 for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal")
            match = round(sm.ratio(), 3)
            rows.append({"slide": block["slide"], "script": seg["text"], "spoken": seg["spoken"], "heard": heard,
                         "match": match, "flag": match < threshold or big, "diffs": diffs})

    rows.sort(key=lambda r: r["match"])
    flagged = [r for r in rows if r["flag"]]
    (project_work / "voice_check.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# 読み上げチェック: {video_title}", "",
             f"TTS: {tts_info['engine_expected']} ({tts_info['model']}) / {len(rows)}文 / 要確認 ⚠️: {len(flagged)}文（一致率 {threshold} 未満、または2文字以上のずれ）",
             "", "一致率は「台本の読み」と「聞き取った読み」の近さ。文字起こし側の誤り（語尾・助詞の脱落など）も混ざるので、"
             "ずれの箇所を見て **声が本当に読み間違えたものだけ** を reading.json に足す。", "",
             "| 一致率 | スライド | 台本 | 聞き取り | ずれ（台本の読み→聞き取り） |", "|---|---|---|---|---|"]
    for r in rows:
        mark = "⚠️ " if r["flag"] else ""
        lines.append(f"| {mark}{r['match']} | {r['slide']} | {r['script']} | {r['heard']} | {' / '.join(r['diffs'][:6])} |")
    (project_work / "voice_check.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if tts_info.get("fallbacks"):
        log(f"   [WARNING] Fish Audio に失敗し {tts_info['fallbacks']}文が edge-tts の声になっています（キー失効・残高不足の可能性）")
    log(f"--- 読み上げチェック完了: {len(rows)}文中 {len(flagged)}文が要確認 → {project_work / 'voice_check.md'} ---")
    return flagged


def draft_review(video_title, base_style):
    """下書き: 全尺を書き出さずに、テロップ1枚ごとの表示中央などのコマだけを撮る（自己レビュー用・数十秒）。
    出力: work/<動画名>/review/NNN_*.jpg と frames.json / frames.md（どのコマが何の場面か）"""
    project_work = WORK_DIR / video_title
    project_work.mkdir(parents=True, exist_ok=True)
    set_log_file(project_work / "draft.log")   # 本番の run.log を上書きしない
    log(f"--- 下書き（自己レビュー用コマ）: {video_title} ---")

    parsed = parse_input(video_title)
    style = resolve_style(base_style, parsed.get("preset"), parsed.get("project_style"))
    audio_data, _ = generate_tts(parsed['script_blocks'], video_title, style,
                                 parsed.get("storyboard"), parsed.get("readings"))
    comp = generate_hyperframes_config(parsed, audio_data, style, video_title)

    shots = []
    subtitled = {e["slide"] for e in comp["timeline"] if e["kind"] == "subtitle"}
    for e in comp["timeline"]:
        if e["kind"] == "subtitle":
            t = (e["start"] + e["end"]) / 2
        elif e["kind"] == "scene":
            for fx in e.get("focus", []):     # 演出ごとの真ん中も撮る（1枚のテロップ中に演出が切り替わる場面を見落とさない）
                shots.append({"kind": "focus", "slide": e["slide"], "position": e["position"],
                              "t": round((fx["start"] + fx["end"]) / 2, 2)})
            if e["slide"] in subtitled:
                continue
            t = (e["start"] + e["end"]) / 2          # テロップを出さない場面も1コマは見る
        elif e["kind"] in ("intro", "outro"):
            t = e["start"] + min(1.6, (e["end"] - e["start"]) * 0.6)
        else:
            continue
        shots.append({**e, "t": round(t, 2)})
    shots.sort(key=lambda x: x["t"])
    seen = set()
    shots = [s for s in shots if not (s["t"] in seen or seen.add(s["t"]))]

    review_dir = project_work / "review"
    raw_dir = project_work / "review_raw"
    for d in (review_dir, raw_dir):
        if d.exists():
            shutil.rmtree(d)
    review_dir.mkdir(parents=True)
    log(f"   - コマを {len(shots)} 枚撮影します...")
    run_command(get_pinned_cli() + ["snapshot", "--at", ",".join(f"{s['t']}" for s in shots),
                                    "--no-end", "--describe", "false", "-o", str(raw_dir.absolute()), "."],
                cwd=str(APP_DIR))
    # snapshot の連番は2桁ゼロ埋め（frame-99, frame-100…）なので文字列順ではなく番号順に並べる
    raws = sorted(raw_dir.glob("frame-*.png"), key=lambda p: int(p.name.split("-")[1]))
    if len(raws) != len(shots):
        raise RuntimeError(f"コマの枚数が合いません（予定 {len(shots)} / 実際 {len(raws)}）。{raw_dir} を確認してください")

    frames = []
    for n, (shot, raw) in enumerate(zip(shots, raws), start=1):
        label = f"s{shot['slide']:02d}" if "slide" in shot else shot["kind"]
        name = f"{n:03d}_{label}.jpg"
        with Image.open(raw) as im:
            im = im.convert("RGB")
            im.thumbnail((1280, 1280))
            im.save(review_dir / name, "JPEG", quality=88)
        active = [f"{fx['effect']}(文{fx['sentence']})" for sc in comp["timeline"]
                  if sc["kind"] == "scene" and sc["slide"] == shot.get("slide")
                  for fx in sc.get("focus", []) if fx["start"] <= shot["t"] <= fx["end"]]
        text = shot.get("text") or next((e["text"] for e in comp["timeline"] if e["kind"] == "subtitle"
                                         and e["start"] <= shot["t"] < e["end"]), None)
        frames.append({"file": name, "t": shot["t"], "kind": shot["kind"], "slide": shot.get("slide"),
                       "subtitle_position": shot.get("position"), "text": text, "focus": active})
    shutil.rmtree(raw_dir)

    (review_dir / "frames.json").write_text(json.dumps(frames, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["| コマ | 秒 | 種類 | スライド | テロップ | 演出 |", "|---|---|---|---|---|---|"]
    for f in frames:
        text = (f["text"] or "").replace("\n", " / ")
        lines.append(f"| {f['file']} | {f['t']} | {f['kind']} | {f['slide'] or ''} | {text} | {', '.join(f['focus'])} |")
    (review_dir / "frames.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"--- 下書き完了: {review_dir}（{len(frames)} コマ・一覧は frames.md） ---")
    return review_dir


def process_project(video_title, base_style, strict=False):
    project_work = WORK_DIR / video_title
    project_work.mkdir(parents=True, exist_ok=True)
    set_log_file(project_work / "run.log")
    log(f"--- プロジェクト処理開始: {video_title} ---")

    parsed = parse_input(video_title)
    style = resolve_style(base_style, parsed.get("preset"), parsed.get("project_style"))
    audio_data, tts_info = generate_tts(parsed['script_blocks'], video_title, style,
                                        parsed.get("storyboard"), parsed.get("readings"))
    comp = generate_hyperframes_config(parsed, audio_data, style, video_title)
    render_video, lint_errors = render_hyperframes(video_title, strict=strict)
    final_video, audio_info = apply_ffmpeg_processing(
        video_title, render_video, style, parsed, comp["sfx_events"], comp["total"])

    # ダッキング実測用に「最初の文の途中」と「最初の文と2文目の間」の時刻を控える
    probe = None
    intro = float(style.get("intro", {}).get("duration", 3.0)) if style.get("intro", {}).get("enabled", True) else 0.0
    st = audio_data[0].get("sentence_times") or []
    tail = float(style["audio"].get("tail_silence", 0.6))
    if st:
        voice_t = intro + st[0]["start"] + 0.3
        if len(st) >= 2:
            gap_t = intro + st[0]["end"] + 0.02          # 文間の無音
        elif len(audio_data) >= 2:
            gap_t = intro + audio_data[0]["duration"] - tail * 0.6   # ブロック末尾の無音パディング
        else:
            gap_t = None
        if gap_t is not None:
            probe = {"voice_t": voice_t, "gap_t": gap_t}

    qa = quality_gate({
        "style": style, "final_video": final_video, "total": comp["total"],
        "audio_info": audio_info, "tts_info": tts_info, "comp": comp,
        "lint_errors": lint_errors, "parse_warnings": parsed["warnings"],
        "work_dir": project_work, "sentence_probe": probe,
    })

    summary = {
        "project": video_title,
        "title": parsed.get("title"),
        "status": qa["status"],
        "output": str(final_video),
        "contact_sheet": None,
        "slides": len(parsed['slides']),
        "narrated_blocks": len(parsed['script_blocks']),
        "total_duration": round(comp["total"], 3),
        "measured": {"duration": qa["duration"], "loudness_i": qa["loudness_i"], "loudness_tp": qa["loudness_tp"]},
        "tts": tts_info,
        "bgm": audio_info["bgm"],
        "subtitles": {"count": comp["subtitle_count"], "mode": comp["subtitle_mode"]},
        "checks": qa["checks"],
        "log": str(project_work / "run.log"),
    }
    out_dir = OUTPUT_DIR / video_title
    if qa.get("contact_sheet"):
        dst = out_dir / "contact_sheet.jpg"
        shutil.copy2(qa["contact_sheet"], dst)
        summary["contact_sheet"] = str(dst)
    if audio_info["bgm"].get("credit"):
        credits_path = out_dir / "credits.txt"
        credits_path.write_text(audio_info["bgm"]["credit"] + "\n", encoding="utf-8")
        summary["credits"] = str(credits_path)
    for p in (project_work / "build_summary.json", out_dir / "build_summary.json"):
        p.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    log(f"--- プロジェクト処理完了: {video_title} [{qa['status']}] ---")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="Slide2Video AI Generator")
    parser.add_argument("--check", action="store_true", help="環境チェックだけ行って終了")
    parser.add_argument("--project", help="inbox 内の特定プロジェクトだけ処理")
    parser.add_argument("--strict", action="store_true", help="lint エラーがあればレンダリング前に停止")
    parser.add_argument("--bgm-candidates", action="store_true", help="BGM 候補を3曲探してプレビューを書き出す（本番生成はしない）")
    parser.add_argument("--bgm-choose", type=int, metavar="N", help="BGM 候補 N を採用して台本に # BGM: を書く")
    parser.add_argument("--bgm-query", help="--bgm-candidates の検索語を指定（英語。例: 'upbeat ukulele'）")
    parser.add_argument("--count", type=int, default=3, help="--bgm-candidates の候補数（既定 3）")
    parser.add_argument("--voice-check", action="store_true",
                        help="音声を文字起こしして台本と読みを照合し work/<動画名>/voice_check.md に書く（読み間違いの検出）")
    parser.add_argument("--draft", action="store_true",
                        help="全尺を書き出さず、テロップごとのコマだけ work/<動画名>/review/ に撮る（自己レビュー用）")
    args = parser.parse_args(argv)

    log("=== 自動制作パイプライン開始 ===")
    base_style = json.loads(STYLE_FILE.read_text(encoding='utf-8'))
    try:
        preflight_check(base_style)
    except PreflightError as e:
        log(f"[ERROR] {e}")
        return EXIT_PREFLIGHT
    if args.check:
        return EXIT_OK

    if not INBOX_DIR.exists():
        log("[ERROR] inbox フォルダがありません。")
        return EXIT_ERROR
    projects = [d for d in INBOX_DIR.iterdir() if d.is_dir() and not d.name.startswith('.')]
    if args.project:
        projects = [d for d in projects if d.name == args.project]
        if not projects:
            log(f"[ERROR] inbox/{args.project} がありません。")
            return EXIT_ERROR
    if not projects:
        log("inbox に処理対象のプロジェクトがありません（inbox/<動画名>/ に PDF と script.md を置いてください）。")
        return EXIT_OK

    if args.draft or args.voice_check:
        failed = False
        for project_dir in sorted(projects):
            try:
                if args.voice_check:
                    voice_check(project_dir.name, base_style)
                else:
                    draft_review(project_dir.name, base_style)
            except Exception as e:
                import traceback
                log(f"[ERROR] {project_dir.name} の{'読み上げチェック' if args.voice_check else '下書き'}でエラー: {e}")
                traceback.print_exc()
                failed = True
            finally:
                set_log_file(None)
        return EXIT_ERROR if failed else EXIT_OK

    if args.bgm_candidates or args.bgm_choose is not None:
        if len(projects) != 1:
            log(f"[ERROR] 対象が {len(projects)} 件あります。--project <動画名> で1つ指定してください: "
                + ", ".join(d.name for d in sorted(projects)))
            return EXIT_ERROR
        title = projects[0].name
        try:
            if args.bgm_choose is not None:
                return bgm_choose(title, args.bgm_choose)
            return bgm_candidates(title, base_style, query=args.bgm_query, count=max(1, args.count))
        except Exception as e:
            import traceback
            log(f"[ERROR] BGM 候補処理でエラー: {e}")
            traceback.print_exc()
            return EXIT_ERROR
        finally:
            set_log_file(None)

    results = []
    for project_dir in sorted(projects):
        try:
            results.append(process_project(project_dir.name, base_style, strict=args.strict))
        except Exception as e:
            import traceback
            log(f"[ERROR] プロジェクト {project_dir.name} の処理中にエラーが発生しました: {e}")
            traceback.print_exc()
            results.append({"project": project_dir.name, "status": "ERROR", "error": str(e)})
        finally:
            set_log_file(None)

    log("=== 結果サマリ ===")
    for r in results:
        if r["status"] == "ERROR":
            log(f"  {r['project']}: ERROR — {r['error']}")
            continue
        m = r["measured"]
        log(f"  {r['project']}: {r['status']} — {m['duration']}s / {m['loudness_i']} LUFS / "
            f"BGM: {r['bgm'].get('file') or 'なし'} / TTS: {r['tts']['engine_expected']} → {r['output']}")
        if r.get("contact_sheet"):
            log(f"    確認用: {r['contact_sheet']}")
        for c in r["checks"]:
            if c["status"] != "PASS":
                log(f"    [{c['status']}] {c['name']}: {c['detail']}")

    if any(r["status"] == "ERROR" for r in results):
        return EXIT_ERROR
    if any(r["status"] == "FAIL" for r in results):
        return EXIT_QA_FAIL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

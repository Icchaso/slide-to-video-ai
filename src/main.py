import os
import sys
import json
import subprocess
import re
import hashlib
import shutil
from pathlib import Path
from pdf2image import convert_from_path
import requests
from dotenv import load_dotenv

# load .env
load_dotenv()

# --- Configuration Paths ---
BASE_DIR = Path(__file__).resolve().parent.parent
INBOX_DIR = BASE_DIR / "inbox"
OUTPUT_DIR = BASE_DIR / "output"
WORK_DIR = BASE_DIR / "work"
ASSETS_DIR = BASE_DIR / "assets"
STYLE_FILE = BASE_DIR / "src" / "video-style.json"
APP_DIR = BASE_DIR / "hyperframes-app"


def log(msg):
    print(f"[slide-video] {msg}")


def run_command(cmd, cwd=None, capture=False):
    log(f"Running command: {' '.join(str(c) for c in cmd)}")
    if capture:
        return subprocess.run(cmd, check=True, cwd=cwd, capture_output=True, text=True)
    return subprocess.run(cmd, check=True, cwd=cwd)


def ffprobe_duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True)
    return float(result.stdout.strip())


def deep_merge(base, override):
    """辞書を再帰マージ（overrideが勝つ）。プリセット/プロジェクト別スタイルの合成に使用"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def resolve_style(base_style, preset_name, project_style):
    """基本スタイル → プリセット → プロジェクト個別設定 の順に上書き合成"""
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
    """hyperframes-app/package.json のピンからCLIコマンドを組み立てる（未ピンnpx実行の乖離バグ対策）"""
    try:
        pkg = json.loads((APP_DIR / "package.json").read_text(encoding="utf-8"))
        m = re.search(r"hyperframes@([\d.]+)", pkg["scripts"]["render"])
        if m:
            return ["npx", "--yes", f"hyperframes@{m.group(1)}"]
    except Exception as e:
        log(f"   [WARNING] ピンの解決に失敗: {e}。hyperframes@latest を使用します。")
    return ["npx", "--yes", "hyperframes@latest"]


# =====================================================================
# 1. 入力パース
# =====================================================================

def determine_mood(script_blocks):
    """台本テキストから動画のムード（upbeat, relaxing, serious）を判定する。"""
    full_text = "".join([b.get('text', '') for b in script_blocks])
    serious_keywords = ["課題", "問題", "深刻", "減少", "対策", "リスク", "注意"]
    upbeat_keywords = ["最高", "おすすめ", "新製品", "新登場", "大ヒット", "突破", "達成", "嬉しい"]
    serious_score = sum(full_text.count(kw) for kw in serious_keywords)
    upbeat_score = sum(full_text.count(kw) for kw in upbeat_keywords)
    if serious_score > upbeat_score and serious_score > 0:
        return "serious"
    elif upbeat_score > serious_score and upbeat_score > 0:
        return "upbeat"
    return "relaxing"


def parse_input(video_title):
    log(f"1. [{video_title}] スライドと台本のパースを開始します...")
    project_inbox = INBOX_DIR / video_title
    project_work = WORK_DIR / video_title
    slides_dir = project_work / "slides"
    slides_dir.mkdir(parents=True, exist_ok=True)

    pdf_path = project_inbox / "slides.pdf"
    pptx_path = project_inbox / "slides.pptx"
    if not pdf_path.exists():
        # ファイル名固定でないPDFも拾う
        pdfs = sorted(project_inbox.glob("*.pdf"))
        if pdfs:
            pdf_path = pdfs[0]
        elif pptx_path.exists():
            raise FileNotFoundError(
                f"PDFが見つかりません。{pptx_path.name} を手動でPDF形式に書き出して配置してください。")
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
            raise FileNotFoundError(f"台本が見つかりません: {project_inbox}")
        script_path = mds[0]

    log("   - 台本(Markdown)を解析中...")
    content = script_path.read_text(encoding='utf-8')

    # タイトル抽出: 「# Title: ...」行（イントロカード用）
    video_display_title = video_title
    m = re.search(r'^#+\s*Title\s*[:：]\s*(.+)$', content, re.MULTILINE | re.IGNORECASE)
    if m:
        video_display_title = m.group(1).strip()
        content = content.replace(m.group(0), '')

    # スタイルプリセット抽出: 「# Style: pop」行（動画ごとの雰囲気切替）
    preset_name = None
    m = re.search(r'^#+\s*Style\s*[:：]\s*(\S+)\s*$', content, re.MULTILINE | re.IGNORECASE)
    if m:
        preset_name = m.group(1).strip().lower()
        content = content.replace(m.group(0), '')

    # プロジェクト個別スタイル（inbox/<project>/style.json）
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
            if current_text:
                text_str = '\n'.join(current_text).strip()
                if text_str:
                    blocks.append({"slide": current_slide, "text": text_str})
            current_slide = int(match.group(1))
            current_text = []
        else:
            current_text.append(line)
    if current_text:
        text_str = '\n'.join(current_text).strip()
        if text_str:
            blocks.append({"slide": current_slide, "text": text_str})

    log(f"   - {len(blocks)}個の台本ブロックを抽出しました。タイトル: {video_display_title}")
    return {
        "slides": slide_images,
        "script_blocks": blocks,
        "title": video_display_title,
        "preset": preset_name,
        "project_style": project_style,
    }


def parse_emphasis(text):
    """**強調** 記法を除去した平文と、強調範囲 [(start, end), ...] を返す"""
    plain = []
    spans = []
    i = 0
    pos = 0
    pattern = re.compile(r'\*\*(.+?)\*\*', re.DOTALL)
    for m in pattern.finditer(text):
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
# 2. TTS（Fish Audio → edge-tts フォールバック）＋無音パディング
# =====================================================================

def tts_text_of(block_text):
    """TTSに渡す平文（強調記法を除去し、改行を句点相当の区切りに）"""
    plain, _ = parse_emphasis(block_text)
    return re.sub(r'\n+', '\n', plain).strip()


def generate_tts(script_blocks, video_title, style):
    log("2. ナレーション音声を生成します...")
    audio_cfg = style.get("audio", {})
    lead = float(audio_cfg.get("lead_silence", 0.25))
    tail = float(audio_cfg.get("tail_silence", 0.6))

    api_key = os.environ.get("FISH_AUDIO_API_KEY", "")
    use_fish = bool(api_key) and api_key != "your_api_key_here"
    if not use_fish:
        log("   - FISH_AUDIO_API_KEY 未設定のため edge-tts (ja-JP-NanamiNeural) を使用します。")

    project_work = WORK_DIR / video_title
    audio_dir = project_work / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for i, block in enumerate(script_blocks):
        text = tts_text_of(block['text'])
        slide_num = block['slide']
        text_hash = hashlib.sha1(text.encode('utf-8')).hexdigest()[:10]
        raw_path = audio_dir / f"slide_{slide_num:03d}_{i:03d}_{text_hash}.mp3"
        padded_path = audio_dir / f"slide_{slide_num:03d}_{i:03d}_{text_hash}_padded.wav"

        if not padded_path.exists():
            if not raw_path.exists():
                log(f"   - スライド {slide_num} の音声を生成中... ({len(text)}文字)")
                generated = False
                if use_fish:
                    try:
                        payload = {
                            "text": text,
                            "format": "mp3",
                            "normalize": True,
                            "temperature": float(audio_cfg.get("tts_temperature", 0.5)),
                            "prosody": {"speed": float(audio_cfg.get("tts_speed", 1.0)), "volume": 0},
                        }
                        voice_id = os.getenv("FISH_AUDIO_VOICE_ID", "").strip()
                        if voice_id:
                            payload["reference_id"] = voice_id
                        response = requests.post(
                            "https://api.fish.audio/v1/tts",
                            headers={"Authorization": f"Bearer {api_key}",
                                     "Content-Type": "application/json"},
                            json=payload, timeout=120)
                        response.raise_for_status()
                        raw_path.write_bytes(response.content)
                        generated = True
                    except Exception as e:
                        log(f"   [WARNING] Fish Audio 失敗 (Slide {slide_num}): {e} → edge-tts にフォールバック")
                if not generated:
                    speed = float(audio_cfg.get("tts_speed", 1.0))
                    rate = f"{'+' if speed >= 1.0 else ''}{round((speed - 1.0) * 100)}%"
                    text_clean = text.replace('"', '').replace("'", "")
                    try:
                        run_command(["edge-tts", "--voice", "ja-JP-NanamiNeural",
                                     f"--rate={rate}", "--text", text_clean,
                                     "--write-media", str(raw_path)])
                    except Exception as e:
                        log(f"   [ERROR] edge-tts も失敗: {e} → 3秒の無音で代替します")
                        run_command(["ffmpeg", "-y", "-f", "lavfi",
                                     "-i", "anullsrc=r=48000:cl=stereo", "-t", "3",
                                     "-q:a", "9", "-acodec", "libmp3lame", str(raw_path)])
            # 前後に無音パディング（間を作る）+ 48kHz stereo 統一
            lead_ms = int(lead * 1000)
            run_command(["ffmpeg", "-y", "-i", str(raw_path),
                         "-af", f"adelay={lead_ms}:all=1,apad=pad_dur={tail}",
                         "-ar", "48000", "-ac", "2", str(padded_path)])
        else:
            log(f"   - スライド {slide_num}: キャッシュ音声を再利用")

        results.append({
            "slide": slide_num,
            "audio_path": str(padded_path),
            "duration": ffprobe_duration(padded_path),
        })
    return results


# =====================================================================
# 3. 文字起こし（word-levelタイムスタンプ）→ テロップ同期
# =====================================================================

def transcribe_words(audio_path, style):
    """hyperframes transcribe でワードタイムスタンプを取得。失敗時は None（推定にフォールバック）"""
    tcfg = style.get("transcribe", {})
    if not tcfg.get("enabled", True):
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
        # stdout: {"ok":true, "transcriptPath": "..."} 形式（transcriptPathに文単位の[{text,start,end}]）
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


NORM_RE = re.compile(r'[^0-9A-Za-zぁ-んァ-ヶ一-龯ー々]')


def norm_chars(s):
    return NORM_RE.sub('', s)


PARTICLE_CHARS = 'をはがでにとへも'
NO_BREAK_NEXT = '。、！？」）ゃゅょんっー'


def find_break_pos(text, max_len, min_pos):
    """自然な分割位置を探す: 読点直後 > 助詞直後 > max_len（語の途中切れを防ぐ）"""
    limit = min(max_len, len(text) - 1)
    window = text[:limit]
    p = window.rfind('、')
    if p + 1 >= min_pos:
        return p + 1
    for i in range(limit - 1, min_pos - 1, -1):
        if text[i] in PARTICLE_CHARS and text[i + 1] not in NO_BREAK_NEXT:
            return i + 1
    return max_len


def build_chunks(plain_text, kw_spans, max_per_line, max_lines):
    """台本平文を放送基準（1行max_per_line文字×max_lines行）のテロップチャンクに分割"""
    max_screen = max_per_line * max_lines
    # 文分割（。！？改行）
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
        # 読点で刻んで詰め直す
        pieces = [p for p in re.split(r'(?<=、)', s) if p]
        cur = ""
        for p in pieces:
            while len(p) > max_screen:  # 読点なしの超長文は助詞境界で強制分割
                if cur:
                    chunks.append(cur)
                    cur = ""
                bp = find_break_pos(p, max_screen, int(max_screen * 0.5))
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

    # 各チャンクの平文内での文字範囲を特定（キーワード強調の対応付けに使用）
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
    """チャンクを最大2行に分割（読点優先 > 助詞境界 > 中央バランス）"""
    if len(chunk) <= max_per_line:
        return chunk
    mid = len(chunk) / 2
    cands = [m.end() for m in re.finditer('、', chunk) if 0 < m.end() < len(chunk)]
    cands = [p for p in cands if p <= max_per_line and len(chunk) - p <= max_per_line]
    if not cands:
        cands = [i + 1 for i, ch in enumerate(chunk[:-1])
                 if ch in PARTICLE_CHARS and chunk[i + 1] not in NO_BREAK_NEXT]
        cands = [p for p in cands if p <= max_per_line and len(chunk) - p <= max_per_line]
    if cands:
        bp = min(cands, key=lambda p: abs(p - mid))
    else:
        bp = min(max_per_line, int(mid + 0.5))
    return chunk[:bp] + '\n' + chunk[bp:]


def chunk_html(chunk_text, chunk_range, kw_spans, max_per_line):
    """キーワード強調spanを埋め込んだテロップHTMLを生成"""
    c0, c1 = chunk_range
    text = split_lines(chunk_text, max_per_line)
    # 改行挿入後のインデックス補正のため、平文インデックス→表示文字列インデックスを構築
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


def time_chunks(chunks, ranges, plain_text, words, duration, lead_time, lead_silence, tail_silence):
    """各チャンクの表示時間を決定。wordsがあれば実発話タイミング、なければ文字数比で推定"""
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
            # 台本文字位置→転写文字位置（比例マップで認識ゆらぎを吸収）
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
            # 表示開始 = 発話のlead_time前。連続チャンクは隣接させ重複ゼロにする（lintのtrack overlap対策）
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
            return timings, "whisper"
        log("   [INFO] 転写文字数が台本と乖離しているため文字数比推定にフォールバック")

    # フォールバック: 文字数比（発話区間 = パディングを除いた範囲）
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


def generate_hyperframes_config(parsed_data, audio_data, style, video_title):
    log("4. HyperFrames用のHTMLコンポジションを生成します...")
    html_path = APP_DIR / "index.html"
    assets_out = APP_DIR / "assets"
    assets_out.mkdir(parents=True, exist_ok=True)

    design = style.get("design", {})
    anim = style.get("animation", {})
    intro_cfg = style.get("intro", {})
    outro_cfg = style.get("outro", {})
    audio_cfg = style.get("audio", {})

    overlap = float(anim.get("transition_duration", 0.6))
    kb_on = bool(anim.get("ken_burns", True))
    kb_zoom = float(anim.get("ken_burns_zoom", 1.06))
    lead_time = float(anim.get("subtitle_lead_time", 0.08))
    max_per_line = int(design.get("subtitle_max_chars_per_line", 16))
    max_lines = int(design.get("subtitle_max_lines", 2))
    lead_silence = float(audio_cfg.get("lead_silence", 0.25))
    tail_silence = float(audio_cfg.get("tail_silence", 0.6))

    slides = parsed_data['slides']
    clips = []
    sfx_events = []
    subtitle_modes = []

    intro_dur = float(intro_cfg.get("duration", 3.0)) if intro_cfg.get("enabled", True) else 0.0
    content_t0 = intro_dur
    current = content_t0

    # --- スライド・音声・テロップ ---
    for i, item in enumerate(audio_data):
        slide_idx = item['slide'] - 1
        duration = max(item['duration'], 1.5)
        start = current
        is_last = (i == len(audio_data) - 1)

        # アセットコピー
        bg_rel = ""
        if slide_idx < len(slides):
            src = Path(slides[slide_idx])
            shutil.copy2(src, assets_out / src.name)
            bg_rel = f"assets/{src.name}"
        asrc = Path(item['audio_path'])
        shutil.copy2(asrc, assets_out / asrc.name)
        audio_rel = f"assets/{asrc.name}"

        # スライドシーン（クロスフェード分だけ次のスライドの下に延長）
        clip_dur = duration + (0 if is_last else overlap)
        kb = KB_ROTATION[i % len(KB_ROTATION)] if kb_on else "none"
        fade_in = overlap if (i > 0 or intro_dur > 0) else 0
        track = i % 2  # 交互トラックでオーバーラップを許可
        z = 11 + i
        clips.append(f'''
        <!-- Slide {slide_idx + 1} -->
        <div class="clip slide-scene" data-start="{start:.3f}" data-duration="{clip_dur:.3f}" data-track-index="{track}"
             data-kb="{kb}" data-kb-zoom="{kb_zoom}" data-fade-in="{fade_in:.2f}" style="z-index:{z}">
          <div class="scene-inner">
            <div class="bg-blur" data-layout-allow-overflow><img src="{bg_rel}" /></div>
            <div class="kb-wrap"><img class="slide-img" src="{bg_rel}" /></div>
          </div>
        </div>
        <audio id="voice-{i}" class="clip" data-start="{start:.3f}" data-duration="{duration:.3f}" data-track-index="2" src="{audio_rel}"></audio>''')

        # テロップ（word-level同期）
        block_text = next((b['text'] for b in parsed_data['script_blocks']
                           if b['slide'] == item['slide']), "")
        plain, kw_spans = parse_emphasis(block_text)
        plain = plain.replace('\n', '')
        chunks, ranges = build_chunks(plain, kw_spans, max_per_line, max_lines)
        words = transcribe_words(item['audio_path'], style)
        timings, mode = time_chunks(chunks, ranges, plain, words, duration,
                                    lead_time, lead_silence, tail_silence)
        subtitle_modes.append(mode)
        for (chunk, rng, (t0, t1)) in zip(chunks, ranges, timings):
            html = chunk_html(chunk, rng, kw_spans, max_per_line)
            # start/endそれぞれをミリ秒に丸めてから差分をとる（丸め誤差による1msの重複を防ぐ）
            sa = round(start + t0, 3)
            ea = round(start + t1, 3)
            clips.append(f'''
        <div class="clip subtitle-wrapper" data-start="{sa:.3f}" data-duration="{max(0.15, round(ea - sa, 3)):.3f}" data-track-index="3" style="z-index:500">
          <div class="subtitle-box">{html}</div>
        </div>''')

        # 転換SFX（次のスライドが入ってくる瞬間）
        if not is_last:
            sfx_events.append(("transition", start + duration - 0.1))
        current = start + duration

    content_end = current

    # --- イントロカード ---
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

    # --- アウトロカード ---
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

    # --- テンプレート適用 ---
    template = (BASE_DIR / "src" / "template.html").read_text(encoding='utf-8')
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
        "__CARD_BG_A__": card.get("bg_a", "#0b1020"),
        "__CARD_BG_B__": card.get("bg_b", "#101830"),
        "__ACCENT_A__": card.get("accent_a", "#5a8cff"),
        "__ACCENT_B__": card.get("accent_b", "#00c8ff"),
    }
    for k, v in replacements.items():
        template = template.replace(k, v)
    html_path.write_text(template, encoding='utf-8')

    modes = set(subtitle_modes)
    log(f"   - {html_path} を生成しました。(合計尺: {total:.1f}秒 / テロップ同期: {'whisper' if modes == {'whisper'} else '+'.join(sorted(modes))})")
    return html_path, sfx_events, total


# =====================================================================
# 5. レンダリング
# =====================================================================

def render_hyperframes(video_title):
    log("5. HyperFramesで映像をレンダリングします...")
    project_work = WORK_DIR / video_title
    output_video = project_work / "render.mp4"
    cli = get_pinned_cli()

    # 事前lint（エラーがあればログに出す。自動パイプラインなので停止はしない）
    try:
        result = subprocess.run(cli + ["lint", "--json"], cwd=str(APP_DIR),
                                capture_output=True, text=True, timeout=300)
        data = json.loads(result.stdout) if result.stdout.strip() else {}
        findings = data.get("findings", data if isinstance(data, list) else [])
        errors = [f for f in findings if isinstance(f, dict) and f.get("severity") == "error"]
        if errors:
            log(f"   [WARNING] lint エラー {len(errors)}件:")
            for e in errors[:5]:
                log(f"     - {e.get('message', e)}")
    except Exception as e:
        log(f"   [INFO] lint スキップ: {e}")

    run_command(cli + ["render", "--quality", "high", "--output", str(output_video.absolute())],
                cwd=str(APP_DIR))
    if not output_video.exists():
        raise FileNotFoundError(f"レンダリングに失敗しました: {output_video}")
    return output_video


# =====================================================================
# 6. 音響仕上げ（SFX → BGMダッキング → 2パスラウドネス正規化）
# =====================================================================

def select_bgm(video_title, mood, style):
    """タイトルのハッシュで決定的にBGMを選ぶ（同じ入力なら常に同じ曲）"""
    override = style.get("audio", {}).get("bgm_mood", "auto")
    if override and override != "auto":
        mood = override
    bgm_dir = ASSETS_DIR / "bgm" / mood
    files = sorted(list(bgm_dir.glob("*.mp3")) + list(bgm_dir.glob("*.wav")))
    if not files:
        fallback = ASSETS_DIR / "bgm" / "relaxing"
        files = sorted(list(fallback.glob("*.mp3")) + list(fallback.glob("*.wav")))
    if not files:
        return None
    idx = int(hashlib.sha256(video_title.encode('utf-8')).hexdigest(), 16) % len(files)
    return files[idx]


def measure_loudness(path):
    """loudnorm 1パス目: 測定値JSONを返す"""
    result = subprocess.run(
        ["ffmpeg", "-i", str(path), "-af",
         "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True)
    m = re.search(r'\{[^{}]*"input_i"[^{}]*\}', result.stderr, re.DOTALL)
    if not m:
        raise RuntimeError("loudnorm測定値の取得に失敗")
    return json.loads(m.group(0))


def apply_ffmpeg_processing(video_title, render_video, style, parsed_data, sfx_events, total_duration):
    log("6. FFmpegでSFX・BGM・ラウドネス仕上げを行います...")
    project_output = OUTPUT_DIR / video_title
    project_output.mkdir(parents=True, exist_ok=True)
    project_work = WORK_DIR / video_title
    final_video = project_output / "final.mp4"

    audio_cfg = style.get("audio", {})
    sfx_cfg = style.get("sfx", {})
    mood = determine_mood(parsed_data.get('script_blocks', []))
    bgm_path = select_bgm(video_title, mood, style)
    log(f"   - ムード判定: {mood} / BGM: {bgm_path.name if bgm_path else 'なし'}")

    # --- Step A: ナレーション + BGM(事前レベル+ダッキング) + SFX をミックス ---
    bgm_db = float(audio_cfg.get("bgm_volume_db", -18))
    th = float(audio_cfg.get("duck_threshold", 0.03))
    ratio = float(audio_cfg.get("duck_ratio", 8))
    atk = float(audio_cfg.get("duck_attack_ms", 20))
    rel = float(audio_cfg.get("duck_release_ms", 400))
    fi = float(audio_cfg.get("fade_in_duration", 2.0))
    fo = float(audio_cfg.get("fade_out_duration", 4.0))

    inputs = ["-i", str(render_video)]
    filters = []
    mix_srcs = []

    if bgm_path:
        inputs += ["-stream_loop", "-1", "-i", str(bgm_path)]
        fade_out_start = max(0.0, total_duration - fo)
        filters.append(
            f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"volume={bgm_db}dB,afade=t=in:st=0:d={fi},afade=t=out:st={fade_out_start:.3f}:d={fo}[bgmv]")
        filters.append("[0:a]asplit=2[vo][sc]")
        filters.append(
            f"[bgmv][sc]sidechaincompress=threshold={th}:ratio={ratio}:attack={atk}:release={rel}[duck]")
        mix_srcs = ["[vo]", "[duck]"]
    else:
        filters.append("[0:a]anull[vo]")
        mix_srcs = ["[vo]"]

    # SFX（存在するファイルだけを重ねる）
    sfx_dir = ASSETS_DIR / "sfx"
    n_input = 2 if bgm_path else 1
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

    mixed_wav = project_work / "mixed.wav"
    amix = f"{''.join(mix_srcs)}amix=inputs={len(mix_srcs)}:duration=first:dropout_transition=0:normalize=0[mix]"
    filters.append(amix)
    run_command(["ffmpeg", "-y"] + inputs +
                ["-filter_complex", ";".join(filters),
                 "-map", "[mix]", "-ar", "48000", str(mixed_wav)])

    # --- Step B: 2パス loudnorm 正規化（-14 LUFS / TP -1.5） ---
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
    run_command(["ffmpeg", "-y", "-i", str(mixed_wav), "-af", ln,
                 "-ar", "48000", str(normalized_wav)])

    # --- Step C: 映像と正規化済み音声をmux（映像は再エンコードなし・faststart） ---
    run_command(["ffmpeg", "-y", "-i", str(render_video), "-i", str(normalized_wav),
                 "-map", "0:v", "-map", "1:a",
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "384k", "-ar", "48000",
                 "-movflags", "+faststart", "-shortest", str(final_video)])

    if not final_video.exists():
        raise FileNotFoundError(f"最終動画の出力に失敗しました: {final_video}")
    log(f"   - 最終動画を出力しました: {final_video}")
    return final_video


def quality_assurance_final(final_video, total_duration):
    log("7. 最終検品（尺・ストリーム・ラウドネス実測）...")
    dur = ffprobe_duration(final_video)
    if abs(dur - total_duration) > 1.5:
        log(f"   [WARNING] 尺のズレ: 期待 {total_duration:.1f}s / 実測 {dur:.1f}s")
    else:
        log(f"   - 尺OK: {dur:.1f}s (期待 {total_duration:.1f}s)")
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
         "-of", "csv=p=0", str(final_video)], capture_output=True, text=True)
    types = set(result.stdout.split())
    if not {"video", "audio"} <= types:
        raise RuntimeError(f"ストリーム欠落: {types}")
    meas = measure_loudness(final_video)
    log(f"   - 最終ラウドネス: I={meas['input_i']} LUFS / TP={meas['input_tp']} dBTP")
    return {"duration": dur, "loudness_i": meas['input_i'], "loudness_tp": meas['input_tp']}


# =====================================================================
# main
# =====================================================================

def main():
    log("=== 自動制作パイプライン開始 ===")
    if not INBOX_DIR.exists():
        log("inbox フォルダがありません。")
        sys.exit(1)
    projects = [d for d in INBOX_DIR.iterdir() if d.is_dir() and not d.name.startswith('.')]
    if not projects:
        log("inbox に処理対象のプロジェクトがありません。")
        return

    base_style = json.loads(STYLE_FILE.read_text(encoding='utf-8'))

    for project_dir in sorted(projects):
        video_title = project_dir.name
        log(f"--- プロジェクト処理開始: {video_title} ---")
        try:
            parsed_data = parse_input(video_title)
            style = resolve_style(base_style, parsed_data.get("preset"),
                                  parsed_data.get("project_style"))
            audio_data = generate_tts(parsed_data['script_blocks'], video_title, style)
            config_path, sfx_events, total = generate_hyperframes_config(
                parsed_data, audio_data, style, video_title)
            render_video = render_hyperframes(video_title)
            final_video = apply_ffmpeg_processing(
                video_title, render_video, style, parsed_data, sfx_events, total)
            qa = quality_assurance_final(final_video, total)
            summary = {
                "title": parsed_data.get("title"),
                "slides": len(parsed_data['slides']),
                "total_duration": total,
                "qa": qa,
            }
            (WORK_DIR / video_title / "build_summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
            log(f"--- プロジェクト処理完了: {video_title} ---")
        except Exception as e:
            log(f"[ERROR] プロジェクト {video_title} の処理中にエラーが発生しました: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()

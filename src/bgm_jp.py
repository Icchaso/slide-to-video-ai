"""
日本の定番フリー BGM サイトから候補を集める（自動で取るのは BGMer だけ）

規約を確認したうえで自動取得してよいサイトだけを使う（2026-09-17 確認・HANDOFF.md に経緯）:
- BGMer  https://bgmer.net/terms  — 商用・収益化チャンネル可。クレジット不要（任意で「音楽：BGMer https://bgmer.net」）。
  再配布も禁止していない。robots.txt は /wp-admin/ 以外を許可。一覧に DL 数があり「よく使われている曲」を選べる

自動取得しないサイト: OpenTracks（旧 DOVA-SYNDROME。規約で bot 収集を禁止）/ 甘茶の音楽工房（直リンク禁止）/
MusMus（広告ブロック中の DL 不可）/ 魔王魂（CC BY 4.0 だが mp3 を直接取ると 403 で拒否される＝望まれていないと判断）。
これらは候補一覧に「人が曲ページから落として assets/bgm/<mood>/ に置く」案内だけを出す。

負荷をかけない: 一覧ページは REQUEST_INTERVAL 秒あけて取り、カタログは work/_bgm_cache/ に7日キャッシュする。
音声は候補になった曲だけを取る（プレビューは短い版、本番採用時に長い版）。
"""

import html
import json
import re
import time
from pathlib import Path

import requests

USER_AGENT = "slide-to-video-ai/1.0 (+https://github.com/Icchaso/slide-to-video-ai)"
REQUEST_INTERVAL = 1.5
CACHE_DAYS = 7

# 曲調の「パターン」。BGMer の雰囲気・ジャンルのタグで振り分ける。上から順に優先して1つだけ付ける
PATTERNS = [
    ("軽快ポップ", {"ポップ"}, {"楽しい", "嬉しい", "元気", "明るい", "ポップ", "爽やか", "かわいい", "コミカル"}),
    ("おしゃれ・Chill", set(), {"おしゃれ", "Chill", "ムーディー", "優雅", "オシャレ", "カフェ"}),
    ("ほのぼの日常", {"アコースティック"}, {"日常", "のんびり", "ほのぼの", "穏やか", "のどか", "ゆったり", "リラックス", "安らぎ"}),
    ("前向き・感動", set(), {"感動", "切ない", "希望", "爽快", "壮大", "前向き"}),
    ("スタイリッシュ", {"EDM", "エレクトロ･8bit", "ロック", "ネオロック"}, {"クール", "かっこいい", "疾走感", "スタイリッシュ"}),
]


def _get(url, log):
    r = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    time.sleep(REQUEST_INTERVAL)
    return r.text


def _text(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def _secs(mmss):
    try:
        m, s = mmss.split(":")
        return int(m) * 60 + int(s)
    except Exception:
        return 0


def pattern_of(genres, moods):
    for name, g_keys, m_keys in PATTERNS:
        if (g_keys and set(genres) & g_keys and set(moods) & m_keys) or (not g_keys and set(moods) & m_keys):
            return name
    for name, g_keys, m_keys in PATTERNS:  # ジャンルか雰囲気のどちらか一方でも当たればそこへ
        if set(genres) & g_keys or set(moods) & m_keys:
            return name
    return "その他"


# ---------------------------------------------------------------- BGMer

def parse_bgmer_page(page_html):
    tracks = []
    for pid, body in re.findall(r'<div class="item" data-id="(\d+)">(.*?)(?=<div class="item" data-id=|$)', page_html, re.S):
        title = re.search(r'<h3>\s*&#9658;\s*(.*?)</h3>', body)
        page = re.search(r'<a href="(https://bgmer\.net/music/[^"]+)"', body)
        files = re.findall(r'data-file="([^"]+\.mp3)"[^>]*data-type="(short|long)"', body)
        if not (title and page and files):
            continue
        genres = [g.strip() for g in _text((re.search(r'<h5>(.*?)</h5>', body) or [None, ""])[1]).split(",") if g.strip()]
        desc = re.search(r'<p class="description">(.*?)</p>', body, re.S)
        mood_txt = desc.group(1).rsplit("</noscript>", 1)[-1] if desc else ""
        moods = [m.strip() for m in _text(mood_txt).split(",") if m.strip()]
        lens = re.findall(r'<small>([\d:]+)</small>', body)
        dl = re.search(r'numberOfDownload">(\d+)', body)
        urls = dict((t, u) for u, t in files)
        name = _text(title.group(1))
        vocal = any("vocal" in u.lower() for u in urls.values()) or "ボーカル" in genres
        tracks.append({
            "source": "bgmer", "id": f"bgmer-{pid}", "title": name, "creator": "BGMer", "page_url": page.group(1),
            "genres": genres, "moods": moods, "downloads": int(dl.group(1)) if dl else 0,
            "short_url": urls.get("short"), "long_url": urls.get("long"),
            "short_s": _secs(lens[0]) if lens else 0, "long_s": _secs(lens[1]) if len(lens) > 1 else 0,
            "vocal": vocal,
        })
    return tracks


def fetch_bgmer(log=print, max_pages=40):
    first = _get("https://bgmer.net/song-list/", log)
    last = max([int(n) for n in re.findall(r'song-list/page/(\d+)/', first)] or [1])
    tracks = parse_bgmer_page(first)
    for n in range(2, min(last, max_pages) + 1):
        tracks += parse_bgmer_page(_get(f"https://bgmer.net/song-list/page/{n}/", log))
    log(f"   - BGMer: {len(tracks)}曲（{min(last, max_pages)}ページ）")
    return tracks


# ---------------------------------------------------------------- カタログ

def load_catalog(cache_dir, log=print, refresh=False):
    """BGMer の曲一覧（メタデータのみ）。7日以内のキャッシュがあれば取りに行かない"""
    cache = Path(cache_dir) / "jp_catalog.json"
    if cache.exists() and not refresh and time.time() - cache.stat().st_mtime < CACHE_DAYS * 86400:
        data = json.loads(cache.read_text(encoding="utf-8"))
        log(f"   - 日本のフリーBGMカタログ（キャッシュ）: {len(data)}曲")
        return data
    data = []
    for name, fn in (("BGMer", fetch_bgmer),):
        try:
            data += fn(log)
        except Exception as e:
            log(f"   [WARNING] {name} の一覧を取得できません: {type(e).__name__} {e}")
    if data:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    elif cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
        log(f"   - 取得に失敗したため古いカタログを使います: {len(data)}曲")
    return data


# 解説・ビジネス動画に合わない曲（戦闘・恐怖・季節物・和風など）は候補にしない
UNSUITABLE = {"バトル", "怒り", "ピンチ", "ホラー", "恐怖", "不気味", "ラウド", "メタル", "和風", "クリスマス", "ハロウィン",
              "正月", "お祭り", "ダンジョン", "戦闘", "ボス", "魔王", "悲しい", "寂しい", "絶望", "遊園地", "人形", "8bit", "不思議", "怪しい", "まぬけ", "ピエロ"}


def pick_by_pattern(catalog, per_pattern=2, exclude_ids=()):
    """パターンごとに人気順（DL 数）で per_pattern 曲。歌あり・合わない曲は除く。同数は id 順で決定的"""
    by = {}
    for t in catalog:
        if t["vocal"] or t["id"] in exclude_ids or (set(t["genres"]) | set(t["moods"])) & UNSUITABLE:
            continue
        t = dict(t, pattern=pattern_of(t["genres"], t["moods"]))
        by.setdefault(t["pattern"], []).append(t)
    out = {}
    for name, _g, _m in PATTERNS:
        lst = by.get(name, [])
        out[name] = sorted(lst, key=lambda t: (-t["downloads"], t["id"]))[:per_pattern]
    return out


def credit_for(track):
    return "音楽：BGMer https://bgmer.net"  # 表記は任意だが、概要欄にそのまま貼れる書式にしておく


def license_label(track):
    return "BGMer 規約（商用可・表記不要）"


def reason_text(t, video_sec, dur_s, lra):
    parts = [t.get("pattern", "")]
    if t["source"] == "bgmer" and t.get("downloads"):
        parts.append(f"{t['downloads']:,}DL の定番" if t["downloads"] >= 50000 else f"{t['downloads']:,}DL")
    moods = t.get("moods", [])[:3]
    if moods:
        parts.append("雰囲気: " + "/".join(moods))
    full = max(dur_s or 0, t.get("long_s") or 0)
    if full >= video_sec:
        parts.append("動画より長くつなぎ目なし" + ("（長い版あり）" if (t.get("long_s") or 0) > (dur_s or 0) + 5 else ""))
    elif full > 0:
        parts.append(f"動画より短く{int(-(-video_sec // full))}回つなぐ")
    if lra is not None:
        parts.append("抑揚ひかえめ" if lra < 4 else ("抑揚ほどよい" if lra <= 10 else "抑揚大きめ"))
    return "・".join(p for p in parts if p)


MANUAL_SITES = [
    ("OpenTracks（旧 DOVA-SYNDROME）", "https://opentracks.com/", "規約で bot 収集を禁止しているため自動では取りません"),
    ("甘茶の音楽工房（ポップ）", "https://amachamusic.chagasi.com/genre_pop.html", "直リンク禁止・ダウンロードページから取る決まり"),
    ("MusMus", "https://musmus.main.jp/", "広告ブロック中のダウンロード不可・表記「BGM:MusMus」が必須"),
    ("魔王魂（アコースティック）", "https://maou.audio/category/bgm/bgm-acoustic/", "CC BY 4.0・表記「音楽：魔王魂」が必要。mp3 の直接取得は拒否される"),
]

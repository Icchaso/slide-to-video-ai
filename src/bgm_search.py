"""
BGM 候補の自動リサーチ — Openverse API（Creative Commons 素材の横断検索・無料・キー不要）

- 検索対象は CC BY / CC0 の楽曲のみ（CC BY は動画の概要欄にクレジット1行が必要 → credits.txt を自動生成）
- 匿名利用の上限は 20回/分・200回/日。1回の候補出しは最大3リクエスト
- 取得した曲には sidecar JSON（<音声ファイル>.json）を添えて出典・ライセンスを保持する
"""

import json
import os
import re
import time
from pathlib import Path

import requests

OPENVERSE_API = os.environ.get("OPENVERSE_API_URL", "https://api.openverse.org/v1/audio/")
USER_AGENT = "slide-to-video-ai/1.0 (+https://github.com/Icchaso/slide-to-video-ai)"

MOOD_QUERIES = {
    "relaxing": ["calm", "ambient", "acoustic", "relaxing"],
    "upbeat": ["upbeat", "happy", "corporate", "energetic"],
    "serious": ["inspiring", "documentary", "piano", "cinematic"],
}
MOOD_SPEEDS = {
    "relaxing": {"speed_low", "speed_medium"},
    "upbeat": {"speed_high", "speed_medium"},
    "serious": {"speed_low", "speed_medium"},
}
VOCAL_TAGS = {"vocal", "vocals", "voice", "singing", "female", "male", "rap", "choir"}


def search_openverse(query, cfg, log=print):
    """Openverse で楽曲を検索。0件は []、接続不可は None（呼び出し側は残りの検索を打ち切る）"""
    params = {
        "q": query,
        "category": "music",
        "license": ",".join(cfg.get("licenses", ["by", "cc0"])),
        "length": "short,medium",
        "page_size": int(cfg.get("page_size", 20)),
    }
    timeout = float(cfg.get("timeout_seconds", 45))
    last_err = None
    for attempt in (1, 2):  # API が遅いことがあるため1回だけリトライ
        t0 = time.time()
        try:
            r = requests.get(OPENVERSE_API, params=params, timeout=timeout,
                             headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            if r.status_code == 429:
                log("   [WARNING] Openverse の利用上限（匿名 20回/分・200回/日）に達しました。しばらく待つか手持ちライブラリを使ってください")
                return []
            r.raise_for_status()
            results = r.json().get("results", [])
            log(f"   - Openverse 検索「{query}」: {len(results)}件（{time.time() - t0:.1f}秒）")
            return [normalize_track(t) for t in results]
        except requests.exceptions.ConnectionError as e:
            log(f"   [WARNING] Openverse に接続できません（{query}）: ネット接続を確認してください")
            return None
        except Exception as e:
            last_err = e
            log(f"   [WARNING] Openverse 検索失敗（{query}・{attempt}回目・{time.time() - t0:.0f}秒）: {type(e).__name__}")
    log(f"   [WARNING] Openverse 検索を諦めました（{query}）: {last_err}")
    return []


def normalize_track(t):
    tags = {(x.get("name") or "").lower() for x in (t.get("tags") or [])}
    tags |= {(g or "").lower() for g in (t.get("genres") or [])}
    return {
        "id": str(t.get("id", "")),
        "title": (t.get("title") or "untitled").strip(),
        "creator": (t.get("creator") or "unknown").strip(),
        "license": (t.get("license") or "").lower(),
        "license_version": t.get("license_version") or "",
        "license_url": t.get("license_url") or "",
        "attribution": t.get("attribution") or "",
        "url": t.get("url") or "",
        "source_url": t.get("foreign_landing_url") or "",
        "provider": t.get("provider") or t.get("source") or "openverse",
        "duration_s": round((t.get("duration") or 0) / 1000.0, 1),
        "tags": sorted(tags),
        "mature": bool(t.get("mature")),
        "fields_matched": t.get("fields_matched") or [],
    }


def score_track(track, mood, cfg):
    """候補としての適性スコア。None は除外"""
    tags = set(track["tags"])
    dur = track["duration_s"]
    if track["mature"] or not track["url"]:
        return None
    if track["license"] not in set(cfg.get("licenses", ["by", "cc0"])):
        return None
    if tags & VOCAL_TAGS:
        return None
    if dur < float(cfg.get("min_seconds", 30)) or dur > float(cfg.get("max_seconds", 360)):
        return None
    score = 0.0
    if "instrumental" in tags:
        score += 3
    speeds = {t for t in tags if t.startswith("speed_")}
    if speeds:
        score += 2 if speeds & MOOD_SPEEDS.get(mood, set()) else -2
    if 60 <= dur <= 240:
        score += 1
    if "title" in track["fields_matched"]:
        score += 1
    if track["license"] == "cc0":
        score += 0.5
    return score


def pick_candidates(tracks, n, mood, cfg):
    """スコア順に n 件。同一作者は1曲まで（候補の多様性）。同点は曲名順で決定的"""
    scored = []
    seen_ids = set()
    for t in tracks:
        if t["id"] in seen_ids:
            continue
        seen_ids.add(t["id"])
        s = score_track(t, mood, cfg)
        if s is not None:
            scored.append((s, t))
    scored.sort(key=lambda x: (-x[0], x[1]["title"].lower()))
    out = []
    creators = set()
    for s, t in scored:
        key = t["creator"].lower()
        if key in creators:
            continue
        creators.add(key)
        t = dict(t, score=s)
        out.append(t)
        if len(out) >= n:
            break
    return out


def slug_for(track):
    base = re.sub(r"[^0-9A-Za-z]+", "_", track["title"]).strip("_").lower()[:40] or "track"
    prov = re.sub(r"[^0-9a-z]", "", track["provider"].lower())[:12]
    return f"{base}_{prov}{track['id']}"


def download_track(track, cache_dir, probe, log=print, min_lra=1.2, min_seconds=30, slug=None):
    """曲を work/_bgm_cache/ にDLし、ffprobe/ebur128 で検証。不適なら None"""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / f"{slug or slug_for(track)}.mp3"
    if not dest.exists():
        try:
            with requests.get(track["url"], stream=True, timeout=90,
                              headers={"User-Agent": USER_AGENT}) as r:
                r.raise_for_status()
                ctype = r.headers.get("content-type", "")
                if "audio" not in ctype and "octet" not in ctype and "force-download" not in ctype:
                    raise RuntimeError(f"音声でない応答: {ctype}")
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        f.write(chunk)
        except Exception as e:
            log(f"   [WARNING] DL 失敗: {track['title']} — {e}")
            dest.unlink(missing_ok=True)
            return None
    try:
        duration, lra = probe(dest)
    except Exception as e:
        log(f"   [WARNING] 検証失敗: {track['title']} — {e}")
        return None
    if duration < min_seconds:
        log(f"   [INFO] 除外（短すぎ {duration:.0f}s）: {track['title']}")
        return None
    if lra is not None and lra < min_lra:
        log(f"   [INFO] 除外（抑揚なし LRA {lra} LU）: {track['title']}")
        return None
    track["duration_s"] = round(duration, 1)
    track["lra"] = lra
    return dest


def sidecar_path(audio_path):
    return Path(f"{audio_path}.json")


def write_sidecar(audio_path, track):
    data = {k: track.get(k) for k in ("id", "title", "creator", "license", "license_version",
                                       "license_url", "attribution", "source_url", "provider",
                                       "duration_s", "url", "tags", "lra", "credit", "pattern", "long_url")}
    sidecar_path(audio_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def read_sidecar(audio_path):
    p = sidecar_path(audio_path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def license_label(sidecar):
    lic = (sidecar.get("license") or "").lower()
    if lic == "bgmer":
        return "BGMer 規約（商用可・表記不要）"
    ver = sidecar.get("license_version") or ""
    if lic == "cc0":
        return "CC0（クレジット不要）"
    if lic:
        return f"CC {lic.upper()} {ver}".strip()
    return "不明"


def credits_text(sidecar):
    """動画の概要欄にそのまま貼れるクレジット文"""
    if not sidecar:
        return ""
    if sidecar.get("credit"):  # 日本のフリー素材サイト（bgm_jp.credit_for が作った書式）
        return sidecar["credit"]
    lic = (sidecar.get("license") or "").lower()
    if lic == "cc0":
        return (f"Music: \"{sidecar.get('title')}\" by {sidecar.get('creator')} (CC0 — クレジット表記不要) "
                f"{sidecar.get('source_url') or ''}").strip()
    attr = sidecar.get("attribution") or f"\"{sidecar.get('title')}\" by {sidecar.get('creator')} is licensed under CC {lic.upper()} {sidecar.get('license_version') or ''}"
    attr = attr.split(" To view a copy")[0].strip()
    return f"Music: {attr} — {sidecar.get('source_url') or ''}".strip(" —")


def licenses_md_row(filename, mood, sidecar, date_str):
    return (f"| {filename} | {mood} | {sidecar.get('title')} / {sidecar.get('creator')} | "
            f"{sidecar.get('source_url') or ''} | {license_label(sidecar)} | {date_str} |")


def reason_text(c, video_sec):
    """おすすめの理由を、タグ・長さ・抑揚・ライセンスから短く組み立てる（人が聴く前の判断材料）"""
    tags = set(c.get("tags") or [])
    parts = []
    if c.get("source") == "local":
        parts.append("以前に採用した曲")
    if "instrumental" in tags:
        parts.append("歌なし")
    if c.get("query"):
        parts.append(f"検索語「{c['query']}」")
    speed = {"speed_low": "ゆったり", "speed_medium": "ふつうのテンポ", "speed_high": "速めのテンポ"}
    parts += [speed[t] for t in sorted(tags) if t in speed][:1]
    genre = [t for t in sorted(tags) if t not in speed and t != "instrumental" and len(t) <= 16][:2]
    if genre:
        parts.append("タグ: " + "/".join(genre))
    dur = float(c.get("duration_s") or 0)
    if dur >= video_sec:
        parts.append("動画より長くつなぎ目なし")
    elif dur > 0:
        parts.append(f"動画より短く{int(-(-video_sec // dur))}回つなぐ")
    lra = c.get("lra")
    if lra is not None:
        parts.append("抑揚ひかえめ" if lra < 4 else ("抑揚ほどよい" if lra <= 10 else "抑揚大きめ"))
    if (c.get("license") or "").lower() == "cc0":
        parts.append("クレジット不要")
    return "・".join(parts) or "—"

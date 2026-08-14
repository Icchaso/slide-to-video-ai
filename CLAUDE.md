# Slide2Video AI Generator — Project Instructions

## What this repo is

An automated pipeline: **PDF slides + Markdown script → TTS narration + synced subtitles + BGM → final MP4**.

```
inbox/<project>/slides.pdf + script.md
  → src/main.py            (Python orchestrator)
  → src/template.html      (composition template — EDIT THIS, not the generated file)
  → hyperframes-app/index.html   (GENERATED — never hand-edit)
  → npx hyperframes render (inside hyperframes-app/)
  → FFmpeg (SFX + BGM ducking + loudness normalize)
  → output/<project>/final.mp4
```

**IMPORTANT: `hyperframes-app/index.html` is overwritten by `src/main.py` on every run.**
To change the video's look, edit `src/template.html` and the generator logic in
`src/main.py` (`generate_hyperframes_config`). Style parameters live in `src/video-style.json`.

## Skills — USE THESE FIRST

**Doing anything with HyperFrames?** Start at `/hyperframes` — it routes intent to the right
workflow and domain skills (`/hyperframes-core`, `/hyperframes-animation`, `/hyperframes-cli`,
`/media-use`, …). Always invoke the relevant skill before writing or modifying compositions.
Skills encode framework-specific patterns (e.g., `window.__timelines` registration, `data-*`
attribute semantics) that are NOT in generic web docs.

## Commands

```bash
./setup.sh           # one-time: brew deps + venv + hyperframes-app install
./run.sh             # full pipeline: inbox/ → output/<project>/final.mp4
npm run dev          # preview server (proxies to hyperframes-app; long-running, run in background)
npm run check        # lint + runtime + layout + motion + contrast (proxies to hyperframes-app)
npm run render       # render current generated composition (proxies to hyperframes-app)
```

> **`npm run dev` is a long-running server.** In Claude Code, always run it with
> `run_in_background: true`.

> **Pinned CLI version.** `hyperframes-app/package.json` pins an exact `hyperframes@X.Y.Z`.
> To move up: `npx hyperframes@latest upgrade --project hyperframes-app --check` (shows the
> delta), then apply without `--check` and verify with `npm run check`.

## Project Structure

- `src/main.py` — pipeline orchestrator (parse → TTS → transcribe → compose → render → mix)
- `src/template.html` — composition template (Ken Burns, crossfades, subtitles, intro/outro)
- `src/video-style.json` — single source of truth for all style/audio parameters
- `hyperframes-app/` — the HyperFrames project (`index.html` is generated; `assets/` is populated per run)
- `assets/bgm/<mood>/` — BGM library (mood = upbeat / serious / relaxing)
- `assets/sfx/` — transition/intro sound effects
- `inbox/<project>/` — input: one PDF + one `script.md` per project
- `work/`, `output/` — generated (gitignored)
- `_archive/` — retired files kept for reference (safe to delete later)

## Composition Key Rules (for src/template.html and generated clips)

1. Every timed element needs `data-start`, `data-duration`, and `data-track-index`
2. Elements with timing **MUST** have `class="clip"` — the framework uses this for visibility control
3. Timelines must be paused and registered on `window.__timelines`
4. The framework forces `opacity: 1` on active clips — animate an **inner wrapper**, never the clip itself
5. Only deterministic logic — no `Date.now()`, no `Math.random()`; use slide-index-derived variation
6. Prefer `tl.fromTo()` with explicit from-states (seek-safe); never bare `tl.from()`
7. Audio uses `<audio class="clip">` elements; BGM/SFX are mixed later in the FFmpeg stage

## Linting — ALWAYS RUN AFTER CHANGES

After changing `src/template.html` or generator logic, regenerate a composition
(run the pipeline or a test project) and run `npm run check`. Fix all errors before
considering the task complete.

## Script format (inbox/<project>/script.md)

```markdown
# Title: 動画タイトル（イントロカードに使用。省略時はフォルダ名）

# Slide 1
ナレーション本文。**強調したい語**は太字記法で書くとテロップで黄色ハイライトされる。

# Slide 2
...
```

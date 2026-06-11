---
name: film-production
description: Operate the film production pipeline — script to storyboard keyframes to per-shot video to animatic/EDL for the user's NLE. Use when the user wants to start/continue a film, short, or video production, work with shots/scenes/storyboards/animatics, or asks about production status.
---

# Film production pipeline (operator's manual)

Full design: `film/PIPELINE.md`. Read it before the first production
session. This skill is the condensed operating procedure.

## Where things live

- Production: `output/claude/productions/<name>/` — `production.json` is
  the single source of truth (shots, style bible, seed bank, takes with
  prompt/seed/engine/cost/QC/approved).
- Tools (run with the bridge venv's python): `film/manifest.py` (scaffold
  via `manifest.scaffold(name)`), `build_animatic.py <name>`,
  `export_edl.py <name>`, `contact_sheet.py <name>`, `make_dailies.py <name>`.
- Templates: `flux_text_to_image` (stills), `kling_animate_image`
  (cloud i2v); capture the user's proven local video graph with
  `history_to_template` when first needed.

## Hard rules (money + trust)

1. **Pacing lock before money**: no cloud render unless the shot is on an
   approved render order with a cost estimate (Kling std 5s ≈ EUR 1).
2. **Prompts are assembled, never freehanded**: style-bible block + shot
   action + camera line. Drift appears around shot 12 otherwise.
3. **Every generated file becomes a take in the manifest immediately**
   (path, engine, model, seed, prompt, cost, prompt_id). Never overwrite a
   take; increment `_t##`.
4. **Conform cloud video output to project fps** (ffmpeg) before edit use.
5. Local renders only when `comfy_status` shows VRAM headroom (the user's
   other GPU apps come first). Never start/stop ComfyUI; never `free_vram`
   unasked.
6. Durations snap to 5s or 10s (Kling's clip lengths); trim in the edit.
7. Max 2 cloud takes per shot, then diagnose the keyframe/brief instead.
8. Upscale/interpolate/lip-sync **only** circled takes in a locked cut;
   lip sync bakes onto one take — picture lock first.

## Stage order (gate after each)

script lock → shot breakdown + cost plan → look dev (local drafts → cloud
heroes → style bible frozen) → keyframes per shot (approved before
animation) → animatic + pacing lock (build_animatic.py; the user retimes in
their NLE) → shot production (local lane free/overnight; cloud Kling lane
per signed order) → dailies (vision QC: extract first/mid/last frames,
compare vs approved key + style heroes, verdict in the take record; rebuild
animatic so the user watches the film) → finishing on circled takes →
export_edl.py + conformed takes → the NLE.

## Vision QC (your unique job)

After every batch: ffmpeg-extract first/mid/last frames of each take, READ
them, check (a) frame 1 matches the approved keyframe, (b) style vs the
lookdev heroes, (c) artifacts (hands, morphing, text). Write verdict +
reason into the take record. Local hard-fails: retry with a mutated prompt
clause + new seed, max 2, free. Cloud fails: report with suggested fix,
never auto-resubmit. You cannot see temporal judder from 3 frames — flag
"needs human motion check" rather than passing it.

## Engine routing

Camera moves your local video model supports → local lane (free, overnight).
Anything else — hero shots, two-keyframe shots, audio shots, lip sync —
cloud Kling nodes (paid, explicit approval). Stills: drafts on the local
image model, heroes on cloud flux-dev. Seeds never transfer across engines.

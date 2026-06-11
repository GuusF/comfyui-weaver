> **Note for adopters:** this document was written against the author's
> reference setup (RTX 2080 Ti local + LTX-2 video + Comfy Cloud with Kling
> partner nodes + DaVinci Resolve as NLE). Map the engine routing table to
> your own local models and editor - the gates and conventions are the
> portable part.

# Film production pipeline — Claude × ComfyUI (local + cloud)

A single-creator pipeline with studio habits, built on what's verified
working on this machine: Flux stills (local GGUF / cloud full-precision),
Kling video nodes (cloud, incl. start-end frame, camera control, audio
generation, lip sync, Omni reference images), LTX-2 19B local video with
camera LoRAs, SeedVR2/interpolation finishing, Joy Caption, BiRefNet —
orchestrated by Claude Code, edited in DaVinci Resolve.

**The one rule that pays for everything: lock pacing before money.**
Cuts are decided on free stills (animatic); cloud credits only buy motion
for shots that survived the edit.

## The spine: production.json

Every production lives in `output\claude\productions\<name>\` with a
manifest (`production.json`) as the single source of truth — shots, style
bible, seed bank, takes with full provenance (engine, model, seed, prompt,
cost, QC verdict, approved flag). Tools (`claude-integration\film\`):

| Tool | Does |
|---|---|
| `manifest.py` | scaffold/load/save productions, best-take resolution |
| `build_animatic.py` | always-watchable cut: video takes > held boards > black slugs, slate burn-ins |
| `export_edl.py` | CMX3600 EDL of circled takes for Resolve |
| `contact_sheet.py` | one-glance grid of every shot's current state |

**Takes vs versions:** generated media are *takes* (`_t01`, `_t02` — dice
rolls), documents and cuts are *versions* (`_v001` — intentional). Never
overwrite a take.

## Stages and gates

**1. Development** — Claude co-writes treatment → screenplay (markdown/
fountain in `script/`). Free text iteration. *Gate: script lock — scene
numbers freeze forever (inserts become sc015a).*

**2. Breakdown** — Claude parses the locked script into manifest shots:
id (`sc010_sh010`), action, dialogue, duration (snap to Kling's 5s/10s!),
camera (controlled vocabulary: `static` / `dolly-left` route to free local
LTX-2 LoRAs; everything else routes to paid Kling camera control), engine
plan, cost forecast. *Gate: shot list + EUR estimate approved.*

**3. Look development** — explore looks on **local Flux Schnell (free)**,
promote winners to **cloud flux1-dev (cents)** for hero reference frames.
Winning prompts/seeds freeze into the style bible + seed bank; hero frames
become Kling Omni `reference_images` for character consistency. Joy Caption
extracts the locked vocabulary. *Gate: style bible frozen — every later
prompt is assembled from it, never freehanded (drift kills you at shot 12).*

**4. Storyboard keyframes** — one approved start frame per shot (start+end
for `KlingStartEndFrameNode` shots — generate both from the same prompt
block with only the framing line changed, or you get morph-goo). Drafts
local, heroes cloud. Contact sheet review; redo by shot code. *Gate: no
shot animates without an approved key — composition is cheapest to fix here.*

**5. Animatic / pacing lock** — `build_animatic.py` from approved boards,
plus temp audio (phone VO / temp music) in Resolve. Retime, kill shots,
re-order — all free. *Gate: durations locked + a signed render order: the
explicit list of which shots go to cloud, with EUR total. No cloud job runs
that isn't on it.*

**6. Shot production** — per the render order:
- **Local lane (free, overnight)**: LTX-2 + camera LoRA for static/dolly
  shots, queued `wait=false` when `comfy_status` shows VRAM headroom
  (Resolve owns the card by day).
- **Cloud lane (paid, parallel)**: `KlingImage2VideoNode` from the approved
  key; `KlingStartEndFrameNode` for two-key shots;
  `KlingCameraControlI2VNode` for moves; `KlingImageToVideoWithAudio` for
  shots wanting diegetic sound (free sound design!); Omni
  `reference_images` where the character might drift.
- Every result ingests immediately as a take (file + manifest record with
  prompt_id/seed/cost) — an output not in the manifest is a bug.
- **Conform every take to project fps with ffmpeg before it touches the
  edit** — Kling returns its own cadence and silently lies to your timings.

**7. Dailies & assembly** — after each batch Claude runs **vision QC**
(extract first/mid/last frames; compare against the approved key + style
heroes; verdict: pass / retake-with-reason), builds contact sheets and an
updated animatic with finished shots swapped in — you always watch the
*film*, not clips. Local hard-fails retry automatically with prompt
mutation (max 2); cloud fails are reported, never auto-resubmitted.
*Gate: circle takes. Max 2 cloud takes per shot before a "why is this
failing" conversation — it's usually the keyframe, not the seed.*

**8. Finishing & delivery** — only on circled takes in a locked cut:
SeedVR2 upscale + frame interpolation (local, overnight), BiRefNet mattes
for comp shots, **lip sync last** (`KlingLipSyncAudioToVideoNode` with your
properly-recorded dialogue — it bakes onto one specific take, so picture
lock first; TTS variant is scratch only). Small continuity flaws on
otherwise-perfect takes: Kling Omni video-to-video *pickup* instead of a
fresh €1 re-roll. `export_edl.py` + conformed takes → Resolve for the real
edit, grade, mix. Final manifest = the film's "negative": any shot
re-renderable in six months.

## Engine routing cheat-sheet

| Need | Engine | Cost |
|---|---|---|
| Look dev drafts, board drafts | local Flux Schnell GGUF | free |
| Hero stills, reference frames | cloud flux1-dev-fp8 | cents |
| Static / dolly-left shots | local LTX-2 + camera LoRA | free (overnight) |
| Other camera moves, hero motion | cloud Kling i2v / camera control | ~€1 per 5s std |
| Two-keyframe shots | cloud KlingStartEndFrameNode | ~€1 |
| Diegetic audio shots | cloud KlingImageToVideoWithAudio | ~€1+ |
| Dialogue performance | cloud KlingLipSync (audio) — finishing only | per shot |
| Upscale / retime / mattes | local SeedVR2 / interpolation / BiRefNet | free (overnight) |

## Enhancement roadmap

**Working today**: vision-QC dailies; Joy Caption searchable shot log
(`grep` your footage by content); slated animatics; contact sheets;
Omni pickup rescue; Kling audio + lip sync.
**Small builds**: camera-language compiler (manifest camera field →
correct engine call automatically); overnight shot factory (drain the
local backlog when the GPU frees up — never launching ComfyUI itself);
parallel cloud shot farm (N shots in one clip's wall-clock via subagents);
pre-flight cost meter with session budget cap.
**Ambitious**: Flow Production Tracking sync (you have it installed —
statuses + dailies proxies pushed via its Python API; the stepping stone
is a `fpt_status.csv` matching FPT's shot-import schema).

## Pitfalls the panel flagged (keep this list honest)

- Rendering before pacing lock is the budget killer (30 shots × 2 takes × €1…).
- Seeds don't transfer across engines — promotion means re-render + re-approve.
- Claude's QC reads frames, not motion: temporal judder needs your eyes. The
  human finals check is not optional.
- The AI-video tell is every shot being a slow push-in. Statics cut cleaner,
  cost nothing locally, and make moves matter when you spend them.
- Never upscale/interpolate un-circled takes; 80% die in the edit.
- 11 GB minus Resolve minus Photoshop ≈ no local renders during the workday.

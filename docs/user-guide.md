# PrevizRender — User Guide

Map video clips onto a 3D scene's materials and render them headlessly — via
**Blender**, **Cinema 4D + Redshift**, or the built-in **three.js** web
renderer — locally or on a Thinkbox Deadline farm.

![Main window](../assets/help/img/main-window.png)

This guide is also built into the app: **Help → User Guide** (with the same
screenshots and clickable links into the relevant settings).

---

## Table of contents

1. [Quick start](#quick-start)
2. [Watch & Auto-render](#watch--auto-render)
   - [① Source](#-source--what-to-watch)
   - [② Mode](#-mode--what-happens-to-a-clip)
   - [③ Naming](#-naming--teach-it-your-convention)
   - [④ Output & delivery](#-output--delivery)
   - [⑤ Activity](#-activity--trust-but-verify)
3. [How-to recipes](#how-to-recipes)
4. [Filename pattern reference](#filename-pattern-reference)
5. [The queue](#the-queue)
6. [Render engines](#render-engines)
7. [Crash reports](#crash-reports)
8. [Troubleshooting & FAQ](#troubleshooting--faq)

> The same guidance is built into the app under **Help → User Guide** — the
> **How-To Guides** tab has these recipes with screenshots and clickable links
> into the relevant settings.

---

## Quick start

1. **Add a scene** — drag a `.blend`, `.c4d`, `.glb`/`.gltf` file onto the
   Scene box and click **Scan Scene**. Its materials appear in the list.
2. **Add clips** — drag videos into the Videos list, pick a **Camera**.
3. **Map** — drag a clip onto a material (or use **Auto-match**, which pairs
   clips to materials by name).
4. **Render** — check the output settings in the Render panel and press
   **Render**. Progress, ETA and logs are live; finished renders can open,
   reveal, or copy themselves to a delivery folder automatically.
5. **Automate it** — open **View → Watch & Auto-render** and let the app do
   steps 2–4 by itself whenever a clip lands in a folder. That's the rest of
   this guide.

---

## How-to recipes

Task-oriented walkthroughs for the things you'll actually do. The [Watch &
Auto-render](#watch--auto-render) deep dive below covers the panel field by
field; these are the "just tell me the steps" versions.

### A · Map clips onto a scene and render

1. Drag your 3D file (`.blend`, `.c4d`, `.glb`) onto the **Scene** box → **Scan Scene**.
2. Drag your videos into the **Videos** list, choose a **Camera**.
3. **Auto-match** to pair clips to materials by name, or drag a clip onto a material.
4. Set resolution / range / format / quality in the **Render** panel.
5. **Render** (`⌘R`). Watch the Live Preview; the output can open/reveal/copy itself when done.

### B · Set up a watch folder (auto-render drops)

1. Load the scene + clips once, then **right-click a material → Mark as Render Target** for each screen that matters.
2. **View → Watch & Auto-render → ① Source → Choose…** the folder.
3. **② Mode** = *Auto-map onto the current scene*.
4. **④ Output** → turn on **Start renders automatically** (off = jobs just queue). Set an output folder or leave blank for a `PREVIZ` subfolder.
5. Press **Start**. Drops import, map and render; a newer `_v2` supersedes `_v1` automatically.

### C · Auto-assemble previz from a filename convention

1. **② Mode** = *Previz assembly — one render per asset*. The **③ Naming** card appears.
2. Build the **pattern** from chips (no regex): `{Field}` text, `{Field#}` number, add `?` for optional. Hyphens are fine.
3. Paste a real filename into the **sample** box — the live preview shows what parses (or where it stopped).
4. Fill **Screen → Material** and, for multiple scenes, **Setup # → Scene** (recipe D).
5. **Preview (dry run)** to confirm what would assemble + what's skipped, then **Start**.

Example: `{ID#}_D{Day#}_{Section}_{Cue}_{Screen}_v{Version#}` parses
`80230_D2_War-Treaty_MusicH_TC-MASTER_v001.mp4` into its fields automatically.

### D · Route setups to different scene files

1. Include a `{Setup#}` (or `{Day#}`) field in the pattern.
2. In **Setup # → Scene**, **+ Add** each setup number → its scene (`1 → D1.c4d`, `2 → D2.c4d`).
3. Unmapped setups use the loaded scene. **Preview (dry run)** confirms each asset's scene.

### E · Batch-render one mapping across many clips

1. Set up the scene + mapping once (recipe A).
2. Add every clip you want rendered with it to the **Videos** list.
3. **Queue current mapping** → one job per clip, each with its own output name.
4. **Render All**. Jobs run in order and report metrics when done.

### F · Deliver finished renders automatically

1. Find **④ Output → Delivery** (or the render settings).
2. Set a **Copy to** folder (a synced review/hand-off folder).
3. Every finished render is also copied there. Combine with B for a fully hands-off pipeline.

### G · Render on a Deadline farm

1. **Properties → Deadline** → point at your repository (and `deadlinecommand` if not auto-found) → **Test**.
2. Build jobs in the Queue as usual (Blender *and* C4D both submit).
3. Choose **Submit to Deadline** instead of a local render — frames spread across your nodes.

### H · Save a look as a preset

1. Dial in resolution, format, quality, tone-mapping, etc.
2. **Save Preset…** and name it.
3. **Load Preset…** later (or apply to queued jobs) to reproduce it in one click.

### I · Fix "my clips aren't being picked up"

| Check | Fix |
|---|---|
| **Skipped badge** in ⑤ Activity | Click **"N clip(s) skipped — why?"** — the dry run names every file and reason. |
| Clip in a subfolder | Turn on **Include subfolders** in ① Source. |
| Cloud placeholder | Right-click → *Always keep on this device* (online-only files are skipped until downloaded). |
| Still copying | Raise the **Wait** window for slow copies of huge files. |
| Pattern mismatch | Use the live preview in ③ Naming — it pinpoints the first divergence. |

---

## Watch & Auto-render

The Watch panel is a single automation rule that reads top to bottom:
**① Source → ② Mode → ③ Naming → ④ Output → ⑤ Activity.**

<img src="../assets/help/img/watch-panel.png" width="440" alt="Watch & Auto-render panel">

### ① Source — what to watch

<img src="../assets/help/img/watch-source.png" width="430" alt="Source card">

Click **Choose…**, pick the folder, press **Start**.

| Setting | What it does |
|---|---|
| **Wait** | How long a file must stop changing before it's imported — so a clip that's still copying is never grabbed half-written. Raise it for very large files arriving over slow links. |
| **Re-scan every** | The backstop poll. Local drops are caught instantly by filesystem events; the poll covers network shares (SMB/NFS) and cloud folders where events don't fire. It backs off automatically when the folder is quiet. |
| **Include subfolders** | Watch the whole tree — clips dropped into per-day/per-setup subfolders are picked up too. The render output folder is always excluded, so renders never re-trigger themselves. |

**Cloud-aware:** Dropbox/OneDrive *online-only* placeholder files are detected
and skipped until their bytes are actually on disk.

### ② Mode — what happens to a clip

| Mode | Behaviour |
|---|---|
| **Auto-map** | Each clip maps onto the *current scene's* material with the matching name. Once every render-target screen has a clip, one multi-screen render is queued (a burst of arrivals coalesces into a single render). `Screen_v2.mp4` landing next to `v1` takes over the mapping — **latest wins**. |
| **Assemble previz** | For shows with a filename convention: clips group into **one render per asset**, each routed to its setup's scene file. Drop 10 clips → get 5 assembled previz jobs. |

### ③ Naming — teach it your convention

*(Previz mode only — the card hides in auto-map mode.)*

<img src="../assets/help/img/watch-naming.png" width="430" alt="Naming card with chip builder and live preview">

Build the filename pattern from **chips** — no regex. Paste a real filename
into the sample box and the live preview shows exactly which fields parse:

```
Pattern:  {ID#}_D{Day#}_{Section}_{Cue}_{Screen}_v{Version#}
Sample:   80230_D2_War-Treaty_MusicH_TC-MASTER_v001.mp4
Result:   ✓ matches   ID=80230 · Day=2 · Section=War-Treaty · Cue=MusicH ·
          Screen=TC-MASTER · Version=1
```

If it doesn't match, the preview says *where* it stopped and what it expected
(`Stopped after "80230_D" — expected a number for 'Day'`) — so fixing the
pattern never requires understanding regular expressions.

| Table | What it does |
|---|---|
| **Screen → Material** | Which scene material each screen code drives. Leave a screen out and it defaults to the material with the same name. |
| **Setup # → Scene** | Routes each setup number to its own scene file (`D1.c4d`, `D2.c4d`, …). Setups without a mapping use the currently loaded scene. |

**Versioning:** newest wins. A `v002` landing after `v001` *updates the
existing job in place* — jobs never pile up per version.

**Preview (dry run)** shows exactly what *would* assemble — per asset:
screen → material → clip → version — plus every skipped clip and the reason,
before anything renders:

<img src="../assets/help/img/preview-assembly.png" width="440" alt="Preview assembly dry run">

### ④ Output & delivery

<img src="../assets/help/img/watch-output.png" width="430" alt="Output card">

- **Output name** supports tokens (e.g. `{prj}_D{day}_S{setup}_A{asset}_PREVIZ_V{ver}`).
- **Output folder** blank = a `PREVIZ` subfolder inside the watch folder
  (always excluded from scanning).
- **Start renders automatically** on = fully hands-off; off = jobs queue and
  wait for you.
- **Delivery folder** — every finished render is also copied there (a synced
  review folder, a hand-off share…). Blank = off.

### ⑤ Activity — trust, but verify

<img src="../assets/help/img/watch-activity.png" width="430" alt="Activity card with skipped badge">

- **Skipped badge** — clips that did **not** become jobs (name doesn't match,
  screen not mapped, superseded by a newer version) are *counted, not
  swallowed*: click **"N clip(s) skipped — why?"** and the dry run explains
  every file.
- **Ingest history** — the feed writes through to
  `~/.blender_video_mapper/logs/ingest_history.log` and reloads on launch, so
  *"did that clip get picked up last night?"* always has an answer. **Clear**
  empties the visible list only; the on-disk history stays.

> **The full pipeline:** watch folder on the production share + previz mode +
> auto-start + delivery folder = clip lands → parsed → assembled → rendered →
> copy appears in the review folder. Zero clicks.

---

## Filename pattern reference

| Token | Meaning | Example match |
|---|---|---|
| `{Name}` | Text field (letters, digits, hyphens) | `TC-MASTER`, `War-Treaty` |
| `{Name#}` | Number field (leading zeros fine, parsed as int) | `001` → 1 |
| `{Name?}` / `{Name#?}` | Optional field — may be absent | — |
| anything else | Literal text, matched case-insensitively | `_`, `D`, `v` |

- The file extension is ignored automatically (`.mp4`, `.mov`, …).
- Each field name can be used once per pattern.
- Special field names the previz assembler understands: **Screen** (→
  material), **Setup** (→ scene routing), **Version** (→ newest wins),
  **Type** (→ "Only ingest type" filter), **Asset**/**Project**/**Day**
  (grouping + output-name tokens). Missing Setup/Asset fields simply group
  everything into one asset per (project, day) — fine for simpler shows.

**Real-world example** (a touring show's `_PREVIEW-MOTION` folder):

```
{ID#}_D{Day#}_{Section}_{Cue}_{Screen}_v{Version#}

80230_D1_Day-1-Pre_Matte-TransA_DJBS_v001.mp4
80230_D2_War-Treaty_MusicH_TC-MASTER_v001.mp4
80230_D2_War-Treaty_MusicH_HOUSE-MASTER_v002.mp4
```

---

## The queue

<img src="../assets/help/img/queue.png" width="430" alt="Queue panel">

Every render is a job: reorder, duplicate, rename, set priority, requeue.
Labels show the **resolved output filename**. Batch-queue the current mapping
across many clips, or let the watch folder feed the queue for you. Finished
jobs report duration, per-frame metrics and estimated power cost, and can
notify you (desktop notification or Discord webhook) when done.

## Render engines

| Engine | Scene files | Notes |
|---|---|---|
| **Blender** (Cycles/Eevee) | `.blend` | The app can download and manage its own Blender — nothing to install. |
| **Cinema 4D + Redshift** | `.c4d` | Uses your installed C4D (`c4dpy`); Redshift settings honoured. |
| **Web (three.js)** | `.glb` / `.gltf` | Fully built-in headless renderer — no external DCC needed. |
| **Deadline farm** | both | Submit Blender and C4D jobs to a Thinkbox Deadline repository. |

## Crash reports

If the app ever crashes — even a hard native crash — the next launch offers
the report: view it, or open a prefilled GitHub issue. **Nothing is sent
anywhere unless you submit it yourself.** Reports live in
`~/.blender_video_mapper/crashes/`.

## Troubleshooting & FAQ

**A clip sits in the watch folder and nothing happens.**
Look at the Activity card — if the skipped badge shows, click it: the dry run
names every skipped file and the reason (pattern mismatch, unmapped screen,
superseded version). Also check: is the clip inside a subfolder while
*Include subfolders* is off? Is it a cloud placeholder that hasn't downloaded?

**Renders re-trigger themselves.**
They can't from the default output location — the output folder is excluded
from scanning. If you point the output *outside* the watch folder and then
back in via symlinks, don't.

**Half-copied files get imported.**
Raise the **Wait** (settle) time in ① Source — a file must hold its size for
that window before it's touched.

**The pattern won't match hyphenated codes.**
It does — text fields accept hyphens (`TC-MASTER`, `Day-1-Pre`). Use the live
preview with a real filename; it pinpoints the first divergence.

**Where are my settings stored?**
`~/.blender_video_mapper/` — profile, presets, logs, ingest history and crash
reports. Delete the folder for a factory reset.

**Is the download safe?**
Releases ship a `SHA256SUMS.txt`, signed build provenance
(`gh attestation verify <file> --repo hayhamLT/PrevizRender`), and macOS
builds are Developer-ID signed + notarized by Apple.

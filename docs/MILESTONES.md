# Development milestones

Each milestone ends in something runnable and tested, and the riskiest work comes first. The synchronisation
engine decides whether the product is trustworthy at all, so it was built and validated before any UI.

Estimates assume one senior engineer; UI milestones parallelise well with a second engineer.

| Milestone | Scope | Estimate | Status |
|---|---|---|---|
| M0 | Architecture, specification, plan | 1 week | ✅ done |
| M1 | Synchronisation engine + synthetic test suite | 2–3 weeks | ✅ done |
| M2 | Media layer: probe, extract, cache, devices | 2–3 weeks | ✅ done |
| M3 | Project persistence, engine service (JSON-RPC), CLI, parallelism | 2–3 weeks | ✅ done |
| M4 | Desktop shell and timeline UI | 4–6 weeks | ✅ done |
| M5 | XML export and NLE validation | 2–3 weeks | next |
| M6 | Hardening, packaging, beta | 3–4 weeks | |

## M0: Architecture and specifications ✅

**Deliverables:** [ARCHITECTURE.md](ARCHITECTURE.md), [TECHNICAL_SPEC.md](TECHNICAL_SPEC.md),
[SYNC_ENGINE.md](SYNC_ENGINE.md), this plan, repository layout, CI.

## M1: Synchronisation engine ✅

**Deliverables (in `engine/`):**

* `mcsync.sync`:
  * analysis-signal preparation;
  * coarse envelope search;
  * windowed GCC-PHAT verification with drift estimation;
  * confidence model and flags;
  * drift-aware global solver with clock domains, manual constraints and outlier rejection;
  * orchestration (pair pruning, clock-prior search windows, analyze/solve split, snap-to-audio, progress,
    cancellation).
* `mcsync.timecode`: rational frame rates, drop-frame, 24 h wrap, BWF.
* `mcsync.testing.synthetic`: acoustic scenes (speech, live music, exact loops, ambience) and device recordings with
  known offsets, clock drift, EQ, reverb, noise, gain and sample rate.
* 148 tests: unit, synthetic property tests, end-to-end multicam scenarios, and hour-long recordings (`-m slow`).
* `scripts/benchmark_sync.py`.

**Exit criteria (met):**

* Sub-millisecond accuracy across randomised recording conditions.
* No confident matches between unrelated recordings.
* Drift measured to within 1 ppm.
* A 3 h × 20 min match completes in under 2 s.
* Manual re-solve completes in under 50 ms.

## M2: Media layer ✅

**Deliverables (in `engine/src/mcsync/media/`):**

* `tools.py`: locates FFmpeg (bundled directory via `MCSYNC_FFMPEG_DIR`, next to a frozen engine, or `PATH`); no
  console windows on Windows.
* `probe.py`: ffprobe → typed metadata:
  * exact rational frame rates and VFR detection;
  * per-stream start times → clip origin and `audio_start_s`;
  * timecode from tmcd tracks, stream tags and container tags (MXF), plus BWF time references;
  * creation time, including Apple's local-offset form, with unset 1904/1970 clocks rejected;
  * make/model/serial from QuickTime tags, GoPro/DJI handlers, MXF identification, BWF originator and Sony XML
    sidecars.
* `riff.py`: bext and iXML read directly from WAV/RF64 chunks (timecode rate, drop-frame flag, project, scene, take,
  track names).
* `extract.py`: streaming FFmpeg decode to the 8 kHz analysis signal, with a chunked band-pass carrying filter state
  and in-place normalisation. Memory use is flat. Cancellable, atomic (temp directory + rename), channel selection,
  explicit channel averaging.
* `cache.py`, `fingerprint.py`: content fingerprints (size + first/last MiB), versioned cache layout, memory-mapped
  loading, LRU eviction that never touches entries in use.
* `waveform.py`: μ-law min/max peak pyramids (6 zoom levels), built during extraction.
* `devices.py`:
  * device identity (serial → make/model + card → file-name family → folder);
  * GoPro chapter and 4 GB split detection;
  * **rec-run timecode detection**: timecode that only advances while recording is not used as a clock.
* `library.py`: scan → extract → `ClipInput`s carrying every clock: timecode per device or per jam-synced rate
  family, creation time per device, chapter position per take.
* **Engine additions:**
  * several clock readings per clip;
  * chapter clocks (placement method `chapter`);
  * per-domain normalisation of epoch-sized readings, keeping solver precision.
* `mcsync.testing.media`: generates real media with FFmpeg: a BWF recorder, a 23.976 MOV with timecode (interrupted),
  a 29.97 DF MP4 with delayed audio, an AVCHD MTS, GoPro chapters (one muted), and a drone without audio.

**Exit criteria (met):**

* On FFmpeg-generated files, probe → extract → sync places audio-matched clips within 0.011 ms of ground truth.
  The muted GoPro chapter is placed exactly by chapter continuity, and the drone within one frame by timecode.
* An MP4 whose audio is delayed through an edit list is placed exactly where FFmpeg decodes its audio. FFmpeg 6/7 and
  8 disagree about such edit lists (found in CI), so the app must bundle one pinned FFmpeg build (M6), and the NLE
  validation matrix includes such a file.
* Extraction is about 400× real time; the 16-minute test shoot extracts in 2.5 s. FFmpeg's resampling and the
  in-memory path agree to 0.0 µs.
* Metadata is covered by generated media or by captured ffprobe JSON in the unit tests. The exceptions are real
  vendor files: MXF from Sony/Canon bodies, iPhone VFR and Sony XAVC sidecars are exercised through parsed samples,
  not real files. Collecting real files is part of the M6 beta.

**Not done, deliberately:** resuming a half-extracted file (an interrupted extraction restarts that file; files take
seconds); XAVC LTC change tables in sidecars (tmcd tracks cover those cameras).

## M3: Project persistence and engine service ✅

**Deliverables:**

* `project/`: SQLite project file (schema v1, WAL). Devices, media with full metadata, clips, sync runs, pair matches
  under content-derived pair keys, an append-only correction log with undo/redo, and placements. Media status
  (online/offline/changed) is refreshed on open.
* `timeline.py`: groups, one track per device (cameras first, recorders last), overflow lanes for overlapping clips of
  one device, and a review queue ordered by severity (conflict, device overlap, detached, uncertain, metadata only,
  unsynced, offline).
* `service/`: JSON-RPC 2.0 over stdio. Every method in ARCHITECTURE §6 except `export.xml` (M5). Background jobs send
  throttled progress and support cancellation. stdout is protected from stray prints. Errors map to JSON-RPC codes.
* **Parallel matching:** `SyncEngine.match_pairs` with a warm process pool (`create_match_pool`); memory-mapped
  signals travel as file paths.
* **Incremental and resumable synchronisation** through pair keys.
* `cli.py`: `mcsync serve | probe | sync`, sharing the service code.

**Exit criteria:**

* **Met.** Kill the engine mid-run, restart, and the run resumes without redoing finished pairs. Tested with a real
  child process and `kill -9`.
* **Met.** The RPC contract is exercised in-process and over real pipes: protocol errors, busy/cancel, the full
  import → sync → correct → undo → snap → re-sync workflow, and shutdown.
* **Changed.** "100 clips in ≤ 3 minutes on 8 cores" is not measured yet. Measured instead: 3.4× on 4 cores with a
  warm pool (99 pairs around a 1 h recorder in 1.8 s instead of 6.2 s). A re-run with nothing changed finishes in about
  1 s.
* **Not done.** JSON schemas for generating the TypeScript types. The TypeScript types will be written against the
  implemented payloads in M4 and checked by contract tests.

## M4: Desktop shell and timeline UI ✅

**Deliverables (in `app/`):**

* **Electron main** (`electron/`):
  * the engine supervisor: spawn, `engine.hello` handshake with a protocol check, request timeouts, automatic
    restart after a crash (up to three times), clean shutdown;
  * a hardened renderer: context isolation, sandbox, CSP, no navigation;
  * the preload bridge, with engine methods allow-listed;
  * native menus with shortcuts, and native dialogs;
  * waveform reads confined to the cache directory.
* **React renderer** (`src/`):
  * **Welcome:** new/open project, recent projects, engine and FFmpeg problems explained.
  * **Media bin:** import by dialog or drag and drop; clips grouped by device; rename a device or change its type;
    per-clip audio stream and channel choice; badges for chapters, VFR and offline media.
  * **Toolbar:** sync mode, jam-synced timecode, reference clip, Synchronise, job progress with cancel, undo/redo.
  * **Timeline:**
    * one track per device, plus overflow lanes for overlapping clips;
    * one canvas for all clips and waveforms, drawn from the engine's peak pyramids;
    * zoom from a whole day down to single samples (Ctrl/⌘ + wheel, buttons, menu) and pan (wheel or drag),
      always keeping some footage in view;
    * status colours: synced, needs review, manual;
    * group tabs for clips not linked to the reference, and a strip of clips that could not be placed;
    * an edit cursor;
    * drag a clip to place it by hand.
  * **Review queue** (most severe first) and **inspector**:
    * facts, flags and drift explained in plain language;
    * nudge by 1/10 frames or 1 ms (also ←/→, Shift, Alt);
    * snap to audio within ±2 s, with the other positions that fit;
    * confirm a position, place at the cursor, reset to automatic, exclude/include, use as reference;
    * every audio match of the clip with Use / Reject / Restore.
* **Tests:**
  * Vitest units (`tests/`);
  * Playwright-for-Electron end to end with the real engine (`e2e/app.spec.ts`);
  * a 300-clip timeline benchmark with a stand-in engine (`e2e/timeline-perf.spec.ts`, Linux and macOS);
  * CI on Linux, macOS and Windows (`.github/workflows/app-ci.yml`).
* **Engine:** `sync.matches` (a clip's matches from its own point of view, including rejected ones).

**Exit criteria:**

* **Not yet met.** "A non-technical tester completes the workflow on a real wedding card dump without help" needs
  real footage and a tester. What is covered: the end-to-end test runs the whole workflow on a generated 7-minute,
  8-file shoot (MOV/MP4/MTS/BWF, chapters, a drone without audio) and checks every clip against the true position
  (within 40 ms; audio clips well under 1 ms).
* **Mostly met: panning yes, fast zooming not quite.** 60 fps with 300 clips was measured with
  `e2e/timeline-perf.spec.ts`: a 3-hour project with a recorder and 300 camera clips on 12 devices. The container
  has no GPU, so Chromium renders in software:
  * panning, whole day or zoomed in: 59–60 fps, p95 frame interval 16.8 ms;
  * fast zooming over the whole day (90× in half a second, loading new waveform levels on the way): 53–55 fps,
    median 16.7 ms, p95 33 ms.

  Real hardware has not been measured yet.

**Changes from the plan:**

* The first version drew each clip as an element with its own waveform canvas. That managed only 28 fps panning and
  12 fps zooming at 300 clips, so it was replaced by the planned single Canvas 2D surface:
  * hit-testing uses `geometry.ts`;
  * a visually hidden list of clip buttons keeps clips reachable by keyboard and screen readers;
  * components subscribe only to the store fields they use, so the media bin does not re-render on every pan frame.
* Not done yet:
  * the match-quality plot and overlaid waveforms in the review inspector (it lists the matches and alternatives
    instead);
  * nudging by single samples (the smallest nudge is 1 ms);
  * an engine health ping (crashes are detected when the process exits).

**Risks:** timeline rendering performance. Mitigation: one canvas, culling, waveform levels chosen by zoom, no
per-frame allocation or style reads, and a benchmark in CI to catch regressions.

## M5: XML export and NLE validation

**Scope:**

* `export/timeline.py`: sequence model, rate selection, exact rational frame conversion, rounding report.
* `export/xmeml.py` (Premiere Pro + Resolve), `export/fcpxml.py` (Resolve, sample-accurate audio).
* Golden-file tests.
* The manual NLE validation matrix (Resolve 19/20, Premiere 2025/2026), with mixed rates and drop-frame.

**Exit criteria:**

* Every cell of the validation matrix imports with all clips at the expected positions (±½ frame for xmeml, exact
  for FCPXML), media linked, and audio channels mapped.

**Risks:** undocumented NLE import behaviour for mixed frame rates. Mitigation: golden files derived from what each NLE
itself exports, and version-specific tests.

## M6: Hardening, packaging, beta

**Scope:**

* PyInstaller engine build with bundled LGPL FFmpeg; signed and notarised installers (macOS DMG, Windows NSIS).
* Crash handling, logging, performance tuning on real multi-hour projects.
* Optional drift retiming in export.
* A closed beta with 10–20 filmmakers, and a regression corpus built from their (consented) problem files.

**Exit criteria:**

* 95 % of clips in beta projects sync without manual intervention.
* No confidently wrong placement reported.
* Clean installs on macOS 13+ and Windows 10/11.

## After v1

* Drift retiming (speed change) in export; AAF export; OpenTimelineIO export.
* Direct creation of multicam clips (Premiere multicam source sequences, Resolve multicam clips).
* Acoustic distance compensation; per-talker delay tracking.
* Batch mode for multi-day events; watch folders.
* GPU FFTs for very large projects (if profiling shows the need).

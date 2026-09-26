# Development milestones

Each milestone ends in something runnable and tested, and the riskiest work comes first. The synchronisation
engine decides whether the product is trustworthy at all, so it was built and validated before any UI.

Estimates assume one senior engineer; UI milestones parallelise well with a second engineer.

| Milestone | Scope | Estimate | Status |
|---|---|---|---|
| M0 | Architecture, specification, plan | 1 week | ✅ done |
| M1 | Synchronisation engine + synthetic test suite | 2–3 weeks | ✅ done |
| M2 | Media layer: probe, extract, cache, devices | 2–3 weeks | next |
| M3 | Project persistence, engine service (JSON-RPC), CLI, parallelism | 2–3 weeks | |
| M4 | Desktop shell and timeline UI | 4–6 weeks | |
| M5 | XML export and NLE validation | 2–3 weeks | |
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

## M2: Media layer

**Scope:**

* `media/probe.py`: ffprobe → typed metadata. Covers rational rates, VFR detection, per-stream start times, timecode
  from every source (tmcd, MXF, format and stream tags), BWF time reference, creation time, make/model/serial.
* `media/extract.py`: streamed FFmpeg extraction to 8 kHz float32, cancellable and resumable, with per-channel
  selection.
* `media/cache.py`, `media/fingerprint.py`: cache layout, fingerprints, LRU eviction, memory-mapped signals.
* `media/devices.py`: device identification and chapter detection (GoPro, 4 GB splits).
* `media/waveform.py`: min/max peak pyramids.
* `fixtures/`: small generated media (ffmpeg `lavfi` sine/noise and `testsrc`) covering MP4/MOV/MXF/MTS/WAV/BWF,
  23.976/25/29.97 DF/59.94, tmcd tracks, VFR, edit lists, multichannel, silence.

**Exit criteria:**

* An integration test runs `ffmpeg`-generated multicam files end-to-end through probe → extract → sync and places them
  within 1 ms of ground truth.
* Every metadata field in the spec is covered by a fixture.
* Extraction runs at ≥ 50× real time per core.

**Risks:** vendor-specific timecode and metadata quirks. Mitigation: store raw ffprobe JSON and build a corpus of real
camera files from beta users.

## M3: Project persistence and engine service

**Scope:**

* `project/`: SQLite schema and migrations, repositories, append-only corrections with undo/redo.
* `service/`: JSON-RPC 2.0 over stdio, job queue with progress (throttled) and cancellation, incremental pair
  persistence and resume, `ProcessPoolExecutor` for extraction and matching, cached reference spectra.
* `cli.py`: `mcsync sync <folder> [--mode hybrid] [--export timeline.xml]` for headless use and QA.
* JSON schemas for every RPC method (source for the TypeScript types).

**Exit criteria:**

* Kill the engine mid-run, restart it, and the run resumes without redoing finished pairs.
* A 100-clip synthetic project syncs in ≤ 3 minutes on 8 cores.
* The RPC contract tests pass.

## M4: Desktop shell and timeline UI

**Scope:**

* **Electron main:** engine supervisor (spawn, handshake, health ping, restart), hardened renderer, preload bridge,
  `mcsync-cache://` protocol, native menus and dialogs.
* **React renderer:**
  * **Media bin:** drag-and-drop import, devices, streams and channels, offline media.
  * **Sync panel:** mode, reference, progress, cancel.
  * **Timeline:** Canvas 2D; one track per device; waveform peaks; zoom from a whole day down to single samples;
    status colours.
  * **Review queue:** reasons, match-quality plot, alternatives, overlaid waveforms, snap, nudge by frame or sample,
    reject, exclude.
  * **Undo/redo.**
* Playwright end-to-end: import → sync → review → export on a fixture project.

**Exit criteria:**

* A non-technical tester completes the workflow on a real wedding card dump without help.
* The timeline stays at 60 fps with 300 clips.

**Risks:** timeline rendering performance. Mitigation: canvas plus virtualisation from day one, and peak pyramids
sized to the zoom level.

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

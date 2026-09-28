# Syncora scalability test report (1.1, re-run with 1.3)

**Date:** 27 September 2026
**Engine:** Syncora 1.1.0, commit `dd92d71`. Later commits change only the pipeline's pause guard, the interface and tests.
The editing timings in §8 are from Syncora 1.1.1, which made corrections faster ([CHANGELOG.md](CHANGELOG.md)).
**Hardware:** one 4-core Linux machine, described in §2.

Everything below was measured, with three exceptions. Where a number is an estimate, it says so. Section 13 lists
what has **not** been tested. Every test can be re-run with the commands in §14.

## 0. Syncora 1.3 re-run (28 September 2026)

The same 4,238-file production (§3), the same 4-core machine (§2), run again with the Syncora 1.3 engine (commit
`659a88e`: noise-robust band envelope, peak prominence, clock anchoring, the confidence cap and two-pass matching
of §6 in docs/SYNC_ENGINE.md). Measured with `python -m mcsync.testing.stress` (§14), which also pauses the analysis,
quits in the middle of it, reopens and synchronises again.

| | 1.1 | 1.3 |
|---|---|---|
| Files that should synchronise, placed exactly (within 20 ms) | 4,100 of 4,200 | **4,199 of 4,200** |
| Scratch-microphone cameras at music sessions | 100 kept in their own group | 500 of 500 placed exactly |
| Placed as synchronised but wrong (false matches) | 0 | **0** |
| Placed off and flagged for review | 0 | 1 (20.4 ms; the solver met conflicting matches) |
| Results: synchronised (confirmed / high / other) | 4,002 | 4,153 (4,138 / 10 / 5) |
| Results: review / manual / failed / skipped | 103 / 120 / 5 / 5 | 52 / 20 / 5 / 5 |
| Silent or sound-less clips (20) | manual sync | manual sync (none placed by audio) |
| Footage from another event (5) | review | review |
| Median / 99th percentile / worst placement error | 0.005 / 0.99 / 10.8 ms | 0.006 / 1.06 / 20.4 ms (the flagged clip) |
| Confidence of automatically synchronised clips | — | mean 1.00, median 1.00, lowest 0.88 |
| Candidate pairs verified | 26,384 of 8.9 million | 26,388 |
| Analysis (4,230 clips) | 16 min 32 s | 11 min 25 s (media in the disk cache; not an engine change) |
| Candidate planning | 4 min 35 s | 6 min 13 s |
| Matching | 3 min 1 s | 5 min 20 s |
| Extended search, clock anchoring, placement | 25 s | 1 min 9 s |
| Empty project to finished sync | 24 min 23 s | 24 min 6 s |
| Whole run (with reopen and a second sync) | 26 min 7 s | 26 min 7 s |
| Peak memory (engine and workers) | 3.6 GB | 3.8 GB |
| Processor | 72 % of 4 cores | 75 % of 4 cores; graphics processor not used |

What the first 1.3 run found, and what was changed before this one:

* **Three confident placements 20–34 ms off.** A far camera's windows agreed on a strong room reflection, with no
  clear coarse peak (PSR 4.8) and no prominent fine peak (10.6), and scored 0.70. Agreeing windows alone are now
  capped as uncertain. After the change: none.
* **Matching 4.3× slower than 1.1** (12 min 51 s), because every pair was searched with both coarse features and
  weaker candidates. Pairs are now matched in two passes (the 1.1 search first, the noise-robust one only when that
  is not clear). After the change: 5 min 20 s, 1.8× 1.1's time, for 99 more placements and 100 fewer manual syncs.

Planning took 6 min 13 s in both 1.3 runs against 4 min 35 s in 1.1; its code did not change, and the difference
was not investigated further.

## 1. Summary

| | Result |
|---|---|
| Maximum tested in one project | **4,238 files: 4,035 video, 200 audio, 3 sidecar files.** Imported, analysed, synchronised, reopened and synchronised again in one run, in one project. |
| Recorded audio in that project | 61.4 hours (44.8 h of camera clips, 16.7 h of recorder files), 3.0 GB of media |
| Time, empty project to finished sync | **24 min 23 s** on 4 cores, including a pause and a quit-and-reopen in the middle of the analysis |
| Correct placements | **4,100 of 4,200** files that should synchronise were placed exactly (within 20 ms; median error 0.005 ms, 99th percentile 1.0 ms, worst 10.8 ms). **0 were placed wrongly.** The other 100 were scratch-microphone cameras at music sessions: their audio never gave a confident match, so they are kept in their own group and listed for manual sync, never guessed. |
| Sessions | 20 of 20 recognised, each on one timeline |
| Problem files | Damaged files: 5 of 5 reported as failed. Identical copies: 5 of 5 skipped. Silent or audio-less clips: 20 of 20 sent to manual sync. Footage from another event: 5 of 5 sent to review. |
| Memory | Peak 3.6 GB for the engine and its workers (during matching); 0.9 GB peak during analysis |
| Processor / graphics | 72 % of 4 cores on average. The graphics processor is **not used**: all analysis runs on the processor. |
| Disk | Project database 130 MB (+47 MB write-ahead log). Analysis cache 6.9 GB (about 115 MB per hour of audio). |
| Interface data at 4,230 clips | Full media index 0.27 s (1.6 MB); timeline 0.48 s; results summary 0.24 s; reopen 0.06 s |

Syncora 1.1 has therefore been tested at 4,000+ files, under the conditions in §2–3. The limits of that statement
are in §13. The most important: the media was generated, and it was 3 GB, not terabytes.

## 2. Test machine

| | |
|---|---|
| Processor | Intel Xeon @ 2.80 GHz, 4 logical cores (virtual machine) |
| Memory | 16 GB |
| Storage | Virtual disk (`vda`). Measured media reads were not the bottleneck at this data size (§11). |
| Graphics | None detected, none used |
| System | Linux 6.18, Python 3.11.15, FFmpeg 6.1.1 |
| Worker plan | Chosen automatically by `resources.recommend_workers()`: 4 metadata, 3 audio-analysis, 3 matching workers |

The installers are tested on Windows 10/11 x64, macOS arm64 and macOS x64 by the release workflow. There, a
279-file production runs end to end through the installed app. The 4,238-file run was done on this Linux machine
only.

## 3. The test production

The production was generated by `python -m mcsync.testing.production` (seed 7). Every file has a known true position,
so every placement can be scored.

* **20 sessions**, 25 minutes each and 3 hours apart. Sessions cycle through speech, mixed and music scenes.
* **Per session:**
  * 2 sound recorders, each writing 5 consecutive Broadcast WAV files of 5 minutes, with continuous time
    references;
  * 8 cameras with 25 clips each (20–60 s), recorded at random moments across the session.
* **Camera audio conditions,** rotated across cameras:

  | Condition | Audio |
  |---|---|
  | clean | 25 dB SNR |
  | noisy | 5 dB SNR |
  | far | reverberant |
  | phone | 400 Hz–3 kHz band |
  | drift | clock 35 ppm off |
  | quiet | −30 dB |
  | distorted | clipped |
  | scratch | 8 dB SNR, heavy reverb, high-passed |

* **Camera clocks:** correct, 97 s off, −1 h, +2 h, or randomly off by up to 10 minutes. A third of the cameras
  write free-run timecode.
* **Problem files:**
  * 10 clips with the microphone off;
  * 10 clips with no audio stream (drone);
  * 5 clips from another event;
  * 5 damaged (truncated) files;
  * 5 byte-identical copies in a backup folder;
  * 3 sidecar files (`.THM`, `.XML`, `README.txt`).
* **Media:** H.264 MP4 (64 × 36 video, AAC audio) and 16 kHz BWF WAV, 3.0 GB in total. The video is deliberately
  tiny, so the test measures file counts and audio rather than video bytes (see §13).

## 4. Time per stage

Measured by `python -m mcsync.testing.stress`, which drives the same engine service the desktop app uses, the way an
editor would. Times are from the start of the import.

| Stage | Time | Notes |
|---|---|---|
| Discovery (find 4,238 files) | 3.0 s | Cold disk cache. Files appear in the browser as they are found. |
| Metadata + audio analysis (4,230 clips) | 16 min 17 s | 260 clips per minute; 61.4 h of audio at 226× real time on 3 analysis workers. Probing runs alongside. |
| ↳ pause, then resume | pause took effect in 0.07 s | The harness waited 3 s. One analysis finished recording in that window; no new work started (see §7). |
| ↳ quit mid-analysis and reopen | close 0.35 s, reopen 0.06 s | Reopening offered to resume 1,675 queued analyses. All finished work was kept. |
| Candidate planning | 4 min 35 s | Clock overlaps plus fingerprint votes: 26,384 pairs to verify, out of 8.9 million possible (0.3 %) |
| Verification (26,384 pairs) | 3 min 1 s | 145 pairs/s on 3 matching processes |
| Extended search (40 clips without a confident match, 240 searches) | 1.6 s | |
| Placement, sessions, results written | 24.6 s | The solve itself takes 0.6 s (one sparse system for all groups). The rest is reading inputs and writing 4,230 placements. |
| **Import to finished sync** | **24 min 23 s** | |
| Save and reopen the synchronised project | 0.07 s + 0.06 s | Reopen checks every file is still there |
| Synchronise again, nothing changed | 1 min 31 s | 0 pairs verified again; all 26,384 reused. Most of this is re-planning (§11). |

## 5. Resources

| | Analysis | Sync | Whole run |
|---|---|---|---|
| Peak memory (engine + workers) | 0.94 GB | 3.57 GB | 3.57 GB |
| Mean memory | 0.41 GB | 1.72 GB | 0.87 GB |
| Mean processor use (4 cores) | 79 % | 59 % | 72 % |
| Graphics processor | not used | not used | not used |

| Storage | Size |
|---|---|
| Project database (`.syncora`, SQLite) | 130 MB, plus 47 MB write-ahead log |
| Rows | 4,230 media, 4,230 clips, 35,079 tasks, 53,248 pair matches, 4,220 analyses |
| Analysis cache (8 kHz signals, waveforms, fingerprints) | 6.9 GB, of which the fingerprint index is 139 MB |
| Media read | 3.0 GB, read in place; never copied, modified or deleted |

The cache is about 115 MB per hour of recorded audio, whatever the video size. For a real production of this length
it would be a small fraction of the media.

## 6. Accuracy at scale

Every placement was scored against the truth (`mcsync.testing.production.evaluate`). "Exact" means within 20 ms,
half a frame at 25 fps.

| Camera condition | Files | Exact | Wrong | Not joined to their session |
|---|---|---|---|---|
| clean, noisy, far, phone, drift, quiet, distorted | 3,500 | 3,500 | 0 | 0 |
| scratch | 500 | 400 | 0 | 100 |
| recorders | 200 | 200 | 0 | 0 |
| **All** | **4,200** | **4,100** | **0** | **100** |

The 100 unjoined files are the scratch-microphone cameras at the 4 music sessions (25 clips each). Their audio never
produced a confident match. Each camera's clips stay together, placed by its own clock, in a group of their own, and
the results list them under **Manual sync required**. Joining a group to a session needs a confident match, so
these are never guessed.

| Result category (as the interface shows it) | Clips |
|---|---|
| Synchronized | 4,002 (3,989 high confidence) |
| Review recommended | 103 |
| Manual sync required | 120 (the 100 above, 10 muted, 10 without audio) |
| Failed (unreadable) | 5 |
| Skipped (identical copies) | 5 |

The 5 clips from another event are all in Review. Nothing below the 85 % threshold is locked automatically.

**Found by this test and fixed before the final run.** Earlier runs of the same production found several problems:

* **File descriptors ran out** after about 250 clips (macOS allows 256). Fix: signals are now memory-mapped lazily
  through a bounded cache.
* **The solve took 33 s.** It is now 0.6 s: vectorised, one sparse system.
* **70 of 4,200 placements were wrong,** with 13 sessions recognised instead of 20. Three causes:
  * split recorder files lost their time references (mistaken for rec-run timecode);
  * uncertain matches joined whole sessions;
  * confident-looking matches with near-zero correlation, in music sessions.

  After these fixes: 0 wrong placements, and all 20 sessions recognised
  (see [docs/SYNC_ENGINE.md](docs/SYNC_ENGINE.md) §6–7).
* **Adding media resumed a paused pipeline,** so files could start processing while paused. Fixed after this
  run; the pause itself was not affected.

## 7. Robustness

| Check | Result |
|---|---|
| Pause during analysis | Took effect in 0.07 s; running audio decodes are interrupted and return to the queue. One analysis finished recording in the 3 s after the pause. Since this run, adding media no longer resumes a paused pipeline: new files are counted and wait for Resume. A unit test checks that nothing starts while paused. |
| Quit during analysis (desktop app) | The e2e suites quit the app mid-analysis on every run. A rare hang was found this way (about 1 quit in 10 on Linux): a request sent in the last moment before the engine exited failed; the unhandled error opened Electron's modal error box, which stalled the app's main process. The error is now handled, nothing is sent once the engine is stopping, and a unit test covers the failing write. |
| Quit during analysis, reopen | The project reopened in 0.06 s and offered to resume 1,675 queued analyses. Finished work was kept, and the run completed normally. |
| Reopen a synchronised project | 0.06 s for 4,230 clips (every file checked to still be there) |
| Synchronise again with nothing changed | Every verified pair reused; nothing re-analysed or re-verified |
| Damaged files | Reported as failed with the reason; retrying them keeps them failed; never fatal to the run |
| Original media | Opened read-only; never modified or deleted. Removing clips from a project leaves the files on disk (checked by `app/e2e/production.spec.ts`). |

## 8. Interface at scale

| Operation (engine side, 4,230 clips) | Time |
|---|---|
| Media index: every clip with its status, for the browser | 0.27 s (1.6 MB) |
| Search "CAMA" / filter recorded 10:30–12:00 / filter unsynchronized / confidence < 80 % / sort by time | ≤ 1 ms each |
| Timeline for the whole production | 0.48 s (2.5 MB) |
| Results summary (per category, per source) | 0.24 s |

In the app, the media browser renders only the rows and cards on screen: the production suite checks that fewer than
80 cards exist in the page for 279 clips. The timeline pans and zooms a 300-clip, 3-hour project at 60 fps (p50 and
p95 16.7 ms, software rendering on a CI Linux runner, `app/e2e/timeline-perf.spec.ts`). Progress shows counts and
states only, never time estimates.

### Editing in the app at 4,230 clips (1.1.1)

The same production, opened in the app and edited on the Timeline. The times run from the action to the visible
result, driven by Playwright on the test machine (§2). They come from one run after the project had been opened
once; the first open of that session, with the project file not yet in the disk cache, took 13.4 s instead of
1.9 s.

| Action | 1.1.0 | 1.1.1 |
|---|---|---|
| Open the project, to the media browser with 4,230 clips | not measured | 1.9 s |
| Switch to the Timeline | not measured | 0.2 s |
| Drag a clip: new position shown | when the engine answered | at once (0.2 s including the drag) |
| Drag a clip: engine confirms (first correction after opening) | up to 17.7 s | 1.3 s |
| Later corrections and undo (engine time) | 3.5–4.6 s | 0.8–1.2 s |
| Five quick nudges | five corrections, one after the other | shown at once; one correction, confirmed in 1.3 s |
| Review list | 4,015 entries, all drawn | 57 entries, only the visible rows drawn |

In 1.1.0, each correction re-read every clip's analysis and every match of the last run. It also saved every
placement again. In 1.1.1 the engine keeps these between corrections, prepares them in the background when the
Timeline opens, and saves only the placements that changed. The main thread of the window was blocked for at most
0.3 s in any of these steps (typing a search at 4,230 clips).

## 9. Database benchmark

`python -m mcsync.testing.dbbench` builds projects of 5,000 and 10,000 media files (5 % audio). Each also holds
50,000 pair matches, 50,000 AI-analysis rows, 100,000 transcript segments and 50,000 markers. It then times what the
app does with them. The timings below are for the current code.

| Operation | 5,000 media | 10,000 media |
|---|---|---|
| Insert media (batches of 500) | 0.77 s | 1.35 s |
| Load the clip list (cold) | 0.16 s | 0.39 s |
| Media rows for the browser | 49 ms | 94 ms |
| Filter by name / by time of day | 1 ms | 2 ms |
| Filter by creation-time range (SQL index) | 0.2 ms | 0.2 ms |
| Task counts for the queue panel | 4 ms | 5 ms |
| Claim and finish every analysis task (batches of 64) | 0.10 s | 0.18 s |
| Store 50,000 matches (batches of 1,000) | 2.8 s | 2.8 s |
| Matches of one clip (index) | 44 ms | 46 ms |
| Store 100,000 transcript segments | 3.4 s | 3.6 s |
| Full-text transcript search | 8 ms | 8 ms |
| Assign 1,000 clips to a camera | 6 ms | 8 ms |
| Reopen (migration check and a stat of every file) | 0.10 s | 0.16 s |
| Database size | 76 MB | 87 MB |
| Peak memory | 311 MB | 360 MB |

Transcript and marker tables exist and are benchmarked, but transcription itself is not part of Syncora 1.1.

## 10. Accuracy benchmark: offsets × conditions

`python -m mcsync.testing.accuracy` renders a 120 s reference recording and a 30 s camera clip starting 0.5, 1, 3,
10 and 60 s into it, under nine audio conditions. Each pair is synchronised two ways:

* by the full matcher (every lag);
* by the large-production path: a fingerprint index shared with 12 unrelated recordings, verification in narrow
  windows, and the extended search when there is no candidate.

| Condition | Full matcher | Large-production path |
|---|---|---|
| clean (40 dB SNR) | 5 / 5 exact, confident | 5 / 5 exact, confident |
| noisy (0 dB SNR) | 5 / 5 | 5 / 5 |
| speech with room reverb | 5 / 5 | 5 / 5 |
| music | 5 / 5 | 5 / 5 (17 false fingerprint candidates in total, all rejected by verification) |
| applause | 5 / 5 | 5 / 5 |
| crowd (10 dB SNR) | 5 / 5 | 5 / 5 |
| scratch mic (6 dB SNR, reverb, band-limited, −20 dB) | 5 / 5 | 5 / 5 |
| distorted (clipped) | 5 / 5 | 5 / 5 |
| missing (camera audio silent) | 0 / 5: reported as no match | 0 / 5: extended search, reported as no match |

In every audible case, both paths found the offset with an error below 0.001 ms and confidence 1.00. The silent
camera was never given a position. These are synthetic signals and flatter real recordings. Real-world accuracy
figures (reverb, distance, drift over hours) are in [docs/SYNC_ENGINE.md](docs/SYNC_ENGINE.md) §9.

## 11. Bottlenecks

1. **Audio analysis: 67 % of the run.** Decoding and the analysis DSP run per clip on 3 workers here. The work is
   independent per file, so it should scale with processor cores. With real camera files it will also depend on
   read speed: FFmpeg reads each file once, in full, to extract its audio. That has not been measured at hundreds of
   gigabytes (§13).
2. **Candidate planning: 19 %.** Building the fingerprint index and querying every clip takes 275 s at 4,230 clips.
   It runs on fewer cores than the other stages, and it runs again when a project is re-synchronised with nothing
   changed (most of that 91 s). This is the next thing to optimise: skip re-planning when no clip changed, and
   parallelise the queries.
3. **Memory during matching.** 3.6 GB peak, from 3 matching processes plus the index. That is fine on 8 GB machines
   and above. On smaller machines, Settings → Performance can lower the matching workers.
4. **Cache size.** 115 MB per hour of audio must fit on the cache drive: 7 GB for this production.

The database and the solve were not bottlenecks at this size (§8, §9). Editing was, in 1.1.0: each correction took
3.5–4.6 s. Version 1.1.1 brought that to about 1 s (§8).

## 12. Recommended hardware

| For | Processor | Memory | Storage |
|---|---|---|---|
| Up to a few hundred files | 4 cores | 8 GB | SSD; cache space of 120 MB per hour of audio |
| 4,000+ files (as tested) | 4 cores works: 24 min for this production. 8+ cores recommended. | 16 GB (3.6 GB peak measured with 3 matching workers) | SSD for the cache; fast storage for the media (see bottleneck 1) |

No graphics processor is needed; Syncora does not use one.

## 13. Not tested (limits of this report)

* **Terabytes of real camera media.** The test production is 3 GB because its video is 64 × 36. Analysis reads
  every file in full. With 4K media, read speed will add to the analysis time; how much depends on the drives, and
  it has not been measured. As an estimate only: reading 2 TB at 400 MB/s takes about 1.5 hours.
* **Real cameras and recorders.** All files were generated with FFmpeg (H.264 MP4, BWF WAV). Other containers and
  codecs are covered by the engine's media tests, not at this scale.
* **Windows and macOS at 4,000+ files.** The large run was on Linux. On the other systems, CI runs the 279-file
  production end to end, including as an installed app.
* **More than 4,238 files in one project.** Nothing in the code limits the count; larger projects are untested.
* **Several projects or users at once.**
* **Hardware of the kind we recommend.** This was a 4-core virtual machine; faster machines have not been measured.
* **AI (visual or speech) sync, transcription, speaker and marker analysis.** Not part of Syncora 1.1. The request's
  "AI fallback" is implemented as an extended audio search followed by manual sync.

## 14. Reproducing

```bash
cd engine && python -m pip install -e ".[dev]"
python -m mcsync.testing.production FESTIVAL          # the 4,238-file production (about 16 min on 4 cores)
python -m mcsync.testing.stress FESTIVAL WORK --out stress-report.json
python -m mcsync.testing.dbbench --out dbbench.json   # 5,000 and 10,000 media
python -m mcsync.testing.accuracy --out accuracy.json
```

The stress report (JSON) holds every figure in §1 and §4–8. The app's end-to-end suites are in `app/e2e`
(see [app/e2e/README.md](app/e2e/README.md)).

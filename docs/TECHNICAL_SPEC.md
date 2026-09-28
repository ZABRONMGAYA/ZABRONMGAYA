# Technical specification: version 1

Audience: engineers building Syncora (formerly Multicam Sync). Architecture and rationale are in [ARCHITECTURE.md](ARCHITECTURE.md); the
synchronisation algorithms in [SYNC_ENGINE.md](SYNC_ENGINE.md).

## 1. Users and core workflow

Wedding filmmakers, event videographers, documentary producers and multicam editors. They typically bring:

* 2–6 cameras, each producing many clips: recording is interrupted between moments, cameras split files at 4 GB or
  12–30 minutes, and batteries get changed;
* 1–4 external audio recorders (lavaliers, a board feed, an ambient recorder) running for hours;
* sometimes phones and drones (drones usually without audio);
* sometimes jam-synced timecode (documentary and higher-end event crews); usually none (weddings).

Workflow: **import** folders/cards → **sync** (one click) → **review** the flagged clips → **export** an XML timeline
→ open it in Resolve or Premiere and edit (typically as a multicam clip).

## 2. Version 1 requirements

| # | Requirement | Design | Milestone |
|---|---|---|---|
| R1 | Import multiple video and audio files | Folder/file import with recursive scan, parallel ffprobe, device grouping (§4) | **M2 ✅** (engine), **M4 ✅** (UI) |
| R2 | Extract audio and read media metadata | ffprobe metadata model (§3), FFmpeg extraction to an 8 kHz analysis cache (§4) | M2 |
| R3 | Synchronise by audio waveform cross-correlation | Coarse envelope correlation + windowed GCC-PHAT verification ([SYNC_ENGINE §3–5](SYNC_ENGINE.md)) | **M1 ✅** |
| R4 | Timecode-based sync where available | Timecode maths (§5), clock domains in the solver, hybrid/timecode modes ([SYNC_ENGINE §7–8](SYNC_ENGINE.md)) | **M1 ✅** (engine), M2 (metadata) |
| R5 | Detect uncertain matches; allow manual corrections | Confidence model and flags; review queue; manual offsets, rejections, exclusions, snap-to-audio (§7) | **M1 ✅** (engine), **M4 ✅** (UI) |
| R6 | Display synchronised camera tracks on a timeline | Timeline model (§6), canvas timeline with waveform peaks | **M4 ✅** |
| R7 | Export XML for DaVinci Resolve and Premiere Pro | xmeml v5 (both NLEs), FCPXML 1.10 (Resolve, sample-accurate audio) (§8) | **M5 ✅** (NLE imports still to validate) |
| R8 | Preserve original media without re-encoding | Read-only access, separate cache, XML references originals; no encoder in the FFmpeg build | all |
| R9 | Long recordings, interrupted clips, different frame rates | Streaming extraction and memory-mapped cache; drift-aware solver; device clock domains; transitive placement; rational frame rates (§5, §6) | **M1 ✅** (engine), M2 |

## 3. Media and metadata

### 3.1 Supported inputs

Anything FFmpeg decodes. The formats below are tested explicitly:

| Kind | Containers | Typical sources |
|---|---|---|
| Video | MP4, MOV, MXF (OP1a), MTS/M2TS (AVCHD), MKV, AVI | Sony, Canon, Panasonic, Blackmagic, GoPro, DJI, phones |
| Audio | WAV/BWF (16/24/32-bit int, 32-bit float, mono and polyphonic), AIFF, MP3, AAC/M4A, FLAC | Zoom, Tascam, Sound Devices, Rode, phones |

* **Frame rates:** 23.976, 24, 25, 29.97 (DF and NDF), 30, 47.952, 48, 50, 59.94, 60, 100, 119.88, 120, and anything
  else FFmpeg reports as a rational.
* **Variable frame rate** is detected when `avg_frame_rate` differs from `r_frame_rate` by more than 0.1 %, or when the
  frame-duration spread is large. Such clips are flagged (see §9).
* **Audio:** any sample rate. Multichannel audio is downmixed for analysis by default; the user can pick one channel
  (for example to skip a −12 dB safety track, or to use the lavalier channel of a camera).

### 3.2 Metadata extracted (ffprobe `-show_format -show_streams -of json`)

| Field | Source | Used for |
|---|---|---|
| Duration, stream list, codecs | format / streams | Clip duration, which audio stream to analyse |
| `r_frame_rate`, `avg_frame_rate`, `time_base` | video stream | Exact rational frame rate, VFR detection |
| `start_time` per stream | streams | `audio_start_s` = audio start − video start (AAC priming, edit lists, MTS offsets) |
| Timecode | `format.tags.timecode`, stream tag `timecode`, `tmcd` data stream (MOV/MP4), MXF material package | Clock reading (TIMECODE) |
| `time_reference`, iXML/bext | WAV tags | Clock reading (BWF); sample-accurate recorder time of day |
| `creation_time` | format / stream tags | Clock reading (CREATION_TIME, σ = 1 s, per-device domain) |
| Make, model, serial | QuickTime `com.apple.quicktime.*`, vendor tags, XMP sidecars when present | Device identity |
| Rotation, dimensions | side data / tags | Display only (the export preserves the source) |

### 3.3 Device identity

Clips from one device never overlap. Knowing the device prunes pairs, orders interrupted clips, and defines a clock
domain. Identity is resolved in this order:

1. serial number;
2. make + model + card folder (for example `DCIM/100CANON`);
3. file-name pattern (for example `C0001.MP4` for Sony, `GH01xxxx.MP4` for GoPro chapters);
4. import folder.

The user can reassign devices in the media bin. **Chapter detection:** files a camera split from one continuous take
(GoPro `GH01/GH02…`, Sony/Canon 4 GB splits) are joined as consecutive segments of one logical clip, since the camera
guarantees they are gapless. That removes spurious gaps.

## 4. Audio extraction and cache

```
ffmpeg -nostdin -v error -i <media> -map 0:<stream> -vn -sn -dn \
       -af "pan=mono|c0=0.5*c0+0.5*c1,aresample=async=1" -ar 8000 -ac 1 -f f32le pipe:1
```

(Implemented in `media/extract.py`, M2.)

* **Downmix:** channels are averaged explicitly. FFmpeg's own `-ac 1` scales stereo by 1/√2, which would make levels
  3 dB different from the in-memory path. `pan=mono|c0=c<n>` selects a single channel instead.
* **Timing:** `aresample=async=1:first_pts=0` fills timestamp gaps with silence and pads a delayed stream from the
  container's time zero, so sample *n* is always *n*/8000 s after the container starts, and `audio_start_s` is the
  container start relative to the clip's first frame. The decoder's own timestamps place the audio. ffprobe's
  per-stream `start_time` is not used, because FFmpeg 6 and 7/8 report it differently for edit-list-delayed audio
  (found in CI: a 0.25 s error on macOS/Windows before this change).
* **Streaming:** the engine reads 1 MiB chunks, band-passes them (filter state carried across chunks), builds the
  waveform overview, and writes a temporary cache entry that is normalised in place and then renamed. FFmpeg's
  resampler and the engine's own agree to within a microsecond.
* **Edit lists:** FFmpeg skips AAC encoder priming when the MP4 edit list says so, which is the normal case. Delays
  expressed as empty edits are interpreted differently by FFmpeg 6/7 and 8. Audio is placed where the bundled decoder
  presents it, so the app ships one pinned FFmpeg build (M6), and such files are part of the NLE validation matrix.
* **Fingerprint:** SHA-1 of the size and the first and last MiB. Cache entries are keyed by fingerprint, stream,
  channel, and a hash of the analysis parameters. Moving, renaming or copying media keeps its cache; editing it
  invalidates it.
* **Waveform pyramid:** min/max peaks at 64·4ⁿ samples per bin (64 … 65 536), written as μ-law int8 arrays next to
  the PCM and read by the renderer through the main process (`peaks:read`), which only serves files inside the cache.
* **Budget:** 8 kHz float32 is 115 MB per hour of audio. The cache location is the OS cache directory (overridable),
  with an LRU size limit (default 20 GB) and a "clear cache" action.

## 5. Timecode and clocks

Implemented in `engine/src/mcsync/timecode.py` (M1).

* **Rates** are exact `Fraction`s. Decimal input snaps to the standard rates within 0.05 %.
* **Drop-frame** (29.97, 59.94, 119.88): skips 2 (or 4, or 8) labels per minute except each tenth minute. Labels that
  do not exist (`00:01:00;00`) are rejected. `01:00:00;00` at 29.97 DF is 107 892 frames = 3599.9964 s.
* **Wrap at 24 h:** readings of one clock spanning over 12 h are unwrapped; receptions run past midnight.
* **BWF:** `time_reference / sample_rate` = seconds since midnight. The iXML chunk (read directly, since ffprobe does
  not expose it) supplies the recorder's timecode rate and drop-frame flag.
* **Timecode families:** a reading's seconds are `frames / rate`, the real time since the label 00:00:00:00.
  Non-drop NTSC timecode (23.976, 29.97 NDF) runs 0.1 % slower than the wall clock (3.6 s per hour), while drop-frame
  and integer rates track it. Readings are only compared within one family, `ntsc` or `wall`.
* **Rec-run timecode:** many camcorders advance timecode only while recording, so consecutive takes look contiguous
  whatever the pause between them. When every pair of consecutive takes of a device (chapters excluded) is contiguous
  to within two frames, that device's timecode is not used as a clock (M2, `devices.is_record_run`).
* **Clock readings** passed to the engine: `start_s`, `domain`, `source`, `sigma_s`. A clip can carry several.
  * Timecode σ is one frame, BWF σ is 20 ms, creation time σ is 1 s, chapter position σ is 2 ms.
  * **Timecode domains:** `tc:<family>` shared by every device when the project says timecode is jam-synced;
    otherwise `tc:<device>:<family>` (still orders one device's takes).
  * **Creation time:** always per device (`ct:<device>`), since camera clocks are set by hand (often to the wrong
    time zone). Known gap: some devices stamp the *end* of recording. Per-device detection (start vs end, using the
    audio-synced clips) is planned for M3.
  * **Chapters:** `chapter:<take>` with each file's offset inside the take: exact, because cameras split takes
    without gaps.

## 6. Timeline model

* **Groups:** group 0 contains the reference clip. Groups ≥1 are sets of clips synced to each other but not to the
  reference (for example a second location). They are shown stacked after group 0 and joined by one manual offset.
* **Tracks:** one video track per camera device (V1 = reference camera or Cam A…) and one audio track per recorder
  channel/device. Clips of one device never overlap; if they do (a wrong device assignment), the clip moves to an
  overflow lane and the device is flagged.
* **Positions:** from the solver, in seconds relative to the reference. The timeline origin is the earliest clip
  start. The **sequence start timecode** is the reference's own timecode if it has one, otherwise `01:00:00:00`.
* **Sequence frame rate:** the most common camera rate (by duration), user-overridable. Clips keep their native rate.
* **Drift:** placements are the best constant alignment. Clips with `drift` show the accumulated error
  (`|drift_ppm|·duration/2`) in the inspector.

## 7. Review and manual corrections

| Action | Stored as | Engine effect |
|---|---|---|
| Drag / nudge a clip (±1 frame, ±1 sample, type an offset) | `ManualOffset(clip, anchor, offset)` | Exact constraint; audio edges that disagree are rejected |
| Snap to audio | `sync.snap`: match within ±`radius` around the dragged position | Replaces the drag with the refined offset |
| Pick an alternative candidate | `ManualOffset` at that candidate's offset | Same as a drag |
| Reject a match | `reject_pair(a, b)` | Edge ignored |
| Exclude a clip | `exclude(clip)` | Clip leaves sync and export |
| Undo / redo | Corrections are an append-only log with `undone_at` | Re-solve (milliseconds; about 1 s at 4,000 clips) |

The review queue lists every clip whose status is not `synced`, and clips that overlap another clip of their device
or are offline. It is ordered by severity:

1. conflicts;
2. detached groups;
3. uncertain matches;
4. metadata-only placements.

Each entry shows the reason (flags), the match-quality-over-time plot (fine windows), alternative candidates, and
overlaid waveforms of the clip and its best neighbour.

## 8. Export

Implemented in M5 (`engine/src/mcsync/export/`). Both formats are built from one sequence model, and one timeline
group is exported: by default group 0, the reference and everything synced to it.

### 8.1 Sequence and placement

* **Frame rate:** the video rate covering the most footage, or the user's choice. **Frame size:** the most common
  size at that rate. **Start timecode:** default `01:00:00:00`; `;` before the frames means drop-frame at 29.97 and
  59.94.
* **Tracks:** one video track per camera device (and per overflow lane), in timeline order. Each device also gets
  one audio track per channel of its audio stream: the stream chosen for syncing, otherwise the file's first.
  Recorder tracks are named from the BWF/iXML track names when present.
* **Positions are exact rationals** (`Fraction`), rounded once:
  * A clip with video starts on the nearest sequence frame from its first frame, at most ½ frame off (±20 ms at
    25 fps). That is the resolution any NLE places video at.
  * An audio-only clip (the recorder) starts on the first sequence frame at or after its true start. Its in point
    carries the sub-frame difference, to the sample. Formats with sub-frame in points place it exactly: FCPXML, and
    Premiere Pro through `pproTicksIn`. Readers that round in points to frames (Resolve reading xmeml) are at most
    ½ frame off.
  * Clip lengths are the media's own length in sequence frames, so an out point never passes the end of the media.
    Back-to-back clips of one device that overlap by a frame or two after rounding are trimmed; larger overlaps are
    reported.
* **Clips left out:** offline or changed media; other groups; clips not placed; optionally clips that need review.
  The report lists each of them with the reason.
* **Report:** per clip, the synchronised and exported positions and the error, as the format's readers will see it.
  Warnings cover clips placed by uncertain matches or camera clocks, variable frame rate, and clock drift beyond ½
  frame at a clip's ends. Clips are aligned at their middle; retiming in export is an M6 option.

### 8.2 FCP 7 XML (xmeml version 5): Premiere Pro and DaVinci Resolve

* One `<sequence>` with `<rate>` (`<timebase>` plus `<ntsc>` TRUE for 1001-denominator rates) and a `<timecode>`.
* Clip items follow Premiere Pro's convention for mixed-rate sequences:
  * `<rate>` is the sequence rate;
  * `<start>`/`<end>` count sequence frames from the sequence's first frame;
  * `<in>`/`<out>`/`<duration>` are sequence frames counted from the file's first frame;
  * `<pproTicksIn>`/`<pproTicksOut>` hold the exact in point (254 016 000 000 ticks per second).
* Each `<file>` keeps its own rate, length and timecode, and is described in full once. After that it is referenced
  as `<file id="…"/>`.
* `<pathurl>` is `file://localhost/…`, percent-encoded as UTF-8 (RFC 3986). Windows paths become
  `file://localhost/C:/…` and UNC paths `file://server/share/…`.
* Camera audio items (one per channel, `<sourcetrack>` = channel) are linked to their video item.

### 8.3 FCPXML 1.10: DaVinci Resolve and Final Cut Pro

* `<resources>` holds one `<format>` per frame rate and size, and one `<asset>` per file. The asset carries a
  `media-rep` URL (`file:///…`) and the file's own start: its timecode, or for recorders the BWF time reference, to
  the sample.
* The spine holds one gap spanning the sequence. Every clip is a connected `asset-clip`: cameras on lanes 1, 2, …
  with their own audio, recorders on lanes −1, −2, ….
  * `offset` is on the sequence's frame grid.
  * `start` is the asset start plus the in point, on the media's frame grid (video) or sample grid (audio).
* Times are rationals over the frame or sample grid, as Final Cut Pro writes them: `"86486400/24000s"`,
  `"1728001824/48000s"`.

### 8.4 Guarantees and validation

* Original files are referenced, never copied or transcoded; exports are written atomically (temporary file, then
  rename).
* **Automated:**
  * golden files (`fixtures/export/`);
  * xmeml read back with OpenTimelineIO's FCP 7 XML adapter;
  * FCPXML read back by a reader of the format's timing rules. OpenTimelineIO's FCPXML adapter truncates NTSC
    rates, so it only checks an integer-rate file.
  * the generated shoot is synced, exported in both formats, and every clip checked against the truth within ½
    frame.
* **NLE validation matrix, per release (manual, not done yet):**

  | NLE | Versions | xmeml import | FCPXML import |
  |---|---|---|---|
  | DaVinci Resolve | 19, 20 | to check | to check |
  | Premiere Pro | 2025, 2026 | to check | — |

  Each import is checked for correct positions, relinked media, audio channel mapping, and mixed-rate behaviour
  (23.976 + 29.97 DF + 50). Open questions an import settles:
  * whether each NLE takes the xmeml mixed-rate convention above;
  * whether Resolve derives FCPXML asset starts from the file's timecode as written;
  * how BWF files without a frame rate display their timecode.

## 9. Edge cases

| Case | Handling |
|---|---|
| Camera never recorded audio (drone, gimbal) | Placed by timecode/clock if available, otherwise unsynced for manual placement |
| Audio silent or muted (lens-cap microphone, input off) | `silent`; placed through its device clock if possible, otherwise manual |
| Recorder safety track (−12 dB duplicate channel) | Downmix is harmless (identical timing); per-channel selection available |
| Same song played at ceremony and reception | `ambiguous` with listed alternatives; hybrid mode resolves with creation time |
| Camera clock in the wrong time zone / set wrong | Per-device clock domain absorbs any constant error |
| Recorder runs past midnight | Timecode unwrap per domain |
| VFR phone footage | Flagged at import; the start is placed by audio, and drift is measured like any other clock error |
| AAC priming / edit lists / MTS start offsets | `audio_start_s` from stream `start_time` |
| Chaptered recordings (GoPro, 4 GB splits) | Joined as gapless segments of one clip (§3.3) |
| Clip shorter than 3 s | Cannot be matched by audio (`no_overlap`); clock or manual placement |
| Files moved or renamed after import | Offline status; relink by fingerprint within a chosen folder |
| Media on removable drives | Paths stored absolute plus relative to the project file; relink on open |

## 10. Persistence

Project file `*.syncora` (`*.mcsync` from earlier versions opens and is migrated) = SQLite, `journal_mode=WAL`,
`foreign_keys=ON`, migrations keyed by `PRAGMA user_version`. The schema is
[`engine/src/mcsync/project/schema.sql`](../engine/src/mcsync/project/schema.sql); version 2 (Syncora 1.1,
[`migrations.py`](../engine/src/mcsync/project/migrations.py)) adds the rows after `export`, with an index for
every query the media browser and the task queue make, and writes in batched transactions.

| Table | Holds |
|---|---|
| `project` | Name, engine version, settings (mode, reference clip, jam-synced timecode, creation-time use) |
| `device` | Identity key from `media/devices.py`, display name, kind, make/model/serial |
| `media_file` | Absolute and project-relative path, size, mtime, fingerprint, full parsed metadata (incl. raw ffprobe JSON), online/offline/changed status (refreshed on open) |
| `clip` | The engine's unit: device, selected audio stream and channel, chapter take/index/offset |
| `sync_run` | Each synchronisation run with its settings and outcome |
| `pair_match` | Every pairwise match, stored as it finishes, under a **pair key**: hash of both signals' fingerprints, streams, channels, audio offsets, the search window and the analysis parameters. A new run reuses every match whose key still applies. |
| `correction` | Append-only log (offset, clear_offset, reject/unreject pair, exclude/include) with `undone_at` for undo/redo |
| `placement` | The latest solve, for opening a project instantly |
| `export` | Exports and their reports (M5) |
| `media_file` (v2 columns), `pair_match` (v2) | Filename, kind, duration, rate, size, codec, audio format, timecode, creation time (for the browser and search without parsing JSON); `duplicate_of`, `duplicate_reason` (identical / probable), `duplicate_decision` (keep / ignore); pair matches gain offset and method (fingerprint, fallback, full) |
| `import_root`, `discovered` | What the user imported (for rescans and relinking), and every file found, before and after probing |
| `media_probe` | Raw ffprobe output, for diagnostics |
| `task` | The persistent work queue: kind (probe, analyze, match, extend), target (unique per kind), clip or pair, stage, priority (0 = most urgent), status (pending, processing, completed, failed, skipped, cancelled), attempts, error, run, timestamps |
| `audio_analysis` | Per clip: cache key (signal, waveform and fingerprint files), rate, samples, level, fingerprint size, status |
| `session` | Sessions with label, wall-clock span, the sync group they came from, and source (auto / manual); `clip.session_id` links clips |
| `meta` | Pipeline state, including the phase of an unfinished sync run (for resume) |
| `ai_analysis`, `transcript_segment`, `marker` | Reserved for analysis features that are not in 1.1; benchmarked at 50k and 100k rows |

Pair keys give **incremental synchronisation** and **crash resume**. Adding a camera to a synced project matches only
that camera's pairs. A run that was cancelled or killed restarts with everything it had already matched (tested by
killing the engine process mid-run). A second run with nothing changed reuses 19/19 pairs of the test shoot and
finishes in about 1 s.

## 11. Performance and accuracy targets

Reference machine: 8-core laptop (Apple M-series or recent x86), SSD, 16 GB RAM.

| Metric | Target | M1 measurement (4-vCPU container, one core) |
|---|---|---|
| Audio alignment error (typical camera/recorder audio) | ≤ 1 ms | ≤ 0.1 ms; ~10 µs typical |
| Pairwise match, 3 h vs 20 min | ≤ 2 s | 0.35 s (+1.4 s signal preparation) |
| Full sync, 100 clips around 2 × 3 h recorders | ≤ 3 min after extraction | not measured yet: at 60 ms/pair on one core, ~3,700 pairs take ~4 min serially; parallelised in M3 |
| Re-solve after a manual edit | ≤ 50 ms | 2.4 ms for 24 clips / 221 pairs; 0.8–1.2 s for 4,230 clips / 26,624 matches (1.1.1, see `SCALABILITY_TEST_REPORT.md` §8) |
| Extraction throughput | ≥ 50× real time per core (FFmpeg decode-bound) | M2 |
| Engine memory | ≤ 2 GB for a 10 h project | about 3× the active analysis signals; memory-mapped in M2 |
| UI | 60 fps timeline scroll/zoom with 300 clips | M4, software rendering (no GPU): panning at 59–60 fps (p95 16.8 ms); fast zooming at 53–55 fps (median 16.7 ms). Not yet measured on real hardware. |
| False confident matches on unrelated audio | 0 in the regression corpus | 0 / 132 synthetic pairs |

## 12. Tooling and conventions

* **Python 3.11+.** Runtime dependencies are NumPy and SciPy only. pytest for tests; ruff for lint and format (line
  length 120).
* **TypeScript (strict), React 18, Vite, Electron (current LTS).** ESLint and Prettier; Vitest for units and
  Playwright for end-to-end tests.
* **CI** (GitHub Actions): engine lint + tests on Linux/macOS/Windows for every push. The slow suite (`-m slow`) runs
  nightly and on release branches.
* **Versioning:** semantic versions shared by app and engine. The RPC handshake rejects a mismatched major version.

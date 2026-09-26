# Technical specification: version 1

Audience: engineers building Multicam Sync. Architecture and rationale are in [ARCHITECTURE.md](ARCHITECTURE.md); the
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
| R1 | Import multiple video and audio files | Folder/file import with recursive scan, parallel ffprobe, device grouping (§4) | M2 (engine), M4 (UI) |
| R2 | Extract audio and read media metadata | ffprobe metadata model (§3), FFmpeg extraction to an 8 kHz analysis cache (§4) | M2 |
| R3 | Synchronise by audio waveform cross-correlation | Coarse envelope correlation + windowed GCC-PHAT verification ([SYNC_ENGINE §3–5](SYNC_ENGINE.md)) | **M1 ✅** |
| R4 | Timecode-based sync where available | Timecode maths (§5), clock domains in the solver, hybrid/timecode modes ([SYNC_ENGINE §7–8](SYNC_ENGINE.md)) | **M1 ✅** (engine), M2 (metadata) |
| R5 | Detect uncertain matches; allow manual corrections | Confidence model and flags; review queue; manual offsets, rejections, exclusions, snap-to-audio (§7) | **M1 ✅** (engine), M4 (UI) |
| R6 | Display synchronised camera tracks on a timeline | Timeline model (§6), canvas timeline with waveform peaks | M4 |
| R7 | Export XML for DaVinci Resolve and Premiere Pro | xmeml v5 (both NLEs), FCPXML 1.10 (Resolve, sample-accurate audio) (§8) | M5 |
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
ffmpeg -nostdin -v error -i <media> -map 0:a:<k> -vn -ac 1 \
       -af aresample=resampler=soxr:osr=8000 -f f32le -     # streamed in 1 MiB chunks
```

* Output streams into `<cache>/<fingerprint>/<stream>/pcm_8000.f32` (temp file + rename), which is memory-mapped for
  analysis. `ac 1` downmixes; per-channel analysis uses `-af pan=mono|c0=c<n>` instead.
* Stream start offsets are preserved: `audio_start_s` comes from ffprobe and the extracted PCM starts at the stream's
  first sample. The engine converts audio offsets to clip offsets.
* **Fingerprint:** SHA-1 of the size, mtime and the first and last MiB. Cache entries are keyed by fingerprint, stream,
  and a hash of the analysis parameters. Moving or renaming media keeps its cache; editing it invalidates it.
* **Waveform pyramid:** min/max peaks at 64·2ⁿ samples per bin, written as int8 arrays next to the PCM and served to
  the renderer through `mcsync-cache://`.
* **Budget:** 8 kHz float32 is 115 MB per hour of audio. The cache location is the OS cache directory (overridable),
  with an LRU size limit (default 20 GB) and a "clear cache" action.

## 5. Timecode and clocks

Implemented in `engine/src/mcsync/timecode.py` (M1).

* **Rates** are exact `Fraction`s. Decimal input snaps to the standard rates within 0.05 %.
* **Drop-frame** (29.97, 59.94, 119.88): skips 2 (or 4, or 8) labels per minute except each tenth minute. Labels that
  do not exist (`00:01:00;00`) are rejected. `01:00:00;00` at 29.97 DF is 107 892 frames = 3599.9964 s.
* **Wrap at 24 h:** readings of one clock spanning over 12 h are unwrapped; receptions run past midnight.
* **BWF:** `time_reference / sample_rate` = seconds since midnight.
* **Clock readings** passed to the engine: `start_s`, `domain`, `source`, `sigma_s`.
  * Timecode σ is one frame, BWF σ is 20 ms, creation time σ is 1 s.
  * **Domains:** all timecode-bearing clips share one domain when the user confirms "timecode is jam-synced"
    (default when every device has timecode that agrees within a minute). Otherwise each device is its own domain.
    Creation time is always per device, since camera clocks are set by hand (often to the wrong time zone).

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
| Undo / redo | Corrections are an append-only log with `undone_at` | Re-solve (milliseconds) |

The review queue lists every clip whose status is not `synced`, ordered by severity:

1. conflicts;
2. detached groups;
3. uncertain matches;
4. metadata-only placements.

Each entry shows the reason (flags), the match-quality-over-time plot (fine windows), alternative candidates, and
overlaid waveforms of the clip and its best neighbour.

## 8. Export

### 8.1 FCP7 XML (xmeml version 5): primary, imported by Premiere Pro and DaVinci Resolve

* One `<sequence>` with `<rate><timebase>` and `<ntsc>` (TRUE for 1001-denominator rates), plus
  `<timecode>` for the start.
* `<media><video>`: one `<track>` per camera device. `<media><audio>`: one `<track>` per audio channel/device.
  Camera audio goes on its own tracks, linked to its video clipitem.
* Each `<clipitem>` has `<start>`/`<end>` (sequence frames), `<in>`/`<out>` (source frames) and a `<file id>` with
  `<pathurl>` (`file://` URL, RFC 3986 percent-encoding, `localhost` form on Windows), `<rate>`, `<duration>`,
  `<timecode>`, and `<media>` audio/video `<samplecharacteristics>`. Repeated references to a file use the empty
  `<file id="…"/>` form.
* **Mixed frame rates:** each file keeps its own `<rate>`; clip positions are converted with exact rationals.
  Which timebase `<in>`/`<out>` must use for mixed-rate clips differs between NLE versions. It is pinned down with
  golden files and real imports in M5 before the exporter is considered done.
* **Quantisation:** positions are rounded to whole sequence frames, at most ½ frame (for example ±20 ms at 25 fps).
  The export report lists each clip's rounding. Audio-only clips on a 25 fps sequence can be ±20 ms off, which is
  audible as echo if camera audio and recorder audio are mixed; hence FCPXML.

### 8.2 FCPXML 1.10: secondary, for DaVinci Resolve (and Final Cut Pro)

Rational time values (`"12012/24000s"`, `"441/44100s"`) allow sample-accurate audio placement. Structure: `<resources>`
(formats, assets with `media-rep src`), `<library><event><project><sequence><spine>` with a gap as the spine and
connected clips on lanes per device.

### 8.3 Guarantees

* Original files are referenced, never copied or transcoded; exports are written atomically.
* A validation pass before writing checks for missing or offline files, zero-length clips and overlapping clips on
  one track.
* **NLE validation matrix, per release:**

  | NLE | Versions | xmeml import | FCPXML import |
  |---|---|---|---|
  | DaVinci Resolve | 19, 20 | ✓ | ✓ |
  | Premiere Pro | 2025, 2026 | ✓ | — |

  Each import is checked for correct positions, relinked media, audio channel mapping, and mixed-rate behaviour
  (23.976 + 29.97 DF + 50).

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

Project file `*.mcsync` = SQLite, `journal_mode=WAL`, `foreign_keys=ON`. Migrations are keyed by
`PRAGMA user_version`. The schema is implemented in M3; the DDL below is the target.

```sql
CREATE TABLE project (
  id             INTEGER PRIMARY KEY CHECK (id = 1),
  name           TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  engine_version TEXT NOT NULL,
  settings_json  TEXT NOT NULL DEFAULT '{}'      -- sync mode, reference, sequence rate, analysis params
);

CREATE TABLE device (
  id           INTEGER PRIMARY KEY,
  name         TEXT NOT NULL,                     -- "Cam A", "Zoom F6"
  kind         TEXT NOT NULL CHECK (kind IN ('camera','recorder','phone','drone','other')),
  make TEXT, model TEXT, serial TEXT,
  clock_domain TEXT,                              -- NULL: the device's own clock
  color        TEXT
);

CREATE TABLE media_file (
  id            INTEGER PRIMARY KEY,
  path          TEXT NOT NULL UNIQUE,             -- absolute
  rel_path      TEXT,                             -- relative to the project file, for relinking
  size_bytes    INTEGER NOT NULL,
  mtime_ns      INTEGER NOT NULL,
  fingerprint   TEXT NOT NULL,
  container     TEXT NOT NULL,
  duration_s    REAL NOT NULL,
  creation_time TEXT,
  probe_json    TEXT NOT NULL,                    -- raw ffprobe output
  status        TEXT NOT NULL DEFAULT 'online' CHECK (status IN ('online','offline','changed'))
);

CREATE TABLE video_stream (
  media_id     INTEGER PRIMARY KEY REFERENCES media_file(id) ON DELETE CASCADE,
  stream_index INTEGER NOT NULL,
  codec TEXT NOT NULL, width INTEGER, height INTEGER, rotation INTEGER NOT NULL DEFAULT 0,
  fps_num INTEGER NOT NULL, fps_den INTEGER NOT NULL,
  is_vfr       INTEGER NOT NULL DEFAULT 0,
  start_time_s REAL NOT NULL DEFAULT 0,
  timecode TEXT, drop_frame INTEGER
);

CREATE TABLE audio_stream (
  media_id           INTEGER NOT NULL REFERENCES media_file(id) ON DELETE CASCADE,
  stream_index       INTEGER NOT NULL,
  codec TEXT NOT NULL, sample_rate INTEGER NOT NULL, channels INTEGER NOT NULL, channel_layout TEXT,
  start_time_s       REAL NOT NULL DEFAULT 0,
  duration_s         REAL,
  bwf_time_reference INTEGER,                     -- samples since midnight
  PRIMARY KEY (media_id, stream_index)
);

CREATE TABLE clip (                               -- the unit the sync engine sees
  id                 INTEGER PRIMARY KEY,
  device_id          INTEGER REFERENCES device(id) ON DELETE SET NULL,
  audio_stream_index INTEGER,
  audio_channel      INTEGER,                     -- NULL: downmix
  audio_start_s      REAL NOT NULL DEFAULT 0,
  clock_start_s      REAL,
  clock_source       TEXT CHECK (clock_source IN ('timecode','bwf','creation_time')),
  clock_sigma_s      REAL,
  excluded           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE clip_segment (                       -- 1 row per file; >1 for chaptered recordings
  clip_id  INTEGER NOT NULL REFERENCES clip(id) ON DELETE CASCADE,
  seq      INTEGER NOT NULL,
  media_id INTEGER NOT NULL REFERENCES media_file(id) ON DELETE CASCADE,
  PRIMARY KEY (clip_id, seq)
);

CREATE TABLE analysis (                           -- cache index; data lives in files
  media_id     INTEGER NOT NULL REFERENCES media_file(id) ON DELETE CASCADE,
  stream_index INTEGER NOT NULL,
  params_hash  TEXT NOT NULL,
  pcm_path TEXT NOT NULL, peaks_path TEXT NOT NULL,
  level_dbfs REAL, created_at TEXT NOT NULL,
  PRIMARY KEY (media_id, stream_index, params_hash)
);

CREATE TABLE sync_run (
  id INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('running','completed','cancelled','failed')),
  params_json TEXT NOT NULL, engine_version TEXT NOT NULL
);

CREATE TABLE pair_match (                         -- written as pairs finish: runs resume after a crash
  run_id        INTEGER NOT NULL REFERENCES sync_run(id) ON DELETE CASCADE,
  ref_clip_id   INTEGER NOT NULL REFERENCES clip(id) ON DELETE CASCADE,
  tgt_clip_id   INTEGER NOT NULL REFERENCES clip(id) ON DELETE CASCADE,
  offset_s REAL, offset_time_s REAL NOT NULL,
  confidence REAL NOT NULL, status TEXT NOT NULL,
  drift_ppm REAL, drift_std_ppm REAL, std_error_s REAL,
  estimate_json TEXT NOT NULL,                    -- windows, alternatives, flags
  PRIMARY KEY (run_id, ref_clip_id, tgt_clip_id)
);

CREATE TABLE manual_correction (                  -- append-only log; undo sets undone_at
  id            INTEGER PRIMARY KEY,
  kind          TEXT NOT NULL CHECK (kind IN ('offset','reject_pair','exclude')),
  clip_id       INTEGER NOT NULL REFERENCES clip(id) ON DELETE CASCADE,
  other_clip_id INTEGER REFERENCES clip(id) ON DELETE CASCADE,
  offset_s      REAL,
  created_at    TEXT NOT NULL,
  undone_at     TEXT
);

CREATE TABLE placement (                          -- latest solve
  clip_id    INTEGER PRIMARY KEY REFERENCES clip(id) ON DELETE CASCADE,
  run_id     INTEGER REFERENCES sync_run(id) ON DELETE SET NULL,
  start_s REAL, group_no INTEGER,
  method TEXT NOT NULL, confidence REAL NOT NULL, status TEXT NOT NULL,
  drift_ppm REAL NOT NULL DEFAULT 0, flags_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE export (
  id INTEGER PRIMARY KEY, format TEXT NOT NULL, path TEXT NOT NULL,
  sequence_rate TEXT NOT NULL, created_at TEXT NOT NULL, report_json TEXT NOT NULL
);
```

## 11. Performance and accuracy targets

Reference machine: 8-core laptop (Apple M-series or recent x86), SSD, 16 GB RAM.

| Metric | Target | M1 measurement (4-vCPU container, one core) |
|---|---|---|
| Audio alignment error (typical camera/recorder audio) | ≤ 1 ms | ≤ 0.1 ms; ~10 µs typical |
| Pairwise match, 3 h vs 20 min | ≤ 2 s | 0.35 s (+1.4 s signal preparation) |
| Full sync, 100 clips around 2 × 3 h recorders | ≤ 3 min after extraction | not measured yet: at 60 ms/pair on one core, ~3,700 pairs take ~4 min serially; parallelised in M3 |
| Re-solve after a manual edit | ≤ 50 ms | 2.4 ms for 24 clips / 221 pairs |
| Extraction throughput | ≥ 50× real time per core (FFmpeg decode-bound) | M2 |
| Engine memory | ≤ 2 GB for a 10 h project | about 3× the active analysis signals; memory-mapped in M2 |
| UI | 60 fps timeline scroll/zoom with 300 clips | M4 |
| False confident matches on unrelated audio | 0 in the regression corpus | 0 / 132 synthetic pairs |

## 12. Tooling and conventions

* **Python 3.11+.** Runtime dependencies are NumPy and SciPy only. pytest for tests; ruff for lint and format (line
  length 120).
* **TypeScript (strict), React 18, Vite, Electron (current LTS).** ESLint and Prettier; Vitest for units and
  Playwright for end-to-end tests.
* **CI** (GitHub Actions): engine lint + tests on Linux/macOS/Windows for every push. The slow suite (`-m slow`) runs
  nightly and on release branches.
* **Versioning:** semantic versions shared by app and engine. The RPC handshake rejects a mismatched major version.

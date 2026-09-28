# Changelog

## 1.3.0

A real multicamera preview in the Sync workspace, a synchronisation engine that places noisy gimbal and phone
footage instead of leaving it for review, and no more "max_workers must be <= 61" on large Windows machines.
Projects from 1.2 open and are upgraded.

### Fixed

- **Windows: "ValueError: max_workers must be <= 61".** On computers with more than 61 usable processor threads the
  matcher asked Windows for more worker processes than it allows, and synchronisation failed. Worker counts now
  come from the processor, memory and platform (never more than 61 processes on Windows, whatever is set in
  Settings → Performance), and one failed file never stops the queue.
- **Too many clips left for review on noisy cameras** (for example a gimbal camera whose microphone hears mostly its
  motors and wind). The engine itself was improved, the thresholds were not lowered:
  - a noise-robust band envelope finds candidates in footage where the room is quieter than the motors;
  - the fine stage measures how far the correlation peak stands out, which tells the same sound from rhythmic
    look-alikes, so faint but genuine matches are no longer capped;
  - a new clock-anchoring step calibrates each camera's clock from its confidently matched clips and searches the
    rest narrowly where the clock puts them (temporal continuity);
  - a camera clock calibrated by confident matches now outweighs a single uncertain match that contradicts it,
    so clips without usable sound land near their true place (for review) instead of minutes away.

  Measured on the multicamera benchmark (known offsets, `python -m mcsync.testing.multicam`): gimbal clips
  auto-synced 54 → 60 of 60 (standard) and 0 → 43 of 60 (hard: 12 of the 17 left have no usable sound), with no
  false match in either. See docs/SYNC_ENGINE.md §9.1.

### Added

- **Multicamera viewer** in the Sync workspace (S06; Sync → Workspace, and the Timeline stage):
  - every camera plays its real media at the master clock's time with its offset and drift applied — natively where
    the computer decodes it (hardware decoding), through FFmpeg otherwise (ProRes, DNxHR, 10-bit, MXF…);
  - one master clock drives the cameras, the audio monitor, the timeline playhead and the timecode;
  - each camera window shows its camera, clip, source timecode, sync status, confidence and offset, and says
    NO MEDIA, OFFLINE, OUTSIDE CLIP, NOT YET SYNCED or REVIEW REQUIRED instead of holding a last frame;
  - grid, program (one camera large) and compare layouts; any number of cameras (16 per page; only the cameras on
    screen load media); select, solo, hide, pin; 1–9 pick a camera, Enter shows it large, F full screen;
  - one source heard at a time (the reference recorder by default), chosen per camera or muted (M);
  - scrubbing on the ruler, frame steps (← →), J K L, previous / next sync point;
  - preview quality Auto, Performance, Quality or a fixed 360 / 540 / 720 / 1080 / original, with the pictures per
    second and GPU or CPU decoding shown.
- **Sync inspector** (S06): offset from the reference in time and frames, method, confidence, evidence (windows
  that agree, peak prominence, how many other sources agree), drift, what the preview shows, master and source
  time; nudges of ±1 ms, ±1 frame, ±10 frames (⌥ , .  —  , .  —  ⇧ , .); Set sync point (S), Lock sync (⌘L), Reset
  (⌘⌫), Re-run AI (⇧A), Snap to audio, Place at playhead, Exclude, Use as reference; "Show evidence" lists the
  audio matches or opens the AI evidence.
- **Sync points:** pin moments of a clip to the timeline; two or more measure its drift and show whether they agree.
- **Review workflow** ("Review N clips"): each clip beside the camera that overlaps it best, with Play both, Accept,
  Adjust, Re-sync, Exclude, Previous and Next.
- **Statuses:** Confirmed (placed or accepted by you, or two other sources agree), High confidence, Synchronized,
  Review recommended, Manual sync required, Failed, Skipped. The project file keeps each clip's synchronisation in a
  `sync_result` view (camera, session, group, reference, offset, drift, method, confidence, evidence, status, manual
  adjustment, sync points, analysis version, time).
- **Acceptance test** of the preview (four cameras including a ProRes one and a noisy gimbal, two recorders, known
  offsets, a clip without sound): every camera window is read back and checked to show the same scene frame.

### Changed

- ← and → now move the playhead a frame (as in the design); , and . nudge the selected clip.
- FFmpeg in the installers adds the MJPEG encoder and hardware decoders (VideoToolbox on macOS, D3D11VA/DXVA2 on
  Windows) for the preview.

## 1.2.0

Transcripts, speakers, search and AI sync, all on your computer; every screen and setting is active. Projects from
1.1 open and are upgraded.

### Fixed

- **"Could not show the timeline" (Failed to execute 'roundRect'…).** A clip narrower than its outline, such as a
  short clip at a low zoom, made the Timeline fail to draw. Clips of any width now draw.
- **Footage wrongly shown as failed or skipped:**
  - files that are not media (camera sidecars such as `.LRF`, `.THM`, `.XML`, LUTs, checksums) were listed as
    failed; they are now left out;
  - clips from cameras started together (same name, time and length) were skipped as duplicates; only files with
    identical content are now left out, and look-alikes are kept until you decide;
  - recordings without a frame rate or a duration in their header (some phones, screen recorders, cut-off files)
    were rejected; they are now read to the end, and variable frame rates are handled;
  - a recording cut off at the end (card full, battery) failed as a whole; everything before the damage is used;
  - RAW video (`.R3D`, `.braw`, `.crm`, ARRIRAW, N-RAW) says why it cannot be decoded.
- **The app icon was missing** in Windows (title bar, taskbar, desktop shortcut, the installer and uninstaller) and in
  the macOS DMG. The installers now carry the Syncora icon and artwork.

### Added

- **Splash screen** while the engine starts (S00), with real start-up steps.
- **Analyze stage (03):**
  - Overview: transcription progress, speakers, moments, per-recording status, Redo and Cancel.
  - Transcript (S09): the recording plays with captions, lines by speaker and language, a mini timeline with
    markers, J K L, ⇧↑/⇧↓ and M; a language popover.
  - Speakers: rename in place, merge by dragging.
  - Markers: sound events heard while transcribing, and your own.
  - Search (S10) with ⌘K: phrases, speakers, markers and clip names, with why each result matched.
- **Transcription on this computer:** Whisper (base ships in the installer; tiny, small and turbo download on
  demand), Silero voice activity detection and 3D-Speaker voice fingerprints, through sherpa-onnx. Recordings are
  transcribed in 10-minute parts in the background pipeline, so long recordings show progress and survive a quit.
- **AI sync (S07):** Find with AI (⇧A) from the Inspector, or "Find them with AI" on Sync results for every clip
  audio could not place. Evidence lanes (speech, visual, audio at close range, clocks), candidates, preview side by
  side, Accept and lock (undoable). Settings → Synchronization → AI visual + speech fallback runs it after each sync
  and proposes placements for review, never locked.
- **Settings:** AI (models, downloads, fallback), Transcription (model, language), Appearance (dark, light,
  system), Media, Proxy, Export defaults, Keyboard shortcuts, Privacy and Updates.
- **Presets and Learn** on Home: starting settings for kinds of shoot (applied to new projects), and a six-step
  guide with tips for reliable sync.
- `mcsync transcribe FILE` on the command line.

### Changed

- Project files move to schema version 3 (transcripts, speakers, markers); older projects are upgraded when opened.
- The installers are about 200 MB larger: they include the speech, voice-activity and voice-fingerprint models.

## 1.1.1

Fixes for the Timeline and faster editing on large projects. Projects from 1.1.0 open unchanged.

### Fixed

- **The Timeline opened blank.** In a new project it was never reloaded after an import. It showed "Import at least
  two recordings" and 0 clips until the project was closed and opened again. The Timeline now reloads while media
  is being imported. Before the first synchronisation it says what is missing and offers the next step:
  - no media yet: Import media;
  - clips not synchronised yet: the clip count, audio-analysis progress and Sync all;
  - a synchronisation running: its phase, and Show progress.
- **The status bar showed 0 clips** while an import was running. It now counts from the live media index.
- **The review list named every clip of a second recording session** ("Not linked to the reference"), even the
  ones placed confidently: 4,015 of 4,230 clips in the test production. It now lists only clips that need a look:
  57 there.
- **An error in one view no longer blanks the window.** The view shows a message with Try again (Close for a
  dialog). If the window's renderer crashes, the window reloads and returns to the open project. The event is
  written to the engine log.
- Track headers used emoji for camera, recorder, phone and drone; they now use the app's icon set.

### Faster on large projects

Measured on the 4,230-clip test production ([SCALABILITY_TEST_REPORT.md](SCALABILITY_TEST_REPORT.md) §8):

- **Dragging or nudging a clip shows the new position at once.** The engine confirms it in about 1 s. Before, the
  clip waited for the engine: 3.5–4.6 s per correction, and up to 17.7 s for the first one after opening.
- **Arrow-key nudges are sent as one correction** when you stop pressing (after 0.35 s), instead of one per key
  press. "Updating…" shows in the Timeline bar while the engine works.
- The engine keeps what a correction needs between corrections, instead of reloading it for every clip each time:
  - every clip's audio analysis;
  - the last synchronisation's matches.

  It prepares both in the background when the Timeline opens, and saves only the placements that changed.
- The review list draws only the rows on screen, and shows each clip's camera next to its name (file names such as
  `C0001.MP4` repeat across cameras).
- Clip lookups by id no longer scan the whole timeline.
- Engine requests slower than half a second are written to `logs/engine.log` in the app's data folder, to help
  diagnose slow projects.

## 1.1.0

Large productions: a background pipeline for thousands of files, and a scalable candidate search and solver. See
[README.md](README.md) and [SCALABILITY_TEST_REPORT.md](SCALABILITY_TEST_REPORT.md).

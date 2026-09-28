# Changelog

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

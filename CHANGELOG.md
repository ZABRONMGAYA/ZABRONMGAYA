# Changelog

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

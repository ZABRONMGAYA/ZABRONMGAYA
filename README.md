# Multicam Sync

A desktop application that automatically synchronises multicamera video and external audio recordings. It matches
waveforms and timecode, flags anything uncertain for review, and exports a timeline XML for DaVinci Resolve and Adobe
Premiere Pro. Original media is never re-encoded or modified.

Built for wedding filmmakers, event videographers, documentary producers and multicam editors: hours of recorder
audio, dozens of interrupted camera clips, mixed frame rates, drifting clocks.

**Stack:** Python (NumPy, SciPy) engine · FFmpeg · SQLite · Electron + React desktop shell.

## Status

| Milestone | State |
|---|---|
| M0 Architecture and specifications | ✅ [docs/](docs/) |
| M1 Synchronisation engine + automated tests | ✅ [engine/](engine/) |
| M2 Media layer (ffprobe/ffmpeg, cache) | next |
| M3 Persistence, engine service, CLI | planned |
| M4 Desktop UI and timeline | planned |
| M5 XML export (Resolve, Premiere) | planned |
| M6 Hardening, packaging, beta | planned |

## Documentation

* [Architecture](docs/ARCHITECTURE.md): processes, key decisions, module layout, IPC contract, repository structure.
* [Technical specification](docs/TECHNICAL_SPEC.md): requirements, media and metadata, cache, timecode, timeline,
  corrections, export, SQLite schema, targets.
* [Synchronisation engine](docs/SYNC_ENGINE.md): the DSP and placement algorithms, confidence model, measured
  accuracy and speed, limitations.
* [Milestones](docs/MILESTONES.md): delivery plan with exit criteria.

## Engine quick start

```bash
cd engine
python -m pip install -e ".[dev]"
python -m pytest -m "not slow"        # ~45 s: unit, synthetic-offset and end-to-end tests
python -m pytest -m slow              # hour-long recordings with clock drift
python scripts/benchmark_sync.py      # speed on 10 min / 1 h / 3 h references
```

```python
from mcsync.sync import ClipInput, SyncEngine, SyncOptions, prepare_signal

clips = [
    ClipInput("recorder", audio=prepare_signal(recorder_pcm, 48000), device_id="zoom"),
    ClipInput("camA_0001", audio=prepare_signal(cam_pcm, 48000), device_id="camA"),
]
result = SyncEngine(SyncOptions(reference_clip_id="recorder")).run(clips)
placement = result.placements["camA_0001"]
placement.start_s, placement.status, placement.confidence, placement.flags
```

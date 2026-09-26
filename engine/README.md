# mcsync engine

The Python side of Multicam Sync: audio/timecode synchronisation today; media probing and extraction, project
persistence, the JSON-RPC service and XML export in later milestones (see `../docs/MILESTONES.md`).

## Layout

```
src/mcsync/
├── timecode.py          SMPTE timecode, drop-frame, rational frame rates
├── sync/
│   ├── params.py        tunable parameters
│   ├── types.py         inputs, results, flags
│   ├── signal.py        PCM → 8 kHz band-limited analysis signal
│   ├── features.py      coarse-stage log-energy envelope
│   ├── correlation.py   FFT cross-correlation, GCC-PHAT, peaks
│   ├── pairwise.py      two-clip offset + drift estimation
│   ├── confidence.py    confidence scoring and classification
│   ├── solver.py        drift-aware global placement
│   └── engine.py        orchestration (analyze / solve / snap)
└── testing/synthetic.py synthetic scenes and device recordings with ground truth
tests/                   pytest suite (`-m slow` for hour-long recordings)
scripts/benchmark_sync.py
```

## Development

```bash
python -m pip install -e ".[dev]"
ruff check src tests scripts && ruff format --check src tests scripts
python -m pytest -m "not slow"
python -m pytest -m slow
```

Algorithms, conventions and measured performance are documented in `../docs/SYNC_ENGINE.md`.

"""Multicam Sync engine.

The engine is the Python side of the desktop application. It owns media
analysis, audio/timecode synchronisation, project persistence and timeline
export. The Electron/React shell talks to it over JSON-RPC (see
``docs/ARCHITECTURE.md``).

Milestone 1 ships the synchronisation core (:mod:`mcsync.sync`) and the
timecode maths (:mod:`mcsync.timecode`).
"""

__version__ = "0.1.0"

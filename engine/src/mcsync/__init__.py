"""Syncora engine (package ``mcsync``, the name it had as Multicam Sync).

The engine is the Python side of the desktop application. It owns media
analysis, audio/timecode synchronisation, the background pipeline for
productions of thousands of files, project persistence and timeline export.
The Electron/React shell talks to it over JSON-RPC (see
``docs/ARCHITECTURE.md``).
"""

__version__ = "1.1.0"

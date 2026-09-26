"""Content fingerprints for cache keys and relinking.

SHA-1 over the file size and its first and last MiB: reading 2 MiB is instant
even on a slow card reader, and camera files differ within their headers
(creation dates, UUIDs) and their tails (index atoms). Moving, renaming or
copying a file keeps its fingerprint, so the analysis cache survives a
reorganised media drive. Editing the file changes it.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

_CHUNK = 1 << 20


def fingerprint(path: str | Path) -> str:
    size = os.path.getsize(path)
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(str(size).encode())
    with open(path, "rb") as f:
        digest.update(f.read(_CHUNK))
        if size > 2 * _CHUNK:
            f.seek(-_CHUNK, os.SEEK_END)
            digest.update(f.read(_CHUNK))
        elif size > _CHUNK:
            digest.update(f.read())
    return digest.hexdigest()

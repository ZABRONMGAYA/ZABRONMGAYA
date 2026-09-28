"""The project file: one SQLite database per project.

The engine is its only writer. One connection is shared by the RPC thread and the pipeline threads behind a lock;
SQLite runs in WAL mode so a crash never corrupts it, and multi-row changes are single transactions (batched:
thousands of files are one statement per table, not one transaction per row).

Built for productions with thousands of files (see ``migrations.py``): indexed metadata columns, a persistent task
queue, and an in-memory copy of the clip list that is only rebuilt when clips change.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path, PurePath
from typing import Any

from mcsync import __version__
from mcsync.media.devices import DeviceGuess
from mcsync.media.library import MediaItem
from mcsync.media.probe import AudioStreamInfo, MediaInfo
from mcsync.serialize import (
    match_from_dict,
    media_info_from_dict,
    media_info_to_dict,
    placement_from_dict,
    to_jsonable,
)
from mcsync.sync.types import ClipPlacement, ManualCorrections, ManualOffset, PairwiseMatch, SyncResult

from .migrations import LATEST, MIGRATIONS, has_fts, media_columns

SCHEMA_VERSION = LATEST
PROJECT_SUFFIX = ".syncora"
LEGACY_SUFFIXES = (".mcsync",)
CORRECTION_KINDS = ("offset", "clear_offset", "reject_pair", "unreject_pair", "exclude", "include")
TASK_STATUSES = ("pending", "running", "done", "failed", "skipped", "cancelled")
_CHUNK = 500  # SQLite host parameters per IN (...) list


class ProjectError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _chunks(values: Sequence, size: int = _CHUNK) -> Iterator[Sequence]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def duplicate_ignored(duplicate_of: int | None, decision: str | None, reason: str | None) -> bool:
    """Whether a clip that repeats another file is left out of sync and export. Byte-identical copies are, unless the
    user keeps them. Probable copies (same name, recording time and length, other bytes) are only a suggestion:
    cameras started together can match all three, so they stay in until the user chooses to ignore them."""
    if duplicate_of is None:
        return False
    if decision is not None:
        return decision != "keep"
    return reason != "probable"


def same_content(a: str, b: str, samples: int = 32, chunk: int = 32 << 10) -> bool:
    """Whether two files hold the same bytes, judged from ``samples`` chunks spread over them. A file that cannot be
    read (offline) is trusted to match its fingerprint."""
    try:
        size = os.path.getsize(a)
        if size != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            for k in range(samples):
                offset = max(0, (size - chunk) * k // max(1, samples - 1))
                fa.seek(offset)
                fb.seek(offset)
                if fa.read(chunk) != fb.read(chunk):
                    return False
    except OSError:
        return True
    return True


@dataclass(frozen=True)
class ClipRow:
    id: int
    name: str
    media_id: int
    path: str
    fingerprint: str
    status: str
    info: MediaInfo
    device_id: int | None
    device_key: str | None
    device_name: str | None
    device_kind: str | None
    audio_stream: int | None
    audio_channel: int | None
    chapter_take: str | None
    chapter_index: int | None
    chapter_offset_s: float | None
    session_id: int | None = None
    duplicate_of: int | None = None
    duplicate_decision: str | None = None
    duplicate_reason: str | None = None

    @property
    def engine_id(self) -> str:
        """The clip's id inside the sync engine."""
        return str(self.id)

    @property
    def audio_stream_info(self) -> AudioStreamInfo | None:
        return next((a for a in self.info.audio if a.index == self.audio_stream), None)

    @property
    def ignored_duplicate(self) -> bool:
        """A copy of another file that is left out of sync and export (see ``duplicate_ignored``)."""
        return duplicate_ignored(self.duplicate_of, self.duplicate_decision, self.duplicate_reason)

    def to_media_item(self) -> MediaItem:
        device = DeviceGuess(
            key=self.device_key or f"clip:{self.id}",
            name=self.device_name or self.name,
            kind=self.device_kind or "camera",
        )
        return MediaItem(
            info=self.info,
            fingerprint=self.fingerprint,
            device=device,
            audio_stream=self.audio_stream_info,
            channel=self.audio_channel,
            chapter=(self.chapter_take, self.chapter_index)  # type: ignore[arg-type]
            if self.chapter_take is not None
            else None,
            chapter_offset_s=self.chapter_offset_s or 0.0,
        )


@dataclass(frozen=True)
class Task:
    id: int
    kind: str
    target: str
    clip_id: int | None
    other_clip_id: int | None
    stage: str | None
    priority: int
    attempts: int
    detail: dict
    run_id: int | None


class Project:
    def __init__(self, path: Path, conn: sqlite3.Connection) -> None:
        self.path = path
        self._conn = conn
        self._lock = threading.RLock()
        #: The placements as last saved or loaded (None: not read yet), so a solve rewrites only what changed.
        self._saved_placements: dict[int, ClipPlacement] | None = None
        self._clip_cache: list[ClipRow] | None = None
        self._clip_version = 0

    # ------------------------------------------------------------ lifecycle

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -65536")  # 64 MB page cache
        return conn

    @classmethod
    def create(cls, path: str | Path, name: str | None = None) -> Project:
        path = Path(path)
        if path.exists():
            raise ProjectError(f"{path} already exists")
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = cls._connect(path)
        project = cls(path, conn)
        try:
            # executescript() commits whatever is open first, so the script carries its own transaction.
            schema = resources.files("mcsync.project").joinpath("schema.sql").read_text()
            conn.executescript(f"BEGIN;\n{schema}\nPRAGMA user_version = 1;\nCOMMIT;")
            project._migrate(1)
            with project._tx():
                conn.execute(
                    "INSERT INTO project (id, name, created_at, engine_version) VALUES (1, ?, ?, ?)",
                    (name or path.stem, _now(), __version__),
                )
        except BaseException:
            conn.close()
            for suffix in ("", "-wal", "-shm"):
                Path(str(path) + suffix).unlink(missing_ok=True)
            raise
        return project

    @classmethod
    def open(cls, path: str | Path) -> Project:
        path = Path(path)
        if not path.is_file():
            raise ProjectError(f"{path} does not exist")
        conn = cls._connect(path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0 or version > SCHEMA_VERSION:
            conn.close()
            raise ProjectError(f"{path} is not a project this version can open (schema {version})")
        project = cls(path, conn)
        project._migrate(version)
        project._refresh_media_status()
        project.reset_running_tasks()
        return project

    def _migrate(self, version: int) -> None:
        """Bring the file up to the current schema, one migration per transaction."""
        for target in range(version + 1, SCHEMA_VERSION + 1):
            with self._tx() as c:
                MIGRATIONS[target](c)
                c.execute(f"PRAGMA user_version = {target}")

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA optimize")
            except sqlite3.Error:
                pass
            self._conn.close()

    def __enter__(self) -> Project:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _query(self, sql: str, args: Sequence = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def _clips_changed(self) -> None:
        self._clip_cache = None
        self._clip_version += 1
        self._saved_placements = None  # removed clips take their placements with them (ON DELETE CASCADE)

    @property
    def clip_version(self) -> int:
        """Changes whenever clips, their media or their devices change (for callers' caches)."""
        return self._clip_version

    # ------------------------------------------------------------- settings

    @property
    def name(self) -> str:
        return self._query("SELECT name FROM project")[0]["name"]

    def settings(self) -> dict:
        return json.loads(self._query("SELECT settings_json FROM project")[0]["settings_json"])

    def update_settings(self, **changes: object) -> dict:
        with self._tx() as c:
            settings = json.loads(c.execute("SELECT settings_json FROM project").fetchone()[0])
            settings.update({k: v for k, v in changes.items()})
            c.execute("UPDATE project SET settings_json = ?", (json.dumps(settings),))
        return settings

    @property
    def index_id(self) -> str:
        """A stable id for this project's derived files outside the project (its fingerprint index)."""
        value = self.meta_get("index_id")
        if not isinstance(value, str):
            value = uuid.uuid4().hex
            self.meta_set("index_id", value)
        return value

    def meta_get(self, key: str, default: Any = None) -> Any:
        rows = self._query("SELECT value FROM meta WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def meta_set(self, key: str, value: object) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(value)))

    # -------------------------------------------------------------- devices

    def _upsert_device(self, c: sqlite3.Connection, guess: DeviceGuess) -> int:
        row = c.execute("SELECT id FROM device WHERE key = ?", (guess.key,)).fetchone()
        if row:
            return int(row["id"])
        cur = c.execute(
            "INSERT INTO device (key, name, kind, make, model, serial) VALUES (?, ?, ?, ?, ?, ?)",
            (guess.key, guess.name, guess.kind, guess.make, guess.model, guess.serial),
        )
        return int(cur.lastrowid)  # type: ignore[arg-type]

    def devices(self) -> list[dict]:
        rows = self._query(
            "SELECT device.*, COUNT(clip.id) AS clips FROM device LEFT JOIN clip ON clip.device_id = device.id "
            "GROUP BY device.id ORDER BY device.kind = 'recorder', device.name"
        )
        return [dict(r) for r in rows]

    def update_device(self, device_id: int, *, name: str | None = None, kind: str | None = None) -> None:
        with self._tx() as c:
            if name is not None:
                c.execute("UPDATE device SET name = ? WHERE id = ?", (name, device_id))
            if kind is not None:
                c.execute("UPDATE device SET kind = ? WHERE id = ?", (kind, device_id))
        self._clips_changed()

    def create_device(self, name: str, kind: str = "camera") -> int:
        with self._tx() as c:
            key = f"manual:{name}:{_now()}"
            cur = c.execute("INSERT INTO device (key, name, kind) VALUES (?, ?, ?)", (key, name, kind))
            return int(cur.lastrowid)  # type: ignore[arg-type]

    # --------------------------------------------------------- media, clips

    def add_media(self, items: Sequence[MediaItem]) -> list[int]:
        """Add (or refresh) probed media; returns the clip id of each item.

        A file whose content fingerprint matches another file already in the project is recorded as its possible
        duplicate (``duplicate_of``); nothing is ever deleted.
        """
        ids = []
        base = self.path.parent
        with self._tx() as c:
            for it in items:
                info = media_info_to_dict(it.info, include_raw=False)
                info_json = json.dumps(info)
                cols = media_columns(info, it.path)
                try:
                    rel = os.path.relpath(it.path, base)
                except ValueError:  # another drive on Windows
                    rel = None
                existing = c.execute("SELECT id FROM media_file WHERE path = ?", (it.path,)).fetchone()
                duplicate_of, reason = self._find_twin(c, it, cols)
                names = ", ".join(f"{k} = ?" for k in cols)
                if existing:
                    media_id = int(existing["id"])
                    c.execute(
                        f"UPDATE media_file SET size_bytes = ?, mtime_ns = ?, fingerprint = ?, info_json = ?, "
                        f"rel_path = ?, status = 'online', {names} WHERE id = ?",
                        (it.info.size_bytes, it.info.mtime_ns, it.fingerprint, info_json, rel, *cols.values(),
                         media_id),
                    )  # fmt: skip
                else:
                    cur = c.execute(
                        f"INSERT INTO media_file (path, rel_path, size_bytes, mtime_ns, fingerprint, info_json, "
                        f"added_at, duplicate_of, duplicate_reason, {', '.join(cols)}) "
                        f"VALUES ({', '.join('?' * (9 + len(cols)))})",
                        (it.path, rel, it.info.size_bytes, it.info.mtime_ns, it.fingerprint, info_json, _now(),
                         duplicate_of, reason, *cols.values()),
                    )  # fmt: skip
                    media_id = int(cur.lastrowid)  # type: ignore[arg-type]
                if it.info.raw:
                    c.execute(
                        "INSERT OR REPLACE INTO media_probe (media_id, raw_json) VALUES (?, ?)",
                        (media_id, json.dumps(it.info.raw)),
                    )
                device_id = self._upsert_device(c, it.device)
                chapter = it.chapter or (None, None)
                clip = c.execute("SELECT id FROM clip WHERE media_id = ?", (media_id,)).fetchone()
                values = (
                    device_id,
                    cols["filename"],
                    it.audio_stream.index if it.audio_stream else None,
                    it.channel,
                    chapter[0],
                    chapter[1],
                    it.chapter_offset_s if it.chapter else None,
                )
                if clip:
                    c.execute(
                        "UPDATE clip SET device_id = ?, name = ?, audio_stream = ?, audio_channel = ?, "
                        "chapter_take = ?, chapter_index = ?, chapter_offset_s = ? WHERE id = ?",
                        (*values, clip["id"]),
                    )
                    ids.append(int(clip["id"]))
                else:
                    cur = c.execute(
                        "INSERT INTO clip (device_id, name, audio_stream, audio_channel, chapter_take, chapter_index, "
                        "chapter_offset_s, media_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (*values, media_id),
                    )
                    ids.append(int(cur.lastrowid))  # type: ignore[arg-type]
        self._clips_changed()
        return ids

    @staticmethod
    def _find_twin(c: sqlite3.Connection, it: MediaItem, cols: dict) -> tuple[int | None, str | None]:
        """Another file already in the project with the same content (identical), or with the same name, length and
        recording time but other bytes (probable: a re-encoded or re-wrapped copy)."""
        row = c.execute(
            "SELECT id, path FROM media_file WHERE fingerprint = ? AND path != ? ORDER BY id LIMIT 1",
            (it.fingerprint, it.path),
        ).fetchone()
        # The fingerprint covers a file's size, start and end; files are only called identical when samples from
        # the middle agree too (two recorder tracks can share a header and silent ends).
        if row and same_content(it.path, row["path"]):
            return int(row["id"]), "identical"
        if cols["creation_time"] is None or cols["duration_s"] is None:
            return None, None
        row = c.execute(
            "SELECT id FROM media_file WHERE filename = ? AND creation_time = ? AND ABS(duration_s - ?) < 0.05 "
            "AND path != ? AND duplicate_of IS NULL ORDER BY id LIMIT 1",
            (cols["filename"], cols["creation_time"], cols["duration_s"], it.path),
        ).fetchone()
        return (int(row["id"]), "probable") if row else (None, None)

    def set_chapters(self, rows: Iterable[tuple[int, str | None, int | None, float | None, int | None]]) -> None:
        """``(clip_id, take, index, offset_s, device_id)``: chapters found once a device's files are all in."""
        with self._tx() as c:
            c.executemany(
                "UPDATE clip SET chapter_take = ?, chapter_index = ?, chapter_offset_s = ?, "
                "device_id = COALESCE(?, device_id) WHERE id = ?",
                [(take, index, offset, device, clip_id) for clip_id, take, index, offset, device in rows],
            )
            c.execute("DELETE FROM device WHERE id NOT IN (SELECT device_id FROM clip WHERE device_id IS NOT NULL)")
        self._clips_changed()

    _CLIP_SQL = """
        SELECT clip.*, media_file.path, media_file.fingerprint, media_file.status, media_file.info_json,
               media_file.duplicate_of, media_file.duplicate_decision, media_file.duplicate_reason,
               device.key AS device_key, device.name AS device_name, device.kind AS device_kind
        FROM clip JOIN media_file ON media_file.id = clip.media_id
        LEFT JOIN device ON device.id = clip.device_id
    """

    @staticmethod
    def _clip_row(r: sqlite3.Row) -> ClipRow:
        return ClipRow(
            id=r["id"],
            name=r["name"],
            media_id=r["media_id"],
            path=r["path"],
            fingerprint=r["fingerprint"],
            status=r["status"],
            info=media_info_from_dict(json.loads(r["info_json"])),
            device_id=r["device_id"],
            device_key=r["device_key"],
            device_name=r["device_name"],
            device_kind=r["device_kind"],
            audio_stream=r["audio_stream"],
            audio_channel=r["audio_channel"],
            chapter_take=r["chapter_take"],
            chapter_index=r["chapter_index"],
            chapter_offset_s=r["chapter_offset_s"],
            session_id=r["session_id"],
            duplicate_of=r["duplicate_of"],
            duplicate_decision=r["duplicate_decision"],
            duplicate_reason=r["duplicate_reason"],
        )

    def clips(self) -> list[ClipRow]:
        with self._lock:
            if self._clip_cache is None:
                self._clip_cache = [self._clip_row(r) for r in self._conn.execute(self._CLIP_SQL + " ORDER BY clip.id")]
            return list(self._clip_cache)

    def clip(self, clip_id: int) -> ClipRow:
        rows = self._query(self._CLIP_SQL + " WHERE clip.id = ?", (clip_id,))
        if not rows:
            raise ProjectError(f"no clip {clip_id}")
        return self._clip_row(rows[0])

    def clip_count(self) -> int:
        return int(self._query("SELECT COUNT(*) FROM clip")[0][0])

    def set_clip_device(self, clip_id: int, device_id: int) -> None:
        self.set_clips_device([clip_id], device_id)

    def set_clips_device(self, clip_ids: Sequence[int], device_id: int) -> None:
        with self._tx() as c:
            c.executemany("UPDATE clip SET device_id = ? WHERE id = ?", [(device_id, i) for i in clip_ids])
            c.execute("DELETE FROM device WHERE id NOT IN (SELECT device_id FROM clip WHERE device_id IS NOT NULL)")
        self._clips_changed()

    def set_clip_audio(self, clip_id: int, stream_index: int | None, channel: int | None) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE clip SET audio_stream = ?, audio_channel = ? WHERE id = ?", (stream_index, channel, clip_id)
            )
            c.execute("DELETE FROM audio_analysis WHERE clip_id = ?", (clip_id,))
        self._clips_changed()

    def remove_clips(self, clip_ids: Sequence[int]) -> None:
        """Remove clips from the project (the files on disk are never touched)."""
        with self._tx() as c:
            for chunk in _chunks(list(clip_ids)):
                marks = ",".join("?" * len(chunk))
                c.execute(
                    f"DELETE FROM task WHERE clip_id IN ({marks}) OR other_clip_id IN ({marks})", (*chunk, *chunk)
                )
                media = [r[0] for r in c.execute(f"SELECT media_id FROM clip WHERE id IN ({marks})", chunk)]
                c.executemany("DELETE FROM media_file WHERE id = ?", [(m,) for m in media])
            c.execute("DELETE FROM device WHERE id NOT IN (SELECT device_id FROM clip WHERE device_id IS NOT NULL)")
        self._clips_changed()

    def _refresh_media_status(self) -> None:
        """Mark media that went missing (unplugged drive) or changed since import. One stat per file."""
        updates = []
        for r in self._query("SELECT id, path, size_bytes, mtime_ns, status FROM media_file"):
            try:
                st = os.stat(r["path"])
            except OSError:
                status = "offline"
            else:
                status = "online" if (st.st_size, st.st_mtime_ns) == (r["size_bytes"], r["mtime_ns"]) else "changed"
            if status != r["status"]:
                updates.append((status, r["id"]))
        if updates:
            with self._tx() as c:
                c.executemany("UPDATE media_file SET status = ? WHERE id = ?", updates)
            self._clips_changed()

    def refresh_media_status(self) -> dict[str, int]:
        self._refresh_media_status()
        rows = self._query("SELECT status, COUNT(*) AS n FROM media_file GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    def known_paths(self) -> dict[str, tuple[int, int]]:
        """Every media path in the project with its size and modification time."""
        rows = self._query("SELECT path, size_bytes, mtime_ns FROM media_file")
        return {r["path"]: (r["size_bytes"], r["mtime_ns"]) for r in rows}

    # ------------------------------------------------------------- duplicates

    def duplicates(self) -> list[dict]:
        rows = self._query(
            "SELECT copy.id AS media_id, copy.path, copy.filename, orig.id AS original_id, orig.path AS original_path, "
            "orig.filename AS original_filename, copy.duplicate_decision AS decision, "
            "copy.duplicate_reason AS reason, copy.size_bytes, copy.duration_s, clip.id AS clip_id "
            "FROM media_file copy JOIN media_file orig ON orig.id = copy.duplicate_of "
            "JOIN clip ON clip.media_id = copy.id ORDER BY copy.id"
        )
        return [dict(r) for r in rows]

    def decide_duplicates(self, media_ids: Sequence[int], decision: str) -> None:
        if decision not in ("keep", "ignore"):
            raise ProjectError(f"unknown duplicate decision {decision!r}")
        with self._tx() as c:
            c.executemany(
                "UPDATE media_file SET duplicate_decision = ? WHERE id = ? AND duplicate_of IS NOT NULL",
                [(decision, m) for m in media_ids],
            )
        self._clips_changed()

    # ---------------------------------------------------------- offline media

    def offline_media(self) -> list[dict]:
        rows = self._query(
            "SELECT media_file.id AS media_id, clip.id AS clip_id, media_file.path, media_file.filename, "
            "media_file.size_bytes, media_file.status FROM media_file JOIN clip ON clip.media_id = media_file.id "
            "WHERE media_file.status != 'online' ORDER BY media_file.path"
        )
        return [dict(r) for r in rows]

    def relink(self, moves: Sequence[tuple[int, str, int, int]]) -> None:
        """``(media_id, new_path, size, mtime_ns)`` for media found at a new place (fingerprints already checked)."""
        base = self.path.parent
        with self._tx() as c:
            for media_id, new_path, size, mtime in moves:
                try:
                    rel = os.path.relpath(new_path, base)
                except ValueError:
                    rel = None
                c.execute(
                    "UPDATE media_file SET path = ?, rel_path = ?, filename = ?, size_bytes = ?, mtime_ns = ?, "
                    "status = 'online' WHERE id = ?",
                    (new_path, rel, PurePath(new_path.replace("\\", "/")).name, size, mtime, media_id),
                )
                info = json.loads(c.execute("SELECT info_json FROM media_file WHERE id = ?", (media_id,)).fetchone()[0])
                info["path"] = new_path
                c.execute("UPDATE media_file SET info_json = ? WHERE id = ?", (json.dumps(info), media_id))
        self._clips_changed()

    # --------------------------------------------------- import roots, discovery

    def add_import_root(self, path: str) -> int:
        with self._tx() as c:
            c.execute("INSERT OR IGNORE INTO import_root (path, added_at) VALUES (?, ?)", (path, _now()))
            return int(c.execute("SELECT id FROM import_root WHERE path = ?", (path,)).fetchone()[0])

    def import_roots(self) -> list[dict]:
        return [dict(r) for r in self._query("SELECT * FROM import_root ORDER BY id")]

    def add_discovered(self, rows: Sequence[tuple[str, int, int, str | None, int | None]]) -> list[int]:
        """``(path, size, mtime_ns, kind_guess, import_root_id)`` for files found on disk; returns the ids of new
        rows (files already known are skipped)."""
        now = _now()
        new: list[int] = []
        with self._tx() as c:
            for path, size, mtime, kind, root in rows:
                cur = c.execute(
                    "INSERT OR IGNORE INTO discovered (path, size_bytes, mtime_ns, kind_guess, import_root_id, "
                    "discovered_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (path, size, mtime, kind, root, now),
                )
                if cur.rowcount:
                    new.append(int(cur.lastrowid))  # type: ignore[arg-type]
        return new

    def discovered(self, ids: Sequence[int]) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for chunk in _chunks(list(ids)):
            rows = self._query(f"SELECT * FROM discovered WHERE id IN ({','.join('?' * len(chunk))})", chunk)
            out.update({r["id"]: dict(r) for r in rows})
        return out

    def finish_discovered(self, rows: Sequence[tuple[int, str, int | None, str | None]]) -> None:
        """``(id, status, media_id, error)``."""
        with self._tx() as c:
            c.executemany(
                "UPDATE discovered SET status = ?, media_id = ?, error = ? WHERE id = ?",
                [(status, media_id, error, i) for i, status, media_id, error in rows],
            )

    def rediscover(self, rows: Sequence[tuple[str, int, int]]) -> list[tuple[int, str]]:
        """Files that changed on disk ``(path, size, mtime_ns)``: back to pending; returns ``(id, path)``."""
        now = _now()
        out = []
        with self._tx() as c:
            for path, size, mtime in rows:
                c.execute(
                    "INSERT INTO discovered (path, size_bytes, mtime_ns, kind_guess, discovered_at) VALUES (?, ?, ?, "
                    "NULL, ?) ON CONFLICT (path) DO UPDATE SET size_bytes = excluded.size_bytes, "
                    "mtime_ns = excluded.mtime_ns, status = 'pending', error = NULL",
                    (path, size, mtime, now),
                )
                out.append((int(c.execute("SELECT id FROM discovered WHERE path = ?", (path,)).fetchone()[0]), path))
        return out

    def discovery_counts(self) -> dict:
        rows = self._query("SELECT kind_guess, status, COUNT(*) AS n FROM discovered GROUP BY kind_guess, status")
        by_kind: dict[str, int] = defaultdict(int)
        by_status: dict[str, int] = defaultdict(int)
        for r in rows:
            by_kind[r["kind_guess"] or "other"] += r["n"]
            by_status[r["status"]] += r["n"]
        return {"total": sum(by_status.values()), "by_kind": dict(by_kind), "by_status": dict(by_status)}

    # ------------------------------------------------------------------ tasks

    def enqueue(self, kind: str, items: Sequence[dict], *, requeue: bool = False) -> int:
        """Add tasks (``target`` required; ``clip_id``, ``other_clip_id``, ``stage``, ``priority``, ``detail``,
        ``run_id`` optional). A task already queued for the same target is kept as it is, unless ``requeue`` asks
        to run finished, failed or cancelled ones again. Returns how many became pending."""
        now = _now()
        rows = [
            (kind, str(it["target"]), it.get("clip_id"), it.get("other_clip_id"), it.get("stage"),
             int(it.get("priority", 5)), json.dumps(it["detail"]) if it.get("detail") is not None else None,
             it.get("run_id"), now, now)
            for it in items
        ]  # fmt: skip
        with self._tx() as c:
            before = c.total_changes
            c.executemany(
                "INSERT INTO task (kind, target, clip_id, other_clip_id, stage, priority, detail_json, run_id, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (kind, target) DO UPDATE SET "
                "status = CASE WHEN ?11 AND status != 'running' THEN 'pending' ELSE status END, "
                "error = CASE WHEN ?11 AND status != 'running' THEN NULL ELSE error END, "
                "priority = CASE WHEN ?11 THEN excluded.priority ELSE MIN(priority, excluded.priority) END, "
                "stage = COALESCE(excluded.stage, stage), detail_json = COALESCE(excluded.detail_json, detail_json), "
                "run_id = COALESCE(excluded.run_id, run_id), updated_at = excluded.updated_at",
                [(*r, 1 if requeue else 0) for r in rows],
            )
            changed = c.total_changes - before
        return changed

    def claim(self, kinds: Sequence[str], limit: int) -> list[Task]:
        """Take up to ``limit`` pending tasks of these kinds, most urgent first, and mark them running."""
        if limit <= 0:
            return []
        now = _now()
        marks = ",".join("?" * len(kinds))
        with self._tx() as c:
            rows = c.execute(
                f"SELECT * FROM task WHERE status = 'pending' AND kind IN ({marks}) ORDER BY priority, id LIMIT ?",
                (*kinds, limit),
            ).fetchall()
            c.executemany(
                "UPDATE task SET status = 'running', attempts = attempts + 1, updated_at = ? WHERE id = ?",
                [(now, r["id"]) for r in rows],
            )
        return [
            Task(r["id"], r["kind"], r["target"], r["clip_id"], r["other_clip_id"], r["stage"], r["priority"],
                 r["attempts"] + 1, json.loads(r["detail_json"]) if r["detail_json"] else {}, r["run_id"])
            for r in rows
        ]  # fmt: skip

    def finish_tasks(self, results: Sequence[tuple[int, str, str | None]]) -> None:
        """``(task_id, status, error)`` for finished tasks, in one transaction."""
        now = _now()
        with self._tx() as c:
            c.executemany(
                "UPDATE task SET status = ?, error = ?, updated_at = ? WHERE id = ? AND status = 'running'",
                [(status, error, now, i) for i, status, error in results],
            )

    def release_tasks(self, ids: Sequence[int]) -> None:
        """Put running tasks back in the queue as if never started (interrupted by a pause)."""
        with self._tx() as c:
            c.executemany(
                "UPDATE task SET status = 'pending', attempts = MAX(attempts - 1, 0) "
                "WHERE id = ? AND status = 'running'",
                [(i,) for i in ids],
            )

    def reset_running_tasks(self) -> int:
        """After a crash or a pause, running tasks go back to the queue (their results were never written)."""
        with self._tx() as c:
            return c.execute("UPDATE task SET status = 'pending' WHERE status = 'running'").rowcount

    def task_counts(self, run_id: int | None = None) -> dict[str, dict[str, int]]:
        """``{kind: {status: n}}``; with ``run_id``, match work of that sync run only (other kinds unaffected)."""
        out: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(TASK_STATUSES, 0))
        if run_id is None:
            rows = self._query("SELECT kind, status, COUNT(*) AS n FROM task GROUP BY kind, status")
        else:
            rows = self._query(
                "SELECT kind, status, COUNT(*) AS n FROM task WHERE run_id IS NULL OR run_id = ? GROUP BY kind, status",
                (run_id,),
            )
        for r in rows:
            out[r["kind"]][r["status"]] = r["n"]
        return dict(out)

    def pending_count(self, kinds: Sequence[str] | None = None) -> int:
        if kinds:
            marks = ",".join("?" * len(kinds))
            q = f"SELECT COUNT(*) FROM task WHERE status IN ('pending', 'running') AND kind IN ({marks})"
            return int(self._query(q, list(kinds))[0][0])
        return int(self._query("SELECT COUNT(*) FROM task WHERE status IN ('pending', 'running')")[0][0])

    def _task_filter(self, ids: Sequence[int] | None, clip_ids: Sequence[int] | None, kinds: Sequence[str] | None):
        where, args = [], []
        if ids is not None:
            where.append(f"id IN ({','.join('?' * len(ids))})")
            args += list(ids)
        if clip_ids is not None:
            marks = ",".join("?" * len(clip_ids))
            where.append(f"(clip_id IN ({marks}) OR other_clip_id IN ({marks}))")
            args += list(clip_ids) * 2
        if kinds is not None:
            where.append(f"kind IN ({','.join('?' * len(kinds))})")
            args += list(kinds)
        return (" AND " + " AND ".join(where)) if where else "", args

    def cancel_tasks(
        self,
        *,
        ids: Sequence[int] | None = None,
        clip_ids: Sequence[int] | None = None,
        kinds: Sequence[str] | None = None,
    ) -> int:
        """Cancel pending tasks (all, or those selected). Finished work is kept."""
        where, args = self._task_filter(ids, clip_ids, kinds)
        with self._tx() as c:
            return c.execute(
                f"UPDATE task SET status = 'cancelled', updated_at = ? WHERE status = 'pending'{where}", (_now(), *args)
            ).rowcount

    def retry_tasks(
        self,
        *,
        statuses: Sequence[str] = ("failed",),
        ids: Sequence[int] | None = None,
        clip_ids: Sequence[int] | None = None,
        kinds: Sequence[str] | None = None,
    ) -> int:
        where, args = self._task_filter(ids, clip_ids, kinds)
        marks = ",".join("?" * len(statuses))
        with self._tx() as c:
            return c.execute(
                f"UPDATE task SET status = 'pending', error = NULL, updated_at = ? WHERE status IN ({marks}){where}",
                (_now(), *statuses, *args),
            ).rowcount

    def prioritize(self, clip_ids: Sequence[int], priority: int = 0) -> int:
        """Move the pending work of these clips to the front of the queue."""
        n = 0
        with self._tx() as c:
            for chunk in _chunks(list(clip_ids)):
                marks = ",".join("?" * len(chunk))
                n += c.execute(
                    f"UPDATE task SET priority = ? WHERE status = 'pending' AND priority > ? "
                    f"AND (clip_id IN ({marks}) OR other_clip_id IN ({marks}))",
                    (priority, priority, *chunk, *chunk),
                ).rowcount
        return n

    def tasks(self, *, statuses: Sequence[str], kinds: Sequence[str] | None = None, limit: int = 200,
              offset: int = 0) -> list[dict]:  # fmt: skip
        """Tasks with the file they concern (for error lists and the running list)."""
        marks = ",".join("?" * len(statuses))
        args: list = list(statuses)
        kind_sql = ""
        if kinds:
            kind_sql = f" AND task.kind IN ({','.join('?' * len(kinds))})"
            args += list(kinds)
        rows = self._query(
            "SELECT task.id, task.kind, task.target, task.clip_id, task.other_clip_id, task.stage, task.priority, "
            "task.status, task.attempts, task.error, task.updated_at, "
            "COALESCE(clip.name, discovered.path) AS name, other.name AS other_name "
            "FROM task LEFT JOIN clip ON clip.id = task.clip_id LEFT JOIN clip other ON other.id = task.other_clip_id "
            "LEFT JOIN discovered ON task.kind = 'probe' AND discovered.id = CAST(task.target AS INTEGER) "
            f"WHERE task.status IN ({marks}){kind_sql} ORDER BY task.updated_at DESC, task.id LIMIT ? OFFSET ?",
            (*args, limit, offset),
        )
        return [dict(r) for r in rows]

    def clip_task_status(self) -> dict[int, dict[str, str]]:
        """``{clip_id: {kind: status}}`` for per-clip pipeline badges (match tasks summarised as their worst)."""
        out: dict[int, dict[str, str]] = defaultdict(dict)
        rank = {"failed": 5, "running": 4, "pending": 3, "cancelled": 2, "skipped": 1, "done": 0}
        for r in self._query("SELECT clip_id, kind, status FROM task WHERE clip_id IS NOT NULL"):
            current = out[r["clip_id"]].get(r["kind"])
            if current is None or rank[r["status"]] > rank[current]:
                out[r["clip_id"]][r["kind"]] = r["status"]
        return dict(out)

    def purge_tasks(self, kinds: Sequence[str], statuses: Sequence[str] = ("done", "cancelled", "skipped")) -> int:
        with self._tx() as c:
            return c.execute(
                f"DELETE FROM task WHERE kind IN ({','.join('?' * len(kinds))}) "
                f"AND status IN ({','.join('?' * len(statuses))})",
                (*kinds, *statuses),
            ).rowcount

    # --------------------------------------------------------- audio analysis

    def set_audio_analysis(self, rows: Sequence[dict]) -> None:
        now = _now()
        with self._tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO audio_analysis (clip_id, cache_key, rate, samples, level_dbfs, n_hashes, "
                "status, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(r["clip_id"], r["cache_key"], r.get("rate"), r.get("samples"), r.get("level_dbfs"),
                  r.get("n_hashes"), r["status"], now) for r in rows],
            )  # fmt: skip

    def task_counts_by_clip(self, kind: str) -> list[dict]:
        """``{clip_id, status, n}`` for one kind of task."""
        rows = self._query(
            "SELECT clip_id, status, COUNT(*) AS n FROM task WHERE kind = ? GROUP BY clip_id, status", (kind,)
        )
        return [dict(r) for r in rows]

    def audio_analysis(self) -> dict[int, dict]:
        return {r["clip_id"]: dict(r) for r in self._query("SELECT * FROM audio_analysis")}

    # --------------------------------------------------------------- sessions

    def replace_auto_sessions(self, sessions: Sequence[dict]) -> None:
        """Replace automatic sessions: ``{label, start_at, end_at, group_no, clip_ids}``. Clips the user put in a
        session by hand stay there."""
        with self._tx() as c:
            manual = {r[0] for r in c.execute(
                "SELECT clip.id FROM clip JOIN session ON session.id = clip.session_id WHERE session.source = 'manual'"
            )}  # fmt: skip
            c.execute(
                "UPDATE clip SET session_id = NULL WHERE session_id IN (SELECT id FROM session WHERE source = 'auto')"
            )
            c.execute("DELETE FROM session WHERE source = 'auto'")
            for s in sessions:
                cur = c.execute(
                    "INSERT INTO session (label, start_at, end_at, group_no, source) VALUES (?, ?, ?, ?, 'auto')",
                    (s["label"], s.get("start_at"), s.get("end_at"), s.get("group_no")),
                )
                sid = cur.lastrowid
                c.executemany(
                    "UPDATE clip SET session_id = ? WHERE id = ?",
                    [(sid, cid) for cid in s["clip_ids"] if cid not in manual],
                )
        self._clips_changed()

    def create_session(self, label: str, clip_ids: Sequence[int]) -> int:
        with self._tx() as c:
            cur = c.execute("INSERT INTO session (label, source) VALUES (?, 'manual')", (label,))
            sid = int(cur.lastrowid)  # type: ignore[arg-type]
            c.executemany("UPDATE clip SET session_id = ? WHERE id = ?", [(sid, cid) for cid in clip_ids])
        self._clips_changed()
        return sid

    def assign_session(self, clip_ids: Sequence[int], session_id: int | None) -> None:
        with self._tx() as c:
            c.executemany("UPDATE clip SET session_id = ? WHERE id = ?", [(session_id, cid) for cid in clip_ids])
            c.execute(
                "DELETE FROM session WHERE source = 'manual' AND id NOT IN "
                "(SELECT session_id FROM clip WHERE session_id IS NOT NULL)"
            )
        self._clips_changed()

    def sessions(self) -> list[dict]:
        rows = self._query(
            "SELECT session.*, COUNT(clip.id) AS clips FROM session LEFT JOIN clip ON clip.session_id = session.id "
            "GROUP BY session.id ORDER BY session.start_at IS NULL, session.start_at, session.id"
        )
        return [dict(r) for r in rows]

    # ---------------------------------------------------------- runs, matches

    def start_run(self, params: dict) -> int:
        with self._tx() as c:
            c.execute("UPDATE sync_run SET status = 'failed', finished_at = ? WHERE status = 'running'", (_now(),))
            cur = c.execute(
                "INSERT INTO sync_run (started_at, status, params_json, engine_version) VALUES (?, 'running', ?, ?)",
                (_now(), json.dumps(to_jsonable(params)), __version__),
            )
            return int(cur.lastrowid)  # type: ignore[arg-type]

    def finish_run(self, run_id: int, status: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE sync_run SET status = ?, finished_at = ? WHERE id = ?", (status, _now(), run_id))

    def runs(self) -> list[dict]:
        return [dict(r) for r in self._query("SELECT * FROM sync_run ORDER BY id")]

    def running_run(self) -> int | None:
        rows = self._query("SELECT id FROM sync_run WHERE status = 'running' ORDER BY id DESC LIMIT 1")
        return int(rows[0]["id"]) if rows else None

    def last_completed_run(self) -> int | None:
        rows = self._query("SELECT id FROM sync_run WHERE status = 'completed' ORDER BY id DESC LIMIT 1")
        return int(rows[0]["id"]) if rows else None

    def save_match(self, run_id: int, pair_key: str, match: PairwiseMatch, method: str | None = None) -> None:
        self.save_matches(run_id, [(pair_key, match, method)])

    def save_matches(self, run_id: int, items: Sequence[tuple[str, PairwiseMatch, str | None]]) -> None:
        """Store finished matches in one transaction."""
        with self._tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO pair_match (run_id, ref_clip_id, tgt_clip_id, pair_key, status, confidence, "
                "match_json, offset_s, method) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(run_id, int(m.ref_id), int(m.tgt_id), key, m.status.value, m.confidence,
                  json.dumps(to_jsonable(m)), m.offset_s, method) for key, m, method in items],
            )  # fmt: skip

    def copy_matches(self, run_id: int, keys: Sequence[tuple[str, int, int]]) -> int:
        """Reuse stored matches in a new run: ``(pair_key, ref_clip_id, tgt_clip_id)`` under today's clip ids."""
        n = 0
        with self._tx() as c:
            for key, ref, tgt in keys:
                row = c.execute(
                    "SELECT match_json, status, confidence, offset_s, method FROM pair_match WHERE pair_key = ? "
                    "ORDER BY run_id DESC LIMIT 1",
                    (key,),
                ).fetchone()
                if row is None:
                    continue
                m = json.loads(row["match_json"])
                m["ref_id"], m["tgt_id"] = str(ref), str(tgt)
                c.execute(
                    "INSERT OR REPLACE INTO pair_match (run_id, ref_clip_id, tgt_clip_id, pair_key, status, "
                    "confidence, match_json, offset_s, method) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, ref, tgt, key, row["status"], row["confidence"], json.dumps(m), row["offset_s"],
                     row["method"]),
                )  # fmt: skip
                n += 1
        return n

    def known_pair_keys(self, pair_keys: Sequence[str]) -> set[str]:
        found: set[str] = set()
        keys = list(dict.fromkeys(pair_keys))
        for chunk in _chunks(keys):
            rows = self._query(
                f"SELECT DISTINCT pair_key FROM pair_match WHERE pair_key IN ({','.join('?' * len(chunk))})", chunk
            )
            found.update(r[0] for r in rows)
        return found

    def find_matches(self, pair_keys: Sequence[str]) -> dict[str, PairwiseMatch]:
        """Most recent stored match for each pair key (from any run, finished or not)."""
        found: dict[str, PairwiseMatch] = {}
        keys = list(dict.fromkeys(pair_keys))
        for chunk in _chunks(keys):
            rows = self._query(
                f"SELECT pair_key, match_json FROM pair_match WHERE pair_key IN ({','.join('?' * len(chunk))}) "
                "ORDER BY run_id",
                chunk,
            )
            for r in rows:
                found[r["pair_key"]] = match_from_dict(json.loads(r["match_json"]))
        return found

    def run_matches(self, run_id: int) -> list[PairwiseMatch]:
        rows = self._query("SELECT match_json FROM pair_match WHERE run_id = ? ORDER BY rowid", (run_id,))
        return [match_from_dict(json.loads(r["match_json"])) for r in rows]

    def run_match_stamp(self, run_id: int) -> tuple[int, int]:
        """Changes whenever a run's matches do: their count and newest row (for caching them)."""
        row = self._query("SELECT COUNT(*) AS n, COALESCE(MAX(rowid), 0) AS last FROM pair_match WHERE run_id = ?",
                          (run_id,))[0]  # fmt: skip
        return int(row["n"]), int(row["last"])

    def run_match_summaries(self, run_id: int) -> list[dict]:
        """Matches of a run without their details (fast: no JSON)."""
        rows = self._query(
            "SELECT ref_clip_id, tgt_clip_id, status, confidence, offset_s, method FROM pair_match WHERE run_id = ?",
            (run_id,),
        )
        return [dict(r) for r in rows]

    def clip_matches(self, run_id: int, clip_id: int) -> list[PairwiseMatch]:
        rows = self._query(
            "SELECT match_json FROM pair_match WHERE run_id = ? AND (ref_clip_id = ? OR tgt_clip_id = ?)",
            (run_id, clip_id, clip_id),
        )
        return [match_from_dict(json.loads(r["match_json"])) for r in rows]

    # ------------------------------------------------------------ corrections

    def add_correction(
        self, kind: str, clip_id: int, *, other_clip_id: int | None = None, offset_s: float | None = None
    ) -> int:
        if kind not in CORRECTION_KINDS:
            raise ProjectError(f"unknown correction kind {kind!r}")
        if kind == "offset" and (other_clip_id is None or offset_s is None):
            raise ProjectError("an offset correction needs other_clip_id (the anchor) and offset_s")
        if kind in ("reject_pair", "unreject_pair") and other_clip_id is None:
            raise ProjectError(f"{kind} needs other_clip_id")
        return self.add_corrections([(kind, clip_id, other_clip_id, offset_s)])[0]

    def add_corrections(self, items: Sequence[tuple[str, int, int | None, float | None]]) -> list[int]:
        """Several corrections as one undoable step would need grouping; each stays its own step, in one write."""
        ids = []
        with self._tx() as c:
            c.execute("DELETE FROM correction WHERE undone_at IS NOT NULL")  # a new edit clears the redo stack
            for kind, clip_id, other_clip_id, offset_s in items:
                cur = c.execute(
                    "INSERT INTO correction (kind, clip_id, other_clip_id, offset_s, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (kind, clip_id, other_clip_id, offset_s, _now()),
                )
                ids.append(int(cur.lastrowid))  # type: ignore[arg-type]
        return ids

    def undo(self) -> bool:
        with self._tx() as c:
            row = c.execute("SELECT id FROM correction WHERE undone_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()
            if row is None:
                return False
            c.execute("UPDATE correction SET undone_at = ? WHERE id = ?", (_now(), row["id"]))
            return True

    def redo(self) -> bool:
        with self._tx() as c:
            row = c.execute("SELECT id FROM correction WHERE undone_at IS NOT NULL ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return False
            c.execute("UPDATE correction SET undone_at = NULL WHERE id = ?", (row["id"],))
            return True

    def correction_log(self) -> list[dict]:
        return [dict(r) for r in self._query("SELECT * FROM correction ORDER BY id")]

    def corrections(self) -> ManualCorrections:
        """Replay the active log into the engine's correction set (engine clip ids)."""
        offsets: dict[int, ManualOffset] = {}
        rejected: set[frozenset[str]] = set()
        excluded: set[str] = set()
        for r in self._query("SELECT * FROM correction WHERE undone_at IS NULL ORDER BY id"):
            clip, other = str(r["clip_id"]), str(r["other_clip_id"]) if r["other_clip_id"] is not None else None
            if r["kind"] == "offset":
                offsets[r["clip_id"]] = ManualOffset(clip, other, float(r["offset_s"]))  # type: ignore[arg-type]
            elif r["kind"] == "clear_offset":
                offsets.pop(r["clip_id"], None)
            elif r["kind"] == "reject_pair":
                rejected.add(frozenset((clip, other)))  # type: ignore[arg-type]
            elif r["kind"] == "unreject_pair":
                rejected.discard(frozenset((clip, other)))  # type: ignore[arg-type]
            elif r["kind"] == "exclude":
                excluded.add(clip)
            elif r["kind"] == "include":
                excluded.discard(clip)
        return ManualCorrections(offsets=list(offsets.values()), rejected_pairs=rejected, excluded_clips=excluded)

    # ------------------------------------------------------------- placements

    def save_placements(self, result: SyncResult, analysis_version: str | None = None) -> None:
        """Store the latest solve. Only placements that changed are written: a correction usually moves one group."""
        now = _now()
        version = analysis_version or __version__
        new = {int(cid): p for cid, p in result.placements.items()}
        with self._tx() as c:
            old = self._saved_placements
            if old is None:
                c.execute("DELETE FROM placement")
                changed = list(new.items())
            else:
                gone = [cid for cid in old if cid not in new]
                c.executemany("DELETE FROM placement WHERE clip_id = ?", [(cid,) for cid in gone])
                changed = [(cid, p) for cid, p in new.items() if old.get(cid) != p]
            c.executemany(
                "INSERT OR REPLACE INTO placement (clip_id, placement_json, solved_at, analysis_version) "
                "VALUES (?, ?, ?, ?)",
                [(cid, json.dumps(to_jsonable(p)), now, version) for cid, p in changed],
            )
            self._saved_placements = new

    def placements(self) -> dict[int, ClipPlacement]:
        with self._lock:
            if self._saved_placements is None:
                self._saved_placements = {
                    int(r["clip_id"]): placement_from_dict(json.loads(r["placement_json"]))
                    for r in self._query("SELECT * FROM placement")
                }
            return dict(self._saved_placements)

    # ------------------------------------------------------------ sync points

    def sync_points(self, clip_id: int) -> list[dict]:
        rows = self._query("SELECT id, clip_id, source_s, group_s, note, created_at FROM sync_point "
                           "WHERE clip_id = ? ORDER BY source_s", (clip_id,))  # fmt: skip
        return [dict(r) for r in rows]

    def add_sync_point(self, clip_id: int, source_s: float, group_s: float, note: str | None = None) -> int:
        with self._tx() as c:
            if c.execute("SELECT 1 FROM clip WHERE id = ?", (clip_id,)).fetchone() is None:
                raise ProjectError(f"unknown clip {clip_id}")
            cur = c.execute(
                "INSERT INTO sync_point (clip_id, source_s, group_s, note, created_at) VALUES (?, ?, ?, ?, ?)",
                (clip_id, source_s, group_s, note, _now()),
            )
            return int(cur.lastrowid)  # type: ignore[arg-type]

    def remove_sync_point(self, point_id: int) -> int | None:
        """Delete a sync point; returns its clip (None: no such point)."""
        with self._tx() as c:
            row = c.execute("SELECT clip_id FROM sync_point WHERE id = ?", (point_id,)).fetchone()
            if row is None:
                return None
            c.execute("DELETE FROM sync_point WHERE id = ?", (point_id,))
            return int(row["clip_id"])

    def sync_results(self) -> list[dict]:
        """Every placed clip's synchronisation, one row each (the ``sync_result`` view)."""
        return [dict(r) for r in self._query("SELECT * FROM sync_result ORDER BY clip_id")]

    # ---------------------------------------------------------------- exports

    def record_export(self, fmt: str, path: str, sequence_rate: str, report: dict) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO export (format, path, sequence_rate, created_at, report_json) VALUES (?, ?, ?, ?, ?)",
                (fmt, path, sequence_rate, _now(), json.dumps(report)),
            )
            return int(cur.lastrowid)  # type: ignore[arg-type]

    def exports(self) -> list[dict]:
        rows = self._query("SELECT id, format, path, sequence_rate, created_at FROM export ORDER BY id")
        return [dict(r) for r in rows]

    # --------------------------------------------- transcripts, markers, AI results

    def add_transcript_segments(
        self, rows: Sequence[tuple[int, float, float, str | None, str | None, str, float | None]]
    ):
        """``(clip_id, start_s, end_s, speaker, language, text, confidence)``, in one transaction."""
        with self._tx() as c:
            c.executemany(
                "INSERT INTO transcript_segment (clip_id, start_s, end_s, speaker, language, text, confidence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def set_transcript_state(self, clip_id: int, model: str, language: str, chunks: int) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO transcript_state (clip_id, model, language, chunks, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (clip_id) DO UPDATE SET model = excluded.model, language = excluded.language, "
                "chunks = excluded.chunks, updated_at = excluded.updated_at",
                (clip_id, model, language, chunks, _now()),
            )

    def transcript_states(self) -> dict[int, dict]:
        return {int(r["clip_id"]): dict(r) for r in self._query("SELECT * FROM transcript_state")}

    def replace_transcript_chunk(
        self,
        clip_id: int,
        chunk: int,
        span: tuple[float, float],
        utterances: Sequence[tuple[float, float, str | None, str | None, str, bytes | None]],
        events: Sequence[tuple[float, str]],
    ) -> None:
        """What one transcription task found in ``span`` of a clip: ``(start_s, end_s, speaker, language, text,
        fingerprint)`` utterances and ``(t_s, label)`` sound events. Replaces an earlier run of the same task."""
        with self._tx() as c:
            c.execute("DELETE FROM transcript_segment WHERE clip_id = ? AND chunk = ?", (clip_id, chunk))
            c.execute("DELETE FROM marker WHERE clip_id = ? AND source = 'speech' AND t_s >= ? AND t_s < ?",
                      (clip_id, *span))  # fmt: skip
            c.executemany(
                "INSERT INTO transcript_segment (clip_id, chunk, start_s, end_s, speaker, language, text, "
                "fingerprint) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(clip_id, chunk, *u) for u in utterances],
            )
            c.executemany(
                "INSERT INTO marker (clip_id, t_s, type, label, source, created_at) VALUES (?, ?, ?, ?, 'speech', ?)",
                [(clip_id, t, label.lower(), label, _now()) for t, label in events],
            )

    def clear_transcripts(self, clip_ids: Sequence[int]) -> None:
        with self._tx() as c:
            for chunk in _chunks(list(clip_ids)):
                marks = ",".join("?" * len(chunk))
                c.execute(f"DELETE FROM transcript_segment WHERE clip_id IN ({marks})", chunk)
                c.execute(f"DELETE FROM transcript_state WHERE clip_id IN ({marks})", chunk)
                c.execute(f"DELETE FROM marker WHERE source = 'speech' AND clip_id IN ({marks})", chunk)

    def transcript(self, clip_id: int) -> list[dict]:
        rows = self._query(
            "SELECT id, clip_id, chunk, start_s, end_s, speaker, language, text, confidence "
            "FROM transcript_segment WHERE clip_id = ? ORDER BY start_s",
            (clip_id,),
        )
        return [dict(r) for r in rows]

    def transcript_totals(self) -> dict:
        row = self._query(
            "SELECT COUNT(*) AS segments, COUNT(DISTINCT clip_id) AS clips, COALESCE(SUM(end_s - start_s), 0) AS "
            "speech_s, COUNT(DISTINCT speaker) AS speakers FROM transcript_segment"
        )[0]
        languages = {
            r["language"]: int(r["n"])
            for r in self._query(
                "SELECT language, COUNT(*) AS n FROM transcript_segment WHERE language IS NOT NULL "
                "GROUP BY language ORDER BY n DESC"
            )
        }
        return {**dict(row), "languages": languages}

    def segments_without_speaker(self) -> list[tuple[int, bytes]]:
        return [
            (int(r["id"]), r["fingerprint"])
            for r in self._query(
                "SELECT id, fingerprint FROM transcript_segment WHERE speaker IS NULL AND fingerprint IS NOT NULL "
                "ORDER BY clip_id, start_s"
            )
        ]

    def set_segment_speakers(self, rows: Sequence[tuple[str, int]]) -> None:
        """``(speaker_key, segment_id)``."""
        with self._tx() as c:
            c.executemany("UPDATE transcript_segment SET speaker = ? WHERE id = ?", rows)

    def speakers(self) -> list[dict]:
        rows = self._query(
            "SELECT speaker.key, speaker.name, speaker.centroid, speaker.utterances, "
            "(SELECT COUNT(*) FROM transcript_segment s WHERE s.speaker = speaker.key) AS segments, "
            "(SELECT COALESCE(SUM(end_s - start_s), 0) FROM transcript_segment s WHERE s.speaker = speaker.key) "
            "AS speech_s FROM speaker ORDER BY speaker.key"
        )
        return [dict(r) for r in rows]

    def speaker_segments(self, key: str, limit: int = 50) -> list[dict]:
        rows = self._query(
            "SELECT id, clip_id, start_s, end_s, speaker, language, text FROM transcript_segment WHERE speaker = ? "
            "ORDER BY end_s - start_s DESC LIMIT ?",
            (key, limit),
        )
        return [dict(r) for r in rows]

    def save_speakers(self, rows: Sequence[tuple[str, bytes, int]]) -> None:
        """``(key, centroid, utterances)``: voices learned so far (names are kept)."""
        with self._tx() as c:
            c.executemany(
                "INSERT INTO speaker (key, centroid, utterances) VALUES (?, ?, ?) ON CONFLICT (key) DO UPDATE SET "
                "centroid = excluded.centroid, utterances = excluded.utterances",
                rows,
            )

    def rename_speaker(self, key: str, name: str | None) -> None:
        with self._tx() as c:
            if c.execute("UPDATE speaker SET name = ? WHERE key = ?", (name or None, key)).rowcount == 0:
                raise ProjectError(f"no speaker {key}")

    def merge_speakers(self, absorbed: str, into: str, centroid: bytes | None = None, utterances: int | None = None):
        """Every utterance of ``absorbed`` becomes ``into``'s; ``absorbed`` keeps no name or segments."""
        with self._tx() as c:
            c.execute("UPDATE transcript_segment SET speaker = ? WHERE speaker = ?", (into, absorbed))
            name = c.execute("SELECT name FROM speaker WHERE key = ?", (absorbed,)).fetchone()
            c.execute("DELETE FROM speaker WHERE key = ?", (absorbed,))
            if name and name[0]:
                c.execute("UPDATE speaker SET name = COALESCE(name, ?) WHERE key = ?", (name[0], into))
            if centroid is not None:
                c.execute("UPDATE speaker SET centroid = ?, utterances = ? WHERE key = ?", (centroid, utterances, into))

    def set_ai_analysis(self, clip_id: int, analysis_type: str, result: dict, confidence: float | None,
                        status: str) -> None:  # fmt: skip
        """The latest result of one kind of AI analysis of a clip (replaces the previous one)."""
        with self._tx() as c:
            c.execute("DELETE FROM ai_analysis WHERE clip_id = ? AND analysis_type = ?", (clip_id, analysis_type))
            c.execute(
                "INSERT INTO ai_analysis (clip_id, analysis_type, result_json, confidence, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (clip_id, analysis_type, json.dumps(result), confidence, status, _now()),
            )

    def ai_analyses(self, analysis_type: str) -> dict[int, dict]:
        rows = self._query("SELECT * FROM ai_analysis WHERE analysis_type = ?", (analysis_type,))
        return {
            int(r["clip_id"]): {"result": json.loads(r["result_json"]) if r["result_json"] else None,
                                "confidence": r["confidence"], "status": r["status"], "created_at": r["created_at"]}
            for r in rows
        }  # fmt: skip

    def set_ai_status(self, clip_id: int, analysis_type: str, status: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE ai_analysis SET status = ? WHERE clip_id = ? AND analysis_type = ?",
                      (status, clip_id, analysis_type))  # fmt: skip

    def add_markers(self, rows: Sequence[tuple[int, float, str, str | None, float | None]]) -> None:
        """``(clip_id, t_s, type, label, confidence)``."""
        with self._tx() as c:
            c.executemany("INSERT INTO marker (clip_id, t_s, type, label, confidence) VALUES (?, ?, ?, ?, ?)", rows)

    def add_marker(self, clip_id: int, t_s: float, label: str | None, *, marker_type: str = "user",
                   source: str = "user") -> int:  # fmt: skip
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO marker (clip_id, t_s, type, label, source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (clip_id, t_s, marker_type, label, source, _now()),
            )
            return int(cur.lastrowid)  # type: ignore[arg-type]

    def update_marker(self, marker_id: int, label: str | None) -> None:
        with self._tx() as c:
            if c.execute("UPDATE marker SET label = ? WHERE id = ?", (label, marker_id)).rowcount == 0:
                raise ProjectError(f"no marker {marker_id}")

    def delete_marker(self, marker_id: int) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM marker WHERE id = ?", (marker_id,))

    def markers(self, clip_id: int | None = None, marker_type: str | None = None) -> list[dict]:
        where, args = [], []
        if clip_id is not None:
            where.append("clip_id = ?")
            args.append(clip_id)
        if marker_type is not None:
            where.append("type = ?")
            args.append(marker_type)
        sql = "SELECT * FROM marker" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY clip_id, t_s"
        return [dict(r) for r in self._query(sql, args)]

    def search_transcripts(self, text: str, limit: int = 100) -> list[dict]:
        """Utterances matching ``text``: the phrase first, then all its words, then any of them (FTS5 when this
        SQLite has it, else LIKE)."""
        cols = "s.id, s.clip_id, s.start_s, s.end_s, s.speaker, s.language, s.text, s.confidence"
        words = [w for w in re.findall(r"\w+", text.lower()) if w]
        if not words:
            return []
        if has_fts(self._conn):
            found: dict[int, dict] = {}
            quoted = ['"' + w.replace('"', '""') + '"' for w in words]
            queries = [('"' + " ".join(words) + '"', "phrase"), (" AND ".join(quoted), "all words")]
            if len(words) > 1:
                queries.append((" OR ".join(quoted), "some words"))
            for query, how in queries:
                rows = self._query(
                    f"SELECT {cols}, bm25(transcript_fts) AS rank FROM transcript_fts JOIN transcript_segment s "
                    "ON s.id = transcript_fts.rowid WHERE transcript_fts MATCH ? ORDER BY rank LIMIT ?",
                    (query, limit),
                )
                for r in rows:
                    found.setdefault(int(r["id"]), {**dict(r), "match": how})
                if len(found) >= limit:
                    break
            return list(found.values())[:limit]
        rows = self._query(
            f"SELECT {cols} FROM transcript_segment s WHERE lower(s.text) LIKE ? ORDER BY s.clip_id, s.start_s LIMIT ?",
            (f"%{' '.join(words)}%", limit),
        )
        return [{**dict(r), "match": "phrase"} for r in rows]

    # ------------------------------------------------------------------ search

    def media_rows(self) -> list[dict]:
        """Compact, indexed columns of every clip (no JSON parsing): what lists and filters need."""
        rows = self._query(
            "SELECT clip.id AS clip_id, clip.name, clip.device_id, clip.session_id, clip.audio_stream, "
            "clip.chapter_take, clip.chapter_index, media_file.id AS media_id, media_file.path, media_file.kind, "
            "media_file.duration_s, media_file.fps, media_file.width, media_file.height, media_file.codec, "
            "media_file.sample_rate, media_file.channels, media_file.timecode, media_file.creation_time, "
            "media_file.size_bytes, media_file.status, media_file.duplicate_of, media_file.duplicate_decision, "
            "media_file.duplicate_reason, device.name AS device_name, device.kind AS device_kind "
            "FROM clip JOIN media_file ON media_file.id = clip.media_id LEFT JOIN device ON device.id = clip.device_id "
            "ORDER BY clip.id"
        )
        return [dict(r) for r in rows]

    def database_stats(self) -> dict:
        page = self._query("PRAGMA page_count")[0][0] * self._query("PRAGMA page_size")[0][0]
        tables = ("media_file", "clip", "task", "pair_match", "transcript_segment", "marker", "audio_analysis")
        counts = {t: int(self._query(f"SELECT COUNT(*) FROM {t}")[0][0]) for t in tables}
        wal = Path(str(self.path) + "-wal")
        return {"bytes": page, "wal_bytes": wal.stat().st_size if wal.exists() else 0, "rows": counts}

"""The project file: one SQLite database per project.

The engine is its only writer. One connection is shared by the RPC thread and
job threads behind a lock; SQLite runs in WAL mode so a crash never corrupts
it, and every multi-row change is a transaction.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

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

SCHEMA_VERSION = 1
PROJECT_SUFFIX = ".mcsync"
CORRECTION_KINDS = ("offset", "clear_offset", "reject_pair", "unreject_pair", "exclude", "include")


class ProjectError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


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

    @property
    def engine_id(self) -> str:
        """The clip's id inside the sync engine."""
        return str(self.id)

    @property
    def audio_stream_info(self) -> AudioStreamInfo | None:
        return next((a for a in self.info.audio if a.index == self.audio_stream), None)

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


class Project:
    def __init__(self, path: Path, conn: sqlite3.Connection) -> None:
        self.path = path
        self._conn = conn
        self._lock = threading.RLock()

    # ------------------------------------------------------------ lifecycle

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
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
            conn.executescript(f"BEGIN;\n{schema}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;")
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
        return project

    def _migrate(self, version: int) -> None:
        """Apply schema migrations in order (none yet: schema 1 is the first)."""
        assert version == SCHEMA_VERSION

    def close(self) -> None:
        with self._lock:
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
        return [dict(r) for r in self._query("SELECT * FROM device ORDER BY kind != 'recorder', name")]

    def update_device(self, device_id: int, *, name: str | None = None, kind: str | None = None) -> None:
        with self._tx() as c:
            if name is not None:
                c.execute("UPDATE device SET name = ? WHERE id = ?", (name, device_id))
            if kind is not None:
                c.execute("UPDATE device SET kind = ? WHERE id = ?", (kind, device_id))

    # --------------------------------------------------------- media, clips

    def add_media(self, items: Sequence[MediaItem]) -> list[int]:
        """Add (or refresh) scanned media; returns the clip id of each item."""
        ids = []
        base = self.path.parent
        with self._tx() as c:
            for it in items:
                info_json = json.dumps(media_info_to_dict(it.info))
                try:
                    rel = os.path.relpath(it.path, base)
                except ValueError:  # another drive on Windows
                    rel = None
                existing = c.execute("SELECT id, fingerprint FROM media_file WHERE path = ?", (it.path,)).fetchone()
                if existing:
                    media_id = int(existing["id"])
                    c.execute(
                        "UPDATE media_file SET size_bytes = ?, mtime_ns = ?, fingerprint = ?, info_json = ?, "
                        "rel_path = ?, status = 'online' WHERE id = ?",
                        (it.info.size_bytes, it.info.mtime_ns, it.fingerprint, info_json, rel, media_id),
                    )
                else:
                    cur = c.execute(
                        "INSERT INTO media_file (path, rel_path, size_bytes, mtime_ns, fingerprint, info_json, "
                        "added_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (it.path, rel, it.info.size_bytes, it.info.mtime_ns, it.fingerprint, info_json, _now()),
                    )
                    media_id = int(cur.lastrowid)  # type: ignore[arg-type]
                device_id = self._upsert_device(c, it.device)
                chapter = it.chapter or (None, None)
                clip = c.execute("SELECT id FROM clip WHERE media_id = ?", (media_id,)).fetchone()
                values = (
                    device_id,
                    Path(it.path).name,
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
        return ids

    _CLIP_SQL = """
        SELECT clip.*, media_file.path, media_file.fingerprint, media_file.status, media_file.info_json,
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
        )

    def clips(self) -> list[ClipRow]:
        return [self._clip_row(r) for r in self._query(self._CLIP_SQL + " ORDER BY clip.id")]

    def clip(self, clip_id: int) -> ClipRow:
        rows = self._query(self._CLIP_SQL + " WHERE clip.id = ?", (clip_id,))
        if not rows:
            raise ProjectError(f"no clip {clip_id}")
        return self._clip_row(rows[0])

    def set_clip_device(self, clip_id: int, device_id: int) -> None:
        with self._tx() as c:
            c.execute("UPDATE clip SET device_id = ? WHERE id = ?", (device_id, clip_id))

    def set_clip_audio(self, clip_id: int, stream_index: int | None, channel: int | None) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE clip SET audio_stream = ?, audio_channel = ? WHERE id = ?", (stream_index, channel, clip_id)
            )

    def remove_clips(self, clip_ids: Sequence[int]) -> None:
        with self._tx() as c:
            for cid in clip_ids:
                row = c.execute("SELECT media_id FROM clip WHERE id = ?", (cid,)).fetchone()
                if row:
                    c.execute("DELETE FROM media_file WHERE id = ?", (row["media_id"],))
            c.execute("DELETE FROM device WHERE id NOT IN (SELECT device_id FROM clip WHERE device_id IS NOT NULL)")

    def _refresh_media_status(self) -> None:
        """Mark media that went missing (unplugged drive) or changed since import."""
        with self._tx() as c:
            for r in c.execute("SELECT id, path, size_bytes, mtime_ns FROM media_file").fetchall():
                try:
                    st = os.stat(r["path"])
                except OSError:
                    status = "offline"
                else:
                    status = "online" if (st.st_size, st.st_mtime_ns) == (r["size_bytes"], r["mtime_ns"]) else "changed"
                c.execute("UPDATE media_file SET status = ? WHERE id = ?", (status, r["id"]))

    # -------------------------------------------------------- runs, matches

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

    def last_completed_run(self) -> int | None:
        rows = self._query("SELECT id FROM sync_run WHERE status = 'completed' ORDER BY id DESC LIMIT 1")
        return int(rows[0]["id"]) if rows else None

    def save_match(self, run_id: int, pair_key: str, match: PairwiseMatch) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO pair_match (run_id, ref_clip_id, tgt_clip_id, pair_key, status, confidence, "
                "match_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    int(match.ref_id),
                    int(match.tgt_id),
                    pair_key,
                    match.status.value,
                    match.confidence,
                    json.dumps(to_jsonable(match)),
                ),
            )

    def find_matches(self, pair_keys: Sequence[str]) -> dict[str, PairwiseMatch]:
        """Most recent stored match for each pair key (from any run, finished or not)."""
        found: dict[str, PairwiseMatch] = {}
        keys = list(dict.fromkeys(pair_keys))
        for start in range(0, len(keys), 500):
            chunk = keys[start : start + 500]
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

    # ---------------------------------------------------------- corrections

    def add_correction(
        self, kind: str, clip_id: int, *, other_clip_id: int | None = None, offset_s: float | None = None
    ) -> int:
        if kind not in CORRECTION_KINDS:
            raise ProjectError(f"unknown correction kind {kind!r}")
        if kind == "offset" and (other_clip_id is None or offset_s is None):
            raise ProjectError("an offset correction needs other_clip_id (the anchor) and offset_s")
        if kind in ("reject_pair", "unreject_pair") and other_clip_id is None:
            raise ProjectError(f"{kind} needs other_clip_id")
        with self._tx() as c:
            c.execute("DELETE FROM correction WHERE undone_at IS NOT NULL")  # a new edit clears the redo stack
            cur = c.execute(
                "INSERT INTO correction (kind, clip_id, other_clip_id, offset_s, created_at) VALUES (?, ?, ?, ?, ?)",
                (kind, clip_id, other_clip_id, offset_s, _now()),
            )
            return int(cur.lastrowid)  # type: ignore[arg-type]

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

    # ----------------------------------------------------------- placements

    def save_placements(self, result: SyncResult) -> None:
        now = _now()
        with self._tx() as c:
            c.execute("DELETE FROM placement")
            c.executemany(
                "INSERT INTO placement (clip_id, placement_json, solved_at) VALUES (?, ?, ?)",
                [(int(cid), json.dumps(to_jsonable(p)), now) for cid, p in result.placements.items()],
            )

    def placements(self) -> dict[int, ClipPlacement]:
        return {
            int(r["clip_id"]): placement_from_dict(json.loads(r["placement_json"]))
            for r in self._query("SELECT * FROM placement")
        }

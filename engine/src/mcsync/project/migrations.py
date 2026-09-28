"""Schema migrations for project files.

Each migration takes a project from version ``n - 1`` to ``n`` inside one transaction. Project files from older
releases are upgraded when opened; nothing is lost.

Version 2 (Syncora 1.1) is built for productions with thousands of files:

* media metadata gets indexed columns (name, kind, duration, rate, size, codec, times) so search and filters run
  in SQL instead of parsing every file's JSON, and the raw ffprobe output moves to its own table;
* ``discovered`` holds files found on disk before they are probed, so imports are incremental and resumable;
* ``task`` is the persistent job queue (probe, analyze, match…) with priorities and per-file status, so work
  survives pauses, crashes and restarts;
* ``audio_analysis`` records each clip's analysis products (the files themselves live in the cache);
* ``session`` groups clips by recording session;
* ``transcript_segment``, ``marker`` and ``ai_analysis`` are reserved for analysis results (with the indexes their
  queries need), and ``meta`` holds pipeline state.

Version 3 (Syncora 1.2) fills them: transcripts are written per transcription task (``chunk``), each utterance
keeps its voice fingerprint, ``speaker`` holds the project's speakers (with the names the user gives them),
``transcript_state`` which model and language a clip was transcribed with, and markers record where they come from.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import PurePath

V2_SQL = """
ALTER TABLE media_file ADD COLUMN filename TEXT;
ALTER TABLE media_file ADD COLUMN kind TEXT;                 -- 'video' | 'audio'
ALTER TABLE media_file ADD COLUMN duration_s REAL;
ALTER TABLE media_file ADD COLUMN fps TEXT;                  -- "30000/1001"
ALTER TABLE media_file ADD COLUMN width INTEGER;
ALTER TABLE media_file ADD COLUMN height INTEGER;
ALTER TABLE media_file ADD COLUMN codec TEXT;
ALTER TABLE media_file ADD COLUMN sample_rate INTEGER;
ALTER TABLE media_file ADD COLUMN channels INTEGER;
ALTER TABLE media_file ADD COLUMN timecode TEXT;
ALTER TABLE media_file ADD COLUMN creation_time TEXT;         -- ISO 8601
ALTER TABLE media_file ADD COLUMN duplicate_of INTEGER REFERENCES media_file (id) ON DELETE SET NULL;
ALTER TABLE media_file ADD COLUMN duplicate_decision TEXT;   -- NULL (undecided) | 'keep' | 'ignore'
ALTER TABLE media_file ADD COLUMN duplicate_reason TEXT;     -- identical | probable
CREATE INDEX media_file_filename ON media_file (filename);
CREATE INDEX media_file_kind ON media_file (kind);
CREATE INDEX media_file_creation ON media_file (creation_time);
CREATE INDEX media_file_status ON media_file (status);
CREATE INDEX media_file_duplicate ON media_file (duplicate_of);

CREATE TABLE media_probe (                -- raw ffprobe output, for diagnostics only
    media_id INTEGER PRIMARY KEY REFERENCES media_file (id) ON DELETE CASCADE,
    raw_json TEXT NOT NULL
);

CREATE TABLE import_root (                -- folders and files the user imported, for rescans
    id       INTEGER PRIMARY KEY,
    path     TEXT NOT NULL UNIQUE,
    added_at TEXT NOT NULL
);

CREATE TABLE discovered (                 -- files found on disk, before and after probing
    id             INTEGER PRIMARY KEY,
    path           TEXT NOT NULL UNIQUE,
    size_bytes     INTEGER NOT NULL,
    mtime_ns       INTEGER NOT NULL,
    kind_guess     TEXT,                  -- from the extension: 'video' | 'audio' | NULL
    import_root_id INTEGER REFERENCES import_root (id) ON DELETE SET NULL,
    media_id       INTEGER REFERENCES media_file (id) ON DELETE SET NULL,
    status         TEXT NOT NULL DEFAULT 'pending',   -- pending | done | failed | skipped
    error          TEXT,
    discovered_at  TEXT NOT NULL
);
CREATE INDEX discovered_status ON discovered (status);

CREATE TABLE task (                       -- the persistent job queue
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,          -- probe | analyze | match | ...
    target        TEXT NOT NULL,          -- what it works on, unique per kind (a path id, clip id or pair key)
    clip_id       INTEGER,
    other_clip_id INTEGER,
    stage         TEXT,                   -- for match tasks: fingerprint | fallback
    priority      INTEGER NOT NULL DEFAULT 5,   -- 0 = most urgent
    status        TEXT NOT NULL DEFAULT 'pending',
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    detail_json   TEXT,
    run_id        INTEGER,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (kind, target)
);
CREATE INDEX task_queue ON task (status, priority, id);
CREATE INDEX task_kind_status ON task (kind, status);
CREATE INDEX task_clip ON task (clip_id);

CREATE TABLE audio_analysis (
    clip_id     INTEGER PRIMARY KEY REFERENCES clip (id) ON DELETE CASCADE,
    cache_key   TEXT NOT NULL,            -- cache entry holding the signal, waveform and fingerprint
    rate        INTEGER,
    samples     INTEGER,
    level_dbfs  REAL,
    n_hashes    INTEGER,                  -- audio fingerprint size
    status      TEXT NOT NULL,            -- done | failed | skipped
    updated_at  TEXT NOT NULL
);
CREATE INDEX audio_analysis_status ON audio_analysis (status);

CREATE TABLE session (                    -- recording sessions (from sync groups and clocks)
    id       INTEGER PRIMARY KEY,
    label    TEXT NOT NULL,
    start_at TEXT,                        -- ISO wall-clock start, when clocks tell
    end_at   TEXT,
    group_no INTEGER,                     -- sync group (timeline group) it came from
    source   TEXT NOT NULL DEFAULT 'auto' -- auto | manual
);
ALTER TABLE clip ADD COLUMN session_id INTEGER REFERENCES session (id) ON DELETE SET NULL;
CREATE INDEX clip_session ON clip (session_id);
CREATE INDEX clip_device ON clip (device_id);

ALTER TABLE pair_match ADD COLUMN offset_s REAL;
ALTER TABLE pair_match ADD COLUMN method TEXT;               -- fingerprint | fallback | full
CREATE INDEX pair_match_ref ON pair_match (run_id, ref_clip_id);
CREATE INDEX pair_match_tgt ON pair_match (run_id, tgt_clip_id);

CREATE TABLE ai_analysis (                -- reserved for model-based analysis results
    id            INTEGER PRIMARY KEY,
    clip_id       INTEGER NOT NULL REFERENCES clip (id) ON DELETE CASCADE,
    analysis_type TEXT NOT NULL,
    result_json   TEXT,
    confidence    REAL,
    status        TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX ai_analysis_clip ON ai_analysis (clip_id, analysis_type);

CREATE TABLE transcript_segment (
    id         INTEGER PRIMARY KEY,
    clip_id    INTEGER NOT NULL REFERENCES clip (id) ON DELETE CASCADE,
    start_s    REAL NOT NULL,
    end_s      REAL NOT NULL,
    speaker    TEXT,
    language   TEXT,
    text       TEXT NOT NULL,
    confidence REAL
);
CREATE INDEX transcript_clip_time ON transcript_segment (clip_id, start_s);
CREATE INDEX transcript_speaker ON transcript_segment (speaker);

CREATE TABLE marker (
    id         INTEGER PRIMARY KEY,
    clip_id    INTEGER NOT NULL REFERENCES clip (id) ON DELETE CASCADE,
    t_s        REAL NOT NULL,
    type       TEXT NOT NULL,
    label      TEXT,
    confidence REAL
);
CREATE INDEX marker_clip_time ON marker (clip_id, t_s);
CREATE INDEX marker_type ON marker (type);

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# Full-text search over transcripts, when this SQLite has FTS5 (the builds Python ships with do).
FTS_STATEMENTS = (
    "CREATE VIRTUAL TABLE transcript_fts USING fts5(text, content='transcript_segment', content_rowid='id')",
    """CREATE TRIGGER transcript_ai AFTER INSERT ON transcript_segment BEGIN
        INSERT INTO transcript_fts (rowid, text) VALUES (new.id, new.text);
    END""",
    """CREATE TRIGGER transcript_ad AFTER DELETE ON transcript_segment BEGIN
        INSERT INTO transcript_fts (transcript_fts, rowid, text) VALUES ('delete', old.id, old.text);
    END""",
    """CREATE TRIGGER transcript_au AFTER UPDATE ON transcript_segment BEGIN
        INSERT INTO transcript_fts (transcript_fts, rowid, text) VALUES ('delete', old.id, old.text);
        INSERT INTO transcript_fts (rowid, text) VALUES (new.id, new.text);
    END""",
)


def media_columns(info: dict, path: str) -> dict:
    """Indexed columns of a media file, from its metadata as stored (``media_info_to_dict``)."""
    video = (info.get("video") or [None])[0]
    audio = (info.get("audio") or [None])[0]
    tc = info.get("timecode")
    return {
        "filename": PurePath(path.replace("\\", "/")).name,
        "kind": "video" if video else "audio",
        "duration_s": info.get("duration_s"),
        "fps": video["frame_rate"] if video else None,
        "width": video.get("width") if video else None,
        "height": video.get("height") if video else None,
        "codec": (video or audio or {}).get("codec"),
        "sample_rate": audio.get("sample_rate") if audio else None,
        "channels": audio.get("channels") if audio else None,
        "timecode": tc.get("text") if tc else None,
        "creation_time": info.get("creation_time"),
    }


def _migrate_v2(conn: sqlite3.Connection) -> None:
    for statement in V2_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)
    try:
        conn.execute("SAVEPOINT fts")
        for statement in FTS_STATEMENTS:
            conn.execute(statement)
        conn.execute("RELEASE fts")
    except sqlite3.OperationalError:  # no FTS5: transcript search falls back to LIKE
        conn.execute("ROLLBACK TO fts")
        conn.execute("RELEASE fts")
    rows = conn.execute("SELECT id, path, info_json FROM media_file").fetchall()
    for media_id, path, info_json in rows:
        info = json.loads(info_json)
        raw = info.pop("raw", None)
        if raw:
            conn.execute("INSERT INTO media_probe (media_id, raw_json) VALUES (?, ?)", (media_id, json.dumps(raw)))
        cols = media_columns(info, path)
        conn.execute(
            f"UPDATE media_file SET info_json = ?, {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?",
            (json.dumps(info), *cols.values(), media_id),
        )


V3_SQL = """
ALTER TABLE transcript_segment ADD COLUMN chunk INTEGER NOT NULL DEFAULT 0;   -- the transcription task that wrote it
ALTER TABLE transcript_segment ADD COLUMN fingerprint BLOB;                   -- voice fingerprint, float32
CREATE INDEX transcript_clip_chunk ON transcript_segment (clip_id, chunk);
CREATE TABLE speaker (
    key        TEXT PRIMARY KEY,                  -- S01, S02 …
    name       TEXT,                              -- given by the user
    centroid   BLOB NOT NULL,                     -- mean voice fingerprint, float32
    utterances INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE transcript_state (
    clip_id    INTEGER PRIMARY KEY REFERENCES clip (id) ON DELETE CASCADE,
    model      TEXT NOT NULL,
    language   TEXT NOT NULL,                     -- requested: 'auto' or a language code
    chunks     INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
ALTER TABLE marker ADD COLUMN source TEXT NOT NULL DEFAULT 'user';            -- user | speech | search | ai
ALTER TABLE marker ADD COLUMN created_at TEXT
"""


def _migrate_v3(conn: sqlite3.Connection) -> None:
    for statement in V3_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)


# Version 4 (Syncora 1.3): sync points a user sets on a clip (a position in the clip and where it lands on its
# sync group's timeline; two or more of them measure the clip's drift), the analysis version of each placement,
# and ``sync_result``: every clip's synchronisation in one row (placement, evidence, manual adjustment, sync points).
V4_SQL = """
CREATE TABLE sync_point (
    id         INTEGER PRIMARY KEY,
    clip_id    INTEGER NOT NULL REFERENCES clip (id) ON DELETE CASCADE,
    source_s   REAL NOT NULL,                 -- position in the clip
    group_s    REAL NOT NULL,                 -- where it lands on the clip's sync group timeline
    note       TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX sync_point_clip ON sync_point (clip_id);
ALTER TABLE placement ADD COLUMN analysis_version TEXT;
CREATE VIEW sync_result AS
SELECT
    c.media_id                                              AS media_id,
    c.id                                                    AS clip_id,
    c.device_id                                             AS camera_id,
    c.session_id                                            AS session_id,
    json_extract(p.placement_json, '$.group')               AS sync_group_id,
    json_extract(pr.settings_json, '$.reference_clip_id')   AS master_reference_id,
    json_extract(p.placement_json, '$.start_s')             AS offset,
    json_extract(p.placement_json, '$.drift_ppm')           AS drift,
    json_extract(p.placement_json, '$.method')              AS method,
    json_extract(p.placement_json, '$.confidence')          AS confidence,
    json_object('flags', json_extract(p.placement_json, '$.flags'),
                'corroboration', json_extract(p.placement_json, '$.corroboration')) AS evidence,
    json_extract(p.placement_json, '$.status')              AS status,
    (SELECT k.offset_s FROM correction k WHERE k.clip_id = c.id AND k.kind = 'offset' AND k.undone_at IS NULL
     ORDER BY k.id DESC LIMIT 1)                            AS manual_adjustment,
    (SELECT COUNT(*) FROM sync_point s WHERE s.clip_id = c.id) AS sync_points,
    p.analysis_version                                      AS analysis_version,
    p.solved_at                                             AS updated_at
FROM clip c
JOIN placement p ON p.clip_id = c.id
CROSS JOIN project pr
"""


def _migrate_v4(conn: sqlite3.Connection) -> None:
    for statement in V4_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {2: _migrate_v2, 3: _migrate_v3, 4: _migrate_v4}
LATEST = max(MIGRATIONS)


def has_fts(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'transcript_fts'").fetchone() is not None

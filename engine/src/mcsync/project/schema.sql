-- Multicam Sync project file, schema version 1.
-- The engine is the only writer (see docs/ARCHITECTURE.md, decision D2).

CREATE TABLE project (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    name           TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    settings_json  TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE device (
    id     INTEGER PRIMARY KEY,
    key    TEXT NOT NULL UNIQUE,          -- identity from media/devices.py
    name   TEXT NOT NULL,
    kind   TEXT NOT NULL CHECK (kind IN ('camera', 'recorder', 'phone', 'drone', 'other')),
    make   TEXT,
    model  TEXT,
    serial TEXT,
    color  TEXT
);

CREATE TABLE media_file (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,     -- absolute
    rel_path    TEXT,                     -- relative to the project file, for relinking
    size_bytes  INTEGER NOT NULL,
    mtime_ns    INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    info_json   TEXT NOT NULL,            -- parsed metadata including raw ffprobe output
    status      TEXT NOT NULL DEFAULT 'online' CHECK (status IN ('online', 'offline', 'changed')),
    added_at    TEXT NOT NULL
);
CREATE INDEX media_file_fingerprint ON media_file (fingerprint);

CREATE TABLE clip (                       -- the unit the sync engine and the timeline see
    id               INTEGER PRIMARY KEY,
    media_id         INTEGER NOT NULL UNIQUE REFERENCES media_file (id) ON DELETE CASCADE,
    device_id        INTEGER REFERENCES device (id) ON DELETE SET NULL,
    name             TEXT NOT NULL,
    audio_stream     INTEGER,             -- ffprobe stream index; NULL = no audio
    audio_channel    INTEGER,             -- NULL = average of all channels
    chapter_take     TEXT,                -- set when the file is one chapter of a longer take
    chapter_index    INTEGER,
    chapter_offset_s REAL
);

CREATE TABLE sync_run (
    id             INTEGER PRIMARY KEY,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL CHECK (status IN ('running', 'completed', 'cancelled', 'failed')),
    params_json    TEXT NOT NULL,
    engine_version TEXT NOT NULL
);

CREATE TABLE pair_match (                 -- written as pairs finish, so runs resume after a crash
    run_id      INTEGER NOT NULL REFERENCES sync_run (id) ON DELETE CASCADE,
    ref_clip_id INTEGER NOT NULL REFERENCES clip (id) ON DELETE CASCADE,
    tgt_clip_id INTEGER NOT NULL REFERENCES clip (id) ON DELETE CASCADE,
    pair_key    TEXT NOT NULL,            -- hash of both signals, the search window and the parameters
    status      TEXT NOT NULL,
    confidence  REAL NOT NULL,
    match_json  TEXT NOT NULL,
    PRIMARY KEY (run_id, ref_clip_id, tgt_clip_id)
);
CREATE INDEX pair_match_key ON pair_match (pair_key);

CREATE TABLE correction (                 -- append-only log; undo sets undone_at
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('offset', 'clear_offset', 'reject_pair', 'unreject_pair', 'exclude', 'include')),
    clip_id       INTEGER NOT NULL REFERENCES clip (id) ON DELETE CASCADE,
    other_clip_id INTEGER REFERENCES clip (id) ON DELETE CASCADE,
    offset_s      REAL,
    created_at    TEXT NOT NULL,
    undone_at     TEXT
);

CREATE TABLE placement (                  -- latest solve
    clip_id        INTEGER PRIMARY KEY REFERENCES clip (id) ON DELETE CASCADE,
    placement_json TEXT NOT NULL,
    solved_at      TEXT NOT NULL
);

CREATE TABLE export (
    id            INTEGER PRIMARY KEY,
    format        TEXT NOT NULL,
    path          TEXT NOT NULL,
    sequence_rate TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    report_json   TEXT NOT NULL
);

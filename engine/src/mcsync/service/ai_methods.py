"""Engine methods of the AI features: models, transcription, transcripts, speakers, markers and search.

Mixed into :class:`mcsync.service.app.EngineService` (which provides the project, pipeline, cache, jobs and
notifications).
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from mcsync.ai.models import ALL_MODELS, DEFAULT_SPEECH_MODEL, SPEECH_MODELS, ModelStore
from mcsync.ai.speech import LANGUAGES, Transcriber
from mcsync.project.db import ClipRow, ProjectError
from mcsync.resources import cpu_usable

if TYPE_CHECKING:
    from mcsync.media.cache import AnalysisCache
    from mcsync.pipeline.runner import Pipeline
    from mcsync.project.db import Project
    from mcsync.service.jobs import Job, JobManager

#: Search scores: an utterance with the whole phrase, with all its words, or with some of them.
_MATCH_SCORE = {"phrase": 1.0, "all words": 0.9, "some words": 0.6}
#: A clip covering less new time than this is not transcribed again by "smart" scope (its moments already are).
_MIN_NEW_S = 10.0


def speech_engine_available() -> bool:
    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        return False
    return True


class AiMethods:
    project: Project | None
    cache: AnalysisCache
    jobs: JobManager
    notify: Any

    def _ai_init(self) -> None:
        self.models = ModelStore(self.cache.root / "models")
        self._transcribers: dict[tuple[str, str], Transcriber] = {}
        self._transcriber_lock = threading.Lock()

    # Provided by EngineService.
    def _require_project(self) -> Project: ...  # pragma: no cover
    def _pipeline(self) -> Pipeline: ...  # pragma: no cover
    def _settings(self) -> dict: ...  # pragma: no cover

    def transcriber(self, model: str, language: str) -> Transcriber:
        """The loaded speech models for ``model`` and ``language`` (one set in memory at a time)."""
        key = (model, language or "auto")
        with self._transcriber_lock:
            found = self._transcribers.get(key)
            if found is None:
                self._transcribers.clear()
                found = Transcriber(self.models, model, language, threads=max(2, min(8, cpu_usable() // 2)))
                self._transcribers[key] = found
            return found

    # ------------------------------------------------------------------ models

    def ai_status(self) -> dict:
        """What the AI features can do on this computer: the speech engine, the models and the languages."""
        models = self.models.status()
        installed = {m["id"] for m in models if m["installed"]}
        engine = speech_engine_available()
        missing = [m for m in ("silero-vad", "voice-eres2net") if m not in installed]
        return {
            "speech_engine": engine,
            "ready": engine and not missing and any(m.id in installed for m in SPEECH_MODELS),
            "problem": None if engine and not missing else
            ("The speech engine is not installed" if not engine else f"Model not installed: {', '.join(missing)}"),
            "models": models,
            "default_model": DEFAULT_SPEECH_MODEL,
            "languages": [{"code": c, "name": n} for c, n in sorted(LANGUAGES.items(), key=lambda kv: kv[1])],
        }  # fmt: skip

    def ai_download_model(self, model_id: str) -> dict:
        if model_id not in ALL_MODELS:
            raise ValueError(f"unknown model {model_id}")

        def work(job: Job) -> dict:
            def progress(received: int, total: int) -> None:
                job.report(received / total if total else 0.0,
                           f"{received / 1e6:.0f} of {total / 1e6:.0f} MB")  # fmt: skip

            path = self.models.download(model_id, cancel=job.cancel, progress=progress)
            return {"model_id": model_id, "path": str(path)}

        return {"job_id": self.jobs.start("model-download", work).id}

    def ai_remove_model(self, model_id: str) -> dict:
        if ALL_MODELS[model_id].bundled:
            raise ValueError("models included with Syncora cannot be removed")
        return {"removed": self.models.remove(model_id)}

    # ----------------------------------------------------------- transcription

    def transcripts_start(self, scope: str = "smart", clip_ids: list[int] | None = None, redo: bool = False) -> dict:
        """Transcribe clips in the background. ``smart``: the clearest recording of each moment (recorders first),
        so every camera of the same speech is not transcribed again; ``all``; or ``clips`` (``clip_ids``)."""
        project = self._require_project()
        settings = self._settings()
        model, language = settings["transcription_model"], settings["transcription_language"]
        if self.models.path(model) is None:
            raise ValueError(f"The speech model {ALL_MODELS[model].title} is not installed")
        rows = [r for r in project.clips() if r.audio_stream is not None and not r.ignored_duplicate]
        if scope == "clips":
            wanted = set(clip_ids or [])
            targets = [r.id for r in rows if r.id in wanted]
        elif scope == "all":
            targets = [r.id for r in rows]
        elif scope == "smart":
            targets = smart_targets(rows, project.placements(), project.audio_analysis())
        else:
            raise ValueError(f"unknown scope {scope}")
        if not redo:
            states = project.transcript_states()
            targets = [c for c in targets if not (c in states and states[c]["model"] == model
                                                   and states[c]["language"] == language)]  # fmt: skip
        tasks = self._pipeline().transcribe(targets, model, language, redo=redo)
        return {"clips": len(targets), "tasks": tasks}

    def transcripts_cancel(self, clip_ids: list[int] | None = None) -> dict:
        project = self._require_project()
        ids = [t["id"] for t in project.tasks(kinds=["transcribe"], statuses=["pending", "running"], limit=1_000_000)
               if clip_ids is None or t["clip_id"] in clip_ids]  # fmt: skip
        return {"cancelled": self._pipeline().cancel(ids=ids) if ids else 0}

    def transcripts_overview(self) -> dict:
        project = self._require_project()
        rows = {r.id: r for r in project.clips()}
        states = project.transcript_states()
        counts: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for t in project.task_counts_by_clip("transcribe"):
            counts[t["clip_id"]][t["status"]] += t["n"]
        per_clip = []
        for clip_id, state in states.items():
            row = rows.get(clip_id)
            if row is None:
                continue
            c = counts.get(clip_id, {})
            done, failed = c.get("done", 0) + c.get("skipped", 0), c.get("failed", 0)
            active = c.get("running", 0) + c.get("pending", 0)
            status = ("running" if c.get("running") else "queued" if active else "failed" if failed and not done
                      else "partial" if failed else "done")  # fmt: skip
            per_clip.append({"clip_id": clip_id, "name": row.name, "device_name": row.device_name,
                             "chunks": state["chunks"], "done": done, "failed": failed, "status": status,
                             "model": state["model"], "language": state["language"]})  # fmt: skip
        return {"totals": project.transcript_totals(), "clips": per_clip}

    def transcript_get(self, clip_id: int) -> dict:
        """A clip's transcript: its own, or what other recordings of the same moments heard (mapped to its time)."""
        project = self._require_project()
        row = project.clip(clip_id)
        names = {s["key"]: s["name"] for s in project.speakers()}
        own = project.transcript(clip_id)
        if own:
            segments = [{**s, "source_clip_id": clip_id} for s in own]
        else:
            segments = shared_transcript(project, row)
        for s in segments:
            s["speaker_name"] = names.get(s["speaker"]) if s["speaker"] else None
        state = project.transcript_states().get(clip_id)
        return {"clip_id": clip_id, "segments": segments, "state": state,
                "markers": project.markers(clip_id)}  # fmt: skip

    # ----------------------------------------------------------------- speakers

    def speakers_list(self) -> list[dict]:
        out = []
        for s in self._require_project().speakers():
            s.pop("centroid", None)
            out.append(s)
        return out

    def speakers_rename(self, key: str, name: str | None) -> list[dict]:
        self._require_project().rename_speaker(key, (name or "").strip() or None)
        return self.speakers_list()

    def speakers_merge(self, keys: list[str], into: str) -> list[dict]:
        project = self._require_project()
        for key in keys:
            if key != into:
                project.merge_speakers(key, into)
        if self.pipeline is not None:
            self.pipeline.forget_speakers()
        return self.speakers_list()

    # ------------------------------------------------------------------ markers

    def markers_list(self, clip_id: int | None = None) -> list[dict]:
        return self._require_project().markers(clip_id)

    def markers_add(self, clip_id: int, t_s: float, label: str | None = None, source: str = "user") -> dict:
        project = self._require_project()
        project.clip(clip_id)
        kind = source if source in ("user", "search") else "user"
        marker_id = project.add_marker(clip_id, float(t_s), label, source=kind)
        return next(m for m in project.markers(clip_id) if m["id"] == marker_id)

    def markers_update(self, marker_id: int, label: str | None) -> dict:
        self._require_project().update_marker(marker_id, label)
        return {"id": marker_id, "label": label}

    def markers_delete(self, marker_id: int) -> dict:
        self._require_project().delete_marker(marker_id)
        return {"deleted": marker_id}

    # ------------------------------------------------------------------- search

    def search_query(self, text: str, limit: int = 50) -> dict:
        """Moments matching ``text``: in what was said (the phrase, all its words or some), by a speaker of that
        name, in markers and in clip names. ``score`` says how well each matched."""
        project = self._require_project()
        text = (text or "").strip()
        if not text:
            return {"query": text, "results": []}
        rows = {r.id: r for r in project.clips()}
        speakers = {s["key"]: s["name"] for s in project.speakers()}
        results: list[dict] = []
        seen: set[tuple[int, float]] = set()

        def add(hit: dict) -> None:
            key = (hit["clip_id"], round(hit["t_s"], 1))
            if key not in seen and hit["clip_id"] in rows:
                seen.add(key)
                r = rows[hit["clip_id"]]
                hit.update(clip_name=r.name, device_name=r.device_name)
                results.append(hit)

        for s in project.search_transcripts(text, limit):
            add({"kind": "speech", "clip_id": s["clip_id"], "t_s": s["start_s"], "end_s": s["end_s"],
                 "text": s["text"], "speaker": s["speaker"], "speaker_name": speakers.get(s["speaker"]),
                 "language": s["language"], "matched_by": ["speech"], "score": _MATCH_SCORE[s["match"]]})  # fmt: skip
        lowered = text.lower()
        named = [k for k, name in speakers.items() if name and (name.lower() in lowered or lowered in name.lower())]
        for key in named:
            for s in project.speaker_segments(key, limit=20):
                add({"kind": "speaker", "clip_id": s["clip_id"], "t_s": s["start_s"], "end_s": s["end_s"],
                     "text": s["text"], "speaker": key, "speaker_name": speakers[key], "language": s["language"],
                     "matched_by": ["speaker"], "score": 0.8})  # fmt: skip
        for m in project.markers():
            if m["label"] and lowered in m["label"].lower():
                add({"kind": "marker", "clip_id": m["clip_id"], "t_s": m["t_s"], "end_s": None, "text": m["label"],
                     "speaker": None, "speaker_name": None, "language": None, "matched_by": ["marker"],
                     "score": 1.0 if m["label"].lower() == lowered else 0.85, "marker_id": m["id"]})  # fmt: skip
        for r in rows.values():
            if lowered in r.name.lower():
                add({"kind": "clip", "clip_id": r.id, "t_s": 0.0, "end_s": None, "text": r.name, "speaker": None,
                     "speaker_name": None, "language": None, "matched_by": ["clip name"],
                     "score": 1.0 if r.name.lower() == lowered else 0.7})  # fmt: skip
        results.sort(key=lambda h: (-h["score"], h["clip_id"], h["t_s"]))
        return {"query": text, "results": results[:limit]}


def smart_targets(rows: list[ClipRow], placements: dict, analysis: dict) -> list[int]:
    """Clips to transcribe so that every moment is heard once: per sync group, recorders first (then the loudest,
    then the longest), each clip only when it covers new time. Clips not placed on a timeline are all included."""
    chosen: list[int] = []
    by_group: dict[int, list[tuple[ClipRow, float]]] = defaultdict(list)
    for r in rows:
        p = placements.get(r.id)
        if p is None or p.start_s is None or p.group is None or r.status != "online":
            if r.status == "online":
                chosen.append(r.id)
            continue
        by_group[p.group].append((r, p.start_s))
    for members in by_group.values():
        members.sort(key=lambda m: (m[0].device_kind != "recorder",
                                    -((analysis.get(m[0].id) or {}).get("level_dbfs") or -120.0),
                                    -m[0].info.duration_s))  # fmt: skip
        covered: list[tuple[float, float]] = []
        for r, start in members:
            span = (start, start + r.info.duration_s)
            new = (span[1] - span[0]) - _overlap(span, covered)
            if new >= min(_MIN_NEW_S, 0.5 * (span[1] - span[0])):
                chosen.append(r.id)
                covered = _union([*covered, span])
    return chosen


def _union(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _overlap(span: tuple[float, float], covered: list[tuple[float, float]]) -> float:
    return sum(max(0.0, min(span[1], b) - max(span[0], a)) for a, b in covered)


def shared_transcript(project: Project, row: ClipRow) -> list[dict]:
    """What transcribed recordings of the same sync group heard while ``row`` was recording, in ``row``'s time."""
    placements = project.placements()
    mine = placements.get(row.id)
    if mine is None or mine.start_s is None or mine.group is None:
        return []
    out = []
    for other_id in project.transcript_states():
        other = placements.get(other_id)
        if other_id == row.id or other is None or other.group != mine.group or other.start_s is None:
            continue
        shift = other.start_s - mine.start_s
        for s in project.transcript(other_id):
            t = s["start_s"] + shift
            if 0 <= t < row.info.duration_s:
                out.append({**s, "start_s": t, "end_s": s["end_s"] + shift, "source_clip_id": other_id})
    out.sort(key=lambda s: s["start_s"])
    return out


__all__ = ["AiMethods", "ProjectError", "smart_targets", "shared_transcript"]

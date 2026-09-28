"""Engine methods of the AI features: models, transcription, transcripts, speakers, markers and search.

Mixed into :class:`mcsync.service.app.EngineService` (which provides the project, pipeline, cache, jobs and
notifications).
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import TYPE_CHECKING, Any

import numpy as np

from mcsync.ai import speakers as voices
from mcsync.ai import visual
from mcsync.ai.aisync import Candidate, Evidence, Placed, Segment, agreement, metadata_evidence, rank, speech_candidates
from mcsync.ai.models import ALL_MODELS, DEFAULT_SPEECH_MODEL, SPEECH_MODELS, ModelStore
from mcsync.ai.speech import LANGUAGES, Transcriber, chunks
from mcsync.project.db import ClipRow, ProjectError
from mcsync.resources import cpu_usable
from mcsync.sync.types import ClipPlacement, Flag, MatchStatus, PlacementMethod, PlacementStatus, SyncResult

if TYPE_CHECKING:
    from mcsync.media.cache import AnalysisCache
    from mcsync.pipeline.runner import Pipeline
    from mcsync.project.db import Project
    from mcsync.service.jobs import Job, JobManager

#: Search scores: an utterance with the whole phrase, with all its words, or with some of them.
_MATCH_SCORE = {"phrase": 1.0, "all words": 0.9, "some words": 0.6}
#: How a placement was made, in the words of AI sync's explanation.
_PLACED_BY = {"audio": "its audio", "timecode": "timecode", "metadata": "its camera clock", "chapter": "its chapter",
              "manual": "hand", "ai": "AI sync"}  # fmt: skip
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
    def _solve(self, clips: Any = None) -> dict: ...  # pragma: no cover
    def _clip_inputs(self, rows: list, job: Any = None, *, extract: bool = True, remember: bool = True) -> list: ...
    def _engine(self) -> Any: ...  # pragma: no cover
    def _run_matches(self, run_id: int) -> list: ...  # pragma: no cover
    def tools(self) -> Any: ...  # pragma: no cover
    def log(self, text: str) -> None: ...  # pragma: no cover

    pipeline: Pipeline | None

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

    # ------------------------------------------------------------------ AI sync

    def ai_sync(self, clip_id: int) -> dict:
        """Look for a clip's place from its speech, light changes, its audio at close range and the camera clocks
        (a job; its result is kept with the clip: ``ai.sync_result``)."""
        project = self._require_project()
        project.clip(clip_id)
        return {"job_id": self.jobs.start("ai-sync", lambda job: self._ai_sync(clip_id, job=job)).id}

    def ai_sync_result(self, clip_id: int) -> dict | None:
        found = self._require_project().ai_analyses("sync").get(clip_id)
        return {**found["result"], "status": found["status"]} if found and found["result"] else None

    def ai_sync_accept(self, clip_id: int, candidate: int = 0) -> dict:
        """Place the clip where AI sync proposes (as a correction the user made: it can be undone)."""
        project = self._require_project()
        found = project.ai_analyses("sync").get(clip_id)
        if not found or not found["result"] or not found["result"]["candidates"]:
            raise ValueError("no AI sync result for this clip")
        c = found["result"]["candidates"][candidate]
        project.add_correction("offset", clip_id, other_clip_id=c["anchor_clip_id"], offset_s=c["offset_s"])
        project.set_ai_status(clip_id, "sync", "accepted")
        return self._solve()

    def ai_sync_reject(self, clip_id: int) -> dict:
        """Discard AI sync's proposal for the clip (it is left for manual sync)."""
        self._require_project().set_ai_status(clip_id, "sync", "rejected")
        return self._solve()

    def _ai_sync(self, clip_id: int, *, job: Job | None = None, source: str = "user") -> dict:
        project = self._require_project()
        rows = {r.id: r for r in project.clips()}
        row = rows[clip_id]
        placements = project.placements()
        cancel = job.cancel if job is not None else None

        def report(fraction: float, message: str) -> None:
            if job is not None:
                job.report(fraction, message)

        # 1. What the clip says (transcribed now if needed).
        segments = project.transcript(clip_id)
        settings = self._settings()
        if not segments and row.audio_stream is not None and self.models.path(settings["transcription_model"]):
            report(0.02, f"Transcribing {row.name}")
            segments = self._transcribe_now(row, cancel, lambda f: report(0.02 + 0.4 * f, f"Transcribing {row.name}"))
        target = [Segment(s["start_s"], s["end_s"], s["text"]) for s in segments]

        # 2. Speech heard in both.
        report(0.45, "Matching sentences with the other recordings")
        placed = []
        transcribed = set(project.transcript_states())
        for cid, p in placements.items():
            if cid == clip_id or cid not in rows or p.start_s is None or p.group is None:
                continue
            own = project.transcript(cid) if cid in transcribed else []
            placed.append(Placed(cid, p.group, p.start_s, rows[cid].info.duration_s,
                                 [Segment(s["start_s"], s["end_s"], s["text"]) for s in own]))  # fmt: skip
        candidates = rank(speech_candidates(target, placed))[:3]

        # 3. Light changes seen by both cameras: around each candidate, or on their own when nothing was said.
        by_id = {p.clip_id: p for p in placed}
        if row.info.video:
            report(0.5, f"Reading the light changes in {row.name}")
            target_lum = self._brightness(row, cancel, lambda f: report(0.5 + 0.2 * f, f"Reading {row.name}"))
            if candidates:
                for c in candidates:
                    anchor = rows[c.anchor_clip_id]
                    if anchor.info.video:
                        lum = self._brightness(anchor, cancel)
                        found = visual.correlate(target_lum, lum, (c.offset_s - 3.0, c.offset_s + 3.0))
                        if found is not None and found.score >= 0.2:
                            c.evidence.append(_visual_evidence(found))
            else:
                for other in self._clock_neighbours(row, rows, by_id)[:3]:
                    lum = self._brightness(rows[other.clip_id], cancel)
                    span = (-row.info.duration_s + 3.0, other.duration_s - 3.0)
                    found = visual.correlate(target_lum, lum, span)
                    if found is not None and found.score >= 0.3 and found.ratio >= 1.5:
                        start = other.start_s + found.lag_s
                        candidates.append(
                            Candidate(other.clip_id, other.group, found.lag_s, start, [_visual_evidence(found)])
                        )

        # 4. The audio compared again, only around each candidate: speech fingerprints missed often still
        # correlates within ±2 s. It also makes the offset exact.
        report(0.75, "Comparing the audio at each candidate")
        for c in candidates:
            anchor = rows[c.anchor_clip_id]
            if row.audio_stream is None or anchor.audio_stream is None:
                continue
            try:
                ref, tgt = self._clip_inputs([anchor, row], remember=False)
                m = self._engine().match_pair(ref, tgt, window=(c.offset_s - 2.0, c.offset_s + 2.0))
            except Exception:  # noqa: BLE001 - audio not readable: the other evidence stands
                continue
            est = m.estimate
            if m.offset_s is not None and est.status == MatchStatus.CONFIDENT:
                c.offset_s = m.offset_s
                if anchor.id in by_id:
                    c.start_s = by_id[anchor.id].start_s + m.offset_s
                times = [(w.time_s, max(0.0, w.correlation)) for w in est.windows if w.inlier]
                note = f"audio agrees within ±2 s (correlation {est.correlation:.2f})"
                c.evidence.append(Evidence("audio", est.confidence, note, times))
            elif est.correlation > 0:
                c.evidence.append(Evidence("audio", 0.0, f"audio does not confirm (correlation {est.correlation:.2f})"))

        # 5. The camera clocks, as a sanity check.
        for c in candidates:
            clock = _clock_offset(row, rows[c.anchor_clip_id])
            e = metadata_evidence(c, clock)
            if e is not None:
                c.evidence.append(e)

        ranked = rank(candidates)[:5]
        result = {
            "clip_id": clip_id,
            "source": source,
            "duration_s": row.info.duration_s,
            "reason": self._ai_reason(row, placements),
            "searched": {
                "speech_s": sum(s.end_s - s.start_s for s in target),
                "sentences": len(target),
                "against": len(placed),
            },  # fmt: skip
            "candidates": [c.to_dict(row.info.duration_s) for c in ranked],
            "agreement": agreement(ranked),
        }
        project.set_ai_analysis(clip_id, "sync", result, ranked[0].confidence if ranked else None,
                                "proposed" if ranked else "failed")  # fmt: skip
        report(1.0, "Done")
        return result

    def _brightness(self, row: ClipRow, cancel: Any = None, progress: Any = None) -> np.ndarray:
        return visual.brightness(self.tools(), row.path, self.cache.base, row.fingerprint, cancel=cancel,
                                 progress=progress, duration_s=row.info.duration_s)  # fmt: skip

    def _clock_neighbours(self, row: ClipRow, rows: dict[int, ClipRow], placed: dict[int, Placed]) -> list[Placed]:
        """Video clips on a timeline, closest by camera clock first (when there is nothing said to match)."""
        out = []
        for cid, p in placed.items():
            other = rows[cid]
            if not other.info.video:
                continue
            clock = _clock_offset(row, other)
            out.append((abs(clock) if clock is not None else 1e9, p))
        return [p for _, p in sorted(out, key=lambda x: x[0])]

    def _ai_reason(self, row: ClipRow, placements: dict) -> str:
        """Why audio could not place the clip, in plain words, with the measured value."""
        p = placements.get(row.id)
        if row.audio_stream is None:
            return f"{row.name} has no sound to compare with the other recordings."
        project = self._require_project()
        run = project.last_completed_run()
        best = None
        if run is not None:
            me = str(row.id)
            for m in self._run_matches(run):
                if me in (m.ref_id, m.tgt_id) and m.estimate.correlation is not None:
                    best = max(best or 0.0, float(m.estimate.correlation))
        placed = p is not None and p.start_s is not None
        if placed and p.status == PlacementStatus.SYNCED:
            if p.method == PlacementMethod.REFERENCE:
                return f"{row.name} is the reference recording: AI sync checks it against the others."
            how = _PLACED_BY.get(p.method.value, p.method.value)
            return (f"{row.name} is already placed by {how} at {p.confidence:.0%} confidence: AI sync checks the "
                    "placement with other evidence.")  # fmt: skip
        if placed and p.status == PlacementStatus.NEEDS_REVIEW:
            return f"{row.name} was placed with low confidence ({p.confidence:.0%})."
        if best is not None:
            return (f"{row.name} audio correlates at {best:.2f} at best with the other recordings "
                    "(a confident match needs clearly more).")  # fmt: skip
        return f"No other recording was found to overlap {row.name} by sound or clock."

    def _transcribe_now(self, row: ClipRow, cancel: Any, progress: Any) -> list[dict]:
        """Transcribe one clip right away (AI sync needs it), keeping the result like a background transcription."""
        project = self._require_project()
        settings = self._settings()
        model, language = settings["transcription_model"], settings["transcription_language"]
        stream = row.audio_stream_info
        assert stream is not None
        spans = chunks(row.info.duration_s)
        project.set_transcript_state(row.id, model, language, len(spans))
        known = [voices.Speaker(r["key"], np.frombuffer(r["centroid"], dtype=np.float32).copy(), int(r["utterances"]))
                 for r in project.speakers()]  # fmt: skip
        transcriber = self.transcriber(model, language)
        for k, (a, b) in enumerate(spans):
            utterances, events = transcriber.transcribe_range(
                self.tools(), row.path, stream.index, row.audio_channel, stream.channels, a, b, cancel,
                lambda f, k=k: progress((k + f) / len(spans)),
            )  # fmt: skip
            keys = voices.assign([u.fingerprint for u in utterances], known)
            rows = [(u.start_s, u.end_s, key, u.language, u.text,
                     u.fingerprint.astype(np.float32).tobytes() if u.fingerprint is not None else None)
                    for u, key in zip(utterances, keys, strict=True)]  # fmt: skip
            project.replace_transcript_chunk(row.id, k, (a, b), rows, [(e.t_s, e.label) for e in events])
        project.save_speakers([(s.key, s.centroid.astype(np.float32).tobytes(), s.count) for s in known])
        if self.pipeline is not None:
            self.pipeline.forget_speakers()
        return project.transcript(row.id)

    def ai_fallback(self) -> dict:
        """AI sync for every clip audio could not place, as a job (asked for by the user: its proposals are shown
        for review whether or not the automatic fallback is switched on)."""
        return self._fallback("fallback")

    def _fallback(self, source: str) -> dict:
        project = self._require_project()

        def work(job: Job) -> dict:
            placements = project.placements()
            todo = [r for r in project.clips() if not r.ignored_duplicate and r.status == "online"
                    and (placements.get(r.id) is None or placements[r.id].start_s is None)]  # fmt: skip
            done = {cid for cid, a in project.ai_analyses("sync").items() if a["status"] in ("accepted", "rejected")}
            todo = [r for r in todo if r.id not in done]
            found = 0
            for k, r in enumerate(todo):
                if job.cancel.is_set():
                    break
                job.report(k / max(1, len(todo)), f"AI sync: {r.name} ({k + 1} of {len(todo)})")
                try:
                    result = self._ai_sync(r.id, source=source)
                except Exception as exc:  # noqa: BLE001 - one clip's problem does not stop the others
                    self.log(f"AI sync of {r.name} failed: {exc}")
                    continue
                found += bool(result["candidates"])
            timeline = self._solve()
            self.notify("ai.fallback_done", {"clips": len(todo), "placed": found})
            return {"clips": len(todo), "placed": found, "timeline": timeline}

        return {"job_id": self.jobs.start("ai-fallback", work).id}

    def after_sync_run(self) -> None:
        """Called when a sync run completes: the automatic AI fallback, when switched on."""
        if self.project is not None and self._settings().get("ai_fallback"):
            self._fallback("auto")

    def with_ai_proposals(self, result: SyncResult) -> SyncResult:
        """The fallback's proposals, as placements for review, for clips audio could not place: the automatic
        fallback's while it is switched on, and those of a fallback the user started."""
        sources = ("auto", "fallback") if self._settings().get("ai_fallback") else ("fallback",)
        proposals = self._require_project().ai_analyses("sync")
        placements = dict(result.placements)
        for clip_id, a in proposals.items():
            res = a["result"]
            if a["status"] != "proposed" or not res or res.get("source") not in sources or not res["candidates"]:
                continue
            best = res["candidates"][0]
            key, anchor = str(clip_id), placements.get(str(best["anchor_clip_id"]))
            current = placements.get(key)
            if best["confidence"] < 0.5 or anchor is None or anchor.start_s is None or (
                    current is not None and current.start_s is not None):  # fmt: skip
                continue
            placements[key] = ClipPlacement(key, anchor.start_s + best["offset_s"], anchor.group, PlacementMethod.AI,
                                            float(best["confidence"]), PlacementStatus.NEEDS_REVIEW,
                                            (Flag.AI_PROPOSAL,), anchor_id=anchor.anchor_id)  # fmt: skip
        return SyncResult(result.reference_id, placements, result.matches, result.edges, result.warnings)


def _visual_evidence(found: visual.VisualMatch) -> Evidence:
    events = len(found.shared_events)
    note = (f"{events} light change{'s' if events != 1 else ''} seen by both cameras" if events
            else f"brightness changes correlate at {found.score:.2f}")  # fmt: skip
    strength = min(1.0, found.score * (1.0 if found.ratio >= 1.5 else 0.6))
    return Evidence("visual", strength, note, [(t, strength) for t in found.shared_events])


def _clock_offset(row: ClipRow, anchor: ClipRow) -> float | None:
    a, b = row.info.creation_time, anchor.info.creation_time
    return (a - b).total_seconds() if a is not None and b is not None else None


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

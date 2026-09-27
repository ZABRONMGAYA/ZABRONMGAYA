"""``mcsync`` command line.

    mcsync serve                       JSON-RPC on stdin/stdout (what the desktop app runs)
    mcsync probe FILE...               metadata as JSON (for bug reports about unusual files)
    mcsync sync PATH... [options]      import folders/files, synchronise, print the placements
    mcsync export PROJECT OUTPUT       write the synchronised timeline as XML (Premiere Pro, Resolve)

``sync`` uses the same service code as the desktop app, so a project it writes
(``--project``) opens in the app with its matches, and running it again on a
grown folder only matches the new pairs.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import sys
import tempfile
from pathlib import Path

from mcsync import __version__


def _fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    sign = "-" if seconds < 0 else ""
    s = abs(seconds)
    return f"{sign}{int(s // 3600):02d}:{int(s // 60) % 60:02d}:{s % 60:06.3f}"


def _progress(method: str, params: dict) -> None:
    if method == "job.progress":
        print(f"\r{params['progress'] * 100:5.1f}%  {params['message'][:70]:<70}", end="", file=sys.stderr, flush=True)
    elif method in ("job.done", "job.failed"):
        print(file=sys.stderr)


def cmd_serve(args: argparse.Namespace) -> int:
    from mcsync.service.app import serve_stdio

    serve_stdio(cache_dir=args.cache_dir, workers=args.workers)
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    from mcsync.media.probe import probe
    from mcsync.serialize import media_info_to_dict

    out = [media_info_to_dict(probe(path), include_raw=args.raw) for path in args.files]
    print(json.dumps(out if len(out) > 1 else out[0], indent=2))
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    from mcsync.project.db import PROJECT_SUFFIX
    from mcsync.service.app import EngineService

    service = EngineService(notify=None if args.json else _progress, cache_dir=args.cache_dir, workers=args.workers)
    try:
        if args.project:
            path = Path(args.project)
            summary = service.project_open(str(path)) if path.exists() else service.project_create(str(path))
        else:
            tmp = Path(tempfile.mkdtemp(prefix="mcsync-")) / f"session{PROJECT_SUFFIX}"
            summary = service.project_create(str(tmp))
        del summary
        job = service.jobs.wait(service.media_import(args.paths)["job_id"])
        if job.status != "done":
            print(f"import failed: {job.error}", file=sys.stderr)
            return 1
        for problem in job.result["problems"]:
            print(f"skipped {problem['path']}: {problem['message']}", file=sys.stderr)
        reference = None
        if args.reference:
            wanted = str(Path(args.reference).resolve())
            matches = [c for c in service.media_list()["clips"] if str(Path(c["path"]).resolve()) == wanted]
            if not matches:
                print(f"reference {args.reference} is not among the imported files", file=sys.stderr)
                return 2
            reference = matches[0]["clip_id"]
        job = service.jobs.wait(
            service.sync_run(mode=args.mode, reference_clip_id=reference, timecode_jam_synced=args.jam_synced)["job_id"]
        )
        if job.status != "done":
            print(f"sync failed: {job.error}", file=sys.stderr)
            return 1
        result = job.result
        report = _export(service, args.export, args) if args.export else None
        if args.json:
            print(json.dumps(result | ({"export": report} if report else {}), indent=2))
            return 0
        _print_timeline(result)
        if report:
            _print_export(report)
        return 0
    finally:
        service.close()


def _export(service, output: str, args: argparse.Namespace) -> dict:  # noqa: ANN001 - EngineService
    fmt = args.format or ("fcpxml" if output.lower().endswith(".fcpxml") else "xmeml")
    return service.export_xml(
        format=fmt,
        path=output,
        sequence_rate=args.rate,
        start_timecode=args.start_tc,
        include_uncertain=not args.exclude_uncertain,
    )


def cmd_export(args: argparse.Namespace) -> int:
    from mcsync.service.app import EngineService

    service = EngineService(notify=None, cache_dir=args.cache_dir, workers=args.workers)
    try:
        service.project_open(args.project)
        _print_export(_export(service, args.output, args))
        return 0
    except ValueError as exc:
        print(f"export failed: {exc}", file=sys.stderr)
        return 1
    finally:
        service.close()


def _print_export(report: dict) -> None:
    seq = report["sequence"]
    rate = seq["rate"].split("/")
    fps = int(rate[0]) / int(rate[1])
    premiere = report.get("max_error_ms_premiere")
    print(
        f"wrote {report['path']} ({report['format']}): {len(report['clips'])} clips on {seq['video_tracks']} video and "
        f"{seq['audio_tracks']} audio tracks, {fps:.3f} fps from {seq['start_timecode']}"
    )
    print(
        f"largest placement error {report['max_error_ms']:.1f} ms"
        + (f" (Premiere Pro, with sub-frame in points: {premiere:.1f} ms)" if premiere is not None else "")
    )
    for s in report["skipped"]:
        print(f"not exported: {s['name']} ({s['reason']})")
    for w in report["warnings"]:
        print(f"warning: {w}")


def _add_export_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--format", choices=["xmeml", "fcpxml"], help="default: from the extension (.fcpxml or .xml)")
    p.add_argument("--rate", help="sequence frame rate, e.g. 25 or 24000/1001 (default: the most common)")
    p.add_argument("--start-tc", default="01:00:00:00", help="sequence start timecode (default 01:00:00:00)")
    p.add_argument("--exclude-uncertain", action="store_true", help="leave out clips that need review")


def _print_timeline(result: dict) -> None:
    timeline = result["timeline"]
    print(f"{result['pairs']} pairs ({result['reused']} reused, {result['matched']} matched)")
    rows = []
    for group in timeline["groups"]:
        for c in group["clips"]:
            rows.append((group["group"], c))
    rows += [(None, c) for c in timeline["unsynced"]]
    print(f"{'group':>5}  {'start':>13}  {'status':<12} {'method':<9} {'conf':>4}  {'device':<24} {'clip':<28} flags")
    for group, c in sorted(rows, key=lambda gc: (gc[0] is None, gc[0] or 0, gc[1]["start_s"] or 0)):
        print(
            f"{'' if group is None else group:>5}  {_fmt_time(c['start_s']):>13}  {c['status']:<12} {c['method']:<9} "
            f"{c['confidence']:4.2f}  {c['device_name'][:24]:<24} {c['name'][:28]:<28} {','.join(c['flags'])}"
        )
    review = timeline["review"]
    if review:
        print(f"\n{len(review)} clip(s) to review: " + ", ".join(f"{r['clip_id']} ({r['reason']})" for r in review))


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()  # the packaged engine starts matcher processes from itself
    parser = argparse.ArgumentParser(prog="mcsync", description="Syncora engine")
    parser.add_argument("--version", action="version", version=f"mcsync {__version__}")
    parser.add_argument("--cache-dir", default=None, help="analysis cache location")
    parser.add_argument("--workers", type=int, default=None, help="matcher processes (default: cores - 1)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="JSON-RPC service on stdin/stdout").set_defaults(func=cmd_serve)

    p = sub.add_parser("probe", help="print media metadata as JSON")
    p.add_argument("files", nargs="+")
    p.add_argument("--raw", action="store_true", help="include the raw ffprobe output")
    p.set_defaults(func=cmd_probe)

    s = sub.add_parser("sync", help="synchronise folders or files")
    s.add_argument("paths", nargs="+")
    s.add_argument("--project", help="project file to create or update (default: temporary)")
    s.add_argument("--mode", choices=["hybrid", "audio", "timecode"], default=None)
    s.add_argument("--reference", help="file to use as the reference clip")
    s.add_argument("--jam-synced", action="store_true", default=None, help="all devices share jam-synced timecode")
    s.add_argument("--json", action="store_true", help="print the full result as JSON")
    s.add_argument("--export", metavar="OUTPUT", help="also write the timeline as XML")
    _add_export_options(s)
    s.set_defaults(func=cmd_sync)

    e = sub.add_parser("export", help="write a project's synchronised timeline as XML for Premiere Pro or Resolve")
    e.add_argument("project")
    e.add_argument("output", help="an .xml (FCP 7 XML) or .fcpxml file")
    _add_export_options(e)
    e.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

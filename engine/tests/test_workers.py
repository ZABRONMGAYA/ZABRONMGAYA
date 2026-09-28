"""Worker counts: sized to the computer, never more processes than the platform allows ("max_workers must be <= 61"
on Windows), and many files are many jobs for a few workers."""

from __future__ import annotations

import sys
from concurrent.futures import ProcessPoolExecutor

import pytest

from mcsync import resources
from mcsync.resources import (
    WINDOWS_MAX_PROCESS_WORKERS,
    Resources,
    max_process_workers,
    recommend_workers,
    safe_process_workers,
)
from mcsync.service.app import EngineService, _plan_for


def machine(cores: int, ram_gb: float | None = 256.0, platform: str = "win32") -> Resources:
    ram = int(ram_gb * 2**30) if ram_gb is not None else None
    return Resources(cpu_logical=cores, cpu_usable=cores, ram_total_bytes=ram, ram_available_bytes=ram, gpu=None,
                     gpu_memory_bytes=None, platform=platform)  # fmt: skip


def test_windows_allows_at_most_61_process_workers():
    assert WINDOWS_MAX_PROCESS_WORKERS == 61
    assert max_process_workers("win32") == 61
    assert max_process_workers("linux") > 61
    assert safe_process_workers(200, platform="win32") == 61
    assert safe_process_workers(15, platform="win32") == 15
    assert safe_process_workers(0, platform="win32") == 1


@pytest.mark.parametrize(("cores", "expected"), [(16, 15), (32, 31), (64, 61), (128, 61), (2, 1), (1, 1)])
def test_recommended_matchers_on_windows(cores, expected):
    assert recommend_workers(machine(cores)).match == expected


def test_memory_still_limits_the_matchers():
    assert recommend_workers(machine(64, ram_gb=5.0)).match == 10  # (5 GB - 1 GB) / 400 MB
    assert recommend_workers(machine(128, platform="linux")).match == 127


def test_explicit_worker_counts_are_capped(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    plan = _plan_for(64, recommend_workers(machine(64)))
    assert plan.match == 61


def test_manual_worker_setting_is_capped_on_windows(monkeypatch, tmp_path):
    svc = EngineService(cache_dir=str(tmp_path / "cache"), workers=1)
    try:
        monkeypatch.setattr(resources.sys, "platform", "win32")
        result = svc.engine_configure({"probe": 4, "analyze": 4, "match": 64})
        assert result["plan"]["match"] == 61 and "at most 61" in result["plan"]["reason"]
    finally:
        svc.close()


def _square(x: int) -> int:
    return x * x


def test_thousands_of_jobs_run_on_a_bounded_pool():
    workers = safe_process_workers(4)
    with ProcessPoolExecutor(workers) as pool:
        results = list(pool.map(_square, range(4000), chunksize=64))
    assert results[-1] == 3999 * 3999 and len(results) == 4000
    assert len(pool._processes or {}) <= workers  # type: ignore[attr-defined]


@pytest.mark.skipif(sys.platform != "win32", reason="the 61-process limit is Windows's")
def test_a_pool_asked_for_more_than_61_matchers_starts_on_windows():
    """On Windows, ProcessPoolExecutor(max_workers=64) raises "max_workers must be <= 61"; the engine's pool caps."""
    from mcsync.sync.engine import create_match_pool

    pool = create_match_pool(64)
    try:
        assert pool._max_workers == 61  # noqa: SLF001
        assert pool.submit(sum, [1, 2]).result() == 3
    finally:
        pool.shutdown()

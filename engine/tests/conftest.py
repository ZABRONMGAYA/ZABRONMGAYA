"""Shared synthetic scenes.

Scenes are expensive to render, so each is built once per test session. Every
test derives its own recordings from them with explicit seeds, so results are
deterministic.
"""

from __future__ import annotations

import pytest

from mcsync.testing.synthetic import Scene, make_scene

SCENE_RATE = 16000


@pytest.fixture(scope="session")
def speech_scene() -> Scene:
    return make_scene(420.0, kind="speech", rate=SCENE_RATE, seed=1)


@pytest.fixture(scope="session")
def mixed_scene() -> Scene:
    return make_scene(900.0, kind="mixed", rate=SCENE_RATE, seed=3)


@pytest.fixture(scope="session")
def live_music_scene() -> Scene:
    return make_scene(240.0, kind="music", rate=SCENE_RATE, seed=7)


@pytest.fixture(scope="session")
def loop_music_scene() -> Scene:
    return make_scene(240.0, kind="music", rate=SCENE_RATE, seed=7, exact_loop=True)


@pytest.fixture(scope="session")
def unrelated_scenes() -> list[Scene]:
    kinds = ("speech", "music", "ambience", "mixed")
    return [make_scene(150.0, kind=k, rate=SCENE_RATE, seed=1000 + i) for i, k in enumerate(kinds * 2)]


@pytest.fixture(scope="session")
def wedding_shoot(tmp_path_factory):
    """Real media generated with FFmpeg (see mcsync.testing.media); shared by media and service tests."""
    from mcsync.testing.media import ffmpeg_available, generate_wedding_shoot

    if not ffmpeg_available():
        pytest.skip("FFmpeg is not installed")
    return generate_wedding_shoot(tmp_path_factory.mktemp("shoot"))

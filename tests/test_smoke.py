from __future__ import annotations

from pathlib import Path

import pytest

from app.errors import VideoUnavailable
from app.smoke import main
from tests.fakes import CANONICAL, FakeBackend


def test_smoke_lists_the_offers_and_saves_every_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    backend = FakeBackend()
    assert main([CANONICAL, "--save", str(tmp_path / "out")], backend=backend) == 0
    output = capsys.readouterr().out
    assert "scout2015" in output and "Video · 1080p" in output and output.strip().endswith("OK")
    saved = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert saved == ["fake_audio.mp4", "fake_cover.mp4", "fake_meta.mp4", "fake_video.mp4", "fake_video_sd.mp4",
                      "fake_video_wm.mp4"]  # fmt: skip
    assert all((tmp_path / "out" / name).stat().st_size == len(backend.payload) for name in saved)
    assert backend.closed == len(saved)


def test_smoke_without_save_only_looks_the_post_up(capsys: pytest.CaptureFixture[str]) -> None:
    backend = FakeBackend()
    assert main([CANONICAL], backend=backend) == 0
    assert backend.open_calls == []
    assert "offers" in capsys.readouterr().out


def test_smoke_reports_failures_with_a_nonzero_exit(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([CANONICAL], backend=FakeBackend(error=VideoUnavailable())) == 1
    assert "video_unavailable" in capsys.readouterr().err


def test_smoke_rejects_non_tiktok_links(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["https://example.com/x"], backend=FakeBackend()) == 1
    assert "invalid_url" in capsys.readouterr().err


def test_smoke_points_profile_links_to_the_profile_check(capsys: pytest.CaptureFixture[str]) -> None:
    backend = FakeBackend()
    assert main(["https://www.tiktok.com/@scout2015"], backend=backend) == 2
    assert "python -m app.bulk @scout2015 --dry-run" in capsys.readouterr().err
    assert backend.fetch_calls == [] and backend.open_calls == []  # it never tried to treat the profile as a post

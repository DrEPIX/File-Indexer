"""The in-app player: grouping tagged moments, and the timeline that shows them.

The grouping is a pure function over annotation rows, so the arithmetic that
decides *where* a mark lands is tested without a window. The widget tests cover
the parts a user touches: clicking near a mark, and what the scrub bar reports.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from mediaengine.studio.player import (  # noqa: E402
    Moment,
    TagTimeline,
    format_timecode,
    group_moments,
)
from mediaengine.studio.tokens import TOKENS  # noqa: E402


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _row(seconds: float | None, label: str, confidence: float = 0.9) -> dict[str, Any]:
    return {"frame_time": seconds, "label": label, "confidence": confidence}


# ── timecodes ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("milliseconds", "expected"),
    [
        (0, "0:00"),
        (7_000, "0:07"),
        (95_000, "1:35"),
        (3_600_000, "1:00:00"),
        (5_425_000, "1:30:25"),
        (-500, "0:00"),
    ],
)
def test_timecodes_read_the_way_people_write_them(milliseconds: int, expected: str) -> None:
    assert format_timecode(milliseconds) == expected


# ── grouping ─────────────────────────────────────────────────────────────────


def test_one_frame_with_many_labels_is_one_moment() -> None:
    """Twelve labels on the same keyframe is one point in the video."""
    moments = group_moments(
        [_row(2.5, "1girl"), _row(2.5, "beach", 0.4), _row(2.5, "sunset", 0.7)]
    )
    assert len(moments) == 1
    assert moments[0].seconds == 2.5
    assert set(moments[0].labels) == {"1girl", "beach", "sunset"}
    # The strongest claim at that instant is what the marker's height reflects.
    assert moments[0].confidence == pytest.approx(0.9)


def test_moments_come_back_in_playback_order() -> None:
    moments = group_moments([_row(61.0, "c"), _row(2.0, "a"), _row(30.5, "b")])
    assert [moment.seconds for moment in moments] == [2.0, 30.5, 61.0]


def test_untimed_claims_are_left_off_the_timeline() -> None:
    """Asset-level tags describe the whole video and belong to no instant."""
    moments = group_moments([_row(None, "summary-tag"), _row(4.0, "real")])
    assert [moment.labels for moment in moments] == [("real",)]


def test_nearly_identical_timestamps_collapse() -> None:
    """Two analyzers sampling the same keyframe must not double the marks."""
    moments = group_moments([_row(10.0, "dog"), _row(10.2, "grass"), _row(12.0, "ball")])
    assert len(moments) == 2
    assert set(moments[0].labels) == {"dog", "grass"}


def test_a_repeated_label_at_one_moment_is_not_listed_twice() -> None:
    moments = group_moments([_row(5.0, "dog"), _row(5.1, "dog")])
    assert moments[0].labels == ("dog",)


def test_malformed_rows_are_skipped_rather_than_raising() -> None:
    """Annotation values come from plugins; the player must survive them."""
    moments = group_moments(
        [
            {"frame_time": "not a number", "label": "x"},
            {"frame_time": 3.0, "label": ""},
            {"frame_time": 3.0, "label": "good", "confidence": "high"},
            {"frame_time": -2.0, "label": "negative"},
        ]
    )
    assert [moment.labels for moment in moments] == [("negative",), ("good",)]
    assert moments[0].seconds == 0.0, "a negative timestamp clamps rather than escaping the bar"


def test_a_video_with_no_tags_yields_no_moments() -> None:
    assert group_moments([]) == []


# ── the timeline widget ──────────────────────────────────────────────────────


def test_clicking_the_bar_seeks_to_that_fraction(qt_app: QApplication) -> None:
    del qt_app
    timeline = TagTimeline(TOKENS)
    timeline.resize(216, 46)  # 8px padding either side -> a 200px track
    timeline.set_duration(100.0)

    assert timeline.seconds_at(timeline._track().left()) == pytest.approx(0.0)
    assert timeline.seconds_at(timeline._track().center().x()) == pytest.approx(50.0, abs=1.0)
    # Past either end clamps instead of seeking outside the video.
    assert timeline.seconds_at(-500.0) == pytest.approx(0.0)
    assert timeline.seconds_at(9999.0) == pytest.approx(100.0)


def test_a_click_near_a_mark_snaps_to_it(qt_app: QApplication) -> None:
    """The marks are the reason to click here; a two-pixel miss must not land 40s away."""
    del qt_app
    timeline = TagTimeline(TOKENS)
    timeline.resize(216, 46)
    timeline.set_duration(100.0)
    timeline.set_moments([Moment(seconds=50.0, labels=("dog",), confidence=0.8)])

    centre = timeline._track().center().x()
    assert timeline.moment_near(centre + 3.0) is not None
    assert timeline.moment_near(centre + 40.0) is None


def test_an_empty_timeline_is_safe_to_click(qt_app: QApplication) -> None:
    """A video opened before it was ever tagged still has a working scrub bar."""
    del qt_app
    timeline = TagTimeline(TOKENS)
    timeline.resize(216, 46)
    assert timeline.moment_near(50.0) is None
    assert timeline.seconds_at(50.0) == 0.0, "no duration yet means nowhere to seek"

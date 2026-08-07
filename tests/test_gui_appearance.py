from __future__ import annotations

import json
from pathlib import Path

from mediaengine.config import Config
from mediaengine.gui.appearance import (
    THEMES,
    UISettings,
    contrast_ratio,
    load_ui_settings,
    mix_color,
    save_ui_settings,
)


def test_theme_text_colors_meet_wcag_aa() -> None:
    for theme in THEMES.values():
        assert contrast_ratio(theme.text, theme.bg) >= 4.5, theme.name
        assert contrast_ratio(theme.muted, theme.bg) >= 4.5, theme.name
        assert contrast_ratio(theme.text, theme.panel) >= 4.5, theme.name
        assert contrast_ratio(theme.accent, theme.panel) >= 4.5, theme.name
        assert contrast_ratio(theme.header_text, theme.header_start) >= 4.5, theme.name


def test_color_interpolation_is_stable() -> None:
    assert mix_color("#000000", "#ffffff", 0.0) == "#000000"
    assert mix_color("#000000", "#ffffff", 1.0) == "#ffffff"
    assert mix_color("#000000", "#ffffff", 0.5) == "#808080"


def test_ui_settings_round_trip_and_normalize(tmp_path: Path) -> None:
    config = Config()
    config.source_path = tmp_path / "config.yaml"
    settings = UISettings(
        theme="Midnight Ink",
        text_scale=120,
        density="Cozy",
        reduced_motion=True,
        tutorial_completed=True,
    )
    path = save_ui_settings(config, settings)

    assert path == tmp_path / "ui.json"
    assert load_ui_settings(config) == settings

    path.write_text(json.dumps({"theme": "missing", "text_scale": 900}), encoding="utf-8")
    normalized = load_ui_settings(config)
    assert normalized.theme == "Claude Light"
    assert normalized.text_scale == 140

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from mediaengine.config import Config
from mediaengine.gui.appearance import contrast_ratio
from mediaengine.studio.components import AnimatedButton, AssetCard, human_duration, human_size
from mediaengine.studio.dialogs import (
    AnalyzerStoreDialog,
    EraseConfirmDialog,
    FilterPackCard,
    SettingsDialog,
    is_temporary_location,
)
from mediaengine.studio.tokens import (
    AUTO_PALETTE,
    PALETTES,
    TOKENS,
    palette,
    palette_choices,
    palette_names,
    system_is_dark,
)
from mediaengine.studio.window import StudioWindow, _as_float, _decorate, _thumbnail_for


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_studio_core_tokens_are_accessible() -> None:
    assert contrast_ratio(TOKENS.ink, TOKENS.canvas) >= 4.5
    assert contrast_ratio(TOKENS.ink_soft, TOKENS.canvas) >= 4.5
    assert contrast_ratio(TOKENS.brand, TOKENS.surface) >= 4.5


@pytest.mark.parametrize("name", list(PALETTES))
def test_every_studio_palette_has_accessible_core_contrast(name: str) -> None:
    tokens = PALETTES[name]
    assert contrast_ratio(tokens.ink, tokens.canvas) >= 4.5
    assert contrast_ratio(tokens.ink_soft, tokens.canvas) >= 4.5
    assert contrast_ratio(tokens.brand, tokens.surface) >= 4.5
    assert contrast_ratio(tokens.on_brand, tokens.brand) >= 4.5


@pytest.mark.parametrize("name", list(PALETTES))
def test_gradient_stops_never_break_body_text(name: str) -> None:
    """Text sits on top of the canvas wash, so every stop must stay legible."""
    tokens = PALETTES[name]
    for stop in tokens.glow_stops:
        assert contrast_ratio(tokens.ink, stop) >= 4.5, f"{name}: ink on {stop}"
        assert contrast_ratio(tokens.ink_soft, stop) >= 4.5, f"{name}: ink_soft on {stop}"


@pytest.mark.parametrize("name", list(PALETTES))
def test_accent_is_readable_as_a_gradient_end_stop(name: str) -> None:
    """``on_brand`` labels run across brand→accent fills, so both ends count."""
    tokens = PALETTES[name]
    assert contrast_ratio(tokens.on_brand, tokens.accent) >= 4.5
    assert contrast_ratio(tokens.brand, tokens.brand_soft) >= 4.5


@pytest.mark.parametrize("name", list(PALETTES))
@pytest.mark.parametrize("chosen", ["#FFF200", "#FF00E6", "#0A0A0A", "#FFFFFF", "#7EC8E3"])
def test_custom_accent_stays_readable(name: str, chosen: str) -> None:
    """A user may pick any colour; Studio must still meet AA against surfaces."""
    tokens = PALETTES[name].with_accent(chosen)
    assert contrast_ratio(tokens.brand, tokens.surface) >= 4.5
    assert contrast_ratio(tokens.on_brand, tokens.brand) >= 4.5


def test_palette_names_lists_light_themes_before_dark() -> None:
    names = palette_names()
    darkness = [PALETTES[name].dark for name in names]
    assert sorted(darkness) == darkness
    assert set(names) == set(PALETTES)


def test_intensity_and_density_are_clamped() -> None:
    assert TOKENS.with_intensity(4.0).intensity == 1.0
    assert TOKENS.with_intensity(-2.0).intensity == 0.0
    flat = TOKENS.with_density(0.0)
    assert flat.radius_sm >= 2 and flat.radius_lg >= 4
    assert TOKENS.with_density(1.4).radius_lg > TOKENS.radius_lg


def test_reduced_motion_removes_every_duration() -> None:
    still = TOKENS.without_motion()
    assert (still.motion_fast, still.motion_normal, still.motion_slow) == (0, 0, 0)


def test_studio_human_metadata() -> None:
    assert human_size(2_621_440) == "2.5 MB"
    assert human_duration(65) == "1:05"
    assert human_duration(3661) == "1:01:01"


def test_thumbnail_resolution_stays_inside_cache(tmp_path: Path) -> None:
    config = Config()
    config.storage.derivatives_path = tmp_path / "cache"
    thumb = config.storage.derivatives_path / "aa" / "thumb.webp"
    thumb.parent.mkdir(parents=True)
    thumb.write_bytes(b"not-an-image")
    assert _thumbnail_for(config, [{"kind": "thumb", "variant": "512", "rel_path": "aa/thumb.webp"}]) == str(thumb)
    assert _thumbnail_for(config, [{"kind": "thumb", "variant": "512", "rel_path": "../outside.webp"}]) is None


def test_temporary_library_locations_are_detected(tmp_path: Path) -> None:
    assert is_temporary_location(Path(tempfile.gettempdir()) / "tmpabc" / "library.db")
    assert not is_temporary_location(Path("C:/Users/someone/Pictures/library.db"))


def test_settings_are_read_back_from_strings() -> None:
    """QSettings hands back registry strings; appearance must survive that."""
    assert _as_float("0.35", 1.0) == pytest.approx(0.35)
    assert _as_float(0.5, 1.0) == pytest.approx(0.5)
    assert _as_float("not-a-number", 1.0) == 1.0
    assert _as_float(None, 0.8) == 0.8


def test_decorate_splits_analyzer_output_into_tags_and_summary() -> None:
    items = [{"id": 7}]
    _decorate(
        items,
        {
            7: [
                {"namespace": "llm.summary", "label": "generated", "value": {"text": "A dog runs."}},
                {"namespace": "llm.category", "label": "pets", "value": None},
                {"namespace": "llm.tag", "label": "dog", "value": None},
                {"namespace": "llm.tag", "label": "dog", "value": None},
            ]
        },
    )
    assert items[0]["ai_summary"] == "A dog runs."
    assert items[0]["tags"] == ["pets", "dog"]


def test_decorate_leaves_unanalyzed_assets_empty() -> None:
    items = [{"id": 1}]
    _decorate(items, {})
    assert items[0]["tags"] == []
    assert items[0]["ai_summary"] == ""


def test_animated_components_have_friendly_touch_targets(qt_app: QApplication) -> None:
    del qt_app
    button = AnimatedButton("Add folder", variant="primary")
    assert button.sizeHint().height() >= 42
    card = AssetCard({"id": 1, "filename": "clip.mp4", "media_type": "video", "duration_s": 65})
    assert card.minimumHeight() >= 250
    assert card.meta_label.text() == "1:05"


def test_asset_card_shows_analyzer_tags(qt_app: QApplication) -> None:
    del qt_app
    card = AssetCard({"id": 2, "filename": "a.mp4", "media_type": "video", "tags": ["beach", "sunset"]})
    assert card.tag_strip.tags == ["beach", "sunset"]
    assert card.tag_strip.isVisibleTo(card)
    bare = AssetCard({"id": 3, "filename": "b.mp4", "media_type": "video"})
    assert bare.tag_strip.tags == []


def test_reduced_motion_button_still_reaches_its_hover_state(qt_app: QApplication) -> None:
    """With animations off, state must change instantly rather than not at all."""
    del qt_app
    button = AnimatedButton("Run", variant="primary", tokens=TOKENS.without_motion())
    button._hover_to(1.0)
    assert button._hover_progress == 1.0


@pytest.fixture()
def studio(qt_app: QApplication, tmp_path: Path) -> StudioWindow:
    del qt_app
    config = Config()
    config.storage.db_path = tmp_path / "library.db"
    config.storage.derivatives_path = tmp_path / "derivatives"
    config.source_path = tmp_path / "config.yaml"
    window = StudioWindow(config)
    yield window
    window.close()


@pytest.mark.parametrize("name", list(PALETTES))
def test_studio_retheme_without_restart(studio: StudioWindow, name: str) -> None:
    """Every palette must survive a live swap; the old UI is fully replaced."""
    studio.apply_tokens(PALETTES[name])
    assert studio.tokens.name == name
    assert studio.canvas.tokens.name == name
    assert studio.inspector.tokens.name == name
    assert studio.status.tokens.name == name


def test_studio_live_preview_honours_accent_and_intensity(studio: StudioWindow) -> None:
    studio.preview_settings(
        {"palette": "Midnight", "accent": "#FF8A00", "intensity": 0.25, "density": 0.5}
    )
    assert studio.tokens.dark
    assert studio.tokens.intensity == pytest.approx(0.25)
    assert studio.tokens.brand != PALETTES["Midnight"].brand
    assert contrast_ratio(studio.tokens.on_brand, studio.tokens.brand) >= 4.5


def test_analyzer_store_reports_a_stale_model_id(qt_app: QApplication) -> None:
    """The failure that made LM Studio look broken must be visible, not silent."""
    del qt_app
    store = AnalyzerStoreDialog(TOKENS)
    store.set_model_state(
        {
            "cli_available": True,
            "served": ["qwen3-vl-8b"],
            "configured": "Qwen 3 Abl.",
            "models": [],
        }
    )
    assert "not currently serving" in store.model_banner.text()
    store.close()


def test_analyzer_store_explains_a_missing_cli(qt_app: QApplication) -> None:
    del qt_app
    store = AnalyzerStoreDialog(TOKENS)
    store.set_model_state({"cli_available": False, "served": [], "configured": "", "models": []})
    assert "command line tool" in store.model_banner.text()
    store.close()


def test_analyzer_store_defaults_expensive_runs_to_video_only(qt_app: QApplication) -> None:
    """Running a VLM over an entire library must be a choice, not the default."""
    del qt_app
    store = AnalyzerStoreDialog(TOKENS)
    store.set_catalog(
        [
            {
                "plugin_id": "local.lm-studio",
                "enabled": True,
                "accepts": ["image", "video", "document"],
                "tasks": {"pending": 1065, "failed": 151},
            }
        ]
    )
    captured: list[dict[str, object]] = []
    store.run_requested.connect(captured.append)
    card = store.rows.itemAt(0).widget()
    assert card.scope.currentText() == "Videos only"
    card._emit_run(retry_failed=True)
    assert captured[0]["media_types"] == ["video"]
    assert captured[0]["retry_failed"] is True
    store.close()


def test_settings_dialog_surfaces_a_temporary_library(qt_app: QApplication) -> None:
    del qt_app
    from PySide6.QtCore import QSettings

    dialog = SettingsDialog(
        QSettings("File Indexer", "StudioTest"),
        TOKENS,
        library_path=Path(tempfile.gettempdir()) / "tmpxyz" / "library.db",
    )
    labels = [child.text() for child in dialog.findChildren(type(dialog.accent_note))]
    assert any("temporary folder" in text for text in labels)
    dialog.close()


# ── theme switching ──────────────────────────────────────────────────────────


def test_the_theme_picker_offers_the_system_option_first() -> None:
    choices = palette_choices()
    assert choices[0] == AUTO_PALETTE
    assert choices[1:] == palette_names()


def test_matching_windows_resolves_to_a_real_palette(qt_app: QApplication) -> None:
    """Whatever the desktop is doing, a usable palette has to come back."""
    del qt_app
    tokens = palette(AUTO_PALETTE)
    assert tokens.name in PALETTES
    assert tokens.dark == system_is_dark()
    assert contrast_ratio(tokens.ink, tokens.canvas) >= 4.5


def test_an_unknown_stored_theme_falls_back_rather_than_crashing() -> None:
    assert palette("Theme From A Newer Version").name == TOKENS.name


def test_choosing_a_theme_applies_and_remembers_it(studio: StudioWindow) -> None:
    studio.choose_palette("Nebula")
    assert studio.tokens.name == "Nebula"
    assert studio.settings.value("palette") == "Nebula"
    assert studio.palette_choice == "Nebula"
    assert studio.theme_button.text() == "Nebula"


def test_following_the_system_theme_repaints_only_when_selected(
    studio: StudioWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mediaengine.studio.tokens.system_is_dark", lambda: True)
    studio.choose_palette("Sage")
    studio._system_theme_changed()
    assert studio.tokens.name == "Sage", "an explicit choice must not be overridden"

    studio.choose_palette(AUTO_PALETTE)
    studio._system_theme_changed()
    assert studio.tokens.dark, "following the system means following it into dark"


# ── library location ─────────────────────────────────────────────────────────


def _settings_dialog(**kwargs: Any) -> SettingsDialog:
    from PySide6.QtCore import QSettings

    return SettingsDialog(QSettings("File Indexer", "StudioTest"), TOKENS, **kwargs)


def test_the_library_tab_offers_both_ways_to_change_location(qt_app: QApplication) -> None:
    del qt_app
    dialog = _settings_dialog(
        usage={
            "home": "D:/Library",
            "db_path": "D:/Library/library.db",
            "db_bytes": 24 * 1024**2,
            "derivatives_bytes": 3 * 1024**3,
            "free_bytes": 500 * 1024**3,
        }
    )
    modes: list[str] = []
    dialog.relocate_requested.connect(modes.append)
    buttons = {child.text(): child for child in dialog.findChildren(AnimatedButton)}
    buttons["Move library…"].click()
    buttons["Use another library…"].click()
    assert modes == ["move", "adopt"]
    # The size is shown, because "move my library" is a question about space.
    assert "24.0 MB" in dialog._usage_text() and "3.0 GB" in dialog._usage_text()
    dialog.close()


def test_the_library_tab_works_before_anything_is_indexed(qt_app: QApplication) -> None:
    del qt_app
    dialog = _settings_dialog()
    assert "Nothing has been indexed" in dialog._usage_text()
    dialog.close()


# ── erasing everything ───────────────────────────────────────────────────────


def test_erasing_needs_the_word_typed_out(qt_app: QApplication) -> None:
    """A yes/no box is muscle memory; this one must not be."""
    del qt_app
    dialog = EraseConfirmDialog("40 GB will be deleted.", TOKENS)
    assert not dialog.confirm.isEnabled()
    dialog.entry.setText("erase everything")
    assert not dialog.confirm.isEnabled()
    dialog.entry.setText("erase")
    assert dialog.confirm.isEnabled(), "case must not be the obstacle"
    dialog.entry.setText("")
    assert not dialog.confirm.isEnabled()
    dialog.close()


def test_the_reset_tab_separates_preferences_from_erasing_everything(
    qt_app: QApplication,
) -> None:
    del qt_app
    dialog = _settings_dialog()
    fired: list[str] = []
    dialog.reset_requested.connect(lambda: fired.append("preferences"))
    dialog.erase_everything_requested.connect(lambda: fired.append("erase"))
    buttons = {child.text(): child for child in dialog.findChildren(AnimatedButton)}
    buttons["Erase library and settings"].click()
    assert fired == ["erase"], "erasing must not be reachable by the preferences button"
    dialog.close()


def test_resetting_preferences_can_stay_quiet(studio: StudioWindow) -> None:
    """The master reset shows its own message; two toasts would fight."""
    studio.choose_palette("Carbon")
    studio.reset_preferences(notify=False)
    assert studio.settings.value("palette") is None
    assert studio.tokens.name == TOKENS.name


# ── filter packs in the store ────────────────────────────────────────────────


def _pack_row(pack_id: str, name: str, *, builtin: bool) -> dict[str, Any]:
    return {
        "pack_id": pack_id,
        "name": name,
        "builtin": builtin,
        "enabled": False,
        "method": "vision" if builtin else "rules",
        "facet": "Genre" if builtin else "Mine",
        "accepts": ["video"],
        "labels": [{"name": "movies", "display": "Movies"}],
    }


def test_only_user_installed_filters_offer_a_delete_button(qt_app: QApplication) -> None:
    del qt_app
    store = AnalyzerStoreDialog(TOKENS)
    store.set_pack_state(
        {
            "packs": [
                _pack_row("filters.genre", "Video genres", builtin=True),
                _pack_row("custom.mine", "My filter", builtin=False),
            ],
            "errors": {},
            "directory": "C:/packs",
        }
    )
    cards = [
        store.pack_rows.itemAt(index).widget() for index in range(store.pack_rows.count() - 1)
    ]
    shipped, custom = cards[0], cards[1]
    assert isinstance(shipped, FilterPackCard)
    shipped_buttons = [item.text() for item in shipped.findChildren(AnimatedButton)]
    custom_buttons = [item.text() for item in custom.findChildren(AnimatedButton)]
    assert "Delete" not in shipped_buttons, "a shipped filter is part of the app"
    assert "Delete" in custom_buttons
    store.close()


def test_the_shipped_genre_filter_reaches_the_store(qt_app: QApplication) -> None:
    """The taxonomy the store advertises must be the one the engine loaded."""
    del qt_app
    from mediaengine.filters import discover_packs

    pack = discover_packs(Config()).packs["filters.genre"]
    store = AnalyzerStoreDialog(TOKENS)
    store.set_pack_state(
        {
            "packs": [
                {
                    "pack_id": pack.id,
                    "name": pack.name,
                    "builtin": True,
                    "enabled": False,
                    "method": pack.method,
                    "facet": pack.facet_title,
                    "accepts": list(pack.accepts),
                    "description": pack.description,
                    "labels": [{"name": item.name, "display": item.title} for item in pack.labels],
                }
            ],
            "errors": {},
            "directory": "C:/packs",
        }
    )
    store.pack_search.setText("genre")
    card = store.pack_rows.itemAt(0).widget()
    assert isinstance(card, FilterPackCard)
    assert card.pack["pack_id"] == "filters.genre"
    store.pack_search.setText("nothing matches this")
    assert store.pack_rows.count() == 1
    store.close()

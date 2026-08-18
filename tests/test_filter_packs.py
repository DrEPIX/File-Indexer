"""Filter packs: parsing, the rules engine, vocabulary, and registration.

A pack is data a user is expected to write by hand, so the failure modes worth
covering are bad TOML, a regex that is too greedy, and a model answering
outside the vocabulary — not the happy path.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from mediaengine.config import Config
from mediaengine.engine import MediaEngine
from mediaengine.errors import ConfigError
from mediaengine.filters import (
    BUILTIN_DIR,
    FilterPackAnalyzer,
    build_vocabulary,
    discover_packs,
    install_pack,
    load_pack,
    pack_path,
    parse_pack,
    remove_pack,
    user_pack_dir,
)
from mediaengine.filters.vocabulary import normalize_alias

from .conftest import make_config, write_jpeg

MINIMAL = """
[pack]
id = "test.demo"
name = "Demo"
version = "1.0.0"
namespace = "demo.kind"
method = "rules"
accepts = ["video"]

[[label]]
name = "alpha"
display = "Alpha"
aliases = ["first"]
[[label.rule]]
field = "filename"
pattern = "alpha"
"""


def _pack(text: str = MINIMAL) -> Any:
    return parse_pack(tomllib.loads(text), source="test.toml")


# ── parsing and validation ───────────────────────────────────────────────────


def test_a_minimal_pack_parses() -> None:
    pack = _pack()
    assert pack.id == "test.demo"
    assert pack.namespace == "demo.kind"
    assert pack.label_names == ("alpha",)
    assert pack.label("alpha") is not None
    assert pack.label("missing") is None


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("[pack]\nid = \"BAD ID\"\nnamespace = \"a\"\n[[label]]\nname=\"x\"", r"\[pack\]\.id"),
        ("[pack]\nid = \"ok\"\nnamespace = \"Bad NS\"\n[[label]]\nname=\"x\"", r"\[pack\]\.namespace"),
        ("[pack]\nid=\"ok\"\nnamespace=\"n\"\nmethod=\"telepathy\"\n[[label]]\nname=\"x\"", "method"),
        ("[pack]\nid=\"ok\"\nnamespace=\"n\"\naccepts=[\"hologram\"]\n[[label]]\nname=\"x\"", "media types"),
        ("[pack]\nid=\"ok\"\nnamespace=\"n\"\nthreshold=4\n[[label]]\nname=\"x\"", "threshold"),
        ("[pack]\nid=\"ok\"\nnamespace=\"n\"", "at least one"),
    ],
)
def test_bad_packs_are_rejected_with_a_useful_message(mutation: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_pack(tomllib.loads(mutation), source="bad.toml")


def test_duplicate_labels_are_rejected() -> None:
    text = MINIMAL + '\n[[label]]\nname = "alpha"\n'
    with pytest.raises(ConfigError, match="duplicate"):
        _pack(text)


def test_an_invalid_regex_names_the_label() -> None:
    text = MINIMAL.replace('pattern = "alpha"', 'pattern = "alpha("')
    with pytest.raises(ConfigError, match="alpha"):
        _pack(text)


def test_an_unknown_rule_field_is_rejected() -> None:
    text = MINIMAL.replace('field = "filename"', 'field = "vibes"')
    with pytest.raises(ConfigError, match="vibes"):
        _pack(text)


def test_missing_pack_table_is_rejected() -> None:
    """The message has to name the fix; a model reads it and tries again."""
    with pytest.raises(ConfigError, match=r"needs a \[pack\] table"):
        parse_pack({"label": []}, source="x.toml")


def test_double_bracketed_pack_header_is_named_exactly() -> None:
    """Observed live: a model wrote [[pack]] six times and never recovered."""
    with pytest.raises(ConfigError, match="double brackets"):
        parse_pack(tomllib.loads('[[pack]]\nid = "a"\nnamespace = "n"\n'), source="x")


def test_single_bracketed_label_header_is_named_exactly() -> None:
    with pytest.raises(ConfigError, match="single brackets"):
        parse_pack(
            tomllib.loads('[pack]\nid = "a"\nnamespace = "n"\n[label]\nname = "x"\n'), source="x"
        )


def test_validation_messages_lead_with_the_problem() -> None:
    """A path-first message gets truncated before the reason reaches anyone."""
    with pytest.raises(ConfigError) as caught:
        parse_pack({"pack": {"name": "x"}}, source="C:/a/very/long/path/pack.toml")
    assert str(caught.value).startswith("[pack].id")


# ── versioning ───────────────────────────────────────────────────────────────


def test_editing_a_pack_changes_its_plugin_version() -> None:
    """Supersession rides on the version, so content must feed into it."""
    before = _pack().plugin_version
    after = _pack(MINIMAL.replace('pattern = "alpha"', 'pattern = "alphabet"')).plugin_version
    assert before != after
    assert before.startswith("1.0.0+") and after.startswith("1.0.0+")


def test_cosmetic_edits_do_not_change_the_version() -> None:
    """Renaming the display text must not re-analyse the whole library."""
    before = _pack().plugin_version
    after = _pack(MINIMAL.replace('display = "Alpha"', 'display = "Alpha Team"')).plugin_version
    assert before == after


# ── the rules engine ─────────────────────────────────────────────────────────


class _Ctx:
    """The slice of AnalysisContext a rules pack reads."""

    def __init__(self, filename: str, *, path: str = "", text: str = "", encoder: str = "") -> None:
        self.filename = filename
        self.media_type = "video"
        self.mime_type = "video/mp4"
        self._path = Path(path or f"C:/media/{filename}")
        self.text: str | None = text or None
        self.metadata: dict[str, Any] = {"encoder": encoder} if encoder else {}
        self.config: dict[str, Any] = {}

    @property
    def path(self) -> Path:
        return self._path


def _origin() -> FilterPackAnalyzer:
    return FilterPackAnalyzer(load_pack(BUILTIN_DIR / "origin-platform.toml", builtin=True))


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("My Twitch Stream 2024.mp4", "twitch"),
        ("Some Video [dQw4w9WgXcQ].webm", "youtube"),
        ("yt-dlp download.mkv", "youtube"),
        ("2024-03-11 21-14-07.mkv", "screen-recording"),
        ("Replay 2024-03-11 21-14-07.mp4", "screen-recording"),
        ("PXL_20240108_072803.jpg", "phone-camera"),
        ("20240108_072803.jpg", "phone-camera"),
        ("DSC01234.ARW", "camera"),
        ("GX010042.MP4", "camera"),
        ("IMG-20240101-WA0002.jpg", "messaging"),
    ],
)
def test_origin_rules_classify_real_naming_conventions(filename: str, expected: str) -> None:
    found = _origin().analyze(_Ctx(filename))
    assert [item.label for item in found] == [expected], filename


@pytest.mark.parametrize(
    "filename",
    [
        # A GUID satisfies "letter, four digits, three more digits" halfway
        # through unless the camera rule is anchored to the extension.
        "C9550013-BB54-43C1-BDD5-831C704B7321.png",
        "1rzbr53tgln61.png",
        "1_Cover_05.jpg",
        "holiday.mp4",
    ],
)
def test_origin_rules_stay_quiet_on_ordinary_filenames(filename: str) -> None:
    assert _origin().analyze(_Ctx(filename)) == []


def test_underscores_do_not_hide_a_keyword() -> None:
    """`\\btwitch\\b` misses `stream_twitch.mp4`, because `_` is a word char."""
    assert [item.label for item in _origin().analyze(_Ctx("stream_twitch.mp4"))] == ["twitch"]
    assert [item.label for item in _origin().analyze(_Ctx("my_obs_capture.mkv"))] == [
        "screen-recording"
    ]


def test_rule_confidence_reflects_how_much_evidence_matched() -> None:
    """One weak hint and four corroborating signals are different claims."""
    weak = _origin().analyze(_Ctx("stream_twitch.mp4"))
    strong = _origin().analyze(
        _Ctx("twitch v123456789 streamlink.mp4", path="D:/twitch/twitch v123456789 streamlink.mp4")
    )
    assert weak and strong
    assert strong[0].confidence > weak[0].confidence
    assert 0.5 <= weak[0].confidence < 1.0
    # A regex over a filename is a hint, never a proof.
    assert strong[0].confidence < 1.0


def test_rule_matches_record_their_evidence() -> None:
    found = _origin().analyze(_Ctx("My Twitch Stream.mp4"))
    value = found[0].value or {}
    assert value["method"] == "rules"
    assert any("twitch" in item.lower() for item in value["evidence"])


def test_a_rules_pack_reads_the_path_as_well_as_the_name() -> None:
    found = _origin().analyze(_Ctx("clip.mp4", path="D:/Videos/Twitch/clip.mp4"))
    assert [item.label for item in found] == ["twitch"]


def test_single_label_packs_return_only_the_best_match() -> None:
    found = _origin().analyze(_Ctx("twitch youtube obs PXL_20240101_101010.mp4"))
    assert len(found) == 1


def test_multi_label_packs_return_every_match() -> None:
    text = MINIMAL.replace("[[label]]", "multi_label = true\n\n[[label]]", 1) + (
        '\n[[label]]\nname = "beta"\n[[label.rule]]\nfield = "filename"\npattern = "beta"\n'
    )
    analyzer = FilterPackAnalyzer(_pack(text))
    found = analyzer.analyze(_Ctx("alpha-beta.mp4"))
    assert {item.label for item in found} == {"alpha", "beta"}


def test_rules_below_the_threshold_are_dropped() -> None:
    text = MINIMAL.replace("[[label]]", "threshold = 0.99\n\n[[label]]", 1)
    assert FilterPackAnalyzer(_pack(text)).analyze(_Ctx("alpha.mp4")) == []


# ── the model-backed schema ──────────────────────────────────────────────────


def _sport() -> FilterPackAnalyzer:
    return FilterPackAnalyzer(load_pack(BUILTIN_DIR / "content-sport.toml", builtin=True))


def test_the_model_is_constrained_to_the_pack_vocabulary() -> None:
    """An enum is what stops a facet filling up with invented spellings."""
    schema = _sport()._schema()["json_schema"]["schema"]
    enum = schema["properties"]["label"]["enum"]
    assert "football" in enum and "unknown" in enum
    assert "association football" not in enum
    assert schema["additionalProperties"] is False


def test_a_label_outside_the_vocabulary_is_discarded() -> None:
    found = _sport()._annotations({"label": "quidditch", "confidence": 0.9}, "m")
    assert found == []


def test_a_declined_answer_produces_no_claim() -> None:
    assert _sport()._annotations({"label": "unknown", "confidence": 0.9}, "m") == []


def test_a_low_confidence_answer_is_dropped() -> None:
    assert _sport()._annotations({"label": "football", "confidence": 0.01}, "m") == []


def test_a_confident_answer_becomes_an_annotation() -> None:
    found = _sport()._annotations({"label": "football", "confidence": 0.88}, "qwen3-vl")
    assert len(found) == 1
    assert found[0].namespace == "content.sport"
    assert found[0].label == "football"
    assert found[0].confidence == pytest.approx(0.88)
    assert (found[0].value or {})["model"] == "qwen3-vl"


def test_a_non_numeric_confidence_falls_back_rather_than_raising() -> None:
    found = _sport()._annotations({"label": "tennis", "confidence": "very"}, "m")
    assert found and found[0].confidence == pytest.approx(0.5)


def test_vision_packs_declare_that_they_need_pixels_and_network() -> None:
    info = _sport().info
    assert info.pixels and info.frames and info.network
    assert info.emits == ("content.sport",)
    assert "content.sport" in info.namespaces


def test_rules_packs_declare_that_they_need_neither() -> None:
    info = _origin().info
    assert not info.pixels and not info.network
    assert info.metadata_only


# ── vocabulary ───────────────────────────────────────────────────────────────


def test_aliases_reach_their_canonical_label() -> None:
    vocab = build_vocabulary(discover_packs(Config()).packs.values())
    assert vocab.resolve_label("footy") == ("content.sport", "football")
    assert vocab.resolve_label("weeb") == ("content.animation", "anime")
    assert vocab.resolve_label("yt") == ("origin.platform", "youtube")


def test_a_namespace_answers_to_its_short_name() -> None:
    vocab = build_vocabulary(discover_packs(Config()).packs.values())
    assert vocab.resolve_namespace("sport") == "content.sport"
    assert vocab.resolve_namespace("platform") == "origin.platform"
    assert vocab.resolve_namespace("content.sport") == "content.sport"
    assert vocab.resolve_namespace("nonsense") is None


def test_expansion_is_symmetric() -> None:
    vocab = build_vocabulary(discover_packs(Config()).packs.values())
    assert "football" in vocab.expand("soccer")
    assert "soccer" in vocab.expand("football")


def test_display_decoration_never_reaches_the_index() -> None:
    """`Football (soccer)` as a phrase query would match nothing at all."""
    assert normalize_alias("Football (soccer)") == "football"
    assert normalize_alias("Rugby  Union!") == "rugby union"
    vocab = build_vocabulary(discover_packs(Config()).packs.values())
    assert not any("(" in term for term in vocab.expand("football"))


def test_an_empty_vocabulary_is_harmless() -> None:
    vocab = build_vocabulary([])
    assert vocab.resolve_label("footy") is None
    # Base synonyms still apply with no packs installed.
    assert "picture" in vocab.expand("photo")


# ── discovery and registration ───────────────────────────────────────────────


def test_every_shipped_pack_is_valid() -> None:
    paths = sorted(BUILTIN_DIR.glob("*.toml"))
    assert len(paths) >= 5
    for path in paths:
        pack = load_pack(path, builtin=True)
        assert pack.labels
        assert pack.builtin
        # Vision packs must give the model something to go on for every label.
        if pack.method == "vision":
            assert all(label.hint for label in pack.labels), pack.id


def test_the_requested_filter_axes_all_ship() -> None:
    packs = {pack.id: pack for pack in discover_packs(Config()).packs.values()}
    assert {"filters.sport", "filters.animation", "filters.format", "filters.genre",
            "filters.origin", "filters.safety-nsfw"} <= set(packs)
    assert packs["filters.origin"].method == "rules"
    assert "twitch" in packs["filters.origin"].label_names
    assert "youtube" in packs["filters.origin"].label_names


def test_the_genre_pack_covers_every_requested_shelf() -> None:
    """Genre is one axis with one value per asset, and it must decline cleanly."""
    pack = discover_packs(Config()).packs["filters.genre"]
    assert {
        "art", "sports", "memes", "games", "gameshows", "news",
        "live-tv", "recorded-tv", "shows", "movies", "adult",
    } <= set(pack.label_names)
    assert pack.accepts == ("video",)
    assert not pack.multi_label
    # "none" exists so the model can decline, and is discarded rather than
    # becoming a facet value nobody would ever filter on.
    assert "none" in pack.label_names
    assert "none" in pack.reject_labels


def test_genre_words_people_actually_type_reach_the_pack() -> None:
    aliases = discover_packs(Config()).packs["filters.genre"].alias_map()
    for typed, expected in (
        ("film", "movies"),
        ("gaming", "games"),
        ("quiz show", "gameshows"),
        ("nsfw", "adult"),
        ("dvr", "recorded-tv"),
    ):
        assert aliases[typed] == ("content.genre", expected)


def test_a_broken_user_pack_is_reported_not_swallowed(tmp_path: Path) -> None:
    config = Config()
    config.source_path = tmp_path / "config.yaml"
    packs_dir = tmp_path / "filter-packs"
    packs_dir.mkdir()
    (packs_dir / "broken.toml").write_text("[pack]\nid = \"nope\"\n", encoding="utf-8")
    discovery = discover_packs(config)
    assert any("broken.toml" in path for path in discovery.errors)
    # The shipped packs still loaded.
    assert "filters.sport" in discovery.packs


def test_a_user_pack_shadows_a_shipped_one(tmp_path: Path) -> None:
    config = Config()
    config.source_path = tmp_path / "config.yaml"
    packs_dir = tmp_path / "filter-packs"
    packs_dir.mkdir()
    (packs_dir / "mine.toml").write_text(
        MINIMAL.replace('id = "test.demo"', 'id = "filters.sport"'), encoding="utf-8"
    )
    packs = discover_packs(config).packs
    assert packs["filters.sport"].name == "Demo"
    assert not packs["filters.sport"].builtin


def test_packs_register_as_analyzers(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    engine = MediaEngine(config).start()
    try:
        rows = {
            str(row["plugin_id"]): row
            for row in engine.plugins.describe()
            if str(row["plugin_id"]).startswith("filters.")
        }
        assert "filters.sport" in rows
        assert rows["filters.sport"]["emits"] == ["content.sport"]
        # Discovered, but off until asked for.
        assert rows["filters.sport"]["enabled"] is False
        assert engine.plugins.packs["filters.sport"].namespace == "content.sport"
    finally:
        engine.close()


def test_an_enabled_pack_runs_over_the_library(tmp_path: Path) -> None:
    """The whole point: a TOML file becomes annotations and therefore a facet."""
    root = tmp_path / "media"
    root.mkdir()
    # Different colours: identical bytes would be one asset with two paths,
    # which is correct dedup behaviour but not what this test is about.
    write_jpeg(root / "PXL_20240108_072803.jpg", colour=(10, 20, 30))
    write_jpeg(root / "ordinary.jpg", colour=(200, 100, 50))
    config = make_config(tmp_path, root)
    config.plugins.enabled = ["filters.origin"]
    engine = MediaEngine(config).start()
    try:
        engine.scan([root], generate_derivatives=False)
        result = engine.backfill(["filters.origin"])
        assert result.completed >= 2
        facet = engine.repos.annotations.facet("origin.platform")
        assert {row["label"] for row in facet} == {"phone-camera"}
        hits = engine.search("origin.platform:phone-camera")
        assert hits.total == 1
    finally:
        engine.close()


# ── installing and removing packs ────────────────────────────────────────────


def test_installing_a_pack_makes_it_discoverable_but_not_active(tmp_path: Path) -> None:
    """Adding a taxonomy must never start spending a GPU on its own."""
    config = make_config(tmp_path)
    config.source_path = tmp_path / "config.yaml"
    downloaded = tmp_path / "downloads" / "whatever.toml"
    downloaded.parent.mkdir()
    downloaded.write_text(MINIMAL, encoding="utf-8")

    installed = install_pack(config, downloaded)

    assert installed.id == "test.demo"
    assert not installed.builtin
    assert pack_path(config, "test.demo").is_file()
    assert "test.demo" in discover_packs(config).packs
    assert not config.plugins.is_enabled("test.demo")


def test_a_broken_pack_is_rejected_before_it_is_copied(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.source_path = tmp_path / "config.yaml"
    bad = tmp_path / "bad.toml"
    bad.write_text('[pack]\nid = "test.bad"\n', encoding="utf-8")

    with pytest.raises(ConfigError):
        install_pack(config, bad)
    assert not user_pack_dir(config).exists() or not any(user_pack_dir(config).glob("*.toml"))


def test_installing_over_an_existing_pack_needs_permission(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.source_path = tmp_path / "config.yaml"
    source = tmp_path / "demo.toml"
    source.write_text(MINIMAL, encoding="utf-8")
    install_pack(config, source)

    with pytest.raises(ConfigError, match="already installed"):
        install_pack(config, source)

    source.write_text(MINIMAL.replace('name = "Demo"', 'name = "Demo two"'), encoding="utf-8")
    assert install_pack(config, source, overwrite=True).name == "Demo two"


def test_removing_a_pack_takes_its_file_and_leaves_the_shipped_ones(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.source_path = tmp_path / "config.yaml"
    source = tmp_path / "demo.toml"
    source.write_text(MINIMAL, encoding="utf-8")
    install_pack(config, source)

    assert remove_pack(config, "test.demo") is not None
    assert "test.demo" not in discover_packs(config).packs
    assert remove_pack(config, "test.demo") is None
    # The shipped taxonomies are part of the application, not user files.
    assert "filters.genre" in discover_packs(config).packs


def test_a_hand_dropped_pack_is_found_by_its_declared_id(tmp_path: Path) -> None:
    """Someone copying a file in by hand will not use our naming convention."""
    config = make_config(tmp_path)
    config.source_path = tmp_path / "config.yaml"
    directory = user_pack_dir(config)
    directory.mkdir(parents=True)
    (directory / "my-own-name.toml").write_text(MINIMAL, encoding="utf-8")

    removed = remove_pack(config, "test.demo")

    assert removed is not None and removed.name == "my-own-name.toml"


def test_the_manager_refuses_to_delete_a_shipped_pack(tmp_path: Path) -> None:
    from mediaengine.plugins import PluginManager

    config = make_config(tmp_path)
    config.source_path = tmp_path / "config.yaml"
    engine = MediaEngine(config).start()
    try:
        manager = PluginManager(engine)
        with pytest.raises(ConfigError, match="ships with the application"):
            manager.remove_filter_pack("filters.genre")
    finally:
        engine.close()


def test_the_manager_installs_enables_and_removes_in_one_step(tmp_path: Path) -> None:
    from mediaengine.plugins import PluginManager

    config = make_config(tmp_path)
    config.source_path = tmp_path / "config.yaml"
    source = tmp_path / "demo.toml"
    source.write_text(MINIMAL, encoding="utf-8")
    engine = MediaEngine(config).start()
    try:
        manager = PluginManager(engine)
        summary = manager.install_filter_pack(source, enable=True)
        assert summary["pack_id"] == "test.demo"
        assert summary["enabled"] is True
        assert engine.plugins.get("test.demo") is not None

        manager.remove_filter_pack("test.demo")
        assert "test.demo" not in config.plugins.enabled
        assert engine.plugins.packs.get("test.demo") is None
    finally:
        engine.close()

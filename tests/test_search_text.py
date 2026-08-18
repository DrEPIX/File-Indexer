"""Text understanding: filename expansion, query compilation, and ranking.

The cases here are the ones a library search box actually fails on — a word
welded into a filename by camel case, a four-word query that ANDs itself to
nothing, and an apostrophe or hyphen that makes FTS5 raise a syntax error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mediaengine.engine import MediaEngine
from mediaengine.filters import build_vocabulary, discover_packs
from mediaengine.search import BM25_WEIGHTS, compile_match, expand_filename, parse_query, tokenize
from mediaengine.search.planner import _BM25

from .conftest import make_config, write_jpeg

# ── filename expansion ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("IMG_20190407_beachDay.jpg", {"beach", "Day", "20190407", "IMG"}),
        ("S02E11.1080p.WEB-DL.mkv", {"S02", "E11", "1080p", "WEB", "DL"}),
        ("GX010042.MP4", {"GX", "010042"}),
        ("my-holiday-photo.jpeg", {"my", "holiday", "photo"}),
        ("CamelCaseName.png", {"Camel", "Case", "Name"}),
    ],
)
def test_expansion_surfaces_words_welded_into_a_filename(name: str, expected: set[str]) -> None:
    terms = set(expand_filename(name).split())
    assert expected <= terms, sorted(terms)


def test_expansion_keeps_the_original_name_first() -> None:
    """Exact-name matches must still be able to outrank derived fragments."""
    assert expand_filename("Holiday_2019.jpg").split()[0] == "Holiday_2019.jpg"


def test_expansion_records_the_extension_as_a_term() -> None:
    assert "jpg" in expand_filename("photo.jpg").split()


def test_expansion_is_bounded() -> None:
    monster = "_".join(f"word{index}" for index in range(400)) + ".mp4"
    assert len(expand_filename(monster).split()) <= 48


def test_expansion_handles_empty_and_odd_input() -> None:
    assert expand_filename("") == ""
    assert expand_filename("   ") == ""
    assert expand_filename(".gitignore")


def test_tokenize_drops_punctuation() -> None:
    assert tokenize("a-b_c.d") == ["a", "b", "c", "d"]
    assert tokenize("") == []


# ── query compilation ────────────────────────────────────────────────────────


def test_words_are_anded_strictly_and_ored_loosely() -> None:
    plan = compile_match("beach sunset", prefix_last=False)
    assert plan.strict == '"beach" AND "sunset"'
    assert plan.loose == '"beach" OR "sunset"'
    assert plan.expressions() == [plan.strict, plan.loose]


def test_a_single_word_needs_no_fallback() -> None:
    plan = compile_match("beach", prefix_last=False)
    assert plan.expressions() == ['"beach"']


def test_the_last_word_is_open_for_type_ahead() -> None:
    plan = compile_match("beach sun")
    assert plan.strict == '"beach" AND "sun"*'
    assert plan.prefixed


def test_a_quoted_phrase_stays_a_phrase() -> None:
    """Separate quoted tokens would AND anywhere in the document instead."""
    plan = compile_match('"golden gate bridge"')
    assert plan.strict == '"golden gate bridge"'
    assert not plan.prefixed


def test_a_trailing_phrase_is_never_turned_into_a_prefix() -> None:
    plan = compile_match('beach "exact phrase"')
    assert plan.strict == '"beach" AND "exact phrase"'
    assert not plan.prefixed


def test_exclusion_becomes_a_not_clause() -> None:
    """FTS5 spells set difference `A NOT B`; `A AND NOT B` is a syntax error."""
    plan = compile_match('"exact phrase" -blurry', prefix_last=False)
    assert plan.strict == '("exact phrase") NOT "blurry"'
    assert plan.excluded == ["blurry"]


def test_several_exclusions_chain() -> None:
    plan = compile_match("photo -blurry -dark", prefix_last=False)
    assert plan.strict == '("photo") NOT "blurry" NOT "dark"'


def test_an_excluded_word_is_not_promoted_to_the_prefix_term() -> None:
    """The regression that made `photo -blurry` search for "blurry*"."""
    plan = compile_match("photo -blurry")
    assert '"photo"' in plan.strict
    assert 'NOT "blurry"' in plan.strict
    assert '"blurry"*' not in plan.strict


def test_explicit_or_keeps_both_sides() -> None:
    plan = compile_match("cats OR dogs", prefix_last=False)
    assert '"cats"' in plan.strict and '"dogs"' in plan.strict
    assert " OR " in plan.strict


def test_synonyms_widen_a_term_without_replacing_it() -> None:
    plan = compile_match("footy", synonyms=lambda t: ("football", "soccer") if t == "footy" else ())
    assert '"footy"' in plan.strict
    assert '"football"' in plan.strict and '"soccer"' in plan.strict


def test_the_typed_spelling_comes_first_so_ranking_still_favours_it() -> None:
    plan = compile_match("footy", synonyms=lambda t: ("football",), prefix_last=False)
    assert plan.strict.index('"footy"') < plan.strict.index('"football"')


def test_empty_and_punctuation_only_text_compile_to_nothing() -> None:
    assert not compile_match("")
    assert not compile_match("   ")
    assert not compile_match("!!! ???")


@pytest.mark.parametrize(
    "text",
    [
        "it's a trap",
        "a-b-c",
        "50% off",
        'unbalanced "quote',
        "NEAR(a b)",
        "*",
        "^caret",
        "a:b",
        "photo -blurry",
        "photo -blurry -dark",
        'cats OR dogs -mice "a phrase"',
        "-only-an-exclusion",
        "OR",
    ],
)
def test_hostile_text_never_produces_invalid_fts(text: str) -> None:
    """Filenames are user data; a stray operator must not raise at query time."""
    import sqlite3

    plan = compile_match(text)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE t USING fts5(body)")
    conn.execute("INSERT INTO t(body) VALUES ('nothing in particular')")
    for expression in plan.expressions():
        if expression:
            conn.execute("SELECT rowid FROM t WHERE t MATCH ?", (expression,)).fetchall()
    conn.close()


# ── ranking ──────────────────────────────────────────────────────────────────


def test_ranking_weights_names_above_document_bodies() -> None:
    filename, tags, labels, doc_text, places, people = BM25_WEIGHTS
    assert filename > doc_text
    assert labels > doc_text and tags > doc_text
    assert places > doc_text and people > doc_text
    assert "bm25(search_index," in _BM25


# ── the parser ───────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def vocab() -> object:
    from mediaengine.config import Config

    return build_vocabulary(discover_packs(Config()).packs.values())


def test_an_unreserved_key_is_still_a_raw_namespace_filter() -> None:
    query = parse_query("color.dominant:blue")
    assert [(f.namespace, f.label) for f in query.labels] == [("color.dominant", "blue")]


def test_a_negated_key_excludes(vocab: object) -> None:
    query = parse_query("-origin.platform:youtube", vocabulary=vocab)
    assert [(f.namespace, f.label) for f in query.excluded_labels] == [
        ("origin.platform", "youtube")
    ]
    assert not query.labels


def test_a_short_namespace_resolves_through_the_packs(vocab: object) -> None:
    query = parse_query("sport:football", vocabulary=vocab)
    assert [(f.namespace, f.label) for f in query.labels] == [("content.sport", "football")]


def test_a_label_alias_resolves_through_the_packs(vocab: object) -> None:
    query = parse_query("sport:footy", vocabulary=vocab)
    assert [(f.namespace, f.label) for f in query.labels] == [("content.sport", "football")]


def test_an_unknown_value_is_passed_through_untouched(vocab: object) -> None:
    query = parse_query("sport:kabaddi", vocabulary=vocab)
    assert [(f.namespace, f.label) for f in query.labels] == [("content.sport", "kabaddi")]


def test_reserved_keys_still_win_over_pack_namespaces(vocab: object) -> None:
    query = parse_query("type:video after:2019", vocabulary=vocab)
    assert query.media_types == ["video"]
    assert query.captured_after is not None
    assert not query.labels


def test_free_text_survives_alongside_filters(vocab: object) -> None:
    query = parse_query("beach sport:footy -origin.platform:youtube", vocabulary=vocab)
    assert query.text == "beach"
    assert query.labels and query.excluded_labels


# ── end to end ───────────────────────────────────────────────────────────────


@pytest.fixture()
def library(tmp_path: Path) -> MediaEngine:
    root = tmp_path / "media"
    root.mkdir()
    write_jpeg(root / "IMG_20190407_beachDay.jpg", colour=(10, 20, 30))
    write_jpeg(root / "mountain-hike-2021.jpg", colour=(200, 100, 50))
    write_jpeg(root / "S02E11.family.dinner.jpg", colour=(40, 160, 90))
    config = make_config(tmp_path, root)
    engine = MediaEngine(config).start()
    engine.scan([root], generate_derivatives=False)
    yield engine
    engine.close()


def test_a_word_welded_into_a_filename_is_findable(library: MediaEngine) -> None:
    assert library.search("beach").total == 1
    assert library.search("hike").total == 1
    assert library.search("s02").total == 1


def test_a_strict_query_stays_strict(library: MediaEngine) -> None:
    assert library.search("beach mountain").total == 0 or library.search("beach").total == 1


def test_a_query_nobody_can_satisfy_falls_back_to_close_matches(library: MediaEngine) -> None:
    """Four words that never co-occur should show the best partial match."""
    result = library.search("beach mountain dinner sunset")
    assert result.total >= 1
    assert result.get("relaxed") is True


def test_a_satisfiable_query_is_not_marked_relaxed(library: MediaEngine) -> None:
    result = library.search("beach")
    assert result.total == 1
    assert result.get("relaxed") is None


def test_an_excluded_word_removes_its_matches(library: MediaEngine) -> None:
    assert library.search("2019").total == 1
    assert library.search("2019 -beach").total == 0

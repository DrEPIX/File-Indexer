from __future__ import annotations

import base64
import json
import sqlite3
import unittest
from pathlib import Path
from typing import Any, Mapping

from mediaengine_qol.errors import QueryError, SheetError
from mediaengine_qol.facade import QoLService
from mediaengine_qol.mediaengine_backend import MediaEngineBackend
from mediaengine_qol.models import (
    GroupOperator,
    PlannedGroup,
    SearchPlan,
    SearchRequest,
    SortDefinition,
)
from mediaengine_qol.planner import QueryPlanner
from mediaengine_qol.registry import SurfaceRegistry
from mediaengine_qol.schema import search_request_schema


ROOT = Path(__file__).resolve().parents[1]


class FakeBackend:
    def __init__(self, capabilities: frozenset[str]) -> None:
        self._capabilities = capabilities
        self.last_plan: SearchPlan | None = None

    def execute_search(self, plan: SearchPlan) -> Mapping[str, Any]:
        self.last_plan = plan
        return {"items": [], "next_cursor": None, "facets": {}}

    def get_asset(self, asset_id: int) -> Mapping[str, Any] | None:
        return {"id": asset_id}

    def list_facets(self, namespace: str | None = None) -> Mapping[str, Any]:
        return {"namespace": namespace, "values": []}

    def capabilities(self) -> frozenset[str]:
        return self._capabilities


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = SurfaceRegistry.from_toml(ROOT / "change_sheet.toml")

    def test_sheet_has_broad_surface(self) -> None:
        self.assertGreaterEqual(len(self.registry.filters), 25)
        self.assertGreaterEqual(len(self.registry.operations), 15)
        self.assertEqual(self.registry.filter("type").key, "media.type")

    def test_plans_nested_filters_and_capabilities(self) -> None:
        request = SearchRequest.from_mapping(
            {
                "text": "beach",
                "where": {
                    "operator": "all",
                    "clauses": [
                        {"key": "type", "operator": "in", "value": ["image", "video"]},
                        {"key": "location.exists", "operator": "is_true"},
                    ],
                    "groups": [
                        {
                            "operator": "any",
                            "clauses": [
                                {"key": "annotation.label", "operator": "eq", "value": "beach"},
                                {"key": "tag.name", "operator": "eq", "value": "vacation"},
                            ],
                        }
                    ],
                },
                "sort": "relevance",
                "page_size": 50,
            }
        )
        plan = QueryPlanner(self.registry).plan(request)
        self.assertEqual(plan.where.clauses[0].backend_field, "assets.media_type")
        self.assertEqual(plan.sort_field, "search.relevance")
        self.assertEqual(
            plan.required_capabilities,
            frozenset({"structured", "spatial", "annotations", "tags", "fts", "facets"}),
        )

    def test_rejects_untrusted_filter_and_operator(self) -> None:
        planner = QueryPlanner(self.registry)
        with self.assertRaises(QueryError):
            planner.plan(
                SearchRequest.from_mapping(
                    {"where": {"clauses": [{"key": "assets.id); DROP TABLE assets", "operator": "eq", "value": 1}]}}
                )
            )
        with self.assertRaises(QueryError):
            planner.plan(
                SearchRequest.from_mapping(
                    {"where": {"clauses": [{"key": "media.type", "operator": "contains", "value": "image"}]}}
                )
            )

    def test_service_checks_backend_capabilities(self) -> None:
        backend = FakeBackend(frozenset({"structured", "facets"}))
        service = QoLService(self.registry, backend)
        result = service.search(
            {"where": {"clauses": [{"key": "media.type", "operator": "eq", "value": "image"}]}}
        )
        self.assertEqual(result["items"], [])
        with self.assertRaises(NotImplementedError):
            service.search(
                {"where": {"clauses": [{"key": "location.exists", "operator": "is_true"}]}}
            )

    def test_schema_lists_declared_keys(self) -> None:
        schema = search_request_schema(self.registry)
        keys = schema["$defs"]["clause"]["properties"]["key"]["enum"]
        self.assertIn("annotation.label", keys)
        self.assertNotIn("type", keys)  # aliases are accepted, but not advertised as canonical.
        self.assertEqual(schema["properties"]["sort"]["default"], "captured")
        self.assertEqual(schema["properties"]["page_size"]["maximum"], 500)

    def test_registry_manifest_is_isolated_and_declarations_are_validated(self) -> None:
        manifest = self.registry.manifest()
        manifest["settings"]["search"]["max_page_size"] = 1
        self.assertEqual(self.registry.settings["search"]["max_page_size"], 500)

        with self.assertRaises(SheetError):
            SortDefinition(
                key="broken",
                label="Broken",
                field="assets.id",
                directions=("sideways",),
                default_direction="sideways",
            )
        with self.assertRaises(SheetError):
            SurfaceRegistry(
                filters=(),
                sorts=self.registry.sorts.values(),
                operations=(),
                settings={"search": {"default_page_size": True}},
            )
        with self.assertRaisesRegex(SheetError, "operators must be an array"):
            SurfaceRegistry._parse_filter(
                {
                    "key": "broken",
                    "label": "Broken",
                    "field": "assets.id",
                    "type": "integer",
                    "operators": "eq",
                }
            )
        with self.assertRaisesRegex(SheetError, "facet must be a boolean"):
            SurfaceRegistry._parse_filter(
                {
                    "key": "broken",
                    "label": "Broken",
                    "field": "assets.id",
                    "type": "integer",
                    "operators": ["eq"],
                    "facet": "false",
                }
            )

    def test_sheet_controls_defaults_and_boolean_depth(self) -> None:
        registry = SurfaceRegistry(
            filters=self.registry.filters.values(),
            sorts=self.registry.sorts.values(),
            operations=self.registry.operations.values(),
            settings={
                "search": {
                    "default_sort": "filename",
                    "default_page_size": 7,
                    "max_page_size": 20,
                    "max_boolean_depth": 1,
                }
            },
        )
        planner = QueryPlanner(registry)
        plan = planner.plan(SearchRequest.from_mapping({}))
        self.assertEqual(plan.sort_field, "files.filename")
        self.assertEqual(plan.page_size, 7)

        too_deep = SearchRequest.from_mapping(
            {
                "where": {
                    "groups": [{"groups": [{"clauses": []}]}],
                }
            }
        )
        with self.assertRaisesRegex(QueryError, "exceeds 1 levels"):
            planner.plan(too_deep)

    def test_malformed_nested_json_is_rejected_as_a_query_error(self) -> None:
        malformed_requests = (
            {"where": {"clauses": [42]}},
            {"where": {"groups": ["not-an-object"]}},
            {"surprise": True},
            {"where": {"surprise": True}},
            {"where": {"clauses": [{"key": "media.type", "operator": "eq", "extra": 1}]}},
            {"page_size": True},
            {"include_facets": "false"},
            {"facet_namespaces": ["valid", ""]},
            {"facet_namespaces": ["valid"] * 101},
            {"text": {"unexpected": "object"}},
        )
        for request in malformed_requests:
            with self.subTest(request=request), self.assertRaises(QueryError):
                SearchRequest.from_mapping(request)

    def test_normalizes_qol_boundary_values(self) -> None:
        backend = FakeBackend(frozenset({"structured", "facets"}))
        service = QoLService(self.registry, backend)
        service.search(
            {
                "text": "   ",
                "facet_namespaces": [" demo ", "demo", "other"],
            }
        )
        assert backend.last_plan is not None
        self.assertIsNone(backend.last_plan.text)
        self.assertNotIn("fts", backend.last_plan.required_capabilities)
        self.assertEqual(backend.last_plan.facet_namespaces, ("demo", "other"))
        with self.assertRaises(ValueError):
            service.asset(True)
        with self.assertRaises(ValueError):
            service.facets("   ")

    def test_cursor_and_nullable_sql_are_strict(self) -> None:
        for payload in (
            {"offset": True},
            {"offset": 1.5},
            {"offset": 2**63},
            {"offset": 1, "unexpected": True},
            [1],
        ):
            encoded = base64.urlsafe_b64encode(
                json.dumps(payload).encode("utf-8")
            ).decode("ascii").rstrip("=")
            with self.subTest(payload=payload), self.assertRaises(QueryError):
                MediaEngineBackend._decode_cursor(encoded)

        params: list[Any] = []
        self.assertEqual(
            MediaEngineBackend._value_predicate("a.captured_at", "eq", None, params),
            "a.captured_at IS NULL",
        )
        self.assertEqual(params, [])

        path_params: list[Any] = []
        path_sql = MediaEngineBackend._value_predicate(
            "fx.path", "under", r"C:\Photos", path_params
        )
        self.assertIn("fx.path = ?", path_sql)
        self.assertEqual(
            path_params,
            [r"C:\Photos", "C:\\\\Photos/%", "C:\\\\Photos\\\\%"],
        )
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE TABLE paths (path TEXT NOT NULL)")
            connection.executemany(
                "INSERT INTO paths VALUES (?)",
                [
                    (r"C:\Photos",),
                    (r"C:\Photos\child.jpg",),
                    (r"C:\Photos2\not-a-child.jpg",),
                    ("C:\\Photos/portable-child.jpg",),
                ],
            )
            matches = connection.execute(
                f"SELECT path FROM paths fx WHERE {path_sql}", path_params
            ).fetchall()
            self.assertEqual(len(matches), 3)
        finally:
            connection.close()

        with self.assertRaises(QueryError):
            MediaEngineBackend._mtime_value("9999-12-31T23:59:59Z")

    def test_parser_depth_limit_runs_before_python_recursion_limit(self) -> None:
        nested: dict[str, Any] = {"clauses": []}
        for _ in range(66):
            nested = {"groups": [nested]}
        with self.assertRaisesRegex(QueryError, "parser safety limit"):
            SearchRequest.from_mapping({"where": nested})

    def test_numeric_and_geo_values_are_finite_and_shape_checked(self) -> None:
        planner = QueryPlanner(self.registry)
        invalid_clauses = (
            {"key": "file.size", "operator": "gt", "value": float("inf")},
            {"key": "dimensions.width", "operator": "eq", "value": 1.5},
            {
                "key": "location.area",
                "operator": "within_radius",
                "value": {"latitude": 91, "longitude": 0, "radius_km": 5},
            },
            {
                "key": "location.area",
                "operator": "within_bbox",
                "value": {"south": 20, "north": 10, "west": 0, "east": 1},
            },
        )
        for clause in invalid_clauses:
            with self.subTest(clause=clause), self.assertRaises(QueryError):
                planner.plan(SearchRequest.from_mapping({"where": {"clauses": [clause]}}))

        plan = planner.plan(
            SearchRequest.from_mapping(
                {
                    "where": {
                        "clauses": [
                            {
                                "key": "location.area",
                                "operator": "within_bbox",
                                "value": {
                                    "south": -10,
                                    "north": 10,
                                    "west": 170,
                                    "east": -170,
                                },
                            }
                        ]
                    }
                }
            )
        )
        self.assertEqual(
            plan.where.clauses[0].value,
            {"min_lat": -10.0, "max_lat": 10.0, "min_lon": 170.0, "max_lon": -170.0},
        )
        params: list[Any] = []
        sql = MediaEngineBackend._geo_predicate(
            "within_bbox", plan.where.clauses[0].value, params
        )
        self.assertIn("gx.max_lon>=? OR gx.min_lon<=?", sql)
        self.assertEqual(params, [-10.0, 10.0, 170.0, -170.0])

    def test_facets_cover_filtered_matches_instead_of_only_current_page(self) -> None:
        class FakeDatabase:
            def query(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
                del params
                if "SELECT a.id, a.content_hash" in sql:
                    return [{"id": 1}, {"id": 2}]
                if "SELECT a.id FROM assets" in sql:
                    return [{"id": 1}, {"id": 2}, {"id": 3}]
                raise AssertionError(sql)

            def scalar(self, sql: str, params: list[Any]) -> int:
                del sql, params
                return 3

        class FakeAnnotations:
            def __init__(self) -> None:
                self.asset_ids: list[int] | None = None

            def facet(
                self, namespace: str, *, asset_ids: list[int]
            ) -> list[dict[str, Any]]:
                self.asset_ids = asset_ids
                return [{"namespace": namespace, "label": "all", "count": len(asset_ids)}]

        class FakeRepositories:
            def __init__(self) -> None:
                self.annotations = FakeAnnotations()

        class FakeEngine:
            def __init__(self) -> None:
                self.db = FakeDatabase()
                self.repos = FakeRepositories()

            def start(self) -> "FakeEngine":
                return self

        engine = FakeEngine()
        backend = MediaEngineBackend(engine)
        plan = SearchPlan(
            text=None,
            where=PlannedGroup(GroupOperator.ALL, (), ()),
            sort_field="assets.imported_at",
            direction="desc",
            page_size=1,
            cursor=None,
            include_facets=True,
            facet_namespaces=("demo",),
            required_capabilities=frozenset(),
        )
        result = backend.execute_search(plan)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["total"], 3)
        self.assertEqual(engine.repos.annotations.asset_ids, [1, 2, 3])
        self.assertEqual(result["facets"]["demo"][0]["count"], 3)


if __name__ == "__main__":
    unittest.main()

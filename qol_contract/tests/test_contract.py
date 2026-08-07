from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any, Mapping

from mediaengine_qol.errors import QueryError
from mediaengine_qol.facade import QoLService
from mediaengine_qol.models import SearchPlan, SearchRequest
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


if __name__ == "__main__":
    unittest.main()

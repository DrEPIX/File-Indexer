"""Dependency-light validation tests for the local reference-pack builder."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from build_reference_pack import build, load_source_manifest


def valid_source() -> dict[str, Any]:
    return {
        "name": "Licensed references",
        "version": "1",
        "source_url": "https://example.invalid/catalog",
        "license_name": "Test license",
        "attribution": "Example creator",
        "rights_statement": "Authorized local test fixture",
        "retention_policy": "Delete after testing",
        "people": [
            {
                "external_id": "person-a",
                "display_name": "Person A",
                "source_url": "https://example.invalid/person-a",
                "images": [
                    {"path": "person-a.jpg", "source_ref": "catalog item 1"}
                ],
            }
        ],
    }


class ReferencePackBuilderTests(unittest.TestCase):
    def write_source(self, directory: str, payload: dict[str, Any]) -> Path:
        path = Path(directory) / "source.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_validates_provenance_without_importing_ml_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = load_source_manifest(self.write_source(directory, valid_source()))
        self.assertEqual(source["people"][0]["external_id"], "person-a")
        self.assertEqual(source["metadata"], {})

    def test_rejects_missing_rights_and_duplicate_people(self) -> None:
        missing_rights = valid_source()
        missing_rights["license_name"] = ""
        duplicate_people = valid_source()
        duplicate_people["people"].append(dict(duplicate_people["people"][0]))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "license_name"):
                load_source_manifest(self.write_source(directory, missing_rights))
            with self.assertRaisesRegex(ValueError, "duplicate external_id"):
                load_source_manifest(self.write_source(directory, duplicate_people))

    def test_refuses_destructive_output_before_loading_models(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source_path = self.write_source(directory, valid_source())
            output_path = Path(directory) / "pack.json"
            output_path.write_text("keep me", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                build(source_path, output_path)
            self.assertEqual(output_path.read_text(encoding="utf-8"), "keep me")
            with self.assertRaisesRegex(ValueError, "must differ"):
                build(source_path, source_path, overwrite=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)

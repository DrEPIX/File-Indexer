"""Focused regression tests for the dependency-free extension validator."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


from scripts.validate_extensions import validate_plugin


VALID = """
[plugin]
id = "example.valid"
version = "1.0.0"
transport = "http"
accepts = ["image"]
emits = ["example.valid"]
depends_on = []
transfer = "both"
embedding_dim = 8

[plugin.requires]
pixels = true
frames = false
audio = false
text = false
metadata_only = false
gpu = false
network = false
max_concurrency = 1

[plugin.http]
base_url = "http://127.0.0.1:9000"
timeout_s = 10.0
health_path = "/health"
manifest_path = "/manifest"
analyze_path = "/analyze"

[plugin.namespaces."example.valid"]
display_name = "Valid"
value_type = "text"
facetable = false
"""


class ValidatorTests(unittest.TestCase):
    def validate(self, text: str) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plugin.toml"
            path.write_text(text, encoding="utf-8")
            return [f"{issue.path}: {issue.message}" for issue in validate_plugin(path)]

    def test_valid_http_manifest(self) -> None:
        self.assertEqual(self.validate(VALID), [])

    def test_reports_multiple_low_context_edit_mistakes(self) -> None:
        broken = VALID.replace('id = "example.valid"', 'id = "BAD ID"')
        broken = broken.replace('accepts = ["image"]', 'accepts = ["image", "spreadsheet"]')
        broken = broken.replace("metadata_only = false", "metadata_only = true")
        messages = self.validate(broken)
        self.assertTrue(any(message.startswith("plugin.id:") for message in messages))
        self.assertTrue(any(message.startswith("plugin.accepts:") for message in messages))
        self.assertTrue(any(message.startswith("plugin.requires.metadata_only:") for message in messages))


if __name__ == "__main__":
    unittest.main(verbosity=2)

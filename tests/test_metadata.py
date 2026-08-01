import re
import tomllib
import unittest
from pathlib import Path

import sqlite_transit_sync

ROOT = Path(__file__).parent.parent


class TestMetadata(unittest.TestCase):
    """Metadata parity.

    These assertions compare the version *sources* against each other instead of
    against a literal. A hard-coded number turns every release into a test edit and
    silently passes when only one of the three places was bumped — which is the one
    failure this test exists to catch.
    """

    def _pyproject_version(self) -> str:
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        return data["project"]["version"]

    def test_version_consistency(self):
        """Package version matches the packaging metadata."""
        self.assertEqual(sqlite_transit_sync.__version__, self._pyproject_version())

    def test_llms_txt_version_parity(self):
        """llms.txt declares the same version the package reports."""
        llms_file = ROOT / "llms.txt"
        self.assertTrue(llms_file.exists(), "llms.txt should exist in repository root")

        content = llms_file.read_text(encoding="utf-8")
        declared = re.search(r"^- Version:\s*(\S+)", content, re.MULTILINE)
        self.assertIsNotNone(declared, "llms.txt must declare a '- Version:' line")
        self.assertEqual(sqlite_transit_sync.__version__, declared.group(1))

    def test_exports_present(self):
        """Verify all declared exports in __all__ are importable and non-None."""
        for item in sqlite_transit_sync.__all__:
            obj = getattr(sqlite_transit_sync, item, None)
            self.assertIsNotNone(obj, f"Exported symbol {item} should be present in module")


if __name__ == "__main__":
    unittest.main()

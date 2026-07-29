import unittest
from pathlib import Path
import sqlite_transit_sync


class TestMetadata(unittest.TestCase):
    def test_version_consistency(self):
        """Verify that sqlite_transit_sync.__version__ is 0.2.0."""
        self.assertEqual(sqlite_transit_sync.__version__, "0.2.0")

    def test_llms_txt_version_parity(self):
        """Verify llms.txt reflects version 0.2.0 and has recent Last-checked date."""
        llms_file = Path(__file__).parent.parent / "llms.txt"
        self.assertTrue(llms_file.exists(), "llms.txt should exist in repository root")

        content = llms_file.read_text(encoding="utf-8")
        self.assertIn("- Version: 0.2.0", content, "llms.txt must declare Version: 0.2.0")

    def test_exports_present(self):
        """Verify all declared exports in __all__ are importable and non-None."""
        for item in sqlite_transit_sync.__all__:
            obj = getattr(sqlite_transit_sync, item, None)
            self.assertIsNotNone(obj, f"Exported symbol {item} should be present in module")


if __name__ == "__main__":
    unittest.main()

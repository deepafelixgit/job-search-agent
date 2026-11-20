"""Offline test: the career-evidence server labels each impact_of_my_work.md line.

Run:  .venv-mcp/bin/python -m unittest tests.test_impact_reader
"""
import tempfile
import unittest
from pathlib import Path


class ImpactReader(unittest.TestCase):
    """The career-evidence server labels each impact_of_my_work.md line (runs where pypdf is installed)."""

    def test_lines_are_labeled(self):
        try:
            from mcp_server.documents import read_impact
        except ImportError:
            self.skipTest("needs the career-evidence environment: "
                          ".venv-mcp/bin/python -m unittest tests.test_impact_reader")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "impact_of_my_work.md"
            p.write_text("# Impact\n\nIntro.\n\n## How to position my work\n\n- **Lead:** outcome first.\n\n"
                         "## Product X\n\n- **Say it like this:** Owned X.\n- **Fact:** I scaled X.\n"
                         "- **Boundary:** I did not own Y.\n- **Perspective:** It mattered.\n"
                         "- **Public fact:** X helps people.\n- **Good fit for:** wearables.\n", encoding="utf-8")
            doc = read_impact(p)
        got = [(x.doc_type, x.text) for x in doc.passages]
        self.assertEqual(got, [("my framing", "**Lead:** outcome first."), ("my framing", "Owned X."),
                               ("impact fact", "I scaled X."), ("impact fact", "I did not own Y."),
                               ("my perspective", "It mattered."), ("public product fact", "X helps people."),
                               ("good fit", "wearables.")])
        self.assertIn("never say or imply the candidate built", doc.passages[5].extra["use"])


if __name__ == "__main__":
    unittest.main()

import importlib.util
import sys
import unittest
from pathlib import Path


def load_generate_docs_site():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "generate_docs_site.py"
    spec = importlib.util.spec_from_file_location("generate_docs_site", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load spec for {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class GenerateDocsSiteTests(unittest.TestCase):
    def test_rewrite_headings_increases_heading_depth(self):
        module = load_generate_docs_site()
        source = "# Title\n\n## Section\nplain text\n"
        rewritten = module.rewrite_headings(source, add=2)

        self.assertIn("### Title", rewritten)
        self.assertIn("#### Section", rewritten)
        self.assertIn("plain text", rewritten)


if __name__ == "__main__":
    unittest.main()

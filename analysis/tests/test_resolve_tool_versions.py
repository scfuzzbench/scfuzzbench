import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


def load_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "resolve_tool_versions.py"
    spec = importlib.util.spec_from_file_location("resolve_tool_versions", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ResolveToolVersionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def _fetcher(self, tags):
        """Return a fetcher that maps repo -> a fake latest-release payload."""

        def fetch(repo, *, token=None):
            return {"tag_name": tags[repo], "draft": False, "prerelease": False}

        return fetch

    def test_resolves_latest_and_strips_leading_v(self):
        fetch = self._fetcher(
            {
                "crytic/echidna": "v2.3.2",
                "crytic/medusa": "1.5.1",
                "Recon-Fuzz/recon-fuzzer": "v0.4.18",
            }
        )
        resolved = self.module.resolve_all({}, fetcher=fetch)
        self.assertEqual(
            resolved,
            {"echidna": "2.3.2", "medusa": "1.5.1", "recon": "0.4.18"},
        )

    def test_explicit_pin_wins_without_network(self):
        def fetch(repo, *, token=None):
            raise AssertionError(f"unexpected network call for {repo}")

        resolved = self.module.resolve_all(
            {
                "ECHIDNA_VERSION": "2.3.0",
                "MEDUSA_VERSION": "1.4.1",
                "RECON_VERSION": "v0.4.6",
            },
            fetcher=fetch,
        )
        self.assertEqual(
            resolved,
            {"echidna": "2.3.0", "medusa": "1.4.1", "recon": "0.4.6"},
        )

    def test_override_modes_skip_release_resolution(self):
        def fetch(repo, *, token=None):
            # Only recon should be resolved here.
            self.assertEqual(repo, "Recon-Fuzz/recon-fuzzer")
            return {"tag_name": "v0.4.18", "draft": False, "prerelease": False}

        resolved = self.module.resolve_all(
            {
                "ECHIDNA_CI_REPO": "https://github.com/crytic/echidna",
                "MEDUSA_GIT_REPO": "https://github.com/crytic/medusa",
            },
            fetcher=fetch,
        )
        self.assertEqual(resolved["echidna"], "")
        self.assertEqual(resolved["medusa"], "")
        self.assertEqual(resolved["recon"], "0.4.18")

    def test_prerelease_latest_is_rejected(self):
        def fetch(repo, *, token=None):
            return {"tag_name": "v2.4.0", "draft": False, "prerelease": True}

        with self.assertRaises(self.module.ResolutionError):
            self.module.resolve_all({}, fetcher=fetch)

    def test_missing_tag_is_rejected(self):
        def fetch(repo, *, token=None):
            return {"tag_name": "", "draft": False, "prerelease": False}

        with self.assertRaises(self.module.ResolutionError):
            self.module.resolve_all({}, fetcher=fetch)

    def test_malformed_tag_is_rejected(self):
        with self.assertRaises(self.module.ResolutionError):
            self.module._normalize_tag("v1.2.3; rm -rf /")

    def test_writes_github_output(self):
        fetch = self._fetcher(
            {
                "crytic/echidna": "v2.3.2",
                "crytic/medusa": "v1.5.1",
                "Recon-Fuzz/recon-fuzzer": "v0.4.18",
            }
        )
        resolved = self.module.resolve_all({}, fetcher=fetch)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gh_output"
            out.write_text("preexisting=1\n", encoding="utf-8")
            self.module._write_github_output(str(out), resolved)
            lines = out.read_text(encoding="utf-8").splitlines()
        self.assertIn("preexisting=1", lines)
        self.assertIn("echidna_version=2.3.2", lines)
        self.assertIn("medusa_version=1.5.1", lines)
        self.assertIn("recon_version=0.4.18", lines)


if __name__ == "__main__":
    unittest.main()

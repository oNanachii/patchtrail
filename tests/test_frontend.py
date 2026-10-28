from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
FRONTEND = ROOT / "frontend"


class FrontendStateTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required for the frontend state contract")
    def test_create_retry_and_evidence_logic(self):
        # Dependency-free node:test suites on the plain-JS modules; not a browser or model run.
        suites = sorted(str(path) for path in (FRONTEND / "tests").glob("*.test.js"))
        self.assertTrue(suites)
        result = subprocess.run([shutil.which("node"), "--test", *suites], capture_output=True, text=True,
                                timeout=60, cwd=FRONTEND)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"(?m)^ℹ fail 0$")


class _Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs, self.inline_scripts, self.style_attrs, self.style_tags = [], 0, 0, 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "style" in attrs:
            self.style_attrs += 1
        if tag == "style":
            self.style_tags += 1
        if tag == "script" and not attrs.get("src"):
            self.inline_scripts += 1
        for key in ("src", "href"):
            if attrs.get(key):
                self.refs.append(attrs[key])


class BuiltFrontendTests(unittest.TestCase):
    """The committed web/ build must load under the API's CSP (self-only, no inline code)."""

    def test_index_references_existing_same_origin_assets_only(self):
        parser = _Assets()
        parser.feed((WEB / "index.html").read_text(encoding="utf-8"))
        self.assertEqual((parser.inline_scripts, parser.style_attrs, parser.style_tags), (0, 0, 0))
        self.assertTrue(any(ref.endswith(".js") for ref in parser.refs))
        for ref in parser.refs:
            self.assertTrue(ref.startswith("/assets/"), ref)
            self.assertTrue((WEB / ref.removeprefix("/assets/")).is_file(), ref)

    def test_bundles_have_no_external_or_inline_font_sources(self):
        for css in WEB.glob("*.css"):
            text = css.read_text(encoding="utf-8")
            self.assertNotIn("data:font", text, css.name)
            self.assertNotRegex(text, r"url\(\s*['\"]?(https?:)?//", css.name)
        for script in WEB.glob("*.js"):
            text = script.read_text(encoding="utf-8")
            # The operator token must never be written to browser storage.
            self.assertNotRegex(text, r"(localStorage|sessionStorage)\.setItem\([^)]*[Tt]oken", script.name)


if __name__ == "__main__":
    unittest.main()

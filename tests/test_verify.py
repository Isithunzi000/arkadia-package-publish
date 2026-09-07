"""Testy audytu katalogu (scripts/verify.py + scripts/import_probe.mjs).

Zasada red->green: padaja na braku implementacji, po dodaniu verify.py
i import_probe.mjs caly zestaw musi przechodzic lokalnie.
"""

import hashlib
import io
import json
import shutil
import sys
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import verify as V  # noqa: E402

NODE = shutil.which("node")
need_node = unittest.skipUnless(NODE, "node niedostepny w PATH")


# ---------------------------------------------------------------------------
# Fixtury
# ---------------------------------------------------------------------------

def make_zip(entries, compression=zipfile.ZIP_STORED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as z:
        for rel, content in entries.items():
            z.writestr(rel, content)
    return buf.getvalue()


GOOD_TS = 'const PLUGIN_VERSION = "1.0.6";\nexport async function init() {}\n'
GOOD_PJ = json.dumps({"name": "truwer", "metadata": {"version": "1.0.6",
                      "author": "Isithunzi000", "description": "d"}})
OUR_ZIP = make_zip({"truwer/index.ts": GOOD_TS, "truwer/plugin.json": GOOD_PJ})
REPACKED_ZIP = make_zip({"truwer/index.ts": GOOD_TS, "truwer/plugin.json": GOOD_PJ},
                        compression=zipfile.ZIP_DEFLATED)


def make_bundle(alias=r"^\/truwer$", trigger=None, popup="truwer|Truwer",
                menu="Truwer", name="truwer", version="1.0.6",
                author="Isithunzi000", description="d"):
    lines = ["export async function init(api) {"]
    if alias is not None:
        lines.append(f"  api.aliases.register(/{alias}/i, () => true);")
    if trigger is not None:
        lines.append(f"  api.triggers.register(/{trigger}/i, () => null);")
    if popup is not None:
        pid, ptitle = popup.split("|", 1)
        lines.append(f'  await api.ui.registerPersistentPopup({{id: "{pid}", title: "{ptitle}"}});')
    if menu is not None:
        lines.append(f'  api.ui.addPopupMenuEntry("{menu}", () => {{}});')
    lines.append(f'  return {{name: "{name}", version: "{version}", author: "{author}", description: "{description}"}};')
    lines.append("}")
    lines.append("export async function destroy() {}")
    return "\n".join(lines) + "\n"


BASE_BUNDLE = make_bundle()


class Resp:
    def __init__(self, body=b"", headers=None, status=200):
        self.status = status
        self.body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.headers = headers or {}


class FakeFetcher:
    """Atrapa HTTP z naglowkami odpowiedzi i rejestron zadan."""

    def __init__(self):
        self.gets = []   # (url, request_headers)
        self.posts = []
        self.routes = {}

    def add(self, url, body=b"", headers=None, status=200):
        self.routes[url] = Resp(body, headers, status)

    def get(self, url, headers=None):
        self.gets.append((url, headers or {}))
        if url not in self.routes:
            raise V.VerifyError(f"GET {url}: brak trasy")
        return self.routes[url]

    def post(self, url, body, headers):
        self.posts.append((url, body, headers))
        return Resp(b"{}")


TRUWER_DETAIL = {
    "plugin": {"slug": "truwer", "latestVersion": "1.0.6", "trustedPublisher": True},
    "versions": [{"version": "1.0.6",
                  "sha256": hashlib.sha256(BASE_BUNDLE.encode("utf-8")).hexdigest(),
                  "yanked": False,
                  "hasSources": True, "provenance": {"repositoryId": V.EXPECTED_REPO_ID}}],
    "install": {"packageUrl": V.REGISTRY + "/r/truwer/1.0.6/package.zip"},
}
JS_HEADERS = {"Content-Type": "text/javascript; charset=utf-8",
              "Access-Control-Allow-Origin": "*"}
ZIP_HEADERS = {"Content-Type": "application/zip"}


def make_flow_fetcher(detail=None, pkg=None, registry_bundle=None, pages_bundle=None,
                      latest_bundle=None, js_headers=None):
    f = FakeFetcher()
    detail = detail if detail is not None else TRUWER_DETAIL
    f.add(V.PAGES_INDEX, json.dumps({"plugins": [
        {"name": "truwer", "zip": "truwer_1_0_6.zip", "file": "truwer.js"}]}))
    f.add(V.REGISTRY + "/api/v1/plugins/truwer", json.dumps(detail))
    f.add(V.RAW_ZIP_TEMPLATE.format(zip="truwer_1_0_6.zip"), OUR_ZIP)
    pkg = pkg if pkg is not None else REPACKED_ZIP
    f.add(detail["install"]["packageUrl"], pkg, ZIP_HEADERS)
    registry_bundle = registry_bundle if registry_bundle is not None else BASE_BUNDLE
    pages_bundle = pages_bundle if pages_bundle is not None else BASE_BUNDLE
    f.add(V.REGISTRY + "/r/truwer/1.0.6/plugin.js", registry_bundle,
          js_headers if js_headers is not None else JS_HEADERS)
    f.add(V.PAGES_BUNDLE.format(name="truwer"), pages_bundle, JS_HEADERS)
    f.add(V.REGISTRY + "/r/truwer/latest/plugin.js",
          latest_bundle if latest_bundle is not None else registry_bundle, JS_HEADERS)
    return f


def run_verify(argv, fetcher):
    out, err = io.StringIO(), io.StringIO()
    rc = V.run(argv, {}, fetcher, out, err)
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# Nazwy i pakiety
# ---------------------------------------------------------------------------

class TestPackages(unittest.TestCase):
    def test_parse_zip_name(self):
        self.assertEqual(V.parse_zip_name("truwer_1_0_6.zip"), ("truwer", "1.0.6"))
        with self.assertRaises(V.VerifyError):
            V.parse_zip_name("truwer.zip")

    def test_compare_identical_content_repacked_container(self):
        self.assertNotEqual(OUR_ZIP, REPACKED_ZIP, "kontenery musza sie roznic bajtowo")
        self.assertEqual(V.compare_packages(OUR_ZIP, REPACKED_ZIP), [])

    def test_compare_tampered_content(self):
        bad = make_zip({"truwer/index.ts": GOOD_TS + "// zlosliwosc\n",
                        "truwer/plugin.json": GOOD_PJ})
        problems = V.compare_packages(OUR_ZIP, bad)
        self.assertTrue(any("index.ts" in p for p in problems))

    def test_compare_missing_entry(self):
        bad = make_zip({"truwer/index.ts": GOOD_TS})
        problems = V.compare_packages(OUR_ZIP, bad)
        self.assertTrue(any("plugin.json" in p for p in problems))

    def test_compare_extra_entry(self):
        bad = make_zip({"truwer/index.ts": GOOD_TS, "truwer/plugin.json": GOOD_PJ,
                        "truwer/extra.js": "x"})
        problems = V.compare_packages(OUR_ZIP, bad)
        self.assertTrue(any("extra" in p for p in problems))


# ---------------------------------------------------------------------------
# Sonda importowa i slad rejestracji
# ---------------------------------------------------------------------------

@need_node
class TestProbe(unittest.TestCase):
    def write_bundle(self, tmp, text):
        p = Path(tmp) / "bundle.js"
        p.write_text(text)
        return str(p)

    def test_probe_extracts_footprint(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            fp = V.run_probe(self.write_bundle(tmp, BASE_BUNDLE))
        self.assertEqual(fp["pluginInfo"], {"name": "truwer", "version": "1.0.6",
                                            "author": "Isithunzi000", "description": "d"})
        self.assertEqual(fp["aliases"], [r"^\/truwer$|i"])
        self.assertEqual(fp["popups"], ["truwer|Truwer"])
        self.assertEqual(fp["menus"], ["Truwer"])
        self.assertEqual(fp["triggers"], [])

    def test_probe_broken_bundle_raises(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(V.VerifyError):
                V.run_probe(self.write_bundle(tmp, "export async function init( {\n"))

    def test_compare_footprints_same(self):
        fp = {"pluginInfo": {"a": 1}, "aliases": ["x"], "triggers": [], "popups": [], "menus": []}
        self.assertEqual(V.compare_footprints(fp, fp), [])

    def test_compare_footprints_diff_plugininfo(self):
        a = {"pluginInfo": {"name": "truwer", "description": "d"}, "aliases": [], "triggers": [], "popups": [], "menus": []}
        b = {"pluginInfo": {"name": "truwer", "description": "INNE"}, "aliases": [], "triggers": [], "popups": [], "menus": []}
        problems = V.compare_footprints(a, b)
        self.assertTrue(any("PluginInfo" in p for p in problems))

    def test_compare_footprints_missing_alias(self):
        a = {"pluginInfo": {}, "aliases": [r"^\/truwer$|i"], "triggers": [], "popups": [], "menus": []}
        b = {"pluginInfo": {}, "aliases": [], "triggers": [], "popups": [], "menus": []}
        problems = V.compare_footprints(a, b)
        self.assertTrue(any("aliases" in p for p in problems))


# ---------------------------------------------------------------------------
# Stan API
# ---------------------------------------------------------------------------

class TestApiState(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(V.check_api_state(TRUWER_DETAIL, "1.0.6"), [])

    def test_version_mismatch(self):
        problems = V.check_api_state(TRUWER_DETAIL, "9.9.9")
        self.assertTrue(any("latestVersion" in p for p in problems))

    def test_yanked(self):
        detail = json.loads(json.dumps(TRUWER_DETAIL))
        detail["versions"][0]["yanked"] = True
        self.assertTrue(any("yanked" in p for p in V.check_api_state(detail, "1.0.6")))

    def test_wrong_repo_id(self):
        detail = json.loads(json.dumps(TRUWER_DETAIL))
        detail["versions"][0]["provenance"]["repositoryId"] = "999999"
        self.assertTrue(any("repositoryId" in p for p in V.check_api_state(detail, "1.0.6")))

    def test_not_trusted_publisher(self):
        detail = json.loads(json.dumps(TRUWER_DETAIL))
        detail["plugin"]["trustedPublisher"] = False
        self.assertTrue(any("trustedPublisher" in p for p in V.check_api_state(detail, "1.0.6")))


# ---------------------------------------------------------------------------
# Deklarowany sha256 katalogu
# ---------------------------------------------------------------------------

class TestDeclaredSha(unittest.TestCase):
    def test_ok(self):
        blob = b"export async function init() {}\n"
        detail = json.loads(json.dumps(TRUWER_DETAIL))
        detail["versions"][0]["sha256"] = hashlib.sha256(blob).hexdigest()
        self.assertEqual(V.check_declared_sha(detail, "1.0.6", blob), [])

    def test_mismatch(self):
        problems = V.check_declared_sha(TRUWER_DETAIL, "1.0.6", b"inne bajty")
        self.assertTrue(any("sha256" in p for p in problems))

    def test_missing_declared(self):
        detail = json.loads(json.dumps(TRUWER_DETAIL))
        del detail["versions"][0]["sha256"]
        problems = V.check_declared_sha(detail, "1.0.6", b"x")
        self.assertTrue(any("sha256" in p for p in problems))


# ---------------------------------------------------------------------------
# Naglowki
# ---------------------------------------------------------------------------

class TestHeaders(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(V.check_js_headers(JS_HEADERS), [])

    def test_missing_cors(self):
        problems = V.check_js_headers({"Content-Type": "text/javascript"})
        self.assertTrue(any("Access-Control-Allow-Origin" in p for p in problems))

    def test_wrong_mime(self):
        problems = V.check_js_headers({"Content-Type": "text/html",
                                       "Access-Control-Allow-Origin": "*"})
        self.assertTrue(any("Content-Type" in p for p in problems))


# ---------------------------------------------------------------------------
# Normalizacja diffa
# ---------------------------------------------------------------------------

class TestNormalize(unittest.TestCase):
    def test_whitespace_variance_disappears(self):
        a = "var x = 1;\n\nvar y = 2;\n"
        b = "var x = 1;\nvar y = 2;   \n"
        self.assertEqual(V.normalize_js(a), V.normalize_js(b))

    def test_real_difference_survives(self):
        self.assertNotEqual(V.normalize_js("var x = 1;"), V.normalize_js("var x = 2;"))


# ---------------------------------------------------------------------------
# Przeplyw end-to-end na atraph
# ---------------------------------------------------------------------------

@need_node
class TestFlow(unittest.TestCase):
    def test_all_pass_readonly(self):
        f = make_flow_fetcher()
        rc, out, _ = run_verify(["--plugin", "truwer"], f)
        self.assertEqual(rc, 0)
        self.assertIn("[OK] api", out)
        self.assertIn("[OK] package", out)
        self.assertIn("[OK] bundle", out)
        self.assertIn("[OK] latest", out)
        self.assertIn("[OK] naglowki", out)
        self.assertEqual(f.posts, [], "verify nie wysyla zadnych POST-ow")
        urls = [u for u, _ in f.gets]
        self.assertFalse(any("oidc" in u for u in urls), "verify nie pobiera tokenu OIDC")

    def test_tampered_package_fails(self):
        bad = make_zip({"truwer/index.ts": GOOD_TS + "// x\n", "truwer/plugin.json": GOOD_PJ})
        f = make_flow_fetcher(pkg=bad)
        rc, out, _ = run_verify(["--plugin", "truwer"], f)
        self.assertEqual(rc, 1)
        self.assertIn("[FAIL] package", out)

    def test_latest_mismatch_fails(self):
        f = make_flow_fetcher(latest_bundle=BASE_BUNDLE + "// stara wersja\n")
        rc, out, _ = run_verify(["--plugin", "truwer"], f)
        self.assertEqual(rc, 1)
        self.assertIn("[FAIL] latest", out)

    def test_missing_cors_fails(self):
        f = make_flow_fetcher(js_headers={"Content-Type": "text/javascript"})
        rc, out, _ = run_verify(["--plugin", "truwer"], f)
        self.assertEqual(rc, 1)
        self.assertIn("[FAIL] naglowki", out)

    def test_tampered_bundle_fails(self):
        evil = make_bundle(description="PODMIENIONY OPIS")
        f = make_flow_fetcher(registry_bundle=evil)
        rc, out, _ = run_verify(["--plugin", "truwer"], f)
        self.assertEqual(rc, 1)
        self.assertIn("[FAIL] bundle", out)


if __name__ == "__main__":
    unittest.main()

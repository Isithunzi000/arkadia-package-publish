"""Testy skryptu publikacji do katalogu Dargoth (scripts/publish.py).

Zasada red->green: te testy padaja na braku implementacji, a po dodaniu
scripts/publish.py caly zestaw musi przechodzic lokalnie.
"""

import io
import json
import sys
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import publish as P  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtury
# ---------------------------------------------------------------------------

def index_doc(truwer_ver="1.0.5", imperium_ver="1.8.23", ishtar_ver="1.8.22", skip=None):
    plugins = []
    for name, ver in (("imperium_cal", imperium_ver), ("ishtar_cal", ishtar_ver), ("truwer", truwer_ver)):
        if name == skip:
            continue
        z = f"{name}_{ver.replace('.', '_')}.zip"
        plugins.append({"name": name, "zip": z, "file": f"{name}.js", "kb": "1.0"})
    return {"built": "2026-09-07T00:00:00.000Z", "plugins": plugins}


def registry_doc(slugs):
    """slugs: dict slug -> wersja (None = brak wpisu w katalogu)."""
    items = [{"slug": s, "latestVersion": v} for s, v in slugs.items() if v is not None]
    return {"items": items}


def make_zip(name, zip_ver, ts_ver=None, json_ver=None, double_ts=False, no_json=False):
    ts_ver = zip_ver if ts_ver is None else ts_ver
    json_ver = zip_ver if json_ver is None else json_ver
    ts = 'import PluginApi, { PluginInfo } from "plugin-api";\n'
    ts += f'const PLUGIN_VERSION = "{ts_ver}";\n'
    if double_ts:
        ts += f'const PLUGIN_VERSION = "{ts_ver}";\n'
    ts += 'export async function init(api) {}\n'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr(f"{name}/index.ts", ts)
        if not no_json:
            pj = {
                "name": name,
                "entryPoint": "index.ts",
                "metadata": {"name": name, "version": json_ver, "author": "Isithunzi000", "description": "x"},
                "folders": [],
            }
            z.writestr(f"{name}/plugin.json", json.dumps(pj))
    return buf.getvalue()


class FakeFetcher:
    """Atrapa warstwy HTTP: kolejkuje odpowiedzi GET i rejestruje POST-y."""

    def __init__(self):
        self.gets = []   # (url, headers)
        self.posts = []  # (url, body, headers)
        self.routes = {}

    def add(self, url, *payloads):
        self.routes[url] = [p if isinstance(p, bytes) else p.encode("utf-8") for p in payloads]

    def get(self, url, headers=None):
        self.gets.append((url, headers))
        if url not in self.routes:
            raise P.PublishError(f"GET {url}: brak trasy")
        queue = self.routes[url]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def post(self, url, body, headers):
        self.posts.append((url, body, headers))
        return b'{"ok": true}'


LIST_URL_ALL = P.REGISTRY + "/api/v1/plugins?slugs=imperium-cal,ishtar-cal,truwer"
OIDC_ENV = {
    "ACTIONS_ID_TOKEN_REQUEST_URL": "http://oidc.local/token?run=1",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "req-tok",
}
OIDC_FULL_URL = "http://oidc.local/token?run=1&audience=arkadia-plugins"


def make_flow_fetcher(registry_slugs):
    """Fetcher z pelna trasa: index, lista katalogu, zip, OIDC, detail."""
    f = FakeFetcher()
    f.add(P.PAGES_INDEX, json.dumps(index_doc()))
    f.add(LIST_URL_ALL, json.dumps(registry_doc(registry_slugs)))
    return f


# ---------------------------------------------------------------------------
# Wersje i nazwy
# ---------------------------------------------------------------------------

class TestVersions(unittest.TestCase):
    def test_parse_version_ok(self):
        self.assertEqual(P.parse_version("1.8.23"), (1, 8, 23))
        self.assertEqual(P.parse_version("0.0.1"), (0, 0, 1))

    def test_parse_version_rejects_garbage(self):
        for bad in ("", "1.8", "1.8.x", "v1.8.23", "1.8.23-rc", "1..2"):
            with self.assertRaises(P.PublishError, msg=bad):
                P.parse_version(bad)

    def test_version_newer_semver(self):
        self.assertTrue(P.version_newer("1.8.23", "1.8.22"))
        self.assertTrue(P.version_newer("1.10.0", "1.9.9"))
        self.assertFalse(P.version_newer("1.8.23", "1.8.23"))
        self.assertFalse(P.version_newer("1.0.5", "1.0.6"))

    def test_parse_zip_name(self):
        self.assertEqual(P.parse_zip_name("imperium_cal_1_8_23.zip"), ("imperium_cal", "1.8.23"))
        self.assertEqual(P.parse_zip_name("truwer_1_0_5.zip"), ("truwer", "1.0.5"))

    def test_parse_zip_name_rejects(self):
        for bad in ("foo.zip", "x_1_2.zip", "x_1_2_3.js", "x_1_2_3.zip.bak"):
            with self.assertRaises(P.PublishError, msg=bad):
                P.parse_zip_name(bad)


# ---------------------------------------------------------------------------
# Plan publikacji
# ---------------------------------------------------------------------------

class TestPlan(unittest.TestCase):
    def test_nothing_new_all_skip(self):
        plan = P.build_plan(index_doc(), registry_doc({
            "imperium-cal": "1.8.23", "ishtar-cal": "1.8.22", "truwer": "1.0.5"}))
        self.assertEqual(len(plan), 3)
        self.assertTrue(all(p.action == "skip" for p in plan))

    def test_one_new_version_only_that_publishes(self):
        plan = P.build_plan(index_doc(), registry_doc({
            "imperium-cal": "1.8.23", "ishtar-cal": "1.8.22", "truwer": "1.0.4"}))
        actions = {p.slug: p.action for p in plan}
        self.assertEqual(actions, {"imperium-cal": "skip", "ishtar-cal": "skip", "truwer": "publish"})
        truwer = [p for p in plan if p.slug == "truwer"][0]
        self.assertEqual(truwer.version, "1.0.5")
        self.assertEqual(truwer.zip_name, "truwer_1_0_5.zip")
        self.assertEqual(truwer.registry_version, "1.0.4")

    def test_registry_missing_slug_means_publish(self):
        plan = P.build_plan(index_doc(), registry_doc({
            "imperium-cal": "1.8.23", "ishtar-cal": "1.8.22"}))
        truwer = [p for p in plan if p.slug == "truwer"][0]
        self.assertEqual(truwer.action, "publish")
        self.assertIsNone(truwer.registry_version)

    def test_registry_newer_means_skip(self):
        plan = P.build_plan(index_doc(), registry_doc({
            "imperium-cal": "1.8.23", "ishtar-cal": "1.8.22", "truwer": "9.9.9"}))
        truwer = [p for p in plan if p.slug == "truwer"][0]
        self.assertEqual(truwer.action, "skip")

    def test_missing_plugin_in_index_is_error(self):
        with self.assertRaises(P.PublishError):
            P.build_plan(index_doc(skip="truwer"), registry_doc({"truwer": "1.0.5"}))

    def test_selected_plugin_limits_plan(self):
        plan = P.build_plan(index_doc(), registry_doc({"truwer": "1.0.4"}), selected={"truwer"})
        self.assertEqual([p.slug for p in plan], ["truwer"])

    def test_selected_unknown_slug_is_error(self):
        with self.assertRaises(P.PublishError):
            P.build_plan(index_doc(), registry_doc({}), selected={"nie-ma-takiego"})


# ---------------------------------------------------------------------------
# Guard wersji na zipie
# ---------------------------------------------------------------------------

class TestGuard(unittest.TestCase):
    def test_ok(self):
        data = make_zip("truwer", "1.0.5")
        self.assertEqual(P.guard_zip("truwer_1_0_5.zip", data), "1.0.5")

    def test_mismatch_ts(self):
        with self.assertRaises(P.PublishError):
            P.guard_zip("truwer_1_0_5.zip", make_zip("truwer", "1.0.5", ts_ver="1.0.4"))

    def test_mismatch_plugin_json(self):
        with self.assertRaises(P.PublishError):
            P.guard_zip("truwer_1_0_5.zip", make_zip("truwer", "1.0.5", json_ver="9.9.9"))

    def test_missing_plugin_json(self):
        with self.assertRaises(P.PublishError):
            P.guard_zip("truwer_1_0_5.zip", make_zip("truwer", "1.0.5", no_json=True))

    def test_double_plugin_version(self):
        with self.assertRaises(P.PublishError):
            P.guard_zip("truwer_1_0_5.zip", make_zip("truwer", "1.0.5", double_ts=True))

    def test_bad_zip_name(self):
        with self.assertRaises(P.PublishError):
            P.guard_zip("truwer.zip", make_zip("truwer", "1.0.5"))


# ---------------------------------------------------------------------------
# Multipart
# ---------------------------------------------------------------------------

class TestMultipart(unittest.TestCase):
    def test_fields_and_file(self):
        body, ctype = P.build_multipart(
            [("slug", "truwer"), ("version", "1.0.5")], "file", "truwer_1_0_5.zip", b"ZIPDATA",
            boundary="TESTBOUNDARY")
        self.assertIn("multipart/form-data; boundary=TESTBOUNDARY", ctype)
        self.assertIn(b'name="slug"\r\n\r\ntruwer\r\n', body)
        self.assertIn(b'name="version"\r\n\r\n1.0.5\r\n', body)
        self.assertIn(b'name="file"; filename="truwer_1_0_5.zip"', body)
        self.assertIn(b"ZIPDATA", body)
        self.assertTrue(body.endswith(b"--TESTBOUNDARY--\r\n"))


# ---------------------------------------------------------------------------
# Przeplyw run() end-to-end na atraph HTTP
# ---------------------------------------------------------------------------

class TestRun(unittest.TestCase):
    def run_script(self, argv, env, fetcher):
        out, err = io.StringIO(), io.StringIO()
        rc = P.run(argv, env, fetcher, out, err)
        return rc, out.getvalue(), err.getvalue()

    def test_dry_run_reports_and_sends_nothing(self):
        f = make_flow_fetcher({"imperium-cal": "1.8.23", "ishtar-cal": "1.8.22", "truwer": "1.0.4"})
        f.add(P.RAW_ZIP_TEMPLATE.format(zip="truwer_1_0_5.zip"), make_zip("truwer", "1.0.5"))
        rc, out, _ = self.run_script([], {}, f)
        self.assertEqual(rc, 0)
        self.assertIn("DRY-RUN", out)
        self.assertEqual(f.posts, [])
        urls = [u for u, _ in f.gets]
        self.assertFalse(any("oidc" in u for u in urls), "bez --publish nie pobieramy tokenu OIDC")

    def test_no_new_versions_is_clean_noop(self):
        f = make_flow_fetcher({"imperium-cal": "1.8.23", "ishtar-cal": "1.8.22", "truwer": "1.0.5"})
        rc, out, _ = self.run_script(["--publish"], OIDC_ENV, f)
        self.assertEqual(rc, 0)
        self.assertIn("Brak nowych wersji", out)
        self.assertEqual(f.posts, [])
        urls = [u for u, _ in f.gets]
        self.assertFalse(any("oidc" in u for u in urls), "bez nowych wersji nie pobieramy tokenu OIDC")

    def test_publish_blocked_outside_actions(self):
        f = make_flow_fetcher({"imperium-cal": "1.8.23", "ishtar-cal": "1.8.22", "truwer": "1.0.4"})
        f.add(P.RAW_ZIP_TEMPLATE.format(zip="truwer_1_0_5.zip"), make_zip("truwer", "1.0.5"))
        rc, _, err = self.run_script(["--publish"], {}, f)
        self.assertEqual(rc, 1)
        self.assertIn("GitHub Actions", err)
        self.assertEqual(f.posts, [])

    def test_full_publish_flow(self):
        f = make_flow_fetcher({"imperium-cal": "1.8.23", "ishtar-cal": "1.8.22", "truwer": "1.0.4"})
        zip_data = make_zip("truwer", "1.0.5")
        f.add(P.RAW_ZIP_TEMPLATE.format(zip="truwer_1_0_5.zip"), zip_data)
        f.add(OIDC_FULL_URL, json.dumps({"value": "tok-123"}))
        f.add(P.REGISTRY + "/api/v1/plugins/truwer", json.dumps({"plugin": {"latestVersion": "1.0.5"}}))
        rc, out, _ = self.run_script(["--publish"], OIDC_ENV, f)
        self.assertEqual(rc, 0)
        self.assertIn("opublikowano truwer 1.0.5", out)
        self.assertEqual(len(f.posts), 1)
        url, body, headers = f.posts[0]
        self.assertEqual(url, P.REGISTRY + "/api/v1/publish")
        self.assertEqual(headers["Authorization"], "Bearer tok-123")
        self.assertIn("multipart/form-data", headers["Content-Type"])
        self.assertIn(b'name="slug"\r\n\r\ntruwer\r\n', body)
        self.assertIn(b'name="version"\r\n\r\n1.0.5\r\n', body)
        self.assertIn(b'name="changelog"\r\n\r\nWydanie 1.0.5\r\n', body)
        self.assertIn(b'filename="truwer_1_0_5.zip"', body)
        self.assertIn(zip_data, body)

    def test_custom_changelog(self):
        f = make_flow_fetcher({"imperium-cal": "1.8.23", "ishtar-cal": "1.8.22", "truwer": "1.0.4"})
        f.add(P.RAW_ZIP_TEMPLATE.format(zip="truwer_1_0_5.zip"), make_zip("truwer", "1.0.5"))
        f.add(OIDC_FULL_URL, json.dumps({"value": "tok-123"}))
        f.add(P.REGISTRY + "/api/v1/plugins/truwer", json.dumps({"plugin": {"latestVersion": "1.0.5"}}))
        rc, _, _ = self.run_script(["--publish", "--changelog", "Poprawki naglowka"], OIDC_ENV, f)
        self.assertEqual(rc, 0)
        self.assertIn("Poprawki naglowka".encode("utf-8"), f.posts[0][1])

    def test_guard_failure_aborts_before_any_post(self):
        f = make_flow_fetcher({"imperium-cal": "1.8.23", "ishtar-cal": "1.8.22", "truwer": "1.0.4"})
        f.add(P.RAW_ZIP_TEMPLATE.format(zip="truwer_1_0_5.zip"), make_zip("truwer", "1.0.5", ts_ver="1.0.4"))
        rc, _, err = self.run_script(["--publish"], OIDC_ENV, f)
        self.assertEqual(rc, 1)
        self.assertEqual(f.posts, [])

    def test_registry_verify_after_publish_must_match(self):
        f = make_flow_fetcher({"imperium-cal": "1.8.23", "ishtar-cal": "1.8.22", "truwer": "1.0.4"})
        f.add(P.RAW_ZIP_TEMPLATE.format(zip="truwer_1_0_5.zip"), make_zip("truwer", "1.0.5"))
        f.add(OIDC_FULL_URL, json.dumps({"value": "tok-123"}))
        # katalog po publikacji nadal pokazuje stara wersje -> blad
        f.add(P.REGISTRY + "/api/v1/plugins/truwer", json.dumps({"plugin": {"latestVersion": "1.0.4"}}))
        rc, _, err = self.run_script(["--publish"], OIDC_ENV, f)
        self.assertEqual(rc, 1)
        self.assertIn("weryfikacja", err.lower())

    def test_selected_plugin_uses_single_slug_query(self):
        f = FakeFetcher()
        f.add(P.PAGES_INDEX, json.dumps(index_doc()))
        f.add(P.REGISTRY + "/api/v1/plugins?slugs=truwer", json.dumps(registry_doc({"truwer": "1.0.5"})))
        rc, out, _ = self.run_script(["--plugin", "truwer"], {}, f)
        self.assertEqual(rc, 0)
        urls = [u for u, _ in f.gets]
        self.assertIn(P.REGISTRY + "/api/v1/plugins?slugs=truwer", urls)
        self.assertNotIn(LIST_URL_ALL, urls)


if __name__ == "__main__":
    unittest.main()

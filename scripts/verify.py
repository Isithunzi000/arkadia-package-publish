#!/usr/bin/env python3
"""Audyt katalogu: czy marketplace serwuje dokladnie nasz kod.

Dla kazdego pluginu (zrodlo prawdy: index.json na GitHub Pages):
  A) api      - stan w API katalogu: wersja, yanked, trustedPublisher,
                provenance.repositoryId przypiety do naszego repo
  B) package  - package.zip z katalogu vs zip z repo: identycznosc ZAWARTOSCI
                wpisow (sha256 per wpis; bajty kontenera moga sie roznic,
                bo katalog moze przepakowac archiwum)
  C) bundle   - plugin.js z katalogu vs nasz bundle z Pages: rownowaznosc
                semantyczna przez sonde importowa (Node): PluginInfo i slad
                rejestracji (aliasy/triggery/popupy/menu) musza byc 1:1;
                znormalizowany diff kodu jest tylko raportowy
  D) latest   - /latest/plugin.js musi byc bajtowo rowny przypietej wersji
  E) naglowki - plugin.js: Content-Type javascript + CORS dla klienta Dargoth;
                package.zip: MIME archiwum

Skrypt jest wylacznie odczytowy: zero POST-ow, zero tokenu OIDC.
Kod wyjscia 0 tylko gdy wszystkie twarde kontrole przechodza.

Uzycie:
  python3 scripts/verify.py [--plugin all|imperium-cal|ishtar-cal|truwer]
"""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

REGISTRY = "https://arkadia-package-repository.vercel.app"
PAGES_INDEX = "https://isithunzi000.github.io/arkadia-dargoth-plugins/index.json"
PAGES_BUNDLE = "https://isithunzi000.github.io/arkadia-dargoth-plugins/{name}.js"
RAW_ZIP_TEMPLATE = (
    "https://raw.githubusercontent.com/Isithunzi000/arkadia-dargoth-plugins/main/releases/{zip}"
)

# repositoryId przypiete przy pierwszej publikacji OIDC (anti-rename/anti-squat)
EXPECTED_REPO_ID = "1360323352"
# klient Dargoth laduje bundle cross-origin z tej domeny
CLIENT_ORIGIN = "https://delwing.github.io"

# (nazwa w index.json, slug w katalogu)
PLUGINS = (
    ("imperium_cal", "imperium-cal"),
    ("ishtar_cal", "ishtar-cal"),
    ("truwer", "truwer"),
)

ZIP_NAME_RX = re.compile(r"^([a-z0-9_]+)_(\d+)_(\d+)_(\d+)\.zip$")
PROBE = Path(__file__).resolve().with_name("import_probe.mjs")


class VerifyError(Exception):
    """Blad operacyjny audytu (fetch, sonda, format danych)."""


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict


class Fetcher:
    """Prawdziwy HTTP (urllib); atrapa w testach ma ten sam interfejs."""

    def get(self, url, headers=None):
        req = urllib.request.Request(
            url, headers={"User-Agent": "arkadia-package-verify/1.0", **(headers or {})}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return Response(resp.status, resp.read(), dict(resp.headers.items()))
        except urllib.error.HTTPError as e:
            raise VerifyError(f"GET {url}: HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise VerifyError(f"GET {url}: {e.reason}") from e


# ---------------------------------------------------------------------------
# Nazwy i pakiety
# ---------------------------------------------------------------------------

def parse_zip_name(zip_name):
    m = ZIP_NAME_RX.match(zip_name)
    if not m:
        raise VerifyError(f"nieoczekiwana nazwa zip: {zip_name!r}")
    return m.group(1), ".".join(m.group(2, 3, 4))


def zip_entry_hashes(zip_bytes):
    hashes = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for name in sorted(z.namelist()):
            if name.endswith("/"):
                continue
            hashes[name] = hashlib.sha256(z.read(name)).hexdigest()
    return hashes


def compare_packages(our_zip, pkg_zip):
    """Identycznosc zawartosci wpisow; bajty kontenera sa bez znaczenia."""
    problems = []
    ours, theirs = zip_entry_hashes(our_zip), zip_entry_hashes(pkg_zip)
    for name in sorted(set(ours) - set(theirs)):
        problems.append(f"brak wpisu w pakiecie katalogu: {name}")
    for name in sorted(set(theirs) - set(ours)):
        problems.append(f"nadmiarowy wpis w pakiecie katalogu: {name}")
    for name in sorted(set(ours) & set(theirs)):
        if ours[name] != theirs[name]:
            problems.append(f"rozna zawartosc wpisu: {name}")
    return problems


# ---------------------------------------------------------------------------
# Sonda importowa i slad rejestracji
# ---------------------------------------------------------------------------

def run_probe(bundle_path):
    node = shutil.which("node")
    if not node:
        raise VerifyError("node niedostepny w PATH (wymagany do sondy importowej)")
    proc = subprocess.run(
        [node, str(PROBE), str(bundle_path)],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        raise VerifyError(f"sonda importowa nie powiodla: {' | '.join(tail)}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise VerifyError(f"sonda importowa zwrocila nie-JSON: {e}") from e


def compare_footprints(registry_fp, pages_fp):
    problems = []
    if registry_fp.get("pluginInfo") != pages_fp.get("pluginInfo"):
        problems.append(
            "PluginInfo rozni sie: "
            f"katalog={json.dumps(registry_fp.get('pluginInfo'), sort_keys=True)} "
            f"pages={json.dumps(pages_fp.get('pluginInfo'), sort_keys=True)}"
        )
    for key in ("aliases", "triggers", "popups", "menus"):
        if registry_fp.get(key) != pages_fp.get(key):
            problems.append(f"{key}: katalog={registry_fp.get(key)} pages={pages_fp.get(key)}")
    return problems


# ---------------------------------------------------------------------------
# Stan API
# ---------------------------------------------------------------------------

def check_api_state(detail, expected_version):
    problems = []
    plugin = detail.get("plugin", {})
    latest = plugin.get("latestVersion")
    if latest != expected_version:
        problems.append(f"latestVersion {latest!r} != nasza wersja {expected_version!r}")
    if plugin.get("trustedPublisher") is not True:
        problems.append("trustedPublisher nie jest ustawione")
    entry = {v.get("version"): v for v in detail.get("versions", [])}.get(expected_version)
    if entry is None:
        problems.append(f"brak wersji {expected_version} w katalogu")
    else:
        if entry.get("yanked"):
            problems.append(f"wersja {expected_version} jest yanked")
        if entry.get("hasSources") is not True:
            problems.append(f"wersja {expected_version} nie ma zrodel (hasSources)")
        prov = entry.get("provenance") or {}
        if str(prov.get("repositoryId")) != EXPECTED_REPO_ID:
            problems.append(
                f"provenance.repositoryId {prov.get('repositoryId')!r} "
                f"!= oczekiwane {EXPECTED_REPO_ID}"
            )
    return problems


# ---------------------------------------------------------------------------
# Deklarowany sha256 katalogu
# ---------------------------------------------------------------------------

def check_declared_sha(detail, version, bundle_bytes):
    """Katalog deklaruje w API sha256 serwowanego bundle'a plugin.js.

    (package.zip serwowany jest bajtowo identyczny z naszym zipem, ale pole
    versions[].sha256 nie jest skrotem zipa - jest skrotem skompilowanego
    bundle'a; hipoteza potwierdzona audytem discovery, run 34171292539.)
    """
    declared = None
    for v in detail.get("versions", []):
        if v.get("version") == version:
            declared = v.get("sha256")
    if not declared:
        return [f"katalog nie deklaruje sha256 dla wersji {version}"]
    actual = hashlib.sha256(bundle_bytes).hexdigest()
    if declared != actual:
        return [f"sha256 bundle'a: katalog deklaruje {declared}, serwuje {actual}"]
    return []


# ---------------------------------------------------------------------------
# Naglowki
# ---------------------------------------------------------------------------

def check_js_headers(headers):
    problems = []
    h = {str(k).lower(): str(v) for k, v in headers.items()}
    ct = h.get("content-type", "")
    if "javascript" not in ct:
        problems.append(f"Content-Type {ct!r} nie jest javascript")
    if not h.get("access-control-allow-origin"):
        problems.append("brak naglowka Access-Control-Allow-Origin (CORS)")
    return problems


# ---------------------------------------------------------------------------
# Normalizacja diffa (raportowa)
# ---------------------------------------------------------------------------

def normalize_js(text):
    return "\n".join(line.rstrip() for line in text.splitlines() if line.strip())


# ---------------------------------------------------------------------------
# Przeplyw
# ---------------------------------------------------------------------------

def verify_one(name, slug, index_entry, fetcher, out):
    print(f"== {slug} ==", file=out)
    failures = []

    def report(step, problems):
        if problems:
            print(f"[FAIL] {step}", file=out)
            for p in problems:
                print(f"  - {p}", file=out)
            failures.extend(problems)
        else:
            print(f"[OK] {step}", file=out)

    zip_name = index_entry.get("zip", "")
    try:
        _, version = parse_zip_name(zip_name)
    except VerifyError as e:
        report("api", [str(e)])
        return False

    # A) stan API
    detail = None
    try:
        raw = fetcher.get(f"{REGISTRY}/api/v1/plugins/{slug}")
        detail = json.loads(raw.body.decode("utf-8"))
        report("api", check_api_state(detail, version))
    except (VerifyError, json.JSONDecodeError) as e:
        report("api", [f"odczyt API: {e}"])

    install = (detail or {}).get("install", {})
    package_url = install.get("packageUrl") or f"{REGISTRY}/r/{slug}/{version}/package.zip"
    pinned_url = f"{REGISTRY}/r/{slug}/{version}/plugin.js"
    latest_url = f"{REGISTRY}/r/{slug}/latest/plugin.js"

    # B) package.zip vs zip z repo
    pkg_resp = None
    try:
        our_zip = fetcher.get(RAW_ZIP_TEMPLATE.format(zip=zip_name)).body
        pkg_resp = fetcher.get(package_url)
        report("package", compare_packages(our_zip, pkg_resp.body))
        declared = None
        for v in (detail or {}).get("versions", []):
            if v.get("version") == version:
                declared = v.get("sha256")
        print(
            f"[info] sha256: katalog_deklaruje={declared} "
            f"nasz_zip={hashlib.sha256(our_zip).hexdigest()[:16]}... "
            f"pakiet_katalogu={hashlib.sha256(pkg_resp.body).hexdigest()[:16]}...",
            file=out,
        )
    except VerifyError as e:
        report("package", [str(e)])

    # C) bundle: sonda importowa + znormalizowany diff (raportowy)
    reg_resp = None
    try:
        reg_resp = fetcher.get(pinned_url, headers={"Origin": CLIENT_ORIGIN})
        pages_resp = fetcher.get(PAGES_BUNDLE.format(name=name))
        with tempfile.TemporaryDirectory(prefix="verify-") as tmp:
            reg_path = Path(tmp) / "registry.js"
            pages_path = Path(tmp) / "pages.js"
            reg_path.write_bytes(reg_resp.body)
            pages_path.write_bytes(pages_resp.body)
            reg_fp = run_probe(str(reg_path))
            pages_fp = run_probe(str(pages_path))
        problems = compare_footprints(reg_fp, pages_fp)
        if detail is not None:
            problems += check_declared_sha(detail, version, reg_resp.body)
        report("bundle", problems)
        reg_norm = normalize_js(reg_resp.body.decode("utf-8", "replace"))
        pages_norm = normalize_js(pages_resp.body.decode("utf-8", "replace"))
        if reg_norm != pages_norm:
            import difflib
            diff = list(difflib.unified_diff(
                pages_norm.splitlines(), reg_norm.splitlines(),
                fromfile="pages", tofile="katalog", lineterm="",
            ))
            print(f"[info] bundle: znormalizowany diff ma {len(diff)} linii (raportowy)", file=out)
            for line in diff[:40]:
                print(f"  {line}", file=out)
    except VerifyError as e:
        report("bundle", [str(e)])

    # D) latest == przypieta wersja (bajtowo)
    try:
        latest_resp = fetcher.get(latest_url, headers={"Origin": CLIENT_ORIGIN})
        if reg_resp is None:
            report("latest", ["brak przypietego bundle'a do porownania"])
        elif latest_resp.body != reg_resp.body:
            report("latest", ["/latest/plugin.js rozni sie bajtowo od przypietej wersji"])
        else:
            report("latest", [])
    except VerifyError as e:
        report("latest", [str(e)])

    # E) naglowki
    problems = []
    if reg_resp is not None:
        problems.extend(check_js_headers(reg_resp.headers))
    else:
        problems.append("brak odpowiedzi plugin.js do kontroli naglowkow")
    if pkg_resp is not None:
        h = {str(k).lower(): str(v) for k, v in pkg_resp.headers.items()}
        ct = h.get("content-type", "")
        if "zip" not in ct and "octet-stream" not in ct:
            problems.append(f"package.zip Content-Type {ct!r} nie jest archiwum")
    report("naglowki", problems)

    return not failures


def run(argv, env, fetcher, out, err):
    parser = argparse.ArgumentParser(prog="verify")
    parser.add_argument("--plugin", default="all",
                        choices=["all"] + [slug for _, slug in PLUGINS])
    args = parser.parse_args(argv)

    try:
        index = json.loads(fetcher.get(PAGES_INDEX).body.decode("utf-8"))
    except (VerifyError, json.JSONDecodeError) as e:
        print(f"BLAD: index.json na Pages: {e}", file=err)
        return 1
    by_name = {p.get("name"): p for p in index.get("plugins", [])}

    selected = PLUGINS if args.plugin == "all" else tuple(
        (n, s) for n, s in PLUGINS if s == args.plugin
    )
    all_ok = True
    for name, slug in selected:
        entry = by_name.get(name)
        if entry is None:
            print(f"== {slug} ==", file=out)
            print("[FAIL] api", file=out)
            print(f"  - brak pluginu {name!r} w index.json", file=out)
            all_ok = False
            continue
        if not verify_one(name, slug, entry, fetcher, out):
            all_ok = False

    print("WYNIK: OK" if all_ok else "WYNIK: BLEDY", file=out)
    return 0 if all_ok else 1


def main():
    return run(sys.argv[1:], dict(os.environ), Fetcher(), sys.stdout, sys.stderr)


if __name__ == "__main__":
    import os
    raise SystemExit(main())

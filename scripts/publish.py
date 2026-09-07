#!/usr/bin/env python3
"""Publikacja pluginow Dargoth do katalogu (arkadia-package-repository).

Zrodlo prawdy "co wydane":  index.json na GitHub Pages repo arkadia-dargoth-plugins.
Zrodlo prawdy "co w katalogu": publiczne API katalogu /api/v1/plugins.

Publikuje wylacznie wersje scisle nowsze niz w katalogu (idempotentnie).
Prawdziwa publikacja mozliwa jest tylko w GitHub Actions (token OIDC);
lokalnie skrypt dziala zawsze w trybie dry-run.

Uzycie:
  python3 scripts/publish.py [--plugin all|imperium-cal|ishtar-cal|truwer]
                             [--publish] [--changelog "tekst"]
"""

import argparse
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass

REGISTRY = "https://arkadia-package-repository.vercel.app"
PAGES_INDEX = "https://isithunzi000.github.io/arkadia-dargoth-plugins/index.json"
RAW_ZIP_TEMPLATE = (
    "https://raw.githubusercontent.com/Isithunzi000/arkadia-dargoth-plugins/main/releases/{zip}"
)
OIDC_AUDIENCE = "arkadia-plugins"

# (nazwa w index.json, slug w katalogu) - kolejnosc determinuje kolejnosc publikacji
PLUGINS = (
    ("imperium_cal", "imperium-cal"),
    ("ishtar_cal", "ishtar-cal"),
    ("truwer", "truwer"),
)

VERSION_RX = re.compile(r"^\d+\.\d+\.\d+$")
ZIP_NAME_RX = re.compile(r"^([a-z0-9_]+)_(\d+)_(\d+)_(\d+)\.zip$")
PLUGIN_VERSION_RX = re.compile(r'const\s+PLUGIN_VERSION\s*=\s*"([^"]+)"')


class PublishError(Exception):
    """Blad operacyjny publikacji (fetch, guard, weryfikacja)."""


@dataclass
class Plan:
    name: str
    slug: str
    zip_name: str
    version: str
    registry_version: str | None
    action: str  # "publish" | "skip"
    reason: str


# ---------------------------------------------------------------------------
# Wersje i nazwy
# ---------------------------------------------------------------------------

def parse_version(v):
    if not isinstance(v, str) or not VERSION_RX.match(v):
        raise PublishError(f"nieprawidlowa wersja semver: {v!r}")
    return tuple(int(x) for x in v.split("."))


def version_newer(a, b):
    return parse_version(a) > parse_version(b)


def parse_zip_name(zip_name):
    m = ZIP_NAME_RX.match(zip_name)
    if not m:
        raise PublishError(f"nieparsowalna nazwa zipa: {zip_name!r}")
    return m.group(1), ".".join(m.group(2, 3, 4))


# ---------------------------------------------------------------------------
# Plan publikacji
# ---------------------------------------------------------------------------

def build_plan(index_doc, registry_doc, selected=None):
    if selected is not None:
        known = {slug for _, slug in PLUGINS}
        unknown = selected - known
        if unknown:
            raise PublishError(f"nieznany slug: {sorted(unknown)}")

    by_name = {}
    for entry in index_doc.get("plugins", []):
        by_name[entry.get("name")] = entry

    registry_by_slug = {item.get("slug"): item.get("latestVersion") for item in registry_doc.get("items", [])}

    plan = []
    for name, slug in PLUGINS:
        if selected is not None and slug not in selected:
            continue
        if name not in by_name:
            raise PublishError(f"brak pluginu {name!r} w index.json")
        zip_name = by_name[name].get("zip", "")
        _, version = parse_zip_name(zip_name)
        registry_version = registry_by_slug.get(slug)
        if registry_version is None:
            action, reason = "publish", "brak wpisu w katalogu"
        elif version_newer(version, registry_version):
            action, reason = "publish", f"nowsza wersja ({registry_version} -> {version})"
        else:
            action, reason = "skip", "brak nowej wersji"
        plan.append(Plan(name, slug, zip_name, version, registry_version, action, reason))
    return plan


# ---------------------------------------------------------------------------
# Guard wersji na zipie
# ---------------------------------------------------------------------------

def _single_entry(names, suffix):
    found = [n for n in names if n == suffix or n.endswith("/" + suffix)]
    if len(found) != 1:
        raise PublishError(f"zip: oczekiwano dokladnie jednego {suffix}, znaleziono {len(found)}")
    return found[0]


def guard_zip(zip_name, data):
    """Sprawdza: wersja z nazwy zipa == PLUGIN_VERSION == plugin.json metadata.version."""
    _, version = parse_zip_name(zip_name)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        ts = z.read(_single_entry(names, "index.ts")).decode("utf-8")
        found = PLUGIN_VERSION_RX.findall(ts)
        if len(found) != 1:
            raise PublishError(f"{zip_name}: oczekiwano dokladnie jednego PLUGIN_VERSION, znaleziono {len(found)}")
        if found[0] != version:
            raise PublishError(f"{zip_name}: PLUGIN_VERSION={found[0]} != wersja z nazwy {version}")
        pj = json.loads(z.read(_single_entry(names, "plugin.json")).decode("utf-8"))
        meta_version = (pj.get("metadata") or {}).get("version")
        if meta_version != version:
            raise PublishError(f"{zip_name}: plugin.json metadata.version={meta_version!r} != {version}")
        if "version" in pj and pj["version"] != version:
            raise PublishError(f"{zip_name}: plugin.json version={pj['version']!r} != {version}")
    return version


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def build_multipart(fields, file_field, filename, data, boundary=None):
    boundary = boundary or uuid.uuid4().hex
    body = io.BytesIO()
    for key, value in fields:
        body.write(f"--{boundary}\r\n".encode())
        body.write(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        body.write(str(value).encode("utf-8"))
        body.write(b"\r\n")
    body.write(f"--{boundary}\r\n".encode())
    body.write(f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode())
    body.write(b"Content-Type: application/zip\r\n\r\n")
    body.write(data)
    body.write(f"\r\n--{boundary}--\r\n".encode())
    return body.getvalue(), f"multipart/form-data; boundary={boundary}"


class Fetcher:
    def __init__(self, timeout=60):
        self.timeout = timeout

    def get(self, url, headers=None):
        req = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            raise PublishError(f"GET {url}: HTTP {e.code}: {e.read()[:500]!r}")
        except urllib.error.URLError as e:
            raise PublishError(f"GET {url}: {e}")

    def post(self, url, body, headers):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            raise PublishError(f"POST {url}: HTTP {e.code}: {e.read()[:500]!r}")
        except urllib.error.URLError as e:
            raise PublishError(f"POST {url}: {e}")


def get_oidc_token(fetcher, env):
    req_url = env.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    req_token = env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not req_url or not req_token:
        raise PublishError(
            "publikacja mozliwa tylko w GitHub Actions "
            "(brak ACTIONS_ID_TOKEN_REQUEST_URL / ACTIONS_ID_TOKEN_REQUEST_TOKEN)"
        )
    url = f"{req_url}&audience={OIDC_AUDIENCE}"
    payload = json.loads(fetcher.get(url, headers={"Authorization": f"bearer {req_token}"}))
    token = payload.get("value")
    if not token:
        raise PublishError("GitHub nie zwrocil tokenu OIDC")
    return token


# ---------------------------------------------------------------------------
# Przeplyw glowny
# ---------------------------------------------------------------------------

def parse_args(argv):
    ap = argparse.ArgumentParser(description="Publikacja pluginow Dargoth do katalogu")
    ap.add_argument("--plugin", default="all",
                    choices=["all"] + [slug for _, slug in PLUGINS])
    ap.add_argument("--publish", action="store_true",
                    help="naprawde publikuj (domyslnie tylko raport, dry-run)")
    ap.add_argument("--changelog", default=None,
                    help="changelog publikacji (domyslnie: 'Wydanie X.Y.Z')")
    return ap.parse_args(argv)


def run(argv, env, fetcher, out, err):
    try:
        args = parse_args(argv)
        selected = None if args.plugin == "all" else {args.plugin}

        slugs = [slug for _, slug in PLUGINS if selected is None or slug in selected]
        list_url = f"{REGISTRY}/api/v1/plugins?slugs={','.join(slugs)}"

        index_doc = json.loads(fetcher.get(PAGES_INDEX))
        registry_doc = json.loads(fetcher.get(list_url))
        plan = build_plan(index_doc, registry_doc, selected)

        for p in plan:
            reg = p.registry_version if p.registry_version is not None else "-"
            print(f"{p.slug}: nasza {p.version} / katalog {reg} -> {p.action} ({p.reason})", file=out)

        to_publish = [p for p in plan if p.action == "publish"]
        if not to_publish:
            print("Brak nowych wersji - nic do publikacji.", file=out)
            return 0

        # Guard: pobierz i zweryfikuj kazdego kandydata zanim cokolwiek wyslemy
        guarded = []
        for p in to_publish:
            data = fetcher.get(RAW_ZIP_TEMPLATE.format(zip=p.zip_name))
            version = guard_zip(p.zip_name, data)
            guarded.append((p, data, version))
            print(f"guard OK: {p.zip_name} (wersja {version})", file=out)

        if not args.publish:
            print(f"DRY-RUN: do publikacji: {', '.join(p.slug for p, _, _ in guarded)} "
                  f"(uruchom z --publish w GitHub Actions)", file=out)
            return 0

        for p, data, version in guarded:
            # katalog odrzuca ponownie uzyty token OIDC (HTTP 422) - swiezy token per publikacja
            token = get_oidc_token(fetcher, env)
            changelog = args.changelog or f"Wydanie {version}"
            body, ctype = build_multipart(
                [("slug", p.slug), ("version", version), ("changelog", changelog)],
                "file", p.zip_name, data)
            fetcher.post(f"{REGISTRY}/api/v1/publish", body,
                         {"Authorization": f"Bearer {token}", "Content-Type": ctype})

            # Weryfikacja po publikacji: katalog musi pokazywac wlasnie wyslana wersje
            detail = json.loads(fetcher.get(f"{REGISTRY}/api/v1/plugins/{p.slug}"))
            latest = (detail.get("plugin") or {}).get("latestVersion")
            if latest != version:
                raise PublishError(
                    f"weryfikacja po publikacji {p.slug}: katalog pokazuje {latest!r}, oczekiwano {version}")
            print(f"opublikowano {p.slug} {version}", file=out)
        return 0
    except PublishError as e:
        print(f"BLAD: {e}", file=err)
        return 1


def main():
    sys.exit(run(sys.argv[1:], os.environ, Fetcher(), sys.stdout, sys.stderr))


if __name__ == "__main__":
    main()

# arkadia-package-publish

Automatyczna publikacja pluginów Dargoth do katalogu [Dargoth Client Plugins](https://arkadia-package-repository.vercel.app) z użyciem GitHub Actions OIDC (trusted publisher).

Obsługiwane pluginy (źródła i release'y: [arkadia-dargoth-plugins](https://github.com/Isithunzi000/arkadia-dargoth-plugins)):

| Plugin | Slug w katalogu |
|---|---|
| Kalendarz Imperium | `imperium-cal` |
| Kalendarz Ishtar | `ishtar-cal` |
| Truwer | `truwer` |

## Jak to działa

1. Workflow pobiera `index.json` z GitHub Pages repo z pluginami (źródło prawdy o wydanych wersjach).
2. Porównuje wersje z publicznym API katalogu (`/api/v1/plugins`).
3. Publikuje wyłącznie wersje **ściśle nowsze** — powtórne uruchomienie bez nowego release'u jest no-op.
4. Przed wysyłką guard wersji: wersja z nazwy zipa == `PLUGIN_VERSION` w `index.ts` == `metadata.version` w `plugin.json`.
5. Publikacja: `POST /api/v1/publish` z tokenem OIDC wystawianym przez GitHub na czas jednego uruchomienia (audience `arkadia-plugins`). W repo nie ma żadnych sekretów.
6. Po publikacji weryfikacja: katalog musi pokazywać właśnie wysłaną wersję, inaczej run kończy się błędem.
7. Po każdej prawdziwej publikacji uruchamia się obowiązkowy audyt `verify` (twarda bramka — run czerwony, gdy katalog serwuje coś innego niż nasz kod).

## Audyt katalogu (verify)

`scripts/verify.py` sprawdza, czy katalog serwuje dokładnie nasz kod. Jest wyłącznie odczytowy: zero POST-ów, zero tokenu OIDC. Dla każdego pluginu:

| Krok | Kontrola |
|---|---|
| `api` | `latestVersion` == nasza wersja, brak `yanked`, `trustedPublisher`, `provenance.repositoryId` przypięte do repo |
| `package` | `package.zip` z katalogu vs zip z repo — identyczność **zawartości wpisów** (sha256 per wpis; bajty kontenera mogą się różnić, bo katalog może przepakować archiwum) |
| `bundle` | `plugin.js` z katalogu vs nasz bundle z Pages — równoważność semantyczna przez sondę importową (Node): `PluginInfo` + ślad rejestracji (aliasy/triggery/popupy/menu) 1:1; znormalizowany diff kodu jest raportowy |
| `latest` | `/latest/plugin.js` bajtowo równy przypiętej wersji |
| `naglowki` | `plugin.js`: `Content-Type: javascript` + CORS (`Access-Control-Allow-Origin`) dla klienta Dargoth; `package.zip`: MIME archiwum |

Kod wyjścia 0 tylko gdy wszystkie kontrole przechodzą.

## Uruchomienie

**Actions → Publish plugins → Run workflow**, parametry:

- **plugin** — `all` albo pojedynczy slug,
- **publish** — `false` = tylko raport (dry-run, domyślnie), `true` = prawdziwa publikacja (zawsze z audytem na końcu),
- **changelog** — opcjonalny tekst changelogu (domyślnie `Wydanie X.Y.Z`),
- **verify** — `true` = samodzielny audyt katalogu bez publikacji (przy `publish=true` audyt wykonuje się zawsze).

## Lokalnie

```bash
python3 -m unittest discover -s tests -v   # testy
python3 scripts/publish.py                 # dry-run (publikacja lokalnie jest zablokowana - brak tokenu OIDC)
python3 scripts/verify.py                  # audyt katalogu (wymaga node w PATH)
```

## Licencja

[AGPL-3.0](LICENSE)

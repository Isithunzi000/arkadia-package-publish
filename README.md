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

## Uruchomienie

**Actions → Publish plugins → Run workflow**, parametry:

- **plugin** — `all` albo pojedynczy slug,
- **publish** — `false` = tylko raport (dry-run, domyślnie), `true` = prawdziwa publikacja,
- **changelog** — opcjonalny tekst changelogu (domyślnie `Wydanie X.Y.Z`).

## Lokalnie

```bash
python3 -m unittest discover -s tests -v   # testy
python3 scripts/publish.py                 # dry-run (publikacja lokalnie jest zablokowana - brak tokenu OIDC)
```

## Licencja

[AGPL-3.0](LICENSE)

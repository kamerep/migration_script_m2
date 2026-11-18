# M2 → CSP Migration: developer guide to the UI

Prepared by Peter Kamere · UI version 1.1 · 18 November 2026

For a developer or a coding agent who has to apply, extend or review the UI of the migration tool.
`AGENTS.md` has the repository map and the rules that are not negotiable; this guide is the how-to.

---

## 1. Read this first if you are an agent

**Never load the change set or the big files into the conversation.** The full patch is about 1.2 MB because it
carries PDFs, PNGs and an ICO as base64: attaching it costs over a million tokens and the request is refused
("Prompt is too long"). `m2_ui.html` alone is about 147 KB.

| Do | Do not |
|---|---|
| Apply the change from disk with one command (section 2) | Attach or paste `*.patch`, `*.pdf`, `*.png`, `*.ico` |
| `grep -n` for a function or a CSS class, then read 40 to 80 lines around it | Read `m2_ui.html` whole |
| Rebuild binaries with the two scripts in `tools/` | Read, diff or hand-edit binaries |
| Run `bin/m2ctl.sh test` after every change | Read `runs/`, `sample_m2*/`, `logs/` (data, not code) |

Useful entry points:

```bash
grep -n "^function \|^async function \|^const [A-Z_]* = " m2_ui.html   # index of the page script
grep -n "^/\* ----------" m2_ui.html                                    # index of the CSS sections
grep -n "^def \|^ROUTES" m2_ui.py                                       # index of the server
```

---

## 2. Applying the change set

The change is delivered as `m2-ui-enhancements.zip`: the changed and new files in their repository paths.

```bash
cd /data/migration_script
git status --short                       # must be clean; commit or stash first
git checkout -b ui-enhancements
unzip -o ~/Downloads/m2-ui-enhancements.zip -d .
git status --short                       # expect 13 modified, 16 new (section 3)
bin/m2ctl.sh deploy                      # tests, restart on the new code, health check
```

`deploy` stops at the first failure and leaves the running UI as it was. Then open `http://127.0.0.1:8796/`,
hard-reload once (`Ctrl+Shift+R`) and walk the checklist in section 9.

Roll back: `git checkout main && git branch -D ui-enhancements && bin/m2ctl.sh restart`.

Not touched by the change: `migration_script_m2_odoo.py`, `m2_ai.py`, `m2_mcp.py`, `m2_config.json`,
`bin/m2ctl.sh`, the n8n workflows. A run produces the same files as before.

---

## 3. What changed, by file

| File | Change |
|---|---|
| `m2_ui.html` | Rewritten page: side bar, palettes, toasts, loaders, progress, Docs tab, command palette, links |
| `m2_ui.py` | `/api/docs`, `/api/routes`, `/api/doc-file`, `/assets/<file>`, `unset` in `/api/decide`, docstring on every route, `version` and `home` in `/api/state`, `started` and `source` in `/api/events` |
| `tests/test_ui.py` | New: 14 tests of the page and the server |
| `assets/` | New: `icon.svg`, `favicon.svg`, `logo.svg`, `logo-dark.svg` (sources) and built PNG, ICO, web manifest |
| `tools/make_icons.py` | New: rebuilds the raster icons from the SVG sources |
| `tools/make_docs_pdf.sh` | New: rebuilds the PDF of each guide from its HTML |
| `docs/*.html`, `docs/*.pdf` | Author line, A4 page rule, default palette colours; "Running and the UI" rewritten for the new page |
| `docs/DEVELOPER_GUIDE.md` | This guide |
| `AGENTS.md`, `README.md` | New agent guide; UI section of the README rewritten |

---

## 4. How the page is built

One file, no build step, no external resource (the CSP is `default-src 'self'`). The server reads it from disk on
every page load: edit, reload the browser. `m2_ui.py` needs a restart.

Order inside `m2_ui.html`: `<style>` (tokens, palettes, base, controls, layout, surfaces, tables, states, toasts,
overlays, sections) → SVG icon sprite → markup shell → one `<script>`.

### State and rendering

* `S` is the whole page state: `run`, `state` (`loading`, `idle`, `running`, `done`, `failed`), `events`,
  `summary`, `info` (settings and metadata from `/api/state`), `ai`, `tab`, filters.
* `render()` = `renderShell()` (side bar, run strip, status pill, banners, title) + `RENDER[S.tab]()`.
  Every section is a function that rebuilds its `<section class="panel" data-tab="…">` from `S`.
* `softRender()` is used by background refreshes: it does not rebuild a section while somebody types in it.
* `poll()` reads `/api/events?since=` every 400 ms while a run goes; `loadAI()` reads `/api/ai` every 1.5 s while
  the assistant works. Both reconnect by themselves when the server stops answering (`S.offline`).

### Navigation

`go(tab, arg)` switches section and writes the address; `fromHash()` reads it back.
Addresses: `#decide`, `#records/<M2 key>`, `#journal/<RULE_CODE>`, `#docs/<guide id>`, and `?run=<stamp>`.

### Feedback helpers (use these, do not invent new ones)

| Helper | Use |
|---|---|
| `toast(msg, {type, detail, action, ms})` | `type`: `success`, `error`, `warn`, `info`, `busy`. `action` is `{label, fn}`. Errors stay 9 s |
| `track(label, promise, done, opt)` | One toast that shows a spinner, then the result or the error |
| `busy(button, asyncFn)` | Spinner on the button, blocks double clicks |
| `skeleton()` | Placeholder while a section has no data yet |
| `empty(icon, title, text, action)` | Empty state with what to do next |
| `api(path, body, {quiet})` | All calls. Drives the top activity bar unless `quiet`. Throws `Error` with `.status` or `.offline` |
| `decide(body, msg, undo)` | Saves one decision, shows the toast with Undo, re-renders in place |
| `esc(value)` | Every value that comes from data goes through it before it enters HTML |
| `ic('name')` | Icon from the sprite |

---

## 5. Colours and palettes

Colours are CSS variables only. Never write a hex value in a rule or in script.

| Token | Meaning |
|---|---|
| `--bg`, `--surface`, `--surface-2`, `--surface-3`, `--border`, `--border-strong`, `--text`, `--text-muted` | Neutrals (light and dark sets) |
| `--accent`, `--accent-hover`, `--accent-contrast` | Fills: primary button, active chip, progress |
| `--link` | Accent as text: links, M2 keys, focus ring |
| `--side-bg`, `--side-text` | Side bar; everything in it is mixed from `--side-text` |
| `--k1`…`--k4`, `--k1t`…`--k4t` | The four tones of the count cards and their text colour |
| `--danger`, `--orange`, `--warn`, `--info`, `--violet`, `--ok` | Levels: error, review, warning, decision, change, done. **Same in every palette** |

Five palettes: `aubergine` (default, in `:root`), `mauve`, `forest`, `lime`, `rose`. The choice is stored per
browser (`localStorage.m2palette`) and set as `data-palette` on `<html>`; `data-theme` is always `light` or `dark`.

**Add a palette**

1. In the palettes block of the CSS add `[data-palette="name"] { … }` with the light values of `--accent*`,
   `--link`, `--side-*`, `--k*`, and `:root[data-palette="name"][data-theme="dark"] { … }` with the dark values of
   `--accent*`, `--link`, `--side-*`.
2. Add `name: 'Label'` to `PALETTES` in the script. Settings, the side-bar button and `Ctrl+K` pick it up.
3. Check contrast: text on `--accent`, on each `--k*`, and `--link` on `--surface`, at least 4.5 : 1.

**Change the default palette**: move the wanted values into `:root` and its two dark blocks, change the fallback
`'aubergine'` in `setPalette`, `cyclePalette` and the init call, recolour `assets/*.svg` and the heading colours in the `<style>` of
`docs/*.html`, then run both scripts of section 8.

---

## 6. Recipes

**A new section**

1. `NAV`: add `['key', 'Label', 'icon']` to a group.
2. Markup: `<section class="panel" data-tab="key"></section>`.
3. Script: `function renderKey() { const el = panel('key'); … }` and `key: renderKey` in `RENDER`.
4. Handle the three states first: `S.state === 'loading'` → `skeleton()`, no run → `empty(…)`, then the data.

**A new icon**: add `<symbol id="i-name" viewBox="0 0 24 24">…</symbol>` to the sprite (stroke icons, 2 px, round
caps, no fill), use `ic('name')`. No emoji.

**A new API route**: a function `api_x(params)` with a one-line docstring, added to `ROUTES`. Raise `Error("…")`
for a message meant for the person (status 400). The docstring is the documentation: `/api/routes` and the Docs tab
show it, and the tests fail without it.

**A new kind of decision**: the migration emits the rule (`self.ev(level, 'CODE', message, key=…, fix=…)`); add
the code to `decisionsNeeded()` and a section to `renderDecide()` with `sec(id, count, title, tag, help, body)`;
if it is saved to the settings, add its type to `api_decide` (with `unset`), to `decided()` and to
`decisionProgress()`; add it to `m2_open_decisions` in `m2_mcp.py`.

**A new guide**: add `docs/<Name>.html` in the style of the others (it needs an `<h1>`); it appears in the Docs
tab by itself. Put its file stem in `GUIDE_ORDER` in `m2_ui.py` to place it. Run `tools/make_docs_pdf.sh`.

---

## 7. The server additions

| Route | Token | Notes |
|---|---|---|
| `GET /api/docs` | yes | Guides of `docs/*.html` (body only, no script or style) plus generated reference pages: HTTP API from `ROUTES`, MCP tools parsed from `m2_mcp.py` with `ast`, command line parsed from the header of `bin/m2ctl.sh` |
| `GET /api/routes` | yes | Every route with method and docstring, as JSON |
| `GET /api/doc-file?name=X.pdf` | yes | PDF copy of a guide; plain names inside `docs/` only |
| `GET /assets/<file>`, `/favicon.ico` | no | `.svg`, `.png`, `.ico`, `.webmanifest` directly inside `assets/` only. No data in them |
| `POST /api/decide` `{type: "unset", target, key, kind?, field?}` | yes | Takes one saved decision back (Undo) |

Unchanged and to keep: the server binds `127.0.0.1` only, every `/api/*` call needs the page token or the stable
API token, `/health` is the only data-free public route.

---

## 8. Binaries

```bash
python3 tools/make_icons.py     # needs rsvg-convert (sudo apt install librsvg2-bin) or inkscape, and Pillow
tools/make_docs_pdf.sh          # needs Chrome, Chromium or Brave; A4 from the @page rule of each guide
```

Edit the SVG or HTML source, run the script, commit source and output together.

---

## 9. Tests and acceptance

```bash
bin/m2ctl.sh test               # 40 tests: migration 19, AI 7, UI 14
```

`tests/test_ui.py` checks: no external resource in the page; one `<script>` that parses (needs `node`, skipped
without it); every icon used exists; `NAV`, panels and `RENDER` agree; the API refuses calls without a token;
assets serve only their own files; every route has a docstring and appears in the Docs reference; decide and undo
round-trip on a copy of the settings.

Walk this once in the browser after a deploy:

- [ ] Sample data › English M2 grids, then Run migration: the strip shows the stages, a "Run finished" message appears with a button
- [ ] Overview: status card, eight count cards in the palette tones, downloads work
- [ ] Needs decision: save a company → tick, Undo in the message, the bar counts it; Run again applies it
- [ ] Journal: filters, click an event → details, `↑` `↓` step, JSON copies
- [ ] Records: `#records/<key>` opens the trail; Link copies the address
- [ ] AI assistant: Ollama shows ready; Run all shows a progress bar per task, then suggestions with Accept
- [ ] Docs: five guides and four reference pages; search highlights; PDF opens
- [ ] Settings › This browser: each of the five palettes in light and in dark
- [ ] `Ctrl+K`, `?`, `[` `]`, `/` work; fold the side bar; narrow the window below 900 px
- [ ] Stop the tool (`bin/m2ctl.sh stop`): banner "Connection lost"; start it: "Connected again"

---

## 10. Known limits

* Tested in the cloud against a stand-in `import_spec.py` built from the column names the script uses. The first
  `bin/m2ctl.sh test` on the workstation is the check against the real template.
* The assistant's progress bar was exercised on its start and failure paths only (no Ollama in the build workspace).
* The Journal renders the last 1,500 matching events with "Show more"; it is not virtualised.
* On the Rose & sky palette the accent red sits close to the error red.
* Records, Finance and Changes & not loaded were restyled, not redesigned.

---

## 11. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "Prompt is too long" in an agent | A patch, PDF or image was attached. Start fresh (`/clear`), give the agent this guide and the path of the zip, nothing else |
| Old teal page after deploy | Browser cache of the tab: hard-reload. The server sends `no-store`, so one reload is enough |
| "Reload the page (token)" | The UI was restarted: the page token is new at every start. Reload |
| Docs tab: "The guides could not be loaded" | `docs/` missing or unreadable beside `m2_ui.py`; check `logs/m2_ui.log` |
| No logo or tab icon | `assets/` not deployed; `curl -I http://127.0.0.1:8796/assets/icon.svg` must answer 200 |
| `import_spec.py … not found` | Set `CSP_IMPORT_SPEC` to `csp_client_import/models/import_spec.py` |
| `test_script_parses` skipped | `node` is not installed; harmless, install it to enable the check |
| `make_icons.py` stops | Install `librsvg2-bin` and Pillow in the venv |

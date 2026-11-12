# web/: the front end

Local rules for the UI. The top-level `AGENTS.md` comes first; the design rules are in `docs/dev/ui-design.md`, and
the name, logo, voice and palette in `brand/GUIDELINES.md` (tokens in `brand/tokens.json`, kept equal to `style.css`).

- **Plain JS, no build step, no framework.** Files are served as they are. `web/vendor/` (MapLibre, three.js, Agora Web SDK) is
  git-ignored and vendored by the Dockerfile with checksums; never load from a CDN.
  The Agora Web SDK is vendored there the same way.
- **DOM through `h(tag, attrs, ...children)`** (`app.js`) or `createElement` + `textContent`. Never `innerHTML`,
  `outerHTML`, `insertAdjacentHTML` or `document.write`: robot and map names are user data.
- **Colours through tokens.** Define them in `style.css` (`:root`, plus the light theme block) and read them with
  `tok("--name", "#fallback")`; map colours via `MAP`, chart series via `SERIES`. Map colours are the same in both
  themes. `tests/test_guards.py` fails if a file gains a hard-coded colour.
- **Less text, more tooltips.** Labels are short; the explanation goes in a `title` tooltip or the Help pages
  (`docs/user/`). A paragraph on screen won't be read.
- **Settings rows** are `srow(name, help, control)` inside the right `sec*` function in `settings.js`; every
  "Settings → X" in copy is a deep link (`#settings/<section>`).
- **Robot-moving buttons** never act on one click: the server's interlocks decide, and the UI shows the reason it
  returns. Don't duplicate interlock logic in JS.
- **Works on a phone.** Check at 375 px wide: no horizontal scroll, tap targets at least 32 px.
- **Map interactions can't be tested in a hidden browser.** Use `scripts/e2e_studio.py` or `scripts/screenshots.py`
  (headless Edge or Chrome with Playwright) against the demo, never against a panel connected to a robot.
- After a visible change, run `scripts/screenshots.py` and look at every image you affected.

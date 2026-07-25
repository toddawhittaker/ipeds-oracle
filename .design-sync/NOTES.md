# design-sync notes — IPEDS Oracle

Repo-specific gotchas for future syncs. Read this before re-running.

## Run it from the repo root

```sh
node .ds-sync/package-build.mjs   --config .design-sync/config.json --node-modules ./frontend/node_modules --out ./ds-bundle
node .ds-sync/package-validate.mjs ./ds-bundle
```

`cfg.entry` is resolved with a bare `resolve()`, i.e. **relative to the working
directory, not the package** — hence `./frontend/ds-entry.js`. Running the build
from `frontend/` will not find it.

## This is an app, not a component library

- **No Storybook** anywhere → `shape: "package"`.
- **No library build.** `frontend/package.json` is `private` with no
  `main`/`module`/`exports`; `vite build` emits the SPA.
- **Zero TypeScript** — no `.ts`/`.tsx`/`.d.ts`, and `react/prop-types` is off in
  the eslint config, so nothing in the repo declares a prop anywhere.

### `frontend/ds-entry.js` is load-bearing — do not delete it

Without an explicit entry the converter synthesizes one with
`export * from "<each src file>"`, and **`export *` does not re-export a
module's `default`**. Almost every component here is `export default function X`,
so that fallback put only the icons and the two providers on
`window.IpedsOracle`: 18 components were missing from the bundle while still
getting preview cards. Symptom to watch for in the build log —
`exported PascalCase symbols: N; bundle export list: M` with M well under the
component count.

When a component becomes reusable, add it in **two** places: a named export in
`frontend/ds-entry.js` and a pin in `cfg.componentSrcMap`. Those two lists are
what this sync treats as the design system's public surface.

### Every prop contract is hand-written

`cfg.dtsPropsFor` carries all 45 bodies. There is no source of truth to
regenerate them from, so **when a component's props change, edit `dtsPropsFor`
by hand** or the design agent codes against a stale API.

The `.d.ts` parse gate needs **typescript 5** in `.ds-sync/node_modules`.
`npm i typescript` now installs **7.x**, whose Node API dropped
`createSourceFile`, and validate then reports the check as *"skipped — typescript
not in node_modules"* — misleading, since it is installed. Pin `typescript@^5`.

### Groups come from stub docs

All source files sit flat in `src/`, so the group heuristic yields `general` for
everything. `.design-sync/groups/<Name>.md` holds a 3-line frontmatter stub per
component (`category: Answer|Data|Navigation|Overlays|Input|Feedback|Icons`),
bound via `cfg.docsMap`. Those paths are package-relative and resolve **from
`PKG_DIR`**, which is `frontend/` — hence `../.design-sync/groups/…`.

## Known render warns (expected — not new)

- **`[FONT_MISSING]`** — "Iowan Old Style", "Palatino Linotype", "Palatino",
  "Book Antiqua", "Cascadia Code". These are **system-font stacks, by design**:
  `--serif` and `--mono` list preferred faces and fall back to Georgia / Times
  and Menlo / Consolas. The app has never shipped webfonts (it keeps the CSP's
  `script-src 'self'` untouched), and the leading faces are Apple/Microsoft
  system fonts that cannot be licensed for redistribution. Nothing to fix —
  designs render with the same fallbacks the app itself uses.

## Things learned while authoring previews

- **A nested `data-theme="dark"` does nothing.** The dark tokens are declared on
  `:root[data-theme="dark"]`, so a wrapper div keeps the LIGHT palette and
  produces a card that lies about the theme. Two such stories were written and
  removed. There is no way to show light and dark in different cells of one
  card; dark mode is a document-root, page-wide switch.
- **`DataTable.config` must carry `comparators` and `tiebreak`**, not just
  `fields`/`nouns` — `sortRows` does `Object.keys(comparators)` and the table
  renders blank with *"Cannot convert undefined or null to object"*. `rowKey` is
  an **accessor function**, not a field name. Both were wrong in the first draft
  of the contract and only the preview caught it.
- Overlay components (`AboutModal`, `ChartModal`, `ConfirmProvider`,
  `ToastProvider`) need `cardMode: "single"`; `Chart` needs `cardMode: "column"`
  or its stories overflow the grid cell.
- `CopyMenu`'s panel opens **upward**, so its open-state story needs top padding
  or the panel is clipped by the cell.
- `HelpPopover` opens on **focus**, `CopyMenu` on **click** — each has one story
  that drives that on mount so the panel is visible in a static card. Only one
  story per card may take focus.

## Re-sync risks — what can go stale silently

1. **`frontend/ds-entry.js` drifting from the component list.** A new component
   added to `src/` appears nowhere until it is exported here. Nothing warns.
2. **`cfg.dtsPropsFor` drifting from the real props.** Hand-written, so a prop
   rename in the app leaves the published contract wrong and the design agent
   will keep using the old name. Re-read the component sources on any sync that
   follows real UI work.
3. **Preview data is inlined.** Institution names, award counts and the version
   strings in the previews are literals; they are illustrative, not live, and
   will look dated eventually. That is fine — they are compositions, not data.
4. **The self-link is gone on purpose.** An earlier attempt created
   `frontend/node_modules/ipeds-query-web -> ..` to make `PKG_DIR` resolve. It
   is unnecessary now that `cfg.entry` is set, and `npm ci` deletes it anyway.
   Don't reintroduce it.
5. Toolchain at time of writing: node 22.23.1, playwright 1.61.1 (chromium
   build 1228, already cached), esbuild + ts-morph in `.ds-sync/`.

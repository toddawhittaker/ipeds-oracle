## Building with IPEDS Oracle

Every component is on `window.IpedsOracle` (loaded from the root `_ds_bundle.js`).

### Wrapping and setup

Three pieces of context. Compose them once at the root of any design:

```jsx
const { ToastProvider, ConfirmProvider, PreviewRouter } = window.IpedsOracle;

<PreviewRouter>          {/* only needed if you render UserMenu — it uses <Link> */}
  <ToastProvider>        {/* required for useToast(); Markdown calls it */}
    <ConfirmProvider>    {/* required for useConfirm() */}
      …your design…
    </ConfirmProvider>
  </ToastProvider>
</PreviewRouter>
```

`useToast()` returns `push(message, kind)` where kind is `""` | `"ok"` | `"error"`.
`useConfirm()` returns `confirm({ variant, title, body, confirmLabel, onConfirm })`
with variant `"warning"` | `"danger"`. `confirm()` is **not awaitable** — never
return a long-running promise from `onConfirm`, or the modal sits spinning.

**Theme.** Light is the base. Dark comes from `data-theme="dark"` on the
**document root** (`<html>`), or from the OS preference when no attribute is
set. The tokens are declared on `:root[data-theme="dark"]` only, so putting
`data-theme` on a nested `<div>` does nothing — such an element silently keeps
the light palette.

### The styling idiom: CSS custom properties

There is **no utility-class system** here — no Tailwind, no styled-props. Write
ordinary CSS for your own layout and take every colour and face from a token.

Colour: `--bg` `--panel` `--panel-2` `--line` `--line-strong` `--text` `--muted`
`--accent` `--accent-d` `--ochre` `--ochre-soft` `--user` `--danger` `--ok`
`--warn` `--on-fg` `--selected-tint`

Type: `--serif` (hero figures, headings) · `--mono` (SQL, captions, source lines).
Body text is the system sans and needs no token.

Two rules that are easy to get wrong:

- Text or an icon sitting on an `--accent` fill uses **`--on-fg`**, never a
  hardcoded `#fff`. This is the single most repeated bug in this codebase — the
  dark theme's accent is light, so white-on-accent fails contrast there.
- `--ochre` is the accent *rule* colour (the underline beneath a hero figure,
  the trend line), not a second text colour.

The reusable class vocabulary is small and real: `.card` · `.link` · `.muted` ·
`.field-label` (mono, letterspaced, uppercase caption) · `.figure` + `.fig-rule`
(the hero-statistic device) · `.num` (tabular lining numerals — put it on any
numeric cell) · `.grid.data` (tables) · `.thin-scroll` · `.sr-only` ·
`.modal-overlay` / `.modal` / `.modal-head` / `.modal-title` / `.modal-body` /
`.modal-actions` / `.modal-confirm` / `.modal-cancel`.

Anything not on that list, invent as your own class — don't guess at a name.

### Where the truth lives

`styles.css` `@import`s `_ds_bundle.css`, which carries **all** the tokens and
every component style. Read it before styling anything; it is the authority, not
this summary. Per component, `<Name>.d.ts` is the API contract and
`<Name>.prompt.md` is the usage reference.

### One idiomatic composition

```jsx
const { Figure, Chart } = window.IpedsOracle;

<section style={{ background: "var(--panel)", border: "1px solid var(--line)",
                  borderRadius: 10, padding: "20px 24px" }}>
  <Figure
    spec={{ value: "324,575", label: "Peak national nursing degrees",
            source: "IPEDS Completions · 2022" }}
    grounding="exact"
  />
  <Chart spec={{ x: "year", y: "awards", title: "Nursing degrees conferred",
                 data: [{ year: 2021, awards: 299444 }, { year: 2022, awards: 324575 },
                        { year: 2023, awards: 318206 }] }} initialTrend />
</section>
```

`Figure`'s `grounding` is positive-only: `exact` / `rounded` / `derived` earn a
quiet "✓ verified" mark; every other value renders **no mark and no warning**.
Never add an "unverified" state. And `Chart` only draws its trend line and
percent-change badge when the x-axis is time-like (`year`/`date`/`month`/
`quarter`/`day`) — across categories both are suppressed on purpose.

# Frontend development

The public dashboard and operator console are plain HTML/CSS/JavaScript under
`web/`. They use saved backend projections. The
[API contract](../architecture/FRONTEND_API.md) is the field/route reference;
[the pick-feed policy](../product/PICK_FEED.md) defines selection and access.

Run the actual paper app from the repository root:

```bash
python3 scripts/local_paper.py
```

Open `http://localhost:8080` and `/admin`. A static-only preview cannot verify
authentication, database projections or worker publication. Mock/stub tools are
for isolated rendering and must not be presented as real prediction evidence.

## Files and responsibilities

| Path | Responsibility |
| --- | --- |
| `web/index.html`, `web/css/index.css` | Public layout and styling |
| `web/js/api.js` | Shared API transport and endpoint wrappers |
| `web/js/auth.js`, `web/css/auth.css` | Account/session UI |
| `web/js/app.js` | Public rendering, date/order handling and refresh |
| `web/js/charts.js`, `web/js/calculator.js` | Analytics and calculators |
| `web/admin.html`, `web/js/admin.js`, `web/css/admin.css` | Operator console |
| `web/tests/` | Browser projection/rendering regressions run through Node |

Keep wire normalization in the shared adapter. Respect server-supplied lock,
price-readiness, provenance and empty-state fields. Do not fabricate offers,
market lines, live scores or arrays to fill the UI. Compare kickoff timestamps
as instants and preserve the backend's chronological display and quality ranks.

Access is enforced server-side. Client previews, hidden cards and tier parameters
cannot authorize data. Operator mutations require the current authenticated role
and CSRF flow. Never log credentials/session cookies, store secret provider keys
in browser storage or ship private JSON reports with assets.

## Verification and packaging

```bash
node web/tests/test_model_board_render.js
node web/tests/test_render_all_robustness.js
node web/tests/test_accumulator_math.js
node web/tests/test_dashboard_updates.js
python3 scripts/build_vercel.py
```

The build copies only allowed public assets into disposable `public/`, excluding
`web/data/`, tests and secrets. Source docs belong under `docs/`, not the asset
directory. After a code update, restart local processes and reload the browser
with its cache bypassed.

Check guest, normal tiers, owner preview, expired sessions, missing history,
empty qualifying feeds, stale prices, postponed matches and unavailable
providers. For temporary all-tier paper reads and exact-contract grading, use
[manual verification](../operations/MANUAL_VERIFICATION.md).

The [design system](DESIGN.md) describes styling. Marketing copy and legacy
menus are not evidence that billing, booking codes, live micro-markets or
executable accumulators are implemented.

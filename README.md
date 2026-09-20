# wcag-axe-crawler
[![PyPI](https://img.shields.io/pypi/v/wcag-axe-crawler)](https://pypi.org/project/wcag-axe-crawler/)

Crawl a site with **axe-core** and get an aggregated WCAG violation report — JSON
and readable HTML. Free, MIT.

```bash
pip install wcag-axe-crawler[browser]   # from PyPI, Python 3.9+
playwright install chromium             # the browser itself
npm i axe-core                          # or point --axe-path at an existing copy
wcag-axe-crawler --url https://example.com \
    --axe-path node_modules/axe-core/axe.min.js --out report

# or straight from a clone, no install:
python3 audit/crawl.py --url https://example.com \
    --axe-path node_modules/axe-core/axe.min.js --out report
```

Playwright is an extra, not a hard requirement: the crawler imports it only when
it actually starts crawling, so `--help`, argument validation and `--axe-path`
resolution all work without a browser installed, and a crawl without one exits 3
with the install command instead of a traceback.

```
Audited 5 page(s); 1 page(s) not audited.
Violations: 21 rule instance(s) across 27 element(s); worst impact: critical
JSON: report.json
HTML: report.html
```

Try it against the bundled deliberately-bad demo site:

```bash
python3 examples/serve_demo.py &          # serves examples/demo-site on :8765
wcag-axe-crawler --url http://127.0.0.1:8765/ \
    --axe-path node_modules/axe-core/axe.min.js --out report
```

## What it does

- crawls same-origin links to a configurable depth and page cap
- runs axe-core on every page, including JS-rendered content after a settle delay
- records for each violation the axe rule id, impact, the WCAG success criteria
  axe maps it to, the affected selector, the HTML snippet, and axe's own
  remediation text with its guidance link
- **reports pages it could not audit** rather than silently skipping them, so a
  gap in coverage stays visible
- handles timeouts, redirects, non-HTML responses and load failures without crashing

Options: `--max-pages`, `--depth`, `--include` / `--exclude`, `--settle-ms`, `--json-only`.

## The line worth reading

Every run prints:

```
Full conformance can be asserted from this audit: no
```

**No automated tool can certify WCAG conformance, and any that claims to is
wrong.** axe-core finds a subset of issues. It cannot tell you whether alt text
is *meaningful* rather than merely present, whether focus order is logical,
whether content makes sense when read aloud, whether instructions rely on colour
alone, or whether captions are accurate. Those need a person.

This tool tells you where the mechanical problems are, so you can spend human
effort on the parts a machine cannot see.

## Requirements

Python 3.9+, Playwright with Chromium (`pip install wcag-axe-crawler[browser] &&
playwright install chromium` — or use your own interpreter that already has it),
and axe-core (`npm i axe-core`, MPL-2.0 — not bundled here, because bundling
third-party code would make its licence your problem). Set `AXE_CORE_PATH` instead
of passing `--axe-path` each time. The axe-core 4.13.0 rule dump ships as package
data for offline reference; the crawler reads its rules from axe-core in the
browser, not from that file.

## The full pack

The paid kit adds the **accessibility statement generator**, which refuses to
claim full conformance when the audit found violations (it exits 4 and writes
nothing); a **remediation library** with wrong-and-correct markup per axe rule
and why each matters to a screen-reader user; a **CI gate** with baselines so
accessibility does not regress; and the EAA/WCAG and manual-checks guides.

<!-- RELATED:START -->

## Related tools

- **[mv3-manifest-lint](https://github.com/duke5am/mv3-manifest-lint)** — Static linter for Chrome Manifest V3 extensions: the mistakes that get you rejected from the Web Store or break at runtime. 37 rules.
  *(if you were searching for "chrome extension manifest v3 errors")*
- **[openapi-breaking-change-lint](https://github.com/duke5am/openapi-breaking-change-lint)** — Diff two OpenAPI specs and classify every change as breaking, potentially breaking or compatible. CI gate plus API design lint, no dependencies.
  *(if you were searching for "openapi breaking changes")*
- **[schema-jsonld-validator](https://github.com/duke5am/schema-jsonld-validator)** — Validate JSON-LD structured data against the real schema.org vocabulary and Google's requirements, and warn on markup features Google has retired.
  *(if you were searching for "json-ld validator")*
- **[stripe-webhook-replay](https://github.com/duke5am/stripe-webhook-replay)** — Replay Stripe subscription webhooks at your local handler with real signatures: out-of-order delivery, dunning failures, duplicates and retries.
  *(if you were searching for "replay stripe webhooks locally")*
- **[vscode-extension-lint](https://github.com/duke5am/vscode-extension-lint)** — Static linter for a VS Code extension project: manifest, .vscodeignore and packaging mistakes that get you rejected or ship a broken extension.
  *(if you were searching for "vscode extension publishing errors")*
- **[webhook-signature-verify](https://github.com/duke5am/webhook-signature-verify)** — Verify Stripe, GitHub and Shopify webhook signatures correctly, plus race-free idempotency so a retried delivery can never execute twice.
  *(if you were searching for "stripe webhook signature verification")*

All 28 tools in this set, grouped by what they check: **[dev-tools-index](https://duke5am.github.io/dev-tools-index/)**

If you arrived here searching for one of these, this is the tool: **wcag accessibility audit tool** · **axe-core crawler** · **accessibility statement generator** · **european accessibility act check**

<!-- RELATED:END -->

→ **[Accessibility Audit Kit + Statement Generator](https://duke5am.gumroad.com/l/35-accessibility-audit)** — $39 on Gumroad <!-- GUMROAD-LINK -->

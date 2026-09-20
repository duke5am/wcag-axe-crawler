"""wcag_axe_crawler -- crawl a site with Playwright + axe-core and report WCAG violations.

The implementation lives in :mod:`wcag_axe_crawler.cli`; the
`wcag-axe-crawler` console script and the repository-root `audit/crawl.py`
wrapper both call ``cli.main``, so there is one copy of the code, not two.

Playwright is imported lazily, inside the crawl, so `--help` and argument
validation work on an interpreter that has never seen a browser. Install it with
``pip install wcag-axe-crawler[browser] && playwright install chromium``.

axe-core itself is deliberately NOT bundled (it is MPL-2.0 third-party code):
point at it with ``--axe-path`` or ``AXE_CORE_PATH``. The axe-core 4.13.0 rule
reference dump ships as package data in ``wcag_axe_crawler/``.
"""

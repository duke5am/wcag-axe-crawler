#!/usr/bin/env python3
"""crawl.py -- crawl a site with axe-core and report WCAG violations.

This wrapper exists so `python3 audit/crawl.py --url ...` keeps working from a
clone. The same CLI is installed as the `wcag-axe-crawler` console script; the
implementation lives in `wcag_axe_crawler/cli.py` so the installed package and
the checkout are the same code, not two versions of it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wcag_axe_crawler.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

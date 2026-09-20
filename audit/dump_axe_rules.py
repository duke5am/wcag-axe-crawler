#!/usr/bin/env python3
"""dump_axe_rules.py -- write axe-core's own rule metadata to JSON.

This wrapper exists so `python3 audit/dump_axe_rules.py --out FILE` keeps
working from a clone. The implementation lives in
`wcag_axe_crawler/dump_axe_rules.py`; it needs Playwright, because it launches
Chromium to ask axe-core for its rule list.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wcag_axe_crawler.dump_axe_rules import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

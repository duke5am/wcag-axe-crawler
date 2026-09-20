#!/usr/bin/env python3
"""Dump axe-core's own rule metadata to JSON.

Run this once if you want a local, offline reference of every rule axe-core
ships: rule id, impact, the WCAG success-criterion and EN 301 549 tags axe
publishes for it, and the help/helpUrl text. ``remediation/`` in this kit is
keyed by rule id and its WCAG/EN columns were checked against this dump rather
than typed from memory.

    python3 audit/dump_axe_rules.py --out axe-rules-reference.json

This repository ships the output for axe-core 4.13.0
(``wcag_axe_crawler/axe-rules-reference.json``, also installed as package data).
Re-run it after upgrading axe-core: the rule set does change between releases.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .cli import AXE_CANDIDATES, annotate_violation, resolve_axe_path

DUMP_JS = r"""
() => {
  const rules = (window.axe && window.axe.getRules) ? window.axe.getRules() : [];
  return {
    version: (window.axe && window.axe.version) || 'unknown',
    rules: rules.map((r) => ({
      id: r.ruleId,
      impact: r.impact || null,
      tags: r.tags || [],
      help: r.help || '',
      description: r.description || '',
      helpUrl: r.helpUrl || ''
    }))
  };
}
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Dump axe-core rule metadata to JSON.")
    parser.add_argument("--axe-path", default=None)
    parser.add_argument("--out", default="axe-rules-reference.json")
    parser.add_argument("--by-id", action="store_true",
                        help="also write a second file, <out>.byid.json, keyed by rule id")
    args = parser.parse_args(argv)

    axe_path, tried = resolve_axe_path(args.axe_path)
    if not axe_path:
        from crawl import axe_missing_message
        print(axe_missing_message(tried), file=sys.stderr)
        return 2

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True,
                                     args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
        page = browser.new_page()
        page.goto("about:blank")
        page.add_script_tag(path=axe_path)
        data = page.evaluate(DUMP_JS)
        browser.close()

    rules = [annotate_violation(r) for r in data["rules"]]
    rules.sort(key=lambda r: r["id"])
    doc = {
        "axe_core_version": data["version"],
        "axe_core_path": axe_path,
        "rule_count": len(rules),
        "note": ("WCAG success criteria and EN 301 549 clauses are derived mechanically "
                 "from axe-core's own tags; no mapping is invented by this kit. "
                 "'impact' is null here because axe.resolveRules() does not carry per-rule impact: "
                 "the impact that matters is the one reported on each violation in a real run. "
                 "A rule with no EN 301 549 clause tag is one axe-core publishes no clause mapping "
                 "for (for example target-size / SC 2.5.8, which is new in WCAG 2.2 and therefore "
                 "absent from EN 301 549 V3.2.1)."),
        "rules": rules,
    }

    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print("axe-core %s: %d rules -> %s" % (data["version"], len(rules), out))

    if args.by_id:
        by_id = {r["id"]: r for r in rules}
        with open(out + ".byid.json", "w", encoding="utf-8") as fh:
            json.dump(by_id, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print("wrote %s.byid.json" % out)

    with_wcag = sum(1 for r in rules if r["wcag_success_criteria"] or r["wcag_levels"])
    with_en = sum(1 for r in rules if r["en_301_549_clauses"])
    print("  with WCAG tags: %d | with EN 301 549 clause tags: %d" % (with_wcag, with_en))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

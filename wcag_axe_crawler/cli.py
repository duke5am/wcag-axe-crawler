#!/usr/bin/env python3
"""WCAG / EN 301 549 crawler for the European Accessibility Act Audit Kit.
This module is the implementation of the `wcag-axe-crawler` console
script. The repo-root `audit/crawl.py` is a thin wrapper around `main()`,
so `python3 audit/crawl.py ...` keeps working from a clone and the
installed script runs exactly the same code.

Crawls a site with headless Chromium (Playwright), injects axe-core into every
page it can load, collects the violations axe reports, and writes:

  * a machine-readable JSON report (consumed by statement/generate_statement.py
    and ci/gate.py), and
  * a human-readable, self-contained HTML report.

Everything the report says about a violation comes from axe-core itself: the
rule id, the impact, the WCAG success criteria (axe's own ``wcagXXXX`` tags),
the EN 301 549 clause tags axe publishes (``EN-9.x.x.x``), axe's ``help`` /
``description`` text and the ``helpUrl`` that links to Deque's rule page. This
tool does not invent remediation advice.

WHAT THIS TOOL CANNOT DO
------------------------
No automated tool can certify conformance. axe finds a subset of accessibility
problems -- roughly the machine-checkable ones. Things like whether alt text is
*meaningful*, whether focus order is *logical*, or whether form errors are
*announced usefully* need a human. See docs/MANUAL-CHECKS.md. A clean run here
is not a conformance certificate and the JSON says so explicitly.

Requires
--------
  * Python 3.9+ (the package declares >=3.9; the code itself runs on 3.8)
  * ``pip install playwright && playwright install chromium``
  * axe-core's browser bundle. Get it with ``npm i axe-core`` (MIT-licensed
    wrapper, the rules themselves are Mozilla Public License 2.0). Point at it
    with ``--axe-path`` or the ``AXE_CORE_PATH`` environment variable.

Usage
-----
  wcag-axe-crawler --url https://example.com --max-pages 25 --depth 2 \\
      --out ./report/example --settle-ms 1500

  wcag-axe-crawler --url https://example.com --json-only --exclude '*/tag/*'

  # from a clone, no install - the wrapper calls the same main()
  python3 audit/crawl.py --url https://example.com --out ./report/example

Exit codes
----------
  0  the crawl completed (violations or not -- use ci/gate.py to fail a build)
  2  usage / configuration error (bad URL, axe-core not found, ...)
  3  the browser could not be started at all
"""
from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import html
import json
import os
import re
import sys
import time
import urllib.parse
from collections import OrderedDict, deque

TOOL_NAME = "EAA Audit Kit crawler"
#: Kept in step with the version in pyproject.toml: this lands in the tool
#: metadata of every report, and pip reports the distribution version.
TOOL_VERSION = "0.1.0"
REPORT_SCHEMA_VERSION = "1.0"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_BROWSER = 3

# axe rules whose impact is reported as "minor"/"moderate"/"serious"/"critical".
IMPACTS = ("critical", "serious", "moderate", "minor")
IMPACT_RANK = {"critical": 4, "serious": 3, "moderate": 2, "minor": 1, None: 0}

# Candidate locations for axe-core's browser bundle, tried in order.
AXE_CANDIDATES = (
    "node_modules/axe-core/axe.min.js",
    "../node_modules/axe-core/axe.min.js",
    "/usr/lib/node_modules/axe-core/axe.min.js",
    "/usr/local/lib/node_modules/axe-core/axe.min.js",
    "/root/axecheck/node_modules/axe-core/axe.min.js",
)

SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "data:", "ftp:", "sms:", "callto:")

# Axe tags are of the form wcagXYZ (success criterion, dots removed),
# wcag2aa / wcag21a / wcag22aaa (level+version), EN-9.x.x.x (EN 301 549 clause),
# cat.* (axe category), plus best-practice / ACT / section508 / experimental.
_WCAG_SC_RE = re.compile(r"^wcag(\d)(\d)(\d+)$")
_WCAG_LEVEL_RE = re.compile(r"^wcag(2|21|22)(a|aa|aaa)$")
_EN_CLAUSE_RE = re.compile(r"^EN-(\d+(?:\.\d+)*)$")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_wcag_tag(tag: str):
    """'wcag2411' -> '2.4.11'. Returns None for anything that is not an SC tag.

    This is a pure reformatting of axe's own tag -- WCAG 2.x numbering is
    always <principle>.<guideline>.<criterion> with single-digit principle and
    guideline numbers, so the digits split unambiguously. No mapping is
    invented here; if axe does not emit the tag we do not claim the SC.
    """
    m = _WCAG_SC_RE.match(tag)
    if not m:
        return None
    return "%s.%s.%s" % (m.group(1), m.group(2), m.group(3))


def parse_level_tag(tag: str):
    """'wcag22aa' -> ('2.2', 'AA'). Returns None for non level tags."""
    m = _WCAG_LEVEL_RE.match(tag)
    if not m:
        return None
    version = {"2": "2.0", "21": "2.1", "22": "2.2"}[m.group(1)]
    return version, m.group(2).upper()


def parse_en_clause_tag(tag: str):
    """'EN-9.1.1.1' -> '9.1.1.1'. 'EN-301-549' returns None (that is the
    standard reference itself, not a clause)."""
    m = _EN_CLAUSE_RE.match(tag)
    if not m:
        return None
    clause = m.group(1)
    return clause if clause != "301-549" else None


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0
    return "%d B" % n


def normalize_url(url: str) -> str:
    """Strip the fragment and normalise an absolute URL for de-duplication."""
    parts = urllib.parse.urlsplit(url)
    path = parts.path or "/"
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def canonical_key(url: str) -> str:
    """A de-duplication key: /dir/index.html and /dir/ are the same page.

    Many sites are reachable both ways and auditing the same content twice
    wastes the page budget and inflates the violation counts. A page that
    declares <link rel="canonical"> is keyed on that instead (see audit_page).
    """
    parts = urllib.parse.urlsplit(normalize_url(url))
    path = parts.path or "/"
    lowered = path.lower()
    for suffix in ("/index.html", "/index.htm", "/default.html"):
        if lowered.endswith(suffix):
            path = path[: -len(suffix) + 1]
            break
    if not path.endswith("/") and path == "":
        path = "/"
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def same_origin(a: str, b: str) -> bool:
    pa, pb = urllib.parse.urlsplit(a), urllib.parse.urlsplit(b)
    return (pa.scheme.lower(), pa.netloc.lower()) == (pb.scheme.lower(), pb.netloc.lower())


def url_matches_any(url: str, patterns) -> bool:
    """Match a URL against glob patterns (or 're:' prefixed regexes).

    Patterns are tested against the full URL and against the path, so both
    'https://x.example/private/*' and '*/tag/*' behave the way people expect.
    """
    if not patterns:
        return False
    path = urllib.parse.urlsplit(url).path or "/"
    for pattern in patterns:
        if pattern.startswith("re:"):
            try:
                if re.search(pattern[3:], url) or re.search(pattern[3:], path):
                    return True
            except re.error:
                continue
        else:
            if fnmatch.fnmatch(url, pattern) or fnmatch.fnmatch(path, pattern):
                return True
            # a bare 'tag' should also match '/tag/' and '/tag'
            stripped = pattern.strip("*")
            if stripped and stripped.strip("/") in (path.strip("/"),):
                return True
    return False


def resolve_axe_path(explicit: str = None):
    """Find axe-core's browser bundle. Returns (path, list_of_tried) or (None, tried)."""
    tried = []
    candidates = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("AXE_CORE_PATH")
    if env:
        candidates.append(env)
    here = os.path.dirname(os.path.abspath(__file__))
    for rel in AXE_CANDIDATES:
        candidates.append(os.path.abspath(os.path.join(os.getcwd(), rel)))
        candidates.append(os.path.abspath(os.path.join(here, rel)))
    for cand in candidates:
        tried.append(cand)
        if os.path.isfile(cand):
            return cand, tried
    return None, tried


def axe_missing_message(tried) -> str:
    lines = [
        "axe-core could not be found.",
        "",
        "This kit does not bundle axe-core. Install it once, in the project you",
        "run the audit from:",
        "",
        "    npm i axe-core",
        "",
        "then either leave the default path in place (node_modules/axe-core/axe.min.js),",
        "or pass it explicitly:",
        "",
        "    python3 crawl.py --url https://example.com --axe-path /path/to/axe-core/axe.min.js",
        "",
        "or set the environment variable AXE_CORE_PATH=/path/to/axe.min.js",
        "",
        "axe-core is third-party software: the package is MIT-licensed and the rule",
        "engine is Mozilla Public License 2.0 (https://www.mozilla.org/en-US/MPL/2.0/).",
        "",
        "Paths tried:",
    ]
    lines += ["  - " + p for p in tried]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# axe plumbing
# --------------------------------------------------------------------------- #
AXE_RUN_JS = r"""
async (opts) => {
  const resultTypes = opts.includeIncomplete ? ['violations', 'incomplete'] : ['violations'];
  const axeOptions = {
    resultTypes: resultTypes,
    reporter: 'v2',
    runOnly: { type: 'tag', values: opts.runOnlyTags },
    elementRef: false,
    iframes: true
  };
  const raw = await window.axe.run(document, axeOptions);

  const nodesOf = (items) => (items || []).map((v) => ({
    id: v.id,
    impact: v.impact || null,
    tags: v.tags || [],
    help: v.help || '',
    description: v.description || '',
    helpUrl: v.helpUrl || '',
    nodes: (v.nodes || []).map((n) => ({
      target: n.target || [],
      html: (n.html || '').slice(0, 600),
      impact: n.impact || null,
      failureSummary: n.failureSummary || '',
      messages: [].concat(
        (n.any || []).map((c) => c.message),
        (n.all || []).map((c) => c.message),
        (n.none || []).map((c) => c.message)
      ).filter(Boolean).slice(0, 6)
    }))
  }));

  return {
    axe_version: (window.axe && window.axe.version) || 'unknown',
    url: document.location.href,
    title: document.title || '',
    violations: nodesOf(raw.violations),
    incomplete: nodesOf(raw.incomplete),
    inapplicable_count: (raw.inapplicable || []).length,
    passes_count: (raw.passes || []).length
  };
}
"""


def annotate_violation(v: dict) -> dict:
    """Add the derived, purely mechanical fields the rest of the kit relies on."""
    scs, levels, en_clauses, cats, flags = [], [], [], [], []
    for tag in v.get("tags", []):
        sc = parse_wcag_tag(tag)
        if sc:
            scs.append(sc)
            continue
        lvl = parse_level_tag(tag)
        if lvl:
            levels.append("%s %s" % lvl)
            continue
        clause = parse_en_clause_tag(tag)
        if clause:
            en_clauses.append(clause)
            continue
        if tag.startswith("cat."):
            cats.append(tag[4:])
            continue
        if tag in ("best-practice", "ACT", "experimental", "section508", "EN-301-549"):
            flags.append(tag)
    v["wcag_success_criteria"] = sorted(set(scs), key=lambda s: [int(x) for x in s.split(".")])
    v["wcag_levels"] = sorted(set(levels))
    v["en_301_549_clauses"] = sorted(set(en_clauses))
    v["axe_categories"] = sorted(set(cats))
    v["tag_flags"] = sorted(set(flags))
    v["is_best_practice"] = "best-practice" in v.get("tags", []) and not v["wcag_success_criteria"]
    v["node_count"] = len(v.get("nodes", []))
    return v


# --------------------------------------------------------------------------- #
# crawling
# --------------------------------------------------------------------------- #
class Crawler:
    def __init__(self, args, axe_path: str):
        self.args = args
        self.axe_path = axe_path
        self.start_url = normalize_url(args.url)
        parsed = urllib.parse.urlsplit(self.start_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("--url must be an absolute http(s) URL, got %r" % args.url)
        self.allowed_hosts = {parsed.netloc.lower()}
        for extra in args.allow_host or []:
            self.allowed_hosts.add(extra.lower())
        self.visited = set()
        self.audited_keys = {}
        self.queue = deque()
        self.pages = []
        self.skipped = []
        self.axe_version = "unknown"

    # -- URL filtering ------------------------------------------------------ #
    def in_scope(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme.lower() in ("mailto", "tel", "javascript", "data", "ftp", "sms"):
            return False
        if parts.scheme.lower() not in ("http", "https"):
            return False
        if parts.netloc.lower() not in self.allowed_hosts:
            return False
        if self.args.include and not url_matches_any(url, self.args.include):
            return False
        if self.args.exclude and url_matches_any(url, self.args.exclude):
            return False
        return True

    def skip(self, url: str, reason: str) -> None:
        self.skipped.append({"url": url, "reason": reason})

    # -- main loop ---------------------------------------------------------- #
    def run(self) -> dict:
        from playwright.sync_api import sync_playwright

        width, _, height = self.args.viewport.partition("x")
        started = time.time()

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(
                viewport={"width": int(width), "height": int(height)},
                device_scale_factor=1,
                user_agent=self.args.user_agent,
                ignore_https_errors=True,
            )
            page = context.new_page()
            page.set_default_navigation_timeout(self.args.timeout_ms)
            page.set_default_timeout(self.args.timeout_ms)

            self.queue.append((self.start_url, 0))
            while self.queue and len(self.pages) < self.args.max_pages:
                url, depth = self.queue.popleft()
                if url in self.visited:
                    continue
                self.visited.add(url)
                record, links = self.audit_page(page, url, depth)
                self.pages.append(record)
                for link in links:
                    nxt = normalize_url(link)
                    if nxt in self.visited:
                        continue
                    if any(nxt == q for q, _ in self.queue):
                        continue
                    if not self.in_scope(nxt):
                        self.skip(nxt, "out of scope (host, include or exclude rule)")
                        continue
                    self.queue.append((nxt, depth + 1))
                if self.queue:
                    time.sleep(max(0.0, self.args.politeness_ms / 1000.0))

            context.close()
            browser.close()

        return self.build_report(started, time.time())

    # -- one page ----------------------------------------------------------- #
    def audit_page(self, page, url: str, depth: int):
        """Audit one URL. Never raises: a problem becomes a recorded failure."""
        record = OrderedDict(
            url=url,
            final_url=None,
            depth=depth,
            status="audited",
            http_status=None,
            content_type=None,
            title="",
            load_ms=None,
            redirected=False,
            error=None,
            violation_count=0,
            node_count=0,
            impact_counts={},
            violations=[],
            incomplete=[],
            axe_passes=0,
        )
        t0 = time.time()
        links = []
        try:
            response = page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:  # timeout, DNS, TLS, download, crash ...
            message = str(exc)
            if "download is starting" in message.lower():
                # A link to a PDF/zip/etc. Playwright refuses to render these.
                # Fetch the headers with the same browser context so the report
                # can say what the file actually was.
                record["status"] = "skipped"
                record["error"] = "response is a file download, not an HTML document"
                try:
                    head = page.context.request.head(url, timeout=self.args.timeout_ms)
                    # Only trust the probe if it actually answered: some servers
                    # reject HEAD with a 405/404 even though GET works fine.
                    if head.status < 400:
                        record["http_status"] = head.status
                        record["content_type"] = head.headers.get("content-type", "")
                        if head.headers.get("content-disposition"):
                            record["error"] += " (Content-Disposition: %s)" % head.headers["content-disposition"]
                except Exception:
                    pass
            else:
                record["status"] = "failed"
                record["error"] = "%s: %s" % (type(exc).__name__, _short(exc))
            record["load_ms"] = int((time.time() - t0) * 1000)
            return record, links

        if response is not None:
            record["http_status"] = response.status
            try:
                record["content_type"] = response.headers.get("content-type", "")
            except Exception:
                record["content_type"] = ""
            final = normalize_url(response.url)
            record["final_url"] = final
            record["redirected"] = final != url
        else:
            record["final_url"] = url

        ctype = (record["content_type"] or "").lower()
        if ctype and "html" not in ctype and "xhtml" not in ctype:
            record["status"] = "skipped"
            record["error"] = "not an HTML document (Content-Type: %s)" % (record["content_type"] or "unknown")
            record["load_ms"] = int((time.time() - t0) * 1000)
            return record, links

        if not ctype:
            # No Content-Type at all. Look at the body before giving up.
            try:
                head = (page.content() or "")[:200].lower()
            except Exception:
                head = ""
            if "<html" not in head and "<!doctype" not in head:
                record["status"] = "skipped"
                record["error"] = "not an HTML document (no Content-Type and no html markup)"
                record["load_ms"] = int((time.time() - t0) * 1000)
                return record, links

        if record["http_status"] is not None and record["http_status"] >= 400:
            # Keep going: an error page still renders and still has violations,
            # but say clearly that this is not the page we asked for.
            record["error"] = "HTTP %s returned for this URL" % record["http_status"]

        # JS-rendered content: give the page a chance to settle. networkidle is
        # best-effort -- single-page apps with websockets or polling never reach
        # it, so a timeout there is normal and must not fail the page.
        try:
            page.wait_for_load_state("networkidle", timeout=min(self.args.timeout_ms, 8000))
        except Exception:
            pass
        if self.args.settle_ms > 0:
            time.sleep(self.args.settle_ms / 1000.0)

        try:
            record["title"] = page.title() or ""
        except Exception:
            pass

        # --- de-duplication -------------------------------------------------
        # A redirect target that has already been audited, or a page that is
        # the same content as one already audited (/index.html vs /), must not
        # be counted twice: it inflates the numbers and wastes the page budget.
        declared_canonical = None
        try:
            declared_canonical = page.eval_on_selector("link[rel~='canonical']", "e => e.href")
        except Exception:
            declared_canonical = None

        key_source = declared_canonical or record["final_url"] or url
        key = canonical_key(key_source)
        if self.args.dedupe and key in self.audited_keys and self.audited_keys[key] != url:
            record["status"] = "skipped"
            if declared_canonical:
                record["error"] = ("declares <link rel=canonical> %s, already audited as %s"
                                   % (normalize_url(declared_canonical), self.audited_keys[key]))
            else:
                record["error"] = ("same page as %s (redirect or /index.html alias), already audited"
                                   % self.audited_keys[key])
            record["load_ms"] = int((time.time() - t0) * 1000)
            return record, []

        # Collect in-scope links even if axe fails below.
        if depth < self.args.depth:
            try:
                hrefs = page.eval_on_selector_all(
                    "a[href]",
                    "els => els.map(e => e.href).filter(Boolean)",
                )
                links = [h for h in hrefs if isinstance(h, str) and not h.lower().startswith(SKIP_SCHEMES)]
            except Exception:
                links = []

        try:
            page.add_script_tag(path=self.axe_path)
            result = page.evaluate(
                AXE_RUN_JS,
                {
                    "includeIncomplete": bool(self.args.include_incomplete),
                    "runOnlyTags": list(self.args.run_only_tags),
                },
            )
        except Exception as exc:
            record["status"] = "failed"
            record["error"] = "axe could not run: %s: %s" % (type(exc).__name__, _short(exc))
            record["load_ms"] = int((time.time() - t0) * 1000)
            return record, links

        self.axe_version = result.get("axe_version", "unknown")
        violations = [annotate_violation(v) for v in result.get("violations", [])]
        incomplete = [annotate_violation(v) for v in result.get("incomplete", [])]
        if self.args.max_nodes_per_rule and self.args.max_nodes_per_rule > 0:
            for v in violations + incomplete:
                if len(v.get("nodes", [])) > self.args.max_nodes_per_rule:
                    v["nodes_truncated_from"] = len(v["nodes"])
                    v["nodes"] = v["nodes"][: self.args.max_nodes_per_rule]
        violations.sort(key=lambda v: (-IMPACT_RANK.get(v.get("impact"), 0), v["id"]))
        incomplete.sort(key=lambda v: v["id"])

        counts = {}
        for v in violations:
            counts[v.get("impact") or "unknown"] = counts.get(v.get("impact") or "unknown", 0) + v["node_count"]

        record["violations"] = violations
        record["incomplete"] = incomplete
        record["violation_count"] = len(violations)
        record["node_count"] = sum(v["node_count"] for v in violations)
        record["impact_counts"] = counts
        record["axe_passes"] = result.get("passes_count", 0)
        record["load_ms"] = int((time.time() - t0) * 1000)
        self.audited_keys[key] = url
        return record, links

    # -- aggregation -------------------------------------------------------- #
    def build_report(self, started: float, finished: float) -> dict:
        audited = [p for p in self.pages if p["status"] == "audited"]
        failed = [p for p in self.pages if p["status"] == "failed"]
        skipped = [p for p in self.pages if p["status"] == "skipped"]

        by_rule = {}
        by_wcag = {}
        by_en = {}
        by_impact = {k: {"violations": 0, "nodes": 0, "pages": 0} for k in IMPACTS}
        for p in audited:
            for v in p["violations"]:
                impact = v.get("impact") or "unknown"
                entry = by_rule.setdefault(
                    v["id"],
                    {
                        "rule_id": v["id"],
                        "impact": v.get("impact"),
                        "help": v.get("help", ""),
                        "description": v.get("description", ""),
                        "helpUrl": v.get("helpUrl", ""),
                        "wcag_success_criteria": v.get("wcag_success_criteria", []),
                        "en_301_549_clauses": v.get("en_301_549_clauses", []),
                        "is_best_practice": v.get("is_best_practice", False),
                        "violations": 0,
                        "nodes": 0,
                        "pages": [],
                    },
                )
                entry["violations"] += 1
                entry["nodes"] += v["node_count"]
                if p["url"] not in entry["pages"]:
                    entry["pages"].append(p["url"])
                by_impact.setdefault(impact, {"violations": 0, "nodes": 0, "pages": 0})
                by_impact[impact]["violations"] += 1
                by_impact[impact]["nodes"] += v["node_count"]
                for sc in v.get("wcag_success_criteria", []):
                    e = by_wcag.setdefault(sc, {"criterion": sc, "rules": [], "nodes": 0})
                    if v["id"] not in e["rules"]:
                        e["rules"].append(v["id"])
                    e["nodes"] += v["node_count"]
                for clause in v.get("en_301_549_clauses", []):
                    e = by_en.setdefault(clause, {"clause": clause, "rules": [], "nodes": 0})
                    if v["id"] not in e["rules"]:
                        e["rules"].append(v["id"])
                    e["nodes"] += v["node_count"]

        # distinct pages per impact (computed separately: a page can have several)
        impact_pages = {k: set() for k in by_impact}
        for p in audited:
            for v in p["violations"]:
                impact_pages.setdefault(v.get("impact") or "unknown", set()).add(p["url"])
        for impact, pageset in impact_pages.items():
            by_impact.setdefault(impact, {"violations": 0, "nodes": 0, "pages": 0})
            by_impact[impact]["pages"] = len(pageset)

        total_nodes = sum(p["node_count"] for p in audited)
        total_violations = sum(p["violation_count"] for p in audited)
        incomplete_nodes = sum(v["node_count"] for p in audited for v in p["incomplete"])
        unaudited = failed + skipped

        reasons = []
        if total_violations:
            reasons.append(
                "%d axe rule violation instance(s) across %d node(s) were found."
                % (total_violations, total_nodes)
            )
        if unaudited:
            reasons.append(
                "%d page(s) could not be audited, so their content is unverified." % len(unaudited)
            )
        if incomplete_nodes:
            reasons.append(
                "%d axe result(s) were 'incomplete': axe needs a human to confirm them."
                % incomplete_nodes
            )
        if not audited:
            reasons.append("No page was successfully audited.")

        can_assert = (total_violations == 0 and not unaudited and bool(audited))

        report = OrderedDict(
            report_schema_version=REPORT_SCHEMA_VERSION,
            generated_at=utcnow_iso(),
            tool=OrderedDict(
                name=TOOL_NAME,
                version=TOOL_VERSION,
                axe_core_version=self.axe_version,
                axe_core_path=self.axe_path,
                engine="playwright-chromium",
            ),
            start_url=self.start_url,
            config=OrderedDict(
                max_pages=self.args.max_pages,
                depth=self.args.depth,
                settle_ms=self.args.settle_ms,
                politeness_ms=self.args.politeness_ms,
                timeout_ms=self.args.timeout_ms,
                viewport=self.args.viewport,
                include=list(self.args.include or []),
                exclude=list(self.args.exclude or []),
                run_only_tags=list(self.args.run_only_tags),
                include_incomplete=bool(self.args.include_incomplete),
                dedupe=bool(self.args.dedupe),
                max_nodes_per_rule=self.args.max_nodes_per_rule,
            ),
            duration_seconds=round(finished - started, 2),
            summary=OrderedDict(
                pages_discovered=len(self.visited),
                pages_audited=len(audited),
                pages_failed=len(failed),
                pages_skipped=len(skipped),
                pages_redirected=sum(1 for p in audited if p["redirected"]),
                total_violation_instances=total_violations,
                total_violation_nodes=total_nodes,
                total_rules_triggered=len(by_rule),
                incomplete_nodes=incomplete_nodes,
                by_impact=OrderedDict(
                    (k, by_impact[k]) for k in sorted(by_impact, key=lambda x: -IMPACT_RANK.get(x, 0))
                ),
                worst_impact=_worst(by_impact),
                by_rule=OrderedDict(
                    sorted(by_rule.items(), key=lambda kv: (-IMPACT_RANK.get(kv[1]["impact"], 0), kv[0]))
                ),
                by_wcag=OrderedDict(sorted(by_wcag.items(), key=lambda kv: [int(x) for x in kv[0].split(".")])),
                by_en_301_549=OrderedDict(sorted(by_en.items())),
            ),
            conformance=OrderedDict(
                automated_result="violations_found" if total_violations else (
                    "no_violations_detected" if audited else "audit_incomplete"
                ),
                can_assert_full_conformance=can_assert,
                automated_clean_is_not_conformance=True,
                manual_review_required=True,
                reasons=reasons,
                statement=(
                    "This is an automated audit, not a conformance certificate. "
                    "No automated tool can assert full conformance to WCAG 2.2 AA or EN 301 549: "
                    "it checks the machine-checkable subset of the requirements only. "
                    "See docs/MANUAL-CHECKS.md for what must be verified by a human."
                ),
            ),
            pages=self.pages,
            skipped_urls=self.skipped,
            unaudited_pages=[
                OrderedDict(url=p["url"], status=p["status"], http_status=p["http_status"], reason=p["error"])
                for p in unaudited
            ],
        )
        return report


def _worst(by_impact) -> str:
    for k in IMPACTS:
        if by_impact.get(k, {}).get("violations"):
            return k
    return "none"


def _short(exc) -> str:
    text = str(exc).replace("\n", " ").strip()
    return text[:300]


# --------------------------------------------------------------------------- #
# HTML report
# --------------------------------------------------------------------------- #
HTML_CSS = """
:root { --bg:#ffffff; --fg:#16181d; --muted:#5b6472; --line:#e3e6ea; --crit:#a4161a;
        --ser:#c1440e; --mod:#9a6700; --min:#3d5a80; --ok:#1b6b3a; --soft:#f6f8fa; }
* { box-sizing:border-box; }
body { margin:0; font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
       color:var(--fg); background:var(--bg); }
header { background:#10243b; color:#fff; padding:26px 28px; }
header h1 { margin:0 0 6px; font-size:25px; }
header p { margin:0; color:#c8d6e5; font-size:14px; }
main { max-width:1080px; margin:0 auto; padding:24px 20px 80px; }
h2 { font-size:21px; margin-top:38px; border-bottom:2px solid var(--line); padding-bottom:6px; }
h3 { font-size:17px; margin-top:26px; }
a { color:#0b5394; }
table { border-collapse:collapse; width:100%; margin:14px 0; font-size:14px; }
th,td { border:1px solid var(--line); padding:7px 9px; text-align:left; vertical-align:top; }
th { background:var(--soft); font-weight:600; }
code, pre { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:13px; }
pre { background:var(--soft); border:1px solid var(--line); border-radius:5px; padding:10px; overflow-x:auto; white-space:pre-wrap; word-break:break-word; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin:18px 0; }
.card { border:1px solid var(--line); border-radius:6px; padding:12px 14px; background:#fff; }
.card .n { font-size:26px; font-weight:700; display:block; line-height:1.15; }
.card .l { font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }
.verdict { border-left:5px solid var(--ok); background:#f2fbf5; padding:14px 16px; border-radius:5px; margin:18px 0; }
.verdict.bad { border-left-color:var(--crit); background:#fdf3f3; }
.verdict h3 { margin:0 0 6px; }
.notice { border-left:5px solid var(--mod); background:#fffaf0; padding:12px 16px; border-radius:5px; margin:16px 0; font-size:14px; }
.impact { display:inline-block; padding:2px 8px; border-radius:999px; color:#fff; font-size:11.5px;
          text-transform:uppercase; letter-spacing:.04em; font-weight:600; white-space:nowrap; }
.i-critical { background:var(--crit); } .i-serious { background:var(--ser); }
.i-moderate { background:var(--mod); } .i-minor { background:var(--min); } .i-unknown { background:#6b7280; }
.rule { border:1px solid var(--line); border-radius:6px; margin:14px 0; overflow:hidden; }
.rule > summary { padding:11px 14px; cursor:pointer; background:var(--soft); font-weight:600; }
.rule[open] > summary { border-bottom:1px solid var(--line); }
.rule .body { padding:12px 14px; }
.node { border-top:1px dashed var(--line); padding:10px 0; }
.node:first-child { border-top:0; }
.sel { font-size:12.5px; color:var(--muted); word-break:break-all; }
.tag { display:inline-block; background:#eef2f7; border:1px solid var(--line); border-radius:4px;
       padding:1px 6px; margin:2px 3px 2px 0; font-size:11.5px; }
footer { border-top:1px solid var(--line); margin-top:40px; padding-top:14px; color:var(--muted); font-size:13px; }
.small { font-size:13px; color:var(--muted); }
"""


def _e(text) -> str:
    return html.escape("" if text is None else str(text), quote=True)


def render_html(report: dict) -> str:
    s = report["summary"]
    conf = report["conformance"]
    cfg = report["config"]
    out = []
    add = out.append

    add("<!DOCTYPE html>")
    add('<html lang="en"><head><meta charset="utf-8">')
    add('<meta name="viewport" content="width=device-width, initial-scale=1">')
    add("<title>Accessibility audit report - %s</title>" % _e(report["start_url"]))
    add("<style>%s</style></head><body>" % HTML_CSS)

    add("<header><h1>Accessibility audit report</h1>")
    add("<p>%s &middot; %s &middot; axe-core %s &middot; generated %s</p></header>"
        % (_e(report["start_url"]), _e(report["tool"]["name"]), _e(report["tool"]["axe_core_version"]),
           _e(report["generated_at"])))
    add("<main>")

    # verdict ------------------------------------------------------------- #
    if conf["can_assert_full_conformance"]:
        add('<div class="verdict"><h3>Automated audit found no violations</h3>')
        add("<p>axe-core reported zero violations on every page that could be audited, and no page failed "
            "to load. That is good news and it is <strong>not</strong> a conformance certificate: automated "
            "checks cover the machine-checkable subset of WCAG 2.2 AA. The manual checks in "
            "<code>docs/MANUAL-CHECKS.md</code> still have to be done by a person before anyone signs a "
            "statement claiming full conformance.</p></div>")
    else:
        add('<div class="verdict bad"><h3>Violations found &mdash; full conformance cannot be claimed</h3>')
        add("<ul>")
        for reason in conf["reasons"]:
            add("<li>%s</li>" % _e(reason))
        add("</ul>")
        add("<p>%s</p></div>" % _e(conf["statement"]))

    add('<div class="notice"><strong>Scope of this report.</strong> Automated testing finds a subset of '
        'accessibility problems and cannot certify conformance to WCAG 2.2 AA or EN 301 549. The impact '
        'labels above are axe-core\'s own severity scale, not a legal category. See '
        '<code>docs/MANUAL-CHECKS.md</code> for what only a human can check.</div>')

    # summary cards ------------------------------------------------------- #
    add('<div class="cards">')
    for label, value in (
        ("Pages audited", s["pages_audited"]),
        ("Pages not audited", s["pages_failed"] + s["pages_skipped"]),
        ("Rule violations", s["total_violation_instances"]),
        ("Affected elements", s["total_violation_nodes"]),
        ("Distinct rules", s["total_rules_triggered"]),
        ("Worst impact", s["worst_impact"]),
    ):
        add('<div class="card"><span class="n">%s</span><span class="l">%s</span></div>' % (_e(value), _e(label)))
    add("</div>")

    # by impact ----------------------------------------------------------- #
    add("<h2>Findings by impact</h2>")
    add("<table><tr><th>Impact</th><th>Rules triggered</th><th>Affected elements</th><th>Pages</th></tr>")
    for impact, data in s["by_impact"].items():
        if not data.get("violations"):
            continue
        add('<tr><td><span class="impact i-%s">%s</span></td><td>%d</td><td>%d</td><td>%d</td></tr>'
            % (_e(impact), _e(impact), data["violations"], data["nodes"], data.get("pages", 0)))
    add("</table>")

    # by rule ------------------------------------------------------------- #
    add("<h2>Findings by rule</h2>")
    if not s["by_rule"]:
        add("<p>No violations were reported.</p>")
    else:
        add("<table><tr><th>Rule</th><th>Impact</th><th>WCAG SC</th><th>EN 301 549</th>"
            "<th>Elements</th><th>Pages</th><th>Guidance</th></tr>")
        for rid, r in s["by_rule"].items():
            add("<tr><td><code>%s</code><br><span class='small'>%s</span></td>"
                '<td><span class="impact i-%s">%s</span></td><td>%s</td><td>%s</td><td>%d</td><td>%d</td>'
                '<td><a href="%s" rel="noopener">axe rule page</a></td></tr>'
                % (_e(rid), _e(r["help"]), _e(r["impact"] or "unknown"), _e(r["impact"] or "unknown"),
                   _e(", ".join(r["wcag_success_criteria"]) or "&mdash;"),
                   _e(", ".join(r["en_301_549_clauses"]) or "&mdash;"),
                   r["nodes"], len(r["pages"]), _e(r["helpUrl"])))
        add("</table>")
        add('<p class="small">WCAG success criteria and EN 301 549 clauses are read from the tags axe-core '
            "puts on each rule. Where a cell is empty, axe-core publishes no mapping for that rule &mdash; "
            "nothing has been guessed.</p>")

    # by criterion -------------------------------------------------------- #
    if s["by_wcag"]:
        add("<h2>Findings by WCAG success criterion</h2>")
        add("<table><tr><th>Success criterion</th><th>Rules</th><th>Affected elements</th></tr>")
        for sc, data in s["by_wcag"].items():
            add("<tr><td>%s</td><td>%s</td><td>%d</td></tr>"
                % (_e(sc), _e(", ".join("<code>%s</code>" % r for r in data["rules"])), data["nodes"]))
        add("</table>")

    if s["by_en_301_549"]:
        add("<h2>Findings by EN 301 549 clause</h2>")
        add("<table><tr><th>Clause</th><th>Rules</th><th>Affected elements</th></tr>")
        for clause, data in s["by_en_301_549"].items():
            add("<tr><td>%s</td><td>%s</td><td>%d</td></tr>"
                % (_e(clause), _e(", ".join("<code>%s</code>" % r for r in data["rules"])), data["nodes"]))
        add("</table>")
        add('<p class="small">Clause numbers come from axe-core\'s own EN 301 549 tags. Which clauses apply '
            "to your organisation depends on the version of EN 301 549 your national law references &mdash; "
            "check that before quoting these numbers.</p>")

    # per page ------------------------------------------------------------ #
    add("<h2>Pages</h2>")
    add("<table><tr><th>#</th><th>URL</th><th>Status</th><th>HTTP</th><th>Violations</th>"
        "<th>Elements</th><th>Load (ms)</th></tr>")
    for i, p in enumerate(report["pages"], 1):
        status = p["status"]
        add("<tr><td>%d</td><td>%s%s</td><td>%s</td><td>%s</td><td>%d</td><td>%d</td><td>%s</td></tr>"
            % (i, _e(p["url"]),
               "<br><span class='small'>redirected to %s</span>" % _e(p["final_url"]) if p["redirected"] else "",
               _e(status), _e(p["http_status"] if p["http_status"] is not None else "&mdash;"),
               p["violation_count"], p["node_count"],
               _e(p["load_ms"] if p["load_ms"] is not None else "&mdash;")))
    add("</table>")

    unaudited = report["unaudited_pages"]
    if unaudited:
        add("<h2>Pages that were not audited</h2>")
        add('<p class="small">These URLs were discovered but not audited. Content here is unverified: do '
            "not assume it is fine, and do not let it into a conformance claim. Duplicate aliases and "
            "non-HTML files are listed here too, with the reason.</p>")
        add("<table><tr><th>URL</th><th>Status</th><th>HTTP</th><th>Reason</th></tr>")
        for p in unaudited:
            add("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                % (_e(p["url"]), _e(p["status"]),
                   _e(p["http_status"] if p["http_status"] is not None else "&mdash;"), _e(p["reason"])))
        add("</table>")

    # detail -------------------------------------------------------------- #
    add("<h2>Violation detail</h2>")
    for p in report["pages"]:
        if not p["violations"]:
            continue
        add("<h3>%s</h3>" % _e(p["url"]))
        add('<p class="small">%s &middot; %d violation(s) on %d element(s)%s</p>'
            % (_e(p["title"] or "(no title)"), p["violation_count"], p["node_count"],
               " &middot; also " + str(len(p["incomplete"])) + " incomplete result(s) needing human review"
               if p["incomplete"] else ""))
        for v in p["violations"]:
            add('<details class="rule"><summary><span class="impact i-%s">%s</span> &nbsp;<code>%s</code>'
                " &nbsp;%s &nbsp;<span class='small'>(%d element%s)</span></summary>"
                % (_e(v.get("impact") or "unknown"), _e(v.get("impact") or "unknown"), _e(v["id"]),
                   _e(v.get("help", "")), v["node_count"], "" if v["node_count"] == 1 else "s"))
            add('<div class="body">')
            if v.get("description"):
                add("<p>%s</p>" % _e(v["description"]))
            if v.get("helpUrl"):
                add('<p class="small">Guidance: <a href="%s" rel="noopener">%s</a></p>'
                    % (_e(v["helpUrl"]), _e(v["helpUrl"])))
            tags = v.get("wcag_success_criteria", [])
            if tags:
                add("<p>WCAG success criteria: %s</p>"
                    % " ".join('<span class="tag">SC %s</span>' % _e(t) for t in tags))
            if v.get("en_301_549_clauses"):
                add("<p>EN 301 549 clauses: %s</p>"
                    % " ".join('<span class="tag">%s</span>' % _e(t) for t in v["en_301_549_clauses"]))
            if v.get("is_best_practice"):
                add("<p><strong>axe best-practice rule.</strong> This is not a WCAG success criterion, "
                    "but it is still a real usability problem for assistive-technology users.</p>")
            add("<p class='small'>Impact set by axe-core: <code>%s</code>%s</p>"
                % (_e(v.get("impact") or "unknown"),
                   " &middot; <span class='small'>axe categories: %s</span>"
                   % _e(", ".join(v.get("axe_categories", []))) if v.get("axe_categories") else ""))
            for n in v.get("nodes", []):
                add('<div class="node"><p class="sel">Selector: <code>%s</code></p>'
                    % _e(" ".join(n.get("target", [])) or "(no selector reported)"))
                if n.get("html"):
                    add("<pre>%s</pre>" % _e(n["html"]))
                if n.get("failureSummary"):
                    add("<p>%s</p>" % _e(n["failureSummary"]))
                for msg in n.get("messages", []):
                    add('<p class="small">&bull; %s</p>' % _e(msg))
                add("</div>")
            if v.get("nodes_truncated_from"):
                add('<p class="small">List truncated: showing %d of %d affected elements.</p>'
                    % (len(v["nodes"]), v["nodes_truncated_from"]))
            add("</div></details>")

    # incomplete ---------------------------------------------------------- #
    if cfg["include_incomplete"] and any(p["incomplete"] for p in report["pages"]):
        add("<h2>Incomplete results (need a human)</h2>")
        add('<p class="small">axe could not decide these automatically. They are not violations, but they '
            "are not passes either.</p>")
        for p in report["pages"]:
            if not p["incomplete"]:
                continue
            add("<h3>%s</h3><ul>" % _e(p["url"]))
            for v in p["incomplete"]:
                add("<li><code>%s</code> &mdash; %s (%d element(s))</li>"
                    % (_e(v["id"]), _e(v.get("help", "")), v["node_count"]))
            add("</ul>")

    # config -------------------------------------------------------------- #
    add("<h2>How this run was configured</h2>")
    add("<table>")
    for key in ("max_pages", "depth", "settle_ms", "politeness_ms", "timeout_ms", "viewport",
                "run_only_tags", "include_incomplete", "dedupe", "max_nodes_per_rule"):
        add("<tr><th>%s</th><td>%s</td></tr>" % (_e(key), _e(cfg[key])))
    add("<tr><th>include</th><td>%s</td></tr>" % _e(cfg["include"] or "(none)"))
    add("<tr><th>exclude</th><td>%s</td></tr>" % _e(cfg["exclude"] or "(none)"))
    add("<tr><th>axe-core file</th><td><code>%s</code></td></tr>" % _e(report["tool"]["axe_core_path"]))
    add("<tr><th>duration</th><td>%s s</td></tr>" % _e(report["duration_seconds"]))
    add("</table>")

    add('<footer><p>Generated by %s %s on %s with axe-core %s. '
        "Automated results only; no tool can certify accessibility conformance.</p></footer>"
        % (_e(report["tool"]["name"]), _e(report["tool"]["version"]), _e(report["generated_at"]),
           _e(report["tool"]["axe_core_version"])))
    add("</main></body></html>")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wcag-axe-crawler",
        description="Crawl a site with Playwright + axe-core and write a WCAG/EN 301 549 audit report.",
        epilog="Exit codes: 0 success, 2 usage/config error, 3 browser could not start.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--url", required=True, help="start URL (must be http or https)")
    p.add_argument("--max-pages", type=int, default=25, help="maximum pages to audit (default: 25)")
    p.add_argument("--depth", type=int, default=1,
                   help="how many link hops from the start URL to follow (0 = only the start URL, default: 1)")
    p.add_argument("--out", default="audit-report",
                   help="output prefix; writes <out>.json and <out>.html (default: audit-report)")
    p.add_argument("--json-only", action="store_true", help="write only the JSON report")
    p.add_argument("--include", action="append", default=[], metavar="GLOB",
                   help="only crawl URLs matching this glob (repeatable). 're:...' uses a regex.")
    p.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                   help="never crawl URLs matching this glob (repeatable). 're:...' uses a regex.")
    p.add_argument("--settle-ms", type=int, default=1200,
                   help="extra wait after load for JS-rendered content, milliseconds (default: 1200)")
    p.add_argument("--politeness-ms", type=int, default=500,
                   help="delay between page requests, milliseconds (default: 500)")
    p.add_argument("--timeout-ms", type=int, default=30000,
                   help="navigation and action timeout, milliseconds (default: 30000)")
    p.add_argument("--viewport", default="1280x800", help="viewport WxH (default: 1280x800)")
    p.add_argument("--user-agent", default=None, help="override the browser user agent")
    p.add_argument("--allow-host", action="append", default=[],
                   help="additional host to treat as in-scope (repeatable)")
    p.add_argument("--dedupe", dest="dedupe", action="store_true", default=True,
                   help="audit a page once even if it is reachable via a redirect or an /index.html "
                        "alias, or declares a canonical URL already audited (default: on)")
    p.add_argument("--no-dedupe", dest="dedupe", action="store_false",
                   help="audit every distinct URL, including aliases and redirect targets")
    p.add_argument("--axe-path", default=None,
                   help="path to axe-core's axe.min.js (default: $AXE_CORE_PATH, then ./node_modules/...)")
    p.add_argument("--run-only-tags", action="append", default=["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22a", "wcag22aa", "best-practice"],
                   help="axe tag filter (repeatable). Default: WCAG 2.0/2.1/2.2 A+AA plus best-practice.")
    p.add_argument("--include-incomplete", action="store_true",
                   help="also record axe 'incomplete' results (things needing a human)")
    p.add_argument("--max-nodes-per-rule", type=int, default=0,
                   help="cap stored elements per rule to keep the JSON readable (0 = no cap)")
    p.add_argument("--verbose", action="store_true", help="log progress to stderr")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    # An explicitly named axe-core that is not there used to fall through to the
    # candidate list and silently audit with a DIFFERENT axe-core build. The rule
    # set a report contains is the rule set of the build that ran, so that is a
    # correctness problem, not a convenience: refuse instead.
    if args.axe_path and not os.path.isfile(args.axe_path):
        print("error: --axe-path does not exist: %s" % args.axe_path, file=sys.stderr)
        print("       Refusing to fall back to another axe-core build: an audit is only\n"
              "       meaningful against the rule set of the build it actually ran.\n"
              "       Install it (npm i axe-core) or point --axe-path at a real file.",
              file=sys.stderr)
        return EXIT_USAGE

    axe_path, tried = resolve_axe_path(args.axe_path)
    if not axe_path:
        print(axe_missing_message(tried), file=sys.stderr)
        return EXIT_USAGE

    def log(msg):
        if args.verbose:
            sys.stderr.write("[crawl] %s\n" % msg)
            sys.stderr.flush()

    try:
        crawler = Crawler(args, axe_path)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return EXIT_USAGE

    log("axe-core: %s" % axe_path)
    log("start: %s (max %d pages, depth %d)" % (crawler.start_url, args.max_pages, args.depth))

    try:
        report = crawler.run()
    except ImportError as exc:
        print(
            "error: Playwright is not installed for this interpreter (%s).\n"
            "       Install it with:\n"
            "           pip install playwright\n"
            "           playwright install chromium\n"
            "       In this kit's reference environment the venv interpreter is used:\n"
            "           /root/browser-tool/venv/bin/python crawl.py --url ...\n" % exc,
            file=sys.stderr,
        )
        return EXIT_BROWSER
    except Exception as exc:  # browser launch failure, missing chromium, ...
        print("error: the browser could not be started: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        print("       Try: playwright install chromium", file=sys.stderr)
        return EXIT_BROWSER

    out_prefix = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_prefix) or ".", exist_ok=True)
    json_path = out_prefix + ".json"
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    log("wrote %s (%s)" % (json_path, human_bytes(os.path.getsize(json_path))))

    html_path = None
    if not args.json_only:
        html_path = out_prefix + ".html"
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(render_html(report))
        log("wrote %s (%s)" % (html_path, human_bytes(os.path.getsize(html_path))))

    s = report["summary"]
    print("Audited %d page(s); %d page(s) not audited." % (s["pages_audited"], s["pages_failed"] + s["pages_skipped"]))
    print("Violations: %d rule instance(s) across %d element(s); worst impact: %s"
          % (s["total_violation_instances"], s["total_violation_nodes"], s["worst_impact"]))
    print("Full conformance can be asserted from this audit: %s"
          % ("yes (automated scope only -- manual checks still required)" if report["conformance"]["can_assert_full_conformance"] else "no"))
    print("JSON: %s" % json_path)
    if html_path:
        print("HTML: %s" % html_path)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

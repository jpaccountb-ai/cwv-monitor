#!/usr/bin/env python3
"""
cwv_monitor.py
------------------------------------------------------------------------------
Monitor Core Web Vitals for every page of a website using Google's
PageSpeed Insights API. Runs each URL on both mobile and desktop, records
field (real-user / CrUX) and lab (Lighthouse) metrics, appends everything to a
historical CSV, and generates a color-coded HTML report with month-over-month
trends.

Designed to be run monthly (via cron on Mac/Linux or Task Scheduler on Windows).

USAGE
  1. Get a free PageSpeed Insights API key (see README).
  2. Set it as an environment variable:
        Windows (PowerShell):  $env:CWV_API_KEY="your_key_here"
        Mac/Linux:             export CWV_API_KEY="your_key_here"
  3. Run:
        python cwv_monitor.py --sitemap https://jawsurgerynj.com/sitemap.xml --site "Jaw Surgery NJ"
     or point at a plain text file of URLs (one per line):
        python cwv_monitor.py --urls-file urls.txt --site "Jaw Surgery NJ"

OUTPUT (in ./output by default)
  - cwv_history.csv        one row per URL/strategy/run, appended every month
  - report_YYYY-MM-DD.html a dashboard for this run, with trends from history
------------------------------------------------------------------------------
"""

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

# Core Web Vitals thresholds (Google's official good / needs-improvement / poor)
THRESHOLDS = {
    "LCP_ms":   (2500, 4000),   # good <= 2500, poor > 4000
    "INP_ms":   (200, 500),
    "CLS":      (0.10, 0.25),
    "FCP_ms":   (1800, 3000),
    "TTFB_ms":  (800, 1800),
    "PERF":     (90, 50),       # good >= 90, poor < 50  (note: reversed direction)
}


# --------------------------------------------------------------------------- #
# URL collection
# --------------------------------------------------------------------------- #
def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "CWV-Monitor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def urls_from_sitemap(sitemap_url, _seen=None):
    """Recursively collect page URLs from a sitemap or sitemap index."""
    if _seen is None:
        _seen = set()
    if sitemap_url in _seen:
        return []
    _seen.add(sitemap_url)

    try:
        raw = fetch(sitemap_url)
    except Exception as e:
        print(f"  ! could not fetch sitemap {sitemap_url}: {e}")
        return []

    # decode and clean up common issues that break strict XML parsing:
    #  - UTF-8 byte-order mark (BOM)
    #  - Yoast/other XSL stylesheet processing instructions before the root
    #  - any stray content before the XML/root element
    text = raw.decode("utf-8-sig", errors="ignore")  # utf-8-sig strips a BOM
    # remove <?xml-stylesheet ...?> processing instructions (Yoast adds these)
    text = re.sub(r"<\?xml-stylesheet[^>]*\?>", "", text)
    # trim anything before the first real tag (declaration or root element)
    lt = text.find("<")
    if lt > 0:
        text = text[lt:]
    text = text.strip()

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        # last resort: try again from the first root-ish tag
        m2 = re.search(r"<(?:\w+:)?(?:sitemapindex|urlset)\b", text)
        if m2:
            try:
                root = ET.fromstring(text[m2.start():])
            except ET.ParseError as e:
                print(f"  ! could not parse sitemap {sitemap_url}: {e}")
                return []
        else:
            print(f"  ! could not parse sitemap {sitemap_url}")
            return []

    def localname(tag):
        return tag.split("}")[-1].lower()

    urls = []
    # sitemap index -> follow children
    if localname(root.tag) == "sitemapindex":
        for sm in root.iter():
            if localname(sm.tag) == "loc" and sm.text:
                urls.extend(urls_from_sitemap(sm.text.strip(), _seen))
    else:
        for loc in root.iter():
            if localname(loc.tag) == "loc" and loc.text:
                urls.append(loc.text.strip())
    return urls


def load_urls(args):
    if args.urls_file:
        with open(args.urls_file, "r", encoding="utf-8") as f:
            urls = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    elif args.sitemap:
        print(f"Reading sitemap: {args.sitemap}")
        urls = urls_from_sitemap(args.sitemap)
    else:
        sys.exit("ERROR: provide either --sitemap or --urls-file")

    # de-duplicate, keep order, drop obvious non-page assets
    seen, clean = set(), []
    for u in urls:
        if u in seen:
            continue
        if any(u.lower().endswith(ext) for ext in
               (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".pdf", ".xml")):
            continue
        seen.add(u)
        clean.append(u)
    return clean


# --------------------------------------------------------------------------- #
# PageSpeed Insights call + parse
# --------------------------------------------------------------------------- #
def run_psi(url, strategy, api_key, retries=3):
    params = {
        "url": url,
        "strategy": strategy,
        "category": "performance",
        "key": api_key,
    }
    full = PSI_ENDPOINT + "?" + urllib.parse.urlencode(params)
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            raw = fetch(full, timeout=90)
            return json.loads(raw)
        except Exception as e:
            last_err = e
            wait = attempt * 5
            print(f"    retry {attempt}/{retries} for {strategy} ({e}); waiting {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"PSI failed after {retries} tries: {last_err}")


def _field_metric(le, key, divide=1.0):
    """Extract a CrUX field metric (percentile + category) if present."""
    metrics = (le or {}).get("metrics", {})
    m = metrics.get(key)
    if not m:
        return (None, None)
    pct = m.get("percentile")
    if pct is not None and divide != 1.0:
        pct = pct / divide
    return (pct, m.get("category"))


def _lab_ms(audits, key):
    a = audits.get(key, {})
    v = a.get("numericValue")
    return round(v) if v is not None else None


def parse_result(data, url, strategy):
    row = {
        "url": url,
        "strategy": strategy,
        "error": "",
    }

    lh = data.get("lighthouseResult", {})
    audits = lh.get("audits", {})
    cats = lh.get("categories", {})

    # Lab (Lighthouse) ------------------------------------------------------
    perf = cats.get("performance", {}).get("score")
    row["perf_score"] = round(perf * 100) if perf is not None else None
    row["lab_LCP_ms"] = _lab_ms(audits, "largest-contentful-paint")
    row["lab_CLS"]    = (round(audits.get("cumulative-layout-shift", {})
                               .get("numericValue", 0), 3)
                         if "cumulative-layout-shift" in audits else None)
    row["lab_TBT_ms"] = _lab_ms(audits, "total-blocking-time")
    row["lab_FCP_ms"] = _lab_ms(audits, "first-contentful-paint")
    row["lab_SI_ms"]  = _lab_ms(audits, "speed-index")

    # Field (CrUX real-user) ------------------------------------------------
    le = data.get("loadingExperience", {})
    row["field_source"] = ("url" if le.get("origin_fallback") is not True
                           and le.get("metrics") else
                           ("origin" if le.get("metrics") else "none"))
    row["field_overall"] = le.get("overall_category", "")

    lcp_p, lcp_c = _field_metric(le, "LARGEST_CONTENTFUL_PAINT_MS")
    inp_p, inp_c = _field_metric(le, "INTERACTION_TO_NEXT_PAINT")
    cls_p, cls_c = _field_metric(le, "CUMULATIVE_LAYOUT_SHIFT_SCORE", divide=100.0)
    fcp_p, fcp_c = _field_metric(le, "FIRST_CONTENTFUL_PAINT_MS")
    ttfb_p, ttfb_c = _field_metric(le, "EXPERIENCE_TIME_TO_FIRST_BYTE")

    row["field_LCP_ms"], row["field_LCP_cat"] = lcp_p, lcp_c
    row["field_INP_ms"], row["field_INP_cat"] = inp_p, inp_c
    row["field_CLS"],    row["field_CLS_cat"] = (round(cls_p, 3) if cls_p is not None else None), cls_c
    row["field_FCP_ms"], row["field_FCP_cat"] = fcp_p, fcp_c
    row["field_TTFB_ms"], row["field_TTFB_cat"] = ttfb_p, ttfb_c
    return row


# --------------------------------------------------------------------------- #
# CSV history
# --------------------------------------------------------------------------- #
CSV_FIELDS = [
    "run_date", "site", "url", "strategy", "perf_score",
    "field_source", "field_overall",
    "field_LCP_ms", "field_LCP_cat", "field_INP_ms", "field_INP_cat",
    "field_CLS", "field_CLS_cat", "field_FCP_ms", "field_FCP_cat",
    "field_TTFB_ms", "field_TTFB_cat",
    "lab_LCP_ms", "lab_CLS", "lab_TBT_ms", "lab_FCP_ms", "lab_SI_ms",
    "error",
]


def append_history(csv_path, rows):
    exists = os.path.isfile(csv_path)
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if not exists:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def read_history(csv_path):
    if not os.path.isfile(csv_path):
        return []
    with open(csv_path, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# --------------------------------------------------------------------------- #
# HTML report
# --------------------------------------------------------------------------- #
def rating(metric, value):
    """Return 'good' | 'ni' | 'poor' | '' for CSS coloring."""
    if value in (None, "", "None"):
        return ""
    try:
        v = float(value)
    except ValueError:
        # textual CrUX category
        s = str(value).upper()
        return {"FAST": "good", "AVERAGE": "ni", "SLOW": "poor",
                "GOOD": "good", "NEEDS_IMPROVEMENT": "ni", "POOR": "poor"}.get(s, "")
    if metric == "PERF":
        good, poor = THRESHOLDS["PERF"]
        return "good" if v >= good else ("poor" if v < poor else "ni")
    if metric not in THRESHOLDS:
        return ""
    good, poor = THRESHOLDS[metric]
    return "good" if v <= good else ("poor" if v > poor else "ni")


def cell(metric, value, suffix=""):
    cls = rating(metric, value)
    disp = "—" if value in (None, "", "None") else f"{value}{suffix}"
    return f'<td class="{cls}">{disp}</td>'


def build_report(html_path, site, run_date, rows, history):
    # summary counts (mobile field CWV pass/fail based on overall)
    mob = [r for r in rows if r["strategy"] == "mobile"]
    def count(cat):
        return sum(1 for r in mob if str(r.get("field_overall", "")).upper() ==
                   {"good": "FAST", "ni": "AVERAGE", "poor": "SLOW"}[cat])
    n_good, n_ni, n_poor = count("good"), count("ni"), count("poor")
    n_nodata = sum(1 for r in mob if not r.get("field_overall"))
    avg_perf = [int(r["perf_score"]) for r in mob if r.get("perf_score") not in (None, "")]
    avg_perf = round(sum(avg_perf) / len(avg_perf)) if avg_perf else "—"

    # trend: average mobile perf score per run_date
    trend = {}
    for h in history:
        if h.get("strategy") != "mobile":
            continue
        try:
            trend.setdefault(h["run_date"], []).append(int(h["perf_score"]))
        except (ValueError, KeyError):
            pass
    trend_points = sorted((d, round(sum(v) / len(v))) for d, v in trend.items() if v)

    def table_rows(strategy):
        out = []
        for r in sorted([x for x in rows if x["strategy"] == strategy],
                        key=lambda x: (x.get("perf_score") or 999)):
            short = r["url"].replace("https://", "").replace("http://", "")
            out.append(
                "<tr>"
                f'<td class="url" title="{r["url"]}">{short}</td>'
                + cell("PERF", r.get("perf_score"))
                + cell("LCP_ms", r.get("field_LCP_ms"), " ms")
                + cell("INP_ms", r.get("field_INP_ms"), " ms")
                + cell("CLS", r.get("field_CLS"))
                + cell("LCP_ms", r.get("lab_LCP_ms"), " ms")
                + cell("CLS", r.get("lab_CLS"))
                + cell("INP_ms", r.get("lab_TBT_ms"), " ms")
                + f'<td>{r.get("field_source","") or "—"}</td>'
                + "</tr>"
            )
        return "\n".join(out)

    trend_js = json.dumps(trend_points)

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Core Web Vitals — {site} — {run_date}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  :root{{--good:#0a8f3c;--ni:#b8860b;--poor:#c62828;}}
  body{{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;background:#f5f6f8;color:#1a1a1a;}}
  header{{background:#1f2d3d;color:#fff;padding:22px 28px;}}
  header h1{{margin:0 0 4px;font-size:20px;}} header p{{margin:0;opacity:.8;font-size:13px;}}
  .wrap{{max-width:1200px;margin:0 auto;padding:24px 20px 60px;}}
  .cards{{display:flex;gap:14px;flex-wrap:wrap;margin:20px 0;}}
  .card{{background:#fff;border-radius:10px;padding:16px 20px;box-shadow:0 1px 3px rgba(0,0,0,.08);min-width:130px;}}
  .card .n{{font-size:26px;font-weight:700;}} .card .l{{font-size:12px;color:#666;margin-top:2px;}}
  .card.good .n{{color:var(--good);}} .card.ni .n{{color:var(--ni);}} .card.poor .n{{color:var(--poor);}}
  h2{{font-size:15px;margin:28px 0 10px;color:#1f2d3d;}}
  table{{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.08);font-size:12.5px;}}
  th,td{{padding:8px 10px;text-align:center;border-bottom:1px solid #eee;}}
  th{{background:#eef1f5;font-weight:600;color:#333;position:sticky;top:0;}}
  td.url{{text-align:left;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#1155cc;}}
  td.good{{background:#e6f4ea;color:var(--good);font-weight:600;}}
  td.ni{{background:#fff6e0;color:var(--ni);font-weight:600;}}
  td.poor{{background:#fdeaea;color:var(--poor);font-weight:600;}}
  .grp{{background:#dde3ea !important;font-size:11px;letter-spacing:.03em;}}
  .chartbox{{background:#fff;border-radius:10px;padding:18px;box-shadow:0 1px 3px rgba(0,0,0,.08);margin:12px 0;}}
  .legend{{font-size:11px;color:#666;margin-top:6px;}}
</style></head><body>
<header>
  <h1>Core Web Vitals Report — {site}</h1>
  <p>Run date: {run_date} &nbsp;·&nbsp; {len(mob)} pages &nbsp;·&nbsp; source: PageSpeed Insights API</p>
</header>
<div class="wrap">

  <div class="cards">
    <div class="card"><div class="n">{avg_perf}</div><div class="l">Avg mobile perf score</div></div>
    <div class="card good"><div class="n">{n_good}</div><div class="l">Pages passing CWV (field)</div></div>
    <div class="card ni"><div class="n">{n_ni}</div><div class="l">Needs improvement</div></div>
    <div class="card poor"><div class="n">{n_poor}</div><div class="l">Failing CWV</div></div>
    <div class="card"><div class="n">{n_nodata}</div><div class="l">No field data yet</div></div>
  </div>

  <div class="chartbox">
    <h2 style="margin-top:0">Average mobile performance score over time</h2>
    <canvas id="trend" height="90"></canvas>
    <div class="legend">Each point is the average Lighthouse performance score across all pages for that monthly run.</div>
  </div>

  <h2>Mobile — per page</h2>
  <table>
    <tr>
      <th class="url" style="text-align:left">Page</th>
      <th>Perf</th>
      <th colspan="3" class="grp">FIELD (real users)</th>
      <th colspan="3" class="grp">LAB (Lighthouse)</th>
      <th>Data</th>
    </tr>
    <tr>
      <th></th><th></th>
      <th>LCP</th><th>INP</th><th>CLS</th>
      <th>LCP</th><th>CLS</th><th>TBT</th>
      <th></th>
    </tr>
    {table_rows("mobile")}
  </table>

  <h2>Desktop — per page</h2>
  <table>
    <tr>
      <th class="url" style="text-align:left">Page</th>
      <th>Perf</th>
      <th colspan="3" class="grp">FIELD (real users)</th>
      <th colspan="3" class="grp">LAB (Lighthouse)</th>
      <th>Data</th>
    </tr>
    <tr>
      <th></th><th></th>
      <th>LCP</th><th>INP</th><th>CLS</th>
      <th>LCP</th><th>CLS</th><th>TBT</th>
      <th></th>
    </tr>
    {table_rows("desktop")}
  </table>

  <p style="font-size:11px;color:#888;margin-top:24px">
    Green = good, amber = needs improvement, red = poor (Google's official CWV thresholds).
    "No field data" means the page lacks enough real-user Chrome traffic in CrUX; lab scores still apply.
    Full history saved to cwv_history.csv.
  </p>
</div>
<script>
  const pts = {trend_js};
  new Chart(document.getElementById('trend'), {{
    type:'line',
    data:{{ labels: pts.map(p=>p[0]),
            datasets:[{{ label:'Avg mobile perf', data: pts.map(p=>p[1]),
                        borderColor:'#1f6fe0', backgroundColor:'rgba(31,111,224,.1)',
                        fill:true, tension:.25, pointRadius:4 }}] }},
    options:{{ scales:{{ y:{{ min:0, max:100 }} }},
              plugins:{{ legend:{{ display:false }} }} }}
  }});
</script>
</body></html>"""
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Monitor Core Web Vitals via PageSpeed Insights API.")
    ap.add_argument("--sitemap", help="URL of the site's sitemap.xml (auto-discovers all pages)")
    ap.add_argument("--urls-file", help="Text file with one URL per line (alternative to --sitemap)")
    ap.add_argument("--site", default="My Site", help="Site name for the report")
    ap.add_argument("--output", default="output", help="Output directory")
    ap.add_argument("--strategies", default="mobile,desktop",
                    help="Comma list: mobile,desktop (default both)")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="Seconds to wait between API calls (default 1)")
    ap.add_argument("--api-key", default=os.environ.get("CWV_API_KEY"),
                    help="PSI API key (or set CWV_API_KEY env var)")
    ap.add_argument("--limit", type=int, default=0, help="Only test first N urls (0 = all)")
    args = ap.parse_args()

    if not args.api_key:
        sys.exit("ERROR: no API key. Set CWV_API_KEY env var or pass --api-key.")

    os.makedirs(args.output, exist_ok=True)
    csv_path = os.path.join(args.output, "cwv_history.csv")
    run_date = dt.date.today().isoformat()
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]

    urls = load_urls(args)
    if args.limit:
        urls = urls[:args.limit]
    if not urls:
        sys.exit("ERROR: no URLs found.")
    print(f"\nTesting {len(urls)} URL(s) x {len(strategies)} strategy = "
          f"{len(urls)*len(strategies)} API calls\n")

    rows = []
    total = len(urls) * len(strategies)
    i = 0
    for url in urls:
        for strat in strategies:
            i += 1
            print(f"[{i}/{total}] {strat:7} {url}")
            try:
                data = run_psi(url, strat, args.api_key)
                r = parse_result(data, url, strat)
            except Exception as e:
                r = {"url": url, "strategy": strat, "error": str(e)}
                print(f"    ERROR: {e}")
            r["run_date"] = run_date
            r["site"] = args.site
            rows.append(r)
            time.sleep(args.delay)

    append_history(csv_path, rows)
    history = read_history(csv_path)
    report_path = os.path.join(args.output, f"report_{run_date}.html")
    build_report(report_path, args.site, run_date, rows, history)

    ok = sum(1 for r in rows if not r.get("error"))
    print(f"\nDone. {ok}/{len(rows)} calls succeeded.")
    print(f"  History : {csv_path}")
    print(f"  Report  : {report_path}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
run_all.py
------------------------------------------------------------------------------
Multi-site driver for the Core Web Vitals monitor.

Reads sites.csv (one row per website) and runs cwv_monitor for each, writing
each site's report + history into its own subfolder under ./docs, then builds
a combined index.html that links to every site with its latest average score.

To add a new website to monitoring: just add a line to sites.csv. Nothing else.

sites.csv format (header required):
    name,sitemap
    Jaw Surgery NJ,https://jawsurgerynj.com/sitemap.xml
    Premier LA Perio,https://premierlaperio.com/sitemap_index.xml

Run:
    python run_all.py                 # uses CWV_API_KEY env var
    python run_all.py --limit 3       # quick trial: first 3 pages per site
------------------------------------------------------------------------------
"""
import argparse
import csv
import json
import datetime as dt
import os
import re
import sys

import cwv_monitor as m


def slugify(name):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "site"


def read_sites(path):
    if not os.path.isfile(path):
        sys.exit(f"ERROR: {path} not found. Create it with header: name,sitemap")
    sites = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = (row.get("name") or "").strip()
            sitemap = (row.get("sitemap") or "").strip()
            if not name or not sitemap or name.startswith("#"):
                continue
            # optional per-site alert settings (blank = default)
            def _int(v, d):
                v = (v or "").strip()
                try:
                    return int(v)
                except ValueError:
                    return d
            emails = [e.strip() for e in (row.get("emails") or "").replace(",", ";").split(";")
                      if e.strip()]
            alert = {
                "on": (row.get("alerts_on") or "yes").strip().lower() not in ("no", "off", "false", "0"),
                "min_score": _int(row.get("min_score"), 80),
                "max_drop": _int(row.get("max_drop"), 10),
                "emails": emails,
            }
            sites.append({"name": name, "sitemap": sitemap, "_alert": alert})
    if not sites:
        sys.exit("ERROR: sites.csv has no valid rows.")
    return sites


def _num(v):
    if v in (None, "", "None"):
        return None
    try:
        f = float(v)
        return int(f) if f == int(f) else f
    except (ValueError, TypeError):
        return None


def run_site(site, api_key, out_dir, strategies, delay, limit):
    """Run the monitor for one site; return a dict for data.json."""
    os.makedirs(out_dir, exist_ok=True)
    run_date = dt.date.today().isoformat()

    # A site can be defined by:
    #   (a) a sitemap URL                          -> auto-discovers pages
    #   (b) a local file of URLs (one per line)    -> e.g. "file:premier-urls.txt"
    #   (c) one or more page URLs inline, '|'-sep  -> manual list
    # (b) and (c) are useful when a site blocks bots from its sitemap.
    src = site["sitemap"].strip()

    if src.lower().startswith("file:"):
        fname = src[5:].strip()
        try:
            with open(fname, "r", encoding="utf-8") as fh:
                urls = [ln.strip() for ln in fh
                        if ln.strip() and not ln.strip().startswith("#")
                        and ln.strip().lower().startswith("http")]
            print(f"  ({len(urls)} URLs loaded from {fname})")
        except FileNotFoundError:
            print(f"  ! URL list file not found: {fname}")
            urls = []
    else:
        parts = [p.strip() for p in re.split(r"[\s|]+", src) if p.strip()]
        looks_like_sitemap = (len(parts) == 1 and
                              ("sitemap" in parts[0].lower() or parts[0].lower().endswith(".xml")))
        if looks_like_sitemap:
            urls = m.urls_from_sitemap(parts[0])
        else:
            urls = parts  # inline manual list

    seen, clean = set(), []
    for u in urls:
        if u in seen:
            continue
        if any(u.lower().endswith(ext) for ext in
               (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".pdf", ".xml")):
            continue
        seen.add(u); clean.append(u)
    if limit:
        clean = clean[:limit]
    if not clean:
        print(f"  ! no URLs found for {site['name']} ({site['sitemap']})")
        return None

    print(f"\n=== {site['name']}: {len(clean)} pages x {len(strategies)} ===")
    rows = []
    total = len(clean) * len(strategies)
    i = 0
    for url in clean:
        for strat in strategies:
            i += 1
            print(f"  [{i}/{total}] {strat:7} {url}")
            try:
                data = m.run_psi(url, strat, api_key)
                r = m.parse_result(data, url, strat)
            except Exception as e:
                r = {"url": url, "strategy": strat, "error": str(e)}
                print(f"      ERROR: {e}")
            r["run_date"] = run_date
            r["site"] = site["name"]
            rows.append(r)
            import time as _t; _t.sleep(delay)

    # ---- stabilize the HOMEPAGE: run it extra times and take the median
    # Performance (the one timing-sensitive, run-to-run-variable score). Other
    # categories are deterministic, so we keep them from the first run.
    homepage_samples = int(os.environ.get("HOMEPAGE_SAMPLES", "5"))
    home_url = None
    for u in clean:
        try:
            from urllib.parse import urlparse
            if urlparse(u).path in ("", "/"):
                home_url = u
                break
        except Exception:
            pass
    if home_url is None:
        home_url = clean[0]

    home_perfs = []
    # include the perf we already got for the homepage this run (mobile)
    for r in rows:
        if r.get("url") == home_url and r.get("strategy") == "mobile":
            v = _num(r.get("perf_score"))
            if v is not None:
                home_perfs.append(v)
    extra = max(0, homepage_samples - 1)
    if extra:
        print(f"\n  Stabilizing homepage ({home_url}) with {extra} extra sample(s)…")
        for k in range(extra):
            try:
                data = m.run_psi(home_url, "mobile", api_key)
                rr = m.parse_result(data, home_url, "mobile")
                v = _num(rr.get("perf_score"))
                if v is not None:
                    home_perfs.append(v)
                    print(f"    homepage sample {k+2}/{homepage_samples}: {v}")
            except Exception as e:
                print(f"    homepage sample {k+2} failed: {e}")
            import time as _t; _t.sleep(delay)

    home_perf_median = None
    if home_perfs:
        s = sorted(home_perfs)
        n = len(s)
        home_perf_median = s[n // 2] if n % 2 else round((s[n//2 - 1] + s[n//2]) / 2)
        print(f"  Homepage Performance: samples={s} -> median {home_perf_median}")

    # keep permanent CSV history per site — capture PREVIOUS score first
    csv_path = os.path.join(out_dir, "cwv_history.csv")
    prev_history = m.read_history(csv_path)
    prev_dates = sorted({h["run_date"] for h in prev_history if h.get("strategy") == "mobile"})
    prev_score = None
    if prev_dates:
        last = prev_dates[-1]
        vals = [_num(h.get("perf_score")) for h in prev_history
                if h.get("strategy") == "mobile" and h.get("run_date") == last]
        vals = [v for v in vals if v is not None]
        if vals:
            prev_score = round(sum(vals) / len(vals))

    m.append_history(csv_path, rows)
    history = m.read_history(csv_path)

    # ---- build per-page structure for the React app ----
    by_url = {}
    for r in rows:
        u = r["url"]
        d = by_url.setdefault(u, {"url": u, "mobile": {}, "desktop": {}})
        strat = r.get("strategy")
        if strat in ("mobile", "desktop"):
            d[strat] = {
                "perf":  _num(r.get("perf_score")),
                "a11y":  _num(r.get("a11y_score")),
                "bp":    _num(r.get("bp_score")),
                "seo":   _num(r.get("seo_score")),
            }
    pages_data = list(by_url.values())

    # apply the stabilized homepage median to the homepage's mobile Performance
    if home_perf_median is not None:
        for p in pages_data:
            if p["url"] == home_url and p.get("mobile"):
                p["mobile"]["perf"] = home_perf_median
                p["mobile"]["perfSamples"] = sorted(home_perfs)

    # ---- trend: avg mobile perf per run_date, from full history ----
    trend_map = {}
    for h in history:
        if h.get("strategy") != "mobile":
            continue
        val = _num(h.get("perf_score"))
        if val is not None:
            trend_map.setdefault(h["run_date"], []).append(val)
    trend = [{"date": d, "score": round(sum(v) / len(v))}
             for d, v in sorted(trend_map.items()) if v]

    mob = [r for r in rows if r["strategy"] == "mobile"
           and _num(r.get("perf_score")) is not None]
    avg = round(sum(_num(r["perf_score"]) for r in mob) / len(mob)) if mob else None
    n_fail = sum(1 for r in rows if r["strategy"] == "mobile"
                 and str(r.get("field_overall", "")).upper() == "SLOW")
    n_nodata = sum(1 for r in rows if r["strategy"] == "mobile"
                   and not r.get("field_overall"))

    return {
        "name": site["name"],
        "sample": clean[0] if clean else site["sitemap"],
        "pages": len(clean),
        "avgMobile": avg,
        "failCount": n_fail,
        "noDataCount": n_nodata,
        "trend": trend,
        "pagesData": pages_data,
        "_alert": site.get("_alert", {}),
        "_prev_score": prev_score,
    }


def write_data_json(docs_dir, site_payloads):
    """Write docs/data.json — the single file the React app reads.
    Internal keys (alert config, prev score) are stripped so recipient
    emails are never exposed in the public dashboard."""
    public = []
    for s in site_payloads:
        public.append({k: v for k, v in s.items() if not k.startswith("_")})
    payload = {
        "generated": dt.date.today().isoformat(),
        "sites": public,
    }
    with open(os.path.join(docs_dir, "data.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser(description="Run CWV monitor for all sites in sites.csv")
    ap.add_argument("--sites", default="sites.csv")
    ap.add_argument("--docs", default="docs")
    ap.add_argument("--strategies", default="mobile")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--api-key", default=os.environ.get("CWV_API_KEY"))
    args = ap.parse_args()

    if not args.api_key:
        sys.exit("ERROR: no API key. Set CWV_API_KEY env var or pass --api-key.")

    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    sites = read_sites(args.sites)
    os.makedirs(args.docs, exist_ok=True)

    print(f"Monitoring {len(sites)} site(s): {', '.join(s['name'] for s in sites)}")
    payloads = []
    for site in sites:
        slug = slugify(site["name"])
        out_dir = os.path.join(args.docs, slug)
        payload = run_site(site, args.api_key, out_dir, strategies, args.delay, args.limit)
        if payload:
            payloads.append(payload)

    write_data_json(args.docs, payloads)
    print(f"\nData written to {os.path.join(args.docs, 'data.json')}")

    # ---- alerts ----
    try:
        import alerts
        prev_scores = {s["name"]: s.get("_prev_score") for s in payloads}
        dashboard_url = os.environ.get("DASHBOARD_URL")
        n = alerts.run_alerts(payloads, prev_scores, dashboard_url)
        print(f"Alerts: {n} email(s) sent." if n else "Alerts: nothing tripped.")
    except Exception as e:
        print(f"Alert step skipped ({e})")

    for s in payloads:
        print(f"  {s['name']:24} avg mobile score: {s['avgMobile']}")


if __name__ == "__main__":
    main()

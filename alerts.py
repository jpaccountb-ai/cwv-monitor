#!/usr/bin/env python3
"""
alerts.py
------------------------------------------------------------------------------
Evaluates each site's results against its alert rules and sends an email via
Resend (https://resend.com) when something trips.

Alert triggers (per-site, configurable in sites.csv):
  1. Mobile performance score below `min_score`      (default 80)
  2. Any Core Web Vital in the POOR range            (LCP>4s, INP>500ms, CLS>0.25)
  3. Mobile score dropped `max_drop`+ points          (default 10) vs the previous run

Environment variables (set as GitHub secrets):
  RESEND_API_KEY   required to send. If unset, alerts are computed and printed
                   but no email is sent (safe dry-run).
  ALERT_FROM       sender address. Default "onboarding@resend.dev" (Resend's
                   test address — delivers only to your own verified email).
                   Set to e.g. "alerts@impacthma.com" once your domain is verified.
------------------------------------------------------------------------------
"""
import json
import os
import urllib.request

TH = {"LCP": 4000, "INP": 500, "CLS": 0.25}  # POOR thresholds


def _poor_cwv(m):
    """Return list of Core Web Vitals in the poor range for a metrics dict."""
    bad = []
    if m.get("fLCP") is not None and m["fLCP"] > TH["LCP"]:
        bad.append(f"LCP {round(m['fLCP']/1000,1)}s")
    if m.get("fINP") is not None and m["fINP"] > TH["INP"]:
        bad.append(f"INP {round(m['fINP'])}ms")
    if m.get("fCLS") is not None and m["fCLS"] > TH["CLS"]:
        bad.append(f"CLS {round(m['fCLS'],2)}")
    return bad


def _homepage(site):
    pd = site.get("pagesData", [])
    for p in pd:
        try:
            from urllib.parse import urlparse
            if urlparse(p["url"]).path == "/":
                return p
        except Exception:
            pass
    return pd[0] if pd else None


def evaluate_site(site, prev_score=None):
    """Return a list of human-readable alert reasons for one site."""
    cfg = site.get("_alert", {})
    if not cfg.get("on", True):
        return []
    min_score = cfg.get("min_score", 80)
    max_drop = cfg.get("max_drop", 10)

    hp = _homepage(site)
    reasons = []
    if not hp:
        return reasons
    m = hp.get("mobile", {}) or {}
    score = m.get("perf")

    # 1. below threshold
    if score is not None and score < min_score:
        reasons.append(f"Mobile score {score} is below the {min_score} threshold.")

    # 2. any CWV poor
    bad = _poor_cwv(m)
    if bad:
        reasons.append("Core Web Vitals in poor range: " + ", ".join(bad) + ".")

    # 3. sharp drop vs previous run
    if score is not None and prev_score is not None:
        drop = prev_score - score
        if drop >= max_drop:
            reasons.append(f"Mobile score dropped {drop} points since last run "
                           f"({prev_score} → {score}).")
    return reasons


def send_email(to_list, subject, html, api_key, sender):
    payload = json.dumps({
        "from": sender,
        "to": to_list,
        "subject": subject,
        "html": html,
    }).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


def build_email_html(site_name, host, reasons, dashboard_url=None):
    items = "".join(f"<li style='margin:6px 0'>{r}</li>" for r in reasons)
    link = (f'<p style="margin:20px 0 0"><a href="{dashboard_url}" '
            f'style="background:#111826;color:#fff;padding:10px 18px;border-radius:8px;'
            f'text-decoration:none;font-size:14px">View full report</a></p>'
            if dashboard_url else "")
    return f"""
    <div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:560px;
                margin:0 auto;color:#111826">
      <p style="font-size:13px;color:#98a1ad;margin:0 0 4px;text-transform:uppercase;
                letter-spacing:.05em">Performance alert</p>
      <h2 style="margin:0 0 2px;font-size:20px">{site_name}</h2>
      <p style="margin:0 0 16px;color:#586372;font-size:14px">{host}</p>
      <p style="margin:0 0 8px;font-size:14px">The following issue(s) were detected on the
         latest check:</p>
      <ul style="padding-left:18px;font-size:14px;color:#111826">{items}</ul>
      {link}
      <p style="margin:26px 0 0;font-size:12px;color:#98a1ad;border-top:1px solid #e9ecf0;
                padding-top:14px">
        Sent automatically by your Site Performance monitor. Thresholds are configured
        per site in sites.csv.</p>
    </div>"""


def run_alerts(payloads, history_prev_scores, dashboard_url=None):
    """
    payloads: list of site dicts (as written to data.json, with _alert config)
    history_prev_scores: {site_name: previous_run_mobile_avg_or_home_score}
    Returns number of alert emails sent.
    """
    api_key = os.environ.get("RESEND_API_KEY")
    sender = os.environ.get("ALERT_FROM", "onboarding@resend.dev")
    sent = 0

    for site in payloads:
        reasons = evaluate_site(site, history_prev_scores.get(site["name"]))
        if not reasons:
            continue
        recipients = site.get("_alert", {}).get("emails", [])
        print(f"\n  ALERT — {site['name']}:")
        for r in reasons:
            print(f"     - {r}")

        if not recipients:
            print("     (no recipient emails set for this site; skipping send)")
            continue
        if not api_key:
            print("     (RESEND_API_KEY not set; would email: "
                  + ", ".join(recipients) + ")")
            continue

        host = ""
        try:
            from urllib.parse import urlparse
            host = urlparse(site.get("sample", "")).host
        except Exception:
            pass
        html = build_email_html(site["name"], host, reasons, dashboard_url)
        subject = f"Performance alert: {site['name']}"
        try:
            send_email(recipients, subject, html, api_key, sender)
            print("     emailed: " + ", ".join(recipients))
            sent += 1
        except Exception as e:
            print(f"     ! email failed: {e}")
    return sent

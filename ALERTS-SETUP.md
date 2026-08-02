# Email Alerts — Setup

Your monitor can email you (and per-site recipients) whenever a site trips an
alert. No extra infrastructure — it uses Resend (a free email API) called from
the same monthly job.

## What triggers an alert (per site, set in sites.csv)
1. **Mobile performance score below `min_score`** (default 80)
2. **Any Core Web Vital in the poor range** (LCP > 4s, INP > 500ms, CLS > 0.25)
3. **Mobile score dropped `max_drop`+ points** since the last run (default 10)

Alerts are evaluated on each site's **homepage** result.

## Per-site settings (sites.csv columns)
```
name,sitemap,alerts_on,min_score,max_drop,emails
Jaw Surgery NJ,https://jawsurgerynj.com/sitemap.xml,yes,80,10,jp@impacthma.com;client@practice.com
Premier LA Perio,https://premierlaperio.com/sitemap_index.xml,yes,90,10,jp@impacthma.com
Prospect Co,https://prospect.com/sitemap.xml,no,,,
```
- **alerts_on** — `yes` / `no` to enable alerts for that site.
- **min_score / max_drop** — leave blank to use defaults (80 / 10).
- **emails** — one or more recipients, separated by `;` (or `,`).

Every site can have its own thresholds and its own recipient list.

## One-time setup

### 1. Get a free Resend API key
1. Sign up at <https://resend.com> (free).
2. **API Keys → Create API Key**, copy it.

### 2. Add it as a GitHub secret
Repo **Settings → Secrets and variables → Actions → Secrets → New repository secret**
- Name: `RESEND_API_KEY`
- Value: your Resend key

### 3. (Recommended) Set the dashboard link in alert emails
Same page → **Variables** tab → New variable:
- `DASHBOARD_URL` = your Pages URL (e.g. `https://yourname.github.io/cwv-monitor/`)

### 4. Choose your sender address
- **To start / test:** do nothing. Emails send from `onboarding@resend.dev`
  (Resend's test address). **Note:** the test address only reliably delivers to
  the email you signed up to Resend with — fine for confirming it works, not for
  client recipients.
- **For real delivery to any recipient:** verify your domain in Resend
  (**Domains → Add Domain**, add the DNS records it shows — takes a few minutes),
  then add a GitHub **Variable** `ALERT_FROM` = `alerts@impacthma.com`.

## Testing without waiting a month
Trigger a run manually: **Actions → Core Web Vitals Monitor → Run workflow**.
The run log prints every alert it evaluates and whether it emailed. If
`RESEND_API_KEY` isn't set yet, it prints "would email …" instead of sending —
a safe dry run.

## Notes
- Recipient emails live only in `sites.csv` and are **never** written to the
  public dashboard (data.json strips them), so your Pages site won't expose them.
- Given GHL sites often score under 80, expect the score-threshold alert to fire
  regularly for those; the **drop** alert is your best early-warning for real
  regressions. Tune `min_score` per site to taste.

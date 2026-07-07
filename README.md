# Commission Tracker — Cymate

A dashboard for tracking affiliate commission expected vs. received across all vendors, with automatic receipt capture and monthly reconciliation.

**Live dashboard:** https://cymate-commissions.vercel.app  
**GitHub repo:** https://github.com/cymateio/commission-tracker  

---

## Tech Stack

| Layer | Tool |
|-------|------|
| Frontend | Single-file React 18 app (`index.html`) — no build step |
| Database | Supabase (`commissions` table) |
| Receipt capture | Gmail Apps Script → Google Sheet |
| Hosting | Vercel |
| Version control | GitHub (`cymateio/commission-tracker`) |

---

## Project Structure

```
commission-tracker/
├── index.html              # Entire frontend app (React via CDN)
├── vercel.json             # Vercel routing config
├── deploy.py               # One-command Vercel deployment script
├── gmail-receipts-sync.gs  # Gmail Apps Script (runs in Google account)
└── README.md               # This file
```

---

## Vendors Tracked

| Vendor | Commission | Payer (in bank) | Payment method |
|--------|------------|-----------------|----------------|
| **Zapmail** | 10% of referred revenue | Rapidify Labs Inc. | Wise → admin@cymate.io |
| **Smartlead** | Payout = commission | 521 code | PayPal → admin@cymate.io |
| **HeyReach** | Payout = commission | Tolt, Inc. | PayPal → admin@cymate.io |
| **Inboxkit** | $1 per active mailbox/month | Enrich Labs L.L.C-FZ | Mercury bank wire → admin@cymate.io (notification from hello@mercury.com) |
| **Icypeas** | TBD | — | Manual |
| **LeadMagic** | TBD | — | Manual |

### Inboxkit Notes
- Commission = $1 × number of active mailboxes that month (recurring, no cap)
- Source of truth: [studio.inboxkit.com/billing](https://studio.inboxkit.com/billing) → Billing & Payouts → "Mailboxes" profit column
- Payout is **manual** — click "Request Payout" (min $50 threshold)
- Confirmation email comes from `hello@mercury.com` to `admin@cymate.io`

### Zapmail Notes
- Month convention = **earned month** (not received month) — ~3 month payout lag
- Example: commissions earned in January → paid in April → recorded as `2026-01`
- Expected values refreshed via `/zapmail-check` skill (run from logged-in browser)

---

## Database — Supabase

**Project:** `eciesfegyvsdsqunkeyf`  
**Table:** `commissions`

| Column | Type | Description |
|--------|------|-------------|
| `vendor` | text | zapmail, smartlead, heyreach, inboxkit, icypeas, leadmagic |
| `month` | text | `YYYY-MM` format |
| `expected` | numeric | Commission amount expected from vendor |
| `received` | numeric | Amount actually received in bank |
| `notes` | text | Calculation notes or flags |
| `updated_at` | timestamp | Last updated |

**Reconciliation logic:**
- `|diff| < $0.50` → ✅ Confirmed
- `diff <= -$0.50` → 🔴 Shortfall (flag for follow-up)
- `expected` set, `received` null, month > 60 days old → 🔴 Overdue

---

## Redeployment Guide

### Every time you update the dashboard

**Step 1 — Edit**  
Make changes to `index.html`

**Step 2 — Push to GitHub**
```bash
cd ~/Documents/GitHub/commission-tracker
git add index.html
git commit -m "describe your changes"
git push
```

**Step 3 — Deploy to Vercel**
```bash
python3 deploy.py <your-vercel-token>
```

Get your Vercel token at [vercel.com/account/tokens](https://vercel.com/account/tokens)  
(Token name: `commission-tracker-deploy`, scoped to admin@cymate.io, expires 1 year)

The live site at **cymate-commissions.vercel.app** updates within seconds.

---

### Optional: Auto-deploy on push

Connect GitHub to Vercel so every `git push` deploys automatically — no need to run `deploy.py`:

1. Go to [vercel.com/dashboard](https://vercel.com/dashboard) → `commission-tracker` → **Settings → Git**
2. Click **Connect Git Repository** → select `cymateio/commission-tracker`
3. From now on, `git push` = auto-deploy ✅

---

## Monthly Reconciliation

Run the scheduled task **commission-reconciliation-reminder** (or `/zapmail-check` for Zapmail) around the 5th of each month.

### Checklist
- [ ] Check Google Sheet "Commission Receipts — Auto Log" for new PayPal/Wise receipts
- [ ] Check `admin@cymate.io` (Mercury) for Inboxkit wire from Enrich Labs L.L.C-FZ
- [ ] Run `/zapmail-check` from a logged-in browser to save pending Zapmail commissions
- [ ] Visit [studio.inboxkit.com/billing](https://studio.inboxkit.com/billing) and update Inboxkit expected
- [ ] Review dashboard for any 🔴 Shortfall or 🔴 Overdue flags

---

## Accounts

| Service | Login |
|---------|-------|
| Vercel | admin@cymate.io |
| GitHub | admin@cymate.io (`cymateio`) |
| Supabase | admin@cymate.io |
| Inboxkit Partner Studio | admin@cymate.io |

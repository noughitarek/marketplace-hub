# Marketplace Hub

Flask information system + Tampermonkey agent for selling through Facebook Marketplace and shipping with EcoTrack.

## What is inside
| Area | Where |
|---|---|
| Products, orders, stock, purchases, dashboard, workers, settings | web UI (`/`) |
| Order auto-creation from phone numbers (every 10 min) | `app/tasks.py → scan_phones` |
| EcoTrack create / update / delete / dispatch + hourly status sync | `app/services/ecotrack.py`, `app/services/orders.py`, `app/tasks.py → sync_ecotrack` |
| Gemini answers with DB cache | `POST /api/v1/answers/generate` |
| Browser agent | `userscript/marketplace-hub-agent.user.js` |

## Deploy
```bash
cp .env.example .env        # set SECRET_KEY, ADMIN_PASSWORD (and DB_PASSWORD)
docker compose up -d --build
```
Open `http://SERVER:8000`, sign in with `ADMIN_PASSWORD`. Put it behind HTTPS (Caddy / nginx / your host's proxy) before exposing it.
Without Docker: `pip install -r requirements.txt`, set `DATABASE_URL`, run `gunicorn -w 1 --threads 8 wsgi:app`.
Keep **one** gunicorn worker (it runs the scheduler; a DB lock also prevents double runs).
Tables are created automatically on start. For later schema changes add Flask-Migrate/Alembic.

## First-time setup (in this order)
1. **Settings → Carrier**: EcoTrack base URL (your carrier's domain) + API token → "Save and test token".
2. **Settings → Assistant**: Gemini key, describe your shop, delivery info.
3. **Products**: add products with price, cost, opening stock and a description (the AI reads it).
4. Install Tampermonkey, create a new script, paste `userscript/marketplace-hub-agent.user.js`.
5. Open Facebook → Marketplace → Inbox in a dedicated window. Tampermonkey menu → **Configure server** (URL + key from Settings → Script connection).
6. The account appears in **Message workers** within a minute. Enable it, choose **Draft** mode first.

## How the pieces behave
- **One tab**: there is no automatic tab guard. Run the script in a single dedicated tab; two tabs on the same account would both answer the same customers.
- **Marketplace only**: a thread counts only if its pane contains a `/marketplace/item/` link (or you are on `/marketplace/inbox`).
- **Phone → order**: customer messages are scanned for Algerian numbers (05/06/07…, +213, Arabic digits). If no order has that number, a draft order is created, pre-filled by Gemini when possible, and flagged "to review" with the chat beside the form. **Confirm** pushes it to EcoTrack (`type=1`, `reference=ORD-<id>`), stores the tracking and takes the items out of stock.
- **Editing**: saving a confirmed order re-checks its live status; it is sent to EcoTrack only while the status is `prete_a_expedier`. Otherwise you get a warning and the change stays local.
- **Statuses** every hour: delivered → revenue/profit counted; `retour_*` → return fee counted, stock restored once `retour_recu`; `annule` → cancelled, stock restored.
- **Profit** = collected amount − cost of goods − carrier fee (fees come from EcoTrack's order list). Purchases feed stock and average cost.

## API for the agent (header `X-API-Key`)
`POST /api/v1/agent/register` · `POST /api/v1/agent/sync` · `POST /api/v1/answers/generate` · `POST /api/v1/answers/<id>/result` · `GET /api/v1/agent/config` · `GET /api/v1/health`

## Known limits
- Facebook's page markup is obfuscated and changes. If threads are not detected, use Tampermonkey menu → **Test scrape** and adjust `CFG.SELECTORS`.
- The script cannot tell "my listing" from "I am the buyer" by itself; override `CFG.isMyListingThread` if you also message sellers.
- Message timestamps are not scraped: messages are ordered by when the server first saw them.
- Money is stored as whole dinars.
- Secrets (tokens) are stored in the database in clear text; protect the DB and backups.

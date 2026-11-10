# PayPal Transaction Resolution Copilot

A full-stack transaction resolution copilot for support agents and merchants. Built around explaining PayPal transaction states from verified database/API records, displaying real payment event timelines, detecting possible duplicate payments, and preparing copy-ready support summaries — with secondary support for dispute analysis and resolution case tracking.

---

## Core Capabilities

- **Transaction State Explanation (Grounded)** — Explains `COMPLETED`, `PENDING`, `FAILED`, `DISPUTED`, and `REFUNDED` states using only verified database/API fields (`status`, `metadata` error codes, shipping expectations, timeline events, linked disputes, and duplicate flags). Runs via deterministic heuristic rules offline or OpenAI `gpt-4o-mini` when configured.
- **Payment Event Timelines** — Tracks and displays real timestamped events (`TransactionEvent` table) updated by seed data, refunds, dispute actions, and incoming webhooks.
- **Duplicate Payment Detection** — Detects matching transactions within a 24-hour window (`buyer_email`, `merchant_id`, `amount`, `currency`) and distinguishes active `POSSIBLE_DUPLICATE` captures from `RETRY_AFTER_FAILURE` and `ALREADY_REFUNDED`.
- **Support Summary Preparation** — Generates copy-ready **Internal Support Notes**, **Buyer Replies**, and **Escalation Briefs** strictly from verified transaction records.
- **Mock vs Sandbox/Live Distinction** — PayPal API responses in mock mode are explicitly labeled (`mode: "mock"`, `is_simulated: true`, `warning: ...`) and return `404` for unknown IDs instead of inventing transaction statuses.
- **Official PayPal Webhook Verification** — Uses PayPal's `POST /v1/notifications/verify-webhook-signature` endpoint when `PAYPAL_VERIFY_WEBHOOKS=true` and `PAYPAL_WEBHOOK_ID` is configured (explicitly tagged `skipped_dev_mode` only when disabled for local dev).
- **Disputes & Cases (Secondary)** — Includes dispute tracking, evidence submission, AI dispute recommendations, and case SLA monitoring.

---

## Tech Stack

| Layer | Technology |
|---|---|
| **Frontend** | React 18, Vite, Tailwind CSS, Axios, React Router v6, Recharts |
| **Backend** | Python 3.11–3.13, FastAPI, SQLAlchemy 2.x, Pydantic v2, httpx, Uvicorn |
| **Database** | SQLite (`copilot.db` in dev, WAL mode) / PostgreSQL-compatible via SQLAlchemy |
| **AI** | OpenAI `gpt-4o-mini` (optional), deterministic grounded heuristic engine (default) |

---

## Setup & Running Locally (Mock Mode)

### Terminal 1 — Backend (port `8000`)

**Windows (PowerShell):**
```powershell
cd d:\paypal-transaction-resolution-copilot\backend
python -m venv venv   # or full path to python.exe if not on PATH
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy ..\.env.example .env
uvicorn main:app --reload
```

**Linux / macOS:**
```bash
cd paypal-transaction-resolution-copilot/backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp ../.env.example .env
uvicorn main:app --reload
```

### Terminal 2 — Frontend (port `5173`)

```bash
cd paypal-transaction-resolution-copilot/frontend
npm install
npm run dev
```

- **Frontend:** `http://localhost:5173`
- **Backend Swagger Docs:** `http://localhost:8000/docs`

On first startup, the backend initializes `copilot.db` and seeds 7 transactions (covering `COMPLETED`, `PENDING`, `FAILED`, `DISPUTED`, `REFUNDED`, plus a possible duplicate pair `TXN-2024-001` & `TXN-2024-007`), per-transaction timeline events, 2 disputes, and 2 resolution cases.

---

## Environment Variables

Copy `.env.example` to `backend/.env`. Leave PayPal and OpenAI keys as placeholders to run in mock/heuristic mode.

| Variable | Description | Default |
|---|---|---|
| `PAYPAL_CLIENT_ID` | PayPal REST app client ID | `your_paypal_client_id` (mock mode) |
| `PAYPAL_CLIENT_SECRET` | PayPal REST app secret | `your_paypal_client_secret` (mock mode) |
| `PAYPAL_BASE_URL` | PayPal API base URL | `https://api-m.sandbox.paypal.com` |
| `PAYPAL_WEBHOOK_ID` | PayPal Webhook ID for signature checks | `""` |
| `PAYPAL_VERIFY_WEBHOOKS` | Enable PayPal API webhook verification (`true`/`false`) | `false` |
| `OPENAI_API_KEY` | Optional OpenAI API key for `gpt-4o-mini` | `your_openai_api_key` (heuristic mode) |
| `DATABASE_URL` | SQLAlchemy connection URL | `sqlite:///./copilot.db` |
| `SECRET_KEY` | Application secret key | `your_secret_key_here` |
| `ALLOWED_ORIGINS` | Comma-separated CORS allowed origins | `http://localhost:5173` |

---

## Implemented API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` (`/api/health`) | Service status (`paypal` mode, `ai` mode, `webhook_verification`) |
| `GET` | `/api/analytics/summary` | Dashboard KPIs, duplicate alerts, status breakdowns, recent events |
| `GET` | `/api/transactions` | Paginated transaction list (`search`, `status`, `date_from`, `date_to`) |
| `POST` | `/api/transactions` | Record a transaction + initial timeline event |
| `GET` | `/api/transactions/{id}` | Fetch transaction details |
| `GET` | `/api/transactions/{id}/resolution` | Complete resolution bundle (record, timeline, duplicates, explanation, disputes) |
| `GET` | `/api/transactions/{id}/timeline` | Verified payment event timeline |
| `GET` | `/api/transactions/{id}/duplicates` | Duplicate/retry detection results |
| `POST` | `/api/transactions/{id}/explain` | Record-grounded transaction state explanation |
| `POST` | `/api/transactions/{id}/support-summary` | Generate copy-ready support summary (`buyer` / `internal` / `escalation`) |
| `POST` | `/api/transactions/{id}/refund` | Issue refund via PayPal API (or tagged mock) & log timeline event |
| `GET` | `/api/transactions/{id}/disputes` | Disputes associated with a transaction |
| `GET` | `/api/paypal/transactions/{id}` | Direct PayPal capture lookup (or tagged mock lookup) |
| `GET` | `/api/disputes` | Paginated dispute list (`status`, `reason`) |
| `GET` | `/api/disputes/{id}` | Fetch dispute details |
| `POST` | `/api/disputes/{id}/evidence` | Submit dispute evidence |
| `POST` | `/api/disputes/{id}/escalate` | Escalate dispute |
| `POST` | `/api/disputes/{id}/accept` | Accept dispute claim |
| `GET` | `/api/cases` | Paginated resolution cases with SLA metrics |
| `GET` | `/api/cases/{id}` | Fetch case details |
| `PUT` | `/api/cases/{id}` | Update case steps / outcome |
| `GET` | `/api/cases/{id}/summary` | Markdown case summary + SLA info |
| `POST` | `/api/ai/analyze` | AI dispute recommendation |
| `POST` | `/api/ai/risk` | Transaction risk classification |
| `POST` | `/api/webhooks/paypal` | Receive & verify PayPal webhooks via `/v1/notifications/verify-webhook-signature` |

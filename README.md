# PayPal Transaction Resolution Copilot

A full-stack transaction resolution copilot for support agents and merchants. Built around explaining PayPal transaction states from verified database/API records, displaying real payment event timelines, detecting possible duplicate payments, and preparing copy-ready support summaries — with secondary support for dispute analysis and resolution case tracking.

---

## Core Capabilities

- **Record-Grounded Transaction Explanations** — Explains `COMPLETED`, `PENDING`, `FAILED`, `DISPUTED`, and `REFUNDED` states strictly from verified database, PayPal API, and webhook evidence. Returns deterministic `source_evidence` alongside `mode` (`heuristic` by default; optional `openai` rewrite layer) and explicitly discloses unavailable fields instead of guessing unrecorded reasons or outcomes.
- **Payment Event Timelines** — Tracks and displays real timestamped events (`TransactionEvent` table) updated by seed data, Sandbox captures, partial/full refunds, dispute actions, and verified webhooks.
- **Duplicate Payment Detection** — Detects matching transactions within a 24-hour window (`buyer_email`, `merchant_id`, `amount`, `currency`) and distinguishes active `POSSIBLE_DUPLICATE` captures from `RETRY_AFTER_FAILURE` and `ALREADY_REFUNDED`.
- **Support Summary Preparation** — Generates copy-ready **Internal Support Notes**, **Buyer Replies**, and **Escalation Briefs** strictly from verified transaction records and `verified_facts`.
- **PayPal Sandbox Integration & Mock Distinction** — Supports real PayPal Sandbox OAuth2, Orders v2 create/capture (`POST /api/paypal/orders`, `/capture`), capture sync (`/api/paypal/sync/{capture_id}`), and full/partial capture refunds (`POST /api/transactions/{id}/refund`). In mock mode, responses are explicitly tagged (`mode: "mock"`, `is_simulated: true`, `warning: ...`) and unknown IDs return `404`.
- **Hardened PayPal Webhook Verification & Reconciliation** — Preserves exact raw request bytes verbatim when delegating signature checks to PayPal's `POST /v1/notifications/verify-webhook-signature` API, deduplicates by both PayPal event ID and `refund_id`, handles `PAYMENT.CAPTURE.PENDING` and `PAYMENT.CAPTURE.REVERSED`, reconciles partial vs full refunds without double-counting, and rejects out-of-order stale status overwrites (`WEBHOOK_STALE_IGNORED`).
- **Disputes & Cases (Secondary)** — Includes dispute tracking, evidence submission, AI dispute recommendations, and case SLA monitoring.

---

## Architecture Overview

```text
PayPal Sandbox API / Verified Webhooks
        │  (raw-byte signature check, event + refund_id dedup, monotonic state guards)
        ▼
Deterministic Status & Evidence Engine (status_engine.py)
        │  authoritative status, partial/full refund math, disputes, unavailable_fields, source_evidence
        ▼
Explanation & Summary Layer (ai_service.py)
        │  Heuristic Grounded (default) or optional OpenAI gpt-4o-mini rewrite
        │  untrusted-data isolation + grounding compliance validator + graceful timeout/429 fallback
        ▼
React Resolution Workspace (:5173) + Support Summaries
```

See [docs/architecture.md](docs/architecture.md) for table schemas, webhook reliability flow, and safety controls.

---

## Setup & Running Locally

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
- **Health Check:** `http://localhost:8000/health`

On first startup, the backend initializes `copilot.db` and seeds 7 transactions (covering `COMPLETED`, `PENDING`, `FAILED`, `DISPUTED`, `REFUNDED`, plus a possible duplicate pair `TXN-2024-001` & `TXN-2024-007`), per-transaction timeline events, 2 disputes, and 2 resolution cases.

### Optional: Enabling PayPal Sandbox & Live Webhooks

1. Add your PayPal Sandbox app credentials to `backend/.env` (`PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_BASE_URL=https://api-m.sandbox.paypal.com`).
2. Expose port `8000` over HTTPS (e.g. `ngrok http 8000`) and register `https://<your-tunnel>/api/webhooks/paypal` in the PayPal Developer Dashboard under the same Sandbox app for `PAYMENT.CAPTURE.COMPLETED`, `PAYMENT.CAPTURE.PENDING`, `PAYMENT.CAPTURE.REFUNDED`, `PAYMENT.CAPTURE.REVERSED`, `CUSTOMER.DISPUTE.CREATED`, `CUSTOMER.DISPUTE.UPDATED`, and `CUSTOMER.DISPUTE.RESOLVED`.
3. Set `PAYPAL_WEBHOOK_ID=<your-webhook-id>` and `PAYPAL_VERIFY_WEBHOOKS=true` in `backend/.env`, then restart `uvicorn`. Never commit `backend/.env` or SQLite `.db` files.

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
| `POST` | `/api/transactions/{id}/refund` | Issue full or partial refund via PayPal API (or tagged mock) & reconcile metadata/timeline |
| `GET` | `/api/transactions/{id}/disputes` | Disputes associated with a transaction |
| `POST` | `/api/paypal/orders` | Create a PayPal Orders v2 checkout order (returns `approve_url`) |
| `POST` | `/api/paypal/orders/{order_id}/capture` | Capture an approved PayPal order & store/update transaction + timeline |
| `POST` | `/api/paypal/sync/{capture_id}` | Sync capture status from PayPal API into local DB |
| `GET` | `/api/paypal/transactions/{transaction_id}` | Direct PayPal capture lookup (or tagged mock lookup) |
| `GET` | `/api/paypal/disputes/{dispute_id}` | Direct PayPal dispute lookup |
| `GET` | `/api/paypal/disputes` | List PayPal disputes via PayPal API (or mock dataset) |
| `GET` | `/api/disputes` | Paginated dispute list (`status`, `reason`) |
| `GET` | `/api/disputes/{id}` | Fetch dispute details |
| `POST` | `/api/disputes` | Record a dispute & timeline event |
| `PUT` | `/api/disputes/{id}/status` | Update dispute status / resolution |
| `POST` | `/api/disputes/{id}/evidence` | Submit dispute evidence |
| `POST` | `/api/disputes/{id}/escalate` | Escalate dispute |
| `POST` | `/api/disputes/{id}/accept` | Accept dispute claim |
| `GET` | `/api/cases` | Paginated resolution cases with SLA metrics |
| `GET` | `/api/cases/{id}` | Fetch case details |
| `POST` | `/api/cases` | Create a resolution case |
| `PUT` | `/api/cases/{id}` | Update case steps / outcome |
| `GET` | `/api/cases/{id}/summary` | Markdown case summary + SLA info |
| `POST` | `/api/ai/analyze` | AI dispute recommendation |
| `POST` | `/api/ai/template` | Generate merchant dispute response template |
| `POST` | `/api/ai/risk` | Transaction risk classification |
| `POST` | `/api/webhooks/paypal` | Receive & verify PayPal webhooks via `/v1/notifications/verify-webhook-signature` |

---

## Testing & Verification Scope

### Running Automated Tests & Production Build

```powershell
# Backend test suite (25 tests: webhooks, grounding, OpenAI adversarial/fallback)
cd d:\paypal-transaction-resolution-copilot\backend
.\venv\Scripts\python.exe -m unittest discover -s tests -t . -v

# Frontend production build
cd d:\paypal-transaction-resolution-copilot\frontend
npm run build
```

### Verification Summary (PayPal Sandbox vs. Mocked Coverage)

- **Verified live against PayPal Sandbox:**
  - OAuth2 authentication, Orders v2 creation & buyer checkout approval, capture storage, full capture refund, and partial capture refund (`remaining_refundable` tracking and over-refund `422` guard).
  - Live webhook delivery via HTTPS tunnel (`PAYMENT.CAPTURE.COMPLETED` and `PAYMENT.CAPTURE.REFUNDED`), signature verification via PayPal's `/v1/notifications/verify-webhook-signature` API with exact raw request bytes, SQLite persistence (`webhook_events` + `transaction_events`), replay deduplication (`"reason": "duplicate"`), and tampered-payload rejection (`401`).
- **Verified in automated tests (`25` passing tests in `backend/tests/`):**
  - `test_webhooks.py`: Raw-body byte preservation, `PAYMENT.CAPTURE.PENDING` & `PAYMENT.CAPTURE.REVERSED`, out-of-order stale webhook preservation, cross-event `refund_id` deduplication, concurrent duplicate delivery safety, retry on transient handler failure, and malformed/unhandled event handling *(uses isolated SQLite DBs and mocked PayPal API responses for local webhook replay/reversal/stale cases)*.
  - `test_heuristic_grounding.py`: All 8 heuristic grounding scenarios (`COMPLETED`, `PENDING`, full refund, partial refund, duplicate webhook, stale webhook, `DISPUTED` status/evidence disclosure, and missing transaction fields).
  - `test_openai_adversarial.py`: Missing/invalid API key fallback, timeout & `429` rate-limit fallback, partial refund figure enforcement, dispute/missing-evidence hallucination guards, prompt-injection resistance, and read-only DB safety *(uses mocked OpenAI completions/errors)*.
- **Heuristic Demo Mode & OpenAI Status:**
  - The hackathon demo runs in **Heuristic Grounded** mode (`mode: "heuristic"`) with zero external LLM API cost. Live OpenAI `429 credit_balance_exhausted` fallback was observed against `api.openai.com` without data loss, while live `mode: "openai"` wording generation has not yet been verified pending API billing credits. Production PayPal credentials and production deployment security still require separate validation outside Sandbox.

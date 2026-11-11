"""
paypal_service.py - PayPal REST API client for Transaction Resolution Copilot.

Implements OAuth2 token caching, transaction/dispute/refund endpoints, and
PayPal's official `/v1/notifications/verify-webhook-signature` API check.
When credentials are absent or placeholder values, the service operates in
explicit mock mode (`mode="mock"`, `is_simulated=True`) and does not fabricate
unverified records for unknown IDs.
"""
import base64
import hashlib
import json
import time
from datetime import datetime
from typing import Optional
import httpx


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class PayPalAPIError(Exception):
    def __init__(self, message: str, status_code: int = 500, details: dict = None):
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(self.message)


# ---------------------------------------------------------------------------
# Explicit Mock Dataset (used only when PayPal credentials are not configured)
# ---------------------------------------------------------------------------

_MOCK_TRANSACTIONS: dict[str, dict] = {
    "TXN-MOCK-001": {
        "id": "TXN-MOCK-001",
        "status": "COMPLETED",
        "amount": {"currency_code": "USD", "value": "250.00"},
        "payer": {"email_address": "buyer@example.com"},
        "payee": {"email_address": "seller@example.com"},
        "description": "Simulated PayPal capture – electronics purchase",
        "create_time": "2024-05-01T10:00:00Z",
        "update_time": "2024-05-01T10:05:00Z",
    },
    "TXN-MOCK-002": {
        "id": "TXN-MOCK-002",
        "status": "DISPUTED",
        "amount": {"currency_code": "USD", "value": "89.99"},
        "payer": {"email_address": "john.doe@example.com"},
        "payee": {"email_address": "merchant@store.com"},
        "description": "Simulated PayPal capture – clothing order",
        "create_time": "2024-04-28T14:22:00Z",
        "update_time": "2024-04-30T09:00:00Z",
    },
}

_MOCK_DISPUTES: dict[str, dict] = {
    "DSP-MOCK-001": {
        "dispute_id": "DSP-MOCK-001",
        "status": "OPEN",
        "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
        "dispute_amount": {"currency_code": "USD", "value": "89.99"},
        "disputed_transactions": [{"buyer_transaction_id": "TXN-MOCK-002"}],
        "create_time": "2024-04-30T09:00:00Z",
        "update_time": "2024-05-01T08:00:00Z",
        "messages": [],
        "evidences": [],
    }
}


# ---------------------------------------------------------------------------
# PayPalService
# ---------------------------------------------------------------------------

class PayPalService:
    """
    Async PayPal REST API client.

    If `client_id` or `client_secret` are empty or placeholder values, the
    service operates in explicit mock mode. Every mock response is labeled with
    `mode="mock"`, `is_simulated=True`, and `data_source="mock_paypal_api"`.
    """

    _PLACEHOLDER_VALUES = {"", "your_paypal_client_id", "your_paypal_client_secret"}

    def __init__(self, client_id: str, client_secret: str, base_url: str):
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.base_url = base_url.rstrip("/") if base_url else "https://api-m.sandbox.paypal.com"
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._mock_mode = (
            self.client_id in self._PLACEHOLDER_VALUES
            or self.client_secret in self._PLACEHOLDER_VALUES
        )

    @property
    def environment_label(self) -> str:
        if self._mock_mode:
            return "mock"
        return "paypal_sandbox" if "sandbox" in self.base_url.lower() else "paypal_live"

    def _tag_response(self, payload: dict, simulated: bool) -> dict:
        tagged = dict(payload)
        tagged["mode"] = "mock" if simulated else self.environment_label
        tagged["is_simulated"] = simulated
        tagged["data_source"] = "mock_paypal_api" if simulated else "paypal_api"
        if simulated:
            tagged["warning"] = "Simulated PayPal response (no live/sandbox PayPal credentials configured)"
        return tagged

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def _get_access_token(self) -> str:
        """Return a valid access token, refreshing if expired or absent."""
        if self._mock_mode:
            return "MOCK_ACCESS_TOKEN"

        now = time.monotonic()
        if self._access_token and now < self._token_expires_at - 60:
            return self._access_token

        credentials = f"{self.client_id}:{self.client_secret}"
        encoded = base64.b64encode(credentials.encode()).decode()

        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{self.base_url}/v1/oauth2/token",
                headers={
                    "Authorization": f"Basic {encoded}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "client_credentials"},
            )

        if response.status_code != 200:
            raise PayPalAPIError(
                "Failed to obtain PayPal access token",
                status_code=response.status_code,
                details=response.json() if response.content else {},
            )

        token_data = response.json()
        self._access_token = token_data["access_token"]
        self._token_expires_at = now + token_data.get("expires_in", 3600)
        return self._access_token

    async def _auth_headers(self) -> dict:
        token = await self._get_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def _get(self, path: str, params: dict = None) -> dict:
        if self._mock_mode:
            raise NotImplementedError("Call the specific mock method instead")
        headers = await self._auth_headers()
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{self.base_url}{path}", headers=headers, params=params)
        if response.status_code >= 400:
            raise PayPalAPIError(
                f"PayPal GET {path} failed",
                status_code=response.status_code,
                details=response.json() if response.content else {},
            )
        return self._tag_response(response.json(), simulated=False)

    async def _post(
        self,
        path: str,
        body: dict = None,
        params: dict = None,
        extra_headers: dict = None,
    ) -> dict:
        if self._mock_mode:
            raise NotImplementedError("Call the specific mock method instead")
        headers = await self._auth_headers()
        if extra_headers:
            headers.update(extra_headers)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.base_url}{path}",
                headers=headers,
                json=body or {},
                params=params,
            )
        if response.status_code >= 400:
            raise PayPalAPIError(
                f"PayPal POST {path} failed",
                status_code=response.status_code,
                details=response.json() if response.content else {},
            )
        raw = response.json() if response.content else {"status": "success"}
        return self._tag_response(raw, simulated=False)

    async def _post_raw_json(
        self,
        path: str,
        raw_json_bytes: bytes,
        extra_headers: dict = None,
    ) -> dict:
        """Send raw UTF-8 JSON bytes directly without dict re-serialization."""
        if self._mock_mode:
            raise NotImplementedError("Call the specific mock method instead")
        headers = await self._auth_headers()
        if extra_headers:
            headers.update(extra_headers)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.base_url}{path}",
                headers=headers,
                content=raw_json_bytes,
            )
        if response.status_code >= 400:
            raise PayPalAPIError(
                f"PayPal POST {path} failed",
                status_code=response.status_code,
                details=response.json() if response.content else {},
            )
        raw = response.json() if response.content else {"status": "success"}
        return self._tag_response(raw, simulated=False)

    # ------------------------------------------------------------------
    # Webhook Signature Verification (Official PayPal API)
    # ------------------------------------------------------------------

    async def verify_webhook_signature(
        self,
        headers: dict,
        raw_body: bytes,
        webhook_id: str,
    ) -> dict:
        """
        Verify a PayPal webhook via PayPal's official
        `POST /v1/notifications/verify-webhook-signature` endpoint while
        preserving the exact raw request JSON bytes verbatim as `webhook_event`.
        """
        normalised = {k.upper(): v for k, v in headers.items()}
        required_headers = [
            "PAYPAL-AUTH-ALGO",
            "PAYPAL-CERT-URL",
            "PAYPAL-TRANSMISSION-ID",
            "PAYPAL-TRANSMISSION-SIG",
            "PAYPAL-TRANSMISSION-TIME",
        ]
        missing = [h for h in required_headers if not normalised.get(h)]
        if missing:
            return {
                "verification_status": "FAILURE",
                "reason": f"Missing required PayPal headers: {', '.join(missing)}",
                "mode": self.environment_label,
                "is_simulated": self._mock_mode,
            }

        if self._mock_mode:
            return {
                "verification_status": "SKIPPED_MOCK_MODE",
                "reason": "PayPal credentials are not configured; cannot call PayPal verify-webhook-signature API.",
                "mode": "mock",
                "is_simulated": True,
            }

        if not isinstance(raw_body, (bytes, bytearray)):
            raise ValueError("raw_body must be bytes to preserve exact webhook payload")

        try:
            raw_text = bytes(raw_body).decode("utf-8")
            parsed_check = json.loads(raw_text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PayPalAPIError("Invalid UTF-8 JSON webhook body", status_code=400) from exc

        if not isinstance(parsed_check, dict):
            raise PayPalAPIError("Webhook body must be a JSON object", status_code=400)

        prefix = json.dumps(
            {
                "auth_algo": normalised["PAYPAL-AUTH-ALGO"],
                "cert_url": normalised["PAYPAL-CERT-URL"],
                "transmission_id": normalised["PAYPAL-TRANSMISSION-ID"],
                "transmission_sig": normalised["PAYPAL-TRANSMISSION-SIG"],
                "transmission_time": normalised["PAYPAL-TRANSMISSION-TIME"],
                "webhook_id": webhook_id,
            }
        )[:-1]
        verify_payload_bytes = f'{prefix}, "webhook_event": {raw_text}}}'.encode("utf-8")

        return await self._post_raw_json(
            "/v1/notifications/verify-webhook-signature",
            raw_json_bytes=verify_payload_bytes,
        )

    # ------------------------------------------------------------------
    # Orders v2 (Create & Capture Sandbox/Live Transactions)
    # ------------------------------------------------------------------

    async def create_order(
        self,
        amount: float,
        currency: str = "USD",
        description: str = "PayPal Transaction Resolution Copilot Order",
        return_url: str = "http://localhost:5173/transactions",
        cancel_url: str = "http://localhost:5173/transactions",
    ) -> dict:
        """Create a PayPal Orders v2 checkout order and return its approval link."""
        if self._mock_mode:
            order_id = f"ORD-MOCK-{hashlib.md5(f'{amount}:{description}:{time.time()}'.encode()).hexdigest()[:10].upper()}"
            return self._tag_response(
                {
                    "id": order_id,
                    "status": "CREATED",
                    "links": [
                        {"rel": "approve", "href": return_url, "method": "GET"}
                    ],
                },
                simulated=True,
            )

        body = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "description": description,
                    "amount": {
                        "currency_code": currency.upper(),
                        "value": f"{amount:.2f}",
                    },
                }
            ],
            "application_context": {
                "return_url": return_url,
                "cancel_url": cancel_url,
                "user_action": "PAY_NOW",
            },
        }
        return await self._post(
            "/v2/checkout/orders",
            body=body,
            extra_headers={"Prefer": "return=representation"},
        )

    async def get_order(self, order_id: str) -> dict:
        """Retrieve an Orders v2 order by ID."""
        if self._mock_mode:
            raise PayPalAPIError("Mock order lookup is not backed by PayPal API", status_code=404)
        return await self._get(f"/v2/checkout/orders/{order_id}")

    async def capture_order(self, order_id: str) -> dict:
        """Capture an approved PayPal Orders v2 order."""
        if self._mock_mode:
            cap_id = f"CAP-MOCK-{hashlib.md5(order_id.encode()).hexdigest()[:10].upper()}"
            return self._tag_response(
                {
                    "id": order_id,
                    "status": "COMPLETED",
                    "payer": {"email_address": "mock.buyer@example.com"},
                    "purchase_units": [
                        {
                            "payments": {
                                "captures": [
                                    {
                                        "id": cap_id,
                                        "status": "COMPLETED",
                                        "amount": {"currency_code": "USD", "value": "19.99"},
                                        "create_time": datetime.utcnow().isoformat() + "Z",
                                        "update_time": datetime.utcnow().isoformat() + "Z",
                                    }
                                ]
                            }
                        }
                    ],
                },
                simulated=True,
            )
        return await self._post(
            f"/v2/checkout/orders/{order_id}/capture",
            body={},
            extra_headers={"Prefer": "return=representation"},
        )

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------

    async def get_transaction(self, transaction_id: str) -> dict:
        """Fetch a single capture/transaction by ID."""
        if self._mock_mode:
            if transaction_id in _MOCK_TRANSACTIONS:
                return self._tag_response(_MOCK_TRANSACTIONS[transaction_id], simulated=True)
            raise PayPalAPIError(
                f"Transaction '{transaction_id}' not found in simulated PayPal mock dataset (available mock IDs: {', '.join(_MOCK_TRANSACTIONS.keys())})",
                status_code=404,
            )
        return await self._get(f"/v2/payments/captures/{transaction_id}")

    # ------------------------------------------------------------------
    # Disputes
    # ------------------------------------------------------------------

    async def get_dispute(self, dispute_id: str) -> dict:
        """Fetch a single dispute by ID."""
        if self._mock_mode:
            if dispute_id in _MOCK_DISPUTES:
                return self._tag_response(_MOCK_DISPUTES[dispute_id], simulated=True)
            raise PayPalAPIError(
                f"Dispute '{dispute_id}' not found in simulated PayPal mock dataset (available mock IDs: {', '.join(_MOCK_DISPUTES.keys())})",
                status_code=404,
            )
        return await self._get(f"/v1/customer/disputes/{dispute_id}")

    async def list_disputes(
        self,
        status: Optional[str] = None,
        start_date: Optional[str] = None,
        page_size: int = 50,
    ) -> dict:
        """List disputes, optionally filtered by status and start date."""
        if self._mock_mode:
            items = list(_MOCK_DISPUTES.values())
            if status:
                items = [d for d in items if d.get("status") == status]
            return self._tag_response(
                {"items": items, "total_items": len(items), "total_pages": 1},
                simulated=True,
            )

        params: dict = {"page_size": page_size}
        if status:
            params["dispute_state"] = status
        if start_date:
            params["start_time"] = start_date
        return await self._get("/v1/customer/disputes", params=params)

    async def submit_evidence(self, dispute_id: str, evidence_data: dict) -> dict:
        """Submit evidence documents for a dispute."""
        if self._mock_mode:
            return self._tag_response(
                {
                    "dispute_id": dispute_id,
                    "status": "UNDER_REVIEW",
                    "message": "[MOCK] Evidence submitted successfully",
                    "submitted_at": datetime.utcnow().isoformat() + "Z",
                },
                simulated=True,
            )
        return await self._post(
            f"/v1/customer/disputes/{dispute_id}/provide-evidence",
            body=evidence_data,
        )

    async def accept_claim(self, dispute_id: str) -> dict:
        """Accept the dispute claim (merchant concedes)."""
        if self._mock_mode:
            return self._tag_response(
                {
                    "dispute_id": dispute_id,
                    "status": "RESOLVED",
                    "outcome": "RESOLVED_BUYER_FAVOUR",
                    "message": "[MOCK] Claim accepted",
                    "resolved_at": datetime.utcnow().isoformat() + "Z",
                },
                simulated=True,
            )
        return await self._post(
            f"/v1/customer/disputes/{dispute_id}/accept-claim",
            body={"note": "Merchant accepts the claim and will issue a refund."},
        )

    async def escalate_dispute(self, dispute_id: str, note: str) -> dict:
        """Escalate a dispute to PayPal for review."""
        if self._mock_mode:
            return self._tag_response(
                {
                    "dispute_id": dispute_id,
                    "status": "UNDER_REVIEW",
                    "message": f"[MOCK] Dispute escalated – note: {note}",
                    "escalated_at": datetime.utcnow().isoformat() + "Z",
                },
                simulated=True,
            )
        return await self._post(
            f"/v1/customer/disputes/{dispute_id}/escalate",
            body={"note": note},
        )

    # ------------------------------------------------------------------
    # Refunds
    # ------------------------------------------------------------------

    async def issue_refund(
        self,
        capture_id: str,
        amount: float,
        currency: str = "USD",
        note_to_payer: str = "Refund issued by merchant",
    ) -> dict:
        """Issue a refund for a captured payment."""
        if self._mock_mode:
            refund_id = f"REF-MOCK-{hashlib.md5(capture_id.encode()).hexdigest()[:8].upper()}"
            return self._tag_response(
                {
                    "id": refund_id,
                    "status": "COMPLETED",
                    "amount": {"currency_code": currency, "value": f"{amount:.2f}"},
                    "seller_payable_breakdown": {
                        "gross_amount": {"currency_code": currency, "value": f"{amount:.2f}"},
                        "paypal_fee": {"currency_code": currency, "value": "0.00"},
                        "net_amount": {"currency_code": currency, "value": f"{amount:.2f}"},
                    },
                    "create_time": datetime.utcnow().isoformat() + "Z",
                    "update_time": datetime.utcnow().isoformat() + "Z",
                    "note": "[MOCK] Refund simulated (no real PayPal API call executed)",
                },
                simulated=True,
            )
        return await self._post(
            f"/v2/payments/captures/{capture_id}/refund",
            body={
                "amount": {"value": f"{amount:.2f}", "currency_code": currency},
                "note_to_payer": note_to_payer,
            },
        )

    async def get_refund(self, refund_id: str) -> dict:
        """Retrieve details of a specific refund."""
        if self._mock_mode:
            if refund_id.startswith("REF-MOCK-"):
                return self._tag_response(
                    {
                        "id": refund_id,
                        "status": "COMPLETED",
                        "amount": {"currency_code": "USD", "value": "0.00"},
                        "create_time": datetime.utcnow().isoformat() + "Z",
                        "update_time": datetime.utcnow().isoformat() + "Z",
                    },
                    simulated=True,
                )
            raise PayPalAPIError(
                f"Refund '{refund_id}' not found in simulated PayPal mock dataset",
                status_code=404,
            )
        return await self._get(f"/v2/payments/refunds/{refund_id}")

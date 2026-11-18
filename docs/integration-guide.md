# DocFlow integration guide: webhooks

DocFlow notifies your system when a submission changes state. This guide covers what you
receive, how to verify it, and what to expect from delivery.

## 1. Register an endpoint

An admin registers your HTTPS endpoint and chooses the events:

```http
POST /webhooks
Authorization: Bearer <admin token>
Content-Type: application/json

{"url": "https://your-system.example.com/docflow-hook",
 "event_types": ["submission.approved", "submission.rejected"]}
```

The response contains a `secret` (`whsec_...`). **It is shown only once.** Store it in your secret
manager. If it leaks, call `POST /webhooks/{id}/rotate-secret` and store the new one.

To check your setup end to end: `POST /webhooks/{id}/test` sends a `webhook.test` event.

Requirements for the URL: `https`, and a host that resolves to a public IP address. Internal
addresses are refused to protect both sides (SSRF).

## 2. Events

| Type | When |
|---|---|
| `submission.approved` | The final approval stage approved the submission |
| `submission.rejected` | A reviewer rejected it (final) |
| `submission.query_raised` | A reviewer asked the submitter about specific fields |
| `submission.extraction_failed` | The document could not be processed |
| `webhook.test` | Sent on request from `POST /webhooks/{id}/test` |

## 3. What a request looks like

```http
POST /docflow-hook HTTP/1.1
Content-Type: application/json
DocFlow-Event-Id: 0b0f6a52-...
DocFlow-Timestamp: 1795000000
DocFlow-Signature: v1=5257a869e7ecebeda32affa62cdca3fa51cad7e77a0e56ff536d0ce8e108d8bd

{"created_at":"2026-11-18T10:00:00+00:00","data":{"stage":null,"status":"approved","submission_id":"..."},"id":"0b0f6a52-...","type":"submission.approved"}
```

The payload only contains identifiers and status, never invoice data. Fetch details with
`GET /submissions/{submission_id}` using your own credentials.

## 4. Verify every request

The signature is `v1=` followed by the hex HMAC-SHA256 of `{timestamp}.{raw body}`, keyed with your
secret. Always:

1. Use the **raw request body bytes**, not re-serialised JSON.
2. Reject timestamps more than 5 minutes from your clock (replay protection).
3. Compare signatures in constant time.

**Python**

```python
import hashlib
import hmac
import time


def verify_docflow(secret: str, timestamp: str, signature: str, body: bytes) -> bool:
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - sent_at) > 300:
        return False
    expected = hmac.new(secret.encode(), f"{sent_at}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"v1={expected}", signature)
```

**Node.js**

```javascript
import crypto from "node:crypto";

export function verifyDocflow(secret, timestamp, signature, rawBody /* Buffer */) {
  const sentAt = Number.parseInt(timestamp, 10);
  if (!Number.isFinite(sentAt) || Math.abs(Date.now() / 1000 - sentAt) > 300) return false;
  const expected =
    "v1=" +
    crypto
      .createHmac("sha256", secret)
      .update(Buffer.concat([Buffer.from(`${sentAt}.`), rawBody]))
      .digest("hex");
  const a = Buffer.from(expected);
  const b = Buffer.from(signature);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
```

In Express, read the raw body with `express.raw({ type: "application/json" })` on this route;
`express.json()` discards the exact bytes the signature covers.

A complete, runnable receiver is in `scripts/webhook_receiver.py`.

## 5. Delivery behaviour

| Topic | Behaviour |
|---|---|
| Success | Any `2xx` response within 10 seconds |
| Redirects | Not followed; a `3xx` counts as a failure |
| Retries | 6 attempts: immediately, then after 1 min, 5 min, 30 min, 2 h and 12 h (about 15 hours) |
| After the last attempt | The delivery is marked `failed`; an admin can redeliver it |
| Duplicates | Possible (delivery is at least once). Use `DocFlow-Event-Id` to ignore repeats |
| Order | Not guaranteed. Use the event's `created_at`, or fetch the current submission state |
| Latency | Usually under 10 seconds after the change |

**Respond quickly.** Acknowledge with `2xx` as soon as the signature is verified and the event is
stored, then process it in your own queue. Slow handlers cause timeouts and retries.

## 6. Troubleshooting

Admins can see every delivery attempt for an endpoint:

```http
GET /webhooks/{id}/deliveries
```

Each entry shows the status, attempt count, last HTTP status code, last error and the next
attempt time. `POST /webhook-deliveries/{delivery_id}/redeliver` sends one again.

# 0005: Webhooks with a transactional outbox (M4)

Date: 2026-11-18. Status: accepted.

## Delivery design: transactional outbox

`webhooks.emit()` writes the event and one pending delivery per subscribed endpoint **in the same
transaction** as the business change (approval, rejection, query, extraction failure). A Celery
beat schedule triggers `dispatch_due` every 10 seconds; the worker sends whatever is due.

- An event exists if and only if its change was committed. No "commit, then enqueue" gap (the
  weakness noted in decision 0002).
- Claims use `SELECT ... FOR UPDATE SKIP LOCKED` plus a 60-second lease in `next_attempt_at`, so
  overlapping dispatch runs never send a delivery twice and a crashed sender's work is retried.
- Cost: up to about 10 seconds of added latency. Acceptable for business events.

## Contract with receivers

- Headers `DocFlow-Event-Id`, `DocFlow-Timestamp`, `DocFlow-Signature: v1=HMAC-SHA256(secret,
  "{timestamp}.{body}")`. Signing the timestamp enables a 5-minute replay window on the receiver.
- At-least-once delivery and no ordering guarantee, stated in the integration guide.
- Thin payloads: identifiers and status only, no invoice values, so a misconfigured or leaked
  endpoint exposes nothing sensitive.
- 2xx within 10 s is success; redirects are not followed; 6 attempts over about 15 hours; then
  `failed`, with manual redelivery.

## Security

- **SSRF:** endpoints must be HTTPS and resolve only to public addresses (no private, loopback,
  link-local such as the cloud metadata service, CGNAT, multicast or IPv4-mapped private), checked
  at registration and again before every send.
- **DNS rebinding (closed in M6):** deliveries now connect to an address the check approved,
  with the hostname sent as SNI and `Host`, so the HTTP client never resolves the name a second
  time. An egress proxy (e.g. Smokescreen) remains a defence-in-depth option.
- `DOCFLOW_WEBHOOK_ALLOW_PRIVATE_TARGETS=true` disables these checks for the local mock receiver
  only. It is set in docker-compose and must never be set in production.
- **Signing secrets are encrypted with AWS KMS** (M6, `secret_cipher.py`, encryption context
  bound to their purpose). They are shown once, rotatable, and never returned by list endpoints.

## Known limitations

- A delivery whose processing raises an unexpected exception (a bug, not an HTTP failure) is
  rolled back and retried when its lease expires, without counting the attempt. It is logged
  each time; a poison delivery would retry until fixed or until an admin intervenes.
- Rotating a secret does not re-sign deliveries already in flight; they are signed at send time,
  so pending retries use the new secret.

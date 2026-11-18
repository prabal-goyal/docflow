"""Reference webhook receiver for DocFlow. Standard library only, so it can be copied as is.

    WEBHOOK_SECRET=whsec_... python webhook_receiver.py        # listens on :9000

POST /hook      receives DocFlow events: verifies the signature and timestamp, ignores duplicates
GET  /received  lists what was received (for local testing)

Without WEBHOOK_SECRET it stores requests unverified, so a test can check signatures itself.
In production, verification must never be optional.
"""

import hashlib
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOLERANCE_SECONDS = 300
SECRET = os.environ.get("WEBHOOK_SECRET", "")
PORT = int(os.environ.get("PORT", "9000"))

received: list[dict[str, object]] = []
seen_event_ids: set[str] = set()


def verify_signature(secret: str, timestamp: str, signature: str, body: bytes) -> bool:
    """What every DocFlow receiver must do before trusting a request."""
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - sent_at) > TOLERANCE_SECONDS:  # stale: possible replay
        return False
    expected = hmac.new(secret.encode(), f"{sent_at}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"v1={expected}", signature)  # constant-time comparison


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/hook":
            self.send_response(404)
            self.end_headers()
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        event_id = self.headers.get("DocFlow-Event-Id", "")
        timestamp = self.headers.get("DocFlow-Timestamp", "")
        signature = self.headers.get("DocFlow-Signature", "")

        if SECRET and not verify_signature(SECRET, timestamp, signature, body):
            self.send_response(401)
            self.end_headers()
            return
        if event_id in seen_event_ids:
            # Delivery is at least once: acknowledge duplicates without processing them again.
            self.send_response(200)
            self.end_headers()
            return
        seen_event_ids.add(event_id)
        received.append(
            {
                "event_id": event_id,
                "timestamp": timestamp,
                "signature": signature,
                "verified": bool(SECRET),
                "body": body.decode(),
            }
        )
        # Acknowledge fast; real work belongs in a queue on the receiver's side.
        self.send_response(204)
        self.end_headers()

    def do_GET(self) -> None:
        if self.path != "/received":
            self.send_response(404)
            self.end_headers()
            return
        payload = json.dumps(received).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    print(f"Webhook receiver on :{PORT} (verifying: {bool(SECRET)})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

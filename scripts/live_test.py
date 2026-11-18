"""Live extraction test against a running stack WITH a real OPENAI_API_KEY. Costs real money.

    uv run python scripts/live_test.py

Run scripts/smoke_test.sh against the same stack first (it creates the users this script uses).
Uploads every samples/*.pdf, waits for extraction, and compares the result with
samples/*.expected.json: field values, flags, tokens, cost and latency.
Exits non-zero if any sample's flags differ from the expected ones.
"""

import json
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

BASE_URL = "http://localhost:8000"
SAMPLES = Path(__file__).resolve().parent.parent / "samples"
SUBMITTER = ("smoke-submitter@example.com", "smoke-submitter-pass-1")
ADMIN = ("smoke-admin@example.com", "smoke-admin-password-1")
TIMEOUT_SECONDS = 240


def request(method: str, path: str, token: str | None = None, **kwargs: Any) -> Any:
    headers = dict(kwargs.pop("headers", {}))
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE_URL + path, method=method, headers=headers, **kwargs)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read() or b"null")


def login(email: str, password: str) -> str:
    form = urllib.parse.urlencode({"username": email, "password": password}).encode()
    return str(request("POST", "/auth/login", data=form)["access_token"])


def upload(token: str, pdf: Path) -> str:
    boundary = uuid.uuid4().hex
    disposition = f'form-data; name="file"; filename="{pdf.name}"'
    head = f"--{boundary}\r\nContent-Disposition: {disposition}\r\n"
    head += "Content-Type: application/pdf\r\n\r\n"
    body = head.encode() + pdf.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    return str(request("POST", "/submissions", token, data=body, headers=headers)["id"])


def wait_for(token: str, submission_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        submission: dict[str, Any] = request("GET", f"/submissions/{submission_id}", token)
        if submission["status"] not in ("uploaded", "extracting"):
            return submission
        time.sleep(2)
    raise TimeoutError(f"Submission {submission_id} still processing after {TIMEOUT_SECONDS}s")


def flatten(fields: dict[str, Any], values_only: bool) -> dict[str, Any]:
    """{'line_items[0].amount': '1.00', ...} from either the API's fields or expected.json."""
    flat: dict[str, Any] = {}
    for name, item in fields.items():
        if name == "line_items":
            for index, line in enumerate(item or []):
                for key, value in line.items():
                    flat[f"line_items[{index}].{key}"] = value if values_only else value["value"]
        else:
            flat[name] = item if values_only else item["value"]
    return flat


def main() -> int:
    token, admin_token = login(*SUBMITTER), login(*ADMIN)
    all_ok = True
    for pdf in sorted(SAMPLES.glob("*.pdf")):
        expected = json.loads(pdf.with_suffix(".expected.json").read_text(encoding="utf-8"))
        submission = wait_for(token, upload(token, pdf))
        attempts = request("GET", f"/submissions/{submission['id']}/attempts", admin_token)

        print(f"\n=== {pdf.name}: {submission['status']}")
        if submission["status"] != "in_review":
            print(f"    failure: {submission['failure_reason']}")
            all_ok = False
            continue

        want = flatten(expected["fields"], values_only=True)
        got = flatten(submission["fields"], values_only=False)
        wrong = {k: (want[k], got.get(k)) for k in want if want[k] != got.get(k)}
        print(f"    fields correct: {len(want) - len(wrong)}/{len(want)}")
        for key, (exp, act) in wrong.items():
            print(f"      {key}: expected {exp!r}, got {act!r}")

        expected_flags = {(f["field"], f["code"]) for f in expected["expected_flags"]}
        actual_flags = {(f["field"], f["code"]) for f in submission["flags"]}
        flags_ok = expected_flags == actual_flags
        all_ok &= flags_ok
        print(f"    flags {'OK' if flags_ok else 'MISMATCH'}: {sorted(actual_flags) or 'none'}")
        if not flags_ok:
            print(f"      missing:    {sorted(expected_flags - actual_flags)}")
            print(f"      unexpected: {sorted(actual_flags - expected_flags)}")
            for flag in submission["flags"]:
                print(f"      - {flag['field']}: {flag['message']}")

        for a in attempts:
            print(
                f"    attempt: {a['model']} effort={a['reasoning_effort']} {a['outcome']} "
                f"tokens in={a['input_tokens']} out={a['output_tokens']} "
                f"(reasoning {a['reasoning_tokens']}) cost=${a['estimated_cost_usd']} "
                f"latency={a['latency_ms']}ms"
            )

    print("\nRESULT:", "all samples matched the expected flags" if all_ok else "MISMATCHES found")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

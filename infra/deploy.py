#!/usr/bin/env python3
"""Deploy the Muse Relay worker (code + KV binding, no zone route).

Usage: deploy.py [worker-file]
Uploads dist/worker.js via PUT /accounts/{id}/workers/scripts/{name}
(multipart, ES modules format) with the RELAY_KV binding in metadata.
The worker is served on its workers.dev subdomain; a custom domain can be
attached later via a zone route.
"""
import io
import json
import os
import sys
import urllib.request
import urllib.error
import uuid

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
from dynamic_credentials import (
    add_surrogate_to_request,
    read_json_response,
)

API = "https://api.cloudflare.com/client/v4"
CREDENTIAL = "custom.cloudflare"
ALLOWED = ["api.cloudflare.com"]

ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "46422d2eae695c4be7e639c9f7f65fbe")
SCRIPT_NAME = os.environ.get("CF_WORKER_NAME", "muse-relay")
KV_NAMESPACE_ID = os.environ.get("CF_KV_NAMESPACE_ID", "4ba22e0af88b46c4971a6f9953289b27")


def authed(url, data=None, method="GET", content_type=None):
    req = urllib.request.Request(url, data=data, method=method)
    if content_type:
        req.add_header("Content-Type", content_type)
    add_surrogate_to_request(
        req, CREDENTIAL, entry_name="access_token", allowed_hosts=ALLOWED
    )
    return req


def call(req):
    try:
        with urllib.request.urlopen(req) as resp:
            return read_json_response(resp), None
    except urllib.error.HTTPError as e:
        try:
            return None, read_json_response(e)
        except Exception:
            return None, {"http_status": e.code}


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    worker_file = sys.argv[1] if len(sys.argv) > 1 else os.path.join(root, "dist", "worker.js")
    with open(worker_file, "r", encoding="utf-8") as f:
        script = f.read()

    boundary = "----cfdeploy" + uuid.uuid4().hex
    metadata = {
        "main_module": "worker.js",
        "compatibility_date": "2025-01-01",
        "bindings": [
            {"type": "kv_namespace", "name": "RELAY_KV",
             "namespace_id": KV_NAMESPACE_ID}
        ],
    }
    buf = io.BytesIO()

    def part(headers, body: bytes):
        buf.write(("--" + boundary + "\r\n").encode())
        for k, v in headers:
            buf.write((k + ": " + v + "\r\n").encode())
        buf.write(b"\r\n")
        buf.write(body)
        buf.write(b"\r\n")

    part([("Content-Disposition", 'form-data; name="metadata"'),
          ("Content-Type", "application/json")],
         json.dumps(metadata).encode())
    part([("Content-Disposition", 'form-data; name="worker.js"; filename="worker.js"'),
          ("Content-Type", "application/javascript+module")],
         script.encode())
    buf.write(("--" + boundary + "--\r\n").encode())

    put_url = f"{API}/accounts/{ACCOUNT_ID}/workers/scripts/{SCRIPT_NAME}"
    payload, err = call(authed(put_url, data=buf.getvalue(), method="PUT",
                               content_type="multipart/form-data; boundary=" + boundary))
    if err or not (payload and payload.get("success")):
        print(json.dumps(err or payload, indent=2), file=sys.stderr)
        print("worker upload FAILED", file=sys.stderr)
        return 1
    print(f"worker '{SCRIPT_NAME}' uploaded OK (KV binding: RELAY_KV)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Build Muse Relay.

1. esbuild bundles: client JS (relay.js, qr.js, space.js) and src/worker.js.
2. public/* assets are inlined into the worker bundle as a JSON map,
   replacing the __ASSETS_JSON__ placeholder.
3. Output: dist/worker.js — a single self-contained ES module, ready to upload.
"""
import base64
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "public")
DIST = os.path.join(ROOT, "dist")
os.makedirs(DIST, exist_ok=True)

TYPES = {
    ".html": "text/html",
    ".css": "text/css",
    ".js": "application/javascript",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain",
    ".xml": "application/xml",
}


def run(cmd, **kw):
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True, **kw)


# Extensions served as raw binary (base64-encoded into the bundle).
BINARY_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".ico"}


def bundle(entry, outfile, extra=()):
    run(["npx", "esbuild", entry, "--bundle", "--format=esm",
         "--platform=browser", "--target=es2022",
         f"--outfile={outfile}", *extra])


def main():
    # Client bundles (served as static assets, imported by the pages).
    bundle("client/api.js", "public/relay.js")
    bundle("client/qr.js", "public/qr.js")
    bundle("client/space.js", "public/space.js",
           ["--external:./relay.js", "--external:./qr.js"])
    # Worker bundle (noble ed25519 for server-side signature verification).
    bundle("src/worker.js", "dist/worker-bundle.js")

    assets = {}
    for name in sorted(os.listdir(PUBLIC)):
        path = os.path.join(PUBLIC, name)
        if not os.path.isfile(path):
            continue
        ext = os.path.splitext(name)[1]
        with open(path, "rb") as f:
            raw = f.read()
        if ext in BINARY_EXTS:
            assets["/" + name] = {
                "type": TYPES.get(ext, "application/octet-stream"),
                "encoding": "base64",
                "body": base64.b64encode(raw).decode("ascii"),
            }
        else:
            assets["/" + name] = {
                "type": TYPES.get(ext, "text/plain"),
                "body": raw.decode("utf-8"),
            }
    assets["/"] = assets["/index.html"]
    assets["/index.html"] = assets["/index.html"]

    # Plausible analytics: injected at build time from a snippet that lives
    # OUTSIDE this repo (~/workspace/muse-relay-analytics/plausible.html),
    # so the public GitHub tree never carries the tracking code. The repo
    # keeps only the <!--PLAUSIBLE--> placeholder comment.
    snippet_path = os.path.expanduser(
        "~/workspace/muse-relay-analytics/plausible.html")
    try:
        with open(snippet_path, encoding="utf-8") as f:
            snippet = f.read().strip()
    except FileNotFoundError:
        snippet = ""
    if snippet:
        for key in ("/", "/index.html", "/app.html"):
            a = assets.get(key)
            if a and a.get("encoding") != "base64":
                a["body"] = a["body"].replace("<!--PLAUSIBLE-->", snippet, 1)
        print("injected Plausible snippet into /, /index.html, /app.html")
    else:
        print("no Plausible snippet found; shipping without analytics")

    with open(os.path.join(DIST, "worker-bundle.js"), encoding="utf-8") as f:
        bundle_src = f.read()
    placeholder = "__ASSETS_JSON__"
    assert placeholder in bundle_src, "placeholder missing from worker bundle"
    bundle_src = bundle_src.replace(placeholder, json.dumps(assets))

    out = os.path.join(DIST, "worker.js")
    with open(out, "w", encoding="utf-8") as f:
        f.write(bundle_src)
    print(f"wrote {out} ({os.path.getsize(out)//1024} KB)")


if __name__ == "__main__":
    sys.exit(main())

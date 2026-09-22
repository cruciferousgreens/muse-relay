#!/usr/bin/env python3
"""Muse Relay agent helper.

End-to-end encrypted messaging between AI agents. The server is a dumb
pipe: it never sees keys, code words, or message plaintext.

Usage:
    relay.py create --name "My assistant"
    relay.py join-code "maple otter ... grove" --name "My assistant"
    relay.py join-link "https://.../s/<id>#k=<key>" --name "My assistant"
    relay.py send <space-id> "Tell my friend's assistant we're on for Friday"
    relay.py fetch <space-id>            # decrypt + verify, append to audit log
    relay.py members <space-id>
    relay.py delete <space-id>           # needs the 8-word code

Set the relay server with --base or the MUSE_RELAY_BASE environment variable.

Keys and code words are stored in ~/.config/muse-relay/ with mode 0600.
Every fetched message is appended (decrypted) to a local audit log:
    ~/.config/muse-relay/audit/<space-id>.log   (JSON lines)
"""
import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)
from cryptography.exceptions import InvalidSignature

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tools"))
from words import WORDS  # noqa: E402

DEFAULT_BASE = os.environ.get("MUSE_RELAY_BASE", "")
CONFIG_DIR = os.path.expanduser("~/.config/muse-relay")
SPACES_DIR = os.path.join(CONFIG_DIR, "spaces")
AUDIT_DIR = os.path.join(CONFIG_DIR, "audit")
SIGN_CTX = b"muse-relay-msg-v1"


def _sig_input(space_id: str, nonce_b64: str, ct_b64: str) -> bytes:
    # Must match client/crypto.js sigInput and the worker exactly:
    # domain || raw space-id bytes || raw nonce || raw ciphertext.
    return (SIGN_CTX + b64d(space_id) + b64d(nonce_b64) + b64d(ct_b64))


CERT_CTX = b"muse-relay-member-v1"


def _cert_input(space_id: str, pub_b64: str, display_name: str) -> bytes:
    # Must match client/crypto.js memberCertInput and the worker exactly.
    return (CERT_CTX + b64d(space_id) + b64d(pub_b64)
            + display_name.strip().encode())


def gen_control_keypair():
    seed = secrets.token_bytes(32)
    pub = Ed25519PrivateKey.from_private_bytes(seed).public_key()
    return b64e(seed), b64e(pub.public_bytes_raw())


def control_pub_from_seed(seed_b64: str) -> str:
    """Derive the Ed25519 control public key from a control seed.

    This is the pinning primitive: a client that unwraps the control seed
    can compute the control public key itself and must never trust a
    server-supplied control_pub that differs from it. A server that swaps
    in a different control_pub (for which it holds the seed) could mint
    rogue member certificates that a trusting client would accept."""
    pub = Ed25519PrivateKey.from_private_bytes(b64d(seed_b64)).public_key()
    return b64e(pub.public_bytes_raw())


def wrap_control_seed(key: bytes, seed_b64: str) -> str:
    pt = json.dumps({"v": 1, "kind": "control-seed", "seed": seed_b64}).encode()
    nonce = secrets.token_bytes(12)
    return b64e(nonce + AESGCM(key).encrypt(nonce, pt, None))


def unwrap_control_seed(key: bytes, wrapped_b64: str) -> str:
    blob = b64d(wrapped_b64)
    m = json.loads(AESGCM(key).decrypt(blob[:12], blob[12:], None))
    if m.get("v") != 1 or m.get("kind") != "control-seed" \
            or not isinstance(m.get("seed"), str):
        raise ValueError("bad control seed")
    return m["seed"]


def sign_member_cert(seed_b64: str, space_id: str, pub_b64: str,
                     display_name: str) -> str:
    seed = Ed25519PrivateKey.from_private_bytes(b64d(seed_b64))
    return b64e(seed.sign(_cert_input(space_id, pub_b64, display_name)))


def verify_member_cert(control_pub_b64: str, space_id: str, pub_b64: str,
                       display_name: str, cert_b64: str) -> bool:
    try:
        pub = Ed25519PublicKey.from_public_bytes(b64d(control_pub_b64))
        pub.verify(b64d(cert_b64), _cert_input(space_id, pub_b64, display_name))
        return True
    except (InvalidSignature, ValueError):
        return False


# ---------------------------------------------------------------- crypto

def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def gen_code_words(n: int = 8):
    raw = secrets.token_bytes((n * 11 + 7) // 8)
    val = int.from_bytes(raw, "big")
    return [WORDS[(val >> (11 * i)) & 0x7FF] for i in range(n - 1, -1, -1)]


def derive_lookup(words) -> str:
    # b64url( SHA256("muse-relay-v1:lookup:" + code_words)[0:12] ) — 16 chars.
    h = hashlib.sha256(b"muse-relay-v1:lookup:" + " ".join(words).encode()).digest()
    return b64e(h[:12])


def derive_kek(words, lookup_b64: str) -> bytes:
    # PBKDF2-HMAC-SHA256(pw=code_words, salt="muse-relay-v1:wrap:"+lookup, 600k, 32)
    return hashlib.pbkdf2_hmac(
        "sha256", " ".join(words).encode(),
        b"muse-relay-v1:wrap:" + lookup_b64.encode(), 600_000, 32)


def wrap_space_key(kek: bytes, space_key: bytes) -> str:
    nonce = secrets.token_bytes(12)
    ct = AESGCM(kek).encrypt(nonce, space_key, None)
    return b64e(nonce + ct)


def unwrap_space_key(kek: bytes, wrapped_b64: str) -> bytes:
    blob = b64d(wrapped_b64)
    return AESGCM(kek).decrypt(blob[:12], blob[12:], None)


def encrypt_message(key: bytes, author: str, body: str):
    pt = json.dumps(
        {"v": 1, "author": author, "ts": int(time.time() * 1000), "body": body}
    ).encode()
    nonce = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(nonce, pt, None)
    return b64e(nonce), b64e(ct)


def decrypt_message(key: bytes, nonce_b64: str, ct_b64: str):
    pt = AESGCM(key).decrypt(b64d(nonce_b64), b64d(ct_b64), None)
    return json.loads(pt.decode())


def _b36(n: int) -> str:
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    for _ in range(3):
        out = chars[n % 36] + out
        n //= 36
    return out


def gen_member_keypair():
    priv = Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes_raw()
    pub_raw = priv.public_key().public_bytes_raw()
    return b64e(pub_raw), b64e(priv_raw)


def sign_message(priv_b64: str, space_id: str, nonce_b64: str, ct_b64: str) -> str:
    priv = Ed25519PrivateKey.from_private_bytes(b64d(priv_b64))
    return b64e(priv.sign(_sig_input(space_id, nonce_b64, ct_b64)))


def verify_message(pub_b64: str, space_id: str, nonce_b64: str, ct_b64: str,
                   sig_b64: str) -> bool:
    try:
        pub = Ed25519PublicKey.from_public_bytes(b64d(pub_b64))
        pub.verify(b64d(sig_b64), _sig_input(space_id, nonce_b64, ct_b64))
        return True
    except (InvalidSignature, ValueError):
        return False


# ---------------------------------------------------------------- storage

def _write_private(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _space_path(space_id: str) -> str:
    return os.path.join(SPACES_DIR, space_id + ".json")


def save_space(s: dict):
    _write_private(_space_path(s["space_id"]), s)


def load_space(space_id: str) -> dict:
    with open(_space_path(space_id), encoding="utf-8") as f:
        return json.load(f)


def audit(space_id: str, entries):
    """Append decrypted messages to the per-space audit log (JSON lines)."""
    path = os.path.join(AUDIT_DIR, space_id + ".log")
    os.makedirs(AUDIT_DIR, exist_ok=True)
    seen = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    seen.add(json.loads(line)["id"])
                except (KeyError, ValueError):
                    pass
    new = [e for e in entries if e["id"] not in seen]
    if not new:
        return 0
    with open(path, "a", encoding="utf-8") as f:
        for e in new:
            f.write(json.dumps({
                "id": e["id"], "ts": e["ts"], "pubkey": e["pubkey"],
                "sig_ok": e["sig_ok"], "decrypt_ok": e["decrypt_ok"],
                "author": e.get("author"), "body": e.get("body"),
            }) + "\n")
    os.chmod(path, 0o600)
    return len(new)


# ---------------------------------------------------------------- API

class Relay:
    def __init__(self, base: str = DEFAULT_BASE):
        self.base = base.rstrip("/")

    def _req(self, method: str, path: str, body=None, params=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "content-type": "application/json",
            # Cloudflare blocks the default "Python-urllib/..." UA (error 1010).
            "user-agent": "MuseRelay/1.0 (+https://github.com/cruciferousgreens/muse-relay)",
        }
        # Cloudflare's bot mitigation sometimes RSTs rapid successive POSTs
        # from non-browser TLS fingerprints; back off and retry.
        last = None
        for attempt in range(4):
            req = urllib.request.Request(url, data=data, method=method,
                                         headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                try:
                    err = json.load(e)
                except Exception:
                    err = {"error": f"http {e.code}"}
                if e.code in (429, 500, 502, 503) and attempt < 3:
                    last = RuntimeError(err.get("error", f"http {e.code}"))
                else:
                    raise RuntimeError(err.get("error", f"http {e.code}")) from None
            except (ConnectionError, TimeoutError, OSError) as e:
                last = e
            if attempt < 3:
                time.sleep(2 ** attempt)
        raise RuntimeError(f"request failed after retries: {last}")

    def create(self, display_name: str) -> dict:
        space_id = b64e(secrets.token_bytes(16))
        space_key = secrets.token_bytes(32)
        words = gen_code_words(8)
        lookup = derive_lookup(words)
        kek = derive_kek(words, lookup)
        wrapped = wrap_space_key(kek, space_key)
        ctrl_seed_b64, ctrl_pub_b64 = gen_control_keypair()
        wrapped_control = wrap_control_seed(space_key, ctrl_seed_b64)
        pub_b64, priv_b64 = gen_member_keypair()
        cert = sign_member_cert(ctrl_seed_b64, space_id, pub_b64, display_name)
        try:
            self._req("POST", "/api/spaces", {
                "space_id": space_id, "lookup": lookup, "wrapped": wrapped,
                "wrapped_control": wrapped_control, "control_pub": ctrl_pub_b64,
                "display_name": display_name, "pubkey": pub_b64, "cert": cert,
            })
        except RuntimeError as e:
            # Our own retried POST can land after the original succeeded
            # (response lost to a RST). Same id + same body means the stored
            # space is exactly what we sent — treat it as created.
            if "space exists" not in str(e):
                raise
        s = {
            "space_id": space_id, "key_b64": b64e(space_key),
            "code": " ".join(words),
            "link": f"{self.base}/s/{space_id}#k={b64e(space_key)}",
            "member": {"pub_b64": pub_b64, "priv_b64": priv_b64},
            "display_name": display_name,
            "control_pub": ctrl_pub_b64,  # pinned: derived from the seed we minted
        }
        save_space(s)
        return s

    def _register(self, space_id: str, ctrl_seed_b64: str,
                  display_name: str) -> dict:
        pub_b64, priv_b64 = gen_member_keypair()
        cert = sign_member_cert(ctrl_seed_b64, space_id, pub_b64, display_name)
        self._req("POST", f"/api/spaces/{space_id}/members",
                  {"display_name": display_name, "pubkey": pub_b64,
                   "cert": cert})
        return {"pub_b64": pub_b64, "priv_b64": priv_b64}

    def join_code(self, code: str, display_name: str) -> dict:
        words = code.strip().lower().split()
        if len(words) != 8:
            raise ValueError("code must be 8 words")
        lookup = derive_lookup(words)
        data = self._req("POST", "/api/spaces/join", {"lookup": lookup})
        kek = derive_kek(words, lookup)
        space_key = unwrap_space_key(kek, data["wrapped"])
        ctrl_seed = unwrap_control_seed(space_key, data["wrapped_control"])
        # Pin the control key: it must equal the server's claimed control_pub.
        # If the server substituted a different control_pub, abort — its
        # rogue member certificates must never be trusted.
        ctrl_pub = control_pub_from_seed(ctrl_seed)
        if data.get("control_pub") != ctrl_pub:
            raise RuntimeError("control key mismatch: server's control_pub does "
                               "not match the unwrapped control seed")
        member = self._register(data["space_id"], ctrl_seed, display_name)
        s = {
            "space_id": data["space_id"], "key_b64": b64e(space_key),
            "code": " ".join(words),
            "link": f"{self.base}/s/{data['space_id']}#k={b64e(space_key)}",
            "member": member,
            "display_name": display_name,
            "control_pub": ctrl_pub,
        }
        save_space(s)
        return s

    def join_link(self, link: str, display_name: str) -> dict:
        if "#k=" not in link:
            raise ValueError("not a Muse Relay link (missing #k= fragment)")
        head, key_b64 = link.split("#k=", 1)
        space_id = head.rstrip("/").rsplit("/s/", 1)[-1]
        if len(space_id) != 22:
            raise ValueError("not a Muse Relay link (bad space id)")
        space_key = b64d(key_b64)
        ctrl = self._req("GET", f"/api/spaces/{space_id}/control")
        ctrl_seed = unwrap_control_seed(space_key, ctrl["wrapped_control"])
        ctrl_pub = control_pub_from_seed(ctrl_seed)
        if ctrl.get("control_pub") != ctrl_pub:
            raise RuntimeError("control key mismatch: server's control_pub does "
                               "not match the unwrapped control seed")
        member = self._register(space_id, ctrl_seed, display_name)
        s = {
            "space_id": space_id, "key_b64": key_b64, "code": None,
            "link": link,
            "member": member,
            "display_name": display_name,
            "control_pub": ctrl_pub,
        }
        save_space(s)
        return s

    def members(self, space_id: str):
        # Roster certificates are verified against the PINNED control key from
        # local state — never blindly against a fresh server-supplied
        # control_pub, which a tampering server could swap to get rogue
        # member certificates trusted.
        s = load_space(space_id)
        ctrl_pub = s.get("control_pub")
        if not ctrl_pub:
            # Trust-on-first-use migration for states saved before pinning
            # existed: pin the server's current key going forward.
            ctrl_pub = self._req(
                "GET", f"/api/spaces/{space_id}/control")["control_pub"]
            s["control_pub"] = ctrl_pub
            save_space(s)
        data = self._req("GET", f"/api/spaces/{space_id}/members")["members"]
        for m in data:
            m["cert_ok"] = verify_member_cert(
                ctrl_pub, space_id, m["pubkey"], m["display_name"], m["cert"])
        return data

    def send(self, space_id: str, body: str):
        s = load_space(space_id)
        key = b64d(s["key_b64"])
        nonce, ct = encrypt_message(key, s["display_name"], body)
        sig = sign_message(s["member"]["priv_b64"], space_id, nonce, ct)
        # Client-generated message id: _req retries resend the same body, and
        # the server dedupes on the id instead of double-posting.
        msg_id = (f"{int(time.time() * 1000):015d}-"
                  f"{_b36(secrets.randbelow(46656))}")
        return self._req("POST", f"/api/spaces/{space_id}/messages", {
            "id": msg_id, "ciphertext": ct, "nonce": nonce,
            "pubkey": s["member"]["pub_b64"], "signature": sig,
        })

    def fetch(self, space_id: str, since: str = "", limit: int = 50,
              audit_log: bool = True):
        s = load_space(space_id)
        key = b64d(s["key_b64"])
        data = self._req("GET", f"/api/spaces/{space_id}/messages",
                         params={"since": since, "limit": limit})
        out = []
        for m in data["messages"]:
            sig_ok = verify_message(m["pubkey"], space_id, m["nonce"],
                                    m["ciphertext"], m["signature"])
            decrypt_ok, author, body = True, None, None
            try:
                pt = decrypt_message(key, m["nonce"], m["ciphertext"])
                author, body = pt.get("author"), pt.get("body")
            except Exception:
                decrypt_ok = False
            out.append({**m, "sig_ok": sig_ok, "decrypt_ok": decrypt_ok,
                        "author": author, "body": body,
                        "mine": m["pubkey"] == s["member"]["pub_b64"]})
        if audit_log and out:
            audit(space_id, out)
        return out

    def delete(self, space_id: str):
        s = load_space(space_id)
        if not s.get("code"):
            raise ValueError("need the 8-word code to delete (link joins can't delete)")
        words = s["code"].split()
        lookup = derive_lookup(words)
        try:
            return self._req("DELETE", f"/api/spaces/{space_id}", {"lookup": lookup})
        except RuntimeError as e:
            # A retried DELETE can land after the original succeeded (response
            # lost). "unknown space" then means the goal state — gone.
            if "unknown space" not in str(e):
                raise
            return {"ok": True, "already_gone": True}


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="relay.py", description="Muse Relay agent helper")
    ap.add_argument("--base", default=DEFAULT_BASE)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create"); p.add_argument("--name", required=True)
    p = sub.add_parser("join-code"); p.add_argument("code"); p.add_argument("--name", required=True)
    p = sub.add_parser("join-link"); p.add_argument("link"); p.add_argument("--name", required=True)
    p = sub.add_parser("send"); p.add_argument("space_id"); p.add_argument("body")
    p = sub.add_parser("fetch"); p.add_argument("space_id"); p.add_argument("--since", default="")
    p = sub.add_parser("members"); p.add_argument("space_id")
    p = sub.add_parser("delete"); p.add_argument("space_id")

    a = ap.parse_args(argv)
    if not a.base:
        print("error: set the relay server with --base or MUSE_RELAY_BASE",
              file=sys.stderr)
        return 2
    r = Relay(a.base)
    try:
        if a.cmd == "create":
            s = r.create(a.name)
            print(f"space:  {s['space_id']}")
            print(f"link:   {s['link']}")
            print(f"code:   {s['code']}")
        elif a.cmd == "join-code":
            s = r.join_code(a.code, a.name)
            print(f"joined: {s['space_id']}")
        elif a.cmd == "join-link":
            s = r.join_link(a.link, a.name)
            print(f"joined: {s['space_id']}")
        elif a.cmd == "send":
            m = r.send(a.space_id, a.body)
            print(f"sent:   {m['id']}")
        elif a.cmd == "fetch":
            msgs = r.fetch(a.space_id, since=a.since)
            for m in msgs:
                tag = "✓" if m["sig_ok"] and m["decrypt_ok"] else "!"
                print(f"[{tag}] {m['author']}: {m['body']}")
            if not msgs:
                print("(no new messages)")
        elif a.cmd == "members":
            for m in r.members(a.space_id):
                flag = {True: "✓", False: "!", None: "?"}[m["cert_ok"]]
                print(f"[{flag}] {m['display_name']}")
        elif a.cmd == "delete":
            r.delete(a.space_id)
            print("deleted")
    except (RuntimeError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

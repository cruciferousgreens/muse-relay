#!/usr/bin/env python3
"""Security tests for Muse Relay.

Covers the server-side registration/message-signature checks and the
client-side control-key pinning (a tampering server must not be able to
swap the control key and get rogue member certificates trusted).

Server-side tests hit the live worker; each test creates its own space
and deletes it afterwards. Requires MUSE_RELAY_BASE.

    MUSE_RELAY_BASE=https://<worker-host> python3 tests/test_security.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# Isolate local state: never touch the real ~/.config/muse-relay.
_tmp_home = tempfile.mkdtemp(prefix="mr-sectest-")
os.environ["HOME"] = _tmp_home

import relay
from relay import (Relay, b64e, b64d, gen_control_keypair,
                   control_pub_from_seed, sign_member_cert,
                   verify_member_cert, gen_member_keypair, sign_message,
                   encrypt_message, unwrap_control_seed)

BASE = os.environ.get("MUSE_RELAY_BASE", "")
if not BASE:
    sys.exit("error: set MUSE_RELAY_BASE to the worker origin under test")

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'ok' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))


def wait_visible(r, space_id, timeout=60):
    """KV writes can take seconds to reach the edge PoP our route hits;
    poll until the space is visible before asserting on it."""
    import time
    end = time.time() + timeout
    while time.time() < end:
        try:
            r._req("GET", f"/api/spaces/{space_id}/control")
            return True
        except RuntimeError:
            time.sleep(2)
    return False


def make_space(r, name="Sec Tester"):
    s = r.create(name)
    assert wait_visible(r, s["space_id"]), "space never became visible"
    return s


def expect_reject(name, fn):
    """fn should raise RuntimeError (HTTP 4xx from the worker)."""
    try:
        fn()
    except RuntimeError as e:
        check(name, True)
        return
    except Exception as e:  # noqa: BLE001
        check(name, False, f"wrong exception: {type(e).__name__}: {e}")
        return
    check(name, False, "request was accepted")


# ---------------------------------------------------------------- server-side

def test_certless_registration_rejected():
    r = Relay(BASE)
    s = make_space(r)
    try:
        pub, _ = gen_member_keypair()
        expect_reject("register without certificate rejected", lambda: r._req(
            "POST", f"/api/spaces/{s['space_id']}/members",
            {"display_name": "Mallory", "pubkey": pub}))
    finally:
        r.delete(s["space_id"])


def test_self_signed_cert_rejected():
    r = Relay(BASE)
    s = make_space(r)
    try:
        rogue_seed, _ = gen_control_keypair()
        pub, _ = gen_member_keypair()
        cert = sign_member_cert(rogue_seed, s["space_id"], pub, "Mallory")
        expect_reject("self-signed certificate rejected", lambda: r._req(
            "POST", f"/api/spaces/{s['space_id']}/members",
            {"display_name": "Mallory", "pubkey": pub, "cert": cert}))
    finally:
        r.delete(s["space_id"])


def test_cross_space_cert_replay_rejected():
    r = Relay(BASE)
    a = make_space(r)
    b = make_space(r)
    try:
        # Mint a cert valid for space A (we know A's seed: we created it, so
        # cheat locally with the same primitive a real second client would
        # not have — the point is the server must still reject it for B).
        pub, _ = gen_member_keypair()
        # We don't have A's seed client-side after create; reconstruct the
        # scenario differently: join B by code to get B's seed, then mint a
        # cert for A's space id with B's seed and try to register it on A.
        jb = r.join_code(b["code"], "Sec Tester B")
        seed_b = None
        # join_code does not return the seed; re-derive via the join endpoint.
        from relay import derive_lookup, derive_kek, unwrap_space_key
        words = b["code"].split()
        lookup = derive_lookup(words)
        data = r._req("POST", "/api/spaces/join", {"lookup": lookup})
        kek = derive_kek(words, lookup)
        space_key = unwrap_space_key(kek, data["wrapped"])
        seed_b = unwrap_control_seed(space_key, data["wrapped_control"])
        cert_for_a = sign_member_cert(seed_b, a["space_id"], pub, "Mallory")
        expect_reject("cross-space certificate replay rejected", lambda: r._req(
            "POST", f"/api/spaces/{a['space_id']}/members",
            {"display_name": "Mallory", "pubkey": pub, "cert": cert_for_a}))
    finally:
        r.delete(a["space_id"])
        r.delete(b["space_id"])


def test_display_name_tampering_rejected():
    r = Relay(BASE)
    s = make_space(r)
    try:
        from relay import derive_lookup, derive_kek, unwrap_space_key
        words = s["code"].split()
        lookup = derive_lookup(words)
        data = r._req("POST", "/api/spaces/join", {"lookup": lookup})
        kek = derive_kek(words, lookup)
        space_key = unwrap_space_key(kek, data["wrapped"])
        seed = unwrap_control_seed(space_key, data["wrapped_control"])
        pub, _ = gen_member_keypair()
        cert = sign_member_cert(seed, s["space_id"], pub, "Mallory")
        # Cert binds "Mallory" but registration claims "Eve".
        expect_reject("display-name/certificate tampering rejected", lambda: r._req(
            "POST", f"/api/spaces/{s['space_id']}/members",
            {"display_name": "Eve", "pubkey": pub, "cert": cert}))
    finally:
        r.delete(s["space_id"])


def test_corrupted_message_signature_rejected():
    r = Relay(BASE)
    s = make_space(r)
    try:
        from relay import b64d as _b64d
        key = _b64d(s["key_b64"])
        nonce, ct = encrypt_message(key, "Sec Tester", "hello")
        member = s["member"]
        sig = sign_message(member["priv_b64"], s["space_id"], nonce, ct)
        bad = sig[:-2] + ("AA" if not sig.endswith("AA") else "BB")
        expect_reject("corrupted message signature rejected", lambda: r._req(
            "POST", f"/api/spaces/{s['space_id']}/messages",
            {"ciphertext": ct, "nonce": nonce,
             "pubkey": member["pub_b64"], "signature": bad}))
    finally:
        r.delete(s["space_id"])


# ---------------------------------------------------------------- client-side pinning

class _FakeRelay(Relay):
    """Relay with scripted server responses (no network)."""

    def __init__(self, script):
        super().__init__("https://fake.invalid")
        self._script = script

    def _req(self, method, path, body=None, params=None):
        key = (method, path)
        if key not in self._script:
            raise AssertionError(f"unexpected request {method} {path}")
        resp = self._script[key]
        if isinstance(resp, Exception):
            raise resp
        return resp


def _make_space_blobs(display_name="Pin Tester"):
    """Local fixtures: space id/key/code/seed as create() would mint."""
    import secrets
    from relay import (gen_code_words, derive_lookup, derive_kek,
                       wrap_space_key, wrap_control_seed)
    space_id = b64e(secrets.token_bytes(16))
    space_key = secrets.token_bytes(32)
    words = gen_code_words(8)
    lookup = derive_lookup(words)
    kek = derive_kek(words, lookup)
    wrapped = wrap_space_key(kek, space_key)
    seed_b64, pub_b64 = gen_control_keypair()
    wrapped_control = wrap_control_seed(space_key, seed_b64)
    return {"space_id": space_id, "key_b64": b64e(space_key),
            "code": " ".join(words), "wrapped": wrapped,
            "wrapped_control": wrapped_control,
            "seed_b64": seed_b64, "pub_b64": pub_b64}


def test_control_pub_substitution_aborts_join():
    fx = _make_space_blobs()
    rogue_seed, rogue_pub = gen_control_keypair()
    assert rogue_pub != fx["pub_b64"]
    join_resp = {"space_id": fx["space_id"], "wrapped": fx["wrapped"],
                 "wrapped_control": fx["wrapped_control"],
                 # Tampering server swaps in ITS control key:
                 "control_pub": rogue_pub}
    r = _FakeRelay({
        ("POST", "/api/spaces/join"): join_resp,
        ("POST", f"/api/spaces/{fx['space_id']}/members"): {"ok": True},
    })
    try:
        r.join_code(fx["code"], "Pin Tester")
    except RuntimeError as e:
        check("control-pub substitution aborts join",
              "control key mismatch" in str(e))
        return
    check("control-pub substitution aborts join", False,
          "join succeeded despite mismatched control_pub")


def test_members_uses_pinned_control_key():
    fx = _make_space_blobs()
    # Simulate a saved state from an honest join (pinned = real control key).
    state = {"space_id": fx["space_id"], "key_b64": fx["key_b64"],
             "control_pub": fx["pub_b64"],
             "member": {"pub_b64": "x", "priv_b64": "y"},
             "display_name": "Pin Tester"}
    relay.save_space(state)

    legit_pub, _ = gen_member_keypair()
    legit_cert = sign_member_cert(fx["seed_b64"], fx["space_id"],
                                  legit_pub, "Legit")
    rogue_seed, rogue_pub = gen_control_keypair()
    rogue_member_pub, _ = gen_member_keypair()
    rogue_cert = sign_member_cert(rogue_seed, fx["space_id"],
                                  rogue_member_pub, "Mallory")
    r = _FakeRelay({
        # Tampering server serves ITS control key and a rogue roster entry:
        ("GET", f"/api/spaces/{fx['space_id']}/members"): {"members": [
            {"pubkey": legit_pub, "display_name": "Legit", "cert": legit_cert},
            {"pubkey": rogue_member_pub, "display_name": "Mallory",
             "cert": rogue_cert},
        ]},
    })
    members = r.members(fx["space_id"])
    by_name = {m["display_name"]: m["cert_ok"] for m in members}
    check("members() verifies against pinned key (not server's)",
          by_name.get("Legit") is True and by_name.get("Mallory") is False,
          f"got {by_name}")


def test_control_pub_derives_from_seed():
    seed_b64, pub_b64 = gen_control_keypair()
    check("control_pub derives from seed",
          control_pub_from_seed(seed_b64) == pub_b64)


def test_message_retry_does_not_duplicate():
    import time
    from relay import b64d as _b64d, _b36
    import secrets as _secrets
    r = Relay(BASE)
    s = make_space(r)
    try:
        key = _b64d(s["key_b64"])
        nonce, ct = encrypt_message(key, "Sec Tester", "idempotency probe")
        member = s["member"]
        sig = sign_message(member["priv_b64"], s["space_id"], nonce, ct)
        msg_id = f"{int(time.time() * 1000):015d}-{_b36(_secrets.randbelow(46656))}"
        body = {"id": msg_id, "ciphertext": ct, "nonce": nonce,
                "pubkey": member["pub_b64"], "signature": sig}
        first = r._req("POST", f"/api/spaces/{s['space_id']}/messages", body)
        second = r._req("POST", f"/api/spaces/{s['space_id']}/messages", body)
        msgs = r._req("GET", f"/api/spaces/{s['space_id']}/messages",
                      params={"limit": 100})["messages"]
        ids = [m["id"] for m in msgs]
        check("retried message POST dedupes on client id",
              first["id"] == msg_id and second.get("duplicate") is True
              and ids.count(msg_id) == 1,
              f"first={first} second={second} ids={ids}")
    finally:
        r.delete(s["space_id"])


def main():
    test_control_pub_derives_from_seed()
    test_control_pub_substitution_aborts_join()
    test_members_uses_pinned_control_key()
    test_message_retry_does_not_duplicate()
    test_certless_registration_rejected()
    test_self_signed_cert_rejected()
    test_cross_space_cert_replay_rejected()
    test_display_name_tampering_rejected()
    test_corrupted_message_signature_rejected()
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

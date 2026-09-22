/* Muse Relay worker — an E2E-encrypted dumb pipe for agent-to-agent messaging.
 *
 * What the server stores: opaque space IDs, SHA-256 fingerprints of pairing
 * codes (lookup only), AES-256-GCM wrapped space keys it cannot open,
 * ciphertext message blobs, nonces, Ed25519 public keys and signatures,
 * timestamps. It NEVER sees plaintext, space keys, or code words.
 *
 * Clients (relay.py, the web UI) do all crypto: key generation, PBKDF2 wrap/
 * unwrap, AES-GCM encrypt/decrypt, Ed25519 sign/verify. The server verifies
 * message signatures against registered member keys so non-members (and a
 * rogue operator who doesn't rewrite this code) cannot inject messages.
 */

import { ed25519 } from "@noble/curves/ed25519.js";

/* Inlined by build.py: { "/": {type, body}, "/s/app": ..., ... } */
const ASSETS = __ASSETS_JSON__;

const SPACE_TTL = 90 * 86400; // idle spaces die after 90 days
const MSG_TTL = 30 * 86400; // messages die after 30 days
const SIG_DOMAIN = "muse-relay-msg-v1";
const CERT_DOMAIN = "muse-relay-member-v1";
const MAX_MEMBERS = 50;
const MAX_CIPHERTEXT_B64 = 44000; // ~32KB plaintext

const RE_B64URL = /^[A-Za-z0-9_-]+$/;

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function b64urlToBytes(s) {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  const pad = s.length % 4;
  if (pad) s += "=".repeat(4 - pad);
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function concat(...arrays) {
  const total = arrays.reduce((n, a) => n + a.length, 0);
  const out = new Uint8Array(total);
  let o = 0;
  for (const a of arrays) {
    out.set(a, o);
    o += a.length;
  }
  return out;
}

function sigInput(spaceIdB64, nonceB64, ciphertextB64) {
  return concat(
    new TextEncoder().encode(SIG_DOMAIN),
    b64urlToBytes(spaceIdB64), // 16 bytes
    b64urlToBytes(nonceB64), // 12 bytes
    b64urlToBytes(ciphertextB64)
  );
}

/* Rate limiting with amortized KV writes. The original limiter did a KV read +
 * write on EVERY request, and the free tier caps KV writes at 1,000/day — a
 * busy relay day burned 50% of the daily quota on writes alone.
 * This version reads the per-minute counter on every request (reads have 100×
 * the headroom at 100k/day) but only writes it back with probability
 * 1/sample, where sample scales with the endpoint's limit so the counter
 * holds ~10 ticks at the threshold on every endpoint. Blocking on the tick
 * count keeps the throttle effective globally while costing ~1/sample of the
 * writes. Low-limit endpoints (create: 10/min) sample at 1, i.e. count
 * exactly — a fixed 1/10 sample made them flaky, blocking legitimate users
 * after just a couple of requests. (A Cache-API counter was tried first, but
 * it is per-edge-PoP — useless when a client's traffic sprays PoPs, as this
 * VM's does.) Approximate, but this is an abuse backstop; message signature
 * verification is the real security boundary. */
const RATE_TICKS = 10;
async function rateLimited(env, ip, endpoint, maxPerMin) {
  const sample = Math.max(1, Math.round(maxPerMin / RATE_TICKS));
  const key = `limit:${ip}:${endpoint}`;
  const cur = parseInt((await env.RELAY_KV.get(key)) || "0", 10);
  if (cur >= RATE_TICKS) return true;
  if (Math.random() < 1 / sample) {
    await env.RELAY_KV.put(key, String(cur + 1), { expirationTtl: 60 });
  }
  return false;
}

function clientIp(req) {
  return (
    req.headers.get("cf-connecting-ip") ||
    req.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ||
    "unknown"
  );
}

async function readJson(req, maxBytes = 65536) {
  const text = await req.text();
  if (text.length > maxBytes) throw new Error("body too large");
  return JSON.parse(text);
}

function validSpaceId(s) {
  return typeof s === "string" && s.length === 22 && RE_B64URL.test(s);
}
function validLookup(s) {
  return typeof s === "string" && s.length === 16 && RE_B64URL.test(s);
}
function validPubkey(s) {
  return typeof s === "string" && s.length === 43 && RE_B64URL.test(s);
}

function validControlPub(s) {
  return typeof s === "string" && s.length === 43 && RE_B64URL.test(s);
}

function validCert(s) {
  return typeof s === "string" && s.length === 86 && RE_B64URL.test(s);
}

// KV list() returns at most 1000 keys per call. Loop on the cursor until
// list_complete so a full prefix listing never silently drops records.
async function listAllKeys(env, prefix) {
  const names = [];
  let cursor;
  for (;;) {
    const page = await env.RELAY_KV.list({
      prefix,
      limit: 1000,
      ...(cursor ? { cursor } : {}),
    });
    for (const k of page.keys) names.push(k.name);
    if (page.list_complete) break;
    cursor = page.cursor;
  }
  return names;
}

/** Must match client/crypto.js memberCertInput exactly. */
function memberCertInput(spaceIdB64, pubB64, displayName) {
  return concat(
    new TextEncoder().encode(CERT_DOMAIN),
    b64urlToBytes(spaceIdB64), // 16 bytes
    b64urlToBytes(pubB64), // 32 bytes
    new TextEncoder().encode(displayName.trim())
  );
}

function verifyMemberCert(controlPubB64, spaceIdB64, pubB64, displayName, certB64) {
  try {
    return ed25519.verify(
      b64urlToBytes(certB64),
      memberCertInput(spaceIdB64, pubB64, displayName),
      b64urlToBytes(controlPubB64)
    );
  } catch {
    return false;
  }
}
function validName(s) {
  return typeof s === "string" && s.trim().length >= 1 && s.trim().length <= 40;
}

async function getSpace(env, spaceId) {
  const raw = await env.RELAY_KV.get(`space:${spaceId}`);
  return raw ? JSON.parse(raw) : null;
}

async function putSpace(env, spaceId, space) {
  await env.RELAY_KV.put(`space:${spaceId}`, JSON.stringify(space), {
    expirationTtl: SPACE_TTL,
  });
}

async function handleCreate(env, req, ip) {
  if (await rateLimited(env, ip, "create", 10))
    return json({ error: "rate limited, try again shortly" }, 429);
  let b;
  try {
    b = await readJson(req);
  } catch {
    return json({ error: "invalid JSON" }, 400);
  }
  const { space_id, lookup, wrapped, wrapped_control, control_pub,
          display_name, pubkey, cert } = b;
  if (!validSpaceId(space_id)) return json({ error: "bad space_id" }, 400);
  if (!validLookup(lookup)) return json({ error: "bad lookup" }, 400);
  if (typeof wrapped !== "string" || wrapped.length > 200 || !RE_B64URL.test(wrapped))
    return json({ error: "bad wrapped key" }, 400);
  if (typeof wrapped_control !== "string" || wrapped_control.length > 300 ||
      !RE_B64URL.test(wrapped_control))
    return json({ error: "bad wrapped control" }, 400);
  if (!validControlPub(control_pub)) return json({ error: "bad control_pub" }, 400);
  if (!validName(display_name)) return json({ error: "bad display_name" }, 400);
  if (!validPubkey(pubkey)) return json({ error: "bad pubkey" }, 400);
  if (!validCert(cert)) return json({ error: "bad cert" }, 400);
  if (!verifyMemberCert(control_pub, space_id, pubkey, display_name, cert))
    return json({ error: "bad member cert" }, 403);
  if (await getSpace(env, space_id)) return json({ error: "space exists" }, 409);
  if (await env.RELAY_KV.get(`codelookup:${lookup}`))
    return json({ error: "lookup taken" }, 409);

  const now = Date.now();
  const space = {
    space_id,
    lookup,
    wrapped,
    wrapped_control,
    control_pub,
    created_at: now,
    members: [{
      display_name: display_name.trim(), pubkey, cert, joined_at: now,
    }],
  };
  await putSpace(env, space_id, space);
  await env.RELAY_KV.put(`codelookup:${lookup}`, space_id, {
    expirationTtl: SPACE_TTL,
  });
  return json({ ok: true, space_id });
}

async function handleJoin(env, req, ip) {
  if (await rateLimited(env, ip, "join", 20))
    return json({ error: "rate limited, try again shortly" }, 429);
  let b;
  try {
    b = await readJson(req);
  } catch {
    return json({ error: "invalid JSON" }, 400);
  }
  if (!validLookup(b.lookup)) return json({ error: "bad lookup" }, 400);
  const spaceId = await env.RELAY_KV.get(`codelookup:${b.lookup}`);
  if (!spaceId) return json({ error: "unknown code" }, 404);
  const space = await getSpace(env, spaceId);
  if (!space) return json({ error: "unknown code" }, 404);
  // Refresh idle TTL on successful join (proves the space is alive).
  await putSpace(env, spaceId, space);
  await env.RELAY_KV.put(`codelookup:${b.lookup}`, spaceId, {
    expirationTtl: SPACE_TTL,
  });
  return json({
    space_id: space.space_id,
    wrapped: space.wrapped,
    wrapped_control: space.wrapped_control,
    control_pub: space.control_pub,
    members: space.members,
    created_at: space.created_at,
  });
}

async function handleAddMember(env, req, ip, spaceId) {
  if (await rateLimited(env, ip, "members", 20))
    return json({ error: "rate limited, try again shortly" }, 429);
  const space = await getSpace(env, spaceId);
  if (!space) return json({ error: "unknown space" }, 404);
  let b;
  try {
    b = await readJson(req);
  } catch {
    return json({ error: "invalid JSON" }, 400);
  }
  const { display_name, pubkey, cert } = b;
  if (!validName(display_name)) return json({ error: "bad display_name" }, 400);
  if (!validPubkey(pubkey)) return json({ error: "bad pubkey" }, 400);
  if (space.members.some((m) => m.pubkey === pubkey))
    return json({ ok: true, members: space.members });
  // Membership requires a certificate minted under the space control key.
  // Only key holders can produce one; the server cannot forge members.
  if (!validCert(cert)) return json({ error: "bad cert" }, 400);
  if (!verifyMemberCert(space.control_pub, spaceId, pubkey, display_name, cert))
    return json({ error: "bad member cert" }, 403);
  if (space.members.length >= MAX_MEMBERS)
    return json({ error: "space is full" }, 403);
  space.members.push({
    display_name: display_name.trim(),
    pubkey,
    cert,
    joined_at: Date.now(),
  });
  await putSpace(env, spaceId, space);
  return json({ ok: true, members: space.members });
}

/** Public control material: the wrapped seed is opaque without the space key,
 *  and the control public key lets clients verify the member roster. */
async function handleGetControl(env, spaceId) {
  const space = await getSpace(env, spaceId);
  if (!space) return json({ error: "unknown space" }, 404);
  return json({
    wrapped_control: space.wrapped_control,
    control_pub: space.control_pub,
  });
}

async function handlePostMessage(env, req, ip, spaceId) {
  if (await rateLimited(env, ip, "messages", 60))
    return json({ error: "rate limited, try again shortly" }, 429);
  const space = await getSpace(env, spaceId);
  if (!space) return json({ error: "unknown space" }, 404);
  let b;
  try {
    b = await readJson(req);
  } catch {
    return json({ error: "invalid JSON" }, 400);
  }
  const { ciphertext, nonce, pubkey, signature, id: clientId } = b;
  if (
    typeof ciphertext !== "string" ||
    ciphertext.length < 10 ||
    ciphertext.length > MAX_CIPHERTEXT_B64 ||
    !RE_B64URL.test(ciphertext)
  )
    return json({ error: "bad ciphertext" }, 400);
  if (typeof nonce !== "string" || nonce.length !== 16 || !RE_B64URL.test(nonce))
    return json({ error: "bad nonce" }, 400);
  if (!validPubkey(pubkey)) return json({ error: "bad pubkey" }, 400);
  if (
    typeof signature !== "string" ||
    signature.length !== 86 ||
    !RE_B64URL.test(signature)
  )
    return json({ error: "bad signature" }, 400);
  if (!space.members.some((m) => m.pubkey === pubkey))
    return json({ error: "unknown member key" }, 403);

  let sigOk = false;
  try {
    sigOk = ed25519.verify(
      b64urlToBytes(signature),
      sigInput(spaceId, nonce, ciphertext),
      b64urlToBytes(pubkey)
    );
  } catch {
    sigOk = false;
  }
  if (!sigOk) return json({ error: "bad signature" }, 403);

  // Idempotent sends: the client generates the message id, so a retried
  // POST (lost response, aggressive retry) dedupes instead of double-posting.
  // The id embeds a millisecond timestamp and must match the server format.
  let id = null;
  if (typeof clientId === "string" && /^\d{15}-[0-9a-z]{3}$/.test(clientId)) {
    const existing = await env.RELAY_KV.get(`msg:${spaceId}:${clientId}`);
    if (existing) {
      const prev = JSON.parse(existing);
      return json({ ok: true, id: clientId, ts: prev.ts, duplicate: true });
    }
    id = clientId;
  }
  const ts = Date.now();
  if (!id) {
    const rand = Math.floor(Math.random() * 46656)
      .toString(36)
      .padStart(3, "0");
    id = `${String(ts).padStart(15, "0")}-${rand}`;
  }
  const msg = { id, ts, ciphertext, nonce, pubkey, signature };
  await env.RELAY_KV.put(`msg:${spaceId}:${id}`, JSON.stringify(msg), {
    expirationTtl: MSG_TTL,
  });
  await putSpace(env, spaceId, space); // refresh idle TTL
  return json({ ok: true, id, ts });
}

async function handleGetMessages(env, url, spaceId) {
  const space = await getSpace(env, spaceId);
  if (!space) return json({ error: "unknown space" }, 404);
  const since = url.searchParams.get("since") || "";
  const limit = Math.min(
    parseInt(url.searchParams.get("limit") || "50", 10) || 50,
    100
  );
  // Keys are zero-padded timestamps, so lexicographic order is chronological.
  const keys = await listAllKeys(env, `msg:${spaceId}:`);
  const ids = keys
    .map((k) => k.slice(`msg:${spaceId}:`.length))
    .filter((id) => id > since)
    .sort()
    .slice(-limit);
  const msgs = (
    await Promise.all(
      ids.map(async (id) => {
        const raw = await env.RELAY_KV.get(`msg:${spaceId}:${id}`);
        return raw ? JSON.parse(raw) : null;
      })
    )
  ).filter(Boolean);
  return json({ messages: msgs });
}

async function handleGetMembers(env, spaceId) {
  const space = await getSpace(env, spaceId);
  if (!space) return json({ error: "unknown space" }, 404);
  return json({ members: space.members });
}

async function handleDelete(env, req, spaceId) {
  const space = await getSpace(env, spaceId);
  if (!space) return json({ error: "unknown space" }, 404);
  let b;
  try {
    b = await readJson(req);
  } catch {
    return json({ error: "invalid JSON" }, 400);
  }
  // Deleting requires the code-derived lookup: link holders alone cannot nuke
  // a space, only someone holding the pairing code.
  if (!validLookup(b.lookup) || b.lookup !== space.lookup)
    return json({ error: "code required" }, 403);
  // Deletion removes every message, not just the first KV page.
  const keys = await listAllKeys(env, `msg:${spaceId}:`);
  await Promise.all(keys.map((k) => env.RELAY_KV.delete(k)));
  await env.RELAY_KV.delete(`space:${spaceId}`);
  await env.RELAY_KV.delete(`codelookup:${space.lookup}`);
  return json({ ok: true });
}

function serveAsset(path) {
  const a = ASSETS[path];
  if (!a) return null;
  if (a.encoding === "base64") {
    // Binary asset (e.g. PNG card): decode from the inlined base64.
    const bin = Uint8Array.from(atob(a.body), (c) => c.charCodeAt(0));
    return new Response(bin, {
      headers: {
        "content-type": a.type,
        "cache-control": "public, max-age=3600",
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
      },
    });
  }
  return new Response(a.body, {
    headers: {
      "content-type": a.type + "; charset=utf-8",
      "cache-control": "public, max-age=300",
      "x-content-type-options": "nosniff",
      "referrer-policy": "no-referrer",
    },
  });
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const path = url.pathname;
    const ip = clientIp(req);

    try {
      if (path === "/api/spaces" && req.method === "POST")
        return await handleCreate(env, req, ip);
      if (path === "/api/spaces/join" && req.method === "POST")
        return await handleJoin(env, req, ip);

      const m = path.match(/^\/api\/spaces\/([A-Za-z0-9_-]{22})\/(members|messages|control)$/);
      if (m) {
        const [, spaceId, what] = m;
        if (what === "members" && req.method === "POST")
          return await handleAddMember(env, req, ip, spaceId);
        if (what === "members" && req.method === "GET")
          return await handleGetMembers(env, spaceId);
        if (what === "control" && req.method === "GET")
          return await handleGetControl(env, spaceId);
        if (what === "messages" && req.method === "POST")
          return await handlePostMessage(env, req, ip, spaceId);
        if (what === "messages" && req.method === "GET")
          return await handleGetMessages(env, url, spaceId);
      }
      const d = path.match(/^\/api\/spaces\/([A-Za-z0-9_-]{22})$/);
      if (d && req.method === "DELETE") return await handleDelete(env, req, d[1]);

      if (path === "/" || path === "/index.html") return serveAsset("/") || json({ error: "not found" }, 404);
      if (path === "/s" || path.startsWith("/s/")) {
        // /s/<space_id> — the space UI reads the key from the URL fragment.
        const r = serveAsset("/app.html");
        if (r) return r;
      }
      const asset = serveAsset(path);
      if (asset) return asset;
      return json({ error: "not found" }, 404);
    } catch (e) {
      return json({ error: "internal error" }, 500);
    }
  },
};

/* Muse Relay space UI. All crypto runs in this page; the server only
 * ever sees ciphertext, key fingerprints, and signatures. */
import {
  parseRelayLink, joinWithKey, codeLookup, registerWithSeed,
  sendMessage, fetchMessages, deleteSpace,
  verifyRoster, agentSetupPrompt, copyText,
} from "./relay.js";
import { QRCode } from "./qr.js";

const $ = (id) => document.getElementById(id);
const BASE = location.origin;
const spaceId = (location.pathname.match(/^\/s\/([A-Za-z0-9_-]{22})/) || [])[1];

const store = {
  load() {
    try { return JSON.parse(localStorage.getItem("mr:" + spaceId) || "null"); }
    catch { return null; }
  },
  // Never throws: a failed save must not look like a failed join (the member
  // is already registered server-side by then; throwing would invite a retry
  // that registers a duplicate member).
  save(s) {
    try { localStorage.setItem("mr:" + spaceId, JSON.stringify(s)); }
    catch { /* private mode etc. — the link still works for this session */ }
  },
};

let S = null; // {spaceId, keyB64, member, displayName, code, members, lastId, link, seen}
let pollTimer = null;

function esc(s) {
  return s.replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function timeAgo(ts) {
  const d = Date.now() - ts;
  if (d < 60000) return "just now";
  if (d < 3600000) return Math.floor(d / 60000) + "m ago";
  if (d < 86400000) return Math.floor(d / 3600000) + "h ago";
  return new Date(ts).toLocaleDateString();
}

async function apiGet(path) {
  const res = await fetch(`${BASE}/api/spaces/${spaceId}${path}`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "request failed");
  return data;
}

/** The join calls already registered our member keypair — never register
 *  twice. This just builds session state from a join result. */
async function initFromJoin(r, displayName, code) {
  store.save({ member: r.member, displayName, code: code || null,
    keyB64: r.keyB64, controlPubB64: r.controlPubB64 });
  S = {
    spaceId, keyB64: r.keyB64, member: r.member, displayName,
    code: code || null,
    members: verifyRoster(r.spaceId, r.controlPubB64, r.members),
    controlPubB64: r.controlPubB64,
    lastId: "", link: r.link, seen: new Set(),
  };
  // Put the key in the URL fragment so a reload restores the session even
  // if localStorage is unavailable. Fragments never reach the server.
  history.replaceState(null, "", `/s/${spaceId}#k=${r.keyB64}`);
  enterThread();
}

/** Returning device that already has a member keypair: no re-registration,
 *  just pull the roster and verify it against the PINNED control key from
 *  local state — never against a fresh server-supplied control_pub, which a
 *  tampering server could swap to get rogue member certificates trusted. */
async function initSaved(keyB64) {
  const saved = store.load();
  let pinned = saved.controlPubB64;
  const [mres, cres] = await Promise.all([
    apiGet("/members"), apiGet("/control"),
  ]);
  if (!pinned) {
    // Trust-on-first-use migration for sessions saved before control-key
    // pinning existed: pin the server's current key, then verify against it
    // from here on. (No production sessions predate pinning.)
    pinned = cres.control_pub;
    saved.controlPubB64 = pinned;
    store.save(saved);
  } else if (cres.control_pub && cres.control_pub !== pinned) {
    throw new Error("control key mismatch — the server's control key changed; refusing to trust this roster");
  }
  S = {
    spaceId, keyB64, member: saved.member, displayName: saved.displayName,
    code: saved.code || null,
    members: verifyRoster(spaceId, pinned, mres.members),
    controlPubB64: pinned,
    lastId: "", link: `${BASE}/s/${spaceId}#k=${keyB64}`, seen: new Set(),
  };
  enterThread();
}

function renderMembers() {
  const names = new Map(S.members.map((m) => [m.pubkey, m.display_name]));
  $("members").innerHTML = S.members.map((m) =>
    `<span class="member"><span class="dot"></span>${esc(m.display_name)}` +
    (m.certOk === false ? ` <span class="badge warn">unverified</span>` : ``) +
    `</span>`).join("");
  return names;
}

function renderMessage(m, names, mine) {
  const who = m.plaintext ? esc(m.plaintext.author) : "unknown";
  const body = m.plaintext ? esc(m.plaintext.body) : "<i>could not decrypt</i>";
  const badge = m.sigOk
    ? `<span class="badge ok">verified</span>`
    : `<span class="badge warn">unverified</span>`;
  const el = document.createElement("div");
  el.className = "msg" + (mine ? " mine" : "");
  el.innerHTML = `<div class="meta"><span class="who">${who}</span>${badge}<span class="when">${timeAgo(m.ts)}</span></div><div class="body">${body}</div>`;
  return el;
}

async function poll(throwOnErr = false) {
  if (!S) return;
  try {
    const msgs = await fetchMessages(BASE, S.spaceId, S.keyB64, S.lastId, 50);
    const names = renderMembers();
    const thread = $("thread");
    for (const m of msgs) {
      if (S.seen.has(m.id)) continue;
      S.seen.add(m.id);
      const mine = m.pubkey === S.member.pubB64;
      thread.appendChild(renderMessage(m, names, mine));
      if (m.id > S.lastId) S.lastId = m.id;
    }
    if (msgs.length) thread.lastChild.scrollIntoView({ block: "nearest" });
  } catch (e) {
    // The interval poll stays quiet on transient failures; the next one retries.
    if (throwOnErr) throw e;
  }
}

function enterThread() {
  $("join-view").hidden = true;
  $("thread-view").hidden = false;
  $("p-who").textContent = S.displayName;
  $("space-sub").textContent = `${S.members.length} member${S.members.length === 1 ? "" : "s"} · encrypted`;
  $("sh-link").textContent = S.link;
  $("sh-copy-link").onclick = () => navigator.clipboard.writeText(S.link);
  // The primary action in the space: a setup prompt for the user's OWN AI
  // assistant to join THIS space. Embeds the 8-word code when this device
  // joined with the code, otherwise the full link.
  $("onboard-ai").onclick = async () => {
    const secret = S.code ? { code: S.code } : { link: S.link };
    await copyText(agentSetupPrompt({ ...secret, origin: BASE }));
    const toast = $("onboard-toast");
    toast.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { toast.hidden = true; }, 2600);
  };
  const qr = new QRCode({ content: S.link, width: 200, height: 200 });
  $("sh-qr").innerHTML = "";
  $("sh-qr").appendChild(qr.svg());
  if (S.code) {
    $("sh-code-wrap").hidden = false;
    $("sh-code").textContent = S.code;
    $("sh-copy-code").onclick = () => navigator.clipboard.writeText(S.code);
    $("danger-zone").hidden = false;
  } else {
    // This device doesn't hold the pairing code: keep the delete control hidden.
    $("danger-zone").hidden = true;
  }
  renderMembers();
  if (pollTimer) clearInterval(pollTimer);
  poll();
  pollTimer = setInterval(() => poll(false), 15000);
}

/** This browser already has a member for this space: re-enter the thread
 *  with the existing keypair instead of registering a duplicate. */
async function reuseSaved(keyB64) {
  await initSaved(keyB64);
}

/** Simple one-time display-name join form. The space itself (not this page)
 *  is the destination: after joining, the prominent "Onboard your Muse"
 *  button at the top of the thread handles agent setup from there. */
function showJoinForm(note) {
  $("join-view").hidden = false;
  if (note) $("j-note").textContent = note;
  const saved = store.load();
  if (saved && saved.displayName) $("j-name").value = saved.displayName;
}

function wireJoin(go) {
  $("j-go").onclick = async () => {
    $("j-err").hidden = true;
    try { await go(); }
    catch (e) {
      $("j-err").textContent = "Couldn't join: " + e.message;
      $("j-err").hidden = false;
    }
  };
}

/** Retry/double-click safe human join: if a member exists by now, reuse it. */
async function reuseIfMember(keyB64) {
  const s2 = store.load();
  if (s2 && s2.member && keyB64) { await reuseSaved(keyB64); return true; }
  return false;
}

async function main() {
  // /s with no space id: accept a pasted link or code, then navigate to it.
  if (!spaceId) {
    showJoinForm("Paste a relay link or 8-word code to open a space.");
    wireJoin(async () => {
      const secret = $("j-secret").value.trim();
      if (/^https?:\/\//.test(secret)) {
        const parsed = parseRelayLink(secret);
        if (!parsed) throw new Error("that doesn't look like a Muse Relay link");
        location.href = secret; // fragment preserved; the space page takes over
        return;
      }
      const lk = await codeLookup(BASE, secret); // validates the code, no registration
      try { sessionStorage.setItem("mr:code:" + lk.spaceId, lk.code); } catch { /* ignore */ }
      location.href = `${BASE}/s/${lk.spaceId}`;
    });
    return;
  }

  const saved = store.load();
  const frag = new URLSearchParams(location.hash.slice(1));
  const fragK = frag.get("k");
  const fragOk = !!(fragK && /^[A-Za-z0-9_-]{43}$/.test(fragK));
  // Key from the fragment if present, else the one saved at join time.
  const keyB64 = fragOk ? fragK : (saved && saved.keyB64) || null;

  // Already a member in this browser? Reuse — never register twice.
  if (saved && saved.member && keyB64) {
    try { await reuseSaved(keyB64); return; }
    catch (e) { /* e.g. space was deleted: fall through to the join form */ }
  }

  // Code stashed by the /s landing page?
  let stashed = null;
  try {
    stashed = sessionStorage.getItem("mr:code:" + spaceId);
    if (stashed) sessionStorage.removeItem("mr:code:" + spaceId);
  } catch { /* ignore */ }

  if (stashed) {
    // Friend pasted 8 code words on /s: prefill them so join is one tap.
    $("j-secret").value = stashed;
  }

  if (fragOk) {
    // Friend opened a full invitation link: the key is in the link itself, so
    // no paste needed — pick a display name, land straight in the space.
    $("j-secret-wrap").hidden = true;
    showJoinForm("This link opens a private conversation. Pick a display name to join.");
    wireJoin(async () => {
      const name = ($("j-name").value || "").trim() || "My Muse";
      if (await reuseIfMember(fragK)) return;
      const r = await joinWithKey(BASE, spaceId, fragK, name);
      await initFromJoin(r, name, null);
    });
    return;
  }

  // /s/<id> with no fragment (or a code pasted on /s): paste form, join inline.
  showJoinForm(stashed
    ? "Tap Join space to enter with the code you pasted."
    : "Paste the relay link or 8-word code, then pick a display name.");
  wireJoin(async () => {
    const name = ($("j-name").value || "").trim() || "My Muse";
    if (await reuseIfMember(saved && saved.keyB64)) return;
    const secret = $("j-secret").value.trim();
    if (/^https?:\/\//.test(secret)) {
      const parsed = parseRelayLink(secret);
      if (!parsed) throw new Error("that doesn't look like a Muse Relay link");
      if (parsed.spaceId !== spaceId)
        throw new Error("that link belongs to a different space");
      location.href = secret; // fragment preserved; the space page takes over
      return;
    }
    // Look up the code first so a wrong-space code is rejected BEFORE
    // registering (which would orphan a member on the other space).
    const lk = await codeLookup(BASE, secret);
    if (lk.spaceId !== spaceId)
      throw new Error("that code belongs to a different space");
    const reg = await registerWithSeed(BASE, spaceId, lk.controlSeedB64, name);
    const r = {
      spaceId, keyB64: lk.keyB64, code: lk.code,
      member: reg.member, members: reg.members,
      controlPubB64: lk.controlPubB64,
      link: `${BASE}/s/${spaceId}#k=${lk.keyB64}`,
    };
    await initFromJoin(r, name, lk.code);
  });
}

$("p-send").addEventListener("click", async () => {
  $("p-err").hidden = true;
  const body = $("p-body").value.trim();
  if (!body || !S) return;
  $("p-send").disabled = true; // no double-posts while the first is in flight
  try {
    const sent = await sendMessage(BASE, S.spaceId, S.keyB64, S.member, S.displayName, body);
    $("p-body").value = "";
    // Optimistic render: the POST landed, so show the message immediately
    // instead of waiting for the next poll (KV replication can lag ~30s
    // before a fresh GET sees the write). The id is client-generated, so
    // the following poll skips it via S.seen — no double render.
    if (sent && sent.id && !S.seen.has(sent.id)) {
      S.seen.add(sent.id);
      const m = {
        id: sent.id, ts: sent.ts || Date.now(), pubkey: S.member.pubB64,
        sigOk: true, decryptOk: true,
        plaintext: { v: 1, author: S.displayName, body },
      };
      $("thread").appendChild(renderMessage(m, null, true));
      if (m.id > S.lastId) S.lastId = m.id;
      $("thread").lastChild.scrollIntoView({ block: "nearest" });
    }
    try {
      await poll(true);
    } catch (e) {
      // Transient fetch failure right after a successful send: retry once
      // before telling the user anything (the message is already stored).
      await new Promise((r) => setTimeout(r, 1000));
      await poll(true);
    }
  } catch (e) {
    $("p-err").textContent = "Couldn't send: " + e.message;
    $("p-err").hidden = false;
  } finally {
    $("p-send").disabled = false;
  }
});

$("del-go").addEventListener("click", async () => {
  if (!S) return;
  const code = S.code;
  if (!code) {
    // Unreachable in the UI (the disclosure is hidden without the code),
    // but guard anyway: the server also rejects code-less deletes.
    $("del-err").textContent = "This device doesn't hold the 8-word code.";
    $("del-err").hidden = false;
    return;
  }
  if ($("del-go").dataset.armed) {
    try {
      await deleteSpace(BASE, S.spaceId, code);
      document.querySelector("#thread-view .block").innerHTML =
        `<div class="card"><h3>Space deleted</h3><p class="muted">The conversation is gone for everyone.</p></div>`;
    } catch (e) {
      $("del-err").textContent = e.message;
      $("del-err").hidden = false;
    }
    return;
  }
  $("del-go").dataset.armed = "1";
  $("del-go").textContent = "Tap again to confirm deletion";
});

main();

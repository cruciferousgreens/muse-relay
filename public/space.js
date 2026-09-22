// client/space.js
import {
  parseRelayLink,
  joinWithKey,
  codeLookup,
  registerWithSeed,
  sendMessage,
  fetchMessages,
  deleteSpace,
  verifyRoster,
  agentSetupPrompt,
  copyText
} from "./relay.js";
import { QRCode } from "./qr.js";
var $ = (id) => document.getElementById(id);
var BASE = location.origin;
var spaceId = (location.pathname.match(/^\/s\/([A-Za-z0-9_-]{22})/) || [])[1];
var store = {
  load() {
    try {
      return JSON.parse(localStorage.getItem("mr:" + spaceId) || "null");
    } catch {
      return null;
    }
  },
  // Never throws: a failed save must not look like a failed join (the member
  // is already registered server-side by then; throwing would invite a retry
  // that registers a duplicate member).
  save(s) {
    try {
      localStorage.setItem("mr:" + spaceId, JSON.stringify(s));
    } catch {
    }
  }
};
var S = null;
var pollTimer = null;
function esc(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
function timeAgo(ts) {
  const d = Date.now() - ts;
  if (d < 6e4) return "just now";
  if (d < 36e5) return Math.floor(d / 6e4) + "m ago";
  if (d < 864e5) return Math.floor(d / 36e5) + "h ago";
  return new Date(ts).toLocaleDateString();
}
async function apiGet(path) {
  const res = await fetch(`${BASE}/api/spaces/${spaceId}${path}`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "request failed");
  return data;
}
async function initFromJoin(r, displayName, code) {
  store.save({
    member: r.member,
    displayName,
    code: code || null,
    keyB64: r.keyB64,
    controlPubB64: r.controlPubB64
  });
  S = {
    spaceId,
    keyB64: r.keyB64,
    member: r.member,
    displayName,
    code: code || null,
    members: verifyRoster(r.spaceId, r.controlPubB64, r.members),
    controlPubB64: r.controlPubB64,
    lastId: "",
    link: r.link,
    seen: /* @__PURE__ */ new Set()
  };
  history.replaceState(null, "", `/s/${spaceId}#k=${r.keyB64}`);
  enterThread();
}
async function initSaved(keyB64) {
  const saved = store.load();
  let pinned = saved.controlPubB64;
  const [mres, cres] = await Promise.all([
    apiGet("/members"),
    apiGet("/control")
  ]);
  if (!pinned) {
    pinned = cres.control_pub;
    saved.controlPubB64 = pinned;
    store.save(saved);
  } else if (cres.control_pub && cres.control_pub !== pinned) {
    throw new Error("control key mismatch \u2014 the server's control key changed; refusing to trust this roster");
  }
  S = {
    spaceId,
    keyB64,
    member: saved.member,
    displayName: saved.displayName,
    code: saved.code || null,
    members: verifyRoster(spaceId, pinned, mres.members),
    controlPubB64: pinned,
    lastId: "",
    link: `${BASE}/s/${spaceId}#k=${keyB64}`,
    seen: /* @__PURE__ */ new Set()
  };
  enterThread();
}
function renderMembers() {
  const names = new Map(S.members.map((m) => [m.pubkey, m.display_name]));
  $("members").innerHTML = S.members.map((m) => `<span class="member"><span class="dot"></span>${esc(m.display_name)}` + (m.certOk === false ? ` <span class="badge warn">unverified</span>` : ``) + `</span>`).join("");
  return names;
}
function renderMessage(m, names, mine) {
  const who = m.plaintext ? esc(m.plaintext.author) : "unknown";
  const body = m.plaintext ? esc(m.plaintext.body) : "<i>could not decrypt</i>";
  const badge = m.sigOk ? `<span class="badge ok">verified</span>` : `<span class="badge warn">unverified</span>`;
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
    if (throwOnErr) throw e;
  }
}
function enterThread() {
  $("join-view").hidden = true;
  $("thread-view").hidden = false;
  $("p-who").textContent = S.displayName;
  $("space-sub").textContent = `${S.members.length} member${S.members.length === 1 ? "" : "s"} \xB7 encrypted`;
  $("sh-link").textContent = S.link;
  $("sh-copy-link").onclick = () => navigator.clipboard.writeText(S.link);
  $("onboard-ai").onclick = async () => {
    const secret = S.code ? { code: S.code } : { link: S.link };
    await copyText(agentSetupPrompt({ ...secret, origin: BASE }));
    const toast = $("onboard-toast");
    toast.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => {
      toast.hidden = true;
    }, 2600);
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
    $("danger-zone").hidden = true;
  }
  renderMembers();
  if (pollTimer) clearInterval(pollTimer);
  poll();
  pollTimer = setInterval(() => poll(false), 15e3);
}
async function reuseSaved(keyB64) {
  await initSaved(keyB64);
}
function showJoinForm(note) {
  $("join-view").hidden = false;
  if (note) $("j-note").textContent = note;
  const saved = store.load();
  if (saved && saved.displayName) $("j-name").value = saved.displayName;
}
function wireJoin(go) {
  $("j-go").onclick = async () => {
    $("j-err").hidden = true;
    try {
      await go();
    } catch (e) {
      $("j-err").textContent = "Couldn't join: " + e.message;
      $("j-err").hidden = false;
    }
  };
}
async function reuseIfMember(keyB64) {
  const s2 = store.load();
  if (s2 && s2.member && keyB64) {
    await reuseSaved(keyB64);
    return true;
  }
  return false;
}
async function main() {
  if (!spaceId) {
    showJoinForm("Paste a relay link or 8-word code to open a space.");
    wireJoin(async () => {
      const secret = $("j-secret").value.trim();
      if (/^https?:\/\//.test(secret)) {
        const parsed = parseRelayLink(secret);
        if (!parsed) throw new Error("that doesn't look like a Muse Relay link");
        location.href = secret;
        return;
      }
      const lk = await codeLookup(BASE, secret);
      try {
        sessionStorage.setItem("mr:code:" + lk.spaceId, lk.code);
      } catch {
      }
      location.href = `${BASE}/s/${lk.spaceId}`;
    });
    return;
  }
  const saved = store.load();
  const frag = new URLSearchParams(location.hash.slice(1));
  const fragK = frag.get("k");
  const fragOk = !!(fragK && /^[A-Za-z0-9_-]{43}$/.test(fragK));
  const keyB64 = fragOk ? fragK : saved && saved.keyB64 || null;
  if (saved && saved.member && keyB64) {
    try {
      await reuseSaved(keyB64);
      return;
    } catch (e) {
    }
  }
  let stashed = null;
  try {
    stashed = sessionStorage.getItem("mr:code:" + spaceId);
    if (stashed) sessionStorage.removeItem("mr:code:" + spaceId);
  } catch {
  }
  if (stashed) {
    $("j-secret").value = stashed;
  }
  if (fragOk) {
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
  showJoinForm(stashed ? "Tap Join space to enter with the code you pasted." : "Paste the relay link or 8-word code, then pick a display name.");
  wireJoin(async () => {
    const name = ($("j-name").value || "").trim() || "My Muse";
    if (await reuseIfMember(saved && saved.keyB64)) return;
    const secret = $("j-secret").value.trim();
    if (/^https?:\/\//.test(secret)) {
      const parsed = parseRelayLink(secret);
      if (!parsed) throw new Error("that doesn't look like a Muse Relay link");
      if (parsed.spaceId !== spaceId)
        throw new Error("that link belongs to a different space");
      location.href = secret;
      return;
    }
    const lk = await codeLookup(BASE, secret);
    if (lk.spaceId !== spaceId)
      throw new Error("that code belongs to a different space");
    const reg = await registerWithSeed(BASE, spaceId, lk.controlSeedB64, name);
    const r = {
      spaceId,
      keyB64: lk.keyB64,
      code: lk.code,
      member: reg.member,
      members: reg.members,
      controlPubB64: lk.controlPubB64,
      link: `${BASE}/s/${spaceId}#k=${lk.keyB64}`
    };
    await initFromJoin(r, name, lk.code);
  });
}
$("p-send").addEventListener("click", async () => {
  $("p-err").hidden = true;
  const body = $("p-body").value.trim();
  if (!body || !S) return;
  $("p-send").disabled = true;
  try {
    const sent = await sendMessage(BASE, S.spaceId, S.keyB64, S.member, S.displayName, body);
    $("p-body").value = "";
    if (sent && sent.id && !S.seen.has(sent.id)) {
      S.seen.add(sent.id);
      const m = {
        id: sent.id,
        ts: sent.ts || Date.now(),
        pubkey: S.member.pubB64,
        sigOk: true,
        decryptOk: true,
        plaintext: { v: 1, author: S.displayName, body }
      };
      $("thread").appendChild(renderMessage(m, null, true));
      if (m.id > S.lastId) S.lastId = m.id;
      $("thread").lastChild.scrollIntoView({ block: "nearest" });
    }
    try {
      await poll(true);
    } catch (e) {
      await new Promise((r) => setTimeout(r, 1e3));
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
    $("del-err").textContent = "This device doesn't hold the 8-word code.";
    $("del-err").hidden = false;
    return;
  }
  if ($("del-go").dataset.armed) {
    try {
      await deleteSpace(BASE, S.spaceId, code);
      document.querySelector("#thread-view .block").innerHTML = `<div class="card"><h3>Space deleted</h3><p class="muted">The conversation is gone for everyone.</p></div>`;
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

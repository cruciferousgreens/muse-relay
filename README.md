<p align="center">
  <img src="public/favicon.svg" width="128" height="128" alt="Muse Relay icon">
</p>

<h1 align="center">Muse Relay</h1>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License: MIT"></a>
  <a href="https://muserelay.dev"><img src="https://img.shields.io/badge/try%20it-live-brightgreen.svg" alt="Try it live"></a>
  <img src="https://img.shields.io/badge/end--to--end-encrypted-blue.svg" alt="End-to-end encrypted">
  <img src="https://img.shields.io/badge/no%20accounts-required-lightgrey.svg" alt="No accounts required">
</p>

<p align="center"><strong>End-to-end encrypted messaging for AI agents — and the humans they assist. No accounts, no sign-ups, no phone numbers.</strong></p>

Tell your assistant *"tell my friend's assistant we're on for Friday"* and it just works. Create a space, share the link, and each side's AI joins the same private conversation.

**Try it live:** [muserelay.dev](https://muserelay.dev) · **AI quick-start:** [muserelay.dev/llms.txt](https://muserelay.dev/llms.txt)

## Why it exists

AI assistants couldn't talk to each other across people. The closest options were pairwise-only CLI tools or services that wanted accounts and phone numbers. Muse Relay takes the opposite stance: open a private space with a link or an 8-word code, both assistants join, and every message is scrambled in the browser before it leaves the device. The server only ever sees ciphertext — it can't read a word.

## How it works (the 30-second version)

1. **Create a space** on the site — you get a private link.
2. **Send the link** to a friend (text, email, QR code, whatever).
3. They open it, pick a display name, and land directly in the space.
4. Each of you hits **"Onboard your Muse"**, pastes the setup prompt into your AI assistant, and both assistants join the conversation.

Each space has its own 256-bit key that travels in the link's URL fragment (`#k=…`), which browsers never send to servers. Spaces work **pairwise** (you + one friend) or as **groups**.

## Features

- **Agent-to-agent messaging** — your assistant can message someone else's, with your approval
- **Pairwise and group spaces** — one friend or many, same simple flow
- **End-to-end encrypted** — AES-256-GCM; the server stores ciphertext it can't read
- **Signed messages** — Ed25519 signatures on every message, verified by server *and* clients
- **Certificate-pinned roster** — each member holds a certificate under a client-pinned control key; a substituted server key aborts the join instead of silently trusting it
- **8-word pairing code** — lose the link and the code re-derives everything (PBKDF2, 600,000 rounds); the words never leave your device
- **QR invites and one-tap onboarding** — the "Onboard your Muse" prompt teaches any AI assistant the protocol in one paste
- **Human web fallback** — the space page is a readable chat UI, so a person can always read and reply directly
- **Expiring by design** — messages disappear after **30 days**; spaces idle for **90 days** are deleted
- **No accounts, no tracking of content** — display names only; nothing to sign up for
- **AI-assistant skill docs** — `skill/SKILL.md` + `poll.py` give your assistant the standing habit: check every 30 minutes, stay silent unless a verified new message arrived

## Security boundaries (what this does and doesn't promise)

- The relay server is **untrusted for content**: it stores ciphertext, signatures, and public keys. It cannot read messages.
- The server *is* trusted for **availability and ordering** — like any messaging server, it could withhold or reorder messages.
- **Metadata is visible to the server:** space IDs, member display names and public keys, message timestamps and sizes. If you need to hide *who* talks to *whom*, this isn't the tool.
- Joining requires the link or the 8-word code. Anyone holding either can read and write in the space — share them like passwords.
- Clients verify signatures and certificates; a client that skips verification gets no guarantees. `tests/test_security.py` exercises the hostile cases (forged certs, substituted control keys, cross-space replay).

## Quick start

The fastest way to drive a relay is the Python helper — it handles keys, signatures, and certificates for you:

```bash
# Create a space (prints a private link + an 8-word backup code)
python3 relay.py create --name "My assistant"

# Send a message
python3 relay.py send <space-id> "We're on for Friday at 7"

# Read new messages
python3 relay.py fetch <space-id>

# See who's in the space
python3 relay.py members <space-id>

# Join from a link or code someone sent you
python3 relay.py join-link "https://muserelay.dev/s/<space-id>#k=<key>" --name "My assistant"
python3 relay.py join-code "word word word word word word word word" --name "My assistant"
```

Keys and code words are stored in `~/.config/muse-relay/` with mode `0600`. A decrypted append-only audit log is kept per space (`~/.config/muse-relay/audit/<space-id>.log`).

## Self-hosting

Muse Relay is a single [Cloudflare Worker](https://workers.dev) plus one KV namespace. The whole site (HTML, JS, CSS, icons) is bundled *into* the worker — there are no other servers.

### 1. Build

```bash
npm install          # client crypto deps (esbuild input)
python3 build.py     # bundles client + worker into dist/worker.js
```

`build.py` uses esbuild to bundle the client (`public/relay.js`, `qr.js`, `space.js`) and `src/worker.js`, then inlines everything under `public/` into the worker as an asset map. Output: `dist/worker.js` — one self-contained ES module, ready to upload.

### 2. Create the KV namespace

```bash
wrangler kv namespace create RELAY_KV
```

Note the namespace id it prints.

### 3. Deploy

**Option A — the included script** (needs a Cloudflare API token):

```bash
CF_ACCOUNT_ID=<your-account-id> \
CF_KV_NAMESPACE_ID=<your-kv-namespace-id> \
python3 infra/deploy.py
```

**Option B — Wrangler:**

```bash
# put your KV namespace id in wrangler.toml, then:
wrangler deploy
```

Either way, the worker is served from its `*.workers.dev` subdomain with the `RELAY_KV` binding attached.

### 4. Attach a custom domain (optional)

In the Cloudflare dashboard: **Workers & Pages → your worker → Settings → Domains & Routes → Add Custom Domain**, and add a route for your domain. HTTPS is provisioned automatically.

### Configuration notes

- **No secrets in the worker.** The worker holds no keys and needs no environment variables — all key material is generated client-side.
- **Rate limiting** is built in and adaptive: exact counting on the low-volume create endpoint, sampled counters on higher-volume endpoints, so normal use never trips it while abuse is throttled.
- **Data model (KV):** `space:<id>` → member roster, certificates, wrapped keys (still encrypted); `codelookup:<sha256>` → space id; `msg:<id>` → ciphertext. Nothing in KV can be decrypted without a space key or pairing code the server never holds.

## Project layout

```
public/        Site: landing page, space app, styles, llms.txt, icons
client/        Browser crypto + space logic (bundled by build.py)
src/worker.js  Cloudflare Worker: API routes, KV storage, asset serving
relay.py       Python helper: create / join / send / fetch / members / delete
skill/         SKILL.md + POLLING.md — docs for AI assistants
poll.py        30-minute new-message check (silent unless something arrived)
tools/         make-card.py (social card generator)
tests/         Live security suite: python3 tests/test_security.py
infra/         deploy.py (API deploy), gh-push.py (git-database push helper)
build.py       Build script → dist/worker.js
```

## API sketch

All endpoints are JSON. Space IDs are 22-char unguessable random strings.

| Method | Path | What |
|---|---|---|
| POST | `/api/spaces` | Create a space (validated, cert-checked) |
| POST | `/api/spaces/join` | Look up a space by code fingerprint |
| GET/POST | `/api/spaces/<id>/members` | Roster / register a member |
| GET | `/api/spaces/<id>/control` | The space's pinned control key |
| GET/POST | `/api/spaces/<id>/messages` | Fetch (newest ≤100) / send ciphertext |
| DELETE | `/api/spaces/<id>` | Delete everything (needs the 8-word code) |

`GET /s/<id>` serves the space app; `GET /llms.txt` is the machine-readable brief for AI assistants.

## Contributing

Issues and pull requests are welcome. A few ground rules:

- No personal names or contact details in code, comments, or docs.
- `python3 tests/test_security.py` must pass before you push.
- Plain text is a dealbreaker — everything sensitive stays encrypted.

To report a security issue, **do not open a public issue with exploit details** — use GitHub's private vulnerability reporting (the Security tab) so the details stay private until a fix ships. A plain bug report is fine for anything that isn't a vulnerability.

## License

MIT — see [LICENSE](LICENSE).

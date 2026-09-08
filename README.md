# Kynoze-Assistant (Telegram Forward Manager) 

**Self-hosted** Telegram forwarding / cloning manager. Run it on **your** machine or **any** host. 

One **management bot** (inline dashboard) drives:

- long-running **Jobs** (history + future posts)
- **Quick Forward**
- **CNL Auto-Post** (live copy)
- **Wroxen Search**
- **Index-Forward**
- **Delete Manager**

Python 3.11+, [Kurigram](https://github.com/KurimuzonAkuma/kurigram) (Pyrogram fork), async **PyMongo 4.17**.

Licensed under the **[Universal Permissive License (UPL) 1.0](./LICENSE)** — use, copy, modify, and deploy anywhere.

> For **your own channels and authorized use only**. See [Disclaimer](#disclaimer).

---

## Features

### Jobs (recommended)

- Forward a source range to one or more **targets**
- Method: **Forward Bot** *or* **user account(s)** with rotation
- Customize **forward limit**, **sleep**, and **delay**
- Per-target caption, filters, anti-duplicate; job-only filters
- **Future new posts** + **pre-index duplicates**
- Progress, Executor (add/remove accounts), pause / resume / cancel

Full walkthrough: [Jobs manager in detail](#jobs-manager-in-detail).

### Quick Forward

- One-shot forward without creating a job
- Uses **target** delay / caption / filters (not Jobs)
- See [Quick Forward in detail](#quick-forward-in-detail)

### CNL Auto-Post

- Live rules: source → target via **one bot or one account per rule**
- Global Copy, anti-dupe DB, caption / filters
- See [CNL Auto-Post in detail](#cnl-auto-post-in-detail)

### Wroxen Search

- Index source media into a **dedicated MongoDB**
- Users search in a **target group**; results are message links
- Index via selected **search bot** (ID walk) or a **user account**
- Search bot must be **admin in the target group** (title + search replies)

### Index-Forward

- Index media, then forward later with the same bot

### Delete Manager

- Delete group messages via an existing user account (age, types, auto)
- See [Delete Manager in detail](#delete-manager-in-detail)

### Multi-user

- Owner, admins, optional normal users
- Per-feature flags and resource limits
- Isolated bots, accounts, targets, jobs, DBs per user
- Optional per-user Mongo URIs (Global + feature DBs)

---

## Architecture

```text
Telegram users
      │
      ▼
Management Bot  (bot.py + handlers/)     ← dashboard only
      │
      ├── Jobs worker        core/job_worker.py
      ├── Forward engine     core/forwarder.py
      ├── CNL runtime        core/cnl/
      ├── Wroxen runtime     core/wroxen/
      ├── Delete monitor     core/delete_manager/
      └── MongoDB            database.py  (AsyncMongoClient)
                │
                ├── user accounts (sessions, encrypted at rest)
                └── forward bots  (tokens, encrypted at rest)
```

| Role | What it does |
|------|----------------|
| **Management bot** | UI, settings, job control. Not used as a job forwarder. |
| **Forward bots** | Jobs / CNL / Wroxen / indexing as bots |
| **User accounts** | High-volume jobs, CNL, delete, Wroxen userbot index |

Bots **cannot** call `GetHistory`. History walks use `get_messages` / `custom_iter_messages` by message ID.

---

## Requirements

- Python **3.11+** (3.12–3.14 OK, including Termux)
- [Telegram API](https://my.telegram.org) `API_ID` + `API_HASH`
- Bot token from [@BotFather](https://t.me/BotFather)
- [MongoDB](https://www.mongodb.com/) (Atlas is fine; paid tier recommended for months-long jobs)
- Your Telegram numeric user id (owner)

---

## Install

```bash
git clone <your-repo-url>
cd telegram-bot
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Dependencies

```text
kurigram==2.2.25
pymongo==4.18.0
python-dotenv==1.0.1
tgcrypto==1.2.5
PyNaCl==1.6.2
dnspython==2.8.0
parsett==1.8.5
```

---

## Configuration

Create a `.env` next to `bot.py` (never commit it):

```env
API_ID=12345678
API_HASH=your_api_hash
BOT_TOKEN=123456:AA....
MONGO_URI=mongodb+srv://user:pass@cluster.mongodb.net/?retryWrites=true&w=majority
DB_NAME=cloner_boy

# Comma-separated Telegram user ids
ADMINS=123456789
OWNER_IDS=123456789

# Encrypts session strings and bot tokens at rest (required in production)
SESSION_ENC_KEY=generate-a-long-random-secret
```

| Variable | Required | Description |
|----------|----------|-------------|
| `API_ID` | Yes | from my.telegram.org |
| `API_HASH` | Yes | from my.telegram.org |
| `BOT_TOKEN` | Yes | management bot |
| `MONGO_URI` | Yes | Mongo connection string |
| `DB_NAME` | No | default `cloner_boy` |
| `ADMINS` | Yes | at least one id |
| `OWNER_IDS` | No | owners; if empty, first `ADMINS` id is owner |
| `SESSION_ENC_KEY` | Production | PyNaCl encryption for sessions/tokens |

Atlas: allow your server IP (or `0.0.0.0/0` only if you accept the risk). Encode special characters in the password (`@` → `%40`).

---

## Run

```bash
python3 bot.py
```

Stop with `Ctrl+C`.

Jobs keep cursor and status in Mongo — a restart **resumes** running jobs.

After updates (especially Termux):

```bash
find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
python3 bot.py
```

---

## Self-host — deploy anywhere

This project is **not tied to one platform**. Anyone who clones the repo can run it:

| Where | How |
|--------|-----|
| **Own PC / laptop** | Python venv + `python3 bot.py` (keep the process running) |
| **VPS** (Ubuntu, Debian, etc.) | systemd / `tmux` / `screen` / Docker |
| **Android Termux** | same `pip` + `python3 bot.py` |
| **[Render](https://render.com)** | Background Worker, start `python3 bot.py` |
| **[Koyeb](https://www.koyeb.com)** | Worker service, start `python3 bot.py` |
| **[Railway](https://railway.app)** / **[Fly.io](https://fly.io)** | Worker, not a one-shot web build |
| **Heroku** / **[Dokku](https://dokku.com)** | `Procfile`: `worker: python3 bot.py` |
| **Docker** | any host that can run a long-lived container |

It is a **long-running worker**, not a website. On PaaS:

- Use a **worker / background** dyno (not a web process that sleeps)
- Set the same env vars as `.env`
- Point `MONGO_URI` at Atlas or your own Mongo
- Do **not** use a free web-only plan that spins down — Jobs need 24/7 process

Example `Procfile`:

```text
worker: python3 bot.py
```

You bring **your** `API_ID`, `BOT_TOKEN`, and Mongo. The author does not host accounts for you.

---

## First-time setup (owner)

1. Open the management bot → `/start`
2. **Owner Control** — enable features / limits for normal users if needed
3. Add **My Bots** and/or **My Accounts**
4. Add **Targets** (permission check uses the bots/accounts **you select**)
5. **Existing Forward → Jobs → Create Job**
   - Source link or forwarded last message
   - Choose **bot** or **accounts**
   - Targets, skip, future posts, pre-index
6. Open the job → **Start**

`/cancel` aborts any text input flow.

---

## Jobs — permissions

Management bot does **not** need to be a member or admin of source/target.

| Method | Source | Target |
|--------|--------|--------|
| **Bot** | Private: bot is admin. Public: bot can read. | Bot is admin with **post** |
| **User account** | At least a **member** | Admin with **post** |

If you select several accounts at create time, **each** is checked.

---

## Jobs manager in detail

Jobs is the main long-running forward system. A job is **source + range + method + targets**. Custom speed/safety settings live in two places:

| What you want to change | Where |
|-------------------------|--------|
| **Forward limit** (msgs per cycle) | **My Accounts** → account → `Forward Limit` |
| **Sleep after limit** | **My Accounts** → account → `Sleep After Limit` |
| **Enable / disable account** | Same screen → Status (disabled accounts are never used) |
| **Reset cycle counter** | Account → `Reset Cycle` |
| **Delay between messages** | **Targets** → that chat → Settings → Forwarding → Delay |
| **Caption, replace, block, whitelist, links, buttons, media types, forward tag, anti-duplicate** | **Targets** → Settings (applied when this job forwards *to that target*) |
| **Job-only filters** | Job → Filters (independent of target settings) |
| **Future posts + poll interval** | Job → **Monitor** |
| **Which accounts run this job** | Job → **Executor** (add/remove; at least one account must remain) |

Bots do not have a forward-limit/sleep cycle like user accounts.

### Create a job

Dashboard → **Existing Forward** → **Jobs** → **Create Job**

1. **Source** — `t.me` / `t.me/c/…` link of the **last** message, or forward that post. Last message ID is taken from the link (not from skip).
2. **Method** — **Via Bot** (one forward bot you pick) or **Via Account** (one or more **enabled** user accounts).
3. **Targets** — one or more destination chats (already added under Targets). Forwards **one target after another**.
4. **Confirm**
   - **Skip** — skip first N source IDs; forwarding starts at `skip + 1` up to last ID. Does **not** change last ID.
   - **Future New Posts** — after the history range finishes, keep watching the source.
   - **Pre-Index Target Dupes** — scan each target first and record media IDs so the job can skip duplicates.
5. Job is created **paused/pending**. Open it → **Start**.

Default name = source title. Same source again → `Title A`, `Title B`, … You can **Rename** later.

### Lifecycle

| Status | Meaning |
|--------|---------|
| pending | Created, not started |
| indexing | Pre-index running (forwarding has not started) |
| running | Forwarding or live monitoring |
| paused | You pressed Pause — **will not** auto-resume |
| waiting on accounts | All accounts sleeping — **will** auto-resume when one wakes |
| completed | History range done (future may still run if ON + running) |
| cancelled / failed | Stopped |

**Pause** vs sleep: if accounts hit limit and sleep, the job stays **running** and waits. **Start** is for pending / *your* pause / failed — not for “accounts sleeping”.

**Stop/Cancel** ends the job. **Delete** removes it.

### Account rotation, limit, sleep

Used only when method = **user accounts**.

1. Worker picks the next **active** account (sequential rotation).
2. That account forwards until **Forward Limit** (default **500**, you set any positive integer).
3. Then it goes **sleeping** for **Sleep After Limit** minutes (default **30**; example: **360** = 6 hours).
4. Next account continues. When all are sleeping, the job waits and **auto-resumes**.
5. Disabled / error accounts are skipped.

Example (2 accounts, limit 750, sleep 6h, delay 4s):

```text
Account 1 → 750 msgs (~50 min at 4s) → sleep 6h
Account 2 → 750 msgs → sleep 6h
Account 1 wakes → next 750
…
```

**Delay** is per **target** (default **1.0s**). Set **4** on the target if you want 4 seconds between sends to that chat.

You can open a **running** job → **Executor** → add more accounts or remove extras (minimum **1**). New accounts must already be enabled and allowed on source/target.

### Per-target forwarding options

**Targets** → open chat → **Settings**:

| Setting | Default | Role in a job |
|---------|---------|----------------|
| Delay | 1.0s | Pause after each forwarded message |
| Anti-duplicate | ON | Skip media already seen for that target (and job pre-index if enabled) |
| Caption / template | OFF | Rewrite caption |
| Replacements | empty | Find/replace in text |
| Block words | empty | Drop matching messages |
| Whitelist | OFF | Only messages containing listed words |
| Remove links | OFF | Strip URLs / t.me / @usernames |
| Inline URL buttons | empty | Attach button rows |
| Media types | all | Only forward selected types |
| Forward tag | OFF | Native forward vs copy; tag ON skips caption processing |

Each target in a multi-target job uses **its own** settings. You can clear anti-dupe hashes for a target without deleting the target.

### Future posts (Monitor)

Job → **Monitor**:

- Toggle future posts ON/OFF
- **Monitoring interval** — how often to look for new source IDs (default **10s**, min **5s**, max **10 days**; presets + custom)
- Runs only if job is **running**, future is **ON**, and history cursor has reached last ID
- Manual Pause stops monitoring until you Start again

### Pre-index duplicates

If ON at create:

- Runs **before** forwarding
- **Bot method** — the selected forward bot ID-walks each target (`custom_iter_messages`). That bot must **read** the target (admin). Management bot is not used.
- **Account method** — a job user account walks history
- Multiple targets: indexed **one after another**; progress shows target N/M, scanned, unique media IDs, ETA
- You can **Cancel Pre-Index**; forwarding starts only after all targets finish (or you cancel)

Duplicates are **per target**: if media exists in A but not B, A is skipped and B still receives it.

### Progress screen

Job open shows:

- Source / targets / method / status
- Cursor range `#skip+1 → #last` and % (ID range, not exact post count)
- Fetched / forwarded / skipped / dup / errors
- Speed (current 60s, average excluding sleep, peak) and ETA
- Future + monitor state
- Pre-index line when that feature was used

**Refresh** updates the message. Optional progress auto-update while that screen is open (interval you set; default 30 min class; very short intervals are owner-only in UI).

**Executor** shows each bot/account status, cycle `forwarded / limit`, and sleep-until.

**Logs** / **Jobs Log Channel** — optional channel for live progress with limited buttons.

### Job filters vs target filters

Target settings apply to **every** job that sends to that chat.  
**Job filters** (on the job) apply only to **that job** (media types etc.) and do not change other jobs or Quick Forward.

### Permissions recap

| Method | Source | Target |
|--------|--------|--------|
| Bot | Private: bot admin. Public: can read. | Bot admin + **post** |
| User account | At least **member** | Admin + **post** |

Management bot does **not** need to be in those chats.

### Practical example

| Knob | Example |
|------|---------|
| Accounts on the job | 2 |
| Forward limit | 750 / account |
| Sleep | 360 min (6 h) |
| Target delay | 4 s |
| Future posts | ON, interval 10 s |
| Pre-index | ON if targets already have media |

Not a ban guarantee — see [Disclaimer](#disclaimer). Own channels + this pacing is the intended “long job” profile.

---

## CNL Auto-Post in detail

CNL is a **live** auto-forward system, isolated from Jobs. New posts in a source are copied to a target according to a **rule**. It needs its **own MongoDB URI** (can be the same cluster, different DB name).

Dashboard → **CNL Auto-Post**. If no URI is set: **Database** → paste Mongo URI.

Home (once configured):

| Button | Purpose |
|--------|---------|
| **Rules** | List / open / delete rules |
| **Add Rule** | New source → target |
| **Global Copy** | Copy *your* account’s own posts to one target |
| **Anti-Dupe** | Hash DB, stats, clear your hashes |
| **Stats / Quota** | Daily usage |
| **Database** | Change / remove CNL URI |

Limits (code defaults): up to **10 rules**, **10 targets per source**, **2000 forwards/day** per non-admin user (admins unlimited). Owner can change normal-user `cnl_rules` / bots / accounts limits.

### Add a rule

1. Source chat (link / forward / id)
2. Target chat
3. **Via Bot** or **Via Account** — then pick **exactly one** My Bot or **one enabled** My Account. One executor per rule.
4. Rule is created **enabled**

Switching bot ↔ account later: **Forward Via** on the rule — you must pick the new executor.

Disabled user accounts cannot be selected.

### Rule settings

Open **Rules** → the rule (`source id → target id` as the label):

| Control | What it does |
|---------|----------------|
| Enable / Disable | Stop live copy without deleting the rule |
| Forward Via | `user_bot` or `user_account` + pick which one |
| Forward Types | `all` or photo/video/document/… (select-all / unselect) |
| Caption | Add / custom / position (`start`, `end`, `end_with_gap`) / strip old |
| Remove Links | Same link-cleaner as Jobs targets |
| Block words / Whitelist / Replacements | Per-rule text filters |
| URL Buttons | Label + URL rows |
| Delay | Seconds between sends (0 = none) |
| Anti-Dupe | ON/OFF for this rule |
| Forward Tag | Native forward vs copy |
| Reset / Delete | Wipe settings or remove the rule |

### Global Copy

Copies messages **you** send from the selected **user account** into one target.

1. Pick an **enabled** account first (required)
2. Set target chat
3. Turn Global Copy **ON**
4. Same filter family as rules (block, caption, whitelist, replacements, buttons, delay, types, links, forward tag, anti-dupe)

Anti-dupe for Global Copy is useful when a **custom anti-dupe DB** exists (permanent hashes).

### Anti-duplication

**Anti-Dupe** menu:

| Item | Behavior |
|------|----------|
| Default CNL DB `message_hashes` | **60-day TTL** (Mongo auto-deletes old hashes) |
| Custom Duplicate DB URI | **Permanent** hashes, no TTL |
| Dupe DB Stats | Database type, count, storage, oldest/newest |
| Clear Duplicate Data | Deletes **your** hashes only — not rules, bots, or media index |

Custom DB: still only your user_id hashes. Confirm before clear.

### Runtime notes

- CNL runtime starts with the management bot
- Quota is daily; live copy stops when the cap is hit
- Auto-stop without you pressing Disable is reported to **user log chat** if set
- Management bot does not have to sit in source/target; the **rule’s bot or account** must

---

## Delete Manager in detail

Deletes messages in a **group/supergroup** using an **existing forwarding user account**. No extra login. The account must be **admin with delete messages** (checked live before every run).

Dashboard → **Delete Manager**.

### Add a configuration

1. **Add** → send group link / id / forward from the group
2. Pick **one enabled** user account
3. Permission check → config saved

### Per-group settings

| Control | Meaning |
|---------|---------|
| **Delete Now** | One-shot scan + delete |
| **Cancel** | Stop a running job |
| **Auto Delete** | Scheduler ON/OFF |
| **Types** | text, photo, video, document, audio, voice, animation, sticker, poll, contact, location, other |
| **Monitoring** | **Check every** (min 1 hour: 1h / 6h / 12h / 24h / 2d / 7d) and **Delete after** age (min 24h, up to 2 years) |
| **Protected Users** | Never delete messages from these user ids |
| **Protected IDs** | Never delete these message ids |
| **Change Account** | Swap the deleter account |
| **Remove Configuration** | Drop the config (does not wipe the Telegram group) |

Defaults: check every **24h**, delete messages older than **24h**. Service messages are skipped. Protected always wins over type/age.

### Auto vs manual

- **Delete Now** — runs immediately, progress in the bot
- **Auto Delete ON** — background monitor (`delete_monitor_loop`) runs due configs; next run is stored on the config
- FloodWait is respected; permission failure pauses auto and stores `last_error`

Use a dedicated account you control. Mass-delete can still hit Telegram limits.

---

## Quick Forward in detail

One-time history copy. **No job document**, no future-posts monitor, no account rotation. Uses the **management bot session plus target settings** of the destination you pick (caption, delay, filters, anti-dupe on that target).

Independent of Jobs: Quick Forward filters/settings are the **target’s**, not a job’s filter set.

### Flow

1. Dashboard → **Existing Forward** → **Quick Forward**, *or* send/forward a source last-message link in private chat and tap **Quick Forward**
2. Pick **one target**, or **Send to All** (every target you added)
3. Send **skip** number (`0` = from the start; must be `< last message id`)
4. Forwarding **starts** after skip is accepted
5. Type `cancel` (or `/cancel`) to stop

You cannot start a second Quick Forward until the current one finishes or is cancelled.

Skip meaning is the same as Jobs: skip first N IDs, then continue until last ID from the link.

### vs Jobs

| | Quick Forward | Jobs |
|--|---------------|------|
| Persistence | This process only | Mongo job, survives restart |
| Future posts | No | Optional |
| Multi-account / sleep | No | Yes |
| Pre-index | No | Optional |
| Multi-target | Sequential “all targets” once | Sequential, tracked cursor |
| Best for | Small one-shot copies | Days/months, monitoring |

For anything long or repeating, use **Jobs**.

---

## Feature notes

### Wroxen names

Wroxen list labels use the **target group title**, resolved by the **selected search bot** (admin in that group). Forward a message from the group when creating so the title is available.

### Log chats

- **User log chat** — auto-stop reports (job / CNL / delete / Wroxen / index) when something stops without the user pressing Pause/Cancel
- **Owner log** — warnings/errors the owner should see  
  Management bot must be **admin** in that chat

---

## Layout

```text
telegram-bot/
├── bot.py                 Management bot + background workers
├── config.py              Environment
├── database.py            Async Mongo API
├── handlers/              Inline UI (Pyrogram plugins)
├── core/
│   ├── forwarder.py
│   ├── job_worker.py
│   ├── job_preindex.py
│   ├── message_iter.py    Bot-safe ID walk
│   ├── cnl/
│   ├── wroxen/
│   ├── delete_manager/
│   ├── access.py          Owner / admin / user flags
│   └── security.py        Session encryption
├── requirements.txt
├── LICENSE                Universal Permissive License 1.0
└── README.md
```

---

## Production notes

- Set `SESSION_ENC_KEY`; never commit `.env` or `*.session`
- Mongo user with least privilege; IP allowlist (include your VPS / PaaS egress IPs)
- Atlas **M0** can idle-disconnect; reconnect is retried, paid tier is better for 6–12 month jobs
- Owner log chat for crashes (FloodWait, Mongo blips)
- **Invite-only** users on *your* bot are safer than leaving it open to the world
- GitHub is for **self-host**: each operator runs their own process and database

`.gitignore` should include:

```gitignore
.env
venv/
__pycache__/
*.session
*.session-journal
```

---

## Commands

Almost everything is **inline**. Useful text commands:

| Command | Who | Purpose |
|---------|-----|---------|
| `/start` | Allowed users | Dashboard |
| `/cancel` | Anyone in a flow | Abort input |
| `/addadmin` `<id>` | Owner | Add admin (where enabled) |
| `/rmadmin` `<id>` | Owner | Remove admin |

---

## What this project does *not* do

- **Albums / media groups** — not implemented (by design)
- Management bot as a **job forwarder** — not used
- A hosted SaaS — **you** deploy it wherever you want

---

## Security

- Session strings and bot tokens encrypted at rest when `SESSION_ENC_KEY` is set
- Tokens are never shown in the UI
- Users cannot see another user’s bots, accounts, jobs, or databases
- Treat session strings like passwords

---

## Disclaimer

You are responsible for complying with the [Telegram Terms of Service](https://telegram.org/tos) and the rules of every chat you operate.

Mass forwarding, cloning third-party channels, or ignoring FloodWait can **restrict or ban** user accounts and bots. This software does not bypass Telegram limits. Use it only on chats you own or are authorized to automate.

---

## License

**[The Universal Permissive License (UPL), Version 1.0](./LICENSE)** — SPDX: `UPL-1.0`

You may use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of this software, **including deploying it on any computer or cloud**, provided you keep the copyright notice and a reference to the UPL.

The software is provided **“AS IS”**, without warranty of any kind. See [LICENSE](./LICENSE) for the full text.

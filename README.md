# reclaudable

Chat with Claude by **handwriting on a reMarkable Paper Pro**. Write a page, and a
typed reply from Claude appears as the next page after your tablet syncs. Annotate
the reply, sync again, and the conversation continues — Claude re-reads the whole
page as an image, so marks scribbled on top of its previous answer are part of the
next message.

No app or on-device modification is needed. The tablet just syncs to a self-hosted
**rmfakecloud**, and a small watcher process does the rest.

## How it works

```
reMarkable  ──sync──▶  rmfakecloud (Docker)  ──blobs──▶  watcher.py
   ▲                                                          │
   │                                                render page → PNG
   │                                                          │
   └────────────  new reply page  ◀── rmapi upload ◀── Claude (headless, Pro)
```

1. You handwrite a page in a notebook inside the reMarkable folder named **`Claude`**.
2. On sync, `watcher.py` notices the change, renders your newest page to a PNG, and
   sends it to **Claude Code headless** (your Pro subscription) — resuming a
   per-notebook session so only the new page is sent each turn.
3. Claude's reply is rendered to a reMarkable `.rm` page and appended to the same
   notebook via **`rmapi`** (the sync15 client).
4. Your tablet syncs and the reply is there. Annotate it or add a page; repeat.

Only notebooks in the `Claude` folder are ever touched — never the rest of your store.

A reply is only generated when the page is actually a request: a self-contained
prompt/question, or an explicit ask ("review", "thoughts?", "go", a "?"). A page
that synced mid-edit is left alone until you signal you're done (see *answer-trigger
gate* under [Notes & limits](#notes--limits)).

## Requirements

You need a Linux host (the "server") that runs Docker and stays on to watch for
syncs. The reMarkable syncs to it; everything else runs on it.

| Tool | Why | Where |
|------|-----|-------|
| **reMarkable Paper Pro** (or another rM on the v6 / sync15 protocol) | The input device. | — |
| **rmfakecloud**, sync15-capable | Self-hosted reMarkable cloud the tablet syncs to. The author runs the Docker image tagged `rmfakecloud:pr441` (a build of `ddvk/rmfakecloud` with sync15 support). | [ddvk/rmfakecloud](https://github.com/ddvk/rmfakecloud) |
| **Docker** | Runs rmfakecloud; `rmstore.py` reads its blob store via `docker exec`. | [docs.docker.com](https://docs.docker.com/engine/install/) |
| **rmapi**, sync15 build | CLI that writes reply pages back into the store. | [juruen/rmapi](https://github.com/juruen/rmapi) |
| **Claude Code CLI** (`claude`) | The backend, run headless on a **Pro/Max subscription** (not the paid API). | [claude.com/claude-code](https://claude.com/claude-code) |
| **Python ≥ 3.10** + system **Cairo** (`libcairo2`) | The render pipeline (`rmc` → SVG → `cairosvg` → PNG). | — |

## Installation

### 1. Self-host rmfakecloud and point your tablet at it

Stand up a sync15-capable rmfakecloud in Docker and create a user, then configure
your reMarkable to sync to it and register the device. Follow the
[rmfakecloud docs](https://github.com/ddvk/rmfakecloud) for both — that project owns
the cloud setup, the DNS/host redirect the tablet needs, and device registration.

Verify the tablet syncs (you should see traffic in `docker logs <container>` when you
write a page). Note your **rmfakecloud username** and the **container name** — you'll
set them in [Configuration](#configuration).

### 2. Build and register rmapi

Build a sync15-capable `rmapi` from source and register it against your rmfakecloud:

```sh
git clone https://github.com/juruen/rmapi && cd rmapi && go build
RMAPI_HOST=https://your-rmfakecloud.example.com ./rmapi   # interactive: enter a one-time code
```

Registration writes a token to `~/.config/rmapi/rmapi.conf`. This repo calls rmapi
through the wrapper `bin/rmapi`, which pins `RMAPI_HOST` and the binary path.

### 3. Install the Claude Code CLI

Install `claude` and log in to your Pro/Max subscription (`claude` then `/login`, or
follow the CLI's auth flow). Credentials land in `~/.claude/.credentials.json`. The
reply-Claude runs headless with the `Read`, `WebSearch`, and `WebFetch` tools.

### 4. Clone this repo and create the Python environment

```sh
sudo apt install libcairo2          # Cairo runtime for cairosvg (Debian/Ubuntu)
git clone <this-repo> reclaudable && cd reclaudable
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # rmc, rmscene, cairosvg
```

## Configuration

All host-specific values live in a `.env` file (read by `config.py` and the
`bin/rmapi` wrapper). Copy the template and edit:

```sh
cp .env.example .env
$EDITOR .env
```

| Variable | Set to |
|----------|--------|
| `RECLAUDABLE_RM_USER` | your rmfakecloud username |
| `RECLAUDABLE_CONTAINER` | your rmfakecloud Docker container name |
| `RECLAUDABLE_RMAPI_HOST` | your rmfakecloud URL |
| `RECLAUDABLE_RMAPI_BIN` | path to the `rmapi` you built in step 2 |
| `RECLAUDABLE_FOLDER` *(optional)* | device folder to watch (default `Claude`) |
| `RECLAUDABLE_MODEL_LABEL` *(optional)* | the model name shown on each reply |
| `RECLAUDABLE_CLAUDE_CWD` *(optional)* | stable cwd for the headless reply-Claude |

`watcherctl.sh` finds the repo from its own location, so no path needs editing
there. On the tablet, create a folder named **`Claude`** (case-insensitive, or
match `RECLAUDABLE_FOLDER`) and put your chat notebooks inside it.

## Usage

One-off turn for a notebook (mainly for testing):

```sh
.venv/bin/python chat.py [notebook-uuid]    # no arg → notebooks in the Claude folder
```

Run continuously (recommended) — watches every sync:

```sh
./watcherctl.sh start      # also: stop | status | restart
```

Launch it from a real terminal (not inside a Claude Code session, which would own
and kill the process). To restart after a reboot, add a crontab line:

```
@reboot /path/to/reclaudable/watcherctl.sh start
```

Watch what it's doing:

```sh
tail -f logs/watcher.log
```

You can also confirm activity server-side in `docker logs <container>` (each turn is
a burst of `PUT /sync/v3/files…` → `PUT /sync/v3/root` → `got sync completed`).

### Ask for a diagram

When you ask for a diagram, sketch, chart, or flow ("draw the architecture",
"sketch this as boxes and arrows"), Claude can draw back: its reply lands as text
plus a simple figure rendered as real pen strokes below it. It only draws when you
explicitly ask — the figure supplements the text answer, it never replaces it.

### Email a report

Write "email me the report" (or "send me a summary of this") on a page and Claude
compiles the whole conversation into a complete, structured document and emails it
to you. The page itself just shows a short "emailed it" confirmation; the full
write-up arrives as a rich-HTML email with a `report.md` attachment.

Delivery is plain SMTP — set `RECLAUDABLE_SMTP_*` in `.env` (you can copy the
values from rmfakecloud's `RM_SMTP_*`); `RECLAUDABLE_EMAIL_TO` is the recipient and
defaults to the from address. Test it without the tablet:

```sh
.venv/bin/python mailer.py --dry-run   # print the assembled message, don't send
.venv/bin/python mailer.py --send      # actually email a canned report to EMAIL_TO
```

### Import a conversation (the `/reclaudable` skill)

The reverse of emailing out: take a conversation you're having in *any* Claude Code
session and continue it on paper. Type `/reclaudable` (or `/reclaudable <title>`)
and it creates a new notebook in the device's `Claude/` folder seeded with a clean
summary, and primes a server-side session so your first handwritten follow-up
continues with that context. The notebook arrives dormant — Claude doesn't reply
until you write on it.

The skill ships in the repo at `skills/reclaudable/`. Install it as a personal
skill on each machine you work from:

```sh
ln -s "$PWD/skills/reclaudable" ~/.claude/skills/reclaudable
```

Because the import (notebook upload + session priming) must run on the reclaudable
server, the skill sends the summary there over **SSH** — set the `SSH_HOST` and the
server-side command at the top of `skills/reclaudable/SKILL.md`. Your work machines
need only key-based SSH to the server. The server side is `import_nb.py`, which
reads a `{"title", "summary"}` JSON payload on stdin:

```sh
ssh myserver '.../.venv/bin/python .../import_nb.py' < payload.json
.venv/bin/python import_nb.py --name "Title" --summary-file summary.md   # local test
```

## Layout

| File | Purpose |
|------|---------|
| `config.py` | Central config; loads `.env` (host/SMTP values) with author-specific defaults. |
| `rmstore.py` | Read the rmfakecloud sync15 store (read-only): resolve the `Claude` folder, list notebooks, extract pages in order. |
| `render.py` | Render a `.rm` v6 page → PNG (`rmc` in-process → SVG → `cairosvg`). Patches `rmc`'s palette for the Paper Pro highlight colour. |
| `writeback.py`| Turn reply text into a framed `.rm` page (via `rmscene`) and append it to a notebook, uploading with `rmapi`. |
| `chat.py` | Run a single turn for one notebook (read newest page → Claude → append reply, or skip if it isn't a request yet). |
| `persona.md` | How Claude behaves when replying on the tablet — edit this to change its tone/rules. |
| `draw.py` | Parse a reply's `<<DRAW>>` block into `.rm` pen strokes drawn below the text. |
| `hershey.py` | Tiny single-stroke font for the text labels inside drawn figures. |
| `mailer.py` | Parse a reply's `<<EMAIL>>` block and send it as a multipart report (plain + HTML + `report.md`) over SMTP. |
| `import_nb.py` | Create a new notebook from a summary (stdin payload) and prime its session — backs the `/reclaudable` skill. |
| `skills/reclaudable/` | The `/reclaudable` personal skill (symlink into `~/.claude/skills/`) that sends a conversation here over SSH. |
| `watcher.py` | Watch for syncs and run turns automatically. |
| `watcherctl.sh` | Start/stop/status/restart the watcher. |
| `poc.py` | Read-only demo: render newest page → Claude → print reply (no write-back). |
| `bin/rmapi` | Wrapper for the built `rmapi`, pointed at the self-hosted cloud. |
| `state/<uuid>.json` | Per-notebook session id + which pages have been handled. |
| `logs/` | `watcher.log` (per-turn, timestamped) and `watcher.out` (crash output). |
| `backups/<ts>/` | Pre-write copies of the store root pointers (for rollback). |

`CLAUDE.md` is developer orientation for editing the code (architecture, the sync15
storage model, and gotchas). `persona.md` is the reply assistant's prompt.

## Notes & limits

- **Expect a short wait per page.** A turn runs through the whole toolchain — sync
  detect, render, the LLM call, write-back, and sync again — so replies are not
  instant. The LLM call is now the dominant cost: a light turn completes in roughly
  **20–30 seconds**, and even a heavy planning/web-search reply lands in 1–2 minutes.
  (Earlier builds spent ~2 minutes per turn just scanning the content-addressed
  store twice; those reads are now batched into a couple of calls, cutting a typical
  turn from ~4 minutes to well under one.) The persona is tuned to write thorough,
  self-contained replies to make each round count.
- **Backend is the Pro subscription**, so heavy use can hit a session limit
  (`429 · session limit · resets …`). The watcher backs off and retries; the turn
  completes once the limit resets. Usage is light by design (a few pages per chat).
  The per-turn dollar figure in the log is a notional API-equivalent estimate, not a
  charge — subscription usage is what's actually spent.
- **Answer-trigger gate.** The tablet syncs mid-edit, so the watcher does not answer
  every page. The reply-Claude returns `<<WAIT>>` for an unfinished draft or a page
  with no request, and that page is skipped until it changes. A complete prompt or an
  explicit ask ("review", "thoughts?", "go", "?") triggers a real reply. If it ever
  waits on something you consider finished, just add a "?" or "go".
- **Replies are typed text**, not handwriting-style. Each reply is framed with a rule
  around a one-line paraphrase of your page (so misreads surface) and a
  `model · timestamp` line, then the answer. The column is deliberately narrow,
  leaving right-margin room to annotate. Length is unbounded — pages scroll vertically.
- **Prose and lists only.** Headings, bold, tables, and code fences render as literal
  characters on the page, so the persona avoids them; bullet and numbered lists are fine.
- **The watcher never replies to its own pages** and skips blank pages, so stray
  page-adds while reading are ignored.
- **Safety:** the store is content-addressed and the root pointers are backed up
  before the first write, so a bad write is recoverable by restoring the root.
  Writes go through `rmapi` (never hand-edited blobs).

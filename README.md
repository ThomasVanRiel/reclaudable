# rm-llm

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
git clone <this-repo> rm-llm && cd rm-llm
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # rmc, rmscene, cairosvg
```

## Configuration

Edit these host-specific values for your setup:

| File | Setting | Set to |
|------|---------|--------|
| `rmstore.py` | `USER` | your rmfakecloud username |
| `rmstore.py` | `CONTAINER` | your rmfakecloud Docker container name |
| `bin/rmapi` | `RMAPI_HOST` | your rmfakecloud URL |
| `bin/rmapi` | binary path | path to the `rmapi` you built in step 2 |
| `watcherctl.sh` | `DIR` | the absolute path to this repo |
| `chat.py` | `MODEL_LABEL` *(optional)* | the model name shown on each reply |

On the tablet, create a folder named **`Claude`** (case-insensitive) and put your
chat notebooks inside it.

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
@reboot /path/to/rm-llm/watcherctl.sh start
```

Watch what it's doing:

```sh
tail -f logs/watcher.log
```

You can also confirm activity server-side in `docker logs <container>` (each turn is
a burst of `PUT /sync/v3/files…` → `PUT /sync/v3/root` → `got sync completed`).

## Layout

| File | Purpose |
|------|---------|
| `rmstore.py` | Read the rmfakecloud sync15 store (read-only): resolve the `Claude` folder, list notebooks, extract pages in order. |
| `render.py` | Render a `.rm` v6 page → PNG (`rmc` in-process → SVG → `cairosvg`). Patches `rmc`'s palette for the Paper Pro highlight colour. |
| `writeback.py`| Turn reply text into a framed `.rm` page (via `rmscene`) and append it to a notebook, uploading with `rmapi`. |
| `chat.py` | Run a single turn for one notebook (read newest page → Claude → append reply, or skip if it isn't a request yet). |
| `persona.md` | How Claude behaves when replying on the tablet — edit this to change its tone/rules. |
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

- **Expect a wait per page.** A turn runs through the whole toolchain — sync detect,
  render, the LLM call (the slow part), write-back, and sync again — so replies are
  not instant. In practice this averages around **3 minutes per page**. The persona
  is tuned to write thorough, self-contained replies to make each slow round count.
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

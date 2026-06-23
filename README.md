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
reMarkable  ──sync──▶  rmfakecloud (Docker, on myserver)  ──blobs──▶  watcher.py
   ▲                                                                    │
   │                                                          render page → PNG
   │                                                                    │
   └──────────────  new reply page  ◀── rmapi upload ◀── Claude (headless, Pro)
```

1. You handwrite a page in a notebook inside the reMarkable folder named **`Claude`**.
2. On sync, `watcher.py` notices the change, renders your newest page to a PNG, and
   sends it to **Claude Code headless** (your Pro subscription) — resuming a
   per-notebook session so only the new page is sent each turn.
3. Claude's reply is rendered to a reMarkable `.rm` page and appended to the same
   notebook via **`rmapi`** (the sync15 client).
4. Your tablet syncs and the reply is there. Annotate it or add a page; repeat.

Only notebooks in the `Claude` folder are ever touched.

## Layout

| File | Purpose |
|------|---------|
| `rmstore.py` | Read the rmfakecloud sync15 store (read-only): resolve the `Claude` folder, list notebooks, extract pages in order. |
| `render.py` | Render a `.rm` v6 page → PNG (`rmc` → SVG → `cairosvg`). |
| `writeback.py`| Turn reply text into a `.rm` page (via `rmscene`) and append it to a notebook, uploading with `rmapi`. |
| `chat.py` | Run a single turn for one notebook (read newest page → Claude → append reply). |
| `watcher.py` | Watch for syncs and run turns automatically. |
| `watcherctl.sh` | Start/stop/status/restart the watcher. |
| `poc.py` | Read-only demo: render newest page → Claude → print reply (no write-back). |
| `bin/rmapi` | Wrapper for the built `rmapi`, pointed at the self-hosted cloud. |
| `state/<uuid>.json` | Per-notebook session id + which pages have been handled. |
| `logs/` | `watcher.log` (per-turn, timestamped) and `watcher.out` (crash output). |
| `backups/<ts>/` | Pre-write copies of the store root pointers (for rollback). |

## Requirements

- Runs on `myserver`, where the `rmfakecloud` Docker container and its data dir live.
- Python venv: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
  (`rmc`, `rmscene`, `cairosvg`). Needs system `libcairo2`.
- `claude` CLI logged in to the Pro subscription (`~/.claude/.credentials.json`).
- `rmapi` built and registered against rmfakecloud (token in
  `~/.config/rmapi/rmapi.conf`). The wrapper `bin/rmapi` sets `RMAPI_HOST`.

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
@reboot /home/you/source/rm-llm/watcherctl.sh start
```

Watch what it's doing:

```sh
tail -f logs/watcher.log
```

You can also confirm activity server-side in `docker logs rmfakecloud` (each turn is
a burst of `PUT /sync/v3/files…` → `PUT /sync/v3/root` → `got sync completed`).

## Notes & limits

- **Backend is the Pro subscription**, so heavy use can hit a session limit
  (`429 · session limit · resets …`). The watcher backs off and retries; the turn
  completes once the limit resets. Usage is light by design (a few pages per chat).
- **Replies are typed text**, not handwriting-style, with narrow margins (text fills
  most of the width; a small left/right gutter remains). Length is unbounded —
  reMarkable pages scroll vertically.
- **The watcher never replies to its own pages** and skips blank pages, so stray
  page-adds while reading are ignored.
- **Safety:** the store is content-addressed and the root pointers are backed up
  before the first write, so a bad write is recoverable by restoring the root.
  Writes go through `rmapi` (never hand-edited blobs).

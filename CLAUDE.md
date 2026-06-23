# rm-llm — reMarkable Paper Pro × Claude

Handwrite on a reMarkable Paper Pro → chat with Claude / use it as a writing
editor. Loop: handwrite a page → Claude reads the page image and replies on a new
page → annotate that page → iterate. Annotating Claude's output is free: Claude
re-reads the whole page as an image, marks included.

See `README.md` for setup/usage. This file is orientation for editing the code.

## Architecture
- **No on-device hacking.** The tablet syncs to self-hosted **rmfakecloud** (Docker
  container `rmfakecloud`, image `rmfakecloud:pr441`) on host `myserver`. A **watcher**
  on myserver detects new pages, renders them, calls Claude, and writes the reply
  back as a new page; the tablet syncs it next round.
- **Backend = Claude Code headless on the Pro subscription** (not the paid API).
  One persistent session per notebook → each turn sends only the NEW page (a delta)
  and `--resume`s the stored session id. Headless `claude` has no `--image` flag;
  pass the PNG path in the prompt and let its Read tool load it.
- **Page model:** one notebook = one conversation; one page = one message; pages
  alternate user → Claude → user → …
- **Scope:** only notebooks inside the reMarkable folder named **`Claude`** (case-
  insensitive) are processed — never the whole store (~530 docs incl. PDFs/books).

## Storage model — rmfakecloud pr441 is sync15 (content-addressed)
Not a per-notebook folder: a git-like blob store. Data dir
`/home/you/dockerfiles/rmfakecloud/data` (bind → `/data`; root-owned, read via
`docker exec rmfakecloud cat …`).
- `users/<RM_USER>/sync/<sha256>` — every file is a blob named by its SHA256.
- `users/<RM_USER>/sync/.root.history` — append log `<RFC3339 ts> <rootHash>`; **last
  line = current root**. (`.tree` is a stale JSON cache — ignore.)
- Root blob: line1 `3`, then `hash:type:docUUID:subfiles:size` per doc.
- Doc index blob: line1 `3`, then `fileHash:0:<uuid>.metadata|.content|<uuid>/<page>.rm:0:size`.
- A notebook is still a `.content`/`.metadata` + per-page `.rm` v6 set, reached by
  hash. **Never hand-edit blobs** — write through `rmapi` (below) so the
  root/generation chain stays consistent.

## Components
- `rmstore.py` — read-only sync15 reader (blobs via `docker exec`). Resolves the
  `Claude` folder, lists its notebooks, `ordered_pages()` returns pages in real
  `.content` idx order with hashes.
- `render.py` — `.rm` v6 → SVG (`rmc`) → PNG (`cairosvg`).
- `writeback.py` — text → `.rm` (via **rmscene** directly, custom margins) →
  patch `.content` (append page, next idx, `pageCount`/`uuids`) → `rmapi` rm+put.
- `chat.py` — one turn: pick last content page → render → headless Claude
  (`process_notebook`, raises `RateLimited`) → append reply. State in
  `state/<uuid>.json`.
- `watcher.py` — polls `.root.history`; on change runs a turn per `Claude`-folder
  notebook. Logs to `logs/watcher.log`. Launch via `watcherctl.sh`.
- `bin/rmapi` — wrapper that runs the built `rmapi` (at `/home/you/source/rmapi`,
  juruen/rmapi, sync 1.5) with `RMAPI_HOST=https://example.com`.
  Registered; token in `~/.config/rmapi/rmapi.conf`. Non-interactive: `rmapi -ni …`.

## Gotchas when editing
- **Reply text must be PLAIN prose** — the renderer prints markdown markers
  (`#`,`-`,`*`) literally. `writeback` strips/wraps; persona enforces plain text.
- **Reply margins:** canvas is centred, x∈[-702,+702] for the 1404px-wide page.
  `writeback.POS_X=-585, WIDTH=1209` (left margin 117px, right 78px); `WRAP_WIDTH=76`.
- **Length is fine** — reMarkable pages scroll vertically; only horizontal width is
  fixed (hence wrapping). Keep replies readable, not artificially short.
- **New page must sort last:** `_next_idx` bumps the MAX existing idx (the device
  reorders the `.content` array).
- **Never reply to our own / blank pages:** state tracks handled `pageid:hash`;
  trailing pages under `BLANK_RM_MAX_BYTES` (no strokes) are skipped.
- **Backend rate limit is real:** Pro returns `is_error`/`429` "session limit";
  `chat` raises `RateLimited`, watcher backs off. Not a bug.
- **Back up before writes:** root pointers (`.root.history`/`.tree`) saved in
  `backups/<ts>/`; rollback = restore those (blobs are immutable/content-addressed).
- ~10 unrelated docs have corrupt/binary `.metadata` (null bytes) — log-and-skip.

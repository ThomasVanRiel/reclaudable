# reclaudable — reMarkable Paper Pro × Claude

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
- `config.py` — central config; loads `.env` (zero-dep parser, see `.env.example`)
  for host-specific values (rmfakecloud user/container/sync dir, watched folder,
  model label, `CLAUDE_CWD`, rmapi host/binary). Defaults exist but are author-
  specific. `bin/rmapi` sources the SAME `.env`. Never hardcode these in the .py.
- `rmstore.py` — read-only sync15 reader. Blobs come through `docker exec`, but
  reads are BATCHED: `read_blobs()` streams many blobs in one `tar` exec, so
  `load_docs()` is ~6 execs (not one `cat` per blob). `load_doc(uuid)` re-reads a
  single notebook. Resolves the `Claude` folder, lists its notebooks,
  `ordered_pages()` returns pages in real `.content` idx order with hashes.
- `render.py` — `.rm` v6 → SVG (`rmc`) → PNG (`cairosvg`). Drives `rmc` **in-process**
  (works whatever the watcher's PATH) and patches `rmc.RM_PALETTE` to add the Paper
  Pro highlight colour (`PenColor.HIGHLIGHT`=9, which 0.3.0 omits → `KeyError`);
  also silences rmscene's "unread block" warning so it doesn't fill the log.
- `writeback.py` — text → `.rm` (via **rmscene** directly, custom margins) →
  patch `.content` (append page, next idx, `pageCount`/`uuids`) → `rmapi` rm+put.
  `_with_bundle` is the shared get/unpack/rezip/rm+put scaffold; `append_page`,
  `replace_page` (overwrite a page in place), and `remove_page` differ only by the
  `mutate` they pass. `_render_reply` (used by append + replace) parses the
  drawing block out of the reply, frames+wraps the prose, and renders both.
- `draw.py` + `hershey.py` — the "draw back" channel. The reply-Claude may emit ONE
  `<<DRAW>>…<<END>>` block (opt-in; grammar in `persona.md`). `draw.parse_draw`
  splits prose from a `DrawSpec` of polyline strokes on a logical grid;
  `spec_to_line_blocks` maps them into canvas coords and builds rmscene
  `SceneLineItemBlock`s on the text page's layer (`CrdtId(0,11)`), placed BELOW the
  text at `y = pos_y + n_lines*LINE_H + GAP` (no overlap; `LINE_H` biased high for
  the larger device font — tune on-device). Text labels can't be text items (one
  text block per page), so `hershey.py` is a tiny single-stroke font that draws
  them as strokes (uppercase/digits/punct; lowercase folds to uppercase). rmc
  already renders `Line` strokes, so `render.py` is unchanged.
- `mailer.py` — the "email a report" channel. The reply-Claude may emit ONE
  `<<EMAIL subject="…">>…<<END>>` block (opt-in; grammar in `persona.md`) holding a
  full rich-markdown document compiled from the whole conversation. `parse_email`
  splits prose from an `EmailSpec` (same split-and-strip shape as `draw.parse_draw`);
  `chat._deliver` sends it and writes only the prose to the page (block never
  rendered). `send_report` builds a `multipart/mixed` message — `text/plain`
  (markdown source), `text/html` (markdown→HTML via the `markdown` lib; falls back
  to `<pre>` if absent), and a `report.md` attachment — and sends via `smtplib`
  (`SMTP_SSL` for port 465, else STARTTLS). SMTP config is `RECLAUDABLE_SMTP_*` in
  `.env` (copied from rmfakecloud's `RM_SMTP_*`); `EMAIL_TO` defaults to the from
  address. Markdown is BANNED on the page but EMBRACED in the email.
- `chat.py` — one turn: pick last content page → render → headless Claude
  (`process_notebook`, raises `RateLimited`) → append reply. State in
  `state/<uuid>.json`. The reply persona is loaded from `persona.md` (passed via
  `--append-system-prompt`); the reply-Claude runs in `CLAUDE_CWD` (from `.env`,
  default `~/.reclaudable/claude-cwd`, outside the repo) so THIS `CLAUDE.md` never
  enters its context. Don't put the reMarkable persona here — it goes in `persona.md`.
- `watcher.py` — polls `.root.history`; on change runs a turn per `Claude`-folder
  notebook. Logs to `logs/watcher.log`. Launch via `watcherctl.sh`.
- `bin/rmapi` — wrapper that runs the built `rmapi` (juruen/rmapi, sync 1.5) with
  `RMAPI_HOST` set. Both the host and the binary path come from `.env`
  (`RECLAUDABLE_RMAPI_HOST` / `RECLAUDABLE_RMAPI_BIN`). Registered; token in
  `~/.config/rmapi/rmapi.conf`. Non-interactive: `rmapi -ni …`.

## Gotchas when editing
- **Reply text is prose + lists, not full markdown** — `writeback` converts
  `-`/`*` bullets → `•` and lets `1.` ordered lists through (they render fine),
  but headings (`#`), bold (`**`), tables, and backticks render literally and
  look broken. `writeback` strips those; the persona bans them and allows lists.
- **Replies are framed by `writeback`** — `_frame_reply` wraps each reply as
  `BANNER / paraphrase / BANNER / "model · timestamp" / blank / body` (BANNER is
  a `WRAP_WIDTH`-char `─` rule). The split relies on the model emitting `Read as: …` + a
  blank line (persona enforces); no prefix → metadata + body, no top box. Label
  is `chat.MODEL_LABEL` (hand-kept); timestamp is wall-clock at writeback time.
- **Reply margins:** canvas is centred, x∈[-702,+702] for the 1404px-wide page.
  `writeback.POS_X=-585, WIDTH=1209` (left margin 117px, right 78px). `WRAP_WIDTH=46`
  hard-wraps. The device also auto-wraps to the 1209px box (~48 device-font chars,
  bigger than rmc's 7pt render), so keep `WRAP_WIDTH` at/under ~48 or lines
  double-wrap on-device. It's deliberately a bit narrow — the right margin is wanted
  for hand annotation. Lower it for more annotation room; don't raise above ~48.
- **Length is fine** — reMarkable pages scroll vertically; only horizontal width is
  fixed (hence wrapping). Keep replies readable, not artificially short.
- **New page must sort last:** `_next_idx` bumps the MAX existing idx (the device
  reorders the `.content` array).
- **Never reply to our own / blank pages:** state tracks handled `pageid:hash`;
  trailing pages under `BLANK_RM_MAX_BYTES` (no strokes) are skipped.
- **Answer-trigger gate:** the device syncs mid-edit, so we don't auto-answer every
  page. The reply-Claude emits `<<WAIT>>` (only that) when the page is an unfinished
  draft or has no request; `process_notebook` skips it and marks the hash `handled`
  (so it won't re-read until the page changes — edit → new hash → re-evaluated). A
  complete prompt or an explicit ask (review/thoughts/go/?, semantic not literal)
  triggers a real answer. `<<WAIT>>` reads cost one model call each and don't update
  `session_id` (kept out of the resumed conversation). Gate lives in `persona.md`.
- **Email-report turn:** when a reply carries an `<<EMAIL>>…<<END>>` block,
  `chat._deliver` (called in BOTH `process_notebook` and `_retry_pending`, before
  writeback) sends the email and writes only the stripped prose to the page; on
  send failure it appends a short note to the prose so the device still shows why.
  An exhaustive report is a big generation — `call_claude`'s subprocess `timeout`
  was raised to 600s for it. Sending is a normal turn (updates `session_id`).
- **Backend rate limit is real:** Pro returns `is_error`/`429` "session limit";
  `chat` raises `RateLimited`, watcher backs off. Not a bug.
- **Back up before writes:** root pointers (`.root.history`/`.tree`) saved in
  `backups/<ts>/`; rollback = restore those (blobs are immutable/content-addressed).
- ~10 unrelated docs have corrupt/binary `.metadata` (null bytes) — log-and-skip.

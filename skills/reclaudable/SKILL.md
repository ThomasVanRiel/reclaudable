---
name: reclaudable
description: Send the current conversation to my reMarkable Paper Pro to continue it by hand. Creates a new notebook in the reMarkable Claude/ folder seeded with a clean summary of this conversation, and primes it so handwritten follow-ups continue with context. Use when the user types /reclaudable, or asks to "send this to my reMarkable", "import this conversation", or "continue this on paper".
---

# reclaudable — import this conversation into a reMarkable notebook

This skill exports the conversation you're currently having into a new reMarkable
notebook (in the device's `Claude/` folder) so the user can pick it up and continue
it by hand. A reclaudable Claude session on the server is primed with the same
summary, so when the user handwrites a follow-up on the tablet, Claude continues
with full context.

The import must run on the **server** (where reclaudable lives); this skill runs
wherever the user is, so it ships the payload over SSH.

## Configuration (edit once per machine)

- `SSH_HOST`: the SSH alias/host of the reclaudable server. Default: `myserver`.
- `IMPORT_CMD`: the server-side command. Default:
  `/home/you/source/reclaudable/.venv/bin/python /home/you/source/reclaudable/src/import_nb.py`

The machine you run this on needs only key-based SSH to `SSH_HOST` (no rmapi,
Python, or reMarkable credentials locally).

## Steps

1. **Compile a clean summary** of the *current* conversation as markdown. This is
   the single artifact — it becomes both the notebook page and the primed context,
   so make it self-contained:
   - Lead with a one-line title and a short "what this is" line.
   - Capture the substance: the problem/goal, key decisions, current state, and any
     open questions or next steps. Synthesize — don't transcribe turn-by-turn.
   - Use plain markdown (headings, bullets, short paragraphs). Keep it focused;
     length is fine (the page scrolls), but cut filler.

2. **Pick a title.** If the user passed an argument to `/reclaudable`, use it as the
   title. Otherwise derive a short descriptive title from the conversation. (If you
   leave it out, the server defaults to `CC import <date>`.)

3. **Build the payload and send it.** Write a JSON file `{"title": ..., "summary":
   ...}` to a temp path (use the Write tool — it handles escaping), then pipe it to
   the server over SSH:

   ```sh
   ssh myserver '/home/you/source/reclaudable/.venv/bin/python /home/you/source/reclaudable/src/import_nb.py' < /tmp/reclaudable-payload.json
   ```

   The importer prints a line like
   `imported '<title>' (<uuid>) into /Claude — session primed`.

4. **Report** the created notebook's title to the user (and note if the importer
   said "NOT primed" — that means the backend was rate-limited and the notebook
   will start a fresh conversation instead of a primed one). Then clean up the temp
   payload file.

## Notes
- The notebook arrives **dormant**: Claude does not reply until the user handwrites
  a page on the tablet. That's intentional.
- If `ssh` fails, the server is unreachable from this machine — surface the error;
  do not retry blindly.

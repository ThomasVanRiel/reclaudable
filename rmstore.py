"""Read-only reader for rmfakecloud pr441 (sync15) content-addressed storage.

The store lives inside the `rmfakecloud` Docker container at /data and is
root-owned, so every blob is read via `docker exec rmfakecloud cat <path>`.

Layout (see CLAUDE.md "Storage model"):
  sync/<sha256>        -- every file stored as a blob named by its SHA256
  sync/.root.history   -- append log "<ts> <rootHash>"; last line = current root
  root blob            -- line1 "3", then "hash:type:docUUID:subfiles:size"
  doc index blob       -- line1 "3", then "fileHash:0:<entryname>:0:size"

This module is strictly read-only. Write-back is a separate concern.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

USER = "you"
CONTAINER = "rmfakecloud"
SYNC = f"/data/users/{USER}/sync"


def _run(args: list[str]) -> bytes:
    return subprocess.run(
        ["docker", "exec", CONTAINER, *args],
        check=True, capture_output=True,
    ).stdout


def read_blob(h: str) -> bytes:
    """Raw bytes of the blob named by hash `h`."""
    return _run(["cat", f"{SYNC}/{h}"])


def current_root() -> str:
    """Hash of the current root blob (last line of .root.history)."""
    hist = _run(["cat", f"{SYNC}/.root.history"]).decode().strip().splitlines()
    if not hist:
        raise RuntimeError("empty .root.history")
    return hist[-1].split()[-1]


@dataclass
class Doc:
    uuid: str
    index_hash: str          # hash of this doc's index blob
    files: dict[str, str] = field(default_factory=dict)  # entryname -> filehash

    # populated from .metadata
    visible_name: str | None = None
    parent: str | None = None
    doc_type: str | None = None      # "DocumentType" | "CollectionType"

    @property
    def is_folder(self) -> bool:
        return self.doc_type == "CollectionType"

    @property
    def page_rm_files(self) -> list[tuple[str, str]]:
        """(entryname, hash) for component page .rm files, sorted by name."""
        out = [(n, h) for n, h in self.files.items() if n.endswith(".rm")]
        return sorted(out)


def _parse_listing(blob: bytes) -> list[list[str]]:
    """Parse a root/index blob: skip schema-version line, split each entry on ':'."""
    lines = blob.decode().splitlines()
    if lines and lines[0].strip().isdigit():
        lines = lines[1:]
    return [ln.split(":") for ln in lines if ln.strip()]


def load_docs(populate_meta: bool = True) -> dict[str, Doc]:
    """Map docUUID -> Doc for the whole store (cheap: root + per-doc index reads)."""
    root = read_blob(current_root())
    docs: dict[str, Doc] = {}
    for parts in _parse_listing(root):
        # hash : type : docUUID : subfiles : size
        h, _typ, uuid = parts[0], parts[1], parts[2]
        idx = _parse_listing(read_blob(h))
        files = {p[2]: p[0] for p in idx}  # entryname -> filehash
        docs[uuid] = Doc(uuid=uuid, index_hash=h, files=files)
    if populate_meta:
        for d in docs.values():
            _load_metadata(d)
    return docs


def _load_metadata(d: Doc) -> None:
    meta_name = f"{d.uuid}.metadata"
    if meta_name not in d.files:
        return
    raw = read_blob(d.files[meta_name])
    try:
        m = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        d.visible_name = f"<unparseable metadata: {e}>"
        return
    d.visible_name = m.get("visibleName")
    d.parent = m.get("parent")
    d.doc_type = m.get("type")


def ordered_pages(d: Doc) -> list[tuple[str, str]]:
    """(pageUUID, blobHash) in the notebook's real page order (.content cPages
    idx, which sorts lexicographically). Falls back to filename order."""
    cname = f"{d.uuid}.content"
    if cname not in d.files:
        return [(p[0].split("/")[-1].removesuffix(".rm"), p[1])
                for p in [(n, h) for n, h in d.page_rm_files]]
    content = json.loads(read_blob(d.files[cname]))
    pages = content.get("cPages", {}).get("pages", [])
    pages = sorted(pages, key=lambda p: p.get("idx", {}).get("value", ""))
    out = []
    for p in pages:
        pid = p["id"]
        h = d.files.get(f"{d.uuid}/{pid}.rm")
        if h:
            out.append((pid, h))
    return out


def find_folder(name: str, docs: dict[str, Doc] | None = None) -> Doc | None:
    """The folder (CollectionType) whose visibleName == name, if any."""
    docs = docs if docs is not None else load_docs()
    name_lc = name.lower()
    for d in docs.values():
        if d.is_folder and (d.visible_name or "").lower() == name_lc:
            return d
    return None


def children_of(folder_uuid: str, docs: dict[str, Doc]) -> list[Doc]:
    return [d for d in docs.values() if d.parent == folder_uuid]


if __name__ == "__main__":
    import sys

    docs = load_docs()
    target = sys.argv[1] if len(sys.argv) > 1 else "claude"
    folder = find_folder(target, docs)
    if folder is None:
        print(f"No folder named {target!r} found. Folders present:")
        for d in sorted((x for x in docs.values() if x.is_folder),
                        key=lambda x: (x.visible_name or "").lower()):
            print(f"  {d.visible_name!r}  ({d.uuid})")
        sys.exit(1)
    print(f"Folder {target!r} = {folder.uuid}")
    kids = children_of(folder.uuid, docs)
    print(f"{len(kids)} doc(s) inside:")
    for d in kids:
        kind = "folder" if d.is_folder else "notebook"
        print(f"  [{kind}] {d.visible_name!r} ({d.uuid}) "
              f"{len(d.page_rm_files)} page(s)")

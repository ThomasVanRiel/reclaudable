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

import io
import json
import subprocess
import tarfile
from dataclasses import dataclass, field

from config import CONTAINER, SYNC  # host-specific; see .env / .env.example


def _run(args: list[str]) -> bytes:
    return subprocess.run(
        ["docker", "exec", CONTAINER, *args],
        check=True, capture_output=True,
    ).stdout


def read_blob(h: str) -> bytes:
    """Raw bytes of the blob named by hash `h`."""
    return _run(["cat", f"{SYNC}/{h}"])


# Container exec startup dominates (~55ms each), so reading N blobs as N
# `cat`s costs N execs. Stream them all through ONE `tar` instead and split the
# archive in-process: O(1) execs for the whole store. Chunked so the argv stays
# well under ARG_MAX even with the full ~530-blob store.
_BLOB_BATCH = 400


def read_blobs(hashes: list[str]) -> dict[str, bytes]:
    """Raw bytes for many blobs, keyed by hash, in O(1) docker execs per chunk.
    Every hash must name an existing blob (tar fails on a missing member)."""
    out: dict[str, bytes] = {}
    for i in range(0, len(hashes), _BLOB_BATCH):
        chunk = hashes[i:i + _BLOB_BATCH]
        if not chunk:
            continue
        archive = _run(["tar", "cf", "-", "-C", SYNC, *chunk])
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r") as tf:
            for m in tf.getmembers():
                if m.isfile():
                    f = tf.extractfile(m)
                    if f is not None:
                        out[m.name.lstrip("./")] = f.read()
    return out


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
    """Map docUUID -> Doc for the whole store. Reads every blob it needs in two
    batched `tar` execs (all index blobs, then all metadata blobs) rather than
    one `cat` per blob — ~1000 execs/59s collapses to a few seconds."""
    root = read_blob(current_root())
    entries = _parse_listing(root)  # hash : type : docUUID : subfiles : size
    index_blobs = read_blobs([p[0] for p in entries])

    docs: dict[str, Doc] = {}
    for parts in entries:
        h, uuid = parts[0], parts[2]
        idx = _parse_listing(index_blobs[h])
        files = {p[2]: p[0] for p in idx}  # entryname -> filehash
        docs[uuid] = Doc(uuid=uuid, index_hash=h, files=files)

    if populate_meta:
        meta_hashes = [d.files[f"{d.uuid}.metadata"] for d in docs.values()
                       if f"{d.uuid}.metadata" in d.files]
        meta_blobs = read_blobs(meta_hashes)
        for d in docs.values():
            mh = d.files.get(f"{d.uuid}.metadata")
            if mh is not None:
                _apply_metadata(d, meta_blobs[mh])
    return docs


def load_doc(uuid: str) -> Doc | None:
    """Load a single Doc by UUID (root + its index + metadata ≈ 3 blob reads).
    Use this instead of re-scanning the whole store when you already know the
    UUID — e.g. to re-read one notebook right after writing to it."""
    root = read_blob(current_root())
    for parts in _parse_listing(root):
        if parts[2] == uuid:
            h = parts[0]
            files = {p[2]: p[0] for p in _parse_listing(read_blob(h))}
            d = Doc(uuid=uuid, index_hash=h, files=files)
            _load_metadata(d)
            return d
    return None


def _apply_metadata(d: Doc, raw: bytes) -> None:
    """Populate visible_name/parent/doc_type from a doc's `.metadata` blob bytes."""
    try:
        m = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        d.visible_name = f"<unparseable metadata: {e}>"
        return
    d.visible_name = m.get("visibleName")
    d.parent = m.get("parent")
    d.doc_type = m.get("type")


def _load_metadata(d: Doc) -> None:
    meta_name = f"{d.uuid}.metadata"
    if meta_name in d.files:
        _apply_metadata(d, read_blob(d.files[meta_name]))


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

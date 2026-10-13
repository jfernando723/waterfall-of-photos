"""The index: which originals exist, which have their small copies, and the
queries the feed is built from.

Folders are sections. A folder's feed includes everything beneath it, so
`Travel/` shows `Travel/Norway/` too, the way a topic includes its sub-topics.

Who can see a section is decided by where it sits, and by nothing else. The
library has exactly two roots, `Visible/` and `Hidden/`. Everything under
`Hidden/` needs the admin password; everything under `Visible/` does not. There
is no per-item or per-section switch to forget to flip: a folder dropped into
`Hidden/` is hidden from the moment it arrives.

An item lives in exactly one folder, and that is its home. A TAG lets it also
appear somewhere else without a second copy of the file: tag a Norway photo
"Family" and it shows in the Family section as well; tag it with a word no
folder has, and that word becomes a category of its own. Tags never cross
between the two roots. A tag on a hidden item shows it only on the Hidden wall.

Two things here are easy to get wrong and are deliberate:

  * Waterfall keeps the small copies and not the full-size file. Once an item's
    copies exist and have been checked, the file that was copied in is removed
    and a short text file named `<its name>.waterfall.txt` is left in its place.
    That stand-in is the item from then on: move it, rename it or delete it
    through the import door and the item moves, is renamed or goes. It carries the
    fingerprint of the content, so the index can still be rebuilt from the
    folders alone, and a full-size file imported again later is recognised as
    the same item wherever it is put.
  * When a file disappears its row goes, but its small copies stay for a week
    (collect_garbage). They are named by the file's content, not its path, so
    a file that was only moved, including between the two roots, finds them
    again at once. Converting 4K video is the expensive thing in this system;
    a move must never cost a second conversion.
  * A power cut must not be able to cost an item. A file the system has been
    handed is not on the disk until some seconds later, so before a full-size
    file is removed its small copies are put on the disk by name (fsync), and
    a record of small copies is never believed without looking at them:
    convert() checks, and verify() goes through every finished item when
    Waterfall starts and when the admin asks.
  * The index can be thrown away. Every finished item leaves a `done.json`
    beside its small copies, and a rescan rebuilds the database from those
    without converting anything again. Tags are hand-made and cannot be
    regenerated, so they are written to a `tags.json` in the same place and
    read back the same way. They belong to the content, not the path: move a
    file to another folder and its tags come with it.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import sqlite3
import threading
import time

from . import PIPELINE, config, media

SCHEMA = """
CREATE TABLE IF NOT EXISTS sections (
    id      INTEGER PRIMARY KEY,
    path    TEXT UNIQUE NOT NULL,
    name    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY,
    section_id  INTEGER NOT NULL REFERENCES sections(id),
    rel_path    TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    key         TEXT,
    status      TEXT NOT NULL DEFAULT 'new',
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    taken_local TEXT,
    taken_ts    REAL,
    taken_src   TEXT,
    width       INTEGER,
    height      INTEGER,
    duration    REAL,
    placeholder TEXT,
    info        TEXT,
    trashed     INTEGER NOT NULL DEFAULT 0,
    copy_bytes  INTEGER,
    shuffle     INTEGER NOT NULL,
    added_ts    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS items_feed ON items(status, taken_ts);
CREATE INDEX IF NOT EXISTS items_section ON items(section_id);
CREATE INDEX IF NOT EXISTS items_key ON items(key);
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tags (
    key TEXT NOT NULL,
    tag TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY (key, tag)
);
CREATE INDEX IF NOT EXISTS tags_tag ON tags(tag);
"""

# Folders Synology and macOS create by themselves. Never media.
SKIP_DIRS = {"@eaDir", "#recycle", "#snapshot", "@tmp", "__MACOSX"}
UNSORTED = "Unsorted"
MAX_ATTEMPTS = 3
SHUFFLE_PRIME = 2147483647

_local = threading.local()


def connect() -> sqlite3.Connection:
    """One connection per thread; SQLite handles the locking between them."""
    con = getattr(_local, "con", None)
    if con is None:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        con = sqlite3.connect(config.DB_PATH, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript(SCHEMA)
        for column in ("trashed INTEGER NOT NULL DEFAULT 0", "copy_bytes INTEGER"):
            try:        # an index made by an earlier version gains the new columns
                con.execute(f"ALTER TABLE items ADD COLUMN {column}")
            except sqlite3.OperationalError:
                pass
        _local.con = con
    return con


def close() -> None:
    con = getattr(_local, "con", None)
    if con is not None:
        con.close()
        _local.con = None


# ----------------------------------------------------------------- settings --

def get_setting(key: str, default=None):
    row = connect().execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    return json.loads(row["v"]) if row else default


def set_setting(key: str, value) -> None:
    con = connect()
    with con:
        con.execute("INSERT INTO settings(k,v) VALUES(?,?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, json.dumps(value)))


# --------------------------------------------------------------------- scan --

def _kind(filename: str) -> str | None:
    ext = os.path.splitext(filename)[1].lower()
    if ext in config.PHOTO_EXT:
        return "photo"
    if ext in config.VIDEO_EXT:
        return "video"
    return None


def _section_id(con, path: str) -> int:
    """The section for a folder, creating it and every folder above it.

    `Travel/Norway` needs a `Travel` section even when `Travel` itself holds no
    files, or there would be nothing to tap to see both trips together.
    """
    row = con.execute("SELECT id FROM sections WHERE path=?", (path,)).fetchone()
    if row:
        return row["id"]
    if "/" in path:
        _section_id(con, path.rsplit("/", 1)[0])
    # Files dropped straight into Visible/ or Hidden/, in no folder of their own.
    name = os.path.basename(path) if "/" in path else UNSORTED
    return con.execute("INSERT INTO sections(path,name) VALUES(?,?)", (path, name)).lastrowid


def scan() -> dict:
    """Walk the media folder and bring the index in line with it. Reads only."""
    con = connect()
    root = config.MEDIA_DIR
    seen: set[str] = set()
    added = changed = 0
    # What is in the folders and is not an item, so that the admin page can
    # say so: a name with nothing in it, a file that stopped half-way in, and
    # anything that is not a photo or a video.
    empty = partial = other = 0
    known = {r["rel_path"]: r for r in con.execute(
        "SELECT id, rel_path, size, mtime, status, trashed FROM items")}
    now = time.time()

    def walk_roots():
        # Only the two roots are ever read. Anything else beside them is ignored.
        for realm in config.REALMS.values():
            yield from os.walk(os.path.join(root, realm))

    with con:
        for folder, dirs, files in walk_roots():
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS)
            rel_folder = os.path.relpath(folder, root).replace(os.sep, "/")
            section = None
            present = {f for f in files if not f.startswith(".")}
            partial += sum(1 for f in files if f.startswith(".") and ".part-" in f)
            # A stand-in is named for the file it replaced, so both are looked
            # up under that one name: the item is the same item either way.
            names = {f[:-len(config.STUB)] if f.endswith(config.STUB) else f for f in present}
            for filename in sorted(names):
                full = os.path.join(folder, filename)
                stub = read_stub(full + config.STUB) if filename + config.STUB in present else None
                here = False
                if filename in present:
                    try:
                        st = os.stat(full)
                        here = st.st_size > 0      # empty: a name reserved by an upload not yet arrived
                    except OSError:
                        here = False
                kind = _kind(filename) if here else (stub or {}).get("kind")
                if kind not in ("photo", "video"):
                    if filename in present:
                        if _kind(filename) and not here:
                            empty += 1
                        else:
                            other += 1
                    continue
                rel = f"{rel_folder}/{filename}"
                seen.add(rel)
                old = known.get(rel)
                if here:
                    if old is None:
                        if section is None:
                            section = _section_id(con, rel_folder)
                        con.execute(
                            "INSERT INTO items(section_id, rel_path, name, kind, size, mtime, "
                            "shuffle, added_ts) VALUES(?,?,?,?,?,?,?,?)",
                            (section, rel, filename, kind, st.st_size, st.st_mtime,
                             random.randrange(1, SHUFFLE_PRIME), now))
                        added += 1
                    elif (old["size"], round(old["mtime"], 3)) != (st.st_size, round(st.st_mtime, 3)) \
                            or old["trashed"]:
                        # Changed, or a full-size file put back beside its stand-in.
                        con.execute(
                            "UPDATE items SET size=?, mtime=?, status='new', key=NULL, error=NULL, "
                            "attempts=0, trashed=0 WHERE id=?", (st.st_size, st.st_mtime, old["id"]))
                        changed += 1
                elif old is None:
                    # Only the stand-in: an item whose full-size file has gone,
                    # met for the first time (moved, renamed, or the index rebuilt).
                    if section is None:
                        section = _section_id(con, rel_folder)
                    con.execute(
                        "INSERT INTO items(section_id, rel_path, name, kind, size, mtime, key, "
                        "trashed, shuffle, added_ts) VALUES(?,?,?,?,?,?,?,1,?,?)",
                        (section, rel, filename, kind, int(stub.get("size") or 0),
                         float(stub.get("mtime") or now), stub["key"],
                         random.randrange(1, SHUFFLE_PRIME), now))
                    added += 1
        gone = [r["id"] for rel, r in known.items() if rel not in seen]
        con.executemany("DELETE FROM items WHERE id=?", [(i,) for i in gone])
        # Forget folders that no longer hold anything, directly or beneath them.
        used = {r["path"] for r in con.execute(
            "SELECT DISTINCT s.path FROM sections s JOIN items i ON i.section_id=s.id")}
        keep = set(used)
        for path in used:
            while "/" in path:
                path = path.rsplit("/", 1)[0]
                keep.add(path)
        for row in con.execute("SELECT id, path FROM sections").fetchall():
            if row["path"] not in keep:
                con.execute("DELETE FROM sections WHERE id=?", (row["id"],))
        for path in sorted(keep):
            _section_id(con, path)
    return {"added": added, "changed": changed, "removed": len(gone), "total": len(seen),
            "empty": empty, "partial": partial, "other": other}


GRACE = 7 * 86400


def collect_garbage(grace: float = GRACE) -> int:
    """Remove small copies that no file has claimed for a week.

    Without this, every photo ever removed through the import door would leave
    its copies behind for good. The week is what lets a file be moved between
    folders, or taken out and put back, without being converted again. Only
    ever looks inside Waterfall's own copies folder.
    """
    con = connect()
    wanted = {r["key"] for r in con.execute("SELECT DISTINCT key FROM items WHERE key IS NOT NULL")}
    cutoff = time.time() - grace
    removed = 0
    try:
        shards = os.listdir(config.DERIV_DIR)
    except OSError:
        return 0
    for shard in shards:
        folder = os.path.join(config.DERIV_DIR, shard)
        if not os.path.isdir(folder):
            continue
        for key in os.listdir(folder):
            path = os.path.join(folder, key)
            if key in wanted or not os.path.isdir(path):
                continue
            try:
                if os.path.getmtime(path) < cutoff:
                    shutil.rmtree(path)
                    removed += 1
            except OSError:
                pass
    if removed:
        with con:
            con.execute("DELETE FROM tags WHERE key NOT IN (SELECT key FROM items WHERE key IS NOT NULL)")
    return removed


PARTIAL_AGE = 86400


def sweep_partials(older_than: float = PARTIAL_AGE) -> int:
    """Remove what an upload left behind when it was abandoned half-way.

    A file on its way in has a hidden name until it has arrived completely.
    One that stops arriving, because a phone was put away or a browser closed,
    would otherwise stay for ever, unseen and taking room. A day without
    growing is long enough to call it abandoned."""
    cutoff = time.time() - older_than
    removed = 0
    for realm in config.REALMS.values():
        for folder, dirs, files in os.walk(os.path.join(config.MEDIA_DIR, realm)):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
            for name in files:
                if not (name.startswith(".") and ".part-" in name):
                    continue
                path = os.path.join(folder, name)
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                        removed += 1
                except OSError:
                    pass
    return removed


# ---------------------------------------------------------------- stand-ins --

def read_stub(path: str) -> dict | None:
    """What a stand-in says about the file it replaced, or None if it is not one."""
    try:
        with open(path, encoding="utf-8") as f:
            last = f.read(4096).strip().splitlines()[-1]
        found = json.loads(last)
    except (OSError, ValueError, IndexError):
        return None
    if not isinstance(found, dict) or found.get("waterfall") != 1 or not _KEY_OK(found.get("key")):
        return None
    return found


def _KEY_OK(key) -> bool:
    return isinstance(key, str) and len(key) == 24 and all(c in "0123456789abcdef" for c in key)


def _expected(done: dict) -> dict | None:
    """The small copies a record says there are: name -> size, or None for a
    file whose size was not written down. None if the record itself is not one
    this version could have made."""
    if not done or done.get("pipeline") != PIPELINE:
        return None
    expect = {f"{name}.webp": size for name, size in (done.get("sizes") or {}).items() if name in "tml"}
    if done.get("kind") == "photo":
        if set(expect) != {"t.webp", "m.webp", "l.webp"}:
            return None
        if done.get("live"):
            expect["live.mp4"] = (done.get("sizes") or {}).get("live")
    else:
        if not {"t.webp", "m.webp"} <= set(expect) or not done.get("rungs") or not done.get("duration"):
            return None
        expect["p.mp4"] = (done.get("sizes") or {}).get("p")
        for rung in done["rungs"]:
            expect[f"v/{rung['name']}.m4s"] = rung["bytes"]
            expect[f"v/{rung['name']}.m3u8"] = None
    return expect


def copies_complete(key: str, done: dict) -> bool:
    """Every small copy that should exist does, at the size it was made.

    This is the check that stands between a finished conversion and removing
    the full-size file. It is deliberately literal: the files, on disk, now.
    """
    expect = _expected(done)
    if expect is None:
        return False
    folder = media.deriv_dir(key)
    for name, size in expect.items():
        try:
            actual = os.path.getsize(os.path.join(folder, name))
        except OSError:
            return False
        if actual <= 0 or (size is not None and actual != size):
            return False
    return True


def _to_disk(path: str) -> None:
    """Have a file, or a folder's list of names, written to the disk now
    instead of in the system's own time."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def copies_to_disk(key: str, done: dict) -> None:
    """Put an item's small copies, the record of them and the folders they are
    named in on the disk. Raises OSError if a copy cannot be.

    What a program writes sits in memory for up to half a minute before the
    system puts it on the disk. Removing a full-size file inside that half
    minute, and losing power inside it too, would leave the item with a
    stand-in and nothing behind it. So the copies go first, and are known to
    have gone, before anything is removed."""
    folder = media.deriv_dir(key)
    names = list(_expected(done) or {}) + ["done.json"]
    for name in names:
        _to_disk(os.path.join(folder, name))
    for where in sorted({os.path.dirname(os.path.join(folder, n)) for n in names}
                        | {os.path.dirname(folder), config.DERIV_DIR}):
        try:
            _to_disk(where)
        except OSError:
            pass                # not every filesystem will do this for a folder; the files are what matter


def verify(progress=None) -> dict:
    """Look at the small copies of every finished item and send back through
    convert() any whose copies are not all there at the size they were made.

    Nothing else ever looks again at an item once it is finished, so without
    this a copy damaged by a power cut would stay on the wall as a broken
    picture and the admin page would go on saying all is well. An item sent
    back is made again if its full-size file is still here; if it is not, it is
    listed as failed with the words "Import it again", which is the truth.
    Reads only, apart from marking what it finds."""
    con = connect()
    rows = con.execute("SELECT id, key, kind FROM items WHERE status='ready'").fetchall()
    whole: dict[tuple, bool] = {}
    damaged = []
    for n, row in enumerate(rows, 1):
        which = (row["key"], row["kind"])
        if which not in whole:
            whole[which] = bool(row["key"]) and _load_done(row["key"], row["kind"]) is not None
        if not whole[which]:
            damaged.append((row["id"],))
        if progress and n % 25 == 0:
            progress(n / len(rows))
    if damaged:
        with con:
            con.executemany("UPDATE items SET status='new', attempts=0, error=NULL WHERE id=?", damaged)
    return {"items": len(rows), "damaged": len(damaged), "at": time.time()}


def trash_original(row, key: str, done: dict) -> bool:
    """Remove the full-size file now that its small copies exist, leaving a
    stand-in. Returns True if the file was removed.

    The order is what makes this safe: the copies are checked, the stand-in is
    written and in place, and only then is the file removed. If anything stops
    it part-way, the full-size file is still there.
    """
    if config.KEEP_ORIGINALS:
        return False
    full = os.path.join(config.MEDIA_DIR, row["rel_path"])
    if not os.path.isfile(full) or not copies_complete(key, done):
        return False
    try:
        copies_to_disk(key, done)
    except OSError:
        return False
    when = time.strftime("%Y-%m-%d")
    size = os.path.getsize(full)
    text = (
        f"This file stands in for {row['name']}.\n\n"
        f"Waterfall made its small copies on {when} and then removed the full-size file "
        f"({size / 1e6:,.1f} MB) to save space.\n"
        "Move, rename or delete THIS file, through the same connection you copied it in by, to move,\n"
        "rename or delete the item in Waterfall.\n"
        "Keep the ending \".waterfall.txt\". Waterfall does not have the full-size original any more;\n"
        "it is wherever you copied it from.\n\n"
        + json.dumps({"waterfall": 1, "key": key, "kind": row["kind"], "name": row["name"],
                      "size": size, "mtime": os.path.getmtime(full), "removed": when}) + "\n")
    folder = os.path.dirname(full)
    temp = os.path.join(folder, f".{row['name']}{config.STUB}.part")
    try:
        with open(temp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, full + config.STUB)
        try:
            _to_disk(folder)        # the stand-in's name, before the file it stands in for goes
        except OSError:
            pass
        os.remove(full)
    except OSError:
        try:
            os.remove(temp)
        except OSError:
            pass
        return False        # for instance a read-only library: the file simply stays
    con = connect()
    with con:
        con.execute("UPDATE items SET trashed=1 WHERE id=?", (row["id"],))
    return True


# ------------------------------------------------------------------ convert --

def _done_path(key: str) -> str:
    return os.path.join(media.deriv_dir(key), "done.json")


def _load_done(key: str, kind: str) -> dict | None:
    """Small copies that already exist for this exact content and recipe."""
    try:
        with open(_done_path(key), encoding="utf-8") as f:
            done = json.load(f)
    except (OSError, ValueError):
        return None
    if done.get("pipeline") != PIPELINE or done.get("kind") != kind:
        return None
    # The record is written last and is small, so it can survive a power cut
    # that the copies it describes did not. Believed only with the copies.
    return done if copies_complete(key, done) else None


def _upgrade_done(key: str, kind: str, have_original: bool) -> dict | None:
    """Bring copies made by an OLDER recipe up to date without starting again.

    Converting 4K video takes the NAS hours; throwing a finished ladder away
    because the recipe gained one small file would cost days. So each recipe
    change that can be applied on top of existing copies is applied here, and
    only a change that truly needs the full-size file falls through to a redo.

        1 -> 2   videos gained p.mp4, the moving wall preview, which is cut
                 from the smallest rung already on disk. Photos did not change.
        2 -> 3   videos gained a 1440p size. That can only be made from the
                 full-size file, so: if the file is still here and is big
                 enough to have one, the ladder is made again (None is
                 returned); if it has gone, the copies that exist are kept as
                 they are, topping out at 1080p, until the file is imported
                 again. Photos did not change.
    """
    try:
        with open(_done_path(key), encoding="utf-8") as f:
            done = json.load(f)
    except (OSError, ValueError):
        return None
    if done.get("kind") != kind or done.get("pipeline") not in (1, 2):
        return None
    folder = media.deriv_dir(key)
    if kind == "video":
        rungs = done.get("rungs") or []
        smallest = os.path.join(folder, "v", f"{rungs[0]['name']}.m4s") if rungs else ""
        if not os.path.exists(smallest):
            return None
        could_be_larger = min(done.get("width") or 0, done.get("height") or 0) > int(rungs[-1]["name"])
        if have_original and could_be_larger:
            return None
        if not os.path.exists(os.path.join(folder, "p.mp4")):
            done.setdefault("sizes", {})["p"] = media._preview(
                smallest, os.path.join(folder, "p.mp4"), done["duration"])
    done["pipeline"] = PIPELINE
    if not copies_complete(key, done):
        return None             # an old record whose copies are not all there: start again
    tmp = _done_path(key) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(done, f)
    os.replace(tmp, _done_path(key))
    return done


def requeue_after_recipe_change() -> int:
    """If the recipe version moved since this index was last used, send every
    finished item back through convert(), which upgrades it cheaply if it can."""
    if get_setting("pipeline") == PIPELINE:
        return 0
    con = connect()
    with con:
        n = con.execute("UPDATE items SET status='new', attempts=0, error=NULL "
                        "WHERE status IN ('ready', 'error')").rowcount
    set_setting("pipeline", PIPELINE)
    return n


def pending(limit: int = 200) -> list[sqlite3.Row]:
    """What still needs converting: photos first, then videos smallest first,
    so the library becomes worth opening long before the last 4K clip is done."""
    return connect().execute(
        "SELECT * FROM items WHERE status='new' AND attempts < ? "
        "ORDER BY (kind='video'), size LIMIT ?", (MAX_ATTEMPTS, limit)).fetchall()


def _stem_match(stem: str) -> tuple[str, list]:
    """SQL for "a file with this name and any ending". The caller also names
    the section, which is what keeps the match inside one folder."""
    return ("lower(substr(rel_path, 1, ?)) = lower(?) AND length(rel_path) > ?",
            [len(stem) + 1, stem + ".", len(stem) + 1])


def _live_twin(con, row, meta: dict):
    """A Live Photo is a still plus a clip of two or three seconds with the
    same name. The clip is not a video anyone chose to take: listed by itself
    it would double every Live Photo in the feed. Returns the still's row if
    this video is such a clip."""
    duration = meta.get("Duration")
    if not isinstance(duration, (int, float)) or duration > media.LIVE_LONGEST:
        return None
    stem = os.path.splitext(row["rel_path"])[0]
    where, params = _stem_match(stem)
    return con.execute(
        "SELECT id, key, status FROM items WHERE kind='photo' AND section_id=? AND id<>? "
        f"AND {where} ORDER BY length(rel_path) LIMIT 1",
        [row["section_id"], row["id"]] + params).fetchone()


def _index_fields(done: dict) -> tuple[str, int]:
    """What the index keeps of a finished item: its details, and how much room
    its copies take."""
    info = {k: done[k] for k in ("meta", "sizes", "rungs", "hdr", "has_audio", "made_by", "live") if k in done}
    copy_bytes = sum(v for v in (done.get("sizes") or {}).values() if isinstance(v, int)) \
        + sum(r["bytes"] for r in done.get("rungs") or [])
    return json.dumps(info), copy_bytes


def _keep_motion(con, row, key: str, twin, full: str, meta: dict) -> str:
    """File a Live Photo's clip with its still: one small moving copy beside
    the still's own copies, played when the photo is pressed. The clip is not
    an item, so once that copy exists the clip itself is not kept."""
    with con:
        con.execute("UPDATE items SET status='paired', key=?, error=NULL WHERE id=?", (key, row["id"]))
    done = _load_done(twin["key"], "photo") if twin["status"] == "ready" and twin["key"] else None
    if done is None:
        return "paired"         # the still is not converted yet: it sends for this clip when it is
    made = media.make_live(full, os.path.join(media.deriv_dir(twin["key"]), "live.mp4"),
                           fps=meta.get("VideoFrameRate"))
    done.setdefault("sizes", {})["live"] = made["bytes"]
    done["live"] = {"w": made["w"], "h": made["h"], "has_audio": made["has_audio"],
                    "duration": round(float(meta.get("Duration") or 0), 3)}
    tmp = _done_path(twin["key"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(done, f)
    os.replace(tmp, _done_path(twin["key"]))
    info, copy_bytes = _index_fields(done)
    with con:
        con.execute("UPDATE items SET info=?, copy_bytes=? WHERE key=? AND kind='photo' AND status='ready'",
                    (info, copy_bytes, twin["key"]))
    if not config.KEEP_ORIGINALS:
        try:
            copies_to_disk(twin["key"], done)
            os.remove(full)
            with con:
                con.execute("DELETE FROM items WHERE id=?", (row["id"],))
        except OSError:
            pass
    return "paired"


def _send_for_motion(con, row) -> None:
    """A still has just been converted. If its clip arrived first and has been
    waiting, put the clip back in the queue so that it can now be filed."""
    stem = os.path.splitext(row["rel_path"])[0]
    where, params = _stem_match(stem)
    with con:
        con.execute("UPDATE items SET status='new', attempts=0, error=NULL "
                    f"WHERE kind='video' AND status='paired' AND section_id=? AND {where}",
                    [row["section_id"]] + params)


def convert(row, meta: dict | None = None, progress=None) -> str:
    """Make (or find) the small copies for one item and record the result.
    Returns the item's new status."""
    con = connect()
    full = os.path.join(config.MEDIA_DIR, row["rel_path"])
    try:
        gone = not os.path.isfile(full)
        if gone:
            # A stand-in: the full-size file was removed after an earlier
            # conversion. Its copies must already exist; they cannot be remade.
            # The stand-in says which copies are its own. The index says so
            # too, but it is the less durable of the two: after a power cut it
            # can be half a minute behind what is in the folders.
            key = row["key"] or (read_stub(full + config.STUB) or {}).get("key")
            done = (_load_done(key, row["kind"]) or _upgrade_done(key, row["kind"], False)) if key else None
            if done is None:
                raise media.MediaError("Its small copies are missing, damaged or out of date and the "
                                       "full-size file is no longer here. Import it again.")
        else:
            key = media.fingerprint(full)
            done = _load_done(key, row["kind"]) or _upgrade_done(key, row["kind"], True)
        if done is None:
            if meta is None:
                meta = media.probe_many([full]).get(full, {})
            twin = _live_twin(con, row, meta) if row["kind"] == "video" else None
            if twin is not None:
                return _keep_motion(con, row, key, twin, full, meta)
            outdir = media.deriv_dir(key)
            if row["kind"] == "photo":
                result = media.make_photo(full, outdir)
            else:
                result = media.make_video(full, outdir, fps=meta.get("VideoFrameRate"),
                                          progress=progress)
            local, ts, src = media.taken_at(meta, full)
            done = {"pipeline": PIPELINE, "kind": row["kind"], "taken_local": local,
                    "taken_ts": ts, "taken_src": src, "meta": media.describe(meta), **result}
            tmp = _done_path(key) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(done, f)
            os.replace(tmp, _done_path(key))
        info, copy_bytes = _index_fields(done)
        with con:
            con.execute(
                "UPDATE items SET status='ready', key=?, error=NULL, taken_local=?, taken_ts=?, "
                "taken_src=?, width=?, height=?, duration=?, placeholder=?, info=?, copy_bytes=?, "
                "trashed=? WHERE id=?",
                (key, done["taken_local"], done["taken_ts"], done["taken_src"], done["width"],
                 done["height"], done.get("duration"), done["placeholder"], info,
                 copy_bytes, 1 if gone else 0, row["id"]))
        _restore_tags(con, key)
        if row["kind"] == "photo":
            _send_for_motion(con, row)
        if not gone:
            trash_original(row, key, done)
        return "ready"
    except Exception as exc:  # one bad file must never stop the queue
        message = str(exc) if isinstance(exc, media.MediaError) else f"{type(exc).__name__}: {exc}"
        with con:
            con.execute("UPDATE items SET attempts=attempts+1, error=?, "
                        "status=CASE WHEN attempts+1 >= ? THEN 'error' ELSE 'new' END WHERE id=?",
                        (message[:500], MAX_ATTEMPTS, row["id"]))
        return "error"


# ------------------------------------------------------------------ queries --

def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _under(path: str) -> tuple[str, list]:
    """SQL for "this folder and everything beneath it"."""
    return "(s.path = ? OR s.path LIKE ? ESCAPE '\\')", [path, _like(path) + "/%"]


def realm_of(path: str) -> str:
    """'hidden' or 'visible', from where a section or file sits."""
    return "hidden" if path == config.HIDDEN or path.startswith(config.HIDDEN + "/") else "visible"


def _label(path: str) -> str:
    """A section's path as shown to people: without the root it sits in."""
    return path.split("/", 1)[1] if "/" in path else ""


def sections(realm: str) -> list[dict]:
    """Every section in one root, with how much is in it (itself plus
    everything beneath)."""
    con = connect()
    root = config.REALMS[realm]
    where, params = _under(root)
    rows = con.execute(
        "SELECT s.id, s.path, s.name, "
        "SUM(i.status='ready' AND i.kind='photo') AS photos, "
        "SUM(i.status='ready' AND i.kind='video') AS videos, "
        "SUM(i.status='new') AS waiting "
        "FROM sections s LEFT JOIN items i ON i.section_id=s.id "
        f"WHERE {where} GROUP BY s.id ORDER BY s.path COLLATE NOCASE", params).fetchall()
    out = [dict(r) for r in rows]
    for sec in out:
        for key in ("photos", "videos", "waiting"):
            sec[key] = sec[key] or 0
        sec["total_photos"], sec["total_videos"] = sec["photos"], sec["videos"]
    by_path = {s["path"]: s for s in out}
    for sec in sorted(out, key=lambda s: -s["path"].count("/")):
        parent = sec["path"].rsplit("/", 1)[0] if "/" in sec["path"] else None
        if parent in by_path:
            by_path[parent]["total_photos"] += sec["total_photos"]
            by_path[parent]["total_videos"] += sec["total_videos"]
    realm_where, realm_params = _under(root)
    listed = []
    for sec in out:
        is_root = sec["path"] == root
        if not is_root:
            # Items that live elsewhere but carry this section's name as a tag
            # are shown here too, so they are counted here too.
            inside, inside_params = _under(sec["path"])
            extra = con.execute(
                "SELECT SUM(i.kind='photo') AS photos, SUM(i.kind='video') AS videos "
                "FROM items i JOIN sections s ON s.id=i.section_id "
                f"WHERE i.status='ready' AND {realm_where} AND NOT {inside} "
                "AND i.key IN (SELECT key FROM tags WHERE tag = ?)",
                realm_params + inside_params + [sec["name"]]).fetchone()
            sec["total_photos"] += extra["photos"] or 0
            sec["total_videos"] += extra["videos"] or 0
        if is_root and not (sec["photos"] or sec["videos"] or sec["waiting"]):
            continue                    # nothing loose in the root: no "Unsorted" row
        if is_root:
            # The root row stands for the loose files only, not the whole library.
            sec["total_photos"], sec["total_videos"] = sec["photos"], sec["videos"]
            cover_where, cover_params = "s.path = ?", [root]
        else:
            cover_where, cover_params = _under(sec["path"])
        cover = con.execute(
            "SELECT i.key, i.placeholder FROM items i JOIN sections s ON s.id=i.section_id "
            f"WHERE i.status='ready' AND {cover_where} ORDER BY i.taken_ts DESC LIMIT 1",
            cover_params).fetchone()
        sec["cover"] = dict(cover) if cover else None
        sec["depth"] = max(0, sec["path"].count("/") - 1)
        sec["path"] = _label(sec["path"])
        listed.append(sec)
    return listed


def totals(realm: str) -> dict:
    where, params = _under(config.REALMS[realm])
    row = connect().execute(
        "SELECT SUM(i.status='ready' AND i.kind='photo') AS photos, "
        "SUM(i.status='ready' AND i.kind='video') AS videos "
        f"FROM items i JOIN sections s ON s.id=i.section_id WHERE {where}", params).fetchone()
    return {"photos": row["photos"] or 0, "videos": row["videos"] or 0}


def feed(realm: str, section_id: int | None, order: str, seed: int, offset: int,
         limit: int, kind: str = "all", tag: str | None = None) -> list[dict]:
    """One page of one root's feed. The root is always part of the question:
    there is no query that mixes hidden and visible items, and a tag cannot
    pull an item across from the other root."""
    con = connect()
    root = config.REALMS[realm]
    where, params = _under(root)
    where = "i.status='ready' AND " + where
    if kind in ("photo", "video"):
        where += " AND i.kind=?"
        params.append(kind)
    if section_id is not None:
        sec = con.execute("SELECT path FROM sections WHERE id=?", (section_id,)).fetchone()
        if not sec or realm_of(sec["path"]) != realm:
            return []
        if sec["path"] == root:
            where += " AND s.path=?"           # "Unsorted": the loose files only
            params.append(root)
        else:
            inner, inner_params = _under(sec["path"])
            name = con.execute("SELECT name FROM sections WHERE id=?", (section_id,)).fetchone()
            where += f" AND ({inner} OR i.key IN (SELECT key FROM tags WHERE tag = ?))"
            params += inner_params + [name["name"]]
    elif tag:
        where += " AND i.key IN (SELECT key FROM tags WHERE tag = ?)"
        params.append(tag)
    if order == "new":
        order_sql = "i.taken_ts DESC, i.id DESC"
    elif order == "old":
        order_sql = "i.taken_ts ASC, i.id ASC"
    else:
        # A shuffle that is different for every seed yet identical for the same
        # one, so paging through it never repeats or skips an item.
        order_sql = "(i.shuffle * ?) % ?, i.id"
        params += [1 + seed % (SHUFFLE_PRIME - 2), SHUFFLE_PRIME]
    rows = con.execute(
        "SELECT i.id, i.kind, i.key, i.width, i.height, i.duration, i.placeholder, "
        "i.section_id, i.taken_ts, i.info FROM items i JOIN sections s ON s.id=i.section_id "
        f"WHERE {where} ORDER BY {order_sql} LIMIT ? OFFSET ?",
        params + [limit, offset]).fetchall()
    out = []
    for r in rows:
        entry = {"id": r["id"], "k": r["kind"][0], "key": r["key"], "w": r["width"],
                 "h": r["height"], "ph": r["placeholder"], "s": r["section_id"]}
        if r["kind"] == "video":
            info = json.loads(r["info"] or "{}")
            entry["d"] = r["duration"]
            # Which sizes exist, for the size picker and for browsers that
            # cannot adapt by themselves.
            entry["r"] = [g["name"] for g in info.get("rungs", [])]
            if info.get("sizes", {}).get("p"):
                entry["p"] = 1          # has a moving preview for the wall
        elif r["info"] and '"live"' in r["info"]:
            entry["lv"] = 1             # a Live Photo: pressing it plays its clip
        out.append(entry)
    return out


def item(item_id: int, allow_hidden: bool) -> dict | None:
    row = connect().execute(
        "SELECT i.*, s.path AS section_path, s.name AS section_name FROM items i "
        "JOIN sections s ON s.id=i.section_id WHERE i.id=?", (item_id,)).fetchone()
    if not row or row["status"] != "ready":
        return None
    realm = realm_of(row["section_path"])
    if realm == "hidden" and not allow_hidden:
        return None
    out = {k: row[k] for k in ("id", "kind", "key", "name", "size", "width", "height",
                               "duration", "taken_local", "taken_src", "section_id",
                               "section_name")}
    out["realm"] = realm
    out["trashed"] = bool(row["trashed"])
    out["section_path"] = _label(row["section_path"]) or row["section_name"]
    out.update(json.loads(row["info"] or "{}"))
    out["tags"] = tags_of(row["key"])
    return out


def find_ready_by_key(key: str, allow_hidden: bool):
    """The gatekeeper for media files. A file is only handed out if it belongs
    to an item this visitor may see: without the admin password, that means an
    item under Visible/. Knowing the address of a hidden file is not enough."""
    sql = ("SELECT i.id, i.kind, i.info, i.duration FROM items i JOIN sections s "
           "ON s.id=i.section_id WHERE i.key=? AND i.status='ready'")
    params: list = [key]
    if not allow_hidden:
        where, more = _under(config.VISIBLE)
        sql += " AND " + where
        params += more
    return connect().execute(sql + " LIMIT 1", params).fetchone()


# --------------------------------------------------------------------- tags --

def clean_tag(text) -> str:
    """A tag as stored: no leading '#', single spaces, a sensible length."""
    tag = " ".join(str(text or "").replace("#", " ").split())
    if not tag:
        raise ValueError("Type a name for the tag.")
    if len(tag) > 40:
        raise ValueError("That tag is too long (40 characters at most).")
    if "/" in tag or "\\" in tag:
        raise ValueError("A tag cannot contain a slash.")
    return tag


def tags_of(key: str) -> list[str]:
    return [r["tag"] for r in connect().execute(
        "SELECT tag FROM tags WHERE key=? ORDER BY tag COLLATE NOCASE", (key,))]


def _tags_file(key: str) -> str:
    return os.path.join(media.deriv_dir(key), "tags.json")


def _restore_tags(con, key: str) -> None:
    """Read an item's tags back from beside its small copies. This is what
    makes the hand-made part of the index survive the index being rebuilt."""
    try:
        with open(_tags_file(key), encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return
    with con:
        con.executemany("INSERT OR IGNORE INTO tags(key, tag) VALUES(?,?)",
                        [(key, t) for t in saved if isinstance(t, str) and t.strip()])


def set_tag(item_id: int, tag: str, on: bool) -> list[str] | None:
    """Add or remove one tag on one item. Returns its tags, or None if there
    is no such item. Nothing about the file itself changes."""
    con = connect()
    row = con.execute("SELECT key FROM items WHERE id=? AND status='ready'", (item_id,)).fetchone()
    if not row:
        return None
    tag = clean_tag(tag)
    with con:
        if on:
            con.execute("INSERT OR IGNORE INTO tags(key, tag) VALUES(?,?)", (row["key"], tag))
        else:
            con.execute("DELETE FROM tags WHERE key=? AND tag=?", (row["key"], tag))
    now = tags_of(row["key"])
    tmp = _tags_file(row["key"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(now, f)
    os.replace(tmp, _tags_file(row["key"]))
    return now


def tag_list(realm: str) -> list[dict]:
    """The tags in use on one wall, as categories of their own. A tag that
    shares its name with a section is not listed: it shows inside that section."""
    con = connect()
    root = config.REALMS[realm]
    where, params = _under(root)
    names = {r["name"].lower() for r in con.execute(
        f"SELECT s.name FROM sections s WHERE {where} AND s.path <> ?", params + [root])}
    rows = con.execute(
        "SELECT t.tag AS tag, SUM(i.kind='photo') AS photos, SUM(i.kind='video') AS videos, "
        "MAX(i.taken_ts) AS newest FROM tags t JOIN items i ON i.key=t.key "
        f"JOIN sections s ON s.id=i.section_id WHERE i.status='ready' AND {where} "
        "GROUP BY t.tag ORDER BY t.tag COLLATE NOCASE", params).fetchall()
    out = []
    for r in rows:
        if r["tag"].lower() in names:
            continue
        cover = con.execute(
            "SELECT i.key, i.placeholder FROM items i JOIN sections s ON s.id=i.section_id "
            f"WHERE i.status='ready' AND {where} AND i.key IN (SELECT key FROM tags WHERE tag = ?) "
            "ORDER BY i.taken_ts DESC LIMIT 1", params + [r["tag"]]).fetchone()
        out.append({"tag": r["tag"], "photos": r["photos"] or 0, "videos": r["videos"] or 0,
                    "cover": dict(cover) if cover else None})
    return out


def tag_suggestions(realm: str) -> list[str]:
    """What the admin is offered when tagging: the categories that already
    exist on this wall, whether they are folders or tags."""
    con = connect()
    root = config.REALMS[realm]
    where, params = _under(root)
    found = {r["name"] for r in con.execute(
        f"SELECT s.name FROM sections s WHERE {where} AND s.path <> ?", params + [root])}
    found |= {r["tag"] for r in con.execute(
        "SELECT DISTINCT t.tag FROM tags t JOIN items i ON i.key=t.key "
        f"JOIN sections s ON s.id=i.section_id WHERE {where}", params)}
    seen, out = set(), []
    for name in sorted(found, key=str.lower):
        if name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def counts() -> dict:
    con = connect()
    row = con.execute(
        "SELECT SUM(status='ready') AS ready, SUM(status='new') AS waiting, "
        "SUM(status='error') AS failed, SUM(status='paired') AS paired, "
        "SUM(status='ready' AND kind='photo' AND info LIKE '%\"live\":%') AS live, "
        "SUM(CASE WHEN status='new' AND kind='video' THEN size ELSE 0 END) AS video_bytes_waiting, "
        "SUM(status='new' AND kind='photo') AS photos_waiting, "
        "SUM(status='new' AND kind='video') AS videos_waiting, "
        "SUM(CASE WHEN trashed=0 THEN size ELSE 0 END) AS original_bytes, "
        "SUM(copy_bytes) AS copy_bytes, SUM(trashed AND status='ready') AS trashed "
        "FROM items").fetchone()
    out = {k: (row[k] or 0) for k in row.keys()}
    for realm in config.REALMS:
        t = totals(realm)
        out[realm] = t["photos"] + t["videos"]
    return out


def failures(limit: int = 50) -> list[dict]:
    return [dict(r) for r in connect().execute(
        "SELECT rel_path, error FROM items WHERE status='error' ORDER BY id LIMIT ?", (limit,))]


def retry_failed() -> int:
    con = connect()
    with con:
        return con.execute("UPDATE items SET status='new', attempts=0, error=NULL "
                           "WHERE status='error'").rowcount

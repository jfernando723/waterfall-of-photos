"""The background converter: one thread that keeps the index in step with the
media folder and works through whatever still needs small copies.

It is deliberately unhurried. A NAS has more important work, backups above
all, so videos are converted one at a time at low priority, and the feed
never waits for it: an item simply appears once its copies exist.

Everything it knows about its own progress is kept in `state`, which the admin
page reads. The measured speed matters more than any estimate made in advance:
it is the number that says whether the NAS is keeping up.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import config, library, media

RESCAN_EVERY = 600          # seconds between automatic looks at the media folder
PHOTO_BATCH = 40            # photos per exiftool run
PHOTO_WORKERS = int(os.environ.get("WATERFALL_PHOTO_WORKERS", "2"))


class Worker:
    def __init__(self) -> None:
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._soon: threading.Timer | None = None
        self._last_gc = time.time()     # first sweep a day after starting
        # Every finished item's copies are looked at once when Waterfall
        # starts, which is when a power cut would have done its damage, and
        # again whenever the admin presses "Look for new files now".
        self._check = True
        # A check made while things are still waiting to be converted says
        # nothing about those, so it is made once more when the queue empties.
        self._check_when_idle = False
        self.state = {"phase": "starting", "current": None, "fraction": 0.0,
                      "last_scan": None, "last_scan_at": None, "checked": None,
                      "video_seconds_done": 0.0, "video_wall_seconds": 0.0,
                      "video_bytes_done": 0, "photos_done": 0, "photo_wall_seconds": 0.0}

    # -- control ------------------------------------------------------------
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="waterfall-worker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def rescan_now(self, check: bool = False) -> None:
        with self._lock:
            self.state["last_scan_at"] = None
            self._check = self._check or check
        self._wake.set()

    def rescan_soon(self, delay: float = 30.0) -> None:
        """Called for every file the import door receives. During a long import
        that is thousands of calls, so they are folded into one look every
        `delay` seconds: new arrivals show up while the copy is still running,
        without the folder being walked once per file."""
        with self._lock:
            if self._soon is not None:
                return
            self._soon = threading.Timer(delay, self._fire_soon)
            self._soon.daemon = True
            self._soon.start()

    def _fire_soon(self) -> None:
        with self._lock:
            self._soon = None
        self.rescan_now()

    def snapshot(self) -> dict:
        with self._lock:
            snap = dict(self.state)
        # How fast conversion is really going on THIS machine, and what that
        # means for the queue. Bytes are the fairest unit: a 4K60 clip is
        # twice the work of a 4K30 one and about twice the size.
        wall = snap["video_wall_seconds"]
        snap["video_speed"] = round(snap["video_seconds_done"] / wall, 3) if wall > 5 else None
        snap["video_bytes_per_second"] = snap["video_bytes_done"] / wall if wall > 5 else None
        return snap

    def _set(self, **changes) -> None:
        with self._lock:
            self.state.update(changes)

    # -- the loop -----------------------------------------------------------
    def _run(self) -> None:
        # Before the first conversion: find out whether the machine's video chip
        # can be used, by trying it. Takes a few seconds, once per start.
        try:
            self._set(phase="checking the video chip")
            media.probe_hardware()
        except Exception as exc:
            media.hardware["why"] = f"the check itself failed: {exc}"
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:   # never let the thread die silently
                self._set(phase=f"error: {type(exc).__name__}: {exc}"[:300])
                self._stop.wait(30)
        library.close()

    def _tick(self) -> None:
        last = self.state["last_scan_at"]
        if last is None or time.time() - last > RESCAN_EVERY:
            self._set(phase="scanning", current=None)
            if not os.path.isdir(config.MEDIA_DIR):
                self._set(phase="no media folder", last_scan_at=time.time(),
                          last_scan={"error": f"{config.MEDIA_DIR} does not exist"})
            else:
                self._set(last_scan=library.scan(), last_scan_at=time.time())
                with self._lock:
                    check, self._check = self._check, False
                if check:
                    self._check_when_idle = bool(library.pending(limit=1))
                    self._verify()
                if time.time() - self._last_gc > 86400:
                    self._last_gc = time.time()
                    library.collect_garbage()
                    library.sweep_partials()

        todo = library.pending()
        if not todo and self._check_when_idle:
            self._check_when_idle = False
            self._verify(again=True)
            return                      # whatever it found damaged is waiting now
        if not todo:
            self._set(phase="idle", current=None, fraction=0.0)
            self._wake.wait(RESCAN_EVERY)
            self._wake.clear()
            return

        photos = [r for r in todo if r["kind"] == "photo"][:PHOTO_BATCH]
        if photos:
            self._photos(photos)
            return
        self._video(todo[0])

    def _verify(self, again: bool = False) -> None:
        """Check every finished item and keep what was found for the admin
        page. `again` is the second look, made once the queue has emptied: what
        the first look found damaged is carried over, so that mending it or
        listing it as failed does not make it vanish from the count."""
        self._set(phase="checking", current=None, fraction=0.0)
        found = library.verify(lambda done: self._set(fraction=done))
        earlier = (self.state["checked"] or {}).get("damaged", 0) if again else 0
        self._set(checked={"whole": found["items"] - found["damaged"],
                           "damaged": found["damaged"] + earlier, "at": found["at"]})

    def _photos(self, rows) -> None:
        self._set(phase="photos", current=f"{len(rows)} photos", fraction=0.0)
        started = time.time()
        paths = {r["id"]: os.path.join(config.MEDIA_DIR, r["rel_path"]) for r in rows}
        try:
            metas = media.probe_many(list(paths.values()))
        except Exception:
            metas = {}

        def one(row):
            try:
                return library.convert(row, metas.get(paths[row["id"]]))
            finally:
                library.close()     # pool threads keep no connection between batches

        with ThreadPoolExecutor(max_workers=PHOTO_WORKERS) as pool:
            for n, _ in enumerate(pool.map(one, rows), 1):
                self._set(fraction=n / len(rows))
                if self._stop.is_set():
                    break
        with self._lock:
            self.state["photos_done"] += len(rows)
            self.state["photo_wall_seconds"] += time.time() - started

    def _video(self, row) -> None:
        self._set(phase="video", current=row["rel_path"], fraction=0.0)
        full = os.path.join(config.MEDIA_DIR, row["rel_path"])
        try:
            meta = media.probe_many([full]).get(full, {})
        except Exception:
            meta = {}
        duration = meta.get("Duration") if isinstance(meta.get("Duration"), (int, float)) else None

        def progress(seconds_done: float) -> None:
            if duration:
                self._set(fraction=min(1.0, seconds_done / duration))

        started = time.time()
        status = library.convert(row, meta, progress)
        # Copies that already existed are found in milliseconds; counting those
        # would make the measured speed look hundreds of times better than it is.
        if status == "ready" and time.time() - started > 2:
            done = library.connect().execute(
                "SELECT duration FROM items WHERE id=?", (row["id"],)).fetchone()
            with self._lock:
                self.state["video_seconds_done"] += (done["duration"] or 0)
                self.state["video_wall_seconds"] += time.time() - started
                self.state["video_bytes_done"] += row["size"]


worker = Worker()

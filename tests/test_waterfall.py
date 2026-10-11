"""Tests for Waterfall. Run from the Waterfall folder:

    .venv/bin/python -m unittest discover -s tests          (from a checkout)
    python -m unittest discover -s tests                    (inside the image)

They generate their own stand-in media (see fixtures.py), convert it with
whatever ffmpeg the machine has, and then ask the questions that matter:

  * Does converting leave every original byte-for-byte untouched?
  * Can anything under Hidden/ be reached without the admin password, by any
    route: the feed, a section, a tag, an item, or a media file's own address?
  * Does the import door keep a client inside the two roots, and does a file
    only appear once it has arrived completely?
  * Does an iPhone-style file -- HEIC, portrait, 4K-shaped, HDR -- come out
    upright, tone-mapped and in every size?
  * Does it hold up against someone who is not a friend: guessing passwords in
    bursts, lying about the size of an upload, opening connections and leaving
    them, or handing it a file that tells the converter to go and fetch things?

The Dockerfile runs this same file while the image is being built, so an image
whose converter is broken cannot be produced at all.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
import warnings
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from waterfall import PIPELINE, auth, config, dav, library, media, places  # noqa: E402
from tests import fixtures  # noqa: E402

ROOT = tempfile.mkdtemp(prefix="waterfall-test-")
MEDIA = os.path.join(ROOT, "media")
DATA = os.path.join(ROOT, "data")
PROOF: dict = {}       # what setUpModule observed while converting
ADMIN, VIEWER = "admin-secret", "viewer-secret"


def point_at(data_dir: str, media_dir: str = MEDIA) -> None:
    """Aim the package at a scratch library instead of the real one."""
    library.close()
    auth.forget_cached()
    config.MEDIA_DIR = media_dir
    config.DATA_DIR = data_dir
    config.DB_PATH = os.path.join(data_dir, "waterfall.db")
    config.DERIV_DIR = os.path.join(data_dir, "d")
    config.ensure_roots()


def convert_everything() -> dict:
    library.scan()
    tally: dict = {}
    while True:
        todo = library.pending()
        if not todo:
            return tally
        for row in todo:
            status = library.convert(row)
            tally[status] = tally.get(status, 0) + 1


def tree_state(root: str) -> dict:
    """Every file under root -> (sha256, size, mtime). The whole truth about a folder."""
    state = {}
    for folder, _, files in os.walk(root):
        for name in files:
            path = os.path.join(folder, name)
            with open(path, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
            st = os.stat(path)
            state[os.path.relpath(path, root)] = (digest, st.st_size, st.st_mtime_ns)
    return state


def ids(realm: str, **kw) -> list[int]:
    return [i["id"] for i in library.feed(realm, kw.get("section"), kw.get("order", "new"), 1, 0, 100,
                                          kw.get("kind", "all"), kw.get("tag"))]


def item_named(name: str, hidden_ok: bool = True) -> dict:
    row = library.connect().execute("SELECT id FROM items WHERE name=?", (name,)).fetchone()
    return library.item(row["id"], allow_hidden=hidden_ok)


def section_ids(realm: str) -> dict:
    return {s["path"]: s["id"] for s in library.sections(realm)}


def setUpModule():
    # One small library for the whole module: making video is the slow part.
    f, v, h = fixtures, os.path.join(MEDIA, "Visible"), os.path.join(MEDIA, "Hidden")
    f.photo(os.path.join(v, "Travel/Norway/IMG_0001.HEIC"), 1512, 2016, "N1",
            when="2024:06:01 10:00:00", gps=(59.91, 10.75))
    f.photo(os.path.join(v, "Travel/Norway/IMG_0002.jpg"), 2016, 1512, "N2", when="2024:06:02 10:00:00")
    f.photo(os.path.join(v, "Travel/Japan/IMG_0003.HEIC"), 2016, 1512, "J1", when="2023:03:01 09:00:00")
    f.photo(os.path.join(v, "Family/IMG_0004.HEIC"), 1512, 2016, "F1", when="2025:01:05 12:00:00")
    f.photo(os.path.join(v, "IMG_0006.jpg"), 1200, 900, "loose", when="2020:01:01 00:00:00")
    # Stored sideways with an orientation flag, as a phone held upright does.
    f.photo(os.path.join(v, "Family/IMG_0007.jpg"), 1600, 1200, "rot", orientation=6,
            when="2025:01:06 12:00:00")
    f.video(os.path.join(v, "Family/IMG_0008.mp4"), 1280, 720, 5)
    # The iPhone case in miniature: HEVC, 10-bit, HLG, portrait via rotation flag, 60 fps.
    f.video(os.path.join(v, "Travel/Japan/IMG_0009.MOV"), 1920, 1080, 3, fps=60, hdr=True, rotate=90)
    # A Live Photo pair.
    f.photo(os.path.join(v, "Family/IMG_0010.HEIC"), 1512, 2016, "live", when="2025:01:07 12:00:00")
    f.video(os.path.join(v, "Family/IMG_0010.MOV"), 640, 480, 2)
    # The other wall, including a section whose NAME also exists on the visible one.
    f.photo(os.path.join(h, "Private/IMG_0005.jpg"), 1200, 900, "P1", when="2021:02:03 08:00:00")
    f.photo(os.path.join(h, "Family/IMG_0011.jpg"), 1200, 900, "HF", when="2022:02:03 08:00:00")
    point_at(DATA)
    # The shared library keeps its full-size files, so that the tests below can
    # read them. Removing them after conversion has tests of its own
    # (TestSmallCopiesOnly), in a library of its own.
    config.KEEP_ORIGINALS = True

    # Every conversion in this module happens here, with the library of
    # originals made read-only first. Any code path in the CONVERTER that tried
    # to write, rename or delete an original would fail.
    warnings.simplefilter("ignore", ResourceWarning)   # Flask's test client leaves files to the GC
    PROOF["before"] = tree_state(MEDIA)
    for folder, _, files in os.walk(MEDIA):
        for name in files:
            os.chmod(os.path.join(folder, name), stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        os.chmod(folder, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
    PROOF["writable_during_run"] = os.access(os.path.join(MEDIA, "Visible", "Family"), os.W_OK)
    PROOF["tally"] = convert_everything()
    PROOF["after"] = tree_state(MEDIA)
    for folder, _, _ in os.walk(MEDIA):
        os.chmod(folder, 0o755)
    auth.set_passwords(ADMIN, None)


def tearDownModule():
    library.close()
    for folder, dirs, _ in os.walk(ROOT):
        for d in dirs:
            os.chmod(os.path.join(folder, d), 0o755)
    shutil.rmtree(ROOT, ignore_errors=True)


class TestOriginalsUntouched(unittest.TestCase):
    """The converter reads originals and never alters them.

    setUpModule converted the whole library with the originals read-only at the
    filesystem level, and recorded every checksum before and after. These tests
    are the verdict on that run.
    """

    def test_the_run_really_was_read_only(self):
        self.assertFalse(PROOF["writable_during_run"], "the test did not lock the folder; it proves nothing")

    def test_everything_converted_without_needing_to_write_there(self):
        tally = PROOF["tally"]
        self.assertEqual(tally.get("error", 0), 0, library.failures())
        self.assertEqual(tally.get("ready"), 11)      # 9 photos + 2 videos
        self.assertEqual(tally.get("paired"), 1)      # the Live Photo's motion clip

    def test_every_original_is_byte_for_byte_identical(self):
        before, after = PROOF["before"], PROOF["after"]
        self.assertEqual(len(before), 12)
        self.assertEqual(sorted(before), sorted(after), "a file appeared or vanished among the originals")
        for name in before:
            self.assertEqual(before[name], after[name], f"{name} was modified (content, size or timestamp)")

    def test_the_web_server_never_opens_an_original(self):
        import inspect
        from waterfall import app as app_module
        source = inspect.getsource(app_module)
        self.assertNotIn("MEDIA_DIR", source, "only the small copies may be served")


class TestPipeline(unittest.TestCase):
    def test_heic_photo_gets_three_sizes_and_its_metadata(self):
        it = item_named("IMG_0001.HEIC")
        self.assertEqual((it["width"], it["height"]), (1512, 2016))
        self.assertEqual(it["taken_local"], "2024-06-01 10:00:00")
        self.assertEqual(it["taken_src"], "camera")
        self.assertEqual(it["meta"]["camera"], "Apple iPhone 15 Pro")
        self.assertAlmostEqual(it["meta"]["lat"], 59.91, places=2)
        folder = media.deriv_dir(it["key"])
        from PIL import Image
        sizes = {}
        for n in "tml":
            with Image.open(os.path.join(folder, f"{n}.webp")) as img:
                sizes[n] = img.size
        self.assertEqual(max(sizes["t"]), 400)
        self.assertEqual(max(sizes["m"]), 1680)
        self.assertEqual(sizes["l"], (1512, 2016))          # never enlarged
        self.assertLess(it["sizes"]["t"], it["sizes"]["m"])

    def test_sideways_photo_is_turned_upright(self):
        it = item_named("IMG_0007.jpg")
        self.assertEqual((it["width"], it["height"]), (1200, 1600))

    def test_plain_video_ladder_never_upscales(self):
        it = item_named("IMG_0008.mp4")
        self.assertEqual([r["name"] for r in it["rungs"]], ["360", "540", "720"])
        self.assertEqual((it["rungs"][-1]["w"], it["rungs"][-1]["h"]), (1280, 720))
        self.assertFalse(it["hdr"])
        self.assertAlmostEqual(it["duration"], 5.0, delta=0.3)

    def test_iphone_style_hdr_portrait_video(self):
        it = item_named("IMG_0009.MOV")
        self.assertTrue(it["hdr"], "HLG footage must be recognised, or it comes out grey")
        self.assertEqual((it["width"], it["height"]), (1080, 1920), "portrait must come out upright")
        self.assertEqual([r["name"] for r in it["rungs"]], ["360", "540", "720", "1080"])
        folder = os.path.join(media.deriv_dir(it["key"]), "v")
        info = media.video_frame_info(os.path.join(folder, "1080.m4s"))
        self.assertEqual((info["width"], info["height"]), (1080, 1920))
        self.assertEqual(info["trc"], "bt709", "the copies must be ordinary SDR video")
        with open(os.path.join(folder, "540.m3u8"), encoding="utf-8") as f:
            playlist = f.read()
        self.assertIn("#EXT-X-MAP:URI=\"540.m4s\"", playlist)
        self.assertIn("#EXT-X-BYTERANGE", playlist)
        self.assertIn("#EXT-X-ENDLIST", playlist)

    def test_video_has_a_small_silent_wall_preview(self):
        it = item_named("IMG_0008.mp4")
        preview = os.path.join(media.deriv_dir(it["key"]), "p.mp4")
        self.assertEqual(it["sizes"]["p"], os.path.getsize(preview))
        self.assertLess(it["sizes"]["p"], 600_000, "a preview must stay a fraction of a second of the link")
        info = media.video_frame_info(preview)
        self.assertFalse(info["has_audio"])
        self.assertEqual((info["width"], info["height"]), (640, 360))

    def test_a_large_original_gets_a_1440p_size(self):
        # The top rung: the first size that fills an iPhone's screen.
        src = fixtures.video(os.path.join(ROOT, "big", "IMG_0100.mp4"), 2560, 1440, 1)
        result = media.make_video(src, os.path.join(ROOT, "big-out"), fps=30)
        self.assertEqual([r["name"] for r in result["rungs"]], ["360", "540", "720", "1080", "1440"])
        top = result["rungs"][-1]
        self.assertEqual((top["w"], top["h"], top["codecs"].split(",")[0]), (2560, 1440, "avc1.640032"))
        made = media.video_frame_info(os.path.join(ROOT, "big-out", "v", "1440.m4s"))
        self.assertEqual((made["width"], made["height"]), (2560, 1440))

    def test_rungs_report_measured_bitrates(self):
        for r in item_named("IMG_0009.MOV")["rungs"]:
            self.assertGreater(r["avg"], 0)
            self.assertGreaterEqual(r["peak"], r["avg"] * 0.9)

    def test_live_photo_motion_clip_is_not_an_item(self):
        row = library.connect().execute("SELECT status FROM items WHERE name='IMG_0010.MOV'").fetchone()
        self.assertEqual(row["status"], "paired")
        names = [library.item(i, True)["name"] for i in ids("visible")]
        self.assertIn("IMG_0010.HEIC", names)
        self.assertNotIn("IMG_0010.MOV", names)

    def test_index_can_be_rebuilt_without_converting_again(self):
        # Throw the database away; the small copies describe themselves.
        scratch = os.path.join(ROOT, "rebuilt")
        os.makedirs(scratch)
        os.symlink(os.path.join(DATA, "d"), os.path.join(scratch, "d"))
        library.close()
        auth.forget_cached()
        config.DATA_DIR, config.DB_PATH = scratch, os.path.join(scratch, "waterfall.db")
        calls = []
        real_photo, real_video = media.make_photo, media.make_video
        media.make_photo = lambda *a, **k: calls.append("photo") or real_photo(*a, **k)
        media.make_video = lambda *a, **k: calls.append("video") or real_video(*a, **k)
        try:
            tally = convert_everything()
        finally:
            media.make_photo, media.make_video = real_photo, real_video
            point_at(DATA)
        self.assertEqual(tally.get("ready"), 11)
        self.assertEqual(calls, [], "nothing should have been converted a second time")


class TestFilesFromStrangers(unittest.TestCase):
    """The owner imports files they were sent. A file must not be able to send
    the converter anywhere."""

    def test_a_playlist_dressed_as_a_video_fetches_nothing(self):
        # A "video" that is really a list of addresses to open. ffmpeg will
        # follow one if it is allowed to; here it is held to the file itself.
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        listener.settimeout(0.2)
        port, calls, done = listener.getsockname()[1], [], threading.Event()

        def listen():
            while not done.is_set():
                try:
                    conn, _ = listener.accept()
                except OSError:
                    continue
                calls.append(1)
                conn.close()

        thread = threading.Thread(target=listen, daemon=True)
        thread.start()
        folder = tempfile.mkdtemp(prefix="waterfall-stranger-")
        try:
            for name in ("clip.mov", "clip.mp4", "clip.m3u8"):
                path = os.path.join(folder, name)
                with open(path, "w", encoding="utf-8") as f:
                    f.write("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:0\n"
                            f"#EXTINF:2.0,\nhttp://127.0.0.1:{port}/segment.ts\n#EXT-X-ENDLIST\n")
                with self.assertRaises(media.MediaError):
                    media.video_frame_info(path)
                with self.assertRaises(media.MediaError):
                    media.make_video(path, os.path.join(folder, "out-" + name))
        finally:
            done.set()
            thread.join()
            listener.close()
            shutil.rmtree(folder, ignore_errors=True)
        self.assertEqual(calls, [], "the converter went to the network because a file told it to")

    def test_every_command_that_opens_an_imported_file_is_held_to_that_file(self):
        info = {"width": 1280, "height": 720, "trc": "bt709", "pix": "yuv420p", "rotation": 0, "has_audio": False}
        planned = media.plan_ladder(1280, 720)
        for choice in (media.SOFTWARE, {"stage": "gpu-scale", "api": "vaapi", "driver": "iHD"},
                       {"stage": "decode", "api": "vaapi", "driver": None}):
            cmd = media._ladder_command("/in/clip.mov", "/out", info, 30, planned, choice)
            at = cmd.index("/in/clip.mov")
            self.assertEqual(cmd[at - 1], "-i")
            self.assertEqual(cmd[at - 3:at - 1], ["-protocol_whitelist", "file,crypto,data"], choice)


class TestRecipeUpgrade(unittest.TestCase):
    """A recipe change must not cost a second conversion when it need not."""

    def test_old_copies_gain_a_preview_without_reconverting(self):
        row = library.connect().execute("SELECT * FROM items WHERE name='IMG_0008.mp4'").fetchone()
        folder = media.deriv_dir(row["key"])
        done_file = os.path.join(folder, "done.json")
        with open(done_file, encoding="utf-8") as f:
            old = json.load(f)
        old["pipeline"] = 1                      # pretend the old recipe made these
        old["sizes"].pop("p")
        os.remove(os.path.join(folder, "p.mp4"))
        with open(done_file, "w", encoding="utf-8") as f:
            json.dump(old, f)
        calls = []
        real = media.make_video
        media.make_video = lambda *a, **k: calls.append(1) or real(*a, **k)
        try:
            self.assertEqual(library.convert(row), "ready")
        finally:
            media.make_video = real
        self.assertEqual(calls, [], "the ladder must not be made again")
        self.assertTrue(os.path.exists(os.path.join(folder, "p.mp4")))
        with open(done_file, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["pipeline"], PIPELINE)

    def test_a_larger_size_is_made_only_while_the_full_size_file_is_still_here(self):
        # Copies made before 1440p existed, from a 4K portrait clip.
        key = "ab" + "0" * 22
        folder = media.deriv_dir(key)
        os.makedirs(os.path.join(folder, "v"))
        rungs = [{"name": n, "w": 1, "h": 1, "bytes": 1, "peak": 1, "avg": 1, "codecs": "x"} for n in ("360", "1080")]
        for name in ("v/360.m4s", "p.mp4"):
            with open(os.path.join(folder, name), "wb") as f:
                f.write(b"x")
        old = {"pipeline": 2, "kind": "video", "width": 2160, "height": 3840, "duration": 5.0,
               "rungs": rungs, "sizes": {"t": 1, "m": 1, "p": 1}}

        def reset(**changes):
            with open(os.path.join(folder, "done.json"), "w", encoding="utf-8") as f:
                json.dump({**old, **changes}, f)

        reset()
        self.assertIsNone(library._upgrade_done(key, "video", have_original=True),
                          "the file is here and could have a 1440p size: make the ladder again")
        reset()
        kept = library._upgrade_done(key, "video", have_original=False)
        self.assertEqual((kept["pipeline"], [r["name"] for r in kept["rungs"]]), (PIPELINE, ["360", "1080"]),
                         "the file has gone: keep what exists, topping out at 1080p")
        reset(width=1080, height=1920)
        self.assertEqual(library._upgrade_done(key, "video", have_original=True)["pipeline"], PIPELINE,
                         "a 1080p original could never have a 1440p size: nothing to redo")
        shutil.rmtree(folder)


class TestPlans(unittest.TestCase):
    def test_short_clip_preview_uses_its_opening(self):
        self.assertEqual(media.preview_segments(3.0), [(0.0, 3.0)])
        self.assertEqual(media.preview_segments(8.0), [(0.0, 6.0)])

    def test_long_clip_preview_is_sampled_across_its_length(self):
        pieces = media.preview_segments(900.0)          # fifteen minutes
        self.assertEqual([start for start, _ in pieces], [90.0, 315.0, 540.0, 765.0])
        self.assertEqual(sum(length for _, length in pieces), 6.0)
        for start, length in media.preview_segments(8.1):
            self.assertLessEqual(start + length, 8.1)

    def test_ladder_sizes(self):
        self.assertEqual([(r.name, w, h) for r, w, h in media.plan_ladder(3840, 2160)],
                         [("360", 640, 360), ("540", 960, 540), ("720", 1280, 720), ("1080", 1920, 1080),
                          ("1440", 2560, 1440)])
        self.assertEqual([(w, h) for _, w, h in media.plan_ladder(2160, 3840)],
                         [(360, 640), (540, 960), (720, 1280), (1080, 1920), (1440, 2560)])
        self.assertEqual([r.name for r, _, _ in media.plan_ladder(1920, 1080)], ["360", "540", "720", "1080"],
                         "a 1080p original gets no made-up 1440p")
        self.assertEqual([(w, h) for _, w, h in media.plan_ladder(480, 270)], [(480, 270)])
        for _, w, h in media.plan_ladder(1437, 1079):
            self.assertEqual((w % 2, h % 2), (0, 0), "H.264 needs even dimensions")

    def test_dates_keep_their_timezone_for_display_and_lose_it_for_sorting(self):
        local, ts = media._parse_date("2025:07:04 17:30:00+02:00")
        self.assertEqual((local, ts), ("2025-07-04 17:30:00", 1751643000.0))     # 15:30 UTC
        self.assertEqual(media._parse_date("2025:07:04 17:30:00.123Z")[0], "2025-07-04 17:30:00")
        for junk in ("0000:00:00 00:00:00", "not a date", None):
            self.assertIsNone(media._parse_date(junk))


class TestHardwarePath(unittest.TestCase):
    """The video chip is used only if a real test conversion proves it works,
    and a file it trips on is redone in software."""

    def test_filters_for_a_portrait_clip_resized_on_the_chip(self):
        info = {"width": 2160, "height": 3840, "pix": "yuv420p10le", "rotation": 90, "trc": "arib-std-b67"}
        chip = {"stage": "gpu-scale", "api": "vaapi", "driver": "iHD"}
        head = media._head(chip, info, 59.94, 1080, 1920)
        # Halve the frame rate, resize on the chip as STORED (sideways), bring it
        # back as 10-bit, then turn it upright.
        self.assertEqual(head, ["fps=30", "scale_vaapi=w=1920:h=1080:format=p010", "hwdownload",
                                "format=p010le", "transpose=1"])
        args = media._hw_input(chip)
        self.assertEqual(args[:4], ["-hwaccel", "vaapi", "-hwaccel_device", config.VAAPI_DEVICE])
        self.assertIn("-noautorotate", args)
        self.assertEqual(args[-2:], ["-display_rotation:v:0", "0"], "or the result is turned twice")
        self.assertEqual(media._hw_env(chip)["LIBVA_DRIVER_NAME"], "iHD")
        self.assertEqual(media._head(dict(chip, stage="decode"), info, 30, 1080, 1920), ["scale=1080:1920:flags=bicubic"])
        self.assertEqual(media._hw_input(media.SOFTWARE), [])

    def test_a_chip_that_fails_falls_back_to_software_for_that_file(self):
        src = os.path.join(MEDIA, "Visible", "Family", "IMG_0008.mp4")
        out = os.path.join(ROOT, "fallback")
        saved = dict(media.hardware)
        # A device that does not exist: the chip attempt must fail, the file must not.
        media.hardware.update(use={"stage": "decode", "api": "vaapi", "driver": None}, fallbacks=0)
        try:
            result = media.make_video(src, out, fps=30)
            self.assertEqual(media.hardware["fallbacks"], 1)
        finally:
            media.hardware.clear()
            media.hardware.update(saved)
        self.assertEqual(result["made_by"], "software")
        self.assertEqual([r["name"] for r in result["rungs"]], ["360", "540", "720"])

    def test_the_self_test_rejects_a_wrong_picture(self):
        a = bytes([10] * 2304)
        self.assertTrue(media._same_picture(a, bytes([14] * 2304)))
        self.assertFalse(media._same_picture(a, bytes([200] * 2304)))
        self.assertFalse(media._same_picture(a, None))

    def test_no_chip_means_software_and_says_why(self):
        saved_mode, saved_dev = config.HWACCEL, config.VAAPI_DEVICE
        try:
            config.HWACCEL = "off"
            self.assertEqual(media._candidates()[0], [])
            self.assertIn("switched off", media._candidates()[1])
        finally:
            config.HWACCEL, config.VAAPI_DEVICE = saved_mode, saved_dev


class TestPlaces(unittest.TestCase):
    """City and country from a position, with no internet connection."""

    def test_names_the_city_a_person_would(self):
        self.assertEqual(places.nearest(48.8584, 2.2945), "Paris, France")          # not "Paris 16 Passy"
        self.assertEqual(places.nearest(35.6812, 139.7671), "Tokyo, Japan")         # not the ward of Chuo
        self.assertEqual(places.nearest(59.91, 10.75), "Oslo, Norway")
        self.assertEqual(places.nearest(48.8049, 2.1204), "Versailles, France")     # a town keeps its name

    def test_a_hand_made_name_wins_inside_its_circle_and_only_there(self):
        places._load()
        saved = list(places._preferred)
        # A park beside a town, and one building inside the park.
        places._preferred[:] = [(48.8070, 2.1100, "Palace Gardens", "FR", 2.5),
                                (48.8146, 2.1048, "Grand Trianon", "FR", 0.4)]
        try:
            self.assertEqual(places.nearest(48.8146, 2.1050), "Grand Trianon, France")
            self.assertEqual(places.nearest(48.8000, 2.1000), "Palace Gardens, France")
            self.assertEqual(places.nearest(48.8584, 2.2945), "Paris, France")
            self.assertEqual(places.nearest(35.6812, 139.7671), "Tokyo, Japan")
        finally:
            places._preferred[:] = saved

    def test_your_own_names_are_read_from_the_data_folder(self):
        folder = tempfile.mkdtemp(prefix="waterfall-places-")
        mine = os.path.join(folder, "places-preferred.tsv")
        with open(mine, "w", encoding="utf-8") as f:
            f.write("# my places\nGrand Trianon\tFR\t48.8146\t2.1048\t0.4\ta note\n"
                    "this line is damaged\nOrangery\tFR\t48.8020\t2.1180\t0.2\n")
        saved, config.PLACES_FILE, places._grid = config.PLACES_FILE, mine, None
        try:
            self.assertEqual(places.nearest(48.8146, 2.1050), "Grand Trianon, France")
            self.assertEqual(places.nearest(48.8020, 2.1180), "Orangery, France",
                             "a damaged line must lose only itself")
        finally:
            config.PLACES_FILE, places._grid = saved, None
            shutil.rmtree(folder, ignore_errors=True)
        self.assertNotIn("Trianon", places.nearest(48.8146, 2.1050))

    def test_every_line_of_the_list_that_ships_is_read(self):
        path = os.path.join(os.path.dirname(places.__file__), "data", "places-preferred.tsv")
        with open(path, encoding="utf-8") as f:
            rows = [line for line in f if line.strip() and not line.startswith("#")]
        places._load()
        self.assertEqual(len(places._preferred), len(rows))

    def test_the_list_of_towns_says_where_it_came_from(self):
        # GeoNames is CC BY 4.0: the credit has to travel with the data.
        path = os.path.join(os.path.dirname(places.__file__), "data", "ATTRIBUTION.txt")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for needed in ("GeoNames", "geonames.org", "creativecommons.org/licenses/by/4.0"):
            self.assertIn(needed, text)

    def test_says_nothing_rather_than_guess(self):
        self.assertIsNone(places.nearest(30.0, -40.0))      # mid-Atlantic
        self.assertIsNone(places.nearest(None, None))
        self.assertIsNone(places.nearest(999, 999))

    def test_details_carry_the_place(self):
        from waterfall.app import app
        c = app.test_client()
        d = c.get(f"/api/item/{item_named('IMG_0001.HEIC')['id']}").get_json()
        self.assertEqual(d["place"], "Oslo, Norway")


class TestWalls(unittest.TestCase):
    """Two roots, never mixed; a folder's root decides who sees it."""

    def test_each_wall_lists_only_its_own_sections(self):
        visible, hidden = section_ids("visible"), section_ids("hidden")
        self.assertEqual(set(visible), {"", "Family", "Travel", "Travel/Japan", "Travel/Norway"})
        self.assertEqual(set(hidden), {"Family", "Private"})
        self.assertNotEqual(visible["Family"], hidden["Family"], "same name, different sections")
        by_path = {s["path"]: s for s in library.sections("visible")}
        self.assertEqual(by_path[""]["name"], "Unsorted")
        self.assertEqual((by_path["Travel"]["photos"], by_path["Travel"]["total_photos"]), (0, 3))
        self.assertEqual(by_path["Travel"]["total_videos"], 1)
        self.assertEqual((by_path["Travel"]["depth"], by_path["Travel/Norway"]["depth"]), (0, 1))

    def test_feeds_never_mix(self):
        self.assertEqual(len(ids("visible")), 9)
        self.assertEqual(len(ids("hidden")), 2)
        self.assertFalse(set(ids("visible")) & set(ids("hidden")))
        self.assertEqual(library.totals("visible"), {"photos": 7, "videos": 2})
        self.assertEqual(library.totals("hidden"), {"photos": 2, "videos": 0})

    def test_a_section_from_one_wall_cannot_be_read_through_the_other(self):
        hidden = section_ids("hidden")
        self.assertEqual(ids("visible", section=hidden["Private"]), [])
        self.assertEqual(len(ids("hidden", section=hidden["Private"])), 1)

    def test_a_section_feed_includes_everything_beneath_it(self):
        sec = section_ids("visible")
        self.assertEqual(len(ids("visible", section=sec["Travel"])), 4)
        self.assertEqual(len(ids("visible", section=sec["Travel/Norway"])), 2)
        self.assertEqual(len(ids("visible", section=sec[""])), 1)      # "Unsorted": the loose file only

    def test_hidden_items_and_their_files_need_permission(self):
        secret = item_named("IMG_0005.jpg")
        self.assertEqual(secret["realm"], "hidden")
        self.assertIsNone(library.item(secret["id"], allow_hidden=False))
        self.assertIsNone(library.find_ready_by_key(secret["key"], allow_hidden=False))
        self.assertIsNotNone(library.find_ready_by_key(secret["key"], allow_hidden=True))
        public = item_named("IMG_0004.HEIC", hidden_ok=False)
        self.assertEqual(public["realm"], "visible")
        self.assertIsNotNone(library.find_ready_by_key(public["key"], allow_hidden=False))

    def test_shuffle_is_stable_complete_and_seeded(self):
        def walk(seed):
            got, offset = [], 0
            while True:
                page = library.feed("visible", None, "shuffle", seed, offset, 4)
                got += [i["id"] for i in page]
                offset += 4
                if len(page) < 4:
                    return got
        first = walk(42)
        self.assertEqual(len(first), 9)
        self.assertEqual(len(set(first)), 9, "paging repeated or skipped an item")
        self.assertEqual(first, walk(42), "the same seed must give the same order")
        self.assertNotEqual(first, walk(43), "a new seed must reshuffle")

    def test_orders_and_kinds(self):
        con = library.connect()
        when = lambda i: con.execute("SELECT taken_ts FROM items WHERE id=?", (i,)).fetchone()[0]
        new = [when(i) for i in ids("visible", order="new")]
        self.assertEqual(new, sorted(new, reverse=True))
        old = [when(i) for i in ids("visible", order="old")]
        self.assertEqual(old, sorted(old))
        self.assertEqual(len(ids("visible", kind="video")), 2)
        self.assertEqual(len(ids("visible", kind="photo")), 7)


class TestTags(unittest.TestCase):
    """One home folder; tags add an item to other categories without a copy."""

    def setUp(self):
        con = library.connect()
        with con:
            con.execute("DELETE FROM tags")
        for folder, _, files in os.walk(config.DERIV_DIR):
            if "tags.json" in files:
                os.remove(os.path.join(folder, "tags.json"))

    tearDown = setUp

    def test_a_tag_named_after_a_section_shows_the_item_there_too(self):
        norway = item_named("IMG_0001.HEIC")
        family = section_ids("visible")["Family"]
        before = ids("visible", section=family)
        self.assertNotIn(norway["id"], before)
        self.assertEqual(library.set_tag(norway["id"], "#family", True), ["family"])
        self.assertEqual(set(ids("visible", section=family)), set(before) | {norway["id"]})
        counted = {s["path"]: s for s in library.sections("visible")}["Family"]
        self.assertEqual(counted["total_photos"] + counted["total_videos"], len(before) + 1)
        # It still lives where it lived, and there is still one file.
        self.assertEqual(item_named("IMG_0001.HEIC")["section_path"], "Travel/Norway")
        self.assertEqual([t["tag"] for t in library.tag_list("visible")], [],
                         "a tag that matches a section is shown inside it, not listed again")

    def test_a_new_word_becomes_a_category_of_its_own(self):
        a, b = item_named("IMG_0001.HEIC"), item_named("IMG_0008.mp4")
        library.set_tag(a["id"], "Best of", True)
        library.set_tag(b["id"], "best of", True)            # same tag, typed differently
        listed = library.tag_list("visible")
        self.assertEqual([(t["tag"].lower(), t["photos"], t["videos"]) for t in listed], [("best of", 1, 1)])
        self.assertEqual(set(ids("visible", tag="BEST OF")), {a["id"], b["id"]})
        library.set_tag(a["id"], "Best of", False)
        self.assertEqual(ids("visible", tag="Best of"), [b["id"]])

    def test_tags_never_cross_between_the_walls(self):
        secret = item_named("IMG_0005.jpg")
        library.set_tag(secret["id"], "Family", True)        # a section name on BOTH walls
        library.set_tag(secret["id"], "Holiday", True)
        visible_family = section_ids("visible")["Family"]
        self.assertNotIn(secret["id"], ids("visible", section=visible_family))
        self.assertEqual(ids("visible", tag="Holiday"), [])
        self.assertEqual([t["tag"] for t in library.tag_list("visible")], [],
                         "the name of a tag used only on hidden items must not show on the visible wall")
        self.assertNotIn("Holiday", library.tag_suggestions("visible"))
        self.assertIn(secret["id"], ids("hidden", section=section_ids("hidden")["Family"]))
        self.assertEqual(ids("hidden", tag="Holiday"), [secret["id"]])

    def test_tags_survive_the_index_being_thrown_away(self):
        it = item_named("IMG_0003.HEIC")
        library.set_tag(it["id"], "Keep me", True)
        con = library.connect()
        with con:
            con.execute("DELETE FROM tags")
            con.execute("UPDATE items SET status='new' WHERE id=?", (it["id"],))
        convert_everything()
        self.assertEqual(item_named("IMG_0003.HEIC")["tags"], ["Keep me"])

    def test_bad_tags_are_refused(self):
        it = item_named("IMG_0001.HEIC")
        for bad in ("", "   ", "#", "a/b", "x" * 41):
            with self.assertRaises(ValueError):
                library.set_tag(it["id"], bad, True)
        self.assertIsNone(library.set_tag(999999, "ok", True))
        self.assertEqual(library.clean_tag("  #Two   words "), "Two words")


class TestAuth(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="waterfall-auth-")
        point_at(self.dir, os.path.join(self.dir, "media"))
        auth._FAILS.clear()

    def tearDown(self):
        point_at(DATA)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_admin_password_and_optional_viewing_password(self):
        self.assertFalse(auth.is_configured())
        auth.set_passwords(ADMIN, None)
        self.assertTrue(auth.is_configured())
        self.assertFalse(auth.has_viewer())
        self.assertEqual(auth.check(ADMIN, "a"), "admin")
        self.assertIsNone(auth.check("something else", "a"))
        auth.set_passwords(None, VIEWER)
        self.assertTrue(auth.has_viewer())
        self.assertEqual(auth.check(VIEWER, "a"), "viewer")
        self.assertNotIn(ADMIN, json.dumps(library.get_setting("admin_hash")))
        auth.set_passwords(None, None, clear_viewer=True)
        self.assertFalse(auth.has_viewer())

    def test_passwords_must_differ_and_be_long_enough(self):
        with self.assertRaises(ValueError):
            auth.set_passwords("same-password", "same-password")
        with self.assertRaises(ValueError):
            auth.set_passwords("short", None)
        auth.set_passwords(ADMIN, VIEWER)
        with self.assertRaises(ValueError):
            auth.set_passwords(None, ADMIN)          # viewer := current admin password
        with self.assertRaises(ValueError):
            auth.set_passwords(VIEWER, None)         # admin := current viewer password

    def test_changing_a_password_signs_everyone_out(self):
        auth.set_passwords(ADMIN, VIEWER)
        token = auth.issue("admin")
        self.assertEqual(auth.role_of(token), "admin")
        auth.set_passwords("another-admin", None)
        self.assertIsNone(auth.role_of(token))
        self.assertIsNone(auth.role_of("forged.token.value"))
        self.assertIsNone(auth.role_of(None))

    def test_guessing_is_throttled_at_sign_in_and_at_the_import_door(self):
        auth.set_passwords(ADMIN, None)
        for _ in range(auth.FAIL_LIMIT):
            self.assertIsNone(auth.check("wrong", "10.0.0.9"))
        self.assertGreater(auth.throttled("10.0.0.9"), 0)
        self.assertFalse(auth.check_admin(ADMIN, "10.0.0.9"), "even the right password must wait")
        self.assertEqual(auth.throttled("10.0.0.10"), 0)
        self.assertTrue(auth.check_admin(ADMIN, "10.0.0.10"))

    def test_signing_in_as_a_viewer_does_not_buy_more_guesses(self):
        # A viewer is exactly who might try to guess the admin password.
        auth.set_passwords(ADMIN, VIEWER)
        for _ in range(auth.FAIL_LIMIT - 1):
            self.assertIsNone(auth.check("wrong", "10.0.0.30"))
        self.assertEqual(auth.check(VIEWER, "10.0.0.30"), "viewer")
        self.assertIsNone(auth.check("wrong", "10.0.0.30"))
        self.assertGreater(auth.throttled("10.0.0.30"), 0)
        self.assertIsNone(auth.check(ADMIN, "10.0.0.30"), "even the right password must wait")
        # The admin's own right password does wipe the slate, as it should.
        for _ in range(auth.FAIL_LIMIT - 1):
            auth.check("wrong", "10.0.0.31")
        self.assertEqual(auth.check(ADMIN, "10.0.0.31"), "admin")
        self.assertNotIn("10.0.0.31", auth._FAILS)

    def test_a_burst_of_guesses_is_counted_before_it_is_checked(self):
        # Thirty guesses sent at the same moment. If each were only counted once
        # checked, all thirty would be checked before the first was counted.
        auth.set_passwords(ADMIN, None)
        checked, real = [], auth._matches

        def slow(password, stored):
            checked.append(1)
            time.sleep(0.05)
            return real(password, stored)

        def guess():
            try:
                auth.check_admin("wrong", "10.0.0.40")
            finally:
                library.close()

        auth._matches = slow
        try:
            threads = [threading.Thread(target=guess) for _ in range(30)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            auth._matches = real
        self.assertEqual(len(checked), auth.FAIL_LIMIT)

    def test_the_list_of_addresses_cannot_grow_without_end(self):
        auth.set_passwords(ADMIN, None)
        saved, auth.FAILS_KEPT = auth.FAILS_KEPT, 50
        try:
            long_ago = time.time() - auth.FAIL_WINDOW - 1
            auth._FAILS.update({f"old-{i}": [long_ago] for i in range(50)})
            self.assertTrue(auth.check_admin(ADMIN, "new-address"), "expired guesses must be swept, not counted")
            self.assertEqual(len(auth._FAILS), 0)
            auth._FAILS.update({f"live-{i}": [time.time()] for i in range(50)})
            self.assertFalse(auth.check_admin(ADMIN, "one-more-address"), "full of live guessers: wait")
            self.assertEqual(len(auth._FAILS), 50)
        finally:
            auth.FAILS_KEPT = saved


class TestHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        point_at(DATA)
        from waterfall.app import app
        cls.app = app

    def setUp(self):
        auth._FAILS.clear()
        auth.set_passwords(None, None, clear_viewer=True)      # the normal arrangement: open viewing

    def client(self, password: str | None = None):
        c = self.app.test_client()
        if password:
            r = c.post("/api/login", json={"password": password}, headers={"X-Waterfall": "1"})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c

    def test_opening_the_address_shows_the_visible_wall_without_signing_in(self):
        c = self.client()
        me = c.get("/api/me").get_json()
        self.assertEqual((me["role"], me["admin"], me["configured"]), ("viewer", False, True))
        self.assertEqual(len(c.get("/api/feed?order=new&limit=100").get_json()["items"]), 9)
        paths = [s["path"] for s in c.get("/api/sections").get_json()["sections"]]
        self.assertIn("Travel/Norway", paths)
        self.assertNotIn("Private", paths)
        key = item_named("IMG_0004.HEIC")["key"]
        self.assertEqual(c.get(f"/m/{PIPELINE}/{key}/t.webp").status_code, 200)

    def test_nothing_hidden_is_reachable_without_the_admin_password(self):
        secret = item_named("IMG_0005.jpg")
        private = section_ids("hidden")["Private"]
        c = self.client()
        for url in ("/api/feed?realm=hidden", "/api/sections?realm=hidden",
                    f"/api/feed?realm=hidden&section={private}"):
            self.assertEqual(c.get(url).status_code, 403, url)
        self.assertEqual(c.get(f"/api/feed?section={private}").get_json()["items"], [],
                         "a hidden section's id must not work on the visible wall")
        self.assertEqual(c.get(f"/api/item/{secret['id']}").status_code, 404)
        for name in ("t.webp", "m.webp", "l.webp"):
            self.assertEqual(c.get(f"/m/{PIPELINE}/{secret['key']}/{name}").status_code, 404,
                             "knowing the address must not be enough")
        self.assertEqual(c.get("/api/admin/status").status_code, 403)
        every = json.dumps(c.get("/api/feed?limit=100").get_json()) + json.dumps(c.get("/api/sections").get_json())
        self.assertNotIn(secret["key"], every)
        self.assertNotIn("Private", every)

    def test_the_admin_password_opens_the_hidden_wall(self):
        secret = item_named("IMG_0005.jpg")
        c = self.client(ADMIN)
        self.assertTrue(c.get("/api/me").get_json()["admin"])
        self.assertEqual(len(c.get("/api/feed?realm=hidden&limit=100").get_json()["items"]), 2)
        self.assertEqual(len(c.get("/api/feed?realm=visible&limit=100").get_json()["items"]), 9)
        self.assertEqual(c.get(f"/m/{PIPELINE}/{secret['key']}/t.webp").status_code, 200)
        self.assertEqual(c.get(f"/api/item/{secret['id']}").get_json()["realm"], "hidden")
        c.post("/api/logout", headers={"X-Waterfall": "1"})
        self.assertEqual(c.get("/api/feed?realm=hidden").status_code, 403, "locking must lock")

    def test_the_health_check_says_how_much_there_is_only_to_the_admin(self):
        # Its count covers both walls, so to a stranger it would give away how
        # much is on the Hidden one.
        stranger = self.client().get("/healthz").get_json()
        self.assertEqual(set(stranger), {"ok", "version"})
        self.assertTrue(stranger["ok"])
        admin = self.client(ADMIN).get("/healthz").get_json()
        self.assertEqual(admin["ready"], library.counts()["ready"])
        self.assertGreater(admin["ready"], len(self.client().get("/api/feed?limit=100").get_json()["items"]))

    def test_the_page_may_not_be_framed_or_load_anything_from_elsewhere(self):
        r = self.client().get("/")
        policy = r.headers["Content-Security-Policy"]
        for needed in ("default-src 'self'", "frame-ancestors 'none'", "object-src 'none'", "base-uri 'none'"):
            self.assertIn(needed, policy)
        self.assertNotIn("unsafe", policy)
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        # ...and the page has to be one that policy allows: no script or style
        # written into it, and nothing fetched from another site.
        page = r.get_data(as_text=True)
        self.assertNotIn("<style", page)
        self.assertNotRegex(page, r"<script(?![^>]*\bsrc=)")
        self.assertNotRegex(page, r"\son[a-z]+\s*=")
        self.assertNotRegex(page, r"(src|href)=\"(https?:)?//")
        self.assertEqual(self.client().get("/api/me").headers["X-Frame-Options"], "DENY")

    def test_a_request_far_larger_than_a_password_is_refused(self):
        from waterfall.app import MAX_BODY
        r = self.client().post("/api/login", data=b"x" * (MAX_BODY + 1),
                               headers={"X-Waterfall": "1", "Content-Type": "application/json"})
        self.assertEqual(r.status_code, 413)

    def test_signing_out_needs_our_own_header(self):
        c = self.client(ADMIN)
        self.assertEqual(c.post("/api/logout").status_code, 400)       # as a foreign page would
        self.assertTrue(c.get("/api/me").get_json()["admin"])

    def test_a_proxy_is_believed_only_when_one_is_named(self):
        from waitress.adjustments import Adjustments
        from waterfall.app import MAX_BODY, serve_options
        plain = serve_options()
        self.assertNotIn("trusted_proxy", plain)
        self.assertEqual(plain["max_request_body_size"], MAX_BODY)
        config.TRUSTED_PROXY = "172.17.0.1"
        try:
            behind = serve_options()
        finally:
            config.TRUSTED_PROXY = ""
        self.assertEqual(behind["trusted_proxy"], "172.17.0.1")
        self.assertTrue(behind["clear_untrusted_proxy_headers"])
        for options in (plain, behind):                 # the web server accepts them as written
            self.assertEqual(Adjustments(**{**options, "port": 0}).max_request_body_size, MAX_BODY)

    def test_wrong_password_and_setup_cannot_be_repeated(self):
        c = self.client()
        r = c.post("/api/login", json={"password": "nope"}, headers={"X-Waterfall": "1"})
        self.assertEqual(r.status_code, 401)
        r = c.post("/api/setup", json={"admin": "takeover-attempt"}, headers={"X-Waterfall": "1"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(auth.check(ADMIN, "x"), "admin")

    def test_an_optional_viewing_password_closes_the_wall(self):
        auth.set_passwords(None, VIEWER)
        c = self.client()
        self.assertIsNone(c.get("/api/me").get_json()["role"])
        key = item_named("IMG_0004.HEIC")["key"]
        for url in ("/api/feed", "/api/sections", f"/m/{PIPELINE}/{key}/t.webp"):
            self.assertEqual(c.get(url).status_code, 401, url)
        v = self.client(VIEWER)
        self.assertEqual(v.get("/api/feed").status_code, 200)
        self.assertEqual(v.get("/api/feed?realm=hidden").status_code, 403)

    def test_changes_need_the_admin_and_our_own_header(self):
        it = item_named("IMG_0001.HEIC")
        viewer, admin = self.client(), self.client(ADMIN)
        for url, body in (("/api/admin/rescan", {}), ("/api/admin/passwords", {"admin": "hijack-it"}),
                          ("/api/admin/tag", {"id": it["id"], "tag": "x", "on": True})):
            self.assertEqual(viewer.post(url, json=body, headers={"X-Waterfall": "1"}).status_code, 403, url)
        r = admin.post("/api/admin/tag", json={"id": it["id"], "tag": "x", "on": True})   # as a foreign form would
        self.assertEqual(r.status_code, 400)
        r = admin.post("/api/admin/tag", json={"id": it["id"], "tag": "Trip", "on": True}, headers={"X-Waterfall": "1"})
        self.assertEqual(r.get_json()["tags"], ["Trip"])
        self.assertEqual(viewer.get(f"/api/item/{it['id']}").get_json()["tags"], ["Trip"])
        self.assertNotIn("suggest", viewer.get(f"/api/item/{it['id']}").get_json())
        self.assertEqual(len(viewer.get("/api/feed?tag=trip").get_json()["items"]), 1)
        admin.post("/api/admin/tag", json={"id": it["id"], "tag": "Trip", "on": False}, headers={"X-Waterfall": "1"})
        self.assertEqual(viewer.get("/api/feed?tag=trip").get_json()["items"], [])

    def test_media_is_cacheable_forever_and_supports_ranges(self):
        c = self.client()
        photo, video = item_named("IMG_0004.HEIC")["key"], item_named("IMG_0009.MOV")["key"]
        r = c.get(f"/m/{PIPELINE}/{photo}/m.webp")
        self.assertEqual((r.status_code, r.mimetype), (200, "image/webp"))
        self.assertEqual(r.headers["Cache-Control"], "private, max-age=31536000, immutable")
        r = c.get(f"/m/{PIPELINE}/{video}/p.mp4", headers={"Range": "bytes=0-9"})
        self.assertEqual((r.status_code, r.mimetype, len(r.data)), (206, "video/mp4", 10))
        r = c.get(f"/m/{PIPELINE}/{video}/v/540.m4s", headers={"Range": "bytes=0-99"})
        self.assertEqual((r.status_code, len(r.data)), (206, 100))
        self.assertTrue(r.headers["Content-Range"].startswith("bytes 0-99/"))
        self.assertEqual(c.get(f"/m/{PIPELINE}/{video}/v/720.m3u8").mimetype, "application/vnd.apple.mpegurl")

    def test_internal_files_and_other_paths_are_not_served(self):
        c = self.client(ADMIN)
        photo = item_named("IMG_0004.HEIC")["key"]
        for name in ("done.json", "tags.json", "../../waterfall.db", "v/9999.m4s", "x.webp", "v/"):
            self.assertEqual(c.get(f"/m/{PIPELINE}/{photo}/{name}").status_code, 404, name)
        self.assertEqual(c.get(f"/m/{PIPELINE + 1}/{photo}/t.webp").status_code, 404)
        self.assertEqual(c.get(f"/m/{PIPELINE}/{'0' * 24}/t.webp").status_code, 404)
        self.assertEqual(c.get(f"/m/{PIPELINE}/not-a-key/t.webp").status_code, 404)

    def test_master_playlist_follows_minimum_and_link_speed(self):
        c = self.client()
        video = item_named("IMG_0009.MOV")["key"]

        def offered(query):
            text = c.get(f"/m/{PIPELINE}/{video}/v/master.m3u8?{query}").get_data(as_text=True)
            return [line.split(".")[0] for line in text.splitlines() if line.endswith(".m3u8")]

        self.assertEqual(sorted(offered("min=0&bw=100000"), key=int), ["360", "540", "720", "1080"])
        self.assertEqual(offered("min=0&bw=100000")[0], "360", "a slow link starts at the bottom")
        self.assertEqual(offered("min=0&bw=900000000")[0], "1080", "a fast link starts at the top")
        self.assertEqual(sorted(offered("min=720&bw=100000"), key=int), ["720", "1080"],
                         "sizes below the chosen minimum must not be offered at all")
        self.assertEqual(offered("min=4000&bw=100000"), ["1080"], "an impossible minimum still plays")

    def test_details_have_what_the_details_button_promises(self):
        d = self.client().get(f"/api/item/{item_named('IMG_0001.HEIC')['id']}").get_json()
        for field in ("name", "taken_local", "section_path", "place", "width", "height", "size", "meta", "sizes", "tags"):
            self.assertIn(field, d)
        self.assertEqual(d["section_path"], "Travel/Norway")
        self.assertNotIn("rel_path", d)


class TestSmallCopiesOnly(unittest.TestCase):
    """Small copies only. Once an item's small copies are
    checked, the full-size file is removed and a stand-in is left."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="waterfall-small-")
        point_at(os.path.join(cls.dir, "data"), os.path.join(cls.dir, "data", "media"))
        config.KEEP_ORIGINALS = False
        cls.folder = os.path.join(config.MEDIA_DIR, "Visible", "Trip")
        fixtures.photo(os.path.join(cls.folder, "IMG_8001.HEIC"), 1512, 2016, "one", when="2024:05:05 10:00:00")
        fixtures.video(os.path.join(cls.folder, "IMG_8002.mp4"), 640, 360, 2)
        fixtures.photo(os.path.join(cls.folder, "IMG_8003.HEIC"), 1512, 2016, "live", when="2024:05:06 10:00:00")
        fixtures.video(os.path.join(cls.folder, "IMG_8003.MOV"), 640, 480, 2)       # its Live Photo half
        with open(os.path.join(cls.folder, "IMG_8004.jpg"), "wb") as f:
            f.write(b"this is not a picture")
        cls.tally = convert_everything()

    @classmethod
    def tearDownClass(cls):
        config.KEEP_ORIGINALS = True
        point_at(DATA)
        shutil.rmtree(cls.dir, ignore_errors=True)

    def listing(self):
        return sorted(os.listdir(self.folder))

    def test_1_full_size_files_are_gone_and_stand_ins_are_left(self):
        self.assertEqual(self.tally.get("ready"), 3)
        self.assertEqual(self.listing(), ["IMG_8001.HEIC.waterfall.txt", "IMG_8002.mp4.waterfall.txt",
                                          "IMG_8003.HEIC.waterfall.txt", "IMG_8004.jpg"],
                         "converted files become stand-ins; the Live Photo's motion half is not kept; "
                         "a file that FAILED to convert is left exactly as it was")
        with open(os.path.join(self.folder, "IMG_8004.jpg"), "rb") as f:
            self.assertEqual(f.read(), b"this is not a picture")
        with open(os.path.join(self.folder, "IMG_8002.mp4.waterfall.txt"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("stands in for IMG_8002.mp4", text)
        self.assertIn("removed the full-size file", text)
        stub = library.read_stub(os.path.join(self.folder, "IMG_8002.mp4.waterfall.txt"))
        self.assertEqual((stub["kind"], stub["name"]), ("video", "IMG_8002.mp4"))

    def test_2_the_items_are_still_there_and_still_play(self):
        from waterfall.app import app
        auth.set_passwords(ADMIN, None)
        c = app.test_client()
        names = {library.item(i, True)["name"] for i in ids("visible")}
        self.assertEqual(names, {"IMG_8001.HEIC", "IMG_8002.mp4", "IMG_8003.HEIC"})
        video = item_named("IMG_8002.mp4")
        self.assertTrue(video["trashed"])
        self.assertEqual(c.get(f"/m/{PIPELINE}/{video['key']}/v/360.m4s", headers={"Range": "bytes=0-9"}).status_code, 206)
        self.assertEqual(c.get(f"/m/{PIPELINE}/{item_named('IMG_8001.HEIC')['key']}/l.webp").status_code, 200)
        counted = library.counts()
        self.assertEqual((counted["trashed"], counted["original_bytes"]), (3, 21),
                         "only the failed file's 21 bytes are still held at full size")
        self.assertGreater(counted["copy_bytes"], 0)

    def test_3_nothing_is_removed_unless_every_copy_is_really_there(self):
        folder = os.path.join(config.MEDIA_DIR, "Visible", "Careful")
        path = fixtures.photo(os.path.join(folder, "IMG_8010.jpg"), 1200, 900, "careful")
        real = library.copies_complete
        library.copies_complete = lambda key, done: False       # as if a copy were missing or short
        try:
            convert_everything()
        finally:
            library.copies_complete = real
        self.assertTrue(os.path.isfile(path), "the full-size file must stay")
        self.assertFalse(os.path.exists(path + config.STUB))
        key = item_named("IMG_8010.jpg")["key"]
        with open(library._done_path(key), encoding="utf-8") as f:
            done = json.load(f)
        self.assertTrue(real(key, done))
        os.truncate(os.path.join(media.deriv_dir(key), "m.webp"), 10)
        self.assertFalse(real(key, done), "a copy that is the wrong size is not a copy")

    def test_4_the_index_can_be_rebuilt_from_stand_ins_alone(self):
        before = {library.item(i, True)["name"]: library.item(i, True)["key"] for i in ids("visible")}
        library.close()
        os.remove(config.DB_PATH)
        for extra in ("-wal", "-shm"):
            if os.path.exists(config.DB_PATH + extra):
                os.remove(config.DB_PATH + extra)
        calls = []
        real_photo, real_video = media.make_photo, media.make_video
        media.make_photo = lambda *a, **k: calls.append(os.path.basename(a[0])) or real_photo(*a, **k)
        media.make_video = lambda *a, **k: calls.append(os.path.basename(a[0])) or real_video(*a, **k)
        try:
            convert_everything()
        finally:
            media.make_photo, media.make_video = real_photo, real_video
        after = {library.item(i, True)["name"]: library.item(i, True)["key"] for i in ids("visible")}
        self.assertEqual({n: k for n, k in after.items() if n.startswith("IMG_800")},
                         {n: k for n, k in before.items() if n.startswith("IMG_800")})
        # The broken file is tried again (and fails again); nothing that had
        # been converted is converted a second time, and nothing could be.
        self.assertEqual(set(calls), {"IMG_8004.jpg"})

    def test_5_moving_or_deleting_the_stand_in_moves_or_deletes_the_item(self):
        hidden = os.path.join(config.MEDIA_DIR, "Hidden", "Away")
        os.makedirs(hidden)
        os.replace(os.path.join(self.folder, "IMG_8001.HEIC.waterfall.txt"),
                   os.path.join(hidden, "Renamed.HEIC.waterfall.txt"))
        convert_everything()
        self.assertNotIn("IMG_8001.HEIC", {library.item(i, True)["name"] for i in ids("visible")})
        self.assertEqual([library.item(i, True)["name"] for i in ids("hidden")], ["Renamed.HEIC"])
        os.remove(os.path.join(hidden, "Renamed.HEIC.waterfall.txt"))
        convert_everything()
        self.assertEqual(ids("hidden"), [])

    def test_6_a_full_size_file_put_back_is_recognised_and_removed_again(self):
        again = fixtures.video(os.path.join(self.folder, "IMG_8002.mp4"), 640, 360, 2)
        self.assertTrue(os.path.isfile(again))
        calls = []
        real = media.make_video
        media.make_video = lambda *a, **k: calls.append(1) or real(*a, **k)
        try:
            convert_everything()
        finally:
            media.make_video = real
        # The fixture is generated the same way, so it is the same content.
        self.assertEqual(calls, [], "same content: its copies already exist")
        self.assertFalse(os.path.exists(again))
        self.assertTrue(os.path.exists(again + config.STUB))
        self.assertEqual([n for n in {library.item(i, True)["name"] for i in ids("visible")} if n == "IMG_8002.mp4"],
                         ["IMG_8002.mp4"], "one item, not two")

    def test_7_copies_missing_and_file_gone_is_reported_not_hidden(self):
        victim = item_named("IMG_8003.HEIC")
        shutil.rmtree(media.deriv_dir(victim["key"]))
        con = library.connect()
        with con:
            con.execute("UPDATE items SET status='new' WHERE id=?", (victim["id"],))
        convert_everything()
        failed = {f["rel_path"]: f["error"] for f in library.failures()}
        self.assertIn("Import it again", failed.get("Visible/Trip/IMG_8003.HEIC", ""))


class TestImportDoor(unittest.TestCase):
    """The only way files get in: WebDAV on its own port, admin password only."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="waterfall-door-")
        point_at(os.path.join(cls.dir, "data"))            # its own library; MEDIA_DIR is inside it
        config.MEDIA_DIR = os.path.join(cls.dir, "data", "media")
        config.ensure_roots()
        config.KEEP_ORIGINALS = True
        auth.set_passwords(ADMIN, None)
        cls.nudges = []
        cls.server = dav.serve(on_change=lambda: cls.nudges.append(1), bind="127.0.0.1", port=0)
        cls.port = cls.server.server_address[1]
        cls.photo = os.path.join(cls.dir, "one.jpg")
        fixtures.photo(cls.photo, 1200, 900, "door", when="2024:05:05 10:00:00")
        with open(cls.photo, "rb") as f:
            cls.bytes = f.read()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        point_at(DATA)
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        auth._FAILS.clear()

    def ask(self, method, path, body=None, password=ADMIN, headers=None, user="anything"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = dict(headers or {})
        if password is not None:
            h["Authorization"] = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, data, dict(r.getheaders())

    def names(self, path):
        status, xml, _ = self.ask("PROPFIND", path, headers={"Depth": "1"})
        self.assertEqual(status, 207)
        hrefs = [e.text for e in ET.fromstring(xml).iter("{DAV:}href")]
        return [h for h in hrefs if h.rstrip("/") != path.rstrip("/")]

    def test_only_the_admin_password_opens_it(self):
        self.assertEqual(self.ask("PROPFIND", "/", password=None)[0], 401)
        self.assertEqual(self.ask("PROPFIND", "/", password="wrong")[0], 401)
        self.assertEqual(self.ask("PUT", "/Visible/x.jpg", body=b"data", password="wrong")[0], 401)
        self.assertFalse(os.path.exists(os.path.join(config.MEDIA_DIR, "Visible", "x.jpg")))
        self.assertEqual(self.ask("PROPFIND", "/", user="")[0], 207, "the username is ignored")
        status, _, headers = self.ask("OPTIONS", "/", password=None)
        self.assertEqual(status, 200)
        self.assertIn("1", headers["DAV"])

    def test_a_length_below_zero_is_refused_and_nothing_is_read(self):
        # "Content-Length: -1" used to mean: read whatever is sent, into memory,
        # until the sender stops. Asked without the password, too.
        self.assertEqual(self.ask("PUT", "/Visible/neg.jpg", password=None, headers={"Content-Length": "-1"})[0], 401)
        self.assertEqual(self.ask("PUT", "/Visible/neg.jpg", headers={"Content-Length": "-1"})[0], 400)
        self.assertEqual(self.ask("PUT", "/Visible/neg.jpg", headers={"Content-Length": "many"})[0], 400)
        self.assertFalse(os.path.exists(os.path.join(config.MEDIA_DIR, "Visible", "neg.jpg")))

    def test_nothing_large_is_read_from_someone_without_the_password(self):
        # They announce ten gigabytes and send none of it. The answer must come
        # at once, not after waiting to be sent it.
        started = time.time()
        for method, expect in (("OPTIONS", 200), ("PROPFIND", 401), ("PUT", 401)):
            status = self.ask(method, "/Visible/big.mov", password=None, headers={"Content-Length": str(10 ** 10)})[0]
            self.assertEqual(status, expect, method)
        self.assertLess(time.time() - started, 10)

    def test_only_so_many_connections_are_served_at_once(self):
        saved, dav.MAX_CLIENTS = dav.MAX_CLIENTS, 2
        try:
            server = dav._Server(("127.0.0.1", 0), dav.Handler)
        finally:
            dav.MAX_CLIENTS = saved
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port, idle = server.server_address[1], []

        def answer():
            with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
                try:
                    s.sendall(b"OPTIONS / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                    return s.recv(64)
                except OSError:
                    return b""
        try:
            self.assertTrue(answer().startswith(b"HTTP/1.1 200"))
            for _ in range(2):                      # two that connect and then say nothing
                idle.append(socket.create_connection(("127.0.0.1", port), timeout=5))
            time.sleep(0.3)
            self.assertEqual(answer(), b"", "a third must be turned away, not queued behind them")
            for s in idle:
                s.close()
            time.sleep(0.3)
            self.assertTrue(answer().startswith(b"HTTP/1.1 200"), "and served again once they have gone")
        finally:
            for s in idle:
                s.close()
            server.shutdown()
            server.server_close()

    def test_the_two_roots_are_all_there_is_and_cannot_be_changed(self):
        self.assertEqual(self.names("/"), ["/Visible/", "/Hidden/"])
        self.assertEqual(self.ask("MKCOL", "/Other")[0], 404)
        self.assertEqual(self.ask("PUT", "/loose.jpg", body=b"x")[0], 404)
        self.assertEqual(self.ask("DELETE", "/Hidden")[0], 403)
        self.assertEqual(self.ask("MOVE", "/Visible", headers={"Destination": "/Hidden/Visible"})[0], 403)
        self.assertEqual(self.ask("PUT", "/Visible", body=b"x")[0], 403)
        self.assertTrue(os.path.isdir(os.path.join(config.MEDIA_DIR, "Hidden")))

    def test_a_client_cannot_climb_out_of_the_library(self):
        outside = os.path.join(self.dir, "outside.txt")
        for path in ("/Visible/../../outside.txt", "/Visible/%2e%2e/%2e%2e/outside.txt",
                     "/Visible/a%2f..%2f..%2foutside.txt", "/Visible/..%5coutside.txt"):
            status = self.ask("PUT", path, body=b"escaped")[0]
            self.assertIn(status, (400, 403, 404), path)
        self.assertFalse(os.path.exists(outside))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "data", "outside.txt")))

    def test_upload_convert_and_see_it_only_on_the_right_wall(self):
        self.assertEqual(self.ask("MKCOL", "/Hidden/Trip")[0], 201)
        self.assertEqual(self.ask("MKCOL", "/Hidden/Trip")[0], 405)
        self.assertEqual(self.ask("MKCOL", "/Hidden/No/Parent")[0], 409)
        before = len(self.nudges)
        self.assertEqual(self.ask("PUT", "/Hidden/Trip/IMG_7000.jpg", body=self.bytes)[0], 201)
        self.assertEqual(len(self.nudges), before + 1, "an arrival must prompt a rescan")
        with open(os.path.join(config.MEDIA_DIR, "Hidden", "Trip", "IMG_7000.jpg"), "rb") as f:
            self.assertEqual(f.read(), self.bytes, "what arrives must be exactly what was sent")
        self.assertEqual(self.ask("GET", "/Hidden/Trip/IMG_7000.jpg")[1], self.bytes)
        self.assertEqual(self.names("/Hidden/Trip/"), ["/Hidden/Trip/IMG_7000.jpg"])
        self.assertEqual(convert_everything().get("ready"), 1)
        name = lambda i: library.item(i, True)["name"]
        self.assertNotIn("IMG_7000.jpg", [name(i) for i in ids("visible")])
        self.assertIn("IMG_7000.jpg", [name(i) for i in ids("hidden")])
        self.assertEqual(item_named("IMG_7000.jpg")["section_path"], "Trip")

    def test_moving_between_walls_changes_who_sees_it_without_reconverting(self):
        self.ask("MKCOL", "/Visible/Moves")
        self.ask("PUT", "/Visible/Moves/IMG_7001.jpg", body=self.bytes + b"\x00move")
        convert_everything()
        self.assertIn("IMG_7001.jpg", [library.item(i, True)["name"] for i in ids("visible")])
        self.ask("MKCOL", "/Hidden/Moved")
        status = self.ask("MOVE", "/Visible/Moves/IMG_7001.jpg",
                          headers={"Destination": f"http://127.0.0.1:{self.port}/Hidden/Moved/IMG_7001.jpg"})[0]
        self.assertEqual(status, 201)
        calls = []
        real = media.make_photo
        media.make_photo = lambda *a, **k: calls.append(1) or real(*a, **k)
        try:
            convert_everything()
        finally:
            media.make_photo = real
        self.assertEqual(calls, [], "a move must not cost a conversion")
        self.assertNotIn("IMG_7001.jpg", [library.item(i, True)["name"] for i in ids("visible")])
        self.assertIn("IMG_7001.jpg", [library.item(i, True)["name"] for i in ids("hidden")])
        self.assertEqual(self.ask("DELETE", "/Visible/Moves")[0], 204)
        self.assertEqual(self.ask("DELETE", "/Visible/Moves")[0], 404)

    def test_chunked_uploads_and_overwrites(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        auth_header = "Basic " + base64.b64encode(f"x:{ADMIN}".encode()).decode()
        pieces = [self.bytes[i:i + 7000] for i in range(0, len(self.bytes), 7000)]
        conn.request("PUT", "/Visible/chunked.jpg", body=iter(pieces), encode_chunked=True,
                     headers={"Authorization": auth_header, "Transfer-Encoding": "chunked"})
        r = conn.getresponse()
        r.read()
        # The same connection must still be usable: the body was read exactly.
        conn.request("PROPFIND", "/Visible/chunked.jpg", headers={"Authorization": auth_header, "Depth": "0"})
        r2 = conn.getresponse()
        r2.read()
        conn.close()
        self.assertEqual((r.status, r2.status), (201, 207))
        with open(os.path.join(config.MEDIA_DIR, "Visible", "chunked.jpg"), "rb") as f:
            self.assertEqual(f.read(), self.bytes)
        self.assertEqual(self.ask("PUT", "/Visible/chunked.jpg", body=b"replaced")[0], 204)
        self.assertEqual(self.ask("GET", "/Visible/chunked.jpg")[1], b"replaced")
        self.ask("DELETE", "/Visible/chunked.jpg")

    def test_half_arrived_files_are_never_seen(self):
        folder = os.path.join(config.MEDIA_DIR, "Visible")
        partial = os.path.join(folder, ".IMG_7002.jpg.part-abcd1234")
        with open(partial, "wb") as f:
            f.write(self.bytes[:1000])
        # A client that locks a name before sending it leaves an empty file for a moment.
        self.assertEqual(self.ask("LOCK", "/Visible/IMG_7003.jpg", body=b"<lockinfo/>")[0], 201)
        self.assertEqual(os.path.getsize(os.path.join(folder, "IMG_7003.jpg")), 0)
        try:
            self.assertNotIn("/Visible/.IMG_7002.jpg.part-abcd1234", self.names("/Visible/"))
            scanned = library.scan()
            names = [r["name"] for r in library.connect().execute("SELECT name FROM items")]
            self.assertNotIn(".IMG_7002.jpg.part-abcd1234", names)
            self.assertNotIn("IMG_7003.jpg", names, "an empty placeholder is not a photo")
            self.assertEqual(scanned["added"], 0)
        finally:
            os.remove(partial)
            self.assertEqual(self.ask("UNLOCK", "/Visible/IMG_7003.jpg")[0], 204)
            self.ask("DELETE", "/Visible/IMG_7003.jpg")

    def test_a_malformed_upload_is_refused_and_leaves_nothing(self):
        status = self.ask("PUT", "/Visible/bad.jpg", body=b"zz\r\nnot chunked at all\r\n",
                          headers={"Transfer-Encoding": "chunked"})[0]
        self.assertEqual(status, 400)
        self.assertEqual([n for n in os.listdir(os.path.join(config.MEDIA_DIR, "Visible")) if "bad.jpg" in n], [])

    def test_the_stand_in_can_be_read_through_the_door(self):
        config.KEEP_ORIGINALS = False
        try:
            self.ask("MKCOL", "/Visible/Kept")
            self.ask("PUT", "/Visible/Kept/IMG_7005.jpg", body=self.bytes + b"\x00stand-in")
            convert_everything()
        finally:
            config.KEEP_ORIGINALS = True
        self.assertEqual(self.names("/Visible/Kept/"), ["/Visible/Kept/IMG_7005.jpg.waterfall.txt"])
        text = self.ask("GET", "/Visible/Kept/IMG_7005.jpg.waterfall.txt")[1].decode()
        self.assertIn("stands in for IMG_7005.jpg", text)
        self.assertEqual(self.ask("DELETE", "/Visible/Kept")[0], 204)

    def test_removed_files_leave_the_index_and_their_copies_are_swept_later(self):
        self.ask("PUT", "/Visible/IMG_7004.jpg", body=self.bytes + b"\x00sweep")
        convert_everything()
        key = item_named("IMG_7004.jpg")["key"]
        self.assertEqual(self.ask("DELETE", "/Visible/IMG_7004.jpg")[0], 204)
        library.scan()
        self.assertIsNone(library.connect().execute("SELECT 1 FROM items WHERE name='IMG_7004.jpg'").fetchone())
        self.assertEqual(library.collect_garbage(), 0, "a week's grace: a moved file must find its copies")
        self.assertTrue(os.path.isdir(media.deriv_dir(key)))
        self.assertEqual(library.collect_garbage(grace=-1), 1)
        self.assertFalse(os.path.exists(media.deriv_dir(key)))

    def test_it_refuses_until_the_admin_password_exists(self):
        saved = library.get_setting("admin_hash")
        library.set_setting("admin_hash", "")
        try:
            self.assertEqual(self.ask("PROPFIND", "/")[0], 503)
        finally:
            library.set_setting("admin_hash", saved)


if __name__ == "__main__":
    unittest.main()

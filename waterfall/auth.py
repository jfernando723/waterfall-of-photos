"""One password that matters, and an optional second one. No usernames.

    admin    set on first visit. Unlocks the Hidden wall, the library status,
             and the import door (dav.py).
    viewer   OPTIONAL. If none is set, anyone who can reach the address can
             browse the Visible sections without signing in: open the address,
             see the wall. If one is set, browsing asks for it first.

So privacy has two layers that do not depend on each other: who can reach the
port at all (your own network, or a private one such as a VPN), and, inside
that, the admin password for anything under Hidden/.

What is stored is a salted scrypt hash of each password; what the browser holds
is a signed cookie saying which role it proved. Changing a password bumps a
counter inside that signature, so every older cookie stops working at once.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time

from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import config, library

COOKIE = "waterfall_session"
MAX_AGE = 60 * 60 * 24 * 60          # sixty days on a phone before asking again
# ...for a viewer. The admin's sign-in lasts only while the admin is using it
# (config.ADMIN_IDLE): the page renews it when it is touched, and one that has
# not been renewed for that long is worth no more than a viewer's.
TICKET_AGE = 60 * 60 * 24            # an upload begun by the admin may take a day to finish
MIN_LENGTH = 6

# After a few wrong guesses an address has to wait. This is meant for a private
# network, so the point is to make guessing pointless, not to survive a siege.
_FAILS: dict[str, list[float]] = {}
_FAIL_LOCK = threading.Lock()
FAIL_LIMIT, FAIL_WINDOW = 6, 300
# How many addresses are remembered at once. Past this, and with none of them
# expired, a new address is made to wait as well: thousands of addresses all
# guessing wrongly within five minutes is not a household.
FAILS_KEPT = 4096


def _hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return salt.hex() + ":" + digest.hex()


def _matches(password: str, stored: str | None) -> bool:
    if not stored or ":" not in stored:
        return False
    salt_hex, _ = stored.split(":", 1)
    return hmac.compare_digest(_hash(password, bytes.fromhex(salt_hex)), stored)


# The signing secret and the session counter are read on every media request
# and change only when a password does, so they are held in memory.
_cached: dict = {}


def _state() -> dict:
    if not _cached:
        secret = library.get_setting("secret")
        if not secret:
            secret = secrets.token_hex(32)
            library.set_setting("secret", secret)
        _cached.update(secret=secret, epoch=library.get_setting("epoch") or 0)
    return _cached


def forget_cached() -> None:
    _cached.clear()


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_state()["secret"], salt="waterfall-session")


def is_configured() -> bool:
    return bool(library.get_setting("admin_hash"))


def has_viewer() -> bool:
    return bool(library.get_setting("viewer_hash"))


def set_passwords(admin: str | None, viewer: str | None, clear_viewer: bool = False) -> None:
    """Store new password hashes. Raises ValueError with a message for the UI."""
    if admin is not None and len(admin) < MIN_LENGTH:
        raise ValueError(f"The admin password needs at least {MIN_LENGTH} characters.")
    if viewer is not None and len(viewer) < MIN_LENGTH:
        raise ValueError(f"The viewer password needs at least {MIN_LENGTH} characters.")
    admin_hash = _hash(admin) if admin is not None else library.get_setting("admin_hash")
    if not admin_hash:
        raise ValueError("An admin password is required.")
    viewer_hash = None if clear_viewer else (
        _hash(viewer) if viewer is not None else library.get_setting("viewer_hash"))
    # Login is by password alone, so the two must differ: identical passwords
    # would make the viewer an admin. Whichever one is being set is compared
    # with the other, new or already stored.
    same = False
    if admin is not None and viewer is not None:
        same = admin == viewer
    elif admin is not None:
        same = _matches(admin, viewer_hash)
    elif viewer is not None:
        same = _matches(viewer, admin_hash)
    if same:
        raise ValueError("The admin and viewer passwords must be different.")
    library.set_setting("admin_hash", admin_hash)
    library.set_setting("viewer_hash", viewer_hash or "")
    library.set_setting("epoch", (library.get_setting("epoch") or 0) + 1)
    forget_cached()


def seed_from_environment() -> None:
    """Optional: WATERFALL_ADMIN_PASSWORD / WATERFALL_VIEWER_PASSWORD set the
    passwords on a first start, for anyone who prefers that to the setup page.
    Ignored once passwords exist, so a stale compose file cannot reset them."""
    if is_configured():
        return
    admin = os.environ.get("WATERFALL_ADMIN_PASSWORD")
    if admin:
        set_passwords(admin, os.environ.get("WATERFALL_VIEWER_PASSWORD") or None)


def _recent(address: str, now: float) -> list[float]:
    """This address's guesses inside the window. The caller holds the lock."""
    recent = [t for t in _FAILS.get(address, ()) if now - t < FAIL_WINDOW]
    if recent:
        _FAILS[address] = recent
    else:
        _FAILS.pop(address, None)
    return recent


def throttled(address: str) -> int:
    """Seconds this address must still wait, or 0."""
    now = time.time()
    with _FAIL_LOCK:
        recent = _recent(address, now)
        if len(recent) >= FAIL_LIMIT:
            return int(FAIL_WINDOW - (now - recent[0])) + 1
    return 0


def _begin(address: str) -> float | None:
    """Count a guess BEFORE its password is checked, and say whether it may be.

    Checking a password is slow on purpose. If a guess were only counted once
    it had been checked, a burst of guesses sent together would all be checked
    before the first was counted, and the limit would be whatever number of
    connections the sender cared to open. Returns the mark to forgive if the
    guess turns out right, or None if this address has to wait.
    """
    now = time.time()
    with _FAIL_LOCK:
        recent = _recent(address, now)
        if len(recent) >= FAIL_LIMIT:
            return None
        if not recent and len(_FAILS) >= FAILS_KEPT:
            for other in list(_FAILS):
                _recent(other, now)
            if len(_FAILS) >= FAILS_KEPT:
                return None
        recent.append(now)
        _FAILS[address] = recent
        return now


def _settle(address: str, mark: float, role: str | None) -> None:
    """A right admin password wipes the address's slate. A right VIEWER
    password only takes back its own mark: a viewer is exactly who might try to
    guess the admin password, and signing in as a viewer between guesses must
    not buy six more."""
    with _FAIL_LOCK:
        if role == "admin":
            _FAILS.pop(address, None)
        elif role == "viewer":
            marks = _FAILS.get(address, [])
            if mark in marks:
                marks.remove(mark)
            if not marks:
                _FAILS.pop(address, None)


# The import door is sent the password with every request, and a file manager
# copying a folder sends many requests at once. Checked the slow way each time,
# every one of them cost a slow check, and each was counted as a guess while it
# was being checked: a seventh arriving beside six others was refused although
# its password was right, and the file manager reported a permission error.
# So a password that has passed the slow check is remembered, for as long as
# this process runs and only as a keyed digest, and recognised at once after
# that. Nothing else changes: a wrong password is still counted before it is
# checked, and an address that is waiting still waits, right password or not.
_PROOF_KEY = secrets.token_bytes(32)
_PROVEN: dict[str, bytes] = {}      # the stored admin hash -> digest of the password that matched it
_SLOW_CHECK = threading.Lock()


def _digest(password: str) -> bytes:
    return hmac.new(_PROOF_KEY, password.encode("utf-8"), hashlib.sha256).digest()


def _proven(password: str, stored: str | None) -> bool:
    known = _PROVEN.get(stored) if stored else None
    return known is not None and hmac.compare_digest(known, _digest(password))


def _forgive(address: str) -> None:
    """A right admin password wipes the address's slate, as _settle does."""
    if address in _FAILS:
        with _FAIL_LOCK:
            _FAILS.pop(address, None)


def check_admin(password: str, address: str) -> bool:
    """For the import door, which only the admin password opens. Wrong
    guesses there count against the same limit as wrong guesses at sign-in."""
    stored = library.get_setting("admin_hash")
    if throttled(address):
        return False
    if _proven(password, stored):
        _forgive(address)
        return True
    # One slow check at a time. Requests that arrive together with the right
    # password wait here behind the first, then pass on the line below instead
    # of each being counted as a guess.
    with _SLOW_CHECK:
        if _proven(password, stored):
            _forgive(address)
            return True
        mark = _begin(address)
        if mark is None:
            return False
        good = _matches(password, stored)
        _settle(address, mark, "admin" if good else None)
        if good:
            _PROVEN.clear()             # a changed password has a new stored hash
            _PROVEN[stored] = _digest(password)
        return good


def check(password: str, address: str) -> str | None:
    """Which role this password proves, if any. None while the address waits."""
    mark = _begin(address)
    if mark is None:
        return None
    role = None
    if _matches(password, library.get_setting("admin_hash")):
        role = "admin"
    elif _matches(password, library.get_setting("viewer_hash")):
        role = "viewer"
    _settle(address, mark, role)
    return role


def issue(role: str) -> str:
    return _signer().dumps({"r": role, "e": _state()["epoch"]})


def _read(token: str | None) -> tuple[str | None, float]:
    """(the role a sign-in cookie proves, how many seconds ago it was issued)."""
    if not token:
        return None, 0.0
    try:
        data, when = _signer().loads(token, max_age=MAX_AGE, return_timestamp=True)
    except BadSignature:
        return None, 0.0
    if data.get("e") != _state()["epoch"]:
        return None, 0.0
    role = data.get("r") if data.get("r") in ("admin", "viewer") else None
    return role, max(0.0, time.time() - when.timestamp())


def role_of(token: str | None) -> str | None:
    role, age = _read(token)
    if role == "admin" and age > config.ADMIN_IDLE:
        return "viewer"         # left alone too long: what anyone may see, and no more
    return role


def admin_seconds_left(token: str | None) -> int:
    """How long this admin sign-in still has, if nobody touches the page."""
    role, age = _read(token)
    return max(0, int(config.ADMIN_IDLE - age)) if role == "admin" else 0


# An upload outlives the sign-in that began it. Sending a large video from a
# phone can take hours, and the admin's sign-in is meant to lapse long before
# that. So the page is given a ticket when an upload starts: it lets its holder
# add files and do nothing else. It cannot open the Hidden wall, read the
# library's status or change anything, and a change of password cancels it.
def issue_ticket() -> str:
    return URLSafeTimedSerializer(_state()["secret"], salt="waterfall-upload").dumps({"e": _state()["epoch"]})


def ticket_ok(token: str | None) -> bool:
    if not token:
        return False
    try:
        data = URLSafeTimedSerializer(_state()["secret"], salt="waterfall-upload").loads(token, max_age=TICKET_AGE)
    except BadSignature:
        return False
    return isinstance(data, dict) and data.get("e") == _state()["epoch"]

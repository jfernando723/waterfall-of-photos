"""Waterfall: a private, password-gated scrolling feed for one person's photos
and videos, built to be watched from a phone over a slow, distant link.

Two version numbers, deliberately separate:

  * __version__ is the application.
  * PIPELINE is the recipe for the small copies (sizes, bitrates, formats).
    It is part of every media URL, so changing the recipe changes the URLs and
    a phone can never show a stale copy it cached under the old one.
"""

__version__ = "0.6.0"
PIPELINE = 3

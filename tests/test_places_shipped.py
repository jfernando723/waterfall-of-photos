"""The places this copy of Waterfall ships with: the towns and villages it is
expected to name, and the hand-made list beside them.

Kept apart from test_waterfall.py because they test DATA, not the program. A
copy built with a different list of places (scripts/make-places.py, and
waterfall/data/places-preferred.tsv) should change or remove this file with it.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from waterfall import places  # noqa: E402


class TestShippedPlaces(unittest.TestCase):
    def test_towns_around_the_world_are_named(self):
        for lat, lon, expect in (
                (8.4840, -13.2340, "Freetown, Sierra Leone"), (8.3389, -13.0709, "Waterloo, Sierra Leone"),
                (7.9647, -11.7383, "Bo, Sierra Leone"), (7.8767, -11.1875, "Kenema, Sierra Leone"),
                (9.3202, 16.1552, "Béré, Chad"), (8.5744, 16.0772, "Moundou, Chad"),
                (34.0483, -117.2612, "Loma Linda, United States"), (40.7580, -73.9855, "New York City, United States"),
                (13.4125, 103.8670, "Siem Reap, Cambodia"), (13.7500, 100.4913, "Bangkok, Thailand"),
                (41.4036, 2.1744, "Barcelona, Spain"), (41.8902, 12.4922, "Rome, Italy"),
                (55.9486, -3.1999, "Edinburgh, United Kingdom"), (53.3498, -6.2603, "Dublin, Ireland")):
            self.assertEqual(places.nearest(lat, lon), expect)

    def test_a_village_is_named_where_no_town_is_near(self):
        # Deep in the country in Sierra Leone the only names are villages with
        # no population on record. One of them is better than a town an hour away.
        named = places.nearest(7.55, -11.35)
        self.assertTrue(named and named.endswith(", Sierra Leone"))
        self.assertNotIn(named.split(",")[0], ("Bo", "Kenema", "Freetown"))

    def test_the_hand_made_names_in_sierra_leone(self):
        # The names chosen by hand, kept in data/places-preferred.tsv. Each
        # is pinned at a position from a map, not at its own circle's centre.
        pinned = (
                # west coast of the peninsula, north to south; Tokeh at the sand and in the village
                (8.4330, -13.2889, "Goderich"), (8.3914, -13.2639, "Lakka Beach"), (8.3827, -13.2595, "Hamilton Beach"),
                (8.3484, -13.2346, "Sussex Beach"), (8.3354, -13.2054, "River No. 2 Beach"),
                (8.3132, -13.1965, "Tokeh Beach"), (8.3085, -13.1908, "Tokeh Beach"), (8.2857, -13.1789, "York"),
                (8.2657, -13.1694, "Black Johnson Beach"), (8.2451, -13.1640, "John Obey Beach"),
                (8.2037, -13.1585, "Bureh Beach"),
                # inside Freetown; Lumley Beach in the middle of the sand and at its south end
                (8.4739, -13.2834, "Lumley Beach"), (8.4600, -13.2790, "Lumley Beach"), (8.4955, -13.2867, "Aberdeen"),
                (8.4916, -13.2653, "Murray Town"), (8.4754, -13.2636, "Wilberforce"), (8.4594, -13.2539, "Hill Station"),
                (8.4686, -13.1893, "Kissy"), (8.4501, -13.1696, "Wellington"), (8.4387, -13.1633, "Calaba Town"),
                (8.4136, -13.1603, "Allen Town"),
                # the hills behind it
                (8.4355, -13.2185, "Regent"), (8.4611, -13.2187, "Leicester"), (8.4496, -13.2238, "Leicester Peak"),
                (8.4522, -13.2139, "Gloucester"), (8.4235, -13.1911, "Charlotte"), (8.4312, -13.1999, "Bathurst"),
                (8.4200, -13.2063, "Tacugama"), (8.3601, -13.1996, "Guma Dam"),
                # across the water: Lungi at the airport and in the town, the Banana Islands at both villages
                (8.6167, -13.1916, "Lungi"), (8.6402, -13.2266, "Lungi"), (8.5223, -13.1491, "Tagrin"),
                (8.5697, -13.0403, "Bunce Island"), (8.5547, -13.0744, "Tasso Island"),
                (8.1373, -13.1924, "Banana Islands"), (8.1009, -13.2362, "Banana Islands"),
                # the road east; Mile 38 has no record of its own, so it is pinned at the mapped Mile 36
                (8.4092, -13.1334, "Jui"), (8.3950, -13.1550, "Grafton"), (8.3813, -13.1389, "Hastings"),
                (8.3788, -13.1028, "Rokel"), (8.3735, -13.0938, "Devil Hole"), (8.3363, -13.0071, "Newton"),
                (8.3685, -12.9327, "Songo"), (8.4331, -12.8800, "Mile 38"), (8.4909, -12.7574, "Masiaka"),
                # up-country; Tiwai on the island and at the boat landing in Kambama
                (9.9751, -11.8510, "Mandia"), (7.5451, -11.3457, "Tiwai"), (7.5571, -11.3653, "Tiwai"),
                # on the highway itself, where it read "Freetown" 50 km out of town
                (8.3891, -12.9475, "Songo"), (8.4157, -12.9046, "Songo"), (8.4791, -12.8192, "Mile 38"))
        for lat, lon, expect in pinned:
            self.assertEqual(places.nearest(lat, lon), expect + ", Sierra Leone")
        # Mandia, Songo and Hastings would read correctly at those positions with no
        # list at all, so also require every circle to hold one of its own pins. A
        # slipped digit moves the circle off its place, and a circle added without
        # being pinned above has none; both fail here.
        for clat, clon, name, _code, radius in places._preferred:
            self.assertTrue(any(expect == name and places._km(clat, clon, lat, lon) <= radius
                                for lat, lon, expect in pinned), f"{name}: no pinned position inside its circle")
        # The towns are left alone: their centres, and the places beside the circles.
        for lat, lon, expect in (
                (8.4840, -13.2340, "Freetown"), (8.4871, -13.2356, "Freetown"), (8.3389, -13.0709, "Waterloo"),
                (8.3390, -13.0696, "Waterloo"), (8.1738, -13.1600, "Kent"), (8.1968, -13.1226, "Mama Beach"),
                (8.2177, -13.0976, "Tombo"), (8.5867, -13.0553, "Pepel")):
            self.assertEqual(places.nearest(lat, lon), expect + ", Sierra Leone")
        # GeoNames keeps a "Tokeh", a "Kent" and a "Tombo" on one wrong spot 800 m
        # from the centre of Waterloo. It reads as Waterloo, not as any of them.
        self.assertEqual(places.nearest(8.3333, -13.0667), "Waterloo, Sierra Leone")
        # Outside every circle the general rule still names a village.
        named = places.nearest(7.20, -11.45)
        self.assertTrue(named and named.endswith(", Sierra Leone"))
        self.assertNotIn(named.split(",")[0], ("Bo", "Kenema", "Freetown", "Tiwai"))

    def test_every_line_of_the_hand_made_list_is_read(self):
        # A line that does not parse stops the list being read at that line, without
        # an error, and every name after it is lost. So count them.
        path = os.path.join(os.path.dirname(places.__file__), "data", "places-preferred.tsv")
        with open(path, encoding="utf-8") as f:
            rows = [line.rstrip("\n").split("\t") for line in f if line.strip() and not line.startswith("#")]
        places._load()
        self.assertEqual(len(places._preferred), len(rows))
        for row in rows:
            self.assertEqual(len(row), 6, row)                  # the note says where the position came from
            self.assertTrue(row[0].strip() and row[5].strip(), row)
            self.assertEqual(row[1], "SL", row)
            self.assertTrue(0 < float(row[4]) <= 7, row)


if __name__ == "__main__":
    unittest.main()

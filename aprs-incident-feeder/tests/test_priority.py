"""Tests for aprs_priority.py: how important an object is.

  python3 -m unittest test_priority        (from this folder; no network)
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import aprs_priority as P


class Levels(unittest.TestCase):
    def level(self, name, text, source=""):
        return P.rate(name, text, source)["level"]

    def test_county_calls(self):
        for name, text, want in [
            ("FIRE7K2QX", "BUILDING FIRE PENN RD/AUBREY RD (ABINGTON) STA100 @12:40", "high"),
            ("FIRE1", "HOUSE MORRIS CIR/ROCKLAND RD", "high"),
            ("FIRE2", "FIRE ALARM AVON RD/THORNBURY RD", "low"),
            ("FIREB2680", "WATERFLOW ALARM LOC MIDDLETOWN", "low"),
            ("FIRE3", "CONTROLLED BURN OAK LN", "low"),
            ("FIRE4", "VEHICLE ACCIDENT WHITEHALL RD", "medium"),
            ("FIRE5", "ROAD CLOSURE S HOUSTON RD", "medium"),
            ("FIRE6", "BRUSH FIRE RIDGE RD", "medium"),
            ("TRAF1", "AUTO EXTRICATION RBOX LINCOLN HWY", "critical"),
            ("TRAF2", "ENTRAPMENT ACCIDENT RT 422", "critical"),
            ("TRAF3", "VEHICLE ACCIDENT RIDGE PK", "medium"),
            ("TRAF4", "TRAFFIC ACCIDENT - PROPERTY DAMAGE", "low"),
            ("TRAF5", "DISABLED MOTORIST RT 1", "low"),
            ("TRAF6", "PEDESTRIAN STRUCK MAIN ST", "high"),
            ("EMS4H9ABC", "RESPIRATORY EMERGENCY DOCK DR", "medium"),
            ("EMS4H9ABD", "CARDIAC ARREST DOCK DR", "high"),
        ]:
            self.assertEqual(self.level(name, text), want, (name, text))

    def test_river_gauges_are_rated_by_their_flood_category(self):
        for text, want in (("MAJOR FLOOD 12.1ft Schuylkill River at Norristown @21:15", "critical"),
                           ("MODERATE FLOOD 9.3ft Maurice River at Bivalve @15:00", "high"),
                           ("MINOR FLOOD 9.7ft Delaware River at Burlington @09:00", "medium"),
                           ("NEAR FLOOD 5.6ft Schuylkill River near Philadelphia @19:00", "low"),
                           ("LOW WATER -0.3ft Delaware River at New Hope @20:45", "low")):
            self.assertEqual(self.level("RIVBDRN4", text), want, text)

    def test_street_names_do_not_make_a_crash_a_building_fire(self):
        self.assertEqual(self.level("TRAF1", "VEHICLE ACCIDENT SCHOOLHOUSE RD/COMMERCIAL DR"), "medium")
        self.assertEqual(self.level("FIRE1", "VEHICLE ACCIDENT OLD FORGE RD/BARN RD (LIMERICK)"), "medium")

    def test_road_feeds(self):
        self.assertEqual(self.level("WORK0AB4C", "roadwork US 202 SB Lane Restriction"), "low")
        self.assertEqual(self.level("WORK1", "roadwork I-95 SB Ramp Closure"), "low")
        self.assertEqual(self.level("WORK2", "Roadwork NJ 42 NB All lanes closed"), "medium")
        self.assertEqual(self.level("TRAF9", "crash I-76 WB Lane Restriction Montgomery"), "medium")
        self.assertEqual(self.level("TRAF9", "crash I-76 WB Montgomery All lanes closed"), "high")
        self.assertEqual(self.level("TRAF9", "Delays NJ 42 NB Camden"), "low")
        self.assertEqual(self.level("EVNTX1", "Concert traffic"), "low")

    def test_weather(self):
        for name, text, want in [
            ("TORPHI123", "TORNADO WRN til 3:15PM", "critical"),
            ("TOAPHI001", "TORNADO WATCH til 9PM", "high"),
            ("SVRPHI010", "SVR TSTM WRN til 4PM", "high"),
            ("SVAPHI010", "SVR TSTM WATCH til 9PM", "medium"),
            ("FFWPHI003", "FLASH FLOOD WRN til 6PM", "high"),
            ("FFWPHI004", "FLASH FLOOD EMERGENCY", "critical"),
            ("WNDPHI002", "HIGH WIND WRN til Fri", "medium"),
            ("WNDPHI001", "HIGH WIND WATCH til Fri", "low"),
            ("SPSPHI001", "WX STMT strong tstm", "low"),
            ("AL-052026", "HURRICANE EXAMPLE", "high"),
        ]:
            self.assertEqual(self.level(name, text, "nws"), want, (name, text))

    def test_weather_is_recognised_without_being_told_the_source(self):
        self.assertEqual(self.level("TORPHI123", "TORNADO WRN til 3:15PM"), "critical")
        self.assertEqual(self.level("FIRE7K2QX", "BUILDING FIRE"), "high")       # FIR is a weather tag too

    def test_an_unknown_name_is_medium_and_unrated(self):
        r = P.rate("MYOBJECT", "hello")
        self.assertEqual((r["level"], r["rated"]), ("medium", False))

    def test_an_outline_in_the_text_is_ignored(self):
        self.assertEqual(self.level("WNDPHI001", "HIGH WIND WATCH til Fri }a0!ABCDEFGH{WRNAA", "nws"), "low")


class Scores(unittest.TestCase):
    def test_score_stays_inside_its_level_band(self):
        for name, text in [("TORPHI1", "TORNADO WRN"), ("FIRE1", "BUILDING FIRE"), ("TRAF1", "VEHICLE ACCIDENT"),
                           ("WORK1", "roadwork")]:
            for age in (None, 0, 30, 500):
                for tail in ("", "?"):
                    r = P.rate(name, text + tail, "", age)
                    lo, hi = P.BAND[r["level"]]
                    self.assertTrue(lo <= r["score"] <= hi, (name, age, tail, r))

    def test_new_scores_higher_and_approximate_lower(self):
        new, old = P.rate("TRAF1", "VEHICLE ACCIDENT", "", 0), P.rate("TRAF1", "VEHICLE ACCIDENT", "", 300)
        self.assertGreater(new["score"], old["score"])
        self.assertIn("new or just changed", new["reasons"])
        approx = P.rate("TRAF1", "VEHICLE ACCIDENT MAIN ST?", "", 300)
        self.assertLess(approx["score"], old["score"])
        self.assertIn("position approximate", approx["reasons"])
        self.assertEqual(new["level"], approx["level"])                          # the level never moves

    def test_rate_packet(self):
        r = P.rate_packet(";FIRE1    *241500z4007.20N/07520.40WdBUILDING FIRE MAIN ST", "montco")
        self.assertEqual(r["level"], "high")
        self.assertIsNone(P.rate_packet(">a status line"))


class Settings(unittest.TestCase):
    def test_minimum_level_is_saved_and_read_back(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(P.load_min_level(folder=d), "low")
            self.assertEqual(P.save_min_level(" High ", folder=d), "high")
            self.assertEqual(P.load_min_level(folder=d), "high")
            with self.assertRaises(ValueError):
                P.save_min_level("urgent", folder=d)
            with open(P.settings_path(d), "w") as f:
                f.write("not json")
            self.assertEqual(P.load_min_level("medium", folder=d), "medium")

    def test_at_least(self):
        self.assertTrue(P.at_least("high", "medium"))
        self.assertTrue(P.at_least("medium", "medium"))
        self.assertFalse(P.at_least("low", "medium"))


if __name__ == "__main__":
    unittest.main()

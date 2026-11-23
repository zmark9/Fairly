import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cleaning  # noqa: E402


def config():
    return {"homes": [
        {"slug": "desert_condo", "name": "Desert Condo"},
        {"slug": "mountain_cabin", "name": "Mountain Cabin",
         "cleaning_notify": {"active": True, "to_email": "a@example.test", "to_name": "A"}}]}


class CleaningToggleTests(unittest.TestCase):
    def test_off_and_on(self):
        cfg = config()
        cleaning.apply(cfg, "off", "cabin")
        self.assertFalse(cfg["homes"][1]["cleaning_notify"]["active"])
        cleaning.apply(cfg, "on", "mountain_cabin")
        self.assertTrue(cfg["homes"][1]["cleaning_notify"]["active"])

    def test_set_keeps_switch_and_new_home_starts_off(self):
        cfg = config()
        cleaning.apply(cfg, "set", "cabin", "B", "b@example.test")
        self.assertEqual(cfg["homes"][1]["cleaning_notify"],
                         {"active": True, "to_email": "b@example.test", "to_name": "B"})
        cleaning.apply(cfg, "set", "desert", "C", "c@example.test")
        self.assertFalse(cfg["homes"][0]["cleaning_notify"]["active"])

    def test_refuses_bad_input(self):
        cfg = config()
        with self.assertRaises(SystemExit):
            cleaning.apply(cfg, "on", "desert")            # no recipient yet
        with self.assertRaises(SystemExit):
            cleaning.apply(cfg, "set", "cabin", "B", "not-an-email")
        with self.assertRaises(SystemExit):
            cleaning.apply(cfg, "on", "o")                 # ambiguous


if __name__ == "__main__":
    unittest.main()

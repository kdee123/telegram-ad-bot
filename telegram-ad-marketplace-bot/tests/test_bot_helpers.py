import unittest

from telegram_ad_bot import normalize_channel, parse_positive_int, parse_words


class HelperTests(unittest.TestCase):
    def test_normalize_at_handle(self):
        self.assertEqual(normalize_channel("@dailytech"), "@dailytech")

    def test_normalize_tme_url(self):
        self.assertEqual(normalize_channel("https://t.me/dailytech/"), "@dailytech")

    def test_reject_invalid_handle(self):
        with self.assertRaises(ValueError):
            normalize_channel("daily-tech")

    def test_parse_quoted_words(self):
        self.assertEqual(parse_words('@daily "$50 per post" tech'), ["@daily", "$50 per post", "tech"])

    def test_parse_positive_int(self):
        self.assertEqual(parse_positive_int("250", "Stars price"), 250)

    def test_reject_zero_int(self):
        with self.assertRaises(ValueError):
            parse_positive_int("0", "Stars price")


if __name__ == "__main__":
    unittest.main()

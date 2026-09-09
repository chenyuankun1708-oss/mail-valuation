import unittest

from valuation_app.underlying_mail import match_archive_attachment


class UnderlyingMailAttachmentTest(unittest.TestCase):
    def test_known_archive_names_are_accepted(self):
        self.assertTrue(match_archive_attachment("底层资产.rar"))
        self.assertTrue(match_archive_attachment("FOF.rar"))
        self.assertTrue(match_archive_attachment("20260819.rar"))

    def test_unrelated_or_malformed_archives_are_rejected(self):
        self.assertFalse(match_archive_attachment("2026081.rar"))
        self.assertFalse(match_archive_attachment("other.rar"))
        self.assertFalse(match_archive_attachment("20260819.zip"))


if __name__ == "__main__":
    unittest.main()

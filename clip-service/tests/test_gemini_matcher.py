import unittest
from unittest.mock import patch

from app import gemini_matcher


class GeminiMatcherTest(unittest.TestCase):
    @patch.object(gemini_matcher, "GEMINI_MATCH_MARGIN_THRESHOLD", 0.03)
    @patch.object(gemini_matcher, "GEMINI_MATCH_LOW_THRESHOLD", 0.85)
    @patch.object(gemini_matcher, "GEMINI_MATCH_AUTO_THRESHOLD", 0.96)
    def test_small_top_two_margin_requires_review_without_rejecting_match(self):
        results = gemini_matcher._greedy_assign(
            [[0.98, 0.97], [0.86, 0.99]],
            [{"filename": "a.jpg"}, {"filename": "b.jpg"}],
            [{"photo_id": "a"}, {"photo_id": "b"}],
        )

        self.assertEqual(results, [
            {"photo_id": "b", "filename": "b.jpg", "similarity": 0.99, "type": "gemini"},
            {"photo_id": "a", "filename": "a.jpg", "similarity": 0.98, "type": "gemini_low"},
        ])


if __name__ == "__main__":
    unittest.main()

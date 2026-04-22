import unittest

from demo_app import estimate_focus_minutes, summarize_queue


class DemoAppTests(unittest.TestCase):
    def test_summarize_queue_handles_empty_input(self) -> None:
        self.assertEqual(summarize_queue([]), "No tasks queued.")

    def test_summarize_queue_shows_preview_and_count(self) -> None:
        summary = summarize_queue(["plan onboarding", "write docs", "ship fallback"])
        self.assertEqual(summary, "3 tasks queued: plan onboarding, write docs, +1 more")

    def test_estimate_focus_minutes_uses_default_block(self) -> None:
        self.assertEqual(estimate_focus_minutes(3), 75)


if __name__ == "__main__":
    unittest.main()

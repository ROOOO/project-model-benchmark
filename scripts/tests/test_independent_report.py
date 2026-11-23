"""Paired deltas use the new blind round and exclude affected pairs."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paired
from independent_report import aggregate, render


def samples():
    result = []
    for ti, (topic, a, b) in enumerate(zip(paired.TOPICS, (4, 0, -1, 5), (2, 3, 1, 5))):
        for cycle in range(1, 6):
            for version, delta in (("original", 0), ("a", a), ("b", b)):
                result.append({"pair_id": f"r{cycle}-t{ti+1}-m1", "topic": topic, "cycle": cycle,
                    "version": version, "quality": 90 + delta, "accuracy": 70, "coverage": 18, "insight": 8,
                    "errors": 0, "omissions": 0, "contaminated": False})
    return result


class IndependentReportTests(unittest.TestCase):
    def test_sixty_texts_twelve_cells_equal_topic_means_and_correct_delta_direction(self):
        summary = aggregate(samples(), paired.TOPICS)
        self.assertEqual(len(summary["cells"]), 12)
        self.assertTrue(all(c["n"] == 5 for c in summary["cells"]))
        self.assertEqual([r["quality"] for r in summary["equal_topic_means"]], [90, 92, 92.75])
        self.assertEqual(summary["overall"]["a_minus_b"], -0.75)
        self.assertEqual([summary["overall"][k] for k in ("wins", "ties", "losses")], [5, 5, 10])

    def test_contaminated_answer_retained_but_whole_pair_excluded_from_clean_comparisons(self):
        source = samples(); affected = next(s for s in source if s["pair_id"] == "r1-t1-m1" and s["version"] == "a")
        affected.update(contaminated=True, quality=0)
        summary = aggregate(source, paired.TOPICS)
        self.assertEqual(summary["overall"]["graded_texts"], 60)
        self.assertEqual(summary["overall"]["clean_pairs"], 19)
        self.assertEqual(summary["overall"]["contaminated_texts"], 1)
        self.assertFalse(next(p for p in summary["pairs"] if p["pair_id"] == "r1-t1-m1")["clean"])
        self.assertEqual(next(c for c in summary["comparisons"] if c["topic"] == "preload")["a_minus_b"], 2)

    def test_static_report_switch_keeps_three_dimensions_and_distinguishes_reviewers(self):
        data = {"summary": aggregate(samples(), paired.TOPICS), "samples": [], "rubrics": {}, "limitations": [],
            "reviewers": [{"arm": "a", "label": "Astra 独立复查", "model": "gpt-6.2-astra", "effort": "max"},
                          {"arm": "b", "label": "Neon 独立自查", "model": "gemini-4-neon-xhigh", "effort": "xhigh"}]}
        doc = render(data)
        self.assertNotIn("<script", doc)
        self.assertIn('id="current" name="mode" type="radio" checked', doc)
        self.assertEqual(doc.count("data-version='a'"), 5)
        self.assertEqual(doc.count("data-version='b'"), 5)
        self.assertNotIn("相对原稿 未知", doc)
        self.assertIn("Full Access", doc)
        for text in ("准确", "覆盖", "洞察", "70.00/70", "18.00/20", "8.00/10", "Astra−Neon", "-0.75"):
            self.assertIn(text, doc)


if __name__ == "__main__":
    unittest.main()

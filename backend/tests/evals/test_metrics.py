"""evals.metrics 纯单元测试（不依赖 Qdrant）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.metrics import (  # noqa: E402
    evaluate_query,
    hit_at_k,
    mean_dict_of_floats,
    mrr,
    precision_at_k,
    recall_at_k,
)


class MetricsTest(unittest.TestCase):
    def test_hit_recall_precision_mrr(self):
        ranked = ["a", "b", "c", "d"]
        relevant = ["c", "x"]

        self.assertEqual(hit_at_k(ranked, relevant, 1), 0.0)
        self.assertEqual(hit_at_k(ranked, relevant, 3), 1.0)
        self.assertAlmostEqual(recall_at_k(ranked, relevant, 3), 0.5)
        self.assertAlmostEqual(recall_at_k(ranked, relevant, 10), 0.5)
        self.assertAlmostEqual(precision_at_k(ranked, relevant, 3), 1.0 / 3.0)
        self.assertAlmostEqual(mrr(ranked, relevant), 1.0 / 3.0)

    def test_empty_relevant(self):
        ranked = ["a", "b"]
        self.assertEqual(hit_at_k(ranked, [], 5), 0.0)
        self.assertEqual(recall_at_k(ranked, [], 5), 0.0)
        self.assertEqual(mrr(ranked, []), 0.0)

    def test_evaluate_query_and_mean(self):
        m1 = evaluate_query(["a", "b"], ["a"], [1, 3])
        m2 = evaluate_query(["x", "a"], ["a"], [1, 3])
        self.assertEqual(m1["hit_at"]["1"], 1.0)
        self.assertEqual(m2["hit_at"]["1"], 0.0)
        means = mean_dict_of_floats([m1["hit_at"], m2["hit_at"]], ["1", "3"])
        self.assertAlmostEqual(means["1"], 0.5)
        self.assertAlmostEqual(means["3"], 1.0)


if __name__ == "__main__":
    unittest.main()

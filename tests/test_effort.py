import copy
import math
import unittest

from effort import EFFORTS, FEATURE, analyze_effort, build_effort_reference, normalize_usage


def reference():
    ranges = [(0, 2, 4), (15, 20, 25), (35, 40, 45), (55, 60, 65), (75, 80, 85)]
    return {
        "schema_version": 1,
        "feature": FEATURE,
        "minimum_outputs": 3,
        "models": [{
            "id": "test-model",
            "reference_count": 15,
            "profiles": [{"effort": effort, "samples": 3, "minimum": math.log1p(lo), "median": math.log1p(mid), "maximum": math.log1p(hi)}
                         for effort, (lo, mid, hi) in zip(EFFORTS, ranges)],
        }],
    }


def output(reasoning=2, count=100):
    return {
        "text": ",".join(str(index % 355 + 1) for index in range(count)),
        "expected_count": count,
        "usage": {"reasoning_tokens": reasoning},
    }


class UsageTests(unittest.TestCase):
    def test_missing_reasoning_differs_from_reported_zero(self):
        missing = normalize_usage({"completion_tokens": 20}, "openai")
        zero = normalize_usage({"completion_tokens": 20, "completion_tokens_details": {"reasoning_tokens": 0}}, "openai")
        self.assertIsNone(missing["reasoning_tokens"])
        self.assertEqual(zero["reasoning_tokens"], 0)
        self.assertIsNone(normalize_usage(None, "responses"))

    def test_protocol_counts_preserve_reasoning_inclusive_output(self):
        usage = normalize_usage({"input_tokens": 10, "output_tokens": 50, "output_tokens_details": {"reasoning_tokens": 30}, "total_tokens": 60}, "responses")
        self.assertEqual(usage, {"input_tokens": 10, "output_tokens": 50, "reasoning_tokens": 30, "total_tokens": 60})
        anthropic = normalize_usage({"input_tokens": 12, "output_tokens": 80, "reasoning_tokens": 40}, "anthropic")
        self.assertIsNone(anthropic["reasoning_tokens"])
        self.assertIsNone(anthropic["total_tokens"])

    def test_invalid_counts_do_not_become_evidence(self):
        for value in [True, -1, 1.5, "4", float("nan"), float("inf"), 2**10000]:
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                normalize_usage({"reasoning_tokens": value}, "normalized")
        with self.assertRaises(ValueError):
            normalize_usage({"output_tokens": 2, "reasoning_tokens": 3}, "normalized")
        with self.assertRaises(ValueError):
            normalize_usage({"input_tokens": 4, "output_tokens": 5, "total_tokens": 8}, "normalized")


class EffortTests(unittest.TestCase):
    def test_usage_not_requested_effort_determines_result(self):
        rows = [output() for _ in range(3)]
        for row in rows:
            row["reasoning_effort"] = "max"
        result = analyze_effort(rows, "test-model", reference())
        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["prediction"], "low")
        self.assertAlmostEqual(result["observed"]["reasoning_tokens_per_100_numbers"], 2)

    def test_missing_usage_cannot_estimate_but_zero_can(self):
        rows = [output(0) for _ in range(3)]
        self.assertEqual(analyze_effort(rows, "test-model", reference())["prediction"], "low")
        rows[0]["usage"] = {"output_tokens": 200}
        result = analyze_effort(rows, "test-model", reference())
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["observed"]["used_outputs"], 2)
        self.assertIsNone(result["observed"]["usage_totals"]["reasoning_tokens"])

    def test_rejected_numeric_output_does_not_supply_third_usage(self):
        rows = [output(), output(), {"text": "refused", "expected_count": 300, "usage": {"reasoning_tokens": 2}}]
        result = analyze_effort(rows, "test-model", reference())
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["observed"]["valid_outputs"], 2)

    def test_normalization_uses_actual_number_count(self):
        rows = [output(4, 200), output(2, 100), output(6, 300)]
        result = analyze_effort(rows, "test-model", reference())
        self.assertEqual(result["prediction"], "low")
        self.assertAlmostEqual(result["observed"]["reasoning_tokens_per_100_numbers"], 2)
        rows[0]["expected_count"] = 250
        self.assertEqual(analyze_effort(rows, "test-model", reference())["prediction"], "low")

    def test_overlapping_profiles_abstain_from_unique_label(self):
        ref = reference()
        ref["models"][0]["profiles"][1]["minimum"] = math.log1p(1)
        result = analyze_effort([output() for _ in range(3)], "test-model", ref)
        self.assertEqual(result["status"], "ambiguous")
        self.assertIsNone(result["prediction"])
        self.assertEqual(set(result["candidates"]), {"low", "medium"})

    def test_range_boundary_is_inclusive_but_gap_and_extreme_abstain(self):
        self.assertEqual(analyze_effort([output(4) for _ in range(3)], "test-model", reference())["prediction"], "low")
        for reasoning in [10, 100000]:
            result = analyze_effort([output(reasoning) for _ in range(3)], "test-model", reference())
            self.assertEqual(result["status"], "out_of_reference")
            self.assertIsNone(result["prediction"])
            self.assertEqual(result["candidates"], [])

    def test_exact_model_conditioning_and_incomplete_reference(self):
        rows = [output() for _ in range(3)]
        for model in [None, "alias", "TEST-MODEL"]:
            self.assertEqual(analyze_effort(rows, model, reference())["status"], "unavailable")
        ref = reference()
        other = copy.deepcopy(ref["models"][0])
        other["id"] = "other-model"
        other["profiles"][0]["minimum"] = math.log1p(5)
        other["profiles"][0]["median"] = math.log1p(7)
        other["profiles"][0]["maximum"] = math.log1p(9)
        other["profiles"][1]["minimum"] = 0
        other["profiles"][1]["median"] = math.log1p(2)
        other["profiles"][1]["maximum"] = math.log1p(3)
        ref["models"].append(other)
        self.assertEqual(analyze_effort(rows, "other-model", ref)["prediction"], "medium")
        ref["models"][0]["profiles"].pop()
        self.assertEqual(analyze_effort(rows, "test-model", ref)["status"], "unavailable")

    def test_median_aggregation_resists_one_long_reasoning_response(self):
        result = analyze_effort([output(), output(), output(100000)], "test-model", reference())
        self.assertEqual(result["prediction"], "low")
        self.assertEqual(result["observed"]["usage_totals"]["reasoning_tokens"], 100004)


class ReferenceTests(unittest.TestCase):
    def records(self):
        rows = []
        for effort in EFFORTS:
            for index in range(3):
                rows.append({
                    "requested_model": "test-model", "effort": effort, "completed": True,
                    "challenge": {"id": f"prompt-{index}", "expected_count": 100},
                    "text": output()["text"],
                    "usage": {"output_tokens": 202, "output_tokens_details": {"reasoning_tokens": index}},
                })
        return rows

    def test_failed_and_missing_reasoning_samples_are_excluded(self):
        rows = self.records()
        failed = copy.deepcopy(rows[0])
        failed["completed"] = False
        failed["challenge"]["id"] = "truncated"
        missing = copy.deepcopy(rows[0])
        missing["usage"] = {"output_tokens": 200}
        missing["challenge"]["id"] = "missing"
        rows.extend([failed, missing])
        ref = build_effort_reference(rows, {})
        self.assertEqual(ref["models"][0]["reference_count"], 15)
        for profile in ref["models"][0]["profiles"]:
            self.assertEqual(profile["samples"], 3)
            self.assertEqual(profile["minimum"], 0)
            self.assertAlmostEqual(profile["median"], math.log1p(1))
            self.assertAlmostEqual(profile["maximum"], math.log1p(2))

    def test_duplicate_probe_identity_is_rejected(self):
        rows = self.records()
        rows.append(copy.deepcopy(rows[0]))
        with self.assertRaises(ValueError):
            build_effort_reference(rows, {})


if __name__ == "__main__":
    unittest.main()

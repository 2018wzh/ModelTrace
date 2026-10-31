import assert from "node:assert/strict";
import { test } from "node:test";
import { analyzeEffort, normalizeUsage } from "../static/effort-core.js";

function reference() {
  return {
    schema_version: 1, feature: "log1p_reasoning_tokens_per_100_numbers", minimum_outputs: 3,
    models: [{ id: "test-model", reference_count: 15,
      profiles: ["low", "medium", "high", "xhigh", "max"].map((effort, index) => ({
        effort, samples: 3, minimum: Math.log1p(index * 20), median: Math.log1p(index * 20 + 2), maximum: Math.log1p(index * 20 + 4),
      })),
    }],
  };
}

function output(reasoning = 2, count = 100) {
  return { text: Array.from({ length: count }, (_, index) => index % 355 + 1).join(","), expected_count: count, usage: { reasoning_tokens: reasoning } };
}

test("Pages distinguishes missing reasoning usage from an explicit zero", () => {
  const zero = Array.from({ length: 3 }, () => output(0));
  assert.equal(analyzeEffort(zero, "test-model", reference()).prediction, "low");
  zero[0].usage = { output_tokens: 200 };
  const missing = analyzeEffort(zero, "test-model", reference());
  assert.equal(missing.status, "unavailable");
  assert.equal(missing.observed.used_outputs, 2);
  assert.equal(missing.observed.usage_totals.reasoning_tokens, null);
});

test("Pages normalizes actual counts, not requested effort or expected length", () => {
  const rows = [output(2), output(4, 200), output(6, 300)];
  rows[0].reasoning_effort = "max";
  rows[1].expected_count = 250;
  const result = analyzeEffort(rows, "test-model", reference());
  assert.equal(result.prediction, "low");
  assert.ok(Math.abs(result.observed.reasoning_tokens_per_100_numbers - 2) < 1e-10);
});

test("Pages abstains on overlaps, outside ranges, and unknown models", () => {
  const rows = Array.from({ length: 3 }, () => output());
  const ref = reference();
  ref.models[0].profiles[1].minimum = 0;
  const overlapping = analyzeEffort(rows, "test-model", ref);
  assert.equal(overlapping.status, "ambiguous");
  assert.equal(overlapping.prediction, null);
  assert.deepEqual(new Set(overlapping.candidates), new Set(["low", "medium"]));
  assert.equal(analyzeEffort(Array.from({ length: 3 }, () => output(1000)), "test-model", ref).status, "out_of_reference");
  assert.equal(analyzeEffort(rows, "model-alias", ref).status, "unavailable");
  ref.models[0].profiles.pop();
  assert.equal(analyzeEffort(rows, "test-model", ref).status, "unavailable");
});

test("Pages cannot infer from rejected text or invalid usage", () => {
  const rows = [output(), output(), { text: "refused", usage: { reasoning_tokens: 2 } }];
  assert.equal(analyzeEffort(rows, "test-model", reference()).status, "unavailable");
  for (const value of [true, -1, 1.5, "0", NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
    assert.throws(() => normalizeUsage({ reasoning_tokens: value }));
  }
  assert.throws(() => normalizeUsage({ output_tokens: 2, reasoning_tokens: 3 }));
  assert.throws(() => normalizeUsage({ input_tokens: 2, output_tokens: 3, total_tokens: 4 }));
});

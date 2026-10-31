import { parseNumbers } from "./fingerprint-core.js";

const EFFORTS = ["low", "medium", "high", "xhigh", "max"];
const TOKEN_FIELDS = ["input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"];
const FEATURE = "log1p_reasoning_tokens_per_100_numbers";
const METHOD = "按参考模型比较每 100 个有效数字的推理 token；经验区间，不是校准概率";

function median(values) {
  const ordered = [...values].sort((a, b) => a - b);
  const middle = Math.floor(ordered.length / 2);
  return ordered.length % 2 ? ordered[middle] : (ordered[middle - 1] + ordered[middle]) / 2;
}

export function normalizeUsage(raw) {
  if (raw == null) return null;
  if (typeof raw !== "object" || Array.isArray(raw)) throw new Error("usage 必须是对象或 null");
  const counts = Object.fromEntries(TOKEN_FIELDS.map((key) => {
    const value = raw[key] ?? null;
    if (value !== null && (!Number.isSafeInteger(value) || value < 0)) throw new Error(`${key} 必须是非负整数或 null`);
    return [key, value];
  }));
  if (counts.output_tokens !== null && counts.reasoning_tokens !== null && counts.reasoning_tokens > counts.output_tokens) {
    throw new Error("reasoning_tokens 不能大于 output_tokens（输出包含推理）");
  }
  if (counts.total_tokens !== null && counts.input_tokens !== null && counts.output_tokens !== null
      && counts.total_tokens < counts.input_tokens + counts.output_tokens) {
    throw new Error("total_tokens 不能小于 input_tokens + output_tokens");
  }
  return counts;
}

export function analyzeEffort(outputs, model, reference) {
  if (reference.schema_version !== 1 || reference.feature !== FEATURE) throw new Error("不支持的推理档位参考库格式");
  const features = [];
  const usages = [];
  for (const output of outputs) {
    const numbers = parseNumbers(output.text ?? "");
    const expected = Number(output.expected_count || 0);
    const minimum = expected ? Math.max(80, Math.ceil(expected * 0.55)) : 80;
    if (numbers.length < minimum) continue;
    const usage = normalizeUsage(output.usage);
    usages.push(usage);
    if (usage?.reasoning_tokens != null) features.push(Math.log1p(100 * usage.reasoning_tokens / numbers.length));
  }
  const observed = features.length ? median(features) : null;
  const totals = Object.fromEntries(TOKEN_FIELDS.map((key) => [key,
    usages.length && usages.every((usage) => usage?.[key] != null)
      ? usages.reduce((sum, usage) => sum + usage[key], 0) : null,
  ]));
  const result = {
    status: "unavailable", model: model || null, prediction: null, candidates: [], reason: "",
    observed: {
      used_outputs: features.length, valid_outputs: usages.length,
      reasoning_tokens_per_100_numbers: observed === null ? null : Math.expm1(observed),
      usage_totals: totals,
    },
    ranking: [], reference_samples: 0, method: METHOD,
  };
  const modelReference = reference.models.find((item) => item.id === model);
  if (!modelReference) {
    result.reason = "请选择参考库中的准确模型版本；接口别名不会自动映射到数字指纹的候选模型。";
    return result;
  }
  result.reference_samples = modelReference.reference_count;
  const available = new Set(modelReference.profiles.filter((profile) => profile.samples >= 3).map((profile) => profile.effort));
  if (available.size !== EFFORTS.length || !EFFORTS.every((effort) => available.has(effort))) {
    result.reason = "参考模型缺少完整五档样本；每档至少需要 3 份有效回答。";
    return result;
  }
  const minimumOutputs = Math.max(3, Number(reference.minimum_outputs || 3));
  if (features.length < minimumOutputs) {
    result.reason = `需要至少 ${minimumOutputs} 份有效回答的独立 reasoning_tokens，目前有 ${features.length} 份；缺失用量不能按 0 处理。`;
    return result;
  }
  for (const profile of modelReference.profiles) {
    if (profile.samples < 3) continue;
    result.ranking.push({
      effort: profile.effort, samples: profile.samples,
      minimum: Math.expm1(profile.minimum), median: Math.expm1(profile.median), maximum: Math.expm1(profile.maximum),
      distance: Math.abs(observed - profile.median),
      in_range: profile.minimum - 1e-12 <= observed && observed <= profile.maximum + 1e-12,
    });
  }
  result.ranking.sort((a, b) => a.distance - b.distance || EFFORTS.indexOf(a.effort) - EFFORTS.indexOf(b.effort));
  result.candidates = result.ranking.filter((item) => item.in_range).map((item) => item.effort);
  if (result.candidates.length === 1) {
    result.status = "estimated";
    [result.prediction] = result.candidates;
    result.reason = "观测值仅落入一个档位的实测区间；这是样本有限的条件估计，不是档位真实性证明。";
  } else if (result.candidates.length) {
    result.status = "ambiguous";
    result.reason = "多个档位的实测区间重叠，当前 token 用量不能唯一确定档位。";
  } else {
    result.status = "out_of_reference";
    result.reason = "观测值不在任何实测档位区间内；仅展示邻近参考，不强行判定。";
  }
  return result;
}

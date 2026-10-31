from __future__ import annotations

import json
import math
from pathlib import Path
from statistics import median

from fingerprint import parse_numbers


DEFAULT_REFERENCE = Path(__file__).parent / "static" / "data" / "effort_reference.json"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
FEATURE = "log1p_reasoning_tokens_per_100_numbers"
TOKEN_FIELDS = ("input_tokens", "output_tokens", "reasoning_tokens", "total_tokens")
MAX_SAFE_INTEGER = 2**53 - 1
METHOD = "按参考模型比较每 100 个有效数字的推理 token；经验区间，不是校准概率"


def _token_count(value, field: str) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value < 0
        or value > MAX_SAFE_INTEGER
        or not math.isfinite(value)
        or int(value) != value
    ):
        raise ValueError(f"{field} 必须是非负整数或 null")
    return int(value)


def normalize_usage(raw: dict | None, api_format: str) -> dict | None:
    """Keep missing counts unknown; OpenAI output counts include reasoning."""
    if api_format not in {"openai", "responses", "anthropic", "normalized"}:
        raise ValueError(f"未知用量格式：{api_format}")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("usage 必须是对象或 null")
    if api_format == "openai":
        details = raw.get("completion_tokens_details") or {}
        if not isinstance(details, dict):
            raise ValueError("completion_tokens_details 必须是对象")
        counts = {
            "input_tokens": raw.get("prompt_tokens"),
            "output_tokens": raw.get("completion_tokens"),
            "reasoning_tokens": details.get("reasoning_tokens"),
            "total_tokens": raw.get("total_tokens"),
        }
    elif api_format == "responses":
        details = raw.get("output_tokens_details") or {}
        if not isinstance(details, dict):
            raise ValueError("output_tokens_details 必须是对象")
        counts = {
            "input_tokens": raw.get("input_tokens"),
            "output_tokens": raw.get("output_tokens"),
            "reasoning_tokens": details.get("reasoning_tokens"),
            "total_tokens": raw.get("total_tokens"),
        }
    else:
        counts = {key: raw.get(key) for key in TOKEN_FIELDS}
        if api_format == "anthropic":
            # Anthropic does not expose a separate reasoning-token counter here.
            counts["reasoning_tokens"] = None
    counts = {key: _token_count(value, key) for key, value in counts.items()}
    output, reasoning = counts["output_tokens"], counts["reasoning_tokens"]
    if output is not None and reasoning is not None and reasoning > output:
        raise ValueError("reasoning_tokens 不能大于 output_tokens（输出包含推理）")
    input_count, total = counts["input_tokens"], counts["total_tokens"]
    if total is not None and input_count is not None and output is not None and total < input_count + output:
        raise ValueError("total_tokens 不能小于 input_tokens + output_tokens")
    return counts


def load_effort_reference(path: Path = DEFAULT_REFERENCE) -> dict:
    reference = json.loads(Path(path).read_text(encoding="utf-8"))
    if reference.get("schema_version") != 1 or reference.get("feature") != FEATURE:
        raise ValueError("不支持的推理档位参考库格式")
    return reference


def _accepted_numbers(output: dict) -> list[int]:
    numbers = parse_numbers(str(output.get("text", "")))
    expected = int(output.get("expected_count") or 0)
    minimum = max(80, math.ceil(expected * 0.55)) if expected else 80
    return numbers if len(numbers) >= minimum else []


def analyze_effort(outputs: list[dict], model: str | None, reference: dict | None = None) -> dict:
    reference = load_effort_reference() if reference is None else reference
    if reference.get("schema_version") != 1 or reference.get("feature") != FEATURE:
        raise ValueError("不支持的推理档位参考库格式")
    features = []
    usages = []
    for output in outputs:
        numbers = _accepted_numbers(output)
        if not numbers:
            continue
        usage = normalize_usage(output.get("usage"), "normalized")
        usages.append(usage)
        if usage is not None and usage["reasoning_tokens"] is not None:
            features.append(math.log1p(100 * usage["reasoning_tokens"] / len(numbers)))
    observed = median(features) if features else None
    totals = {
        key: sum(usage[key] for usage in usages)
        if usages and all(usage is not None and usage[key] is not None for usage in usages)
        else None
        for key in TOKEN_FIELDS
    }
    result = {
        "status": "unavailable",
        "model": model or None,
        "prediction": None,
        "candidates": [],
        "reason": "",
        "observed": {
            "used_outputs": len(features),
            "valid_outputs": len(usages),
            "reasoning_tokens_per_100_numbers": math.expm1(observed) if observed is not None else None,
            "usage_totals": totals,
        },
        "ranking": [],
        "reference_samples": 0,
        "method": METHOD,
    }
    model_reference = next((item for item in reference["models"] if item["id"] == model), None)
    if model_reference is None:
        result["reason"] = "请选择参考库中的准确模型版本；接口别名不会自动映射到数字指纹的候选模型。"
        return result
    result["reference_samples"] = model_reference["reference_count"]
    available = {profile["effort"] for profile in model_reference["profiles"] if profile["samples"] >= 3}
    if available != set(EFFORTS):
        result["reason"] = "参考模型缺少完整五档样本；每档至少需要 3 份有效回答。"
        return result
    minimum_outputs = max(3, int(reference.get("minimum_outputs", 3)))
    if len(features) < minimum_outputs:
        result["reason"] = f"需要至少 {minimum_outputs} 份有效回答的独立 reasoning_tokens，目前有 {len(features)} 份；缺失用量不能按 0 处理。"
        return result
    for profile in model_reference["profiles"]:
        if profile["samples"] < 3:
            continue
        in_range = profile["minimum"] - 1e-12 <= observed <= profile["maximum"] + 1e-12
        result["ranking"].append({
            "effort": profile["effort"],
            "samples": profile["samples"],
            "minimum": math.expm1(profile["minimum"]),
            "median": math.expm1(profile["median"]),
            "maximum": math.expm1(profile["maximum"]),
            "distance": abs(observed - profile["median"]),
            "in_range": in_range,
        })
    result["ranking"].sort(key=lambda row: (row["distance"], EFFORTS.index(row["effort"])))
    result["candidates"] = [row["effort"] for row in result["ranking"] if row["in_range"]]
    if len(result["candidates"]) == 1:
        result["status"] = "estimated"
        result["prediction"] = result["candidates"][0]
        result["reason"] = "观测值仅落入一个档位的实测区间；这是样本有限的条件估计，不是档位真实性证明。"
    elif result["candidates"]:
        result["status"] = "ambiguous"
        result["reason"] = "多个档位的实测区间重叠，当前 token 用量不能唯一确定档位。"
    else:
        result["status"] = "out_of_reference"
        result["reason"] = "观测值不在任何实测档位区间内；仅展示邻近参考，不强行判定。"
    return result


def build_effort_reference(records: list[dict], provenance: dict) -> dict:
    """Import completed clean-probe evidence, never requested-effort echoes as query features."""
    groups = {}
    seen = set()
    for record in records:
        if record.get("completed") is not True:
            continue
        model, effort = record.get("requested_model"), record.get("effort")
        challenge = record.get("challenge") or {}
        if not model or effort not in EFFORTS or not challenge.get("id"):
            continue
        numbers = _accepted_numbers({"text": record.get("text", ""), "expected_count": challenge.get("expected_count")})
        usage = normalize_usage(record.get("usage"), "responses")
        if not numbers or usage is None or usage["reasoning_tokens"] is None:
            continue
        identity = (model, effort, challenge["id"])
        if identity in seen:
            raise ValueError(f"重复的参考样本：{model}/{effort}/{challenge['id']}")
        seen.add(identity)
        values = groups.setdefault(model, {}).setdefault(effort, [])
        values.append(math.log1p(100 * usage["reasoning_tokens"] / len(numbers)))
    models = []
    for model, effort_values in sorted(groups.items()):
        profiles = []
        for effort in EFFORTS:
            values = effort_values.get(effort, [])
            if len(values) >= 3:
                profiles.append({"effort": effort, "samples": len(values), "minimum": min(values), "median": median(values), "maximum": max(values)})
        if profiles:
            models.append({"id": model, "reference_count": sum(map(len, effort_values.values())), "profiles": profiles})
    if not models:
        raise ValueError("没有包含独立推理 token 用量且每档至少 3 份有效回答的参考数据")
    return {
        "schema_version": 1,
        "feature": FEATURE,
        "minimum_outputs": 3,
        "provenance": provenance,
        "models": models,
    }

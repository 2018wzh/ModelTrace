from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from effort import DEFAULT_REFERENCE, build_effort_reference


def main() -> None:
    parser = argparse.ArgumentParser(description="从完成的 ModelTrace Codex 档位实验导入 token 参考数据")
    parser.add_argument("records", type=Path, help="逐次原始记录 records.jsonl")
    parser.add_argument("--manifest", type=Path, required=True, help="记录挑战和干净请求条件的 manifest.json")
    parser.add_argument("--output", type=Path, default=DEFAULT_REFERENCE)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    method = manifest.get("method") or {}
    if method.get("instructions") != "" or method.get("history") is not False or method.get("tools") is not False:
        parser.error("参考数据必须来自空系统提示、无历史、无工具的独立整数挑战")
    records = [json.loads(line) for line in args.records.read_text(encoding="utf-8").splitlines() if line.strip()]
    challenges = {item["id"]: item for item in manifest["challenges"]}
    for record in records:
        challenge = record.get("challenge") or {}
        if challenge != challenges.get(challenge.get("id")):
            parser.error("原始记录中的挑战与 manifest 不一致")
    reference = build_effort_reference(records, {
        "source": manifest.get("source"),
        "source_commit": manifest.get("commit"),
        "model_bank_sha256": manifest.get("bank_sha256"),
        "collected_at": manifest.get("started_at"),
        "provider": method.get("provider"),
        "records_sha256": hashlib.sha256(args.records.read_bytes()).hexdigest(),
        "conditions": "空系统提示；无历史和工具；独立短整数挑战；服务端报告的推理 token",
        "labels": "请求中的推理档位标签；不构成对服务端模型或档位真实性的独立验证",
        "scope": "仅用于同模型、同类独立整数挑战；经验区间未经概率校准",
    })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(reference, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sample_count = sum(model["reference_count"] for model in reference["models"])
    print(f"已导入 {sample_count} 份有效样本、{len(reference['models'])} 个模型：{args.output}")


if __name__ == "__main__":
    main()

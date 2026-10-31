export function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[character]);
}

const tokenFields = [
  ["reasoning_tokens", "推理 token"],
  ["input_tokens", "输入 token"],
  ["output_tokens", "输出 token（含推理）"],
  ["total_tokens", "总 token"],
];

export function renderUsageInputs(index) {
  return `<details class="usage-inputs">
    <summary>填写这份回答的 API usage（可选）</summary>
    <p class="effort-note">填写接口实际返回的 token 数；未提供的留空，明确返回 0 时填 0。输出 token 包含推理 token。</p>
    <div class="form-grid two">${tokenFields.map(([field, label]) => `
      <label for="usage-${index}-${field}"><span>${label}</span><input id="usage-${index}-${field}" type="number" min="0" step="1" inputmode="numeric" placeholder="未知"></label>
    `).join("")}</div>
  </details>`;
}

export function readManualOutputs(challenges, root = document) {
  return challenges.map((challenge, index) => {
    const usage = Object.fromEntries(tokenFields.map(([field, label]) => {
      const input = root.getElementById(`usage-${index}-${field}`);
      const raw = input.value.trim();
      if (input.validity.badInput) throw new Error(`挑战 ${index + 1} 的${label}必须是非负整数。`);
      if (raw === "") return [field, null];
      const value = Number(raw);
      if (!Number.isSafeInteger(value) || value < 0) throw new Error(`挑战 ${index + 1} 的${label}必须是非负整数。`);
      return [field, value];
    }));
    if (usage.reasoning_tokens !== null && usage.output_tokens !== null && usage.reasoning_tokens > usage.output_tokens) {
      throw new Error(`挑战 ${index + 1} 的推理 token 不能大于输出 token。`);
    }
    if (usage.total_tokens !== null && usage.input_tokens !== null && usage.output_tokens !== null && usage.total_tokens < usage.input_tokens + usage.output_tokens) {
      throw new Error(`挑战 ${index + 1} 的总 token 不能小于输入与输出之和。`);
    }
    return {
      text: root.getElementById(`output-${index}`).value,
      expected_count: challenge.expected_count,
      usage: Object.values(usage).every((value) => value === null) ? null : usage,
    };
  });
}

export async function loadEffortReference(url) {
  const response = await fetch(url, { cache: "no-cache" });
  if (!response.ok) throw new Error(`推理档位参考库加载失败（HTTP ${response.status}）`);
  return response.json();
}

export function populateEffortModels(select, reference, blankLabel = "不选择参考模型") {
  select.innerHTML = `<option value="">${escapeHtml(blankLabel)}</option>` + reference.models.map((model) =>
    `<option value="${escapeHtml(model.id)}">${escapeHtml(model.id)} · ${escapeHtml(model.reference_count)} 份参考</option>`
  ).join("");
  select.disabled = false;
}

function number(value) {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })
    : "未知";
}

export function renderEffortResult(result) {
  const effort = result || { status: "unavailable", reason: "未获得推理档位分析结果。" };
  const labels = { estimated: "经验估计", ambiguous: "多个档位重叠", out_of_reference: "超出参考范围", unavailable: "无法估计" };
  const observed = effort.observed || {};
  const totals = observed.usage_totals || {};
  const candidates = effort.candidates || [];
  const rows = (effort.ranking || []).map((item) => `
    <tr class="${item.in_range ? "winner" : ""}">
      <td><strong>${escapeHtml(item.effort)}</strong></td><td>${number(item.samples)}</td>
      <td>${number(item.minimum)} – ${number(item.maximum)}</td><td>${number(item.median)}</td>
      <td>${item.in_range ? "落入范围" : "范围外"}</td>
    </tr>`).join("");
  return `<section class="effort-result" aria-label="推理档位估计">
    <div class="effort-heading"><strong>推理档位估计</strong><span class="status-tag ${effort.status === "estimated" ? "ok" : "warning"}">${escapeHtml(labels[effort.status] || "无法估计")}${effort.prediction ? ` · ${escapeHtml(effort.prediction)}` : ""}</span></div>
    <p class="effort-note">${escapeHtml(effort.reason || "")}</p>
    <p class="effort-note">参考模型：<strong>${escapeHtml(effort.model || "未选择")}</strong>${candidates.length ? `；候选档位：${candidates.map(escapeHtml).join("、")}` : ""}。参考样本 ${number(effort.reference_samples)} 份。</p>
    <div class="effort-metrics">
      <div><span>每 100 个数字的推理 token（中位数）</span><strong>${number(observed.reasoning_tokens_per_100_numbers)}</strong></div>
      <div><span>用于档位估计 / 数字有效回答</span><strong>${number(observed.used_outputs)} / ${number(observed.valid_outputs)}</strong></div>
      ${tokenFields.map(([field, label]) => `<div><span>${label}合计</span><strong>${number(totals[field])}</strong></div>`).join("")}
    </div>
    ${rows ? `<div class="table-wrap"><table><thead><tr><th>参考档位</th><th>样本数</th><th>经验范围（推理 token / 100 数字）</th><th>参考中位数</th><th>观测位置</th></tr></thead><tbody>${rows}</tbody></table></div>` : ""}
    <p class="effort-note">这是指定模型下的经验估计，与上方模型归因独立。参考来自短提示词、纯整数输出；范围重叠时保留多个候选，不提供校准概率。请求的 reasoning effort 只是配置，不是推断证据。合计中任一有效回答缺少的字段显示为未知。</p>
  </section>`;
}

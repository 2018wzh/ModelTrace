import { analyzeGlobalOutputs } from "./fingerprint-core.js";
import { generateChallenges } from "./challenge-browser.js";
import { analyzeEffort } from "./effort-core.js";
import { escapeHtml, loadEffortReference, populateEffortModels, readManualOutputs, renderEffortResult, renderUsageInputs } from "./effort-ui.js";

const state = { bank: null, challenges: [], effortReference: null };
const byId = (id) => document.getElementById(id);


function percent(value) {
  return `${(value * 100).toFixed(1)}%`;
}

function setMessage(text, type = "error") {
  const element = byId("test-message");
  element.textContent = text;
  element.className = `message ${type}`;
  element.hidden = !text;
}

async function copyText(text, button) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  button.textContent = "已复制";
  window.setTimeout(() => { button.textContent = "复制提示词"; }, 1000);
}

function renderChallenges() {
  byId("result").hidden = true;
  byId("challenge-list").innerHTML = state.challenges.map((challenge, index) => `
    <article class="challenge-item">
      <div class="challenge-header">
        <strong>挑战 ${index + 1}</strong>
        <span>${challenge.expected_count} 个数字</span>
        <button type="button" data-copy="${index}">复制提示词</button>
      </div>
      <div class="challenge-columns">
        <div><label>发送给待测模型</label><pre>${escapeHtml(challenge.prompt)}</pre></div>
        <div><label for="output-${index}">粘贴完整输出</label><textarea id="output-${index}" spellcheck="false" placeholder="保留文字、标点、代码块和完整数字序列"></textarea></div>
      </div>
      ${renderUsageInputs(index)}
    </article>
  `).join("");
  document.querySelectorAll("[data-copy]").forEach((button) => {
    button.addEventListener("click", () => copyText(state.challenges[Number(button.dataset.copy)].prompt, button));
  });
}

function regenerate() {
  state.challenges = generateChallenges(3);
  setMessage("");
  renderChallenges();
}

function renderResult(payload) {
  const diagnostics = payload.diagnostics.map((item, index) => `
    <span class="diagnostic ${item.accepted ? "accepted" : "rejected"}">挑战 ${index + 1}: ${item.parsed_numbers} 个数字 · ${item.accepted ? "计入" : "忽略"}</span>
  `).join("");
  const rows = payload.results.map((item, index) => `
    <tr class="${index === 0 ? "winner" : ""}">
      <td>${index + 1}</td>
      <td><strong>${escapeHtml(item.display_name)}</strong></td>
      <td>${escapeHtml(item.family_name)}</td>
      <td><div class="probability-cell"><span><i style="width:${item.probability * 100}%"></i></span><strong>${percent(item.probability)}</strong></div></td>
      <td>${percent(item.profile_similarity)}</td>
    </tr>
  `).join("");
  const result = byId("result");
  result.innerHTML = `
    <div class="result-summary">
      <div><span>最可能模型</span><strong>${escapeHtml(payload.prediction_name)}</strong></div>
      <div><span>统一库概率</span><strong>${percent(payload.probability)}</strong></div>
      <div><span>模型家族</span><strong>${escapeHtml(payload.family_prediction_name)} · ${percent(payload.family_probability)}</strong></div>
      <div><span>有效查询</span><strong>${payload.used_outputs}/3</strong></div>
    </div>
    <div class="diagnostics">${diagnostics}</div>
    <div class="table-wrap"><table><thead><tr><th>排序</th><th>候选模型</th><th>家族</th><th>归因概率</th><th>分布相似度</th></tr></thead><tbody>${rows}</tbody></table></div>
    ${renderEffortResult(payload.effort)}
    <div class="result-guidance" role="note" aria-label="结果说明">
      <p>本工具仅对指纹库内的模型进行归因；若待测模型不在指纹库中，得到任何结果都有可能。</p>
      <p>Claude Code 的系统提示词会影响模型偏好，测试结果存在较大偏差，建议不要在 Claude Code 中测试。</p>
    </div>
  `;
  result.hidden = false;
  result.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function analyze() {
  const button = byId("analyze");
  button.disabled = true;
  byId("result").hidden = true;
  setMessage("正在浏览器本地计算……", "working");
  await new Promise((resolve) => requestAnimationFrame(resolve));
  try {
    const outputs = readManualOutputs(state.challenges);
    const payload = analyzeGlobalOutputs(outputs, state.bank);
    payload.effort = state.effortReference
      ? analyzeEffort(outputs, byId("manual-effort-model").value || null, state.effortReference)
      : { status: "unavailable", reason: "推理档位参考库尚未加载，模型归因不受影响。" };
    renderResult(payload);
    setMessage("");
  } catch (error) {
    setMessage(error.message || "无法完成归因。", "error");
  } finally {
    button.disabled = false;
  }
}

async function initialize() {
  try {
    const response = await fetch("./data/unified_bank.json", { cache: "no-cache" });
    if (!response.ok) throw new Error(`指纹库加载失败（HTTP ${response.status}）`);
    state.bank = await response.json();
    const responseCount = state.bank.models.reduce((sum, model) => sum + model.response_count, 0);
    byId("topbar-bank-count").textContent = `${state.bank.models.length} 个候选模型`;
    byId("active-bank-badge").textContent = `${state.bank.models.length} 个模型 · ${responseCount} 条指纹`;
    byId("regenerate").disabled = false;
    byId("analyze").disabled = false;
    regenerate();
  } catch (error) {
    setMessage(`${error.message}。请通过 HTTP 服务或 GitHub Pages 打开本页面。`, "error");
  }
}

async function initializeEffortModels() {
  const note = document.querySelector("[data-effort-reference-note]");
  try {
    state.effortReference = await loadEffortReference("./data/effort_reference.json");
    populateEffortModels(byId("manual-effort-model"), state.effortReference);
    note.textContent = "推理档位使用实际 usage，与模型归因分别在本地计算。至少需要三份含推理 token 的有效回答。";
  } catch (error) {
    note.textContent = `${error.message}；模型归因仍可使用。`;
  }
}

byId("regenerate").addEventListener("click", regenerate);
byId("analyze").addEventListener("click", analyze);
initialize();
initializeEffortModels();

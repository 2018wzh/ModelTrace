# ModelTrace

ModelTrace 是一个本地运行的主动模型归因工具。它通过三条独立的长整数生成挑战提取输出指纹，在统一候选库中自动判断模型家族和具体版本。

## 运行

```powershell
python -m pip install -r requirements.txt
python start.py
```

页面地址为 `http://127.0.0.1:7860/`。

## GitHub Pages

`static/index.html` 是不依赖后端的手动测试版本，归因计算和指纹库读取都在浏览器本地完成。仓库附带的 GitHub Actions 会将 `static/` 部署到 GitHub Pages。

## 使用

- **手动测试**：复制三条挑战，分别发送给同一个待测模型，再粘贴每次完整输出。
- **API 自动测试**：填写 Base URL、API Key 和模型名，可显式选择 OpenAI Chat Completions、OpenAI Responses 或 Anthropic Messages；自动模式会依次尝试适用协议，以三份有效回答为目标完成归因。Codex 兼容中转接口应选择 Responses。
- **指纹库管理**：可以新建指纹库，或通过 API 为现有指纹库添加模型指纹。

API Key 只用于当前页面发起请求，不写入磁盘。
自动采集会在对应的 `*_reference.jsonl` 中保存实际 user prompt、base prompt、system prompt、user prefix、原始和归一化 usage 及返回模型名；拟合后的 `*_bank.json` 与 `unified_bank.json` 仍只保存模型身份的统计指纹和校准参数。

## 推理档位估计

模型身份归因之外，页面可使用 API 返回的 token 用量，独立估计指定模型的 `low / medium / high / xhigh / max` 档位。Flask 页面和 GitHub Pages 均支持手动填写每份回答的 usage；API 自动测试会保留服务端实际返回的用量。

- 选择准确的**推理档位参考模型**。API 模式默认精确匹配接口模型名；使用别名时需手动选择，不会用数字指纹的候选模型代替。
- 至少三份数字有效的回答必须具有独立的 `reasoning_tokens`。未报告该字段时保留未知，不能把它当作 0；明确报告 0 才是有效的零推理用量。Anthropic Messages 未提供独立推理计数时，只展示已知用量。
- 每份回答的特征为 `log1p(100 × reasoning_tokens / 实际解析数字数)`，查询使用三份或更多回答的特征中位数。按模型与参考档位的实测最小值、中位数、最大值比较，归一化避免数字序列长度直接主导消耗。
- 只落入一个档位区间时给出经验估计；区间重叠时保留多个候选；缺少参考或用量、五档参考不全、观测值超出所有区间时不强行判定。不输出未经校准的档位概率，也不改变原有模型身份评分。

共享参考库 `static/data/effort_reference.json` 来自 2026 年 10 月 31 日的 120 份有效 Codex 实测回答：8 个模型、5 档、每档 3 份。参考仅覆盖空系统提示、无历史和工具的独立短整数挑战；不适用于长任务上下文、其他提示词或任意模型。参考标签是请求的档位配置，不是对服务端真实性的独立验证。当前小样本区间有明显重叠，显示多个候选是正常结果。

API 参数：

- `/api/test/probe`、`/api/test/auto` 接受 `api_format`（`auto/openai/responses/anthropic`）和可选 `reasoning_effort`。请求档位只是配置，不作为估计特征；Anthropic 不接受此设置。指纹库采集保留原有协议自动识别流程。
- `/api/test/probe` 返回 `text`、`usage`、`raw_usage`、`response_model`、`api_format` 和数字有效性；Responses 流必须有正常的 `response.completed`，失败、截断和断流不进入评分。
- `/api/analyze` 接受 `effort_model` 和 `outputs[].usage`，返回独立的 `effort` 结果；`/api/test/auto` 同时返回实际采用的 `outputs`。归一化 usage 形如 `{"input_tokens":180,"output_tokens":1200,"reasoning_tokens":300,"total_tokens":1380}`，未知字段为 `null`，OpenAI 的输出计数已经包含推理，不能再次相加。
- Python `request_completion()` 现在返回包含上述文本与用量的对象，不再仅返回字符串。

可以从同类实验的原始记录重建参考库，不需要修改模型身份指纹库：

```powershell
python build_effort_reference.py path/to/records.jsonl --manifest path/to/manifest.json
```

导入器核验 manifest 中的干净请求条件与逐条挑战，只采用正常完成、数字有效且具有推理计数的样本；重复的模型/档位/挑战记录会报错。参考 JSON 仅保留模型档位区间、样本数、来源与原始记录摘要，不重复分发逐次实验明细，不保存凭据。

长整数探针可能持续输出至服务端 token 上限，低档位也不例外；截断或断流不代表没有消耗。页面 token 合计仅覆盖进入分析的数字有效回答、且所有回答均已报告的对应字段，不是整轮请求的账单。

验证命令：

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
node --test tests/*.test.mjs
```

## 指纹方法

### Codex 任务内监测插件

[ModelTrace Guard](codex-plugin/modeltrace-guard/README.md) 按工具调用次数，在后台从原 Codex 任务的冻结快照分别 fork 进行普通检测和异常复测，测后清理临时分支。正常结果静默保存，主任务无需等待检测。用户可调整工具间隔、复测次数（默认 3）、有效期和十种语言，不设每轮或任务累计探针上限。网页仪表盘提供完整历史分页和实时提醒；首次不一致要求智能体告知并暂停工作，额外复测全部不一致时停止原任务并拦截后续受支持的工作工具。探针由 Codex 自身生成并消耗对应推理额度，评分使用插件附带的 ModelTrace 指纹库，页面刷新不消耗模型额度。

安装后在 Codex CLI 的 `/hooks` 中审阅并信任插件 hooks，再在目标任务中开启监测。安装、配置和结果解释详见[插件使用说明](codex-plugin/modeltrace-guard/README.md)。

### 核心算法

所有模型使用同一个全局特征空间、同一套特征与权重：

```text
0.75 × 去除环境方向后的 Hellinger 模型中心相似度
+ 0.25 × 有序块数字序列特征
```

建库时使用共享环境的平均偏移估计主要干扰方向。归因时先从数字分布和有序特征中投影掉这些方向，再同时与全部模型中心比较。三份回答分别评分后取平均，并使用与查询数量对应的校准温度转换为概率。

具体模型概率由一次全局 softmax 得到，家族概率是该家族下各模型概率之和：

```text
P(具体模型) = softmax(β × 全局模型得分)
P(模型家族) = Σ P(该家族中的具体模型)
```

结果是当前候选库内、均匀先验下的闭集概率。未收录模型仍会被归到最相似的现有候选。

## 项目结构

```text
app.py              Web 接口
fingerprint.py      指纹提取与归因
effort.py           token 用量归一化与独立档位估计
build_effort_reference.py  从实验原始记录重建档位参考库
bank_builder.py     指纹库构建与概率校准
challenge_suite.py  自动建库挑战
enrollment.py       API 调用与指纹采集
rebuild_unified_bank.py  重建统一全局库
data/               参考数据与指纹库
static/             页面资源
templates/          页面模板
```

## 指纹库说明

项目中现有指纹库共包含两个模型家族、18 个模型：

```
gpt-5.4
gpt-5.5
gpt-5.6-luna
gpt-5.6-terra
gpt-5.6-sol
gpt-6-astra
gpt-6-sol
gpt-6-luna
claude-haiku-4-5-20251001
claude-haiku-5-5
claude-sonnet-4-6
claude-sonnet-5
claude-sonnet-5-5
claude-opus-4-6
claude-opus-4-7
claude-opus-4-8
claude-opus-5
claude-opus-5-5
```

GPT 采集自官方订阅 Codex，Claude 采集自 [OAIPro](https://api.oaipro.com/)。

## 声明

测试结果仅供参考，并非判断模型的决定性证据

注意该项目为归因工具，只对指纹库内的模型尽可能去判断属于哪种模型，若待测模型不在指纹库中，得到任何结果都是有可能的

目前已知因系统提示词严重影响模型偏好，在Claude Code中得到的测试结果存在较大偏差，建议不要在Claude Code中测试

## 致谢

感谢 [hanlinwenyuan/hlwy-ai-checker](https://github.com/hanlinwenyuan/hlwy-ai-checker)。该项目较早将语言模型的随机数字生成偏差用于第三方 API 渠道一致性检查，为 ModelTrace 提供了重要参考。



[LINUX DO](https://linux.do/)

# 执行接口

所有命令使用 `python -X utf8 <skill>/scripts/benchmark.py ...`。Python 3.11+；脚本不安装依赖、不修改 Codex 配置，复用当前登录和 provider。默认只在 `run` / `resume` 发起模型推理。

## 工程与暂存区

先确定 skill 所在绝对目录。运行暂存资料放 `$CODEX_HOME/benchmarks/_staging/<unique-id>`（未配置时使用 `~/.codex`），不要放工程里。已登记项目和最近至多 50 条普通任务及 pinned 任务可由 Codex 工具读取；保存最小元数据为 JSON，不保留任务正文或凭据。

```text
benchmark.py discover --project <explicit-root>
benchmark.py discover --current <current-working-directory>
benchmark.py discover --no-current --projects-json <projects.json> --threads-json <threads.json> --output <selection.json>
benchmark.py history
```

`projects.json` 接受项目工具返回的 `{projects:[...]}`，字段使用 `projectId / projectKind / hostId / label / path`；`threads.json` 为 `{pinnedThreads:[...],threads:[...]}`，只需 `id / projectId / hostId / updatedAt`。也接受包含 text JSON 的 MCP 返回包装。将 workspace 记录转换为同一最小项目格式即可复用排序；没有任务数据时按路径稳定排序，并明确没有使用频次证据。远端与云项目不参与本机回退。

`discover` 返回入口工程，不解析业务规则语义；代理仍须读取它的 AGENTS 和 workspace 映射，确定实际源码仓后写入 case。

## Case bundle

代理根据实际工程生成以下文件；这些是输入契约，不是固定题库。

```text
case/
  case.json
  question.txt
  gold.json
  evidence/S1.txt
  evidence/S2.txt
```

`question.txt` 只写问题、已知条件、期望回答形式及明确的证据边界。脚本将 sources 中的全部摘录追加为统一题面；不要重复手工粘贴。所有摘录转为 UTF-8，保留逻辑路径和原始行号，记录原文件字节 SHA256；旧编码按实际编码读取后转换，不把转码后的哈希冒充原文件哈希。

`case.json` 最小示例（实际候选与依据必须来自当前工程）：

```json
{
  "title": "配置覆盖顺序为何导致启动参数未生效",
  "project": {"name": "example", "root": "<resolved-project-root>", "selection_reason": "current project"},
  "candidates": [
    {"title": "配置覆盖顺序为何导致启动参数未生效", "why_common": "近期维护涉及多个配置入口", "verifiable": "可从解析顺序和小型断言验证", "selected": true},
    {"title": "缓存何时失效", "why_common": "日常状态刷新路径", "verifiable": "可核对状态转换与回归断言", "selected": false},
    {"title": "异步结果为何没有更新界面", "why_common": "常用交互调用链", "verifiable": "可沿消费条件核对", "selected": false}
  ],
  "sources": [
    {"id": "S1", "file": "evidence/S1.txt", "logical_path": "src/config.cpp", "ranges": "L20-L80", "source_sha256": "<64 lowercase hex characters>"}
  ]
}
```

`gold.json` 必须有 `reference_facts`、`verification`、`rubric`。`verification` 记录实际执行或静态对照的方法、结果与可复核依据，不能写未运行的验证。rubric 每项有 `id / dimension / max / criterion`。新题默认声明 `scoring_version: 2`，dimension 为 `accuracy / coverage / insight / readability`，合计分别为50/20/10/20。未声明或明确为1时保持旧70/30，未知版本拒绝。具体事实、必答要点和允许洞察随题目生成；不得把模型答案当标准答案。

```json
{
  "scoring_version": 2,
  "reference_facts": ["来自证据的已验证事实及允许推断的范围"],
  "verification": [{"method": "实际使用的方法", "result": "实际结果与依据"}],
  "rubric": [
    {"id": "A1", "dimension": "accuracy", "max": 50, "criterion": "已陈述的关键事实、因果与建议是否正确；具体错误及扣分锚点须随题细化"},
    {"id": "C1", "dimension": "coverage", "max": 20, "criterion": "按本题必答清单检查遗漏与部分回应，错误不重复扣覆盖"},
    {"id": "I1", "dimension": "insight", "max": 10, "criterion": "材料支持的有用边界洞察；列出允许洞察及具体分值，不按长度计分"},
    {"id": "R1", "dimension": "readability", "max": 20, "criterion": "结论组织、自然表达、详略与易执行性"}
  ]
}
```

## 价格快照

通过 OpenAI Docs 搜索并读取当次官方价格页面。写 `prices.json`，`as_of` 用实际核对日期，`sources` 是支持数据的官方页面 URL，`models` 以精确 model ID 为键。各 rate 是每百万 token 的数值，仅填已验证字段：`input_usd / cached_usd / write_usd / output_usd / input_credits / cached_credits / output_credits`。不支持的字段省略；不从旧报告无条件继承价格，不编造内部别名映射。

无可靠价格时不传 `--prices`，报告费用为未知。输出 token 含 reasoning，不重复计费。估算缓存费用要求输入、缓存读取、缓存写入及输出计数齐全；字段未上报不当作 0。无缓存对照只需输入和输出。Codex credits 单独按其官方口径估算，不加未经验证的缓存写入加价。

## 准备、预检、采样

```text
benchmark.py prepare --case-dir <case> [--run-dir <new-directory>] [--prices <prices.json>]
benchmark.py prepare --case-dir <case> --models gpt-6-sol --efforts xhigh max --repetitions 5
benchmark.py preflight --run-dir <run> [--codex <explicit-executable>]
benchmark.py run --run-dir <run> [--codex <explicit-executable>]
benchmark.py resume --run-dir <run>
benchmark.py resume --run-dir <run> --retry-failed
```

`prepare` 将输入封存在 `input/`，生成 protocol、种子、计划及哈希；不能覆写已有运行目录。不要编辑封存文件。预检检查全部请求组合，不发起推理，不支持时停止且不自动替换。当前 App 二进制优先，随后已安装 App 和 PATH；可用 `--codex` 明确指定。

运行输出仅展示总体进度，避免向随后评分者泄露映射。每个计划样本存入 `runs/<slot>/attempt-<n>/`，保留 `result.json / answer.txt / events.jsonl`。发生错误时查看经过脱敏的诊断和进度；不要打印 config/auth/原始 stderr。

`resume` 跳过成功和已有失败的样本，继续尚未开始的计划；`--retry-failed` 才新增失败样本的尝试，不删除旧记录。有活跃 runner/app-server 时持续等待，不重复启动。传输中断的远端完成状态可能未知，重试可能产生额外费用；报告保留该缺口。运行环境指纹变化时另建复测批次。

服务端对 `turn/start` 返回匹配的 JSON-RPC 错误时，该次请求记为明确失败并清理 inflight；它仍需显式重试。连接中断不能据此认定远端已停止，继续保留 inflight 并先核实进程状态。

## 评分与报告

```text
benchmark.py blind --run-dir <run>
benchmark.py blind --run-dir <run> --refresh
benchmark.py seal --run-dir <run> --scores <completed-scores.json>
benchmark.py report --run-dir <run>
benchmark.py validate --run-dir <run>
```

只读取 `grading/packet/`，复制 `scores-template.json` 为待填写文件。每个匿名 ID 必须逐项填写数值 `points` 和文字 `notes`；null、缺项、空依据均拒绝封存。V2 另需 `evidence`（同评分项ID，每项为原文字符串列表，必须能在该答案中精确找到；正分项至少一段）和显式 `issues` 列表。问题字段为 `kind: error|omission`、`severity: critical|minor`、`criterion`、`quote`、`note`；错误需非空原文，遗漏可用空quote但须解释缺失。没有问题填空列表。全部有效答案封存后才可读报告与映射。不要把运行记录或性能信息带入评分。已封存评分不能原地覆盖；确需更正时建立独立修订并保留旧证据。

封存、报告及校验会检查评分包的 `prompt.txt / gold.json` 与原始封存输入一致；匿名答案、原始答案及记录的答案哈希一致；评分映射恰好覆盖本批次所有选中的有效答案，没有重复或遗漏。

`blind` 默认复用并校验现有包，不覆盖正在填写的分数。若在匿名化后执行 `resume --retry-failed` 导致答案集合变化，旧映射会被拒绝；运行 `blind --refresh` 后对新的全部匿名答案重新评分。该命令只在采样已终止且评分尚未封存时有效，将旧 `grading/` 完整保存在本批次 `grading-history/<unique-id>/`，连同未完成的模板和备注保留在结果包中。新包使用新的匿名编号，旧分数文件不能套用到新包。受损的未封存包也可通过此命令保留后重建；已经封存的评分无法刷新。

首次 `report` 提供揭示结果。随后写运行目录下的 `assessment.json`，再次生成报告：

```json
{"conclusion": "本题的选择建议及适用条件", "evidence": ["引用具体指标或事实错误"], "limitations": ["本次实际证据缺口"]}
```

`report --partial` 仅用于明确标识的未完成报告，可缺评分；它不构成完整验收，也不据此宣称最优模型。脚本产生 HTML、UTF-8 BOM CSV、完整 JSON、validation 和 ZIP。若费用只覆盖部分样本，合计须连同覆盖次数阅读。

`validate` 从原始记录重算全部 JSON 字段（生成时间除外），核对 CSV 表头和逐行内容，并用相同渲染函数核对完整 HTML；导出文件不齐、数值、答案或标签不一致均拒绝通过。修改选择建议后重新运行 `report`，不要直接编辑导出文件。

若所有尝试都失败而没有可评分答案，跳过 blind/seal，用 `report --partial` 展示失败与缺口；不能生成质量排名。

## 已有答案离线重审

```text
benchmark.py regrade --run-dir <original-run> --rubric <v2-gold.json> --output <new-sibling-directory>
benchmark.py blind --run-dir <new-sibling-directory>
benchmark.py seal --run-dir <new-sibling-directory> --scores <v2-scores.json>
benchmark.py report --run-dir <new-sibling-directory>
benchmark.py validate --run-dir <new-sibling-directory>
```

原批次须已评分、生成报告且无活跃或未核实请求。V2 gold 保留原 `reference_facts / verification`，只修订评分版本、细则及说明，先固定再逐份审计。目标必须是不存在且不包含原目录的新目录；不覆盖原封存评分，不从重审目录继续重审，应始终以原采样批次创建另一修订。

`regrade` 只复制允许的原题面、输入、用量、答案及必要事件，保存原评分与summary供事后对照；不创建模型会话，不读取auth/config。`evaluation/manifest.json` 的来源记录、哈希和新细则由新protocol绑定，修改任何输入、答案、用量或细则均拒绝通过；新目录的 `input/gold.json` 仍是原件，活动V2细则为 `evaluation/rubric.json`，匿名包里提供同哈希的 `rubric.json`。

评分时仅读新packet中的题面、原gold、V2 rubric、匿名答案和空模板。不要读取旧分数、summary、映射或性能；新分数封存后再看旧新对照。标注“事后新增细则，评分者此前见过模型身份”，不声称全新独立盲评。所有性能和费用均复用原记录，不把评分时间计入推理耗时，也不把原费用当成本次新增费用。

重审目录的 `preflight / run / resume / retest` 均拒绝；需要付费原题复测时指定原采样目录。新报告为独立HTML/CSV/JSON/ZIP，V2导出含版本、权重、四维分数、逐项原文依据及错误/遗漏；V1导出保持原结构。不同评分版本单独展示，不合并排名。

## 原题复测

```text
benchmark.py retest --parent <previous-run> --efforts max [--prices <fresh-prices.json>]
benchmark.py preflight --run-dir <new-run>
benchmark.py run --run-dir <new-run>
```

`retest` 只准备新批次，沿用原题、摘录、指令和 gold，默认每组 5 次，可覆盖模型与档位。父批次须已评分并生成报告。价格快照须重新核对，不传时标未知。父批次统计单独嵌入新报告，不复制全部私有历史答案、不合并抽样；环境变化会提示。

## 维护验证

运行 `python -X utf8 -m unittest discover -s <skill>/scripts/tests -v`。历史回放用 `python -X utf8 <skill>/scripts/replay_history.py --cohorts <old-batch-1> <old-batch-2> <old-batch-3> --output <new-validation-directory>`，只读取本机历史记录，输出独立报告，绝不启动推理。历史业务材料不随 skill 分发。

Node.js 可用时，validate 会额外运行离线交互脚本检查，保存 `ui-check.json`；只模拟 DOM 与选择操作，不打开浏览器、不发网络请求，也不冒充浏览器目视检查。完整运行仍只要求 Python 标准库。

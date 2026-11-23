# Antigravity CLI 循环测试

用于用户指定以官方 `agy` 复测已封存的工程题目。所有操作通过命令行，不调用桌面自动化，不重写临时 runner。遵守用户选定的模型、次数及题目顺序。

## 准备与预检

先执行 `agy --version`、`agy models`；可通过 `agy -p /usage` 查询额度。安装位置和参数以当次官方文档及本机 help 为准。优先复用系统安全凭据，不导出 token；需要重新登录时由用户完成。使用 `--settings` 的会话覆盖，不更改全局权限或默认模型。

现有 sealed 批次必须具有相同源码并集和指令。以下命令只读旧记录并创建新批次，不发起推理：

```text
benchmark.py study-prepare --source-runs <题1批次> <题2批次> <题3批次> <题4批次> --study-dir <新目录> --model gemini-4-neon-xhigh --effort xhigh --cycles 5
benchmark.py study-preflight --study-dir <新目录>
benchmark.py study-status --study-dir <新目录>
```

`study-prepare` 复用题面、证据、gold、评分版本及原字节源码，另建源码只读区和每份隔离的 scratch。schedule 按 `cycle → 题目顺序` 封存；每个题目的 repetition 对应同一轮次。普通 Codex 命令和历史批次保持原行为。

零推理预检只证明配置、模型目录、输入和运行时指纹。CLI 没有提供零推理的原生工具执行入口，实际隔离必须由下一步独立付费能力验收证实，不能把配置声明当作通过。

## 能力验收与采样

```text
benchmark.py study-smoke --study-dir <新目录>
benchmark.py study-run --study-dir <新目录>
benchmark.py study-resume --study-dir <新目录>
benchmark.py study-resume --study-dir <新目录> --retry-failed
```

`study-smoke` 只调用一次所选模型，独立计量，不算正式答案。要求真实原生工具读取冻结源码、写 scratch、阻止只读区控制文件写入及私有 canary 读取，并按工具回执和文件哈希验证；失败保留记录且阻止正式采样，不自动重复付费验收。

源码只通过窄 `read_file` 规则开放；scratch 是会话工作目录。终端仅允许在原生沙箱内自动执行，拒绝网页和 MCP，禁止自动批准沙箱外命令。不使用 `--dangerously-skip-permissions`。原生委派策略保持默认，不为得分要求子代理数量。

以上为默认 `restricted` 模式。用户明确要求 Full Access 时，在新 `study-prepare` 上加 `--access-mode full-access`，封存为 `project-full-access-cli`。本模式使用 `--dangerously-skip-permissions`，会话覆盖设 `enableTerminalSandbox=false`、`allowNonWorkspaceAccess=true`、`toolPermission=always-proceed`，清空本次 Ask/Deny，不传 `--sandbox`，不更改全局默认配置。验收只检查真实源码读取、scratch 命令写入和最终回答，取消拒写／拒读哨兵要求；有效 `init.permission_mode` 必须为 `always-proceed`。旧受限模式与失败记录不修改。

Full Access 下仍明确要求模型只分析冻结材料、不读取历史答案、gold、评分、会话或记忆，不调用 Computer Use，不修改业务源码。工具参数中出现明确评分／历史资料路径时记录污染标记并排除干净样本统计；此审计仅覆盖 CLI 可见工具，不声称系统隔离或完整子代理访问覆盖。用户已明确授权继续测试和修复权限故障时，无需重复确认；失败调用保留，通过 `study-smoke --retry-failed` 或 `study-resume --retry-failed` 追加尝试，不覆盖原记录。

完整原生事件已证明成功，仅本地解析失败时，可创建同题、同源码、同权限的新批次，使用 `study-recover-smoke --study-dir <新批次> --source <原批次>` 离线复核。只允许 runner 代码指纹变化，校验原有效终态、实际命令输出、源码哈希和 Full Access 回执；保留原失败，复制原回执但不改原模型身份／消耗，历史付费调用单列，新调用为0。不能从模型宣称或残余文字补造工具证据。

每份正式答案使用独立 CLI 进程和新会话，通过 stdin 提交题面，以 `stream-json` 收集允许字段。只以终态 `result.response` 提取答案；不拼接中间回复，不保存私有推理或原始 stderr。模型身份不符、非成功终态、空答案、多终态回执等均不是有效样本。并发隔离启用 `--new-project` 时，脚本在确认终态后按本轮唯一 `scratch` 路径精确回收新建的临时 `~/.gemini/config/projects/<uuid>.json`（不触碰既有用例、默认项目、sidecar 或代码仓项目）；也可按需传 `project` 复用固定项目。异常中断遗留的已完成临时项目可通过 `benchmark.py cleanup-agy-projects`（默认 dry-run，加 `--apply` 与可选 `--archive-dir` 执行）回收。

成功样本不重跑。额度或限流明确失败后停止追加请求、保留全部尝试和下一位置；`study-resume` 不自动重试失败，用户授权后才加 `--retry-failed`。缺少终态时保留 inflight，先检查实际进程和持久证据，不能从本地进程退出推断远端已停。不设置整体生成截止时间。

## 评分与交付

正式采样完成后，各题复用 `blind / seal / report / validate`，再用 `compare` 生成四题综合表。额度不足可先用各题 `report --partial` 交付实际覆盖与缺口，不宣称完成五轮。

与历史 Codex 按题并列，执行模式按批次标为 `project-readonly-cli` 或 `project-full-access-cli`；工具、指令渠道和沙箱不同，不合并统计或称为相同工程代理环境。保留 CLI 原生累计 usage 和实际耗时；子代理覆盖、全树终态、最终正文 token/TPS 以及费用未核实时均为未知，不套用 OpenAI 费率，也不把未知子代理费用填成零。评分者已知请求模型，匿名编号不等于独立盲评。

官方入口：<https://antigravity.google/docs/cli/headless/>、<https://antigravity.google/docs/cli/reference>、<https://antigravity.google/docs/permissions/>。

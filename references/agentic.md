# 工程只读代理执行

用于日常工程分析中的模型、思考档和速度档选型。它衡量允许检索、只读验证和原生委派的工作流；不衡量修改业务代码、外部系统操作或大型构建。题目、验证事实和评分仍按实验方法先封存，不从旧答案复制评分。

## 封存源码与权限

`prepare` CLI 默认 `project-readonly`，必须传 `--execution-spec <JSON>`：

```json
{
  "source_roots": [{"path": "<resolved-source-directory>", "logical_path": "project/src"}],
  "runtime_read_roots": ["<narrow-python-runtime-directory>", "<narrow-powershell-runtime-directory>", "<rg-executable>"]
}
```

source_roots 可以指定完整模块、必要调用者、SDK、对照工程或源文件；保留逻辑目录，避免只给预选答案路径。先解析工程入口与编码，核对问题所需源码范围。所有组合使用同一快照；文件集合、原字节哈希、排除目录和来源均封存。原始 GB2312 等文件保持字节不变，由模型按实际编码读取。标准答案、评测历史、凭据、缓存与会话不得充当源码。

源码同时保存在私有 `input/source-snapshot/` 和独立的模型可读临时目录，前者进入归档；scratch 为唯一可写目录。每份结束后安全归档并清空 scratch，拒绝跟随链接、junction 和 hardlink。离线导出只依赖私有副本，采样时额外核对实际工作区和运行库哈希。

runtime_read_roots 只开放执行所需的狭窄程序/库路径，不能开放用户目录、磁盘根、整个评测目录。例如自包含 PowerShell 还需它的运行库目录；仅放行 exe 可能无法启动。工具进程环境使用系统与路径白名单，临时目录指向 scratch，不继承鉴权变量。

当前工程模式使用 Windows 原生 MXC，按进程设置 `windows.sandbox=mxc`，不改全局配置。named permission profile 只开放平台 `:minimal`、源码与运行库读取、scratch 写入，不给 `:root` 读取。thread/start 使用 `permissions`，不能同时传旧 `sandbox` 字段。其他平台或不支持所需隔离的二进制必须说明缺口并停止，不静默换后端。

## 预检与能力验收

```text
benchmark.py prepare --case-dir <case> --execution-spec <spec.json> --models <model-a> <model-b> --efforts xhigh ultra --service-tiers default fast --repetitions 5 --prices <fresh-prices.json>
benchmark.py preflight --run-dir <run>
benchmark.py smoke --run-dir <run>
benchmark.py run --run-dir <run>
```

`preflight` 不发起模型推理。除了模型目录与速度能力，还以同一二进制和权限配置运行 command/exec 哨兵，验证源码可读、scratch 可写、源码不可写，以及旧评测、gold、会话和记忆不可读。包括绝对路径、`..`、Windows 路径别名及可用的 junction 对照；检查未实际执行的部分须披露。不得使用不受线程沙箱约束的 thread/shellCommand 或 process/spawn 冒充权限验证。失败保留报告，不生成成功 seal。

`smoke` 是显式付费运行，与正式样本分开存档；每模型×速度档执行一次 Ultra，要求真实子代理读取源码、主代理核对、收齐任务树及逐响应 usage。两模型×两速度档即4次。失败不自动重试，正式采样被阻止；保留原记录，修复后新建批次。smoke 只证明能力可用，不能计入质量/性能样本。正式题不要求委派数量，零子代理也是有效自然观测。

若故障仅来自归约器，且原事件、逐响应用量、终态和权限证据完整，可在修复后创建独立批次、完成相同运行环境的无推理预检，再显式离线复核：

```text
benchmark.py recover-smoke --run-dir <fresh-revision> --source <failed-run>
benchmark.py smoke --continue --run-dir <fresh-revision> --codex <sealed-binary>
```

`recover-smoke` 不启动模型或修改原批次，按原归档顺序重放首条能力验收。原答案、事件、逐响应用量和执行指纹保留；修复代码版本、来源哈希、修订账本及验证依据另存。缺失的身份、设置、usage 或权限不能靠复核补造。报告分列历史付费尝试、零调用复核和本批新增付费尝试；离线复核不重复计费。`smoke --continue` 仅执行未尝试位置，遇到任何失败即停止，不能重试已失败位置，也不能通过删除失败记录继续。

多题研究可先对一题运行 `smoke`，其他题各自完成无推理 `preflight` 后执行 `adopt-smoke --run-dir <target> --source <verified-run>`。只有相同的稳定二进制、源码、工具/权限、模型矩阵、代理配置和指令指纹才能共用；每题应使用相同的完整源码并集快照。共用验收保留4份原记录及同一 capability_id，目标新增尝试为0、当地新增验收费用为0，历史验收费用单列。身份不符即拒绝，不自动追加付费验收。

题面、开发者指令和继承规则都要检查，不能只解除 runner 的禁令却在题面继续禁止工具。thread/start 的有效 permission profile 必须与封存配置相符，后代也要保留自己的权限回执；实际能力验收检查继承，缺失或不符阻止正式采样。指令来源保存文件内容哈希，不能只散列路径列表。

子代理初始配置可能只成为通知基线，不发送 settings/updated。runner 仅对已发现、已加载且已有历史的后代使用不带任何配置覆盖的 `thread/resume`（`threadId`、`excludeTurns:true`），读取实际权限、模型和速度档；不加载已卸载线程，不改名或补造历史，不启动新 turn。回执记录为有 RPC 来源的账本事件，不能冒充原生通知；回执之前缺失的速度档和费用保持未知。

封存二进制/版本、模型目录及 Ultra 解析依据、原生代理后端、角色/默认子模型/effort、并发配置、指令链、工具与权限。回执中的 ultra 名称不等于已证明具体底层推理档；记录请求与目录解析依据。子代理可能选择不同模型或未沿用 Fast，须按实际可观察设置报告，不推断网关内部路由。

预检通过 `config/read(includeLayers:true,cwd)` 和 `configRequirements/read` 封存实际合并配置、配置层及策略要求的哈希，只展示白名单内的原生工具与代理配置。引用的角色和指令文件也按内容核验。跨批身份仅规范化已知批次路径和生成权限 ID；实际权限、层顺序、并发或外部配置变化仍区分。未显式配置的数值标注沿用封存二进制原生默认，不能声称已从配置读到。

## 任务树与消耗

持久化隔离线程，收集所有子孙线程及乱序关系事件；根线程完成后继续发现、核对后代状态及各线程后台终端，直到已启动工作终止或空闲，不要求可复用线程全部关闭。不设置整体等待截止时间。连接中断时保留已观测账本及 inflight，不能把未知远端状态认作结束，也不能自动重新提交。

逐响应 `rawResponse/completed` 的 responseId 去重计量；这是实验协议能力，必须实际验证。累计 thread/tokenUsage 通知仅作交叉核验，不能累加，也不能把 fork 继承的历史重新计费。事件归档只保留允许字段，不保存私有推理、完整响应元数据或原始错误。

RPC 等待期间收到的通知须每轮按 FIFO 消费，不延迟至根线程完成。累计计数按观测时间归约，旧事件不覆盖新值；同时间冲突记录异常，不能逐字段取最大值拼出虚构计数。线程状态及 turn 开始通知同样防止旧消息重新激活已完成工作。设置通知按历史时间归约，晚到的早期证据可重算受影响响应的模型和速度档；较晚的回执不能追溯填补此前未知设置。原始归档顺序与所有事件均保留。

分别报告主代理、子代理、全树 input/cache/output/reasoning 和费用。逐响应按实际模型、速度档及上下文区间选价；cache write 的保留时长未知时不猜费率。缺少节点、usage 或价格时保留未知及已观测部分；失败尝试已发生的消耗仍计入记录。不能用根 Fast 费率给整棵树定价。官方估算不代表网关实扣。

根 final_answer 单独提取用于评分；commentary 与子代理正文不拼入交付答案。分列正文首字、进度首字、答案交付、根 turn 结束及全树耗时。多次响应的根线程无法可靠归因最终正文 token 时，正文解码 TPS 为未知；主线程/全树输出吞吐另行明确标注，不能混用时间区间。

## 比较与恢复

工程模式改变了证据获取和执行条件，不能套用旧闭卷结果或旧分数。复用题目时重新核验事实，执行模式变化必须新 prepare；旧 retest 会复制旧指令。普通闭卷保留显式 `--execution-mode fixed-evidence` 供受限实验与历史兼容，新 Ultra 采样拒绝该模式。

run/resume 在采样前核验权限、能力 seal 和完整环境指纹；成功不重跑，失败仅用户授权后显式重试。离线 report/compare 不启动推理，不依赖仍存在的临时源码工作区，不回写原批次。质量按题与细则分别比较，执行模式/源码/指令/环境不同不能自动合并；跨批速度观察明确标历史对照。

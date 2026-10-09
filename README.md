# Agent Benchmark Harness

一个面向工具型 Agent 的可插拔评测骨架。隔离环境默认为 Docker（`TaskSpec.environment_type` 默认 `"docker"`），`local` 环境必须显式 opt-in；在此基础上跑通完整链路，后续可接入 Inspect AI、Harbor、BrowserGym、OSWorld、MCP 和多 Agent 运行时。

## 当前能力

- 版本化任务 schema（Pydantic）
- `tasks/<name>/` 任务目录为单一真相来源；`tasks/<name>/workspace/` 起始文件在 `reset()` 时拷入 trial 工作区
- **Docker 为默认环境，`local` 为显式 opt-in**：`TaskSpec.environment_type` 默认 `"docker"`；`DockerEnvironment` 用 `resolve_docker_binary`（显式参数 → `BENCHMARK_DOCKER_BIN` → `PATH` → macOS Docker Desktop 路径）定位 CLI，并在 `reset()` 时对 daemon 做 preflight，失败时报出可操作的错误；容器默认非 root、`--network none`、`--cap-drop ALL`、`--security-opt no-new-privileges`，verifier 用只读挂载的独立容器
- Agent / Environment / Grader 解耦
- scripted agent baseline
- `agent: cli` 通用编码 CLI 适配器（`CLIAgent`），以 `argv` / `stdin` / `prompt_file` 三种模式投递 prompt，`profiles.py` 提供 `claude` / `codex` 起始配置；它在宿主机执行，因此要求 `environment_type: local`
- 任务集：`tasks/` 下 4 个任务（`fix_fizzbuzz` / `parse_log_counts` / `makefile_target` / `seed_reproducible`），每个都带 `oracle.sh` 参考解；`configs/tasks.yaml` 通过 `tasks_root` 加载
- 4 个任务均用 `grader_type: composite`：`PolicyGrader` 行为门禁 + 自动注入的 `tests/test.sh` verifier。`PolicyGrader` 把 task 根的 `oracle.sh` 纳入受保护基线（改写/删除报 `verifier_tampering`、复制进工作区报 `verifier_copy`），并对引用 task 目录或 `oracle.sh` 的命令报 `reference_solution_access`（high、fail-closed）
- `benchmark oracle --tasks-root tasks`：对每个任务运行其 oracle 与自带 verifier，任一任务失败则以非零码退出（任务集自洽性自检）
- `provider` 名称真正选择 LLM backend（`openai` / `openai-compatible`，未配置时回退默认；未知名称在构建期报错，不再静默走 OpenAI 兼容）
- `RunConfig.dataset` 作为 provenance 写入 `report.json` 与 SQLite 索引，并可用 `benchmark compare --dataset` 过滤
- observation/action/tool-result JSONL trajectory
- `replay` 真正重放已记录的动作并与轨迹结果对比；`regrade` 基于 artifact 快照重评分（二者语义分离，见下）
- command grader（支持部分分）与 state grader；composite grader 支持权重与阈值
- 任务目录用 `grader_type: composite` 时，`tests/test.sh` 会自动注入为 command 子 grader，因此行为门禁与正确性检查可同时生效，而 verifier 仍只有一份
- trial 结果和汇总报告（按 trial 分组，重试不稀释成功率）
- 重试默认覆盖模型/网关错误（`runner_error` / `environment_error` / `timeout` / `error`）
- run manifest 与带 sha256 的 artifact 索引
- run 级指纹：`harness_version`、git commit、provider 配置摘要、依赖版本与 run config
- artifact 落盘上限（单文件 / 总量 / 数量）与 ignore 规则，轨迹 JSONL 按字符截断
- 结构化日志（`--log-level` / `--log-json`），核心链路带 `run_id` / `trial_id` 上下文
- SQLite 跨 run 索引与横向对比（`benchmark index` / `benchmark compare`）：索引 schema v2，`trials` 增加 `violations` / `uncertainties` / `violation_count`，`runs` 增加 `violation_count` / `uncertain_trials`；旧库（含 `PRAGMA user_version = 0`）在打开时自动迁移；`compare --violations` 只比较「任一次 attempt 记录过 violation」的 run（与按最终 attempt 计的成功率口径不同，见「统计口径」）
- CI（ruff lint + mypy 类型检查 + pytest + `benchmark oracle` 任务集自检）与 `uv.lock` 依赖锁
- 可扩展的 CLI

## 快速开始

推荐用 `uv`（会按 `uv.lock` 精确复原依赖）：

```bash
uv sync --extra dev
uv run pytest
uv run python -m benchmark run configs/example.yaml --output reports/example
```

也可以继续用 venv + pip：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
python -m benchmark run configs/example.yaml --output reports/example
```

结果：

```text
reports/example/report.json
reports/example/manifest.json
reports/example/traces/<trial-id>.jsonl
reports/example/artifacts/<task-id>/<run>/<attempt>/index.json
```

**环境默认值：Docker。** `TaskSpec.environment_type` 默认为 `"docker"`，真实任务默认跑在容器里，`local` 必须显式 opt-in。上面的 smoke run 之所以无需 Docker 即可完成，是因为 `tasks/hello_task/task.yaml` 显式写了 `environment_type: local`（随附的 4 个任务同样如此）；run config 本身不设环境默认值，命令行可用 `--environment` 覆盖。`tasks/hello_task/Dockerfile` 是一个 docker 环境基线，仅在任务选用 `environment_type: docker` 并配置 `environment.image`（预构建镜像名）时才相关；它由任务记录在 `metadata.dockerfile`，harness 不会自动 build。

## 任务目录

一个任务由一个目录定义，各文件各司其职，不重复：

```text
tasks/hello_task/
  instruction.md    # Agent 指令（唯一来源）
  tests/test.sh     # verifier（唯一来源，经 bash -c 执行）
  task.yaml         # 其余配置：id、seed、limits、adapter 选择、scripted actions
  Dockerfile        # 可选环境基线
```

在 `task.yaml` 里重复定义 `instruction` 或 `grader.command` 会被显式拒绝，避免两份副本各自漂移。唯一的例外是 `grader_type: composite`：`task.yaml` 只声明行为子 grader（如 `- type: policy`），`tests/test.sh` 由加载器自动注入为 command 子 grader，因此 verifier 仍只有一份，同时行为门禁与正确性检查都会执行。

run config 通过目录引用任务：

```yaml
task_dirs:
  - ../tasks/hello_task
# 或批量加载：
# tasks_root: ../tasks
```

也可用 `--task-dir tasks/hello_task` 与 `--tasks-root tasks` 在命令行指定。

## 统计口径

`report.json` 的 `trials` 保留每一次 attempt 作为审计轨迹，而 `summary` 按 `parent_trial_id` 分组、每组只计最终 attempt。因此「重试 3 次后成功」计为 1 次成功而非 1/3，`total_attempts` 与 `retried_groups` 单独暴露重试量。

outcome 指标（`success_rate` / `average_score` / `failures_by_category`）始终按最终 attempt 计；而 violation / uncertainty 计数是 **any-attempt** 语义：某次 attempt 记录过的 violation 不会因为后续重试成功而被洗掉，`runs.violation_count` 等于该 run 所有 `trials.violation_count` 之和，`compare --violations` 也据此过滤。

## 重放与重评分

二者都读取已有 run 目录，但语义不同，不可混用：

- `benchmark replay RUN_DIR TRIAL_ID` **重放执行**：在临时工作区重建任务环境，按轨迹顺序重新执行记录下来的 action，并逐步与记录的结果对比（`ok` / `returncode` 始终比较，`stdout` 仅在轨迹未被截断时比较）。输出 `mode: "replay"` 与 `divergences`。`--no-execute` 只列出动作，输出 `mode: "list"`，不会被标成 replay。
- `benchmark regrade RUN_DIR TRIAL_ID --command …` **重评分**：不重放动作，而是把 artifact 快照还原成工作区，用给定的 verifier 重新打分，用于修好评分脚本后复评。`--command` 之后的全部参数原样作为 verifier argv（`-lc` / `-c` 不会被当成选项），因此必须放在整行最后，例如 `--command bash -lc 'test -f marker.txt'`。输出 `mode: "artifact_snapshot"`；artifact 快照缺失时显式报错，而不是对空工作区打分。

两者都不会重新调用原始 agent。

## 工程化与可观测性

- 质量门禁：`make check` 依次执行 `ruff check`、`mypy`、`pytest`、`benchmark oracle`（任务集自洽性）；CI（`.github/workflows/ci.yml`）在 Python 3.11 / 3.12 上跑同一组检查，并用 `uv lock --check` 确认锁文件未漂移。
- 产物版本号：`manifest.json` 的 `schema_version` 为 `"2"`（`violations` / `uncertainties` 由 `list[str]` 变为 `list[Finding]`，与 v1 不兼容）；`report.json` 自带 `schema_version`（当前 `"1"`）；artifact `index.json` 的版本号独立（当前 `"1"`）；SQLite 跨 run 索引为 schema v2。外部消费者应据各自版本号判断兼容性，而不要假定三者同步演进。
- 日志：核心模块使用 `benchmark.*` 命名空间的标准 `logging`，导入库时不会配置 root logger；CLI 用 `--log-level` 与 `--log-json` 打开。日志只记录 id、计数和异常类型，不写入 prompt、stdout 或凭证。
- 跨 run 索引：`benchmark index RUN_DIR… --db reports/index.sqlite3` 把若干 run 目录的 run/trial 摘要写入 SQLite；`benchmark compare [RUN_ID…]` 读取该库做横向对比（默认附按 task 的成功率矩阵，`--no-tasks` 关闭）。索引幂等，同一 `run_id` 重复写入只更新不重复。
- 体积上限：`ArtifactStore` 默认忽略 `.git` / `__pycache__` / `node_modules` 等路径，并受 `RunConfig.artifact_max_files` / `artifact_max_file_bytes` / `artifact_max_total_bytes` 约束，被跳过的文件记入 `index.json` 的 `skipped_counts`。轨迹 JSONL 的每个字符串受 `RunConfig.trace_max_chars` 截断，截断处保留长度标记，避免单条命令写出无上限的 trace。

## 可复现性

`seed` 会真正流通：orchestrator 按 `task.seed + run_index` 派生 per-trial seed，写入 `TaskSpec` 后传给 environment 与 agent，并记录在 `TrialResult.seed`。环境把它注入命令的 `BENCHMARK_SEED` / `PYTHONHASHSEED`，LLM adapter 透传给 backend。artifact 的 sha256 索引持久化为 `index.json`，`TrialResult.termination_reason` 记录终止原因（`agent_finished` / `environment_terminal` / `timeout` / `max_steps` / `runner_error`）。

`manifest.json` 头部记录 run 级指纹：`harness_version`、`git_commit`（不在 git 仓库时为 `null`）、`providers`（按配置去重，已剥离 URL 内嵌凭证、只保留 `api_key_env` 名称）、`dependencies` 与完整 `run_config`，用于判断两次 run 是否可比。`provider` 名称（而非仅 `model`）决定实际调用的 backend；未知名称会在构建 agent 时报错，而不是静默回退到 OpenAI 兼容。

命令超时同时受两层约束：task 可用 `environment.command_timeout` 设置单条命令上限（默认 30s），runner 每步把 trial 剩余预算传下去，实际超时取两者的较小值。因此单条长命令无法跑完整轮预算，超时以 returncode 124 作为工具结果返回给 agent。`failure_category` 在 agent 出错或超时时以 agent 状态为准，避免被 grader 的 `test_failure` 掩盖而漏掉重试。

## 架构边界

```text
TaskSpec → Runner → AgentAdapter ↔ EnvironmentAdapter → Independent Grader
                         ↓
                    TraceRecorder
```

- `benchmark.schemas`：任务、事件、Agent 结果、评分和试验结果
- `benchmark.tasks`：任务目录加载与发现
- `benchmark.runner`：单 trial 生命周期、预算和清理
- `benchmark.orchestrator`：批量运行与报告
- `adapters/agents`：Agent 接入层
- `adapters/environments`：环境接入层
- `adapters/graders`：独立评分层

核心代码不依赖具体模型 provider。真实 LLM、Inspect AI 和 Harbor 应通过 adapter 接入，而不是写入任务逻辑。

## 安全边界

**Docker 是默认环境，`local` 是显式 opt-in，且 `LocalEnvironment` 仍然不是安全沙箱。** `DockerEnvironment` 默认非 root（`10001:10001`）、`--network none`、`--cap-drop ALL`、`--security-opt no-new-privileges`，并带 CPU / 内存 / PID 限制；verifier 在只读挂载（`:ro`）的独立容器里运行。把 `environment_type` 从默认的 `"docker"` 改成 `"local"` 只是显式选择了「不具备隔离」的确定性 smoke 路径，并不等于获得沙箱。

`LocalEnvironment` 不再继承宿主的全部环境变量：默认只透传一组白名单（`PATH`、`HOME`、`LANG` 等），任务需要的额外变量必须通过 `environment.env` 显式声明，因此 `OPENAI_API_KEY` / `COMATE_API_KEY` 等凭证不会被 agent 或任务直接读走。接入真实 Agent 或不可信任务前仍必须使用独立容器/VM，并默认：非 root、无网络、无 Docker socket、资源限制、独立临时卷、独立 verifier 和完整 manifest。

## Known limitations（已知限制）

以下限制是当前实现的真实边界，逐条记录在案，不做能力上的过度声明：

- **Docker 集成测试在本地可跳过，在 CI 必须执行成功。** 默认 `uv run pytest` 在 daemon 或镜像不可用时跳过真实容器测试；安全参数单元测试不需要 Docker。`pytest -m "not docker_integration"` 完全不探测 Docker。CI 的独立 `Docker integration (3.11)` / `Docker integration (3.12)` job 显式检查 daemon、拉取并检查基础镜像，再用 `--require-docker` 运行真实容器测试：skip、xfail、未选中集成测试或仅收集不执行均不能通过。测试报告以 JUnit artifact 保留。建议将这两个 job 加入分支保护的 required status checks；工作流本身不能设置分支保护。

  本地复现 CI（默认镜像 `python:3.12-slim`，可替换为包含 Python、sh 和 sleep 的本地/内网镜像）：

  ```bash
  uv sync --locked --extra dev --python 3.12
  export BENCHMARK_TEST_IMAGE=python:3.12-slim
  docker version
  docker info
  docker pull "$BENCHMARK_TEST_IMAGE"
  docker image inspect "$BENCHMARK_TEST_IMAGE"
  uv run pytest tests/test_docker_environment.py -m docker_integration --require-docker -v -ra
  ```

  集成测试检查运行中的安全参数、命令成功/失败返回码、Agent 可写与 Verifier 只读的差异，以及两种容器的清理。测试专用目录允许容器非 root 用户访问，marker 文件允许写入，以免把普通文件权限拒绝误当作只读挂载生效。
- **`agent: cli` 要求 `environment_type: local`。** CLI 适配器在宿主机上直接启动进程；容器环境报告的是容器内路径（如 `/workspace`），宿主上并不存在。`build_agent` 在构建期拒绝 `agent: cli` 与非 `local` 的组合，容器内委托执行尚未实现。
- **`agent: cli` 绕过 `LocalEnvironment` 的凭证白名单。** `CLIAgent` 用 `{**os.environ, **self.env}` 启动 CLI，因此保护 agent 派生任务命令的 env allowlist 不适用于这条路径：CLI（以及它派生的任何进程）能看到宿主上的全部环境变量，包括 `OPENAI_API_KEY` / `COMATE_API_KEY`。这是「在宿主机上运行真实编码 CLI」的固有代价，唯一真正的缓解手段是容器/沙箱或凭证代理；本限制只在选用 `agent: cli` 时成立。
- **`claude` / `codex` profiles 未经真实 CLI 验证。** 开发主机未安装任何编码 CLI，网络出口也被阻断，`profiles.py` 中的预设只编码了文档记载的非交互式调用方式，尚未对真实命令行运行过；使用前请按本机安装的 CLI 版本确认。
- **`LocalEnvironment` 仍然不是安全沙箱。** 把它降级为显式 opt-in 只是减少误用，并不等于安全。staging 会把 `tasks/<name>/workspace/` 拷进 trial 工作区；符号链接不会被跟随（也不会被复制），因此 fixture 无法越过自己的目录树。
- **`makefile_target` 的 verifier 按文本检查 recipe 声明的输入。** 它在 Makefile 的 tab 缩进行里要求出现 `$(INPUT)` 或字面量 `input.txt`；因此惯用的 `sort $< > $(OUTPUT)` 会被拒绝，必须写成 instruction 中给出的 `$(INPUT)` / `input.txt` 形式。
- **参考解访问检测基于命令文本，可被混淆绕过。** 4 个任务已通过 composite grader 启用 `PolicyGrader`：改写/删除 `oracle.sh`、把它复制进工作区、以及按绝对路径引用 task 目录或 `oracle.sh` 都会 fail-closed。但检测是确定性字符串匹配，agent 若用变量拼接、编码或 `find` 动态定位参考解路径仍可能绕过这一层；只能真正缓解的手段是独立 verifier 沙箱（只把声明的 artifact 传给它），尚未实现。

## 后续路线

1. Docker 路径已具备单元测试（注入 fake runner 固定安全参数）与一个本地可跳过、CI 强制执行的集成测试；`LLMAdapter` 注入式 backend 由 `tests/test_llm_adapter.py` 覆盖。
2. `GraderResult.violations` / `uncertainties` 由 `PolicyGrader`（确定性）与可选的 `JudgeGrader`（LLM）产出：行为违规默认 fail-closed（`failure_category="policy_violation"`，不重试），不确定性只标注置信度、不改变判分。
3. Harbor、BrowserGym、OSWorld、MCP 和固定拓扑 multi-agent 仍是诚实 scaffold：`adapters.errors.unavailable` 统一了「缺可选依赖」与「依赖已装但未接线」两种拒绝，`agent: mcp` / `environment_type: mcp` / `agent: multi_agent` 路由已对称；真实 SDK 桥接待实现。

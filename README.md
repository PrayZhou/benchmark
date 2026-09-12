# Agent Benchmark Harness

一个面向工具型 Agent 的可插拔评测骨架。当前 MVP 使用本地确定性环境验证完整链路，后续可接入 Docker、Inspect AI、Harbor、BrowserGym、OSWorld、MCP 和多 Agent 运行时。

## 当前能力

- 版本化任务 schema（Pydantic）
- `tasks/<name>/` 任务目录为单一真相来源
- Agent / Environment / Grader 解耦
- scripted agent baseline
- `provider` 名称真正选择 LLM backend（`openai` / `openai-compatible`，未配置时回退默认；未知名称在构建期报错，不再静默走 OpenAI 兼容）
- `RunConfig.dataset` 作为 provenance 写入 `report.json` 与 SQLite 索引，并可用 `benchmark compare --dataset` 过滤
- observation/action/tool-result JSONL trajectory
- `replay` 真正重放已记录的动作并与轨迹结果对比；`regrade` 基于 artifact 快照重评分（二者语义分离，见下）
- command grader（支持部分分）与 state grader；composite grader 支持权重与阈值
- trial 结果和汇总报告（按 trial 分组，重试不稀释成功率）
- 重试默认覆盖模型/网关错误（`runner_error` / `environment_error` / `timeout` / `error`）
- run manifest 与带 sha256 的 artifact 索引
- run 级指纹：`harness_version`、git commit、provider 配置摘要、依赖版本与 run config
- artifact 落盘上限（单文件 / 总量 / 数量）与 ignore 规则，轨迹 JSONL 按字符截断
- 结构化日志（`--log-level` / `--log-json`），核心链路带 `run_id` / `trial_id` 上下文
- SQLite 跨 run 索引与横向对比（`benchmark index` / `benchmark compare`）
- CI（ruff lint + mypy 类型检查 + pytest）与 `uv.lock` 依赖锁
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

当前示例使用 `LocalEnvironment` 以便在没有 Docker 的机器上先验证 harness。`tasks/hello_task/Dockerfile` 是后续 Docker adapter 的环境基线；它不会被 LocalEnvironment 自动使用，仅记录在 `metadata.dockerfile`。

## 任务目录

一个任务由一个目录定义，各文件各司其职，不重复：

```text
tasks/hello_task/
  instruction.md    # Agent 指令（唯一来源）
  tests/test.sh     # verifier（唯一来源，经 bash -c 执行）
  task.yaml         # 其余配置：id、seed、limits、adapter 选择、scripted actions
  Dockerfile        # 可选环境基线
```

在 `task.yaml` 里重复定义 `instruction` 或 `grader.command` 会被显式拒绝，避免两份副本各自漂移。

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

## 重放与重评分

二者都读取已有 run 目录，但语义不同，不可混用：

- `benchmark replay RUN_DIR TRIAL_ID` **重放执行**：在临时工作区重建任务环境，按轨迹顺序重新执行记录下来的 action，并逐步与记录的结果对比（`ok` / `returncode` 始终比较，`stdout` 仅在轨迹未被截断时比较）。输出 `mode: "replay"` 与 `divergences`。`--no-execute` 只列出动作，输出 `mode: "list"`，不会被标成 replay。
- `benchmark regrade RUN_DIR TRIAL_ID --command …` **重评分**：不重放动作，而是把 artifact 快照还原成工作区，用给定的 verifier 重新打分，用于修好评分脚本后复评。`--command` 之后的全部参数原样作为 verifier argv（`-lc` / `-c` 不会被当成选项），因此必须放在整行最后，例如 `--command bash -lc 'test -f marker.txt'`。输出 `mode: "artifact_snapshot"`；artifact 快照缺失时显式报错，而不是对空工作区打分。

两者都不会重新调用原始 agent。

## 工程化与可观测性

- 质量门禁：`make check` 依次执行 `ruff check`、`mypy`、`pytest`；CI（`.github/workflows/ci.yml`）在 Python 3.11 / 3.12 上跑同一组检查，并用 `uv lock --check` 确认锁文件未漂移。
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

当前 LocalEnvironment 只适合确定性 smoke test，不是安全沙箱。它不再继承宿主的全部环境变量：默认只透传一组白名单（`PATH`、`HOME`、`LANG` 等），任务需要的额外变量必须通过 `environment.env` 显式声明，因此 `OPENAI_API_KEY` / `COMATE_API_KEY` 等凭证不会被 agent 或任务直接读走。接入真实 Agent 或不可信任务前仍必须使用独立容器/VM，并默认：非 root、无网络、无 Docker socket、资源限制、独立临时卷、独立 verifier 和完整 manifest。

## 后续路线

1. `DockerEnvironment` 尚无测试；注入式 backend 已就绪，`LLMAdapter` 已由 `tests/test_llm_adapter.py` 覆盖。
2. `GraderResult.violations` / `uncertainties` 目前没有任何 grader 产出，安全违规上报仍是空壳。
3. Harbor、BrowserGym、OSWorld、MCP 和固定拓扑 multi-agent 仍是诚实 scaffold：`adapters.errors.unavailable` 统一了「缺可选依赖」与「依赖已装但未接线」两种拒绝，`agent: mcp` / `environment_type: mcp` / `agent: multi_agent` 路由已对称；真实 SDK 桥接待实现。

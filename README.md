# Agentic-RL Sandbox Benchmark Suite

针对 AWS EC2 c7i (Intel SPR) vs c8g (Graviton4) 在 Agentic-RL 沙盒典型负载下的容器化 Benchmark。

跑测目标是真实 RL rollout 链路上**每秒能完成多少 episode**、**P99 长尾**、**每千次 rollout 成本**，而不是单跑微基准。

---

## 子项一览

| ID  | 名称                | 现状 | Workload (实测)                                                       | 容器拓扑                                  |
| --- | ------------------- | ---- | --------------------------------------------------------------------- | ----------------------------------------- |
| B1  | CodeExec            | ✅   | HumanEval 164 + MBPP-sanitized 427 = **591 个 Python snippet**，每条 fork 新 `python3 -c` 子进程跑 | orchestrator + b1-codeexec-worker (FastAPI) |
| B2  | SWE-Build           | ⏸️   | 占位，需要预构建 SWE-bench Lite 仓库镜像                                | -                                         |
| B3  | ToolCall            | ✅   | τ-bench-style mock e-commerce API，**7 个模板 → 200 条 trajectory**（avg 11.7 步） | b3-mock-api (FastAPI + SQLite) + 内置 client |
| B4  | Browser             | ✅   | Playwright + Chromium headless，**8 个模板 → 80 条 trajectory** 跑在自托管的真实 SPA (~25KB JS) | b4-webarena-static (nginx) + b4-playwright-worker |
| B5  | SQLExec             | ✅   | DuckDB in-process **TPC-H sf=1**，22 条标准 query 时间驱动循环          | b5-sql-runner (FastAPI + DuckDB)          |
| B6  | DataSci             | ⏸️   | 占位                                                                  | -                                         |
| B7  | Sim-TextGame        | ✅   | ALFWorld/TextWorld-style minigrid（8 房间 + 30 物品 + 6 个 goal 模板），每 episode 30–50 步纯 Python 循环 | b7-textgame (FastAPI + 多 uvicorn worker) |
| B8  | ColdStart           | ✅   | `docker run python:3.11-slim` × N 次，挂钟测沙盒拉起开销                | orchestrator 通过 docker socket 起 container |
| B9  | Concurrent-Rollout  | ✅   | 端到端综合：每个 rollout 是 10–30 步 mini-episode，按 B3:60% / B1:25% / B5:15% 抽 task；并发 64→256→1024，**默认 30 分钟稳态** | 复用 b1-codeexec + b3-mock-api + b5-sql-runner |

详细每个子项的 trajectory / corpus / 数据集来源见各 runner 模块顶部 docstring：

- `orchestrator/runners/b1_codeexec.py` + `b1_corpus.py`
- `orchestrator/runners/b3_toolcall.py` + `b3_trajectories.py`
- `orchestrator/runners/b4_browser.py`   + `b4_trajectories.py`
- `orchestrator/runners/b5_sqlexec.py`
- `orchestrator/runners/b7_textgame.py`
- `orchestrator/runners/b8_coldstart.py`
- `orchestrator/runners/b9_concurrent.py`

---

## 信号侧重

| 子项 | 主要捕捉的微架构差异                                    | 直觉上谁更占优                  |
| ---- | ------------------------------------------------------- | ------------------------------- |
| B1   | CPython 解释器 cold-start，单线程标量、分支密集           | c7i (高 boost 频率 + 强 OoO)     |
| B3   | FastAPI/uvicorn + JSON + SQLite 高并发 OLTP             | c8g (多核高吞吐、单价低)         |
| B4   | V8 JIT + Skia layout/paint + Chromium 进程模型           | 趋平 (V8 在两架构都成熟)         |
| B5   | DuckDB **列式 + SIMD 矢量化** (NEON/SVE vs AVX2/AVX-512) | c8g (内存带宽 + SVE)             |
| B7   | Python 状态机 + 字符串处理 + GIL contention (多 worker 进程) | c8g (核多吞吐高)              |
| B8   | 内核 syscall + container/cgroup 启动                     | c8g (新内核更顺)                 |
| B9   | 端到端长尾放大：rollout 内最慢 step 决定整体 latency       | 视 task mix 而定，通常 c8g (并发深) |

---

## 目录结构

```
agentic-rl-bench/
├── docker-compose.yml             # 服务编排，按 profile 拉起
├── .env.example                   # 配置模板（注册表、S3、TPC-H sf 等）
├── scripts/
│   ├── ensure-docker.sh           # 自动装 docker / buildx / compose v2
│   ├── fetch-datasets.sh          # build 前拉 HumanEval + MBPP
│   ├── build-all.sh               # 当前架构 native build + push ECR
│   ├── run-single.sh              # 跑单子项 (B1|B3|B4|B5|B8)
│   ├── run-suite.sh               # 跑全套
│   └── _lib.sh                    # ECR login / compose pull 共用函数
├── orchestrator/                  # 主控（Python async）
│   ├── main.py                    # 入口
│   ├── config.py                  # 环境变量装载
│   ├── metrics.py                 # latency sink + resource sampler
│   ├── s3_uploader.py
│   ├── instance_meta.py           # IMDSv2 自识别 c7i / c8g
│   ├── datasets/                  # build 时由 fetch-datasets.sh 填充
│   └── runners/                   # 各子项 runner
│       ├── base.py                # 通用 drive_load / 成本计算
│       ├── b1_codeexec.py + b1_corpus.py
│       ├── b3_toolcall.py + b3_trajectories.py
│       ├── b4_browser.py   + b4_trajectories.py
│       ├── b5_sqlexec.py
│       └── b8_coldstart.py
└── workers/
    ├── b1-codeexec/               # FastAPI subprocess sandbox
    ├── b3-mock-api/               # FastAPI + SQLite 仿 τ-bench
    ├── b4-playwright/             # Playwright headless Chromium
    ├── b4-webarena-static/        # nginx serving SPA (~25KB JS)
    ├── b5-sql-runner/             # FastAPI + DuckDB + TPC-H
    └── b7-textgame/                # FastAPI + minigrid text simulator
```

---

## 快速开始

### 0. 前置

- 需要一台 build 主机（**当前架构 native build**，amd64 / arm64 各一台分别 push）
- 需要 c7i.* 和 c8g.* 各一台 run 主机
- 三台机器都需要 IAM instance profile 含：
  - build 主机：`ecr:GetAuthorizationToken`、`ecr:DescribeRepositories`、`ecr:CreateRepository`、`ecr:Put*` 系列（或直接挂 `AmazonEC2ContainerRegistryPowerUser`）
  - run 主机：`AmazonEC2ContainerRegistryReadOnly` 即够，要上传结果再加 `s3:PutObject`

### 1. 配置

```bash
cp .env.example .env
# 编辑 .env: 填入 ECR 仓库、S3 桶、AWS region、可选 TPCH_SF 等
```

### 2. Build（在 build 主机上，amd64 + arm64 各跑一次）

```bash
bash scripts/build-all.sh
```

行为：
1. `ensure-docker.sh` 自动装 docker / buildx / docker-compose-plugin
2. `fetch-datasets.sh` 拉 HumanEval + MBPP-sanitized 到 `orchestrator/datasets/`
3. 自动建 ECR repository（如不存在）
4. **当前架构 native build** 6 个镜像并 push 到 `:v1-${arch}` (e.g. `v1-amd64`)
5. 用 `docker buildx imagetools create` 把已有的 `v1-amd64` / `v1-arm64` 合成 `:v1` 这个 multi-arch manifest

run 主机上 `docker pull image:v1` 会按本地架构自动选 manifest。

### 3. 在 c7i 或 c8g 上跑

跑单个子项（迭代调试用）：

```bash
bash scripts/run-single.sh B1   # 或 B3 / B4 / B5 / B8
```

跑全套：

```bash
bash scripts/run-suite.sh
```

orchestrator 会：
1. 通过 IMDSv2 自动识别实例类型 + 架构
2. 依次 warmup → 多并发档位扫描（`CONCURRENCIES=1,8,32,128`）→ cooldown
3. 输出 `<workload>_result_<instance>_<ts>/c0001.json` ... 到本地 `./results/`
4. 上传到 S3：`s3://${S3_BUCKET}/agentic-rl-bench/<arch>/...`
5. 生成最终汇总 `summary_*.json` + minimal HTML 报告

---

## 结果产物

每个 (子项, 并发档位) 出一个 JSON：

```json
{
  "benchmark": "B5",
  "instance": {"instance_type": "c8g.4xlarge", "arch": "aarch64", ...},
  "concurrency": 32,
  "duration_sec": 300,
  "throughput": {"queries_per_sec": 91.4, "total_queries": 27420},
  "latency_ms": {
    "trajectory": {"p50": 320, "p95": 980, "p99": 1840},
    "per_query": {
      "Q01": {"count": 1247, "p50": 280, "p99": 410},
      "Q06": {"count": 1242, "p50": 95,  "p99": 180},
      ...
    }
  },
  "resource": {"cpu_util_avg": 0.94, "mem_peak_gb": 8.7, ...},
  "cost":     {"instance_hourly_usd": 0.6381, "cost_per_1k_units_usd": 0.060},
  "extra":    {"engine": "duckdb", "tpch_sf": 1.0, "duckdb_version": "1.1.3", ...}
}
```

每个子项的 `extra` 会把数据集大小、模板分布、随机种子等都打进去，保证可复现。

---

## 命名约定

- 结果目录：`<workload>_result_<instance-type>_<YYYYMMDD-HHMMSS>`
- S3 路径：`s3://${S3_BUCKET}/agentic-rl-bench/<arch>/<workload>_result_*/`

`<workload>` 是 runner 自己声明的（`codeexec` / `toolcall` / `browser` / `sqlexec` / `coldstart`）。

---

## 关键配置开关 (`.env`)

| 变量              | 作用                                                 | 默认       |
| ----------------- | ---------------------------------------------------- | ---------- |
| `REGISTRY`        | ECR 仓库地址                                          | (必填)     |
| `IMAGE_TAG`       | 镜像 tag                                              | `v1`       |
| `DURATION_SEC`    | 每个并发档位的压测持续时间                             | 300        |
| `CONCURRENCIES`   | 并发档位列表（CSV）                                    | `1,8,32,128` |
| `WARMUP_SEC`      | warmup 时长                                           | 30         |
| `COOLDOWN_SEC`    | 档位间 cooldown                                       | 60         |
| `TPCH_SF`         | B5 DuckDB dbgen 的 scale factor                       | `1.0`      |
| `DUCKDB_THREADS`  | B5 DuckDB worker 内部线程，0=auto                      | `0`        |
| `B7_UVICORN_WORKERS` | B7 uvicorn worker 进程数（>= vCPU 数饱和容器）        | `8`        |
| `B8_TRIALS`       | B8 cold-start 次数                                    | 1000       |
| `B9_DURATION_SEC` | B9 每个并发档位持续时间（秒），默认 30 分钟              | 1800       |
| `B9_CONCURRENCIES`| B9 并发档位（CSV）                                     | `64,256,1024` |
| `SKIP`            | 跳过的子项 ID（CSV，e.g. `B2,B6`）                     | -          |
| `S3_BUCKET`       | 结果上传桶                                            | -          |
| `PRICE_C7I_4XL`   | c7i.4xlarge 每小时美元（用于 cost-per-1k 换算）         | 0.7140     |
| `PRICE_C8G_4XL`   | c8g.4xlarge 每小时美元                                | 0.6381     |

---

## 参考

各子项实现参考的开源数据集 / benchmark：

- **B1**: [openai/human-eval](https://github.com/openai/human-eval) (MIT) + [google-research/mbpp](https://github.com/google-research/google-research/tree/master/mbpp) (Apache-2.0)，sanitized 子集
- **B3**: trajectory 模板 inspired by [sierra-research/tau-bench](https://github.com/sierra-research/tau-bench) retail 任务
- **B4**: trajectory 模板 inspired by [WebArena](https://github.com/web-arena-x/webarena) shopping 任务，自托管 SPA
- **B5**: DuckDB 官方 [`tpch` extension](https://duckdb.org/docs/extensions/tpch.html) (Apache-2.0)，TPC-H 标准 22 查询

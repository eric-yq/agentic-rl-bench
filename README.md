# Agentic-RL Sandbox Benchmark Suite

针对 AWS EC2 c7i (Intel SPR) vs c8g (Graviton4) 在 Agentic-RL 沙盒典型负载下的容器化 Benchmark。

## 子项

| ID  | 名称                | 容器拓扑                          |
| --- | ------------------- | --------------------------------- |
| B1  | CodeExec            | orchestrator + python-worker pool |
| B2  | SWE-Build           | (占位，需要预构建仓库镜像)        |
| B3  | ToolCall            | mock-api + httpx client (内置)    |
| B4  | Browser             | webarena-static + playwright pool |
| B5  | SQLExec             | postgres + sql-runner             |
| B6  | DataSci             | (占位)                            |
| B7  | Sim-TextGame        | (占位)                            |
| B8  | ColdStart           | orchestrator 直接 docker run      |
| B9  | Concurrent-Rollout  | 复用 B1+B3+B5 的 worker           |

## 目录

```
agentic-rl-bench/
├── docker-compose.yml         # 所有服务容器编排
├── .env.example               # 配置模板
├── scripts/                   # 构建/运行辅助脚本
├── orchestrator/              # 主控程序（Python，async）
│   ├── main.py                # 入口
│   ├── config.py
│   ├── metrics.py             # 指标聚合
│   ├── s3_uploader.py
│   └── runners/               # 各子项 runner
└── workers/                   # 各子项 worker 容器
    ├── b1-codeexec/
    ├── b3-mock-api/
    ├── b4-playwright/
    ├── b4-webarena-static/
    └── b5-sql-runner/
```

## 快速开始

### 1. 配置

```bash
cp .env.example .env
# 编辑 .env: 填入 ECR 仓库、S3 桶、AWS region
```

### 2. 构建 multi-arch 镜像（在带 buildx 的机器上）

```bash
bash scripts/build-all.sh
```

### 3. 在 c7i 或 c8g 实例上拉起 + 跑全套

```bash
bash scripts/run-suite.sh
```

orchestrator 会：
1. 自动识别实例类型（IMDSv2）和架构
2. 依次拉起每个子项的容器组、warmup、跑测、清理
3. 多并发档位扫描
4. 输出 JSON 结果到本地 `results/` + 上传到 S3
5. 生成最终汇总报告

## 命名约定（遵循 workspace 规范）

- 结果目录：`<workload>_result_<instance-type>_<YYYYMMDD-HHMMSS>`
- S3 路径：`s3://${S3_BUCKET}/agentic-rl-bench/<arch>/<workload>_result_*/`

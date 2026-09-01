# Evolve Workspace 结构草图

`evolve init` 以「recipe + seed + dataset」为输入，生成一个自包含的 workspace。
它本身是一个独立的 Git 仓库：每个候选代（generation）是一次 commit，并打上
`gen/<id>` 标签；`archive.jsonl` 记录 append-only 的谱系。

```mermaid
flowchart TB
    init["evolve init\n(recipe + seed + dataset)"] -->|生成| WS

    subgraph WS["Workspace &nbsp;·&nbsp; 独立 Git 仓库（每代 commit 打 gen/&lt;id&gt; tag）"]
        direction TB

        subgraph MECH["🔒 机制层（framework-owned，不参与进化）"]
            console["./evolve 控制台"]
            vendored[".evolve/\nvendored 框架运行时 + launchers"]
            lockfiles["pyproject.toml · uv.lock · .python-version\n锁定的运行环境"]
            console --> vendored
        end

        subgraph CONFIG["📋 配置（init 时由 recipe 渲染，之后只读）"]
            yaml["evolve.yaml\n渲染后的 recipe 配置（含 surface 规则）"]
            manifest[".evolve-components.json\nrecipe / seed / engine / integration 清单"]
            docs["PROTOCOL.md · program.md · AGENTS.md"]
        end

        subgraph OPS["⚙️ 算子（recipe 选定）"]
            operators["operators/\nselect · rollout · analyze · mutate\nvalidate · gate · record · preflight"]
            library["library/\n闭源根 + 各阶段辅助脚本包"]
            skills["skills/evolve-agent/\n方法指南与操作手册"]
        end

        subgraph EVAL["🔒 冻结评估器 evaluator/"]
            evalsh["eval.sh + engines/"]
            envs["eval.env · agent.env · verifier.env\nenvironment.kwargs"]
            pins["splits.json · dataset.pin\nruntime.pin / runtime.json\n（绑定数据与运行时，改动即失效）"]
        end

        subgraph TARGET["🧬 进化对象 target/"]
            seed["seed 的 vendored 副本（+ UPSTREAM.json）\n仅 surface 允许的路径可被修改"]
        end

        subgraph STATE["📈 谱系与状态"]
            archive["archive.jsonl\nappend-only 谱系记录"]
            best["best_ever.json"]
            runs["runs/\n每代运行状态"]
            artifacts["artifacts/\nuser/ · generations/"]
        end
    end

    console -.->|驱动循环| OPS
    OPS -.->|"mutate（仅 surface 内）"| TARGET
    TARGET -.->|候选快照| EVAL
    EVAL -.->|score / verdict| STATE
```

## 一轮循环（简化）

```mermaid
flowchart LR
    S[select] --> M[mutate target/] --> E[evaluator 评分] --> G[gate] --> R["record\n(archive.jsonl + git tag gen/&lt;id&gt;)"] --> S
```

## 目录速览

```text
workspace/
├── evolve                    # 控制台（唯一入口）
├── .evolve/                  # 🔒 vendored 框架运行时
├── evolve.yaml               # 渲染后的 recipe 配置
├── .evolve-components.json   # 组件清单
├── operators/                # recipe 选定的算子脚本
├── library/                  # 算子辅助脚本包
├── skills/evolve-agent/      # 操作手册
├── evaluator/                # 🔒 冻结评估器（eval.sh、splits.json、*.pin …）
├── target/                   # 🧬 进化对象（seed 副本，surface 内可改）
├── archive.jsonl             # append-only 谱系
├── best_ever.json
├── runs/                     # 每代运行状态
└── artifacts/                # user/ 与 generations/ 持久上下文
```

要点：只有 `target/` 中 surface 允许的路径参与进化；`.evolve/` 与
`evaluator/` 受保护，谱系字段由机制盖章，不作为候选输入。

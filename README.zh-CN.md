<p align="center">
  <img src="docs/rsihub-lockup.svg" width="460" alt="RSIHub：环形标志与 RSIHub 字标。">
</p>

<p align="center">
  一个基于文件，用于 evaluator 驱动的学习和可复现的 candidate lineage，以及可控的修改范围的演化框架。
</p>

<p align="center">
  <a href="https://github.com/KaiWU5/Awesome-AI4AI">
    <img alt="Awesome AI4AI" src="https://img.shields.io/badge/Awesome-AI4AI-fc60a8?logo=awesomelists&amp;logoColor=white">
  </a>
  <a href="https://github.com/KaiWU5/Awesome-AI4AI/blob/main/assets/AI4AI-Survey.pdf">
    <img alt="AI4AI Survey PDF" src="https://img.shields.io/badge/Survey-AI4AI%20(PDF)-b31b1b?logo=adobeacrobatreader&amp;logoColor=white">
  </a>
  <a href="https://simpleagentlab.com/ai4ai/">
    <img alt="AI4AI Blog" src="https://img.shields.io/badge/Blog-AI4AI-6f42c1?logo=rss&amp;logoColor=white">
  </a>
  <a href="LICENSE">
    <img alt="Apache-2.0 License" src="https://img.shields.io/badge/License-Apache--2.0-0095fd?logo=opensourceinitiative&amp;logoColor=white">
  </a>
  <a href="https://github.com/simple-agent-lab/RSIHub/actions/workflows/test.yml">
    <img alt="RSIHub tests" src="https://github.com/simple-agent-lab/RSIHub/actions/workflows/test.yml/badge.svg">
  </a>
  <a href="https://www.python.org/">
    <img alt="Python 3.12+" src="https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&amp;logoColor=white">
  </a>
  <a href="https://simpleagentlab.com/RSIHub/">
    <img alt="RSIHub documentation" src="https://img.shields.io/badge/Documentation-RSIHub-0F766E?logo=materialformkdocs&amp;logoColor=white">
  </a>
</p>

<p align="center">
  <a href="https://simpleagentlab.com/rsihub/">RSIHub 官网</a> ·
  <a href="#rsihub-是什么">RSIHub 是什么</a> ·
  <a href="#工作原理">工作原理</a> ·
  <a href="#哪些部分可以演化">哪些部分可以演化</a> ·
  <a href="#recipes">Recipes</a> ·
  <a href="#skill-演化案例">案例展示</a> ·
  <a href="#快速开始">快速开始</a> ·
  <a href="#文档">文档</a>
</p>

<p align="center">
  <a href="README.md">English</a> · <strong>简体中文</strong>
</p>

> [!TIP]
> **第一次接触 AI-for-AI 与 agent 自我改进？** 建议先浏览精选论文列表
> [Awesome-AI4AI](https://github.com/KaiWU5/Awesome-AI4AI)，通读
> [AI4AI 综述（PDF）](https://github.com/KaiWU5/Awesome-AI4AI/blob/main/assets/AI4AI-Survey.pdf)
> 了解全貌，并关注 [AI4AI 博客](https://simpleagentlab.com/ai4ai/)
> 获取 Simple Agent Lab 的持续记录。RSIHub 正是把这些方法放到同一套标准下比较的实验基础设施。

<br>

<p align="center">
  <a href="docs/assets/benchmark-results-rsihub-v2.svg">
    <img src="docs/assets/benchmark-results-rsihub-v2.svg" alt="AHE、Hyperagents、A-Evolve 与 GEPA 在 MiniSWE 和 Codex target agent 上的 Terminal-Bench 2 与 Tau³ Banking 结果。每根堆叠柱在深色部分标注 seed 分数，在浅色部分上方标注演化后分数及变化量。">
  </a>
</p>

## RSIHub 是什么

RSIHub 为 agent 提供了一条受控的自我改进路径。它让候选 agent 在固定的 evaluator 下运行，
完整保留每一代的**溯源记录**——改了什么、为什么改——并在 evaluator 本身始终不被触碰的前提下，
把验证过的改进沉淀到 agent 上。

- **对 agent 开发者：** 在一个可复用的实验 workspace 里改进 prompts、skills、harnesses 与 agent code。
- **对研究者：** 在固定的 evaluation 与 mutation 边界下，公平比较不同的演化策略。
- **证据内建：** 每个候选 agent 都可追溯到分数、artifacts、归档记录与 Git lineage。

## 工作原理

所有 recipe 都由同一条主循环组合而成：

<p align="center">
  <strong>选择 select → 执行 rollout → 分析 analyze → 变异 mutate → 护栏 guardrail → 评测 evaluate → 吸收 absorb</strong>
</p>

<p align="center">
  <a href="docs/assets/architecture.svg">
    <img src="docs/assets/architecture.svg" alt="RSIHub 架构图。左侧为 evolution driver：recipe 驱动的 evolve.yaml 声明 target seed、可修改的 surface、每个阶段的 operator、evaluator，以及训练/门控/封存划分，并写入包含父结点 commit、评测记录与 artifacts 的可验证归档。中间为 evolution loop：围绕候选 agent 依次进行 select 挑选父结点、rollout 在训练任务上执行、analyze 提炼证据、mutate 修改候选、guardrail 检查边界、evaluate 打分与认证、absorb 记录并准入。其中 select、rollout、analyze、mutate、absorb 属于 recipe 策略阶段，guardrail 与 evaluate 由框架控制。右侧为 protected runtime：Harbor executor、冻结的 evaluation 与数据划分，以及由 evolve.yaml、operator、target、evaluator、runs 和 archive.jsonl 组成的 workspace 结构。">
  </a>
</p>

recipe 决定：如何挑选父结点、如何分析轨迹、哪些文件允许被编辑、以及什么样的评测结果才能让新一代通过。
框架独占其中两个阶段，让这些决策可被检验；所有试验都在
[Harbor](https://github.com/harbor-framework/harbor) 上执行，由它提供容器化运行、轨迹捕获与逐任务验证。

| 阶段 | 归属 | 做什么 |
| --- | --- | --- |
| `select` | recipe | 从归档中挑选一个合格的父结点，并检出它确切的那次 commit。 |
| `rollout` | recipe | 在训练集上运行父结点，返回执行证据。 |
| `analyze` | recipe | 把 rollout 轨迹提炼为范围受限的 mutation 依据。 |
| `mutate` | recipe | 在声明过的 surface 内提出修改。 |
| `guardrail` | **框架** | 用实际 diff 比对 surface，拒绝越界的修改提案。 |
| `evaluate` | **框架** | 对审查后的候选 agent 做一次干净检出并打分，认证结果。 |
| `absorb` | recipe | 决定是否准入、写入标注、并延续 lineage。 |

理解组合模型只需三个术语：

| 术语 | 含义 |
| --- | --- |
| `stage` 阶段 | 固定的生命周期槽位——即上表中的一行。 |
| `operator` | 某个阶段的一份可复用实现，位于 `library/<stage>/<name>.py`。 |
| `recipe` | 对这些 operator 的免代码选择与配置。 |

evaluate 永远不是可选的 operator——它由框架独占。operator 的编写、校验与组合方式见
[operator 指南](docs/reference/operators.md)。

## 哪些部分可以演化

| Surface | 示例 | 适用场景 |
| --- | --- | --- |
| prompts 与 skills | system prompt、任务 skill、可复用指令 | 策略与行为改进 |
| harness 与 target code | 工具、编排逻辑、agent 实现 | agent 工程 |
| 被选中的 operator | recipe 选定的 analyze 或 mutate 策略 | 受控的协同演化 |

每个 recipe 都会声明自己可修改的路径。evaluator、归档戳记与框架代码本身均在该范围之外。

## 监督实验

`evolve view` 可作为单个 workspace 的只读视图，或用 `--catalog` 作为多个相关 workspace 的统一索引：

```bash
evolve view /path/to/experiment
```

它会展示实验健康度、代际进度、修改内容、标准评测表现，以及分页的试验结果。
当 workspace 保留了 Harbor job 时，点击试验行可在同一服务上打开 Harbor 的完整轨迹、日志、verifier 输出与 artifacts。
该查看器只是本地检查工具，并非权限边界。

catalog、远程（DevBox）隧道、Harbor 深入排查与常见问题见
[实验查看器指南](docs/guides/experiment-viewer.md)。

## Recipes

| 当你想要… | Recipe | 可修改的 surface |
| --- | --- | --- |
| 从当前最优父结点出发改进单个候选 agent | `hill_climb` | target |
| 演化 prompt 与可复用的 agent skill | `aevolve` | prompt 与 target skills |
| 依据 evaluator 反馈打磨 agent harness | `ahe` | target |
| 用 minibatch 验证平衡多个目标 | `gepa` | prompt 与 task skill |
| 让 target 与被选中的演化策略协同演化 | `hyperagents` | target 与被选中的 operator |

各策略的工作流与配置见 [recipe 指南](recipes/README.md)。

## Skill 演化案例

RSIHub 可以把一个 Skill 当作完整的包来改进：指令、参考资料与校验脚本一同演化，
而冻结的 evaluator 保证比较是诚实的。下面这次本地 Paper2Poster 运行中，相同的 Codex 模型与论文 prompt
生成了两版 LoRA 海报。

<table>
  <tr>
    <th width="50%">Gen 0 · 仅 12 行的最小 Skill</th>
    <th width="50%">Gen 2 · 演化出的编排型 Skill</th>
  </tr>
  <tr>
    <td><img src="docs/assets/paper-poster-lora-gen0.png" alt="Gen 0 的 LoRA 研究海报，采用通用的仪表盘式排版"></td>
    <td><img src="docs/assets/paper-poster-lora-gen2.png" alt="Gen 2 的 LoRA 研究海报，采用贴合论文的编排式排版并含低秩矩阵示意图"></td>
  </tr>
  <tr>
    <td>未通过确定性几何 gate：14 个文本元素溢出 SVG viewBox。</td>
    <td>通过了确定性可渲染性与几何 gate；论文还原度仍作为评审的建议性反馈。</td>
  </tr>
</table>

在这组四篇论文的案例中，确定性完成通过率从 Gen 0 的 **1/4** 提升到 Gen 2 的 **4/4**。
这些试验通过 Harbor 的本地环境并发运行，无需 Docker，并完整保留了 agent 轨迹与 evaluator 提供的视觉反馈。
这是一次有代表性的演化运行，而非广泛的基准测试；详见
[结果快照](docs/results/paper-poster-skill-evolution.json)、
[冻结 rubric](evals/skills/make-paper-poster/rubric.json) 与
[最小 seed Skill](evals/skills/make-paper-poster/seed/skills/make-paper-poster/SKILL.md)。

## 快速开始

使用 `./scripts/setup_terminal_bench.sh` 与 `./scripts/run_recipe_demo.sh`，
可在内容固定（content-pinned）的 Terminal-Bench 2.0 公共子集上运行任一受支持的 recipe。
前置条件、凭据配置、可用的 recipe 取值与启动参数覆盖见[快速开始指南](docs/QUICKSTART.md)。

## 基准结果

分数为百分比，格式是 **seed → evolved agent**。训练分数在该 recipe 的训练集上测得；
完整基准分数在整个基准集上测得。括号中的变化量：紫色表示提升，橙色表示持平，红色表示回退。
所有运行均以 GPT-5.4（high 推理档）作为 target model，mutate operator 为由 GPT-5.4（xhigh 推理档）驱动的 Codex。

<table width="100%">
  <thead>
    <tr>
      <th align="center" width="18%">基准<br><img src="docs/assets/benchmark-column-spacer.svg" alt="" width="150" height="1"></th>
      <th align="center" width="11%">Target agent<br><img src="docs/assets/benchmark-column-spacer.svg" alt="" width="110" height="1"></th>
      <th align="center" width="13%">方法<br><img src="docs/assets/benchmark-column-spacer.svg" alt="" width="125" height="1"></th>
      <th align="center" width="29%">训练分数<br><img src="docs/assets/benchmark-column-spacer.svg" alt="" width="280" height="1"></th>
      <th align="center" width="29%">完整基准分数<br><img src="docs/assets/benchmark-column-spacer.svg" alt="" width="280" height="1"></th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td align="center" rowspan="8"><strong>Terminal-Bench 2</strong><br><sub>50 训练 / 19 门控 / 20 封存</sub></td>
      <td align="center" rowspan="4">MiniSWE</td>
      <td align="center"><a href="https://arxiv.org/pdf/2604.25850">AHE</a></td>
      <td align="center">70.0%&nbsp;→&nbsp;74.0%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-4-0.svg" alt="(+4.0)" width="68" height="20"></td>
      <td align="center">56.2%&nbsp;→&nbsp;65.2%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-9-0.svg" alt="(+9.0)" width="68" height="20"></td>
    </tr>
    <tr>
      <td align="center"><a href="https://arxiv.org/abs/2603.19461">Hyperagents</a></td>
      <td align="center">58.0%&nbsp;→&nbsp;68.0%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-10-0.svg" alt="(+10.0)" width="68" height="20"></td>
      <td align="center">56.2%&nbsp;→&nbsp;69.7%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-13-5.svg" alt="(+13.5)" width="68" height="20"></td>
    </tr>
    <tr>
      <td align="center"><a href="https://github.com/A-EVO-Lab/a-evolve">A-Evolve</a></td>
      <td align="center">66.0%&nbsp;→&nbsp;68.0%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-2-0.svg" alt="(+2.0)" width="68" height="20"></td>
      <td align="center">56.2%&nbsp;→&nbsp;69.7%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-13-5.svg" alt="(+13.5)" width="68" height="20"></td>
    </tr>
    <tr>
      <td align="center"><a href="https://arxiv.org/abs/2507.19457">GEPA</a></td>
      <td align="center">58.0%&nbsp;→&nbsp;68.0%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-10-0.svg" alt="(+10.0)" width="68" height="20"></td>
      <td align="center">56.2%&nbsp;→&nbsp;64.0%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-7-8.svg" alt="(+7.8)" width="68" height="20"></td>
    </tr>
    <tr>
      <td align="center" rowspan="4">Codex</td>
      <td align="center"><a href="https://arxiv.org/pdf/2604.25850">AHE</a></td>
      <td align="center">60.0%&nbsp;→&nbsp;74.0%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-14-0.svg" alt="(+14.0)" width="68" height="20"></td>
      <td align="center">69.7%&nbsp;→&nbsp;71.9%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-2-2.svg" alt="(+2.2)" width="68" height="20"></td>
    </tr>
    <tr>
      <td align="center"><a href="https://arxiv.org/abs/2603.19461">Hyperagents</a></td>
      <td align="center">58.0%&nbsp;→&nbsp;72.0%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-14-0.svg" alt="(+14.0)" width="68" height="20"></td>
      <td align="center">69.7%&nbsp;→&nbsp;70.8%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-1-1.svg" alt="(+1.1)" width="68" height="20"></td>
    </tr>
    <tr>
      <td align="center"><a href="https://github.com/A-EVO-Lab/a-evolve">A-Evolve</a></td>
      <td align="center">62.0%&nbsp;→&nbsp;62.0%&nbsp;<img src="docs/assets/benchmark-deltas/no-change-percent-0.svg" alt="(0.0)" width="68" height="20"></td>
      <td align="center">69.7%&nbsp;→&nbsp;70.8%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-1-1.svg" alt="(+1.1)" width="68" height="20"></td>
    </tr>
    <tr>
      <td align="center"><a href="https://arxiv.org/abs/2507.19457">GEPA</a></td>
      <td align="center">68.0%&nbsp;→&nbsp;68.0%&nbsp;<img src="docs/assets/benchmark-deltas/no-change-percent-0.svg" alt="(0.0)" width="68" height="20"></td>
      <td align="center">69.7%&nbsp;→&nbsp;69.7%&nbsp;<img src="docs/assets/benchmark-deltas/no-change-percent-0.svg" alt="(0.0)" width="68" height="20"></td>
    </tr>
    <tr>
      <td align="center" rowspan="8"><strong>Tau³ Banking</strong><br><sub>50 训练 / 20 门控 / 27 封存</sub></td>
      <td align="center" rowspan="4">MiniSWE</td>
      <td align="center"><a href="https://arxiv.org/pdf/2604.25850">AHE</a></td>
      <td align="center">20.0%&nbsp;→&nbsp;34.0%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-14-0.svg" alt="(+14.0)" width="68" height="20"></td>
      <td align="center">27.8%&nbsp;→&nbsp;28.9%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-1-1.svg" alt="(+1.1)" width="68" height="20"></td>
    </tr>
    <tr>
      <td align="center"><a href="https://arxiv.org/abs/2603.19461">Hyperagents</a></td>
      <td align="center">28.0%&nbsp;→&nbsp;40.0%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-12-0.svg" alt="(+12.0)" width="68" height="20"></td>
      <td align="center">27.8%&nbsp;→&nbsp;33.0%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-5-2.svg" alt="(+5.2)" width="68" height="20"></td>
    </tr>
    <tr>
      <td align="center"><a href="https://github.com/A-EVO-Lab/a-evolve">A-Evolve</a></td>
      <td align="center">26.0%&nbsp;→&nbsp;26.0%&nbsp;<img src="docs/assets/benchmark-deltas/no-change-percent-0.svg" alt="(0.0)" width="68" height="20"></td>
      <td align="center">27.8%&nbsp;→&nbsp;27.8%&nbsp;<img src="docs/assets/benchmark-deltas/no-change-percent-0.svg" alt="(0.0)" width="68" height="20"></td>
    </tr>
    <tr>
      <td align="center"><a href="https://arxiv.org/abs/2507.19457">GEPA</a></td>
      <td align="center">22.0%&nbsp;→&nbsp;28.0%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-6-0.svg" alt="(+6.0)" width="68" height="20"></td>
      <td align="center">27.8%&nbsp;→&nbsp;29.9%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-2-1.svg" alt="(+2.1)" width="68" height="20"></td>
    </tr>
    <tr>
      <td align="center" rowspan="4">Codex</td>
      <td align="center"><a href="https://arxiv.org/pdf/2604.25850">AHE</a></td>
      <td align="center">32.0%&nbsp;→&nbsp;36.0%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-4-0.svg" alt="(+4.0)" width="68" height="20"></td>
      <td align="center">24.7%&nbsp;→&nbsp;34.0%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-9-3.svg" alt="(+9.3)" width="68" height="20"></td>
    </tr>
    <tr>
      <td align="center"><a href="https://arxiv.org/abs/2603.19461">Hyperagents</a></td>
      <td align="center">34.0%&nbsp;→&nbsp;36.0%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-2-0.svg" alt="(+2.0)" width="68" height="20"></td>
      <td align="center">24.7%&nbsp;→&nbsp;28.9%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-4-2.svg" alt="(+4.2)" width="68" height="20"></td>
    </tr>
    <tr>
      <td align="center"><a href="https://github.com/A-EVO-Lab/a-evolve">A-Evolve</a></td>
      <td align="center">36.0%&nbsp;→&nbsp;38.0%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-2-0.svg" alt="(+2.0)" width="68" height="20"></td>
      <td align="center">24.7%&nbsp;→&nbsp;28.9%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-4-2.svg" alt="(+4.2)" width="68" height="20"></td>
    </tr>
    <tr>
      <td align="center"><a href="https://arxiv.org/abs/2507.19457">GEPA</a></td>
      <td align="center">28.0%&nbsp;→&nbsp;30.0%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-2-0.svg" alt="(+2.0)" width="68" height="20"></td>
      <td align="center">24.7%&nbsp;→&nbsp;33.0%&nbsp;<img src="docs/assets/benchmark-deltas/gain-percent-8-3.svg" alt="(+8.3)" width="68" height="20"></td>
    </tr>
  </tbody>
</table>

## 可信性来自结构设计

RSIHub 把可演化的策略与评判它的机制分开：

1. **evaluator 是冻结的。** 候选 agent 无法改变打分契约。
2. **mutation 是有界的。** 每个 recipe 都声明哪些 target 路径与 operator 路径允许改动。
3. **evaluation 是标准化的。** 新一代总是基于候选 agent 的干净快照进行评分。
4. **证据是持久的。** 报告从带戳记的 `archive.jsonl` 记录与 Git 代际 tag 重新计算结果。

operator 以子进程方式运行，而不是被导入框架进程。完整的归属模型与不变量见
[设计指南](docs/concepts/design.md)。

## 项目状态

RSIHub 是一个用于研究与受控实验的活跃原型。当前重点是可靠的实验机制、本地优先的工作流，
以及面向不同 agent 演化场景的可组合策略。

## 路线图

- **接入 DeepSeek harness（`dsh`）：** 在 MiniSWE 与 Codex 之外提供又一个受支持的 target agent。
- **更强的轨迹分析工具：** 更多 analyze 阶段的 operator，把保留下来的轨迹转化为可执行的 mutation 反馈。
- **异步演化：** 让 select、rollout 与 mutate 跨代重叠进行，而不必严格同步推进。
- **面向场景的 recipe：** 为更多 agent 演化用例提供有明确主张的 recipe。
- **本地优先的工作流：** 为可信的本地 agent、prompt 与 skill 提供一等的免 Docker 迭代体验。
- **接入更多方法：** 在共享的 evaluator、lineage 与证据契约下纳入更多演化与搜索方法。

## 面向 AI Agent

用 coding agent 开发 RSIHub 或基于它工作？从这里开始：

- [`llms.txt`](https://simpleagentlab.com/RSIHub/llms.txt) —— 遵循
  [llms.txt 约定](https://llmstxt.org/)的机器友好文档索引，附带原始 Markdown 源文件链接。
- [`AGENTS.md`](AGENTS.md) —— 面向 coding agent 的仓库须知，包含分层测试策略
  （不要在每次编辑后运行全量测试套件）。
- [术语表](docs/reference/terminology.md) —— 定义框架、recipe 与 workspace 通用语言的词汇表。
- [设计](docs/concepts/design.md) 与[架构图](docs/ARCHITECTURE.md) ——
  进行非平凡改动前的必读内容；接口以 `src/evolve/frozen/interfaces.py` 中的 operator 契约为准。

## 文档

| 文档 | 用途 |
| --- | --- |
| [文档站点](https://simpleagentlab.com/RSIHub/) | 安装、运行、概念、指南与参考。 |
| [快速开始](docs/QUICKSTART.md) | recipe 启动器的安装与配置。 |
| [设计](docs/concepts/design.md) | 系统模型、归属边界与不变量。 |
| [架构](docs/ARCHITECTURE.md) | 强制执行的源码模块图与行数预算。 |
| [Recipes](recipes/README.md) | 受支持的演化策略。 |
| [Operators](docs/reference/operators.md) | operator 的编写、校验与组合。 |
| [实验查看器](docs/guides/experiment-viewer.md) | 只读实验检查、DevBox 隧道与 Harbor 深入排查。 |
| [贡献指南](.github/CONTRIBUTING.md) | 开发环境搭建与仓库约定。 |
| [发布流程](docs/RELEASING.md) | 源码、产物与发布检查清单。 |

## 许可证

RSIHub 采用 [Apache-2.0](LICENSE) 许可证。必需的署名信息见 [NOTICE](NOTICE)。

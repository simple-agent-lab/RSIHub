# Documentation

The maintained public documentation has distinct roles:

| Document | Role |
| --- | --- |
| [Repository README](https://github.com/simple-agent-lab/RSIHub/blob/main/README.md) | concise repository overview and benchmark results |
| [Quick start guide](https://github.com/simple-agent-lab/RSIHub/blob/main/docs/QUICKSTART.md) | recipe launcher quick start |
| [`../index.md`](../index.md) | public documentation home |
| [`../concepts/design.md`](../concepts/design.md) | framework model and ownership boundaries |
| [Source architecture map](https://github.com/simple-agent-lab/RSIHub/blob/main/docs/ARCHITECTURE.md) | enforced `src/evolve/` module map and budgets |
| [`../reference/terminology.md`](../reference/terminology.md) | canonical framework language |
| [`coding-style.md`](coding-style.md) | coding conventions |
| [`../rsihub-mark.svg`](../rsihub-mark.svg) | generated RSIHub Ring identity mark |
| [`../rsihub-wordmark.svg`](../rsihub-wordmark.svg) | generated RSIHub gradient wordmark |
| [`../rsihub-lockup.svg`](../rsihub-lockup.svg) | generated RSIHub masthead lockup, mark beside wordmark |
| [`../evolve-lineage.svg`](../evolve-lineage.svg) | lineage figure, retained but not currently in the README |
| [`../assets/architecture.svg`](../assets/architecture.svg) | generated architecture diagram |
| [Operator interfaces](https://github.com/simple-agent-lab/RSIHub/blob/main/src/evolve/frozen/interfaces.py) | machine-readable operator contract |

Put dated proposals in `docs/designs/` and implementation plans in
`docs/plans/`. Update the maintained document that describes a behavior in the
same change as that behavior.

Regenerate the maintained README visuals after changing their content or style:

```bash
uv run python tools/generate_readme_assets.py
uv run python tools/generate_readme_assets.py --check
uv run python tools/generate_architecture_svg.py
uv run python tools/generate_architecture_svg.py --check
```

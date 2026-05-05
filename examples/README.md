# scitex-agent-container — examples

Minimal end-to-end recipes that exercise sac's public surface. Each
example is self-contained and runnable from a clean `pip install
-e .[dev,mcp]` checkout, and lands its outputs under `_out/`.

| File | What it shows |
|---|---|
| [`01_list_running_agents.py`](01_list_running_agents.py) | Programmatic version of `sac agent list --json` via the public Python API — useful when sac is embedded in a longer-running orchestrator. |
| [`02_mcp_self_introspect.py`](02_mcp_self_introspect.py) | Spin the sac MCP server in-process and enumerate every registered tool — the "what can an LLM do with sac?" answer. |
| [`00_run_all.sh`](00_run_all.sh) | Drives every example top-to-bottom (CI-friendly). |

## Running

```bash
pip install -e ".[dev,mcp]"
bash examples/00_run_all.sh
```

Outputs land under `examples/_out/<NN_name>/`.

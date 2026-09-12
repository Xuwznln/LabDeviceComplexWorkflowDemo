# Uni-Lab-OS Complex Workflow Demo

**English** | [中文](README_zh.md)

This external device package demonstrates **runtime control flow** in Uni-Lab-OS
workflows: loop containers (`for` / `while`) are executed round by round by the
local scheduler instead of being unrolled into N copies in the editor. One
virtual reactor provides deterministic, checkable state and return values; five
`@workflow` templates each introduce one loop form, and the last one chains them
into a realistic procedure:

- **`for` with a fixed count** (`with ctx.loop_for(3)`): body parameters use
  `{{loop.iteration}}` to produce sample ids S-1 / S-2 / S-3; a parameter that is
  exactly a placeholder keeps its numeric type, embedded placeholders are text
  substitutions;
- **`while` on device state** (`ctx.device_state("reactor", "temperature_c", "<", 80)`):
  the body is empty, the condition reads the reported `temperature_c` every 0.5 s
  while the device heats up in the background at 30 °C/s — the "wait until a state"
  idiom;
- **`while` on a node's return value** (`ctx.step_output("取样检测", "ready", "==", False)`):
  the body polishes and then samples; the condition references the probe step
  *inside* the body, so the body runs at least once — "repeat until ready";
- **nested `for`**: the outer loop switches plates, the inner loop fills wells;
  `{{loop.iteration}}` refers to the innermost loop, and the inner loop node is
  re-executed on every outer round;
- **combined**: dispense ×2 → heat in the background and wait for 80 °C → polish
  until ready → report.

Every round is a **new attempt** of the body nodes (`trigger=loop_iteration`): the
run page shows the current round's result plus the full history per node, and the
loop node itself shows "round i/N" (`control_data.loop`).

## Install from GitHub

```bash
unilab package install https://github.com/Xuwznln/LabDeviceComplexWorkflowDemo --ref <commit-sha>
```

For local development:

```bash
git clone https://github.com/Xuwznln/LabDeviceComplexWorkflowDemo.git
cd LabDeviceComplexWorkflowDemo
python -m pip install -e .
```

No AK/SK and no cloud lab are needed. Requires a Uni-Lab-OS build with workflow
loop containers (`type="loop"` nodes, `ctx.loop_for` / `ctx.loop_while` in
`@workflow`).

## Bounded dual-runtime smoke

```bash
python -m complex_workflow_demo.smoke --backend hostlink --timeout 90
python -m complex_workflow_demo.smoke --backend ros2 --timeout 150
```

The smoke starts a real runtime (`unilab -g graph/complex_workflow_demo.json`,
which reports the `@workflow` templates with the registry) and then replays the
web UI's HTTP calls: `GET /api/v1/registry/workflow-templates` to find a template →
`POST /api/v1/workflows/from-template` to instantiate it →
`POST /api/v1/workflow-tasks` to run → `GET /api/v1/workflow-tasks/{uuid}/node-runs`
to assert.

Results are always read from node-runs: one row per workflow node, `status /
return_info` is the current attempt, `attempts` embeds the node's full history
(one per loop round). All five workflows are expected to end `succeeded`:

| Template | Node runs (topological order) and attempt counts | Report assertions |
| --- | --- | --- |
| Fixed-count dispensing | reset 1 · **dispense ×3** (loop) 1 · dispense **3** · report 1 | `additions = ["S-1","S-2","S-3"]` |
| Wait for temperature | reset 1 · start heating 1 · **wait** (loop, empty body, 4 polls observed) 1 · stop heating 1 · report 1 | `temperature_c = 80` |
| Polish until ready | reset 1 · **polish until ready** (loop) 1 · polish **2** · probe **2** (ready false → true) · report 1 | `polish_count = 2`, `purity = 0.9` |
| Nested plates/wells | reset 1 · **plates ×2** 1 · switch plate **2** · **wells ×3** (inner loop) **2** · fill well **6** · report 1 | `plates = {"1":[1,2,3],"2":[1,2,3]}`, `fill_count = 6` |
| Complex workflow | reset · dispense ×2 · dispense **2** · start heating · wait · stop heating · polish until ready · polish **2** · probe **2** · report | `additions = ["S-1","S-2"]`, `temperature_c = 80`, `polish_count = 2` |

A loop node's `return_info.return_value.iterations` is the number of completed
rounds and `control_data.loop` is the current round with a summary, e.g.
`{"mode": "while", "condition": "reactor.temperature_c < 80.0", "iteration": 3, "max_iterations": 120}`.

## Manual start

```bash
python -m unilabos --backend hostlink --skip_env_check \
  --devices ./complex_workflow_demo --external_devices_only \
  --visual disable --disable_browser \
  -g ./graph/complex_workflow_demo.json

python -m unilabos --backend ros2 --disable_hostlink --skip_env_check \
  --devices ./complex_workflow_demo --external_devices_only \
  --visual disable --disable_browser \
  -g ./graph/complex_workflow_demo.json
```

Then open the management UI: insert one of the loop templates from the template
panel and run it, or build your own loop in the editor — select a few nodes,
right-click "Wrap in loop…", pick for / while and fill in the condition (device
state fields list the device's latest reported values). The run page draws the
loop as a frame with "round i/N", and the job list shows one attempt per round.

## Writing the template

```python
from unilabos.registry.workflows import WorkflowBuildContext, workflow

@workflow(display_name="复杂工作流演示")
def complex_workflow(ctx: WorkflowBuildContext) -> None:
    ctx.run("reactor/reset", {})
    with ctx.loop_for(2, name="加样 ×2"):
        ctx.run("reactor/add_reagent", {"sample_id": "S-{{loop.iteration}}"})
    ctx.run("reactor/start_heating", {"target_c": 80.0})
    with ctx.loop_while(ctx.device_state("reactor", "temperature_c", "<", 80.0), interval_seconds=0.5):
        pass  # empty body: wait until the temperature is reached
    ctx.run("reactor/stop_heating", {})
    with ctx.loop_while(ctx.step_output("取样检测", "ready", "==", False)):
        ctx.run("reactor/polish", {})
        ctx.run("reactor/check_quality", {"threshold": 0.85}, name="取样检测")
    ctx.run("reactor/report", {})
```

`max_iterations` on a `while` (default 1000) is a safety cap — reaching it fails
the loop; an empty-bodied `while` must set `interval_seconds`. Steps inside the
body are not declared yet when the `with` opens, so `step_output` references the
step by **name** and resolves it when the block closes.

## Layout

```text
graph/complex_workflow_demo.json     one graph for both backends (a single virtual reactor "reactor")
complex_workflow_demo/
  reactor.py                         virtual reactor: dispense / background heating / polish & probe / plate wells / report
  workflows.py                       five @workflow loop templates
  smoke.py                           bounded real-runtime proof driven through the management API
tests/test_hostlink_smoke.py         HostLink integration assertions
```

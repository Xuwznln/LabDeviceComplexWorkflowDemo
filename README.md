# Uni-Lab-OS Complex Workflow Demo

**English** | [中文](README_zh.md)

This external device package demonstrates **runtime control flow** in Uni-Lab-OS
workflows: loop containers (`for` / `while`) are executed round by round by the
local scheduler instead of being unrolled into N copies in the editor. One
virtual reactor provides deterministic, checkable state and return values; five
`@workflow` templates each introduce one control-flow form, and the last one chains them
into a realistic procedure:

- **`for` with a fixed count** (`with ctx.loop_for(3)`): body parameters use
  `{{loop.iteration}}` to produce sample ids S-1 / S-2 / S-3; a parameter that is
  exactly a placeholder keeps its numeric type, embedded placeholders are text
  substitutions;
- **`while` on device state** (`ctx.device_state("reactor", "temperature_c", "<", 80)`):
  the body is empty, the condition reads the reported `temperature_c` every 0.5 s
  while the device heats up in the background at 30 °C/s — the "wait until a state"
  idiom;
- **native `if/else` on an action result**: the workflow source is compiled statically,
  so `if quality.ready` becomes a condition on the action's result. Three measurements return false / false / true; only the
  selected branch executes, and the other branch records `skipped`;
- **nested `for`**: the outer loop switches plates, the inner loop fills wells;
  `{{loop.iteration}}` refers to the innermost loop, and the inner loop node is
  re-executed on every outer round;
- **combined**: dispense ×2 → heat in the background and wait for 80 °C → polish
  until ready → report.

Every round is a **new attempt** of the body nodes (`trigger=loop_iteration`): the
run page shows the current round's result plus the full history per node, and the
loop node itself shows "round i/N" (`control_data.loop`).

## Install from GitHub

Requires the unified Uni-Lab-OS core (dev architecture with the V1.0 HTTP API) on
**CPython 3.14**, plus its companion wheels: the maintained PyLabRobot fork
`1.0.0b1+unilabos…` and `opentrons-shared-data 10.1.0+unilabos.np2.1`. Build them with the
core's `scripts/build_wheel_release.py --dependencies-only` and install with `--find-links`.
ROS 2 runs use RoboStack Jazzy built for cp314 (`ros2-distro-mutex` 0.20).

With a Host running (`unilab --backend hostlink`), install through its management API. The
spec is the GitHub URL with an optional `@<commit-sha>`:

```bash
unilab package install https://github.com/Xuwznln/LabDeviceComplexWorkflowDemo@<commit-sha> --wait
```

Or install the distribution into the current Python environment (this is what CI does):

```bash
python -m pip install "git+https://github.com/Xuwznln/LabDeviceComplexWorkflowDemo@<commit-sha>"
```

For local development:

```bash
git clone https://github.com/Xuwznln/LabDeviceComplexWorkflowDemo.git
cd LabDeviceComplexWorkflowDemo
python -m pip install -e .
```

No AK/SK and no cloud lab are needed. Requires a Uni-Lab-OS build with workflow
loop containers (`type="loop"` nodes, `ctx.loop_for` / `ctx.loop_while` in
`@workflow`) and native `if/elif/else` on action results in `@workflow` source.
HostLink has been verified locally against the unified core; the ROS 2 smoke runs in CI only.

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
| Polish until ready | reset 1 · **probe ×3** (loop) 1 · probe/condition **3** · confirm **3** (first two skipped) · polish **3** (last skipped) · report 1 | `polish_count = 2`, `purity = 0.9` |
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

Then open a frontend connected to this backend (`unilab` serves no UI itself; the
page at `http://127.0.0.1:8002/` links OpenLab): insert one of the loop templates from the template
panel and run it, or build your own loop in the editor — select a few nodes,
right-click "Wrap in loop…", pick for / while and fill in the condition (device
state fields list the device's latest reported values). The run page draws the
loop as a frame with "round i/N", and the job list shows one attempt per round.

## Writing the template

Templates are compiled statically from source (the function body is never executed);
real action results are read at runtime.

```python
@workflow(display_name="三轮质量检查")
def quality_check(ctx):
    ctx.run("reactor/reset", {})
    with ctx.loop_for(3):
        quality = ctx.run("reactor/check_quality", {"threshold": 0.85})
        if quality.ready:
            ctx.run("reactor/report", {})
        else:
            ctx.run("reactor/polish", {})
    ctx.run("reactor/report", {})
```

The original combined workflow is compiled the same way:

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
  reactor.py                         virtual reactor: dispense / background heating / polish & probe / plate wells / report,
                                     plus the custody, transfer and aliquot actions
  workflows.py                       five loop templates and three resource templates (@workflow)
  smoke.py                           bounded real-runtime proof driven through the management API
  physical_smoke.py                  the joint custody / aliquot / restock scenario that smoke.py runs last
tests/test_hostlink_smoke.py         HostLink integration assertions
tests/test_workflow_template_contract.py  template import contract (no runtime)
tests/test_action_selectors.py       registry: site and device parameters get Site / device pickers
```


### Workflow inputs and outputs

"循环演示：提纯达标" declares a `threshold` input with a static annotation (default 0.85, range
0–1, unit fraction); adjust it in the frontend's workflow input area before a run. Input
references in action parameters stay in the flow; rebinding devices is still done in the
frontend instance configuration. The template returns `PolishResult`: `purity` and
`polish_count` become the task's named outputs and are checked for type and range on
completion. The HostLink smoke also checks the default input, the condition attempt history
and the final output `{purity: 0.9, polish_count: 2}`.


### Durable resource intervals (unified core)

Polish (`polish`) and quality check (`check_quality`) occupy the device, and the scheduling
authority persists the interval between the two actions. The other demo actions keep their
lock-free declarations. Besides the results of the five workflows, the HostLink smoke checks
each device attempt's resource admission, that the in-use list is empty afterwards, and that
every acquired interval is released. This uses the virtual reactor; it is not a real-hardware
or multi-slave acceptance.


## Resource lending and device custody

`资源演示：样品托管`: `sample` is a ResourceSlot input picked in the frontend, `vessel` defaults to
the readable device ID `reactor`;
`reactor/load_sample → reactor/process_sample → reactor/unload_sample`.
The graph holds a virtual sample. After loading, the task pauses in single-step mode, other
tasks still cannot use the device, and they can continue only after unloading succeeds.
`资源演示：空闲借还`: two consecutive checks; after the first step the paused task's interval is
lent explicitly, and the original task continues with the second step only after the borrower
has finished. A failed loan has to be checked and returned by hand.

Requires the unified Uni-Lab-OS core (durable resource intervals, lending and device custody).
`python -m complex_workflow_demo.smoke --backend hostlink --timeout 90` runs the five original
workflows, four resource-scenario tasks and the joint settlement workflow below, ten tasks in
all, and checks start permits, resource use, loans and the final custody state against the
separate authority / Host databases. The demo uses virtual devices; it is not a real-hardware
or multi-slave acceptance.

The resource semantics of these actions are one experimental `@action` field, `material_contract=` (`reactor.py`):

```python
@action(display_name="装载样品", material_contract=MaterialRole.load("sample", device="vessel"))
def load_sample(self, sample: ResourceSlot, vessel: DeviceSlot) -> CustodyResult: ...

@action(display_name="原位处理", material_contract=MaterialRole.in_place("sample"))
@action(display_name="卸载样品", material_contract=MaterialRole.unload("sample", device="vessel"))

@action(display_name="抓取到夹爪", material_contract=MaterialRole.pick("sample", from_site="from_site"))
def pick(self, sample: ResourceSlot, from_site: SiteSlot) -> None: ...

@action(display_name="放回目标库位", material_contract=MaterialRole.place("sample", to_site="to_site"))
def place(self, sample: ResourceSlot, to_site: SiteSlot) -> None: ...

@action(display_name="按实测量分装", material_contract=MaterialRole.dispense(from_material="source", to_materials=["target"]))
```

The strings are parameter names, checked against the signature at registration. Parameters
and return values are validated against the signature and the return annotation (one standard
for every action; core doc `docs/developer_guide/action_standard.md`).

`pick` / `place` / `dispense` declare real material actions: the core settles the sample's
location from the declaration frozen at dispatch (the driver returns no transfer receipt) and the
contents from the aliquot receipt, so the driver never writes the location itself. The owner of `from_site` / `to_site` is derived from the
site, and the carrier is the executing device (`carrier_device=` omitted): the reactor has one
`meta_data.role == "gripper"` site (`available_sites` on `@device`, `grip` in the graph), and the
sample sits on it between `pick` and `place` while the task holds the reactor. The whole
`material_contract=` field is experimental: each action that uses it warns once at registration; `device=` is a
`DeviceSlot` parameter and may be omitted to mean the executing device.

The parameter type picks the editor's selector and the lock: `ResourceSlot` gives a material
picker and a material lock, `SiteSlot` a Site picker (the value is a site UUID) and a site lock,
`DeviceSlot` a device picker (the value is a device ID) and a device lock. Workflow inputs
accept only basic types and `ResourceSlot`, so the template inputs `vessel` and
`origin_site` / `target_site` stay `str` and are passed on to those parameters.


## Joint demo: conditional inventory, custody and actual settlement

The backend is the unified Uni-Lab-OS core (with
[#360](https://github.com/deepmodeling/Uni-Lab-OS/pull/360): loop scopes, lending / custody,
conditional inventory and physical receipt settlement; CPython 3.14 only). The core commit CI
pins is `STABLE_CORE_REF` in `.github/workflows/check_registry.yml`. Local HostLink runs do
not replace a fresh install or ROS 2 verification.

`联合演示：托管分装与补料` is a `@workflow` template. It uses the device ID `reactor` as its
default role and, inside an explicit resource scope, picks the sample, selects an inventory
source and aliquots in two rounds, then places the sample on the target site. The frontend
handles role rebinding and the entry resource parameters. `pick.from_site` and
`place.to_site` are bound to the workflow inputs, and the real site UUIDs arrive with the
task `input`. A script therefore imports the template with
`"site_binding_mode": "preserve"`, as the browser does; with the default `resolve` the import
treats the input references as Site labels and fails.

`python -m complex_workflow_demo.smoke --backend hostlink --timeout 90` starts an isolated
authority + Host and runs the original loop, lending and custody workflows plus the joint
scenario. It first stocks 7 ul; the aliquot action's preflight reports a shortage before the
action is dispatched (the pending decision list shows a `preflight` decision: retry, skip,
operator replacement with success, or abort); after restocking 20 ul it retries. Each round
plans 10 ul and the virtual measurement is 8 ul, leaving 84 ul at the source, 16 ul at the
target and 11 ul in stock. The unselected branch reserves no stock; at the end the gripper is
empty and custody and physical occupancy are released. Requires a unified core with
material_execution and cross-loop resource_scope.

The demo's aliquot returns only a virtual measured receipt, and pick/place return nothing (the
core settles the declared move when the action succeeds). A real driver must report the amount
actually done; the runtime does not fabricate measurements from the planned parameters.

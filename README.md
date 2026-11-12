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
- **native `if/else` on an action result**: `compiler="ast"` statically compiles
  `if quality.ready`. Three measurements return false / false / true; only the
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
`@workflow`) and the new `compiler="ast"` condition support.
The local migration version has been verified with HostLink; ROS 2 has not been verified.

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

Then open the management UI: insert one of the loop templates from the template
panel and run it, or build your own loop in the editor — select a few nodes,
right-click "Wrap in loop…", pick for / while and fill in the condition (device
state fields list the device's latest reported values). The run page draws the
loop as a frame with "round i/N", and the job list shows one attempt per round.

## Writing the template

The conditional template uses static compilation; real action results are read at runtime.

```python
@workflow(display_name="三轮质量检查", compiler="ast")
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

The original combined workflow keeps the builder mode:

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


### 工作流输入输出合同

「循环演示：提纯达标」使用静态注解声明 `threshold` 输入（默认 0.85，范围 0–1，单位 fraction），
运行前在前端工作流输入区调整。动作参数中的输入引用随流程保留，设备换绑仍由前端实例配置完成。
末尾返回 `PolishResult`：`purity` 与 `polish_count` 会成为任务的具名输出，并在完成时检查类型与范围。
HostLink smoke 同时核对默认输入、条件 attempt 历史和最终输出 `{purity: 0.9, polish_count: 2}`。


### 持久资源区间验证（dev 迁移工作区）

提纯（polish）和质量检测（check_quality）现在使用设备占用，两个动作之间的连续区间
由调度权威持久保存。其余免锁演示动作保留原有声明。HostLink smoke 除五条流程的
业务结果外，还核对每次设备尝试的资源准入、结束后的实际使用清单为空，以及取得过
的区间全部释放。该验证使用虚拟反应釜，不代表真实硬件或多 slave 验收。


## 资源借还与设备托管演示

新增 `资源演示：样品托管`：sample 为前端选择的 ResourceSlot 输入，vessel 默认使用可读设备 ID reactor，
`reactor/load_sample → reactor/process_sample → reactor/unload_sample`。
图中增加虚拟 sample；装载后任务单步停顿，其他任务仍不能使用设备，卸载成功后才可继续。
新增 `资源演示：空闲借还`：连续两次检测，第一步结束后显式借出暂停任务的区间，
借用者执行完后，原任务才能继续第二步。借用异常需要核实后人工归还。

需要支持 runtime.db v8 的 dev 迁移后端。`python -m complex_workflow_demo.smoke --backend hostlink --timeout 120`
会运行原有五条流程、四次资源场景任务及下述联合结算流程，共十个任务，并对照独立 authority / Host 数据库，
验证开始许可、资源使用、借还及托管最终状态。该演示使用虚拟设备，不代表真实硬件或多 slave 验收。


## 条件库存、托管与实际结算联合演示

配套后端为 [Uni-Lab-OS #360](https://github.com/deepmodeling/Uni-Lab-OS/pull/360)，当前验证提交 `a4dc4e6af010b16dcbe22809d696c2122c464bb8`，CI 使用同一提交。该 Core 的 PLR 配套包发布校验仍待完成，因此本演示的 PR 保持 Draft；已有 HostLink 联调结果不替代全新环境安装或 ROS 2 验证。

`联合演示：托管分装与补料` 是 AST `@workflow` 模板。它以 `reactor` 设备 ID 为默认角色，在显式资源作用域里抓取样品、循环两次选择库存来源并分装，最后放回目标库位。前端负责角色换绑和入口资源参数。

`python -m complex_workflow_demo.smoke --backend hostlink --timeout 120` 会启动隔离的 authority + Host，运行原有循环、借还和托管流程，以及新的联合场景：先入库 7 ul，进入缺料等待后补入 20 ul；每轮计划 10 ul、虚拟实测 8 ul，最终来源剩余 84 ul、目标收到 16 ul、库存剩余 11 ul。未选分支不占库存，最终夹爪为空、托管及物理占用均释放。需要包含 material_execution 和跨循环 resource_scope 的新版 dev。

演示中的 pick/place/aliquot 只返回虚拟回执。真实驱动必须报告实际完成量和实际到达的目标库位；运行时不会据计划参数伪造实测值。

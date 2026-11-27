# Uni-Lab-OS 复杂工作流演示

[English](README.md) | **中文**

这个外部设备包演示 Uni-Lab-OS 工作流里的**运行时控制流**：循环容器（`for` / `while`）由本机调度器
逐轮执行，而不是在编辑器里把节点复制 N 份。一台虚拟反应釜提供可判定、可复现的状态与返回值，
五条 `@workflow` 模板逐个引入一种控制流形态，最后一条把它们串成一个真实流程：

- **`for` 固定轮数**（`with ctx.loop_for(3)`）：循环体参数里用 `{{loop.iteration}}` 生成样品编号
  S-1 / S-2 / S-3；整个参数值恰为占位符时保持数字类型，嵌在文本里做替换；
- **`while` 判定设备状态**（`ctx.device_state("reactor", "temperature_c", "<", 80)`）：循环体为空，
  每 0.5 s 读一次设备上报的 `temperature_c`，设备在后台以 30 ℃/s 升温——这是「等到某状态」的写法；
- **原生 `if/else` 判定节点返回值**：工作流按源码静态编译，`if quality.ready` 成为对动作结果的
  条件判断，三轮检测依次为 false / false / true；未达标才提纯，达标则确认，未选分支记录 `skipped`；
- **嵌套 `for`**：外层切换板、内层给孔加样，`{{loop.iteration}}` 取最内层；内层循环节点随外层每轮
  重新执行；
- **组合**：加样 ×2 → 后台升温并等到 80 ℃ → 提纯直到达标 → 汇总。

每一轮都是循环体节点的一个**新 attempt**（`trigger=loop_iteration`）：运行页按节点看到当前轮结果与
全部轮次历史，循环节点自己显示「第 i/N 轮」（`control_data.loop`）。

## 从 GitHub 安装

需要统一版 Uni-Lab-OS 核心（dev 架构 + V1.0 HTTP API），仅支持 **CPython 3.14**；配套 wheel
（PyLabRobot 维护分支 `1.0.0b1+unilabos…` 与 `opentrons-shared-data 10.1.0+unilabos.np2.1`）
用核心的 `scripts/build_wheel_release.py --dependencies-only` 构建，安装时经 `--find-links` 提供。
ROS 2 运行使用为 cp314 构建的 RoboStack Jazzy（`ros2-distro-mutex` 0.20）。

Host 运行时（`unilab --backend hostlink`）经管理 API 安装，来源是 GitHub 地址，可附 `@<commit-sha>`：

```bash
unilab package install https://github.com/Xuwznln/LabDeviceComplexWorkflowDemo@<commit-sha> --wait
```

也可以直接装进当前 Python 环境（CI 即如此）：

```bash
python -m pip install "git+https://github.com/Xuwznln/LabDeviceComplexWorkflowDemo@<commit-sha>"
```

本地开发可使用：

```bash
git clone https://github.com/Xuwznln/LabDeviceComplexWorkflowDemo.git
cd LabDeviceComplexWorkflowDemo
python -m pip install -e .
```

本地演示不需要 AK/SK，也不依赖云端实验室。需要包含工作流循环容器的 Uni-Lab-OS 版本
（`type="loop"` 节点、`ctx.loop_for` / `ctx.loop_while`），以及 `@workflow` 源码里按动作结果的原生 `if/elif/else`。
条件流程已在统一版核心上本地通过 HostLink 验证；ROS 2 smoke 只在 CI 中运行。

## 有终止条件的双运行时 smoke

```bash
python -m complex_workflow_demo.smoke --backend hostlink --timeout 90
python -m complex_workflow_demo.smoke --backend ros2 --timeout 150
```

smoke 启动真实运行时（`unilab -g graph/complex_workflow_demo.json`，启动时把 `@workflow` 模板随注册表
上报），然后经管理 HTTP API 完整复现网页的操作：`GET /api/v1/registry/workflow-templates` 找模板 →
`POST /api/v1/workflows/from-template` 实例化 → `POST /api/v1/workflow-tasks` 运行 →
`GET /api/v1/workflow-tasks/{uuid}/node-runs` 断言。

节点结果一律读 node-runs：每个工作流节点一条，`status / return_info` 是当前 attempt 的结果，
`attempts` 内嵌该节点的完整执行历史（循环体节点每轮一个）。五条工作流全部预期 `succeeded`：

| 模板 | 节点运行（拓扑序）与 attempt 数 | 汇总报告断言 |
| --- | --- | --- |
| 「循环演示：定次加样」 | 复位 1 · **加样 ×3**（loop）1 · 加样 **3** · 汇总报告 1 | `additions = ["S-1","S-2","S-3"]` |
| 「循环演示：等待升温」 | 复位 1 · 开始升温 1 · **等待升温**（loop，空循环体，实测轮询 4 次）1 · 停止升温 1 · 汇总报告 1 | `temperature_c = 80` |
| 「循环演示：提纯达标」 | 复位 1 · **检测 ×3**（loop）1 · 检测/条件 **3** · 达标确认 **3**（前两轮 skipped）· 提纯 **3**（末轮 skipped）· 汇总 1 | `polish_count = 2`，`purity = 0.9` |
| 「循环演示：嵌套板孔」 | 复位 1 · **板 ×2** 1 · 切换到板 **2** · **板孔 ×3**（内层 loop）**2** · 孔位加样 **6** · 汇总报告 1 | `plates = {"1":[1,2,3],"2":[1,2,3]}`，`fill_count = 6` |
| 「复杂工作流演示」 | 复位 · 加样 ×2 · 加样 **2** · 开始升温 · 等待升温 · 停止升温 · 提纯直到达标 · 提纯 **2** · 取样检测 **2** · 汇总报告 | `additions = ["S-1","S-2"]`，`temperature_c = 80`，`polish_count = 2` |

循环节点的 `return_info.return_value.iterations` 是完成的轮数，`control_data.loop` 是当前轮次与摘要
（如 `{"mode": "while", "condition": "reactor.temperature_c < 80.0", "iteration": 3, "max_iterations": 120}`）。

## 手动启动

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

然后打开连接本后端的前端（`unilab` 本身不提供页面，`http://127.0.0.1:8002/` 上有 OpenLab 入口）：
在「工作流模板」里插入任一条「循环演示」模板运行，或在编辑器里自己包一个循环——
选中几个节点右键「包成循环…」，选 for / while 并填条件（设备状态字段会列出设备最近上报的值）；
运行页的循环框显示「第 i/N 轮」，节点作业列表里每一轮是一个 attempt。

## 模板怎么写

模板按源码静态编译：不执行函数体，运行时才读取真实动作结果。

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

下面的原有复杂流程同样按源码静态编译：

```python
from unilabos.registry.workflows import WorkflowBuildContext, workflow

@workflow(display_name="复杂工作流演示")
def complex_workflow(ctx: WorkflowBuildContext) -> None:
    ctx.run("reactor/reset", {})
    with ctx.loop_for(2, name="加样 ×2"):
        ctx.run("reactor/add_reagent", {"sample_id": "S-{{loop.iteration}}"})
    ctx.run("reactor/start_heating", {"target_c": 80.0})
    with ctx.loop_while(ctx.device_state("reactor", "temperature_c", "<", 80.0), interval_seconds=0.5):
        pass  # 空循环体：等到温度到位
    ctx.run("reactor/stop_heating", {})
    with ctx.loop_while(ctx.step_output("取样检测", "ready", "==", False)):
        ctx.run("reactor/polish", {})
        ctx.run("reactor/check_quality", {"threshold": 0.85}, name="取样检测")
    ctx.run("reactor/report", {})
```

`while` 的 `max_iterations`（缺省 1000）是安全上限，达到仍未结束按失败收敛；空循环体的 `while`
必须给 `interval_seconds`。循环体里的步骤在 `with` 打开时还没声明，所以 `step_output` 用步骤**名**
引用，块结束时解析。

## 目录

```text
graph/complex_workflow_demo.json     两种 backend 共用的一份图（一台虚拟反应釜 reactor）
complex_workflow_demo/
  reactor.py                         虚拟反应釜：加样 / 后台升温 / 提纯检测 / 板孔加样 / 汇总，以及托管、搬运与分装动作
  workflows.py                       五条循环模板与三条资源模板（@workflow）
  smoke.py                           经管理 API 驱动的有终止条件真实运行时证明
  physical_smoke.py                  smoke.py 最后运行的托管 / 分装 / 补料联合场景
tests/test_hostlink_smoke.py         HostLink 集成断言
tests/test_workflow_template_contract.py  模板导入合同（不起运行时）
tests/test_action_selectors.py       注册表：库位与设备参数得到库位 / 设备选择器
```


### 工作流输入输出合同

「循环演示：提纯达标」使用静态注解声明 `threshold` 输入（默认 0.85，范围 0–1，单位 fraction），
运行前在前端工作流输入区调整。动作参数中的输入引用随流程保留，设备换绑仍由前端实例配置完成。
末尾返回 `PolishResult`：`purity` 与 `polish_count` 会成为任务的具名输出，并在完成时检查类型与范围。
HostLink smoke 同时核对默认输入、条件 attempt 历史和最终输出 `{purity: 0.9, polish_count: 2}`。


### 持久资源区间验证（统一版核心）

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

需要统一版 Uni-Lab-OS 核心（持久资源区间、借还与设备托管）。`python -m complex_workflow_demo.smoke --backend hostlink --timeout 90`
会运行原有五条流程、四次资源场景任务及下述联合结算流程，共十个任务，并对照独立 authority / Host 数据库，
验证开始许可、资源使用、借还及托管最终状态。该演示使用虚拟设备，不代表真实硬件或多 slave 验收。

这些动作的资源语义写在 `@action` 的一个字段 `material_contract=` 里（`reactor.py`，实验性功能）：

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

字符串都是参数名，注册时对照签名检查。参数与返回值按签名和返回注解校验（所有动作同一标准，核心文档
`docs/developer_guide/action_standard.md`）。

`pick` / `place` / `dispense` 声明的是真实的物料动作：样品位置由核心按派发前冻结的声明结算（驱动不交搬运回执），内容物按分装回执结算，驱动不自己写位置。
`from_site` / `to_site` 的所属物料由库位决定；搬运设备省略（不写 `carrier_device=`）即执行设备：reactor 有唯一一个
`meta_data.role == "gripper"` 的库位（`@device` 的 `available_sites`，图里的 `grip`），`pick` 到 `place` 之间样品在这个库位上、
任务托管 reactor。整个 `material_contract=` 字段是实验性功能，用到它的动作注册时各告警一次；`device=` 是 `DeviceSlot` 参数，省略即执行设备。

参数类型决定编辑器里的选择器和锁：`ResourceSlot` 是物料选择器并加物料锁，`SiteSlot` 是库位选择器（值为 site uuid）并加库位锁，
`DeviceSlot` 是设备选择器（值为设备 ID）并加设备锁。工作流输入只支持基础类型与 `ResourceSlot`，
所以模板输入 `vessel` 和 `origin_site` / `target_site` 仍是 `str`，再传给上述参数。

## 条件库存、托管与实际结算联合演示

配套后端为统一版 Uni-Lab-OS 核心（合并了 [#360](https://github.com/deepmodeling/Uni-Lab-OS/pull/360) 的循环作用域、借还 / 托管、条件库存与物理回执结算，仅支持 CPython 3.14），CI 固定的核心提交见 `.github/workflows/check_registry.yml` 的 `STABLE_CORE_REF`。本地 HostLink 联调结果不替代全新环境安装或 ROS 2 验证。

`联合演示：托管分装与补料` 使用 `@workflow` 模板和 `reactor` 设备 ID 默认角色。显式作用域覆盖抓取、两轮动态来源与条件分装，以及放回目标库位；设备换绑和入口资源选择由前端负责。`pick.from_site` 与 `place.to_site` 绑定到工作流输入，真实 site uuid 随任务 `input` 提交，所以脚本导入该模板时传 `"site_binding_mode": "preserve"`（与浏览器相同）；默认的 `resolve` 会把输入引用当作库位标签去解析而失败。

运行上面的 smoke 命令会先入库 7 ul，分装动作执行前预检报缺料（待决策清单出现 `preflight` 决策：重试、跳过、人工替换为成功或放弃），补入 20 ul 后重试；每轮计划 10 ul、虚拟实测 8 ul。最终来源剩余 84 ul、目标收到 16 ul、库存剩余 11 ul；未选分支不预留，夹爪、托管和物理占用全部释放。

aliquot（`MaterialRole.dispense`）返回虚拟实测回执；pick/place（`MaterialRole.pick` / `place`）不返回内容，动作成功后由核心按声明结算位置。真实驱动必须报告实际完成量，不能用计划参数替代实测。

"""复杂工作流演示的 @workflow 模板：循环容器（for / while）在真实调度器里逐轮执行。

host 启动时主仓 AST 扫描发现本模块，模板随注册表上报（``GET /api/v1/registry/workflow-templates``）；
网页"工作流模板"面板或 ``POST /api/v1/workflows/from-template`` 把它实例化成工作流，再
``POST /api/v1/workflow-tasks`` 运行。五条模板逐个引入一种控制流，最后一条把它们串成一个流程：

1. 「循环演示：定次加样」——``for`` 固定 3 轮，循环体参数用 ``{{loop.iteration}}`` 生成样品编号；
2. 「循环演示：等待升温」——``while`` 对**设备状态** ``reactor.temperature_c < 80`` 判定，循环体为空
   （每 0.5 s 判定一次，"等到某状态"），设备在后台升温；
3. 「循环演示：提纯达标」——静态 AST 编译原生 ``if/else``：三轮检测，未达标则提纯，
   达标则确认；三个检测结果为 false / false / true，未选分支记录 skipped；
4. 「循环演示：嵌套板孔」——两层 ``for``：外层切换板、内层给孔加样，各自的 ``{{loop.iteration}}``
   取最内层；
5. 「复杂工作流演示」——加样 ×2 → 后台升温并等到位 → 提纯直到达标 → 汇总。

循环体每轮是循环体节点的一个新 attempt（``trigger=loop_iteration``），运行页按节点看到当前轮
结果与全部轮次历史；循环节点自己显示"第 i/N 轮"。
"""

from typing import Annotated, TypedDict

from pydantic import Field
from unilabos.registry.workflows import WorkflowBuildContext, WorkflowGuide, workflow


class PolishResult(TypedDict):
    purity: Annotated[float, Field(ge=0, le=1, unit="fraction")]
    polish_count: Annotated[int, Field(ge=0, unit="count")]


#: smoke/测试按显示名检索模板，保持单一出处。
FOR_WORKFLOW_NAME = "循环演示：定次加样"
WAIT_WORKFLOW_NAME = "循环演示：等待升温"
UNTIL_WORKFLOW_NAME = "循环演示：提纯达标"
NESTED_WORKFLOW_NAME = "循环演示：嵌套板孔"
COMPLEX_WORKFLOW_NAME = "复杂工作流演示"

#: 与设备 reactor.py 的常量配套：20 ℃ 起以 30 ℃/s 升到 80 ℃ 约 2 s；纯度 0.4 起每次 +0.25，两次后 ≥ 0.85。
TARGET_TEMPERATURE_C = 80.0
PURITY_THRESHOLD = 0.85
#: 取样检测步骤名：while 条件按名字引用循环体里的这一步（块结束时解析）。
QUALITY_STEP_NAME = "取样检测"

_LOOP_NOTE = (
    "循环体每轮是循环体节点的一个新 attempt：运行页节点上能看到当前轮结果与全部轮次历史，"
    "循环节点自己显示「第 i/N 轮」。"
)


def _reset(ctx: WorkflowBuildContext) -> None:
    ctx.run(
        "reactor/reset",
        {},
        name="复位",
        description="温度回到 20 ℃、纯度回到 0.4，清空累计记录——每条演示都从同一状态出发。",
    )


@workflow(
    display_name=FOR_WORKFLOW_NAME,
    description="复位 -> for ×3 加样（编号 S-{{loop.iteration}}）-> 汇总（预期 additions = S-1, S-2, S-3）",
    tags=["complex-workflow", "loop-for"],
    guide=WorkflowGuide(
        preparation=["「设备」页确认虚拟反应釜 reactor 在线；无需准备物料。"],
        expected=[
            "「加样」节点 attempt_count = 3，三轮的样品编号分别是 S-1 / S-2 / S-3。",
            '「汇总报告」返回 additions = ["S-1", "S-2", "S-3"]。',
        ],
        notes=[_LOOP_NOTE, "整个参数值恰好是 {{loop.index}} 这类占位符时保持数字类型，嵌在文本里则做替换。"],
    ),
)
def for_loop_dispense(ctx: WorkflowBuildContext) -> None:
    """for 循环：固定轮数，迭代变量进参数。"""

    _reset(ctx)
    with ctx.loop_for(3, name="加样 ×3", description="固定三轮，每轮加一份样品。"):
        ctx.run(
            "reactor/add_reagent",
            {"sample_id": "S-{{loop.iteration}}", "volume_ml": 5.0},
            name="加样",
            description="样品编号由当前轮次生成：第 1 轮 S-1，第 2 轮 S-2……",
        )
    ctx.run("reactor/report", {}, name="汇总报告", description="读回加样清单。")


@workflow(
    display_name=WAIT_WORKFLOW_NAME,
    description="复位 -> 开始后台升温 -> while 温度 < 80 ℃（空循环体，每 0.5 s 判定）-> 停止升温 -> 汇总",
    tags=["complex-workflow", "loop-while", "device-state"],
    guide=WorkflowGuide(
        preparation=["「设备」页确认 reactor 在线，能看到状态字段 temperature_c 在上报。"],
        expected=[
            "「开始升温」立刻返回，设备在后台以 30 ℃/s 升温；「等待升温」循环每 0.5 s 读一次设备状态 temperature_c。",
            "温度达到 80 ℃ 后条件不成立，循环结束，「停止升温」与「汇总报告」执行，temperature_c = 80。",
        ],
        notes=[
            "这是「等到某状态」的写法：while 的循环体为空，只按间隔反复判定设备状态；达到最多轮数仍未到位会按失败收敛。",
            _LOOP_NOTE,
        ],
    ),
)
def wait_for_temperature(ctx: WorkflowBuildContext) -> None:
    """while + 设备状态：空循环体轮询，直到温度到位。"""

    _reset(ctx)
    ctx.run(
        "reactor/start_heating",
        {"target_c": TARGET_TEMPERATURE_C},
        name="开始升温",
        description="启动后台加热线程，动作本身立刻返回。",
    )
    with ctx.loop_while(
        ctx.device_state("reactor", "temperature_c", "<", TARGET_TEMPERATURE_C),
        interval_seconds=0.5,
        max_iterations=120,
        name="等待升温",
        description="每 0.5 s 判定一次设备状态 temperature_c < 80，不成立即结束。",
    ):
        pass
    ctx.run("reactor/stop_heating", {}, name="停止升温", description="回报到位后的温度。")
    ctx.run("reactor/report", {}, name="汇总报告", description="temperature_c 应为 80。")


@workflow(
    display_name=UNTIL_WORKFLOW_NAME,
    compiler="ast",
    description="复位 -> 三轮检测：未达标则提纯，达标则确认 -> 汇总（预期提纯两次，纯度 0.9）",
    tags=["complex-workflow", "loop-for", "condition", "ast"],
    guide=WorkflowGuide(
        preparation=["确认虚拟反应釜 reactor 在线；需要支持 compiler=ast 的框架版本。"],
        expected=[
            "三轮检测 ready 分别为 false、false、true；条件节点保存每轮 matched 结果。",
            "「未达标提纯」前两轮 succeeded，第三轮 skipped；「达标确认」恰好相反。",
            "最终 polish_count = 2、purity = 0.9；跳过的分支不会收到设备调用。",
        ],
        notes=["函数体仅静态分析；运行时读取每轮检测动作的真实返回值。", _LOOP_NOTE],
    ),
)
def polish_until_ready(
    ctx: WorkflowBuildContext, *, threshold: Annotated[float, Field(ge=0, le=1, unit="fraction")] = 0.85
) -> PolishResult:
    """固定三轮覆盖 if/else 两条路径，便于复现和检查 attempt 历史。"""

    ctx.run("reactor/reset", {}, name="复位")
    with ctx.loop_for(3, name="检测 ×3"):
        quality = ctx.run(
            "reactor/check_quality",
            {"threshold": threshold},
            name=QUALITY_STEP_NAME,
        )
        if quality.ready:
            ctx.run("reactor/report", {}, name="达标确认")
        else:
            ctx.run("reactor/polish", {}, name="未达标提纯")
    report = ctx.run("reactor/report", {}, name="汇总报告")
    return {"purity": report.purity, "polish_count": report.polish_count}


@workflow(
    display_name=NESTED_WORKFLOW_NAME,
    description="复位 -> for 板 ×2 { 切换到板 -> for 孔 ×3 { 孔位加样 } } -> 汇总（预期 6 次孔位加样）",
    tags=["complex-workflow", "loop-for", "nested"],
    guide=WorkflowGuide(
        preparation=["「设备」页确认 reactor 在线；无需准备物料。"],
        expected=[
            "外层两轮各切换一块板（plate = 1、2），内层每块板三个孔（well = 1、2、3）。",
            "「孔位加样」attempt_count = 6，「板孔 ×3」内层循环节点 attempt_count = 2（外层每轮重新执行一遍）。",
            '「汇总报告」返回 plates = {"1": [1,2,3], "2": [1,2,3]}、fill_count = 6。',
        ],
        notes=["嵌套循环里 {{loop.iteration}} 取最内层循环的轮次。", _LOOP_NOTE],
    ),
)
def nested_plate_wells(ctx: WorkflowBuildContext) -> None:
    """两层 for：外层板、内层孔。"""

    _reset(ctx)
    with ctx.loop_for(2, name="板 ×2", description="外层：两块板。"):
        ctx.run(
            "reactor/start_plate",
            {"plate": "{{loop.iteration}}"},
            name="切换到板",
            description="板号 = 外层当前轮次（整个参数恰为占位符，保持整数类型）。",
        )
        with ctx.loop_for(3, name="板孔 ×3", description="内层：每块板三个孔。"):
            ctx.run(
                "reactor/fill_well",
                {"well": "{{loop.iteration}}", "volume_ml": 2.0},
                name="孔位加样",
                description="孔号 = 内层当前轮次。",
            )
    ctx.run("reactor/report", {}, name="汇总报告", description="plates 应为两块板各三孔。")


@workflow(
    display_name=COMPLEX_WORKFLOW_NAME,
    description="加样 ×2 -> 后台升温并等到 80 ℃ -> 提纯直到达标 -> 汇总：三种循环串成一条流程",
    tags=["complex-workflow", "loop-for", "loop-while"],
    guide=WorkflowGuide(
        preparation=["「设备」页确认 reactor 在线，状态字段 temperature_c 在上报；无需准备物料。"],
        expected=[
            "「加样」2 个 attempt（S-1、S-2）；「等待升温」在温度到 80 ℃ 后结束；「提纯」「取样检测」各 2 个 attempt。",
            '「汇总报告」返回 additions = ["S-1", "S-2"]、temperature_c = 80、polish_count = 2、purity = 0.9。',
        ],
        notes=[_LOOP_NOTE],
    ),
)
def complex_workflow(ctx: WorkflowBuildContext) -> None:
    """三种循环串成一条真实流程：定次加样、等待设备状态、重复直到达标。"""

    _reset(ctx)
    with ctx.loop_for(2, name="加样 ×2", description="两份样品。"):
        ctx.run(
            "reactor/add_reagent",
            {"sample_id": "S-{{loop.iteration}}", "volume_ml": 5.0},
            name="加样",
            description="样品编号 S-1、S-2。",
        )
    ctx.run("reactor/start_heating", {"target_c": TARGET_TEMPERATURE_C}, name="开始升温", description="后台升温。")
    with ctx.loop_while(
        ctx.device_state("reactor", "temperature_c", "<", TARGET_TEMPERATURE_C),
        interval_seconds=0.5,
        max_iterations=120,
        name="等待升温",
        description="每 0.5 s 判定设备状态，到 80 ℃ 结束。",
    ):
        pass
    ctx.run("reactor/stop_heating", {}, name="停止升温", description="回报到位温度。")
    with ctx.loop_while(
        ctx.step_output(QUALITY_STEP_NAME, "ready", "==", False),
        name="提纯直到达标",
        description="提纯 -> 检测，直到 ready。",
    ):
        ctx.run("reactor/polish", {}, name="提纯", description="纯度 +0.25。")
        ctx.run(
            "reactor/check_quality", {"threshold": PURITY_THRESHOLD}, name=QUALITY_STEP_NAME, description="ready 判定。"
        )
    ctx.run("reactor/report", {}, name="汇总报告", description="一次读回加样、温度、提纯结果。")


from unilabos.registry.placeholder_type import ResourceSlot


@workflow(display_name="资源演示：样品托管", compiler="ast", tags=["resource-custody"])
def sample_custody(ctx: WorkflowBuildContext, *, sample: ResourceSlot, vessel: str = "reactor") -> None:
    """设备绑定使用 reactor；样品和目标设备由调用方提供参数。"""
    ctx.run("reactor/load_sample", {"sample": sample, "vessel": vessel}, name="装载样品")
    ctx.run("reactor/process_sample", {"sample": sample}, name="原位处理")
    ctx.run("reactor/unload_sample", {"sample": sample, "vessel": vessel}, name="卸载样品")


@workflow(display_name="资源演示：空闲借还", compiler="ast", tags=["resource-loan"])
def idle_lending(ctx: WorkflowBuildContext) -> None:
    """两次连续设备占用；单步执行第一次后可显式借出空闲区间。"""
    ctx.run("reactor/check_quality", {}, name="借出前检测")
    ctx.run("reactor/check_quality", {}, name="归还后检测")


@workflow(
    display_name="联合演示：托管分装与补料",
    compiler="ast",
    tags=["inventory", "physical", "resource-scope"],
    guide=WorkflowGuide(
        preparation=["由 physical_smoke 初始化样品、来源库位、目标库位和库存。"],
        expected=[
            "抓取后样品托管在 reactor 夹爪；缺料时停在待派发，补料后两轮各实测 8 ul。",
            "样品放回目标库位；来源剩余 84 ul，目标收到 16 ul，库存按 16 ul 实际量结算。",
        ],
        notes=["整个循环由显式 reactor 作用域覆盖；设备 ID 绑定由前端实例化控制。接入真实设备时须替换虚拟回执。 "],
    ),
)
def physical_inventory(
    ctx: WorkflowBuildContext,
    *,
    sample: ResourceSlot,
    source: ResourceSlot,
    origin: ResourceSlot,
    destination: ResourceSlot,
    carrier: ResourceSlot,
    origin_site: str,
    target_site: str,
    gripper_site: str,
    lot: str,
) -> None:
    with ctx.resource_scope("reactor", name="抓取到放回"):
        ctx.run("reactor/reset", {}, name="复位")
        ctx.run(
            "reactor/pick",
            {"sample": sample, "owner": origin, "site": origin_site, "carrier": carrier, "gripper_site": gripper_site},
            name="抓取样品",
        )
        with ctx.loop_for(2, name="分装两轮"):
            selection = ctx.run("reactor/select_source", {"lot": lot}, name="动态选择来源")
            quality = ctx.run("reactor/check_quality", {}, name="选择条件")
            if quality.ready:
                ctx.run(
                    "reactor/aliquot",
                    {"source": source, "target": sample},
                    name="未选分支",
                    inventory=[
                        {
                            "key": "solvent",
                            "kind": "lot",
                            "lot_uuid": "unselected-no-stock",
                            "quantity": 1000,
                            "unit": "ul",
                        }
                    ],
                )
            else:
                ctx.run(
                    "reactor/aliquot",
                    {"source": source, "target": sample},
                    name="实际分装",
                    inventory_mode="on_ready",
                    inventory=[
                        {
                            "key": "solvent",
                            "kind": "lot",
                            "lot_uuid": selection.lot,
                            "quantity": selection.quantity,
                            "unit": "ul",
                        }
                    ],
                )
        ctx.run(
            "reactor/place",
            {"sample": sample, "owner": destination, "site": target_site, "carrier": carrier},
            name="放回样品",
        )

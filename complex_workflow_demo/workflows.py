"""复杂工作流演示的 @workflow 模板：循环容器（for / while）在真实调度器里逐轮执行。

host 启动时主仓 AST 扫描发现本模块，模板随注册表上报（``GET /api/v1/registry/workflow-templates``）；
网页"工作流模板"面板或 ``POST /api/v1/workflows/from-template`` 把它实例化成工作流，再
``POST /api/v1/workflow-tasks`` 运行。五条模板逐个引入一种控制流，最后一条把它们串成一个流程：

1. 「循环演示：定次加样」——``for`` 固定 3 轮，循环体参数用 ``{{loop.iteration}}`` 生成样品编号；
2. 「循环演示：等待升温」——``while`` 对**设备状态** ``reactor.temperature_c < 80`` 判定，循环体为空
   （每 0.5 s 判定一次，"等到某状态"），设备在后台升温；
3. 「循环演示：提纯达标」——``while`` 对**节点返回值**判定：循环体先提纯再取样检测，条件引用
   循环体里的检测步骤（首轮前没有产出 → 至少跑一轮，"重复直到达标"）；
4. 「循环演示：嵌套板孔」——两层 ``for``：外层切换板、内层给孔加样，各自的 ``{{loop.iteration}}``
   取最内层；
5. 「复杂工作流演示」——加样 ×2 → 后台升温并等到位 → 提纯直到达标 → 汇总。

循环体每轮是循环体节点的一个新 attempt（``trigger=loop_iteration``），运行页按节点看到当前轮
结果与全部轮次历史；循环节点自己显示"第 i/N 轮"。
"""

from unilabos.registry.workflows import WorkflowBuildContext, WorkflowGuide, workflow

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
            "「汇总报告」返回 additions = [\"S-1\", \"S-2\", \"S-3\"]。",
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
    description="复位 -> while 取样未达标（提纯 -> 取样检测）-> 汇总（预期两轮后纯度 0.9 达标）",
    tags=["complex-workflow", "loop-while", "node-output"],
    guide=WorkflowGuide(
        preparation=["「设备」页确认 reactor 在线；无需准备物料。"],
        expected=[
            "首轮前「取样检测」还没有结果，循环体先跑一轮：提纯到 0.65，检测 ready=false。",
            "第二轮提纯到 0.9，检测 ready=true → 条件不成立，循环结束；「提纯」「取样检测」各 2 个 attempt。",
            "「汇总报告」返回 polish_count = 2、purity = 0.9。",
        ],
        notes=["条件引用的是循环体里的检测步骤（按步骤名），这就是「重复直到达标」的写法。", _LOOP_NOTE],
    ),
)
def polish_until_ready(ctx: WorkflowBuildContext) -> None:
    """while + 节点返回值：do-while 语义的"重复直到达标"。"""

    _reset(ctx)
    with ctx.loop_while(
        ctx.step_output(QUALITY_STEP_NAME, "ready", "==", False),
        name="提纯直到达标",
        description="每轮先提纯再检测；检测 ready 为 false 就再来一轮。",
    ):
        ctx.run("reactor/polish", {}, name="提纯", description="纯度 +0.25。")
        ctx.run(
            "reactor/check_quality",
            {"threshold": PURITY_THRESHOLD},
            name=QUALITY_STEP_NAME,
            description="返回 purity 与 ready；循环条件读它的最新返回值。",
        )
    ctx.run("reactor/report", {}, name="汇总报告", description="polish_count 应为 2，purity 0.9。")


@workflow(
    display_name=NESTED_WORKFLOW_NAME,
    description="复位 -> for 板 ×2 { 切换到板 -> for 孔 ×3 { 孔位加样 } } -> 汇总（预期 6 次孔位加样）",
    tags=["complex-workflow", "loop-for", "nested"],
    guide=WorkflowGuide(
        preparation=["「设备」页确认 reactor 在线；无需准备物料。"],
        expected=[
            "外层两轮各切换一块板（plate = 1、2），内层每块板三个孔（well = 1、2、3）。",
            "「孔位加样」attempt_count = 6，「板孔 ×3」内层循环节点 attempt_count = 2（外层每轮重新执行一遍）。",
            "「汇总报告」返回 plates = {\"1\": [1,2,3], \"2\": [1,2,3]}、fill_count = 6。",
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
            "「汇总报告」返回 additions = [\"S-1\", \"S-2\"]、temperature_c = 80、polish_count = 2、purity = 0.9。",
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
        ctx.run("reactor/check_quality", {"threshold": PURITY_THRESHOLD}, name=QUALITY_STEP_NAME, description="ready 判定。")
    ctx.run("reactor/report", {}, name="汇总报告", description="一次读回加样、温度、提纯结果。")

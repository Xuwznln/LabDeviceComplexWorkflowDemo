"""网页工作流提交模式的有限时 smoke：运行五条循环流程及借还、托管场景。

网页对本机 Workflow Authority 的操作就是下面这几个 HTTP 调用，本脚本逐一复现：

1. ``GET  /api/v1/registry/workflow-templates``     找到 host 启动时随注册表上报的 @workflow 模板；
2. ``POST /api/v1/workflows/from-template``          把模板按角色绑定实例化成工作流（网页"插入模板 / 运行"）；
3. ``POST /api/v1/workflow-tasks``                   创建任务（网页"运行"按钮）；
4. ``GET  /api/v1/workflow-tasks/{uuid}`` / ``/node-runs``  断言任务终态、每个节点运行的 attempt 数
   （循环体节点每轮一个 attempt）、循环节点的 ``control_data.loop`` 轮次与「汇总报告」的返回值。

五条工作流全部预期 ``succeeded``；轮数由设备的确定性状态演化决定（见 reactor.py），因此可以精确断言。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import socket
import subprocess
import sys
import sysconfig
import tempfile
import time
from uuid import uuid4
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Sequence

# 新版 Uni-Lab-OS 的管理 API 写请求要求控制面 token。本脚本自己拉起运行时，所以由它决定 token：
# 经环境变量交给运行时，每个请求都放进 X-UniLab-Token；旧版运行时忽略这个变量和请求头。
CONTROL_TOKEN_ENV = "UNILABOS_BASICCONFIG_CONTROL_TOKEN"
CONTROL_TOKEN_HEADER = "X-UniLab-Token"
CONTROL_TOKEN = os.environ.get(CONTROL_TOKEN_ENV, "").strip() or secrets.token_urlsafe(24)

#: 与 complex_workflow_demo/workflows.py 保持一致（smoke 独立运行，不 import 设备包）。
FOR_WORKFLOW_NAME = "循环演示：定次加样"
WAIT_WORKFLOW_NAME = "循环演示：等待升温"
UNTIL_WORKFLOW_NAME = "循环演示：提纯达标"
NESTED_WORKFLOW_NAME = "循环演示：嵌套板孔"
COMPLEX_WORKFLOW_NAME = "复杂工作流演示"

TERMINAL = {"succeeded", "failed", "canceled"}


# ---------------------------------------------------------------------------
# 断言
# ---------------------------------------------------------------------------


def _by_name(proof: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {run["name"]: run for run in proof["node_runs"]}


def _assert_succeeded(proof: dict[str, Any], expected_names: list[str]) -> dict[str, dict[str, Any]]:
    assert proof["task_status"] == "succeeded", f"预期任务成功，实际: {proof}"
    names = [run["name"] for run in proof["node_runs"]]
    assert names == expected_names, f"节点运行顺序应为拓扑序 {expected_names}，实际 {names}"
    assert all(run["status"] == "succeeded" for run in proof["node_runs"]), proof["node_runs"]
    return _by_name(proof)


def _task_value(proof: dict[str, Any], field: str) -> dict[str, Any]:
    """任务入口参数 / 具名输出。本 smoke 的证据带 ``task_input`` / ``task_output``；Uni-Lab-OS
    README demo e2e 复用这些断言时，证据里是整个任务 ``proof["task"]``（``input`` / ``output``）。"""

    key = f"task_{field}"
    if key in proof:
        return proof[key]
    return dict(proof["task"].get(field) or {})


def _assert_loop_attempts(run: dict[str, Any], iterations: int) -> None:
    """循环体节点：每轮一个 attempt，首轮 initial、其后 loop_iteration，全部成功。"""

    assert run["attempt_count"] == iterations, run
    triggers = [attempt["trigger"] for attempt in run["attempts"]]
    assert triggers == ["initial"] + ["loop_iteration"] * (iterations - 1), triggers
    assert all(attempt["status"] == "succeeded" for attempt in run["attempts"]), run["attempts"]


def assert_for_workflow(proof: dict[str, Any]) -> None:
    """「定次加样」：for ×3，迭代变量进了参数，汇总里三份样品按序。"""

    assert proof["workflow_name"] == FOR_WORKFLOW_NAME
    runs = _assert_succeeded(proof, ["复位", "加样 ×3", "加样", "汇总报告"])
    loop = runs["加样 ×3"]
    assert loop["executor_kind"] == "loop" and loop["attempt_count"] == 1
    assert loop["return_info"]["return_value"]["iterations"] == 3, loop
    assert loop["control_data"]["loop"] == {
        "mode": "for",
        "count": 3,
        "max_iterations": None,
        "condition": None,
        "iteration": 2,
    }, loop
    _assert_loop_attempts(runs["加样"], 3)
    sample_ids = [attempt["return_info"]["return_value"]["sample_id"] for attempt in runs["加样"]["attempts"]]
    assert sample_ids == ["S-1", "S-2", "S-3"], sample_ids
    assert runs["汇总报告"]["return_info"]["return_value"]["additions"] == ["S-1", "S-2", "S-3"]


def assert_wait_workflow(proof: dict[str, Any]) -> None:
    """「等待升温」：空循环体按设备状态轮询，温度到 80 ℃ 结束。"""

    assert proof["workflow_name"] == WAIT_WORKFLOW_NAME
    runs = _assert_succeeded(proof, ["复位", "开始升温", "等待升温", "停止升温", "汇总报告"])
    loop = runs["等待升温"]
    assert loop["executor_kind"] == "loop" and loop["attempt_count"] == 1
    assert loop["return_info"]["return_value"]["iterations"] >= 1, loop
    assert loop["control_data"]["loop"]["condition"] == "reactor.temperature_c < 80.0", loop
    report = runs["汇总报告"]["return_info"]["return_value"]
    assert report["temperature_c"] == 80.0 and report["heating"] is False, report


def assert_until_workflow(proof: dict[str, Any]) -> None:
    """三轮真实检测覆盖 if/else，确认未选分支未派发且保存 skipped 历史。"""
    assert proof["workflow_name"] == UNTIL_WORKFLOW_NAME
    assert proof["task_status"] == "succeeded", proof
    runs = _by_name(proof)
    conditions = [run for run in proof["node_runs"] if run["executor_kind"] == "condition"]
    assert len(conditions) == 1 and len(runs) == 7, proof
    condition = conditions[0]
    assert [run["name"] for run in proof["node_runs"]] == [
        "复位",
        "检测 ×3",
        "取样检测",
        condition["name"],
        "达标确认",
        "未达标提纯",
        "汇总报告",
    ]
    assert all(
        run["status"] == ("skipped" if run["name"] == "未达标提纯" else "succeeded") for run in proof["node_runs"]
    ), proof
    assert runs["检测 ×3"]["return_info"]["return_value"]["iterations"] == 3
    _assert_loop_attempts(runs["取样检测"], 3)
    _assert_loop_attempts(condition, 3)
    for run, field in ((runs["取样检测"], "ready"), (condition, "matched")):
        assert [attempt["return_info"]["return_value"][field] for attempt in run["attempts"]] == [False, False, True]
    for name, statuses in (
        ("达标确认", ["skipped", "skipped", "succeeded"]),
        ("未达标提纯", ["succeeded", "succeeded", "skipped"]),
    ):
        run = runs[name]
        assert run["attempt_count"] == 3
        assert [attempt["status"] for attempt in run["attempts"]] == statuses
        assert [attempt["trigger"] for attempt in run["attempts"]] == ["initial", "loop_iteration", "loop_iteration"]
    report = runs["汇总报告"]["return_info"]["return_value"]
    assert report["polish_count"] == 2 and report["purity"] == 0.9, report
    assert _task_value(proof, "input") == {"threshold": 0.85}
    assert _task_value(proof, "output") == {"purity": 0.9, "polish_count": 2}


def assert_nested_workflow(proof: dict[str, Any]) -> None:
    """「嵌套板孔」：外层 2 轮 × 内层 3 轮；内层循环节点随外层每轮重新执行。"""

    assert proof["workflow_name"] == NESTED_WORKFLOW_NAME
    runs = _assert_succeeded(proof, ["复位", "板 ×2", "切换到板", "板孔 ×3", "孔位加样", "汇总报告"])
    assert runs["板 ×2"]["return_info"]["return_value"]["iterations"] == 2
    inner = runs["板孔 ×3"]
    assert inner["executor_kind"] == "loop"
    _assert_loop_attempts(inner, 2)
    _assert_loop_attempts(runs["切换到板"], 2)
    _assert_loop_attempts(runs["孔位加样"], 6)
    plates = [attempt["return_info"]["return_value"]["plate"] for attempt in runs["切换到板"]["attempts"]]
    assert plates == [1, 2], plates
    wells = [
        (attempt["return_info"]["return_value"]["plate"], attempt["return_info"]["return_value"]["well"])
        for attempt in runs["孔位加样"]["attempts"]
    ]
    assert wells == [(1, 1), (1, 2), (1, 3), (2, 1), (2, 2), (2, 3)], wells
    report = runs["汇总报告"]["return_info"]["return_value"]
    assert report["plates"] == {"1": [1, 2, 3], "2": [1, 2, 3]} and report["fill_count"] == 6, report


def assert_complex_workflow(proof: dict[str, Any]) -> None:
    """「复杂工作流演示」：三种循环串成一条流程，汇总同时体现三段结果。"""

    assert proof["workflow_name"] == COMPLEX_WORKFLOW_NAME
    runs = _assert_succeeded(
        proof,
        ["复位", "加样 ×2", "加样", "开始升温", "等待升温", "停止升温", "提纯直到达标", "提纯", "取样检测", "汇总报告"],
    )
    _assert_loop_attempts(runs["加样"], 2)
    assert runs["等待升温"]["return_info"]["return_value"]["iterations"] >= 1
    _assert_loop_attempts(runs["提纯"], 2)
    _assert_loop_attempts(runs["取样检测"], 2)
    report = runs["汇总报告"]["return_info"]["return_value"]
    assert report["additions"] == ["S-1", "S-2"], report
    assert report["temperature_c"] == 80.0, report
    assert report["polish_count"] == 2 and report["purity"] == 0.9, report


# ---------------------------------------------------------------------------
# 进程与 HTTP
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def _stop(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _graph_path(repo_root: Path) -> Path:
    """优先读取 wheel 安装的数据文件，editable/source 模式回退到仓库 graph。"""

    installed = (
        Path(sysconfig.get_path("data")) / "share" / "complex_workflow_demo" / "graph" / "complex_workflow_demo.json"
    )
    if installed.is_file():
        return installed
    source = repo_root / "graph" / "complex_workflow_demo.json"
    if source.is_file():
        return source
    raise FileNotFoundError("complex workflow demo graph 未随 distribution 安装")


def _base_command(repo_root: Path, database_root: Path, management_port: int, backend: str) -> list[str]:
    import unilabos

    config_path = Path(unilabos.__file__).resolve().parent / "config" / "example_config.py"
    command = [
        sys.executable,
        "-m",
        "unilabos",
        "--backend",
        backend,
        "--skip_env_check",
        "--devices",
        str(repo_root / "complex_workflow_demo"),
        "--external_devices_only",
        "--visual",
        "disable",
        "--disable_browser",
        "--port",
        str(management_port),
        "--server_database_root",
        str(database_root),
        "--working_dir",
        str(database_root / "work"),
        "--config",
        str(config_path),
        "-g",
        str(_graph_path(repo_root)),
    ]
    return command


#: 统一 Core 的业务码：Host 子进程尚未接入控制面（启动 / 安静点重启）时，Host 专有路由
#: （如 ``/runtime/endpoints``）返回 HTTP 200 + ``{"code": 5001}``，就绪等待应继续轮询。
SERVICE_UNAVAILABLE = 5001


class ManagementAPIError(RuntimeError):
    """管理 API 返回了非 0 业务码（V1 信封 ``{"code", "error": {"msg"}}``）。"""

    def __init__(self, path: str, body: dict[str, Any]) -> None:
        super().__init__(f"管理 API {path} 返回错误: {body}")
        self.path = path
        self.code = body.get("code")
        self.body = body


def _api_request(
    port: int, path: str, payload: dict[str, Any] | None = None, *, method: str | None = None, raw: bool = False
) -> Any:
    """请求管理 API；V1 信封 {"code":0,"data":...} 自动解包，非信封路由原样返回。

    ``raw=True`` 不解包：``POST /inventory/commands`` 按 V1 返回裸结果
    ``{command_id, status, result | error, ...}``，被拒时另带 ``code``，由调用方按 ``status`` 判断。
    """

    url = f"http://127.0.0.1:{port}/api/v1{path}"
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    # GET 不能带 JSON Content-Type：服务端 Backend 路由会尝试解码空 body 而报错
    headers = {} if payload is None else {"Content-Type": "application/json"}
    headers[CONTROL_TOKEN_HEADER] = CONTROL_TOKEN
    request = urllib.request.Request(
        url, data=data, headers=headers, method=method or ("GET" if payload is None else "POST")
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"管理 API {path} 返回 {error.code}: {error.read().decode('utf-8')}") from error
    if raw:
        return body
    if isinstance(body, dict) and "code" in body:
        if body["code"] != 0:
            raise ManagementAPIError(path, body)
        return body.get("data")
    return body


def _wait_management_api(port: int, process: subprocess.Popen[Any], deadline: float) -> None:
    """HTTP 存活不代表 Host 已上报能力；等执行端点包含本演示的动作再导入模板。"""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("runtime process exited before the management API came up")
        try:
            health = _api_request(port, "/health")
            endpoints = _api_request(port, "/runtime/endpoints?state=online&limit=100")
            actions = {
                capability["action_name"]
                for endpoint in endpoints
                for capability in endpoint.get("action_capabilities", [])
                if capability.get("state", "active") == "active"
            }
            if (
                health.get("status") == "ok"
                and health.get("execution") == "ready"
                and {"reset", "add_reagent", "start_heating", "report"} <= actions
            ):
                return
        except ManagementAPIError as error:
            # 默认拓扑里 Host 是调度权威的子进程，接入控制面前 Host 专有路由返回 5001
            if error.code != SERVICE_UNAVAILABLE:
                raise
        except RuntimeError as error:
            if not isinstance(error.__cause__, urllib.error.HTTPError) or error.__cause__.code != 503:
                raise
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.3)
    raise RuntimeError("管理 API / Host 执行面 / 设备动作能力未在时限内就绪")


def _find_template(port: int, name: str, deadline: float) -> dict[str, Any]:
    while time.monotonic() < deadline:
        listing = _api_request(port, "/registry/workflow-templates")
        matches = [item for item in listing["templates"] if item["display_name"] == name]
        assert len(matches) <= 1, f"工作流模板显示名重复: {name!r}"
        if matches:
            return matches[0]
        time.sleep(0.3)
    raise RuntimeError(f"未在注册表检索到工作流模板 {name!r}")


def run_workflow(port: int, name: str, deadline: float) -> dict[str, Any]:
    """检索模板 -> 实例化 -> 创建任务 -> 等待终态 -> 汇总节点运行（含 attempt 历史与循环进度）。"""

    template = _find_template(port, name, deadline)
    instantiated = _api_request(port, "/workflows/from-template", {"template_uuid": template["uuid"], "bindings": {}})
    workflow = instantiated["workflow"]
    task = _api_request(port, "/workflow-tasks", {"workflow_uuid": workflow["uuid"], "run_mode": "normal"})
    return finish_workflow(port, name, workflow, task, deadline)


def finish_workflow(port, name, workflow, task, deadline):
    task_uuid = task["uuid"]

    status = str(task.get("status") or "")
    while time.monotonic() < deadline and status not in TERMINAL:
        time.sleep(0.3)
        status = str(_api_request(port, f"/workflow-tasks/{task_uuid}").get("status") or "")
    if status not in TERMINAL:
        raise RuntimeError(f"工作流任务 {task_uuid} 未在时限内结束: {status}")

    final = _api_request(port, f"/workflow-tasks/{task_uuid}")
    node_names = {node["uuid"]: node["name"] for node in final["workflow_snapshot"]["nodes"]}
    node_runs = _api_request(port, f"/workflow-tasks/{task_uuid}/node-runs")
    return {
        "workflow_uuid": workflow["uuid"],
        "workflow_name": name,
        "task_uuid": task_uuid,
        "task_status": status,
        "task_input": dict(final.get("input") or {}),
        "task_output": dict(final.get("output") or {}),
        "task_error": list(final.get("error_info") or []),
        # 节点结果一律取节点运行（当前 attempt 的投影）；attempts 是该节点的执行历史（循环每轮一个）
        "node_runs": [
            {
                "uuid": run["uuid"],
                "name": node_names.get(run["workflow_node_uuid"], run["workflow_node_uuid"]),
                "executor_kind": run["executor_kind"],
                "status": run["status"],
                "attempt_count": int(run.get("attempt_count") or 0),
                "return_info": dict(run.get("return_info") or {}),
                "control_data": dict(run.get("control_data") or {}),
                "error_info": list(run.get("error_info") or []),
                "attempts": [
                    {
                        "uuid": attempt["uuid"],
                        "attempt_no": int(attempt["attempt_no"]),
                        "trigger": attempt.get("trigger", ""),
                        "status": attempt["status"],
                        "return_info": dict(attempt.get("return_info") or {}),
                    }
                    for attempt in run.get("attempts", [])
                ],
            }
            for run in node_runs
        ],
    }


def run_resource_scenarios(port, deadline):
    def create(name, mode="step", values=None):
        template = _find_template(port, name, deadline)
        workflow = _api_request(port, "/workflows/from-template", {"template_uuid": template["uuid"], "bindings": {}})[
            "workflow"
        ]
        task = _api_request(
            port, "/workflow-tasks", {"workflow_uuid": workflow["uuid"], "run_mode": mode, "input": values or {}}
        )
        return workflow, task

    def control(task, kind):
        current = _api_request(port, f"/workflow-tasks/{task['uuid']}")
        if kind == "pause" and current["control_status"] == "paused":
            return current
        return _api_request(
            port,
            f"/workflow-tasks/{task['uuid']}/commands",
            {"type": kind, "expected_revision": current["control_revision"], "idempotency_key": str(uuid4())},
        )

    def wait_steps(task, count):
        while time.monotonic() < deadline:
            runs = _api_request(port, f"/workflow-tasks/{task['uuid']}/node-runs")
            if sum(run["status"] == "succeeded" for run in runs) == count:
                return runs
            if any(run["status"] in {"failed", "intervention_required"} for run in runs):
                raise AssertionError(runs)
            time.sleep(0.1)
        raise TimeoutError("资源演示动作未完成")

    def tenancies():
        """读设备托管。托管转换与动作终态在同一事务提交：节点一落 succeeded，托管即已是新状态，不需要轮询。"""

        return _api_request(port, "/workflow-device-tenancies")

    proof = {}
    name = "资源演示：空闲借还"
    lw, lender = create(name)
    control(lender, "step")
    wait_steps(lender, 1)
    control(lender, "pause")
    bw, borrower = create(name)
    owner = _api_request(port, f"/workflow-resource-intervals?task_uuid={lender['uuid']}")[0]
    loan = _api_request(
        port,
        f"/workflow-resource-intervals/{owner['owner_uuid']}/lend",
        {
            "loan_uuid": str(uuid4()),
            "borrower_task_uuid": borrower["uuid"],
            "expected_version": owner["version"],
            "reason": "已暂停原任务并确认设备空闲",
        },
    )
    control(lender, "resume")
    control(borrower, "step")
    wait_steps(borrower, 1)
    assert (
        len(
            [
                run
                for run in _api_request(port, f"/workflow-tasks/{lender['uuid']}/node-runs")
                if run["status"] == "succeeded"
            ]
        )
        == 1
    )
    assert _api_request(port, "/workflow-resource-loans")[0]["loan_uuid"] == loan["loan_uuid"]
    control(borrower, "resume")
    proof["borrower_workflow"] = finish_workflow(port, name, bw, borrower, deadline)
    proof["lender_workflow"] = finish_workflow(port, name, lw, lender, deadline)
    assert _api_request(port, "/workflow-resource-loans") == []

    name = "资源演示：样品托管"
    sample = _api_request(port, "/materials/instances/by-resource-id/sample")["material"]["material_uuid"]
    cw, custody = create(name, values={"sample": {"uuid": sample}, "vessel": "reactor"})
    control(custody, "step")
    wait_steps(custody, 1)
    held = tenancies()
    assert (
        len(held) == 1
        and held[0]["device_key"] == "/devices/reactor"
        and held[0]["material_uuid"] == sample
        and held[0]["state"] == "loaded"
    ), held
    ow, other = create("资源演示：空闲借还", mode="normal")
    control(custody, "step")
    wait_steps(custody, 2)
    assert all(run["status"] == "pending" for run in _api_request(port, f"/workflow-tasks/{other['uuid']}/node-runs"))
    control(custody, "step")
    proof["custody_workflow"] = finish_workflow(port, name, cw, custody, deadline)
    proof["custody_competitor"] = finish_workflow(port, "资源演示：空闲借还", ow, other, deadline)
    assert tenancies() == []
    assert all(value["task_status"] == "succeeded" for value in proof.values()), proof
    return proof


def _execution_start_proof(database_root: Path, proofs: dict[str, Any]) -> dict[str, Any]:
    """对照两进程的持久事实，确认每次设备执行都取得唯一开始许可和回执。"""
    expected = {
        attempt["uuid"]
        for proof in proofs.values()
        for run in proof["node_runs"]
        if run["executor_kind"] not in {"loop", "condition"}
        for attempt in run["attempts"]
        if attempt["status"] == "succeeded"
    }
    authorities = []
    hosts = []
    for path in database_root.rglob("runtime.db"):
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT job_uuid,authority_epoch,authorized_at,receipt FROM workflow_execution_dispatch"
            ).fetchall()
            if rows:
                authorities.append((path, rows))
            jobs = connection.execute("SELECT job_uuid,status FROM execution_job").fetchall()
            if jobs:
                hosts.append((path, jobs))
    assert len(authorities) == len(hosts) == 1, (authorities, hosts)
    authority_path, rows = authorities[0]
    host_path, jobs = hosts[0]
    assert authority_path != host_path, "必须经过默认 authority + Host 双进程链路"
    assert {row["job_uuid"] for row in rows} == expected
    assert {row["job_uuid"] for row in jobs if row["status"] == "succeeded"} == expected
    records = []
    for row in rows:
        assert row["authorized_at"] and row["receipt"], dict(row)
        receipt = json.loads(row["receipt"])
        assert receipt["permit"]["authority_epoch"] == row["authority_epoch"]
        records.append(
            {
                "job_uuid": row["job_uuid"],
                "authorized_at": row["authorized_at"],
                "receipt_uuid": receipt["receipt_uuid"],
                "started_at_ms": receipt["started_at_ms"],
            }
        )
    with sqlite3.connect(f"file:{authority_path.as_posix()}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        admissions = connection.execute("SELECT job_uuid,state FROM workflow_resource_admission").fetchall()
        assert {row["job_uuid"] for row in admissions} == expected
        assert {row["state"] for row in admissions} == {"released"}
        assert connection.execute("SELECT count(*) FROM workflow_resource_use").fetchone()[0] == 0
        intervals = connection.execute(
            "SELECT owner_uuid,task_uuid,lock_key,state,definition FROM workflow_resource_interval"
        ).fetchall()
        assert intervals and {row["state"] for row in intervals} == {"released"}, [
            {
                **dict(row),
                "runs": [
                    dict(run)
                    for run in connection.execute(
                        "SELECT workflow_node_uuid,status,control_data,current_job_uuid FROM workflow_node_run WHERE workflow_task_uuid=?",
                        (row["task_uuid"],),
                    )
                ],
            }
            for row in intervals
            if row["state"] != "released"
        ]
        loans = [dict(row) for row in connection.execute("SELECT * FROM workflow_resource_loan")]
        tenancies = [dict(row) for row in connection.execute("SELECT * FROM workflow_device_tenancy")]
        assert loans and all(row["state"] == "returned" for row in loans), loans
        assert tenancies and all(row["state"] == "released" for row in tenancies), tenancies
        interval_proof = [{**dict(row), "definition": json.loads(row["definition"])} for row in intervals]
    return {
        "separate_host_database": True,
        "device_attempt_count": len(expected),
        "attempts": records,
        "resource_admission_count": len(admissions),
        "active_resource_uses": 0,
        "resource_intervals": interval_proof,
        "loans": loans,
        "tenancies": tenancies,
    }


def run_smoke(backend: str = "hostlink", timeout: float = 60.0) -> dict[str, Any]:
    """启动真实图，经管理 API 运行五条循环流程和四次资源场景任务，返回证据。"""

    if backend not in {"hostlink", "ros2"}:
        raise ValueError("backend must be hostlink or ros2")
    repo_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix=f"complex-workflow-demo-{backend}-") as directory:
        root = Path(directory)
        log_path = root / "runtime.log"
        environment = os.environ.copy()
        environment[CONTROL_TOKEN_ENV] = CONTROL_TOKEN
        environment["PYTHONUNBUFFERED"] = "1"
        management_port = _free_port()
        command = _base_command(repo_root, root / "db", management_port, backend)
        # ROS2 也保留 HostLink 的能力登记与物料管理通道。
        command += ["--hostlink_bind", "127.0.0.1", "--hostlink_port", str(_free_port())]
        if backend == "ros2":
            domain_id = str(10 + management_port % 190)
            environment["ROS_DOMAIN_ID"] = domain_id
            command += ["--ros_domain_id", domain_id]

        with log_path.open("w", encoding="utf-8") as output:
            process = subprocess.Popen(
                command,
                cwd=repo_root,
                env=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                deadline = time.monotonic() + timeout
                _wait_management_api(management_port, process, deadline)
                proofs = {}
                for key, name, check in (
                    ("for_workflow", FOR_WORKFLOW_NAME, assert_for_workflow),
                    ("wait_workflow", WAIT_WORKFLOW_NAME, assert_wait_workflow),
                    ("until_workflow", UNTIL_WORKFLOW_NAME, assert_until_workflow),
                    ("nested_workflow", NESTED_WORKFLOW_NAME, assert_nested_workflow),
                    ("complex_workflow", COMPLEX_WORKFLOW_NAME, assert_complex_workflow),
                ):
                    proof = run_workflow(management_port, name, deadline)
                    check(proof)
                    proofs[key] = proof
                proofs.update(run_resource_scenarios(management_port, deadline))
                from .physical_smoke import run_physical_scenario

                proofs["physical_workflow"] = run_physical_scenario(management_port, deadline)
                starts = _execution_start_proof(root / "db", proofs)
                return {"success": True, "backend": backend, "execution_starts": starts, **proofs}
            except Exception:
                sys.stderr.write("SMOKE FAILED\n" + log_path.read_text(encoding="utf-8", errors="replace") + "\n")
                raise
            finally:
                _stop(process)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("hostlink", "ros2"), default="hostlink")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)
    print(json.dumps(run_smoke(args.backend, args.timeout), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    # ``python -m`` 把本文件作为 __main__ 执行，而 physical_smoke 导入的是 complex_workflow_demo.smoke：
    # 两份模块各自生成 CONTROL_TOKEN，物料命令会带着运行时不认识的 token（401）。统一走包内模块。
    from complex_workflow_demo.smoke import main as _package_main

    raise SystemExit(_package_main())

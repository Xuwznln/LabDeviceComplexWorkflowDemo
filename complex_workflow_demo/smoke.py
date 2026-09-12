"""网页工作流提交模式的有限时 smoke：起真实运行时，把五条循环模板实例化成工作流并逐条运行。

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
import socket
import subprocess
import sys
import sysconfig
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Sequence

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
    assert loop["control_data"]["loop"] == {"mode": "for", "count": 3, "max_iterations": None, "condition": None, "iteration": 2}, loop
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
    """「提纯达标」：条件引用循环体里的检测步骤，首轮无产出先跑一轮，两轮后达标。"""

    assert proof["workflow_name"] == UNTIL_WORKFLOW_NAME
    runs = _assert_succeeded(proof, ["复位", "提纯直到达标", "提纯", "取样检测", "汇总报告"])
    assert runs["提纯直到达标"]["return_info"]["return_value"]["iterations"] == 2
    _assert_loop_attempts(runs["提纯"], 2)
    _assert_loop_attempts(runs["取样检测"], 2)
    readiness = [attempt["return_info"]["return_value"]["ready"] for attempt in runs["取样检测"]["attempts"]]
    assert readiness == [False, True], readiness
    report = runs["汇总报告"]["return_info"]["return_value"]
    assert report["polish_count"] == 2 and report["purity"] == 0.9, report


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
    if backend == "ros2":
        command.append("--disable_hostlink")
    return command


def _api_request(port: int, path: str, payload: dict[str, Any] | None = None) -> Any:
    """请求管理 API；workflow 风格 {"code":0,"data":...} 自动解包，诊断路由原样返回。"""

    url = f"http://127.0.0.1:{port}/api/v1{path}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    # GET 不能带 JSON Content-Type：服务端 Backend 路由会尝试解码空 body 而报错
    headers = {} if payload is None else {"Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, headers=headers, method="GET" if payload is None else "POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        body = json.loads(response.read().decode("utf-8"))
    if isinstance(body, dict) and "code" in body:
        if body["code"] != 0:
            raise RuntimeError(f"管理 API {path} 返回错误: {body}")
        return body.get("data")
    return body


def _wait_management_api(port: int, process: subprocess.Popen[Any], deadline: float) -> None:
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("runtime process exited before the management API came up")
        try:
            if _api_request(port, "/health").get("status") == "ok":
                return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.3)
    raise RuntimeError("管理 API 未在时限内就绪")


def _find_template(port: int, name: str, deadline: float) -> dict[str, Any]:
    while time.monotonic() < deadline:
        listing = _api_request(port, "/registry/workflow-templates")
        matches = [item for item in listing["templates"] if item["display_name"] == name]
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


def run_smoke(backend: str = "hostlink", timeout: float = 60.0) -> dict[str, Any]:
    """启动真实图，经管理 API 依次运行五条循环工作流，返回可机读证据。"""

    if backend not in {"hostlink", "ros2"}:
        raise ValueError("backend must be hostlink or ros2")
    repo_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix=f"complex-workflow-demo-{backend}-") as directory:
        root = Path(directory)
        log_path = root / "runtime.log"
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        management_port = _free_port()
        command = _base_command(repo_root, root / "db", management_port, backend)
        if backend == "hostlink":
            command += ["--hostlink_bind", "127.0.0.1", "--hostlink_port", str(_free_port())]
        else:
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
                return {"success": True, "backend": backend, **proofs}
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
    raise SystemExit(main())

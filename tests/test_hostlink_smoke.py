from __future__ import annotations

from complex_workflow_demo.smoke import (
    assert_complex_workflow,
    assert_for_workflow,
    assert_nested_workflow,
    assert_until_workflow,
    assert_wait_workflow,
    run_smoke,
)


def test_real_complex_workflow_hostlink_smoke() -> None:
    proof = run_smoke("hostlink", timeout=120.0)
    assert proof["execution_starts"]["separate_host_database"]
    assert proof["execution_starts"]["device_attempt_count"] > 0
    assert proof["execution_starts"]["resource_admission_count"] == proof["execution_starts"]["device_attempt_count"]
    assert proof["execution_starts"]["active_resource_uses"] == 0
    assert {row["state"] for row in proof["execution_starts"]["resource_intervals"]} == {"released"}
    # for：固定轮数 + {{loop.iteration}} 进参数
    assert_for_workflow(proof["for_workflow"])
    # while + 设备状态：空循环体轮询直到温度到位
    assert_wait_workflow(proof["wait_workflow"])
    # 静态 if/else + 节点返回值：三轮覆盖两条分支与 skipped 历史
    assert_until_workflow(proof["until_workflow"])
    # 两层 for：内层循环节点随外层每轮重新执行
    assert_nested_workflow(proof["nested_workflow"])
    # 三种循环串成一条流程
    assert_complex_workflow(proof["complex_workflow"])

    assert len(proof["execution_starts"]["loans"]) == 1
    assert {row["state"] for row in proof["execution_starts"]["loans"]} == {"returned"}
    assert len(proof["execution_starts"]["tenancies"]) == 2
    assert {row["state"] for row in proof["execution_starts"]["tenancies"]} == {"released"}
    assert all(proof[key]["task_status"] == "succeeded" for key in
               ("lender_workflow", "borrower_workflow", "custody_workflow", "custody_competitor"))

    assert proof["physical_workflow"]["physical_settlement"]["target_actual_ul"] == 16

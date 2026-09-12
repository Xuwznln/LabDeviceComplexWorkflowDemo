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
    proof = run_smoke("hostlink", timeout=90.0)
    # for：固定轮数 + {{loop.iteration}} 进参数
    assert_for_workflow(proof["for_workflow"])
    # while + 设备状态：空循环体轮询直到温度到位
    assert_wait_workflow(proof["wait_workflow"])
    # while + 节点返回值：重复直到检测达标
    assert_until_workflow(proof["until_workflow"])
    # 两层 for：内层循环节点随外层每轮重新执行
    assert_nested_workflow(proof["nested_workflow"])
    # 三种循环串成一条流程
    assert_complex_workflow(proof["complex_workflow"])

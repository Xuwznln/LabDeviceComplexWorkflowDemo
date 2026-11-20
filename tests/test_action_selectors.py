"""参数类型决定前端选择器与物料锁：库位参数是 SiteSlot，设备参数是 DeviceSlot。

注册表按类型推出 placeholder_keys：ResourceSlot → 物料选择器（并加物料锁），SiteSlot → 库位选择器，
DeviceSlot → 设备选择器。库位 / 设备参数写成 str 时编排画布只给文本框。
"""

import time
from pathlib import Path

import pytest

import complex_workflow_demo
from complex_workflow_demo import physical_smoke

PACKAGE_DIR = Path(complex_workflow_demo.__file__).resolve().parent


def _reactor_actions():
    from unilabos.registry.registry import Registry

    registry = Registry()
    registry.setup(devices_dirs=[str(PACKAGE_DIR)], complete_registry=True, external_only=True)
    return registry.device_type_registry["virtual_reactor_demo"]["class"]["action_value_mappings"]


def test_site_and_device_parameters_get_frontend_selectors():
    actions = _reactor_actions()
    resource, site, device = "unilabos_resources", "unilabos_sites", "unilabos_devices"
    expected = {
        "pick": {"sample": resource, "owner": resource, "site": site, "carrier": resource, "gripper_site": site},
        "place": {"sample": resource, "owner": resource, "site": site, "carrier": resource},
        "load_sample": {"sample": resource, "vessel": device},
        "unload_sample": {"sample": resource, "vessel": device},
    }
    for name, selectors in expected.items():
        action = actions[name]
        assert action["placeholder_keys"] == selectors, (name, action["placeholder_keys"])
        goal = action["schema"]["properties"]["goal"]["properties"]
        for parameter, selector in selectors.items():
            if selector != resource:
                # 库位 UUID / 设备 ID 都是字符串：值的形态不变，只换前端选择器
                assert goal[parameter]["type"] == "string", (name, parameter, goal[parameter])
        # 物料锁只落在物料参数上，库位与设备参数不加锁
        locked = sorted(key for key, value in selectors.items() if value == resource)
        assert sorted(action["materials_need_lock"]) == locked, (name, action["materials_need_lock"])


def test_physical_template_import_keeps_site_inputs_for_the_task(monkeypatch):
    """库位参数绑定到工作流输入：导入按 preserve 保存，真实 site_uuid 随任务 input 提交。"""

    calls = []

    class Submitted(Exception):
        pass

    def api(port, path, payload=None, **_kwargs):
        calls.append((path, payload))
        if path == "/workflows/from-template":
            return {"workflow": {"uuid": "workflow"}}
        raise Submitted(path)

    values = {"origin_site": "origin-site-uuid", "target_site": "target-site-uuid", "gripper_site": "gripper-site-uuid"}
    monkeypatch.setattr(physical_smoke, "seed", lambda port: (None, values, None))
    monkeypatch.setattr(physical_smoke, "_find_template", lambda port, name, deadline: {"uuid": "template"})
    monkeypatch.setattr(physical_smoke, "_api_request", api)
    with pytest.raises(Submitted, match="/workflow-tasks"):
        physical_smoke.run_physical_scenario(8002, time.monotonic() + 2)
    assert calls[0] == (
        "/workflows/from-template",
        {"template_uuid": "template", "bindings": {}, "site_binding_mode": "preserve"},
    )
    assert calls[1][0] == "/workflow-tasks" and calls[1][1]["input"] == values

"""真实双进程 HostLink：跨循环区间、托管、条件补料与实际结算联合验证。"""

from __future__ import annotations

import time
from uuid import uuid4
from .smoke import _api_request, _find_template, finish_workflow

NAME = "联合演示：托管分装与补料"


def seed(port):
    from unilabos.client.materials import HTTPMaterialsClient
    from unilabos.protocol.materials import (
        InventoryMutation,
        InventoryLotInbound,
        ResourceTemplateWrite,
        MaterialTreeCreate,
        MaterialNodeCreate,
        MaterialIdentityWrite,
        MaterialDataWrite,
        MaterialPosition,
        MaterialSubstance,
        SiteCreate,
    )

    class Client(HTTPMaterialsClient):
        """经 smoke 的 _api_request 发请求，带上本次运行时的控制面 token。

        统一 Core 的客户端写入一律走 ``POST /inventory/commands``（``root=True``、裸结果
        ``envelope=False``），读走 ``/inventory/*``；只有 dev 独有的读路由仍在 ``/materials`` 下。
        """

        def _request(self, method, path, body=None, *, root=False, envelope=True):
            return _api_request(port, path if root else "/materials" + path, body, method=method, raw=not envelope)

    client = Client(f"http://127.0.0.1:{port}")

    def mutation(operation):
        return InventoryMutation(
            command_uuid=str(uuid4()), effect_key=operation, operation=operation, actor_type="user"
        )

    template = str(uuid4())
    template_name = "physical-demo-" + template
    client.put_template(
        mutation("put_template"),
        ResourceTemplateWrite(
            template_uuid=template,
            name=template_name,
            display_name="实物结算演示容器",
            resource_type="container",
            class_name="Resource",
        ),
    )

    def node(name, *, parent=None, sites=(), amount=0):
        return MaterialNodeCreate(
            client_ref=name,
            parent_client_ref=parent,
            identity=MaterialIdentityWrite(
                resource_id="demo-" + str(uuid4()),
                name=name + "-" + template[:8],
                template_name=template_name,
                resource_type="container",
                class_name="Resource",
                config={"type": "Resource", "capacity": {"max_volume_ul": 1000}},
            ),
            position=MaterialPosition(
                position_x=0, position_y=0, position_z=0, size_width=10, size_height=10, size_depth=10
            ),
            sites=list(sites),
            data=MaterialDataWrite(
                substances=[MaterialSubstance(name="water", quantity=amount, quantity_unit="ul")] if amount else []
            ),
        )

    original = client.create_tree(
        mutation("create_material_tree"),
        MaterialTreeCreate(
            nodes=[
                node(
                    "origin",
                    sites=[
                        SiteCreate(template_name=template_name, site_index=0, label="A", occupied_client_ref="sample")
                    ],
                ),
                node("sample", parent="origin"),
            ]
        ),
    ).data
    destination = client.create_tree(
        mutation("create_material_tree"),
        MaterialTreeCreate(
            nodes=[node("destination", sites=[SiteCreate(template_name=template_name, site_index=0, label="B")])]
        ),
    ).data
    source = client.create_tree(
        mutation("create_material_tree"), MaterialTreeCreate(nodes=[node("source", amount=100)])
    ).data
    reactor = client.get_material_by_resource_id("reactor")
    carrier = next(
        item
        for item in client.get_tree(reactor.material.material_uuid).nodes
        if item.material.resource_id == "reactor/gripper"
    )
    lot = str(uuid4())

    def replenish(quantity):
        return client.inbound_inventory_lot(
            mutation("inbound_inventory_lot"),
            InventoryLotInbound(lot_uuid=lot, template_uuid=template, batch_no=lot, unit="ul", quantity=quantity),
        )

    replenish(7)
    values = {
        "sample": {"uuid": original.client_ref_map["sample"]},
        "source": {"uuid": source.root_material_uuid},
        "origin": {"uuid": original.root_material_uuid},
        "destination": {"uuid": destination.root_material_uuid},
        "carrier": {"uuid": carrier.material.material_uuid},
        "origin_site": client.get_material(original.root_material_uuid).sites[0].site_uuid,
        "target_site": client.get_material(destination.root_material_uuid).sites[0].site_uuid,
        "gripper_site": carrier.sites[0].site_uuid,
        "lot": lot,
    }
    return client, values, replenish


def run_physical_scenario(port, deadline):
    client, values, replenish = seed(port)
    template = _find_template(port, NAME, deadline)
    workflow = _api_request(port, "/workflows/from-template", {"template_uuid": template["uuid"], "bindings": {}})[
        "workflow"
    ]
    task = _api_request(
        port, "/workflow-tasks", {"workflow_uuid": workflow["uuid"], "run_mode": "normal", "input": values}
    )
    while time.monotonic() < deadline:
        report = _api_request(port, "/scheduler/inventory-recovery")
        if any(row["task_uuid"] == task["uuid"] for row in report.get("waiting", [])):
            break
        decisions = _api_request(port, "/error-decisions")["decisions"]
        assert not decisions, decisions
        current = _api_request(port, f"/workflow-tasks/{task['uuid']}")
        assert current["status"] not in {"failed", "canceled"}, current
        time.sleep(0.2)
    else:
        raise TimeoutError("没有进入预期的等待补料")
    sample = values["sample"]["uuid"]
    assert client.get_material(values["carrier"]["uuid"]).sites[0].occupied_material_uuid == sample
    held = _api_request(port, "/workflow-device-tenancies")
    assert any(row["task_uuid"] == task["uuid"] and row["state"] == "loaded" for row in held), held
    assert not client.list_inventory_reservations(task_uuid=task["uuid"])
    replenish(20)
    proof = finish_workflow(port, NAME, workflow, task, deadline)
    assert proof["task_status"] == "succeeded", proof
    assert client.get_material(values["origin"]["uuid"]).sites[0].occupied_material_uuid is None
    assert client.get_material(values["carrier"]["uuid"]).sites[0].occupied_material_uuid is None
    assert client.get_material(values["destination"]["uuid"]).sites[0].occupied_material_uuid == sample
    assert client.get_material(values["source"]["uuid"]).data.substances[0].quantity == 84
    assert client.get_material(sample).data.substances[0].quantity == 16
    lot = client.get_inventory_lot(values["lot"])
    assert (lot.quantity_total, lot.quantity_available, lot.quantity_reserved) == (11, 11, 0), lot
    assert not _api_request(port, "/workflow-device-tenancies")
    assert not _api_request(port, "/scheduler/physical-recovery")
    reservations = client.list_inventory_reservations(task_uuid=task["uuid"])
    assert len(reservations) == 2 and all(row.status == "consumed" for row in reservations), reservations
    proof["physical_settlement"] = {
        "source_remaining_ul": 84,
        "target_actual_ul": 16,
        "inventory_remaining_ul": 11,
        "conditional_reservations": 2,
        "final_site_uuid": values["target_site"],
        "active_holds": 0,
    }
    return proof

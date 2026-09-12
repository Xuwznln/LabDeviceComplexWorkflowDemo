"""虚拟反应釜 — 给工作流循环容器提供可判定、可复现的设备状态与动作返回值。

这台设备没有任何自跑闭环，每个能力都是一个可被工作流循环体反复调用的动作：

- ``add_reagent``：按样品编号加样并累计（``for`` 循环用 ``{{loop.iteration}}`` 生成编号）；
- ``start_heating`` / ``stop_heating``：后台线程按固定速率升温到目标，``temperature_c`` 作为
  状态字段周期上报——``while`` 循环对**设备状态**判定"温度到位"（空循环体 = 等到某状态）；
- ``polish`` / ``check_quality``：每次提纯纯度 +0.25，检测返回 ``ready``——``while`` 循环对
  **节点返回值**判定"重复直到达标"；
- ``start_plate`` / ``fill_well``：板 × 孔两层嵌套循环，设备记住当前板；
- ``report``：一次读回全部累计结果，供 smoke / e2e 断言。

数值都是确定的（升温到目标即停、纯度按固定步长增长），所以每条工作流的轮数和终态都可以
精确断言。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from unilabos.registry.decorators import action, device, not_action, topic_config

#: 初始温度 / 初始纯度 / 每次提纯的纯度增量：与 workflows.py 里的条件阈值配套。
INITIAL_TEMPERATURE_C = 20.0
INITIAL_PURITY = 0.4
PURITY_STEP = 0.25


@device(
    id="virtual_reactor_demo",
    display_name="虚拟反应釜",
    category=["virtual_device"],
    description="加样 / 后台升温 / 提纯检测 / 板孔加样的演示设备，状态与返回值可被工作流循环判定",
    supported_backends=["hostlink", "ros2"],
)
class VirtualReactorDemo:
    """按固定步长演化状态的虚拟反应釜。"""

    run_in_test_mode = True

    def __init__(
        self,
        device_id: Optional[str] = None,
        heating_rate_c_per_s: float = 30.0,
        **kwargs: Any,
    ) -> None:
        """初始化虚拟反应釜。

        Args:
            device_id[设备ID]: 设备实例 ID，默认 virtual_reactor_demo。
            heating_rate_c_per_s[升温速率]: 后台加热线程每秒升高的温度（℃/s）。
        """
        self.device_id = device_id or "virtual_reactor_demo"
        self.logger = logging.getLogger(f"VirtualReactor.{self.device_id}")
        self._heating_rate = float(heating_rate_c_per_s)
        self._lock = threading.Lock()
        self._heater: Optional[threading.Thread] = None
        self._reset_state()

    @not_action
    def post_init(self, node: Any) -> None:
        self._device_node = node

    @not_action
    def _reset_state(self) -> None:
        self._temperature = INITIAL_TEMPERATURE_C
        self._target = INITIAL_TEMPERATURE_C
        self._heating = False
        self._purity = INITIAL_PURITY
        self._polish_count = 0
        self._additions: List[Dict[str, Any]] = []
        self._plates: Dict[int, List[int]] = {}
        self._current_plate: int = 0
        self._fill_count = 0

    # ============ 周期上报的状态（while 循环的 device_state 条件读这里） ============

    @property
    @topic_config(period=0.25)
    def temperature_c(self) -> float:
        """当前温度（℃），后台加热时每 0.1 s 演化一次。"""
        return round(self._temperature, 2)

    @property
    @topic_config(period=0.5)
    def heating(self) -> bool:
        """后台加热是否在进行。"""
        return self._heating

    @property
    @topic_config(period=0.5)
    def purity(self) -> float:
        """当前纯度（0~1）。"""
        return round(self._purity, 3)

    @property
    @topic_config(period=0.5)
    def addition_count(self) -> int:
        """累计加样次数。"""
        return len(self._additions)

    @property
    @topic_config(period=0.5)
    def fill_count(self) -> int:
        """累计孔位加样次数。"""
        return self._fill_count

    # ============ 动作 ============

    @action(
        display_name="复位",
        description="温度回到 20 ℃、纯度回到 0.4、清空加样与板孔记录",
        always_free=True,
        feedback_interval=1.0,
    )
    def reset(self) -> Dict[str, Any]:
        """复位全部状态（停止后台加热）。"""
        self._stop_heater()
        with self._lock:
            self._reset_state()
        self.logger.info("[VirtualReactor] 已复位")
        return {"success": True, "temperature_c": self._temperature, "purity": self._purity}

    @action(
        display_name="加样",
        description="按样品编号加样并累计；for 循环里用 {{loop.iteration}} 生成编号",
        always_free=True,
        feedback_interval=1.0,
    )
    def add_reagent(self, sample_id: str = "S-1", volume_ml: float = 5.0) -> Dict[str, Any]:
        """加入一份样品。

        Args:
            sample_id[样品编号]: 样品标识，循环体里可写 "S-{{loop.iteration}}"。
            volume_ml[体积]: 加样体积（ml）。
        """
        with self._lock:
            self._additions.append({"sample_id": str(sample_id), "volume_ml": float(volume_ml)})
            count = len(self._additions)
        self.logger.info(f"[VirtualReactor] 加样 {sample_id} {volume_ml} ml（第 {count} 份）")
        return {"success": True, "sample_id": str(sample_id), "volume_ml": float(volume_ml), "addition_count": count}

    @action(
        display_name="开始升温",
        description="后台线程按固定速率升温到目标后自动停止；温度作为状态字段周期上报",
        always_free=True,
        feedback_interval=1.0,
    )
    def start_heating(self, target_c: float = 80.0) -> Dict[str, Any]:
        """启动后台加热。

        Args:
            target_c[目标温度]: 升到该温度（℃）后自动停止。
        """
        self._stop_heater()
        with self._lock:
            self._target = float(target_c)
            self._heating = self._temperature < self._target
        if self._heating:
            self._heater = threading.Thread(target=self._heat_loop, name=f"{self.device_id}-heater", daemon=True)
            self._heater.start()
        self.logger.info(f"[VirtualReactor] 开始升温：{self._temperature} → {self._target} ℃")
        return {"success": True, "target_c": self._target, "temperature_c": self._temperature, "heating": self._heating}

    @action(
        display_name="停止升温",
        description="停止后台加热并回报当前温度",
        always_free=True,
        feedback_interval=1.0,
    )
    def stop_heating(self) -> Dict[str, Any]:
        """停止后台加热。"""
        self._stop_heater()
        self.logger.info(f"[VirtualReactor] 停止升温，当前 {self._temperature} ℃")
        return {"success": True, "temperature_c": round(self._temperature, 2), "heating": False}

    @action(
        display_name="提纯",
        description="每次提纯纯度 +0.25（上限 1.0）",
        always_free=True,
        feedback_interval=1.0,
    )
    def polish(self) -> Dict[str, Any]:
        """执行一次提纯。"""
        with self._lock:
            self._purity = min(1.0, self._purity + PURITY_STEP)
            self._polish_count += 1
            purity, count = self._purity, self._polish_count
        self.logger.info(f"[VirtualReactor] 第 {count} 次提纯，纯度 {purity:.2f}")
        return {"success": True, "purity": round(purity, 3), "polish_count": count}

    @action(
        display_name="取样检测",
        description="返回当前纯度与是否达标（ready）；while 循环用返回值判定是否继续提纯",
        always_free=True,
        feedback_interval=1.0,
    )
    def check_quality(self, threshold: float = 0.85) -> Dict[str, Any]:
        """检测纯度是否达标。

        Args:
            threshold[达标阈值]: 纯度不低于该值即 ready。
        """
        purity = self._purity
        ready = purity >= float(threshold)
        self.logger.info(f"[VirtualReactor] 取样检测：纯度 {purity:.2f}，{'达标' if ready else '未达标'}")
        return {"success": True, "purity": round(purity, 3), "threshold": float(threshold), "ready": ready}

    @action(
        display_name="切换到板",
        description="嵌套循环外层：记住当前处理的板号",
        always_free=True,
        feedback_interval=1.0,
    )
    def start_plate(self, plate: int = 1) -> Dict[str, Any]:
        """切换到某块板。

        Args:
            plate[板号]: 从 1 起；外层循环可写 "{{loop.iteration}}"。
        """
        with self._lock:
            self._current_plate = int(plate)
            self._plates.setdefault(self._current_plate, [])
        return {"success": True, "plate": self._current_plate}

    @action(
        display_name="孔位加样",
        description="嵌套循环内层：给当前板的某个孔加样",
        always_free=True,
        feedback_interval=1.0,
    )
    def fill_well(self, well: int = 1, volume_ml: float = 2.0) -> Dict[str, Any]:
        """给当前板的一个孔加样。

        Args:
            well[孔号]: 从 1 起；内层循环可写 "{{loop.iteration}}"。
            volume_ml[体积]: 加样体积（ml）。
        """
        with self._lock:
            if self._current_plate == 0:
                raise RuntimeError("尚未切换到任何板，先调用 start_plate")
            self._plates.setdefault(self._current_plate, []).append(int(well))
            self._fill_count += 1
            plate, count = self._current_plate, self._fill_count
        return {"success": True, "plate": plate, "well": int(well), "volume_ml": float(volume_ml), "fill_count": count}

    @action(
        display_name="汇总报告",
        description="读回全部累计结果：加样清单、温度、提纯次数与纯度、各板孔位",
        always_free=True,
        feedback_interval=1.0,
    )
    def report(self) -> Dict[str, Any]:
        """汇总当前状态。"""
        with self._lock:
            return {
                "success": True,
                "additions": [item["sample_id"] for item in self._additions],
                "temperature_c": round(self._temperature, 2),
                "heating": self._heating,
                "polish_count": self._polish_count,
                "purity": round(self._purity, 3),
                "plates": {str(plate): list(wells) for plate, wells in sorted(self._plates.items())},
                "fill_count": self._fill_count,
            }

    # ============ 后台加热 ============

    @not_action
    def _heat_loop(self) -> None:
        """每 0.1 s 升温一格，到目标即停；stop_heating / reset 也会让它退出。"""

        step = self._heating_rate * 0.1
        while True:
            time.sleep(0.1)
            with self._lock:
                if not self._heating:
                    return
                self._temperature = min(self._target, self._temperature + step)
                if self._temperature >= self._target:
                    self._heating = False
                    self.logger.info(f"[VirtualReactor] 已升到目标 {self._target} ℃")
                    return

    @not_action
    def _stop_heater(self) -> None:
        with self._lock:
            self._heating = False
        heater = self._heater
        if heater is not None and heater.is_alive() and heater is not threading.current_thread():
            heater.join(timeout=1.0)
        self._heater = None

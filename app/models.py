"""请求 / 响应数据模型（Pydantic v2）。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Operation = Literal["add", "dilute", "pour", "clean", "hold"]
TransferOperation = Literal["transfer", "hold"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ComponentIn(_Strict):
    reagent_id: str = Field(..., description="试剂编号，见 /catalog")
    concentration: float = Field(..., description="试剂浓度（质量/体积百分比，0-100）")
    volume_ml: float = Field(..., description="该加液的体积（毫升，须 > 0）")


class StepIn(_Strict):
    seq: int = Field(..., description="步骤序号，正整数，整个流程内不得重复")
    op: Operation = Field(..., description="add=加液, dilute=加水稀释, pour=倒出, clean=清洗, hold=静置等待")
    reagent_id: str | None = Field(None, description="op=add 时必填：加入的试剂编号")
    concentration: float | None = Field(None, description="op=add 时必填：加入试剂的浓度（0-100）")
    volume_ml: float | None = Field(
        None,
        description="add/dilute: 加入体积；pour: 倒出体积；均须 > 0。clean/hold 不得提供。",
    )
    at_seconds: float | None = Field(
        None,
        description="该步骤完成时刻（秒，相对流程起点），须非负且不早于上一步。缺省沿用上一步时刻。",
    )
    note: str | None = Field(None, description="可选备注，不参与计算")


class ContactAllowanceIn(_Strict):
    a: str = Field(..., description="反应对一端：试剂编号，或材质编号（材质用 material:<id> 或直接 <id>）")
    b: str = Field(..., description="反应对另一端：试剂编号或材质编号")
    max_contact_seconds: float = Field(..., description="声明允许的接触时长（秒，须 > 0）")


class PrecheckRequest(_Strict):
    vessel_material: str = Field(..., description="容器材质编号，见 /catalog")
    temperature_c: float = Field(..., description="体系温度（摄氏度），允许范围 -50 ~ 300")
    initial_liquid: list[ComponentIn] = Field(default_factory=list, description="容器初始液体")
    steps: list[StepIn] = Field(default_factory=list, description="按顺序执行的加液/操作步骤")
    contact_allowances: list[ContactAllowanceIn] = Field(
        default_factory=list, description="可选：为某些反应对声明允许接触时长（覆盖规则默认值）"
    )


# ---------------------------------------------------------------------------
# 双容器转移预检请求
# ---------------------------------------------------------------------------
class VesselIn(_Strict):
    vessel_id: str = Field(..., min_length=1, description="容器编号，两只容器之间须互异")
    vessel_material: str = Field(..., description="容器材质编号，见 /catalog")
    initial_liquid: list[ComponentIn] = Field(default_factory=list, description="该容器初始液体")


class TransferStepIn(_Strict):
    seq: int = Field(..., description="步骤序号，正整数，整个流程内不得重复")
    op: TransferOperation = Field(..., description="transfer=定量转移, hold=静置等待")
    from_vessel: str | None = Field(None, min_length=1, description="op=transfer 时必填：源容器编号")
    to_vessel: str | None = Field(None, min_length=1, description="op=transfer 时必填：目标容器编号")
    volume_ml: float | None = Field(None, description="op=transfer 时必填：定量转移体积（毫升，须 > 0）")
    at_seconds: float | None = Field(
        None,
        description="该步骤完成时刻（秒，相对流程起点），须非负且不早于上一步。缺省沿用上一步时刻。",
    )
    note: str | None = Field(None, description="可选备注，不参与计算")


class TransferPrecheckRequest(_Strict):
    vessels: list[VesselIn] = Field(..., description="两只容器（恰好 2 只，编号互异）")
    temperature_c: float = Field(..., description="统一体系温度（摄氏度），允许范围 -50 ~ 300")
    steps: list[TransferStepIn] = Field(default_factory=list, description="按时刻执行、同刻按序号执行的转移/静置步骤")
    contact_allowances: list[ContactAllowanceIn] = Field(
        default_factory=list, description="可选：为某些反应对声明允许接触时长（覆盖规则默认值）"
    )


# ---------------------------------------------------------------------------
# 配液核对请求（实测浓度对账）
# ---------------------------------------------------------------------------
class ReconcileObservationIn(_Strict):
    at_initial: bool | None = Field(
        None, description="true=核对初始态；缺省或 false 时须给出 steps 中存在的 seq"
    )
    seq: int | None = Field(None, description="核对位置：该步骤完成后的状态（须为原流程中存在的步骤序号）")
    reagent_id: str = Field(..., description="被核对试剂编号（未知试剂整单拒绝；该位置未出现的已知试剂按 0 计）")
    concentration: float = Field(..., description="该位置的实测浓度（0~100 百分点）")

    @field_validator("at_initial", mode="before")
    @classmethod
    def _strict_bool(cls, v):
        if v is not None and not isinstance(v, bool):
            raise ValueError("at_initial 必须是布尔值 true/false")
        return v

    @field_validator("seq", mode="before")
    @classmethod
    def _strict_int(cls, v):
        if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
            raise ValueError("seq 必须是整数")
        return v

    @field_validator("concentration", mode="before")
    @classmethod
    def _strict_float(cls, v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("concentration 必须是数值")
        return float(v)


class TransferReconcileObservationIn(ReconcileObservationIn):
    vessel_id: str = Field(..., min_length=1, description="双容器必填：观测取自哪只容器")


class ReconcileRequest(_Strict):
    request: PrecheckRequest = Field(..., description="原单容器预检请求（沿用原校验与模拟）")
    tolerance_pp: float = Field(..., description="统一的有限非负浓度容差（百分点）：|实测-预测| ≤ 容差 才算通过")
    observations: list[ReconcileObservationIn] = Field(
        default_factory=list, description="实测观测；可为空，此时只返回原预检风险结果"
    )

    @field_validator("tolerance_pp", mode="before")
    @classmethod
    def _strict_tolerance(cls, v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("tolerance_pp 必须是数值")
        return float(v)


class TransferReconcileRequest(_Strict):
    request: TransferPrecheckRequest = Field(..., description="原双容器转移预检请求（沿用原校验与模拟）")
    tolerance_pp: float = Field(..., description="统一的有限非负浓度容差（百分点）：|实测-预测| ≤ 容差 才算通过")
    observations: list[TransferReconcileObservationIn] = Field(
        default_factory=list, description="实测观测（每条另须指定 vessel_id）；可为空"
    )

    @field_validator("tolerance_pp", mode="before")
    @classmethod
    def _strict_tolerance(cls, v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("tolerance_pp 必须是数值")
        return float(v)


# ---------------------------------------------------------------------------
# 响应
# ---------------------------------------------------------------------------
class ComponentState(_Strict):
    reagent_id: str
    name: str
    concentration: float
    pure_volume_ml: float


class StepTrace(_Strict):
    seq: int | None
    op: str
    at_seconds: float
    liquid_volume_ml: float
    cleaned: bool
    components: list[ComponentState]


class Finding(_Strict):
    seq: int | None = Field(..., description="首次触发的步骤序号；初始液体触发时为 0")
    category: Literal["forbidden_mix", "overtime_contact", "material_incompatible"]
    rule_id: str
    rule_name: str
    entities: list[str] = Field(..., description="涉及组分：试剂编号或 material:<id>")
    concentrations: dict[str, float] = Field(..., description="涉及试剂在触发时刻的估算浓度（%）")
    contact_seconds: float | None = Field(None, description="超时类：实际接触时长（秒）")
    allowed_seconds: float | None = Field(None, description="超时类：允许接触时长（秒）")
    severity: str
    reason: str
    guidance: str
    blocking_reason: str = Field(..., description="阻断原因（人类可读）")


class PrecheckResponse(_Strict):
    safe: bool
    blocked: bool
    first_blocking_seq: int | None
    ruleset_version: str
    vessel_material: str
    temperature_c: float
    findings: list[Finding]
    final_state: StepTrace
    steps: list[StepTrace]


# ---------------------------------------------------------------------------
# 双容器转移预检响应
# ---------------------------------------------------------------------------
class VesselStateOut(_Strict):
    vessel_id: str
    vessel_material: str
    liquid_volume_ml: float
    cleaned: bool
    components: list[ComponentState]


class TransferFinding(_Strict):
    seq: int | None = Field(..., description="首次触发的步骤序号；初始液体触发时为 0")
    vessel_id: str = Field(..., description="风险所在容器编号（规则在该容器的液相/材质界面上成立）")
    category: Literal["forbidden_mix", "overtime_contact", "material_incompatible"]
    rule_id: str
    rule_name: str
    entities: list[str] = Field(..., description="涉及组分：试剂编号或 material:<id>")
    concentrations: dict[str, float] = Field(..., description="涉及试剂在触发时刻的估算浓度（%）")
    contact_seconds: float | None = Field(None, description="超时类：实际接触时长（秒）")
    allowed_seconds: float | None = Field(None, description="超时类：允许接触时长（秒）")
    severity: str
    reason: str
    guidance: str
    blocking_reason: str = Field(..., description="阻断原因（人类可读，含容器编号）")


class TransferStepTrace(_Strict):
    seq: int | None
    op: str
    at_seconds: float
    from_vessel: str | None = Field(None, description="transfer：源容器编号；hold/initial 为 null")
    to_vessel: str | None = Field(None, description="transfer：目标容器编号；hold/initial 为 null")
    volume_ml: float | None = Field(None, description="transfer：定量转移体积（毫升）")
    vessels: list[VesselStateOut] = Field(..., description="该步完成后两只容器的状态快照（按提交顺序）")


class TransferPrecheckResponse(_Strict):
    safe: bool
    blocked: bool
    first_blocking_seq: int | None
    first_blocking_vessel_id: str | None
    ruleset_version: str
    temperature_c: float
    findings: list[TransferFinding]
    steps: list[TransferStepTrace]
    final_states: list[VesselStateOut] = Field(..., description="末态：两只容器的最终状态（按提交顺序）")


class ErrorDetail(_Strict):
    code: str
    message: str
    seq: int | None = None
    field: str | None = None


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    error: Literal["validation_error", "simulation_error", "internal_error"]
    message: str
    details: list[ErrorDetail]


# ---------------------------------------------------------------------------
# 配液核对响应（实测浓度对账）
# ---------------------------------------------------------------------------
class ReconcileItem(_Strict):
    observation_index: int = Field(..., description="观测在提交数组中的下标（0 起）")
    at_initial: bool = Field(..., description="该观测是否核对初始态")
    seq: int | None = Field(None, description="非初始观测对应的步骤序号；初始观测为 null")
    reagent_id: str
    measured_concentration: float = Field(..., description="实测浓度（百分点）")
    predicted_concentration: float = Field(..., description="预测浓度（百分点，未四舍五入的计算值；该位置未出现的已知试剂为 0）")
    deviation: float = Field(..., description="绝对偏差 |实测-预测|（百分点，未四舍五入）")
    passed: bool = Field(..., description="绝对偏差不超过容差且实测在 0~100 内时为 true")


class TransferReconcileItem(ReconcileItem):
    vessel_id: str = Field(..., description="该观测所在容器编号")


class FirstOutOfTolerance(_Strict):
    observation_index: int = Field(..., description="最早超差观测的提交下标（同位置按提交顺序）")
    at_initial: bool
    seq: int | None = None
    reagent_id: str
    measured_concentration: float
    predicted_concentration: float
    deviation: float
    tolerance_pp: float


class TransferFirstOutOfTolerance(FirstOutOfTolerance):
    vessel_id: str


class ReconcileResponse(_Strict):
    tolerance_pp: float
    all_passed: bool = Field(..., description="全部观测偏差均不超过容差时为 true（无观测恒为 true）")
    first_out_of_tolerance: FirstOutOfTolerance | None = Field(
        None, description="按步骤执行顺序的最早超差位置（同位置按观测提交顺序）；无超差为 null"
    )
    precheck: PrecheckResponse = Field(..., description="原预检风险结果（与 POST /api/v1/precheck 响应一致）")
    observations: list[ReconcileItem] = Field(..., description="逐条预测值与偏差，按观测提交顺序")


class TransferReconcileResponse(_Strict):
    tolerance_pp: float
    all_passed: bool
    first_out_of_tolerance: TransferFirstOutOfTolerance | None = None
    precheck: TransferPrecheckResponse = Field(..., description="原转移预检风险结果（与 POST /api/v1/transfer-precheck 响应一致）")
    observations: list[TransferReconcileItem]

# 化学试剂混配风险预检 HTTP API

提交**容器初始液体、按顺序执行的加液步骤、试剂浓度与体积、容器材质与温度**，
服务逐步计算混配后各组分的浓度，依据内置的公开化学相容性规则识别：

- **禁配组合**（forbidden_mix）：如强酸 + 漂白水 → 氯气、漂白水 + 氨水 → 氯胺、氧化剂 + 易燃物等；
- **超时接触**（overtime_contact）：允许短时接触的反应对超过允许接触秒数（可用 `contact_allowances` 声明覆盖默认值）；
- **材质不相容**（material_incompatible）：如 HF 进玻璃容器、强碱长期接触玻璃、酸对碳钢/铝的腐蚀等。

输出**首次触发步骤、涉及组分、触发时刻的估算浓度与阻断原因**；`dilute`（稀释）、`pour`（倒出）、
`clean`（清洗）会改变后续状态。输入存在问题时**逐项拒绝**（序号重复、体积非正、浓度越界、
未知试剂、清洗后仍引用旧状态等），一次性返回全部错误。

> 用途：实验室/工艺 SOP 的自动化预检与教学。规则库为简化的公开化学常识集合，
> **不能替代** MSDS/SDS 与专业 EHS 评估。

> **本轮新增**：
> 1. 双容器定量转移预检 `POST /api/v1/transfer-precheck`
>    （[说明见此节](#双容器转移预检本轮新增)）；
> 2. **配液核对（实测浓度对账）** `POST /api/v1/reconcile` 与
>    `POST /api/v1/transfer-reconcile`
>    （[说明见此节](#配液核对实测浓度对账本轮新增)）：提交原流程请求、统一容差与若干实测观测，
>    服务先沿用原预检校验与模拟，再用**未四舍五入**的预测浓度逐条对账。
> 3. **预检风险的物料来源溯源** `POST /api/v1/precheck-source-trace` 与
>    `POST /api/v1/transfer-source-trace`
>    （[说明见此节](#预检风险物料来源溯源本轮新增)）：请求体与原预检**完全相同**，
>    响应包含**原预检结果**与每条风险涉及试剂在**触发时刻**的来源批次、仍存纯物量及其占比。
>
> 原 `/api/v1/precheck` 单容器预检的**请求与响应完全保持兼容，启动方式不变**
> （仍是同一个 `uvicorn app.main:app` / `docker compose up`，同一端口 8000）。

---

## 目录

- [快速开始（Docker Compose）](#快速开始docker-compose)
- [本地运行（不用 Docker）](#本地运行不用-docker)
- [API 说明](#api-说明)
- [双容器转移预检（本轮新增）](#双容器转移预检本轮新增)
- [配液核对：实测浓度对账（本轮新增）](#配液核对实测浓度对账本轮新增)
- [预检风险物料来源溯源（本轮新增）](#预检风险物料来源溯源本轮新增)
- [逐步状态与浓度模型](#逐步状态与浓度模型)
- [操作类型](#操作类型)
- [规则、阈值与接触计时](#规则阈值与接触计时)
- [错误码（逐项拒绝）](#错误码逐项拒绝)
- [内置试剂 / 材质 / 规则](#内置试剂--材质--规则)
- [调用示例（curl / Python）](#调用示例curl--python)
- [测试](#测试)
- [项目结构](#项目结构)

---

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 服务监听 http://localhost:8000
curl http://localhost:8000/health
curl http://localhost:8000/catalog | python -m json.tool
```

发起一次预检（禁配：往漂白水里加盐酸 → 氯气）：

```bash
curl -s http://localhost:8000/api/v1/precheck \
  -H 'Content-Type: application/json' \
  --data @examples/forbidden_mix.json | python -m json.tool
```

## 本地运行（不用 Docker）

需要 Python 3.11+：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## API 说明

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查，返回规则库版本 |
| `GET` | `/catalog` | 内置试剂、容器材质与全部规则（含选择器、阈值、默认接触时长） |
| `POST` | `/api/v1/precheck` | 单容器混配流程，返回风险与逐步状态（请求/响应保持兼容） |
| `POST` | `/api/v1/transfer-precheck` | **双容器转移预检**：两只容器之间的定量转移与静置，返回带容器编号的风险与两侧状态 |
| `POST` | `/api/v1/reconcile` | **单容器配液核对**：原预检请求 + 统一容差 + 实测观测，返回原预检结果、逐条预测/偏差与最早超差位置 |
| `POST` | `/api/v1/transfer-reconcile` | **双容器配液核对**：同上对账语义，每条观测另须指定容器编号 |
| `POST` | `/api/v1/precheck-source-trace` | **单容器风险来源溯源**：请求同 `/api/v1/precheck`，返回原预检结果 + 每条风险涉及试剂的来源批次、触发时仍存纯物量与占比 |
| `POST` | `/api/v1/transfer-source-trace` | **双容器风险来源溯源**：请求同 `/api/v1/transfer-precheck`，返回原转移预检结果 + 带来源容器的逐条风险来源 |

### 请求体

```json
{
  "vessel_material": "glass",
  "temperature_c": 22.0,
  "initial_liquid": [
    {"reagent_id": "naocl", "concentration": 10.0, "volume_ml": 100.0}
  ],
  "steps": [
    {"seq": 1, "op": "add", "reagent_id": "hcl",
     "concentration": 30.0, "volume_ml": 50.0, "at_seconds": 30}
  ],
  "contact_allowances": []
}
```

| 字段 | 说明 |
| --- | --- |
| `vessel_material` | 容器材质编号，见 `/catalog`（如 `glass`/`hdpe`/`ptfe`/`aluminum`） |
| `temperature_c` | 体系温度 ℃，允许 [-50, 300]；达到部分规则的高温阈值时阻断原因会追加风险提示 |
| `initial_liquid[]` | 容器初始液体；同一试剂不得重复出现 |
| `steps[]` | 按顺序执行的操作；`seq` 为正整数且**不得重复**，乱序提交会按 `seq` 稳定排序处理 |
| `steps[].at_seconds` | 该步骤完成时刻（相对流程起点，秒），非负、不早于上一步；缺省沿用上一步时刻 |
| `contact_allowances[]` | 为反应对声明允许接触时长（秒），覆盖规则默认值；端可写试剂编号或材质编号（`glass` 或 `material:glass` 均可） |

### 响应（发现风险时 HTTP 200，`blocked=true`）

```json
{
  "safe": false,
  "blocked": true,
  "first_blocking_seq": 1,
  "ruleset_version": "chem-rules-2026-10-01",
  "vessel_material": "glass",
  "temperature_c": 22.0,
  "findings": [
    {
      "seq": 1,
      "category": "forbidden_mix",
      "rule_id": "F01",
      "rule_name": "强酸 + 次氯酸盐释放氯气",
      "entities": ["hcl", "naocl"],
      "concentrations": {"hcl": 10.0, "naocl": 6.6667},
      "contact_seconds": null,
      "allowed_seconds": null,
      "severity": "critical",
      "reason": "强酸使次氯酸盐转化为氯气（Cl₂）……",
      "guidance": "严禁向漂白水中加酸……",
      "blocking_reason": "禁配组合：强酸 + 次氯酸盐释放氯气（F01）在步骤 1 首次共存"
    }
  ],
  "steps": [ /* 每个步骤完成后的状态快照，按 seq 排序 */ ],
  "final_state": { /* 末态快照 */ }
}
```

- `findings` 按 `(seq, 类别)` 稳定排序：初始液体触发时 `seq=0`；类别顺序为禁配 → 材质不相容 → 超时接触。
- 每个状态快照包含：`at_seconds`、`liquid_volume_ml`、`cleaned`、`components[]`
  （`reagent_id` / `name` / `concentration` / `pure_volume_ml`）。
- 超时类 finding 另含 `contact_seconds`（实际接触时长）与 `allowed_seconds`（允许时长）。

完整样例见 [`examples/response_blocked.json`](examples/response_blocked.json)。

### 错误响应（HTTP 400，逐项列出）

```json
{
  "error": "validation_error",
  "message": "预检请求被逐项拒绝，未进行模拟",
  "details": [
    {"code": "DUPLICATE_STEP_SEQ", "message": "步骤序号 2 重复", "seq": 2, "field": "seq"},
    {"code": "INVALID_VOLUME", "message": "体积须为正数（毫升），收到 0.0", "seq": 3, "field": "volume_ml"}
  ]
}
```

---

## 双容器转移预检（本轮新增）

`POST /api/v1/transfer-precheck` 判断**已配液在两只容器之间定量转移**时的风险。
请求描述**编号互异、各有材质与初始液体的两只容器、统一温度**，以及按时刻执行、
**同刻按序号（seq）执行**的 `transfer`（定量转移）与 `hold`（静置）步骤。
沿用同一套试剂、浓度阈值与相容性规则（F/T/M 规则、`contact_allowances` 覆盖均通用）。

### 转移语义（充分混合假设）

- **transfer**：按转出体积占源容器当前体积的比例，**同步扣减源容器各组分纯物量与总体积**
  （部分转出浓度不变）；同一份纯物量与体积**计入目标容器**（目标既有液体会被稀释，各组分重新计浓）。
- **接触计时跨容器延续规则**：两侧**既有且仍达阈值**的接触片段继续计时；
  **仅新达阈值**的接触（如转入的试剂与目标容器既有组分/材质首次共存）**从转入时刻起**作为新片段计时；
  任一条件跌破阈值（含源容器被**转空**）片段即结束，重新满足则重新计时。
- **检查顺序**：初始状态（t=0）也检查风险；每个步骤**先把时间推进到该时刻并结算两侧此前超时**，
  再执行操作，然后检查转入后的**即时禁配与材质风险**。
- 发现风险时模拟继续完成（可看到全部首次触发点），响应给**首次阻断**；输入非法时**整体拒绝、不返回任何部分模拟结果**。

### 请求体

```json
{
  "vessels": [
    {"vessel_id": "acid-bottle", "vessel_material": "hdpe",
     "initial_liquid": [{"reagent_id": "hcl", "concentration": 30.0, "volume_ml": 100.0}]},
    {"vessel_id": "bleach-tank", "vessel_material": "hdpe",
     "initial_liquid": [{"reagent_id": "naocl", "concentration": 10.0, "volume_ml": 100.0}]}
  ],
  "temperature_c": 22.0,
  "steps": [
    {"seq": 1, "op": "transfer", "from_vessel": "acid-bottle",
     "to_vessel": "bleach-tank", "volume_ml": 50.0, "at_seconds": 30.0}
  ],
  "contact_allowances": []
}
```

| 字段 | 说明 |
| --- | --- |
| `vessels` | **恰好 2 只**容器；`vessel_id` 非空且**互异**，`vessel_material` 见 `/catalog`，各自带 `initial_liquid`（同容器内试剂不得重复） |
| `temperature_c` | 两只容器的统一温度 ℃，允许 [-50, 300] |
| `steps[].op` | `transfer`（定量转移）或 `hold`（仅推进时间） |
| `transfer` 字段 | `from_vessel` / `to_vessel`（两只互异的已知容器）、`volume_ml`（>0，且不超过源容器当前体积） |
| `at_seconds` | 同单容器：非负、不回退；缺省沿用上一步时刻；`hold` 须显式且严格晚于当前时刻 |
| `contact_allowances` | 与单容器相同的反应对时长覆盖，对两侧容器同时生效 |

### 响应要点

- `first_blocking_seq` + **`first_blocking_vessel_id`**：首次阻断所在步骤与容器；无风险时均为 `null`。
- `findings[]`：每条 finding 额外带 **`vessel_id`**（规则在该容器液相/材质界面上成立），
  `blocking_reason` 以“容器 X：……”开头；排序为 `(seq, 类别, 容器提交顺序)`，初始触发 `seq=0`。
- `steps[]`：每步含 `op` / `at_seconds` / `from_vessel` / `to_vessel` / `volume_ml`，
  以及 **`vessels[]` 两侧状态快照**（各含 `vessel_id`、`vessel_material`、`liquid_volume_ml`、`components[]`）。
- `final_states[]`：两只容器的末态快照（按提交顺序）。

完整样例见 [`examples/transfer_forbidden.json`](examples/transfer_forbidden.json)
与 [`examples/transfer_response_blocked.json`](examples/transfer_response_blocked.json)；
安全/超时样例见 `examples/transfer_safe.json`、`examples/transfer_overtime.json`。

### 转移预检的拒绝码

沿用单容器全部通用码（`DUPLICATE_STEP_SEQ` / `INVALID_SEQ` / `INVALID_VOLUME` /
`CONCENTRATION_OUT_OF_RANGE` / `UNKNOWN_REAGENT` / `UNKNOWN_MATERIAL` / `UNKNOWN_ENTITY` /
`TEMPERATURE_OUT_OF_RANGE` / `INVALID_TIME` / `NON_MONOTONIC_TIME` / `DUPLICATE_COMPONENT` /
`INVALID_STEP_FIELDS` / `INVALID_ALLOWANCE` / `DUPLICATE_ALLOWANCE` / `MALFORMED_REQUEST`），另加：

| Code | 触发条件 |
| --- | --- |
| `INVALID_VESSEL` | 容器数量不是恰好 2 只 |
| `DUPLICATE_VESSEL_ID` | 两只容器 `vessel_id` 相同 |
| `UNKNOWN_VESSEL` | transfer 的源/目标编号不在两只容器中 |
| `SAME_VESSEL` | transfer 的源容器与目标容器相同 |
| `EMPTY_SOURCE_VESSEL` | 源容器为空仍发起转移 |
| `TRANSFER_EXCEEDS_VOLUME` | 转移体积超过源容器当前液体体积 |
| `EMPTY_VESSEL_OPERATION` | 两只容器皆空时执行 hold |

---

## 配液核对：实测浓度对账（本轮新增）

`POST /api/v1/reconcile`（单容器）与 `POST /api/v1/transfer-reconcile`（双容器）
用于核对**实际配液是否沿预检轨迹运行**。请求把一次**原预检请求原封不动**嵌在 `request` 字段，
再加一个统一容差和若干条现场实测观测；服务：

1. **先沿用原请求的逐项校验与模拟**（`build_plan` / `build_transfer_plan` + 同一引擎，
   不做任何放宽）；原流程无效 → 整单 HTTP 400 拒绝，**不返回任何对账结果**；
2. 把每条观测定位到**初始态**（`at_initial: true`）或**某步骤完成后**（`seq`），
   与该位置的**预测浓度**比较；
3. 该位置**未出现的已知试剂按 0% 计**（因此“此处不应有某试剂”也可观测为 0）；
4. 比较一律使用引擎的**未四舍五入计算值**（预检快照里的 4 位小数仅用于展示）；
   `|实测 - 预测| ≤ tolerance_pp` 才判通过（百分点，边界相等算通过）。

返回内容包括：

- `precheck`：**原预检风险结果**，结构与 `POST /api/v1/precheck`
  （双容器为 `/api/v1/transfer-precheck`）的响应逐字段一致；预检 `blocked=true`
  不影响对账，风险结果与对账同时给出；
- `observations[]`：**逐条预测值与偏差**，按观测提交顺序排列，每条含
  `observation_index`（提交下标）、`at_initial` / `seq`、`reagent_id`
  （双容器另含 `vessel_id`）、`measured_concentration`、
  **`predicted_concentration`（未四舍五入）**、**`deviation`（绝对差，未四舍五入）**、
  `passed`；
- `first_out_of_tolerance`：**按步骤执行顺序的最早超差位置**——初始态位置排在最前，
  之后按步骤执行顺序（乱序提交的步骤按归一化后的 `seq` 顺序），
  **同一位置内按观测提交顺序**；无超差或无观测时为 `null`；
- `all_passed`：全部观测通过为 `true`；**无观测恒为 `true`**，即不传观测也能只取回预检结果。

### 核对请求体（单容器）

```json
{
  "request": {
    "vessel_material": "glass",
    "temperature_c": 22.0,
    "initial_liquid": [{"reagent_id": "naocl", "concentration": 10.0, "volume_ml": 100.0}],
    "steps": [
      {"seq": 1, "op": "add", "reagent_id": "hcl",
       "concentration": 30.0, "volume_ml": 50.0, "at_seconds": 30}
    ],
    "contact_allowances": []
  },
  "tolerance_pp": 0.5,
  "observations": [
    {"at_initial": true, "reagent_id": "naocl", "concentration": 10.2},
    {"seq": 1, "reagent_id": "hcl", "concentration": 9.8},
    {"seq": 1, "reagent_id": "naocl", "concentration": 6.67},
    {"seq": 1, "reagent_id": "water", "concentration": 0.0}
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `request` | **原预检请求体原样嵌入**（单容器字段见上；双容器为 `vessels`/`temperature_c`/`steps`/`contact_allowances`） |
| `tolerance_pp` | 统一的**有限非负**浓度容差（百分点）；负数、NaN、无穷、非数值一律拒绝 |
| `observations[]` | 实测观测，可为空数组或缺省 |
| `observations[].at_initial` | `true` 表示核对**初始态**；与 `seq` 互斥 |
| `observations[].seq` | 核对**该步骤完成后**的状态；必须是原流程中存在的步骤序号（乱序提交也按归一化计划识别） |
| `observations[].reagent_id` | 已知试剂编号；该位置未出现的已知试剂预测浓度按 0；**未知试剂整单拒绝** |
| `observations[].concentration` | 实测浓度，允许 **[0, 100]**（端点，0 合法）；越界整单拒绝 |
| `observations[].vessel_id` | **仅双容器接口必填**：观测取自哪只容器；单容器接口携带该字段按 schema 错误拒绝 |

### 核对响应要点

```json
{
  "tolerance_pp": 0.5,
  "all_passed": false,
  "first_out_of_tolerance": {
    "observation_index": 2,
    "at_initial": false,
    "seq": 1,
    "reagent_id": "naocl",
    "measured_concentration": 6.0,
    "predicted_concentration": 6.666666666666667,
    "deviation": 0.666666666666667,
    "tolerance_pp": 0.5
  },
  "precheck": { "...": "与 POST /api/v1/precheck 响应一致" },
  "observations": [
    {
      "observation_index": 0,
      "at_initial": true,
      "seq": null,
      "reagent_id": "naocl",
      "measured_concentration": 10.2,
      "predicted_concentration": 10.0,
      "deviation": 0.1999999999999993,
      "passed": true
    }
  ]
}
```

双容器的 `first_out_of_tolerance` 与每条 `observations[]` 额外带 `vessel_id`；
`precheck` 为完整的转移预检响应（含 `first_blocking_vessel_id`、两侧 `final_states`）。

### 核对接口的拒绝规则（整单 400，绝不返回部分对账）

- 原流程请求不合法：沿用原预检/转移预检的**全部拒绝码**（见上两节）；
- `INVALID_TOLERANCE`：容差为负、NaN/无穷或非数值（布尔值也按非数值拒绝）；
- `UNKNOWN_POSITION`：`seq` 不在原流程的步骤序号中；
- `INVALID_POSITION`：位置表达非法——既未给 `at_initial: true` 又未给正整数 `seq`、
  二者同时给出、`seq` 类型错误；
- `UNKNOWN_VESSEL`（双容器）：观测容器编号不在两只容器中；缺少 `vessel_id` 报 `MALFORMED_REQUEST`；
- `UNKNOWN_REAGENT`：观测引用未知试剂；
- `CONCENTRATION_OUT_OF_RANGE`：实测浓度超出 [0, 100]；
- `DUPLICATE_OBSERVATION`：**同一位置、同一试剂**（双容器再加同一容器）重复观测；
  同试剂在不同位置、或同位置不同试剂均合法；
- `MALFORMED_REQUEST`：JSON/schema 错误（含未知字段、类型错误、单容器观测携带 `vessel_id`）。

错误响应体结构与原预检一致（`error` / `message` / `details[]`），
`message` 为“核对请求被整单拒绝：原流程无效或容差/观测非法，未进行模拟与对账”。

完整样例见 [`examples/reconcile_mix.json`](examples/reconcile_mix.json) +
[`examples/reconcile_response.json`](examples/reconcile_response.json)、
[`examples/transfer_reconcile.json`](examples/transfer_reconcile.json) +
[`examples/transfer_reconcile_response.json`](examples/transfer_reconcile_response.json)；
含最早超差定位的样例见
[`examples/reconcile_out_of_tolerance.json`](examples/reconcile_out_of_tolerance.json)。

---

## 预检风险物料来源溯源（本轮新增）

`POST /api/v1/precheck-source-trace`（单容器）与
`POST /api/v1/transfer-source-trace`（双容器）回答“**触发这条风险的物料是从哪来的**”：

- **请求体与原预检逐字段相同**（单容器 `/api/v1/precheck`、双容器 `/api/v1/transfer-precheck`），
  不需要任何新字段；非法输入沿用原预检的**整单拒绝**（同一套错误码、HTTP 400，不返回任何部分结果）。
- 响应中的 `precheck` 即**原预检结果**，与原端点响应逐字段一致；
  另给与 `precheck.findings` **一一对应、同序**的 `risk_sources[]`（以 `finding_index` 对齐）；
  **无风险时 `risk_sources` 为空数组**。
- 原预检接口 `/api/v1/precheck`、`/api/v1/transfer-precheck`（以及两个 reconcile）的
  **请求与响应保持兼容、不含任何溯源字段**。

### 来源标识

| 场景 | 来源批次标识 |
| --- | --- |
| 单容器初始液体 | 以**试剂编号**标识：`origin="initial"` + `reagent_id` |
| 单容器后续加液（add） | 以**步骤序号**标识：`origin="step"` + `seq` + `reagent_id` |
| 双容器初始液体 | 以**容器编号 + 试剂编号**标识：`origin="vessel"` + `vessel_id` + `reagent_id` |

### 份额口径与操作语义

- 每条风险的每个涉及试剂列出其各来源批次：
  - `pure_volume_ml`：**风险触发时该批次仍存**的纯物体积（毫升，4 位小数）；
  - `fraction`：该批次占**触发时刻、触发容器内该试剂纯物总量**的比例
    （0~1，6 位小数；同一试剂各批次**合计为 1**）；
  - `pure_volume_ml`（试剂级）：触发时刻该试剂在触发容器中的纯物总量。
- **稀释（dilute）不产生来源**：纯水无批次，仅稀释各组分浓度。
- **倒出（pour）与跨容器转移（transfer）按充分混合比例同步分摊**：每个来源批次按
  转出体积占源容器当前体积的比例缩减，转入侧同键合并。
- **清洗（clean）清空来源**：清洗后重新加入的同试剂是全新批次，与清洗前无关。
- **同一试剂再次加入须与旧来源分开**：不同 `seq`（单容器）或不同原始容器（双容器）
  的同试剂批次永远分列；跨容器转移**不产生新来源**，批次保留其原始容器标识，
  即使再次转回原容器，同键批次也只合并、不另立。
- **材质风险**（如 HF 进玻璃）在风险条目中另给触发容器材质：
  - 单容器：`vessel_material`、`material_id`（取自规则材质侧，如 `glass`）；
  - 双容器：另带 `vessel_id`（触发容器）；
  - **材质不给液体份额**：`reagents[]` 只列液相试剂（HF），不出现材质实体。
  - 试剂-试剂风险的 `material_id` 为 `null`，但仍带触发容器的 `vessel_material`。
- **超时风险**在“时间推进后、该步操作执行前”结算，来源按**该步操作前的物料归属**快照；
  随后的倒出或跨容器转空**不会改写已触发风险**的来源明细。
  立即类风险（禁配、立即材质）在操作完成后的新状态上触发，来源取操作后归属。

### 响应示例（单容器）

初始 10% 漂白水 100 mL → 步骤 1 加 3% 盐酸 100 mL（酸仅 1.5%，未过 5% 阈值）→
步骤 2 倒出 40 mL → 步骤 3 再加 30% 盐酸 50 mL，F01 在步骤 3 首次触发；
盐酸的两个加液批次分开列示（步骤 1 批次已经倒出分摊）：

```json
{
  "precheck": { "...": "与 POST /api/v1/precheck 响应逐字段一致" },
  "risk_sources": [
    {
      "finding_index": 0,
      "seq": 3,
      "category": "forbidden_mix",
      "rule_id": "F01",
      "vessel_material": "glass",
      "material_id": null,
      "reagents": [
        {
          "reagent_id": "hcl",
          "pure_volume_ml": 17.4,
          "lots": [
            {"origin": "step", "reagent_id": "hcl", "seq": 1,
             "pure_volume_ml": 2.4, "fraction": 0.137931},
            {"origin": "step", "reagent_id": "hcl", "seq": 3,
             "pure_volume_ml": 15.0, "fraction": 0.862069}
          ]
        },
        {
          "reagent_id": "naocl",
          "pure_volume_ml": 8.0,
          "lots": [
            {"origin": "initial", "reagent_id": "naocl", "seq": null,
             "pure_volume_ml": 8.0, "fraction": 1.0}
          ]
        }
      ]
    }
  ]
}
```

双容器响应中每条 `risk_sources[]` 额外带 `vessel_id`（触发容器），批次结构为
`{"origin": "vessel", "vessel_id": "<原始容器>", "reagent_id": "...",
"pure_volume_ml": ..., "fraction": ...}`；同试剂在两只容器的初始批次即使在目标容器
汇合也分开列示。完整样例见
[`examples/source_trace_request.json`](examples/source_trace_request.json) +
[`examples/source_trace_response.json`](examples/source_trace_response.json)（单容器）、
[`examples/transfer_source_trace_request.json`](examples/transfer_source_trace_request.json) +
[`examples/transfer_source_trace_response.json`](examples/transfer_source_trace_response.json)（双容器）。

### 调用示例

```bash
# 单容器：请求体即原预检请求
curl -s http://localhost:8000/api/v1/precheck-source-trace \
  -H 'Content-Type: application/json' \
  --data @examples/source_trace_request.json | python -m json.tool

# 双容器：请求体即原转移预检请求
curl -s http://localhost:8000/api/v1/transfer-source-trace \
  -H 'Content-Type: application/json' \
  --data @examples/transfer_source_trace_request.json | python -m json.tool
```

```python
import requests

payload = {
    "vessel_material": "glass",
    "temperature_c": 22.0,
    "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}],
    "steps": [
        {"seq": 1, "op": "add", "reagent_id": "hcl",
         "concentration": 3, "volume_ml": 100},
        {"seq": 2, "op": "pour", "volume_ml": 40, "at_seconds": 10},
        {"seq": 3, "op": "add", "reagent_id": "hcl",
         "concentration": 30, "volume_ml": 50, "at_seconds": 20},
    ],
    "contact_allowances": [],
}
r = requests.post("http://localhost:8000/api/v1/precheck-source-trace",
                  json=payload, timeout=10)
r.raise_for_status()
d = r.json()
for rs in d["risk_sources"]:
    f = d["precheck"]["findings"][rs["finding_index"]]
    print(f"风险 {rs['rule_id']}（步骤 {rs['seq']}）：{f['blocking_reason']}")
    for rg in rs["reagents"]:
        for lot in rg["lots"]:
            where = f"步骤 {lot['seq']} 加入" if lot["origin"] == "step" else "初始液体"
            print(f"  {rg['reagent_id']} <- {where}：仍存 {lot['pure_volume_ml']} mL，"
                  f"占该试剂 {lot['fraction']:.1%}")
```

---

## 逐步状态与浓度模型

采用透明的“**纯物体积**”簿记（体积可加、充分混合假设）：

- 每个试剂记录纯物体积 `pure_ml`，当前浓度 `= pure_ml / 液体总体积 × 100%`；
- **add**：`pure_ml += concentration × volume / 100`，总体积增加；
- **dilute**：仅总体积增加（加纯水），各组分浓度等比下降；**纯水不登记来源**；
- **pour**：按倒出比例同步缩减总体积与每个 `pure_ml`（浓度不变）；完全倒空会终止全部接触片段；
- **clean**：清空全部组分与总体积、所有接触计时归零，置 `cleaned=true`。

每个试剂的纯物量另按**来源批次**记账（与 `pure_ml` 同步增减）：初始液体 / 加液步骤 /
双容器初始（容器+试剂）各为独立批次；pour 与跨容器 transfer 按充分混合比例同步分摊，
clean 清空，dilute 不动；同键批次相遇合并、不同键分开。来源溯源接口在每条风险触发瞬间
对涉及试剂的批次存量做快照，详见[预检风险物料来源溯源](#预检风险物料来源溯源本轮新增)。

规则只在组分浓度**达到规则侧阈值**（默认 1%，强酸/强碱等规则更高）时生效，
痕量组分不会触发——稀释越过阈值会结束对应接触片段，重新越过则作为**新片段**重新计时。

## 操作类型

| `op` | 必填字段 | 语义 / 状态影响 |
| --- | --- | --- |
| `add` | `reagent_id`, `concentration`, `volume_ml` | 加入试剂；同一试剂多次加入会合并纯物量 |
| `dilute` | `volume_ml` | 加纯水稀释；**清洗后禁止**（旧状态已不存在） |
| `pour` | `volume_ml` | 均匀倒出，不得超过当前总体积；**清洗后/空容器禁止** |
| `clean` | — | 清洗：清空液体并重置接触计时 |
| `hold` | `at_seconds`（严格晚于当前时刻） | 静置等待，仅推进时间用于超时判定；**空容器/清洗后禁止** |

`at_seconds` 缺省沿用上一步时刻；时间只能前进，回退报 `NON_MONOTONIC_TIME`。

> 双容器转移预检（`/api/v1/transfer-precheck`）只支持 `transfer` 与 `hold` 两种操作，
> 语义见[双容器转移预检](#双容器转移预检本轮新增)。

## 规则、阈值与接触计时

三类规则（完整列表以 `GET /catalog` 为准）：

- `forbidden_mix`：两种试剂一经共存且双方都过阈值，**立即阻断**（F01–F08）；
- `limited_contact`：试剂-试剂限时接触，超时阻断（T01–T02）；
- `material_incompatible`：试剂-容器材质。无接触时长的（如 HF/玻璃）立即阻断（M01、M02）；
  带时长的（如强碱/玻璃 1800 s、酸/碳钢 600 s）超时阻断。

计时规则：

1. 接触“片段”从**双方（含材质）条件同时满足**的那一刻开始；
2. 时间在下一个操作的 `at_seconds` 推进，推进后检查 `已接触时长 > 允许时长`（严格大于）；
3. 倒空、清洗或任一 方浓度跌破阈值都会结束片段；重新满足则重新计时；
4. `contact_allowances` 按反应对（试剂-试剂 或 试剂-材质）覆盖该对所有限时规则的默认秒数；
   它**不能豁免禁配规则**，且必须为正数、两端为不同类的已知实体，重复声明会被拒绝。

## 错误码（逐项拒绝）

| Code | 触发条件 |
| --- | --- |
| `MALFORMED_REQUEST` | JSON/类型/schema 错误（含未知字段，请求模型禁止额外字段） |
| `DUPLICATE_STEP_SEQ` | 步骤序号重复 |
| `INVALID_SEQ` | 序号非正整数 |
| `UNKNOWN_OPERATION` | 未知 `op` |
| `UNKNOWN_REAGENT` / `UNKNOWN_MATERIAL` / `UNKNOWN_ENTITY` | 未知试剂/材质/allowance 实体 |
| `CONCENTRATION_OUT_OF_RANGE` | 浓度 ≤0、>100，或超过该试剂现实可出现上限（如盐酸 37%） |
| `INVALID_VOLUME` | add/dilute/pour 的体积缺失或 ≤0 |
| `INVALID_STEP_FIELDS` | 操作与字段不匹配（如 dilute 带 reagent_id、clean 带体积） |
| `DUPLICATE_COMPONENT` | 初始液体中同一试剂重复出现 |
| `TEMPERATURE_OUT_OF_RANGE` | 温度超出 [-50, 300] ℃ |
| `INVALID_TIME` / `NON_MONOTONIC_TIME` | 时刻为负、hold 未推进时间、时刻回退 |
| `POUR_EXCEEDS_VOLUME` | 倒出体积超过当前液体体积 |
| `EMPTY_VESSEL_OPERATION` | 容器为空仍 pour/hold |
| `STALE_STATE_REFERENCE` | **清洗后仍对旧状态做 pour/dilute/hold** |
| `INVALID_ALLOWANCE` / `DUPLICATE_ALLOWANCE` | 接触时长声明非法（非正/同实体）或重复 |

> 双容器转移预检另用 `INVALID_VESSEL` / `DUPLICATE_VESSEL_ID` / `UNKNOWN_VESSEL` /
> `SAME_VESSEL` / `EMPTY_SOURCE_VESSEL` / `TRANSFER_EXCEEDS_VOLUME`，详见
> [双容器转移预检](#双容器转移预检本轮新增)一节。

## 内置试剂 / 材质 / 规则

- 试剂 21 种：`h2so4, hcl, hno3, hf, acoh, naoh, koh, nh3, nacl, nabr, naocl, h2o2,
  kmno4, k2cr2o7, nh4cl, nh4no3, na2s, ethanol, acetone, formaldehyde, water`
- 材质 7 种：`glass, hdpe, ptfe, natural_rubber, aluminum, carbon_steel, stainless_steel`
- 规则 22 条（F01–F08 禁配、T01–T02 限时、M01–M12 材质），均以分类标签匹配，
  规则文本与版本号见 `app/rules.py` / `GET /catalog`。

## 调用示例（curl / Python）

```bash
# 安全示例：限时接触 + allowance 覆盖
curl -s http://localhost:8000/api/v1/precheck \
  -H 'Content-Type: application/json' \
  --data @examples/allowance_safe.json | python -m json.tool

# 双容器转移（禁配：盐酸从 acid-bottle 转入漂白水 bleach-tank → 氯气）
curl -s http://localhost:8000/api/v1/transfer-precheck \
  -H 'Content-Type: application/json' \
  --data @examples/transfer_forbidden.json | python -m json.tool
```

```python
import requests

payload = {
    "vessel_material": "glass",
    "temperature_c": 22.0,
    "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}],
    "steps": [
        {"seq": 1, "op": "add", "reagent_id": "hcl",
         "concentration": 30, "volume_ml": 50, "at_seconds": 30}
    ],
    "contact_allowances": [],
}
r = requests.post("http://localhost:8000/api/v1/precheck", json=payload, timeout=10)
r.raise_for_status()
result = r.json()
if result["blocked"]:
    f = result["findings"][0]
    print(f"阻断于步骤 {f['seq']}：{f['blocking_reason']}")
    print("涉及组分浓度：", f["concentrations"])
```

双容器转移预检（Python）：

```python
import requests

payload = {
    "vessels": [
        {"vessel_id": "A", "vessel_material": "hdpe",
         "initial_liquid": [{"reagent_id": "hcl", "concentration": 30, "volume_ml": 100}]},
        {"vessel_id": "B", "vessel_material": "hdpe",
         "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}]},
    ],
    "temperature_c": 22.0,
    "steps": [
        {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
         "volume_ml": 50, "at_seconds": 30}
    ],
    "contact_allowances": [],
}
r = requests.post("http://localhost:8000/api/v1/transfer-precheck", json=payload, timeout=10)
r.raise_for_status()
result = r.json()
if result["blocked"]:
    f = result["findings"][0]
    print(f"阻断于容器 {f['vessel_id']} 步骤 {f['seq']}：{f['blocking_reason']}")
else:
    for v in result["final_states"]:
        print(v["vessel_id"], v["liquid_volume_ml"], "mL")
```

也可直接查看 [`examples/`](examples/) 下的三个请求样例与一个响应样例。

### 配液核对（curl / Python）

```bash
# 单容器：原预检请求放入 request，另给统一容差与实测观测
curl -s http://localhost:8000/api/v1/reconcile \
  -H 'Content-Type: application/json' \
  --data @examples/reconcile_mix.json | python -m json.tool

# 双容器：每条观测另带 vessel_id
curl -s http://localhost:8000/api/v1/transfer-reconcile \
  -H 'Content-Type: application/json' \
  --data @examples/transfer_reconcile.json | python -m json.tool

# 无观测也合法：等价于只做一次原预检
curl -s http://localhost:8000/api/v1/reconcile \
  -H 'Content-Type: application/json' \
  -d '{"request": {"vessel_material": "hdpe", "temperature_c": 20, "steps": []},
       "tolerance_pp": 0.0, "observations": []}' | python -m json.tool
```

```python
import requests

original_request = {
    "vessel_material": "glass",
    "temperature_c": 22.0,
    "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}],
    "steps": [
        {"seq": 1, "op": "add", "reagent_id": "hcl",
         "concentration": 30, "volume_ml": 50, "at_seconds": 30}
    ],
    "contact_allowances": [],
}
payload = {
    "request": original_request,
    "tolerance_pp": 0.5,  # 百分点：|实测-预测| ≤ 0.5 才通过
    "observations": [
        {"at_initial": True, "reagent_id": "naocl", "concentration": 10.2},
        {"seq": 1, "reagent_id": "hcl", "concentration": 9.8},
        {"seq": 1, "reagent_id": "naocl", "concentration": 6.67},
        {"seq": 1, "reagent_id": "water", "concentration": 0.0},  # 未出现的已知试剂按 0
    ],
}
r = requests.post("http://localhost:8000/api/v1/reconcile", json=payload, timeout=10)
r.raise_for_status()
result = r.json()

# 原预检风险结果照常可见（即使 blocked=true，对账也照常给出）
print("blocked:", result["precheck"]["blocked"])
for ob in result["observations"]:
    print(f"[#{ob['observation_index']}] {ob['reagent_id']} @seq={ob['seq']}: "
          f"预测 {ob['predicted_concentration']:.6f}% / 实测 {ob['measured_concentration']}% "
          f"偏差 {ob['deviation']:.4f} -> {'通过' if ob['passed'] else '超差'}")
if result["first_out_of_tolerance"]:
    f = result["first_out_of_tolerance"]
    print("最早超差：观测", f["observation_index"], f["reagent_id"], "偏差", f["deviation"])
```

双容器只需改用 `/api/v1/transfer-reconcile`，并在每条观测里加 `"vessel_id"`：

```python
payload = {
    "request": {  # 与 POST /api/v1/transfer-precheck 的请求体完全相同
        "vessels": [
            {"vessel_id": "acid-bottle", "vessel_material": "hdpe",
             "initial_liquid": [{"reagent_id": "hcl", "concentration": 30, "volume_ml": 100}]},
            {"vessel_id": "bleach-tank", "vessel_material": "hdpe",
             "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}]},
        ],
        "temperature_c": 22.0,
        "steps": [
            {"seq": 1, "op": "transfer", "from_vessel": "acid-bottle",
             "to_vessel": "bleach-tank", "volume_ml": 50, "at_seconds": 30}
        ],
        "contact_allowances": [],
    },
    "tolerance_pp": 0.2,
    "observations": [
        {"at_initial": True, "vessel_id": "acid-bottle", "reagent_id": "hcl", "concentration": 29.9},
        {"seq": 1, "vessel_id": "bleach-tank", "reagent_id": "hcl", "concentration": 10.0},
        {"seq": 1, "vessel_id": "bleach-tank", "reagent_id": "naocl", "concentration": 6.6},
    ],
}
r = requests.post("http://localhost:8000/api/v1/transfer-reconcile", json=payload, timeout=10)
```

## 测试

```bash
pip install pytest httpx
python3 -m pytest -q
```

覆盖：禁配识别与估算浓度、初始液体触发（seq=0）、浓度阈值与稀释、超时接触与 allowance
覆盖、倒空/稀释对接触片段的重置、HF/玻璃等材质规则、清洗后旧状态引用拒绝、全部输入拒绝码、
乱序 seq 的稳定处理（单容器 35 个用例）；双容器转移另覆盖：比例扣减/计入簿记、源转空结束片段、
转入侧新片段计时、既有片段跨转移延续、材质/禁配在转入侧触发、初始状态两侧检查、
同刻按 seq 执行，以及全部转移拒绝码（`tests/test_transfer_api.py`，39 个用例）。
配液核对另覆盖：未四舍五入预测值对账、未出现已知试剂按零、边界容差、最早超差按执行顺序
（同位置按提交顺序）定位、乱序 seq 归一化、空观测仅返回预检、清洗后位置按零，
以及未知步骤/容器/试剂、重复观测、浓度越界、非法容差、原流程无效整单拒绝且无部分对账
（`tests/test_reconcile_api.py` 27 个、`tests/test_transfer_reconcile_api.py` 16 个用例）。
来源溯源另覆盖：初始/加液/双容器初始三类批次标识、同试剂分次加入分开、倒出与跨容器
转移按比例分摊、同键回流合并、稀释不产生来源、清洗清空、超时按该步操作前归属快照且
倒出/转空不改写已触发风险、材质风险标注触发容器与材质且不参与液体份额、无风险明细为空、
非法输入整单拒绝、原预检接口请求/响应逐字段兼容
（`tests/test_source_trace_api.py` 17 个、
`tests/test_transfer_source_trace_api.py` 13 个用例）。

## 项目结构

```
app/
  catalog.py            # 试剂 / 材质目录与标签
  rules.py              # 相容性规则库（禁配 / 限时 / 材质）与版本
  models.py             # 请求响应 Pydantic 模型（单容器 + 双容器）
  errors.py             # 错误码
  validator.py          # 单容器逐项校验 + 归一化执行计划
  transfer_validator.py # 双容器逐项校验（容器/转移/超量/空源/时序）+ 执行计划
  runtime.py            # 单只容器的状态机：浓度簿记、来源批次台账、接触片段对账与超时
  engine.py             # 单容器预检：重放计划、事件转 findings、快照
  transfer_engine.py    # 双容器预检：两侧 runtime + transfer/hold 重放
  responses.py          # 预检响应组装（预检端点与配液核对共用，保证 precheck 字段一致）
  reconcile.py          # 配液核对：原校验/模拟 + 未四舍五入预测浓度逐条对账、最早超差定位
  provenance.py         # 风险物料来源溯源：触发瞬间来源批次快照 -> risk_sources（单/双容器）
  main.py               # FastAPI 路由与异常映射
examples/                 # curl 可用的请求/响应样例（含 transfer_*、reconcile_*、*_source_trace_* 样例）
tests/test_api.py
tests/test_transfer_api.py
tests/test_reconcile_api.py
tests/test_transfer_reconcile_api.py
tests/test_source_trace_api.py
tests/test_transfer_source_trace_api.py
Dockerfile
docker-compose.yml
```

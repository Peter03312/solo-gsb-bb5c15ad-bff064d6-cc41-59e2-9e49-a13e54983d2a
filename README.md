# 实验室测量可信性追溯服务

基于 **Python 3.11 + FastAPI + SQLite** 的校准证书 / 测量读数可信性追溯 HTTP 服务。

- 校准证书：证书编号、设备编号、UTC **左闭右开**有效期 `[valid_from, valid_to)`、**闭区间**允许值范围 `[min_value, max_value]`。
- 测量读数：读数编号、设备编号、UTC 测量时间、数值。
- 每次成功变更返回**全局单调递增版本号**；所有写请求必须带 `request_id`（请求标识）。
- 证书只能**撤销**或由新证书版本**纠正**：纠正后旧版本在新版本起失效，历史只追加、永不覆盖。
- 读数按测量时间匹配**指定提交版本视角**下的有效证书：
  - 无匹配 → `unverified`（未验证）
  - 多份匹配 → `review`（待复核）
  - 唯一匹配且数值在闭区间内 → `conforming`（合格）
  - 唯一匹配但数值超出闭区间 → `out_of_range`（超限）
- 撤销 / 纠正会改变当前受影响读数的结论，并在响应中给出前后对照；旧版本的结论与所依证书仍可随时查询。
- **人工选证裁决**：处于 `review` 的读数可人工从候选证书中选定一个**证书编号 + 版本序号**进行裁决，成功裁决占用一个全局版本，按所选证书闭区间给出 `conforming` / `out_of_range`；所选版本日后被纠正或撤销时裁决自动失效，读数回到自动结论并标明裁决已失效（历史版本视角与取证包永不变）。
- **幂等**：相同 `request_id` + 相同内容重复提交不产生新版本（返回首次结果，`idempotent_replay=true`）；`request_id` 复用但内容不同返回 `409`；无效区间 / 未知目标返回 `422` / `404`，单事务保证不留下任何部分变更。

---

## 一、启动

### 方式 A：Docker Compose（推荐，一键启动并初始化数据库）

```bash
docker compose up -d --build
```

服务启动时会自动在数据卷中创建并初始化 SQLite 数据库（表不存在才建，幂等可重复执行）。

查看状态 / 日志 / 停止：

```bash
docker compose ps
docker compose logs -f
docker compose down            # 停止并删除容器，数据卷保留
docker compose down -v         # 同时删除数据库数据卷
```

数据库文件位于容器内 `/data/trace.db`，通过命名卷 `trace-data` 持久化。

### 方式 B：本地 Python 运行

```bash
pip install -r requirements.txt
export TRACE_DB_PATH=./data/trace.db   # 可选，默认即此值
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

> 需要 Python 3.11+。首次启动自动建库（会自动创建 `./data/` 目录）。

---

## 二、地址与端口

| 用途 | 地址 |
| --- | --- |
| 服务根地址 | <http://localhost:8080/> |
| 健康检查 | `GET http://localhost:8080/api/v1/health` |
| API 前缀 | `http://localhost:8080/api/v1` |
| Swagger 交互文档 | <http://localhost:8080/docs> |
| OpenAPI 描述 | <http://localhost:8080/openapi.json> |

---

## 三、配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TRACE_DB_PATH` | `./data/trace.db`（容器内 `/data/trace.db`） | SQLite 数据库文件路径 |
| `TRACE_HOST` | `0.0.0.0` | 监听地址 |
| `TRACE_PORT` | `8080` | 监听端口 |
| `TRACE_BUSY_TIMEOUT_MS` | `5000` | SQLite 写锁等待毫秒数 |
| `TRACE_MAX_PAGE_SIZE` | `200` | 列表接口单页最大条数 |

Compose 中可直接修改 `docker-compose.yml` 的 `environment` 与 `ports` 映射。

---

## 四、数据模型约定

- 所有时间必须**带时区**，服务端统一归一化为 UTC，例：`2026-01-01T00:00:00Z` 或 `2026-01-01T08:00:00+08:00`。
- 有效期：`valid_from <= measured_at < valid_to`（左闭右开，右边界时刻不匹配）。
- 允许值：`min_value <= value <= max_value`（闭区间，边界值合格）。
- 写请求体必须含非空 `request_id`（≤128 字符）。
- 错误响应统一格式：

  ```json
  { "error": { "code": "request_id_conflict", "message": "..." } }
  ```

---

## 五、调用示例

以下示例可直接用 `curl` 执行。

### 1. 签发校准证书

```bash
curl -s -X POST http://localhost:8080/api/v1/certificates \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "req-001",
    "certificate_no": "CERT-001",
    "device_no": "DEV-A",
    "valid_from": "2026-01-01T00:00:00Z",
    "valid_to":   "2026-07-01T00:00:00Z",
    "min_value": 0.0,
    "max_value": 100.0
  }'
```

返回：

```json
{
  "version": 1,
  "request_id": "req-001",
  "event_type": "cert_issue",
  "idempotent_replay": false,
  "certificate": {
    "certificate_no": "CERT-001", "version_seq": 1,
    "device_no": "DEV-A",
    "valid_from": "2026-01-01T00:00:00.000000Z",
    "valid_to":   "2026-07-01T00:00:00.000000Z",
    "min_value": 0.0, "max_value": 100.0,
    "created_at_version": 1, "superseded_at_version": null
  }
}
```

### 2. 提交读数（支持批量，立即返回当前版本视角下的结论）

```bash
curl -s -X POST http://localhost:8080/api/v1/readings \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "req-002",
    "readings": [
      {"reading_no": "M-1", "device_no": "DEV-A", "measured_at": "2026-03-01T10:00:00Z", "value": 42.0},
      {"reading_no": "M-2", "device_no": "DEV-A", "measured_at": "2026-03-01T11:00:00Z", "value": 140.0},
      {"reading_no": "M-3", "device_no": "DEV-B", "measured_at": "2026-03-01T12:00:00Z", "value": 1.0}
    ]
  }'
```

- `M-1` 唯一匹配且在范围内 → `conforming`
- `M-2` 唯一匹配但超范围 → `out_of_range`
- `M-3` 设备无证书覆盖 → `unverified`

### 3. 待复核：同一测量时刻有多份有效证书

再签发一份覆盖同一时刻的证书后，`M-1` 的当前结论变为 `review`，响应中 `candidates` 列出全部候选证书。

### 4. 纠正证书（新版本使旧版本失效）

```bash
curl -s -X POST http://localhost:8080/api/v1/certificates/CERT-001/correct \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "req-003",
    "certificate_no": "CERT-001",
    "device_no": "DEV-A",
    "valid_from": "2026-01-01T00:00:00Z",
    "valid_to":   "2026-07-01T00:00:00Z",
    "min_value": 50.0,
    "max_value": 100.0,
    "reason": "允许范围纠正为 50-100"
  }'
```

返回新版本号，`superseded_version_seq` 指向被失效的旧版本，`affected_readings` 给出相关设备上所有读数的前后结论对照（例如 `M-1：conforming -> out_of_range`）。旧版本内容仍保留在历史中。

### 5. 撤销证书

```bash
curl -s -X POST http://localhost:8080/api/v1/certificates/CERT-001/revoke \
  -H 'Content-Type: application/json' \
  -d '{"request_id": "req-004", "certificate_no": "CERT-001", "reason": "证书作废"}'
```

受影响读数结论随之改变（例如 `out_of_range -> unverified`）。撤销后不能再纠正，重复撤销返回 `409`。

### 6. 人工选证裁决（review 读数）

当读数处于 `review`（同一测量时刻存在多份候选证书）时，可人工选定其中一份**证书编号 + 版本序号**进行裁决。提交体包含 `request_id`、读数编号、所选证书编号及版本序号、**所依据的当前全局版本** `basis_version` 与理由。

```bash
# 先确认当前全局版本（裁决依据必须等于受理时的当前版本）
curl -s http://localhost:8080/api/v1/status

curl -s -X POST http://localhost:8080/api/v1/readings/M-1/adjudications \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "req-adj-001",
    "reading_no": "M-1",
    "certificate_no": "CERT-001",
    "certificate_version_seq": 1,
    "basis_version": 6,
    "reason": "经核对，采信 CERT-001 第 1 版"
  }'
```

返回（节选）：

```json
{
  "version": 7,
  "request_id": "req-adj-001",
  "event_type": "reading_adjudicate",
  "idempotent_replay": false,
  "reading_no": "M-1",
  "before": {
    "at_version": 6, "status": "review", "reliance": null,
    "candidates": [ {"certificate_no": "CERT-001", "...": "..."},
                    {"certificate_no": "CERT-002", "...": "..."} ]
  },
  "after": {
    "at_version": 7, "status": "conforming",
    "reliance": {"certificate_no": "CERT-001", "version_seq": 1, "...": "..."},
    "candidates": [ {"...": "裁决时刻的全部候选，原样保留..."} ],
    "adjudication": {
      "adjudicate_version": 7, "state": "active", "decision": "conforming",
      "certificate_no": "CERT-001", "certificate_version_seq": 1,
      "basis_version": 6, "reason": "经核对，采信 CERT-001 第 1 版",
      "invalidated_at_version": null
    }
  },
  "adjudication": { "...同 after.adjudication..." },
  "basis": {
    "basis_version": 6,
    "current_version_before": 6,
    "candidates": [ {"...": "依据版本视角下的全部候选证书快照..."} ],
    "reason": "经核对，采信 CERT-001 第 1 版"
  }
}
```

规则：

- **成功裁决占用一个全局版本**（事件类型 `reading_adjudicate`），不修改读数与证书，候选证书全部保留；`status` 按所选证书版本的**闭区间**允许值判定为 `conforming` / `out_of_range`。
- **依据版本校验**：`basis_version` 必须等于受理时刻的当前全局版本，否则 `409 stale_basis_version`（期间已有其他写入，请重新拉取后重试）。
- **候选校验**：所选 `(certificate_no, certificate_version_seq)` 必须是 `basis_version` 视角下该读数测量时刻的候选之一；版本不存在 → `422 unknown_certificate_version`，存在但不属于候选 → `422 certificate_not_candidate`。
- 读数未知 → `404 unknown_reading`；依据版本视角读数不是 `review`（如 `unverified` / `conforming` / `out_of_range`）→ `409 reading_not_in_review`；已有生效裁决且未失效 → `409 adjudication_exists`。
- **任何拒绝都在单事务内回滚，不产生新版本、不留半成品。**
- 相同 `request_id` + 相同内容重复提交沿用首次结果（`idempotent_replay=true`，版本不变）；相同 `request_id` 异内容 → `409 request_id_conflict`。

### 7. 裁决失效与历史可查

所选证书版本日后被**纠正**或**撤销**时，该裁决在相应事件版本起自动失效：

- 读数查询 / 列表的当前视角**回到原自动结论**，并在 `adjudication` 块中标明 `"state": "invalidated"` 与 `invalidated_at_version`；
- 纠正 / 撤销响应的 `affected_readings` 同步给出裁决失效导致的前后对照；
- 裁决失效后读数重新处于 `review` 时，可再次提交裁决（原裁决行保留）；
- **旧版本视角永远不变**：用 `at_version` 查询裁决当时或之前的版本，结论仍是裁决结论、`state=active`，失效标记按版本投影（`invalidated_at_version` 晚于查询版本时为 `null`）；
- 裁决流水可随时追溯：

```bash
curl -s http://localhost:8080/api/v1/readings/M-1/adjudications
```

读数查询与列表按版本呈现裁决：

```bash
# 当前视角（裁决生效时为裁决结论；失效后回自动结论并标 invalidated）
curl -s http://localhost:8080/api/v1/readings/M-1

# 裁决前的版本：纯自动 review，adjudication 为 null
curl -s "http://localhost:8080/api/v1/readings/M-1?at_version=6"

# 裁决版本：conforming / out_of_range + state=active
curl -s "http://localhost:8080/api/v1/readings/M-1?at_version=7"

# 列表同样支持按版本呈现，status 过滤作用于（叠加裁决后的）呈现结论
curl -s "http://localhost:8080/api/v1/readings?device_no=DEV-A&status=conforming"
```

取证包（下一节）按版本呈现裁决结论与依赖证书：**只有该版本视角存在裁决的读数条目才会多出 `adjudication` 键**，没有裁决的条目键集合与摘要规则完全不变；旧版本包的条目内容、`package_digest` / `segment_digest` 不随后续的纠正、撤销或新裁决改变。

### 8. 幂等重放与冲突

```bash
# 与步骤 1 完全相同的 request_id + 内容：不产生新版本，返回首次结果
curl -s -X POST http://localhost:8080/api/v1/certificates \
  -H 'Content-Type: application/json' -d '{ ...同步骤1... }'
# -> 200，"version": 1，"idempotent_replay": true

# 同一 request_id 但内容不同：
# -> 409 {"error":{"code":"request_id_conflict", ...}}，无任何数据落库
```

### 9. 查询

```bash
# 读数当前结论
curl -s http://localhost:8080/api/v1/readings/M-1

# 读数在历史版本视角下的结论（旧结论及所依证书仍可查）
curl -s "http://localhost:8080/api/v1/readings/M-1?at_version=2"

# 按设备 / 结论过滤读数
curl -s "http://localhost:8080/api/v1/readings?device_no=DEV-A&status=out_of_range"

# 读数的人工裁决流水（只增，含失效版本；不存在的读数 404）
curl -s http://localhost:8080/api/v1/readings/M-1/adjudications

# 证书当前版本
curl -s http://localhost:8080/api/v1/certificates/CERT-001

# 证书在指定版本视角下的内容
curl -s "http://localhost:8080/api/v1/certificates/CERT-001?at_version=1"

# 证书完整版本流水（不可变历史）
curl -s http://localhost:8080/api/v1/certificates/CERT-001/history

# 全局版本流水（每次成功变更一条）
curl -s "http://localhost:8080/api/v1/versions?limit=50"

# 按请求标识追溯原始请求与响应
curl -s http://localhost:8080/api/v1/requests/req-003

# 健康检查 / 当前版本
curl -s http://localhost:8080/api/v1/health
curl -s http://localhost:8080/api/v1/status
```

### 10. 审计取证追溯包

按**设备编号 + 带时区测量时间左闭右开区间 + 已提交版本**生成取证包（只读，不产生新版本）。包内只收录该版本**之前（含该版本）已提交**的读数，每条给出在该版本视角的结论 `status` 与**全部候选证书快照** `candidates`；若该读数在该版本视角存在人工裁决，结论与 `reliance` 按所选证书呈现，并附 `adjudication` 块（无裁决的条目不含该键，摘要规则不变）。

```bash
curl -s -G http://localhost:8080/api/v1/audit/packages \
  --data-urlencode 'device_no=DEV-A' \
  --data-urlencode 'measured_from=2026-01-01T00:00:00Z' \
  --data-urlencode 'measured_to=2026-07-01T00:00:00Z' \
  --data-urlencode 'at_version=5' \
  --data-urlencode 'limit=50' \
  --data-urlencode 'offset=0'
```

返回（节选）：

```json
{
  "package_id": "cf35d7dd...",
  "filter": {
    "device_no": "DEV-A",
    "measured_from": "2026-01-01T00:00:00.000000Z",
    "measured_to":   "2026-07-01T00:00:00.000000Z",
    "at_version": 5
  },
  "at_version": 5,
  "total_count": 3,
  "segment": {"limit": 50, "offset": 0, "count": 3, "has_more": false,
              "order": ["measured_at ASC", "reading_no ASC"]},
  "items": [
    {
      "reading_no": "M-1", "device_no": "DEV-A",
      "measured_at": "2026-03-01T10:00:00.000000Z", "value": 42.0,
      "submitted_at_version": 2,
      "status": "conforming",
      "reliance": { "certificate_no": "CERT-001", "version_seq": 1, "...": "..." },
      "candidates": [ { "certificate_no": "CERT-001", "version_seq": 1, "...": "..." } ],
      "adjudication": {
        "adjudicate_version": 7, "state": "active", "decision": "conforming",
        "certificate_no": "CERT-001", "certificate_version_seq": 1,
        "basis_version": 6, "reason": "...", "invalidated_at_version": null
      }
    }
  ],
  "digest": {
    "algorithm": "sha256",
    "spec_version": "audit-v1",
    "package_digest": "29773bbf...",
    "package_digest_input": { "canonical_json": "{...规范化后的完整包...}" },
    "segment_digest": "ab12...",
    "segment_digest_input": { "canonical_json": "{...本分段...}" },
    "canonical_rules": { "...": "随包返回的摘要规范化规则，可据其独立复算" }
  }
}
```

要点：

- **无匹配读数返回空包**（`200`，`total_count=0`、`items=[]`，摘要照常给出）；不存在的设备同样返回空包而非 404。
- **版本越界**（`at_version<1` 或 `>当前版本`）、**无效时间区间**（含缺时区、起点 ≥ 终点）一律 `422`，不读取数据、不产生半成品。
- 大量条目用 `limit`（≤200）/`offset` 分段；固定按 `(measured_at ASC, reading_no ASC)` 排序，拼接无遗漏、无重复。各分段的 `package_id` 与 `package_digest` 完全一致（包摘要始终覆盖全部 `total_count` 条），另给 `segment_digest` 校验本分段。
- 包生成在单一只读快照事务内完成；生成期间发生的新提交、证书纠正或撤销对该次取证不可见，且因只收录 `submitted_at_version <= at_version` 的读数、结论按 `at_version` 视角评估，**相同条件 + 版本的内容与摘要在任何时刻重取都一致**。
- **人工裁决按版本呈现**：裁决版本及之后的包中，该读数条目按所选证书给出 `status` / `reliance` 并附 `adjudication`（`state=active` 时覆盖自动结论；所选版本在取证版本前已被纠正/撤销时 `state=invalidated`，条目回到自动结论）；裁决版本之前的包不含该键。**无裁决条目的对象键集合与摘要规则完全不变，旧版本包（含其 `adjudication` 投影）不随后续操作改变**；摘要算法版本仍为 `audit-v1`，规则见随包返回的 `canonical_rules.adjudication_rules`。

独立复算摘要（任何语言均可）：

```bash
# package_digest = lower(hex(sha256(package_digest_input.canonical_json)))
python3 - <<'PY'
import hashlib, json, urllib.parse, urllib.request
q = urllib.parse.urlencode({
    "device_no": "DEV-A", "measured_from": "2026-01-01T00:00:00Z",
    "measured_to": "2026-07-01T00:00:00Z", "at_version": 5})
b = json.load(urllib.request.urlopen("http://localhost:8080/api/v1/audit/packages?" + q))
s = b["digest"]["package_digest_input"]["canonical_json"]
print("recomputed:", hashlib.sha256(s.encode("utf-8")).hexdigest())
print("matches:   ", hashlib.sha256(s.encode("utf-8")).hexdigest() == b["digest"]["package_digest"])
PY
```

---

## 六、HTTP 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/certificates` | 签发证书 |
| POST | `/api/v1/certificates/{cert_no}/correct` | 纠正证书（追加新版本，旧版本失效） |
| POST | `/api/v1/certificates/{cert_no}/revoke` | 撤销证书 |
| GET  | `/api/v1/certificates` | 证书列表（`at_version` / `device_no` / 分页） |
| GET  | `/api/v1/certificates/{cert_no}` | 证书当前/指定版本视角内容 |
| GET  | `/api/v1/certificates/{cert_no}/history` | 证书版本历史 |
| POST | `/api/v1/readings` | 批量提交读数（读数不可变） |
| POST | `/api/v1/readings/{reading_no}/adjudications` | 人工选证裁决（仅 review 读数；占一个全局版本） |
| GET  | `/api/v1/readings` | 读数列表（`at_version` / `device_no` / `status` / 分页；按版本呈现裁决） |
| GET  | `/api/v1/readings/{reading_no}` | 读数结论（默认当前版本，`at_version` 查历史；含裁决块） |
| GET  | `/api/v1/readings/{reading_no}/adjudications` | 读数的人工裁决流水（只增，含失效版本） |
| GET  | `/api/v1/versions` | 全局单调版本流水 |
| GET  | `/api/v1/requests/{request_id}` | 按请求标识追溯 |
| GET  | `/api/v1/audit/packages` | 审计取证追溯包（设备 + 左闭右开时间区间 + 版本） |
| GET  | `/api/v1/health` | 健康检查 |

主要状态码：`200` 成功；`404` 未知目标（证书 / 读数 / 请求）；`409` 请求标识冲突、证书已存在 / 已撤销、读数已存在、依据版本过期（`stale_basis_version`）、读数非 review（`reading_not_in_review`）、已有生效裁决（`adjudication_exists`）；`422` 参数校验失败（含无效时间区间、范围倒置、缺时区、所选证书版本不存在或非候选等）。**裁决被拒绝时不产生新版本。**

---

## 七、设计要点

- **单调整局版本**：`versions` 表自增主键，五类事件 `cert_issue / cert_correct / cert_revoke / reading_submit / reading_adjudicate` 全部追加，记录请求体、指纹与响应快照。
- **历史不可覆盖**：证书内容存为只追加的 `certificate_versions`，纠正 / 撤销仅在旧行登记 `superseded_at_version`；读数只追加；人工裁决存为只追加的 `adjudications`（裁决版本、所选证书编号+版本序号、依据版本、理由、失效版本）。
- **版本视角查询**：证书在版本 V 有效 ⇔ 其内容版本 `created_at_version <= V` 且（`superseded_at_version` 为空或 `> V`），且测量时刻落在 `[valid_from, valid_to)`。
- **人工裁决叠加**：仅在依据版本视角为 `review`（多候选）时受理，所选版本必须在候选集合内，结论由所选版本闭区间直接判定；裁决只在 `V >= adjudicate_version` 的视角生效。所选证书版本被纠正 / 撤销时在 `adjudications` 登记 `invalidated_at_version`，读数当前结论回到自动评估并标明 `state=invalidated`；更早版本视角投影为仍生效，旧结论与旧取证包永久可查。
- **事务与并发**：所有写操作在单条 `BEGIN IMMEDIATE` 事务内完成「幂等判定 → 业务校验 → 落库」，任何错误整体回滚（裁决的依据版本过期、非候选等拒绝均不占用版本号）；WAL 模式支持读写并发。
- **幂等指纹**：对归一化（UTC 化）后的请求体做排序 JSON 指纹；相同 `request_id` + 指纹返回首次响应，`request_id` 复用且指纹不同则拒绝。
- **旧库平滑升级**：启动建表幂等；旧版本库 `versions` 的 `event_type` CHECK 不含裁决事件时，自动在事务内重建该表（数据与自增版本序列保留），并补建 `adjudications` 表。
- **审计取证包**：`GET /audit/packages` 在单一只读快照事务（WAL `BEGIN` 读快照）内完成筛选与评估，事务开始后的提交一律不可见；逻辑上再叠加 `submitted_at_version <= at_version` 与测量时间 `[from, to)` 双重过滤。候选证书快照做版本视角投影——`superseded_at_version` 晚于取证版本时投影为 `null`，使历史版本包不携带"未来"事件；人工裁决块同样做版本投影（仅在该版本视角存在裁决时输出该键）。包摘要 `sha256(canonical_json(...))` 覆盖固定键集合下的全部条目（与分页无关），无裁决条目键集合保持不变、旧包摘要持续可复算，`package_id` 为筛选条件 + 版本规范化哈希的前 32 位；规范化规则（键排序、无空白、UTC 定长时间、binary64 最短往返数字、条目排序）随包返回，支持独立复算。

---

## 八、测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

测试通过 TestClient 覆盖：四种结论、左闭右开边界、纠正 / 撤销的结论联动、旧版本可追溯、幂等重放、标识冲突、无效区间、未知目标、批次部分变更防护、多时区归一化，以及人工选证裁决的成功受理与依据返回、各类拒绝（依据版本过期 / 非候选 / 非 review / 重复裁决）不产生新版本、幂等与冲突、纠正 / 撤销导致裁决失效并回到自动结论、按版本呈现裁决状态、取证包裁决呈现与旧包摘要恒定、旧库 CHECK 迁移。

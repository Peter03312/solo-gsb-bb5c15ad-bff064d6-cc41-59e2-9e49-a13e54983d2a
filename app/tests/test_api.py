"""端到端测试：通过 TestClient 覆盖全部业务规则。"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib

import pytest

# 必须在导入应用代码前设置数据库路径
DB_PATH = "/tmp/trace_test_%d.db" % os.getpid()
for suffix in ("", "-wal", "-shm"):
    p = pathlib.Path(DB_PATH + suffix)
    if p.exists():
        p.unlink()
os.environ["TRACE_DB_PATH"] = DB_PATH

from fastapi.testclient import TestClient  # noqa: E402

from app.config import reset_settings_cache  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    reset_settings_cache()
    with TestClient(app) as c:
        yield c


API = "/api/v1"


def test_01_issue_and_conforming_reading(client):
    # 签发证书：设备 D1，有效期 [2026-01-01, 2026-02-01)，允许值 [10.0, 20.0]
    r = client.post(f"{API}/certificates", json={
        "request_id": "req-cert-1",
        "certificate_no": "CERT-1",
        "device_no": "D1",
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": "2026-02-01T00:00:00Z",
        "min_value": 10.0,
        "max_value": 20.0,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["event_type"] == "cert_issue"

    # 提交合格读数
    r = client.post(f"{API}/readings", json={
        "request_id": "req-read-1",
        "readings": [{
            "reading_no": "R1", "device_no": "D1",
            "measured_at": "2026-01-15T08:00:00Z", "value": 15.5,
        }],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 2
    assert body["results"][0]["status"] == "conforming"
    assert body["results"][0]["reliance"]["certificate_no"] == "CERT-1"

    # 超限读数
    r = client.post(f"{API}/readings", json={
        "request_id": "req-read-2",
        "readings": [{
            "reading_no": "R2", "device_no": "D1",
            "measured_at": "2026-01-16T08:00:00Z", "value": 21.0,
        }],
    })
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["status"] == "out_of_range"

    # 未验证读数：时间在左闭右开区间外（右边界）
    r = client.post(f"{API}/readings", json={
        "request_id": "req-read-3",
        "readings": [{
            "reading_no": "R3", "device_no": "D1",
            "measured_at": "2026-02-01T00:00:00Z", "value": 15.0,
        }, {
            "reading_no": "R4", "device_no": "D2",
            "measured_at": "2026-01-10T00:00:00Z", "value": 1.0,
        }],
    })
    assert r.status_code == 200, r.text
    statuses = {x["reading_no"]: x["status"] for x in r.json()["results"]}
    assert statuses["R3"] == "unverified"
    assert statuses["R4"] == "unverified"


def test_02_half_open_boundary(client):
    # 左边界包含
    r = client.post(f"{API}/readings", json={
        "request_id": "req-read-boundary",
        "readings": [{
            "reading_no": "R-EDGE", "device_no": "D1",
            "measured_at": "2026-01-01T00:00:00Z", "value": 10.0,
        }],
    })
    assert r.json()["results"][0]["status"] == "conforming"
    # 闭区间边界值
    r = client.get(f"{API}/readings/R1")
    assert r.json()["status"] == "conforming"


def test_03_multiple_matches_review(client):
    # 为 D1 再签一份覆盖同一测量时刻的证书 -> 多份匹配待复核
    r = client.post(f"{API}/certificates", json={
        "request_id": "req-cert-2",
        "certificate_no": "CERT-2",
        "device_no": "D1",
        "valid_from": "2026-01-10T00:00:00Z",
        "valid_to": "2026-01-20T00:00:00Z",
        "min_value": 0.0,
        "max_value": 100.0,
    })
    assert r.status_code == 200
    # 事件序：CERT-1=v1, 三批读数=v2/v3/v4, 边界读数=v5, CERT-2=v6
    assert r.json()["version"] == 6

    r = client.get(f"{API}/readings/R1")
    body = r.json()
    assert body["status"] == "review"
    assert {c["certificate_no"] for c in body["candidates"]} == {"CERT-1", "CERT-2"}


def test_04_correct_certificate_changes_conclusions(client):
    # 纠正 CERT-2：把范围收紧，使 R1 唯一匹配时（后续撤销 CERT-1 前先纠正）
    # 这里直接纠正 CERT-1 的允许值范围，15.5 变为超限
    r = client.post(f"{API}/certificates/CERT-1/correct", json={
        "request_id": "req-cert-1-fix",
        "certificate_no": "CERT-1",
        "device_no": "D1",
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": "2026-02-01T00:00:00Z",
        "min_value": 16.0,
        "max_value": 20.0,
        "reason": "range corrected",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["superseded_version_seq"] == 1
    assert body["certificate"]["version_seq"] == 2
    fix_version = body["version"]

    # 受影响读数中应能看到 R1/R2 的变化记录（此刻多匹配仍可能为 review，校验结构存在）
    affected = {a["reading_no"]: a for a in body["affected_readings"]}
    assert "R1" in affected and "R2" in affected
    assert set(affected["R1"]["before"]) >= {"at_version", "status"}

    # 当前视角：R1 同时匹配两份仍为 review；撤销 CERT-2 后唯一匹配 CERT-1 v2 -> 超限
    r = client.post(f"{API}/certificates/CERT-2/revoke", json={
        "request_id": "req-cert-2-revoke",
        "certificate_no": "CERT-2",
        "reason": "duplicate calibration",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    revoke_version = body["version"]
    by_no = {a["reading_no"]: a for a in body["affected_readings"]}
    # R1: review -> out_of_range（15.5 不在 [16,20]）
    assert by_no["R1"]["before"]["status"] == "review"
    assert by_no["R1"]["after"]["status"] == "out_of_range"
    assert by_no["R1"]["conclusion_changed"] is True
    assert by_no["R1"]["after"]["reliance"]["version_seq"] == 2

    r = client.get(f"{API}/readings/R1")
    assert r.json()["status"] == "out_of_range"

    # 旧版本结论仍可查询：在纠正前的版本视角，R1 对 CERT-1 v1 合格（但彼时 CERT-2 未撤销仍 review）
    r = client.get(f"{API}/readings/R1", params={"at_version": fix_version - 1})
    old = r.json()
    assert old["at_version"] == fix_version - 1
    assert old["status"] == "review"  # CERT-1 与 CERT-2 均有效

    # 在读数提交当时（v2，CERT-2 尚未签发），R1 唯一匹配 CERT-1 v1，合格
    r = client.get(f"{API}/readings/R1", params={"at_version": 2})
    assert r.json()["status"] == "conforming"
    assert r.json()["reliance"]["version_seq"] == 1

    # 撤销后版本：明确超限
    r = client.get(f"{API}/readings/R1", params={"at_version": revoke_version})
    assert r.json()["status"] == "out_of_range"


def test_05_certificate_history_immutable(client):
    r = client.get(f"{API}/certificates/CERT-1/history")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["current_version_seq"] == 2
    assert len(body["versions"]) == 2
    v1, v2 = body["versions"]
    assert v1["version_seq"] == 1 and v1["superseded_at_version"] is not None
    assert v2["version_seq"] == 2 and v2["superseded_at_version"] is None
    # 旧版本内容没有被覆盖
    assert v1["min_value"] == 10.0 and v2["min_value"] == 16.0

    # at_version 视角能取到当时的证书内容
    r = client.get(f"{API}/certificates/CERT-1", params={"at_version": 1})
    one = r.json()
    assert one["version_seq"] == 1 and one["status"] == "active"
    r = client.get(f"{API}/certificates/CERT-1")
    cur = r.json()
    assert cur["version_seq"] == 2 and cur["status"] == "active"
    r = client.get(f"{API}/certificates/CERT-2")
    assert r.json()["status"] == "revoked"


def test_06_idempotency_same_request(client):
    payload = {
        "request_id": "req-cert-1",  # 与签发 CERT-1 完全相同
        "certificate_no": "CERT-1",
        "device_no": "D1",
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": "2026-02-01T00:00:00Z",
        "min_value": 10.0,
        "max_value": 20.0,
    }
    r = client.post(f"{API}/certificates", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["idempotent_replay"] is True
    assert body["version"] == 1

    before = client.get(f"{API}/status").json()["current_version"]
    r = client.post(f"{API}/readings", json={
        "request_id": "req-read-1",
        "readings": [{
            "reading_no": "R1", "device_no": "D1",
            "measured_at": "2026-01-15T08:00:00Z", "value": 15.5,
        }],
    })
    assert r.json()["idempotent_replay"] is True
    after = client.get(f"{API}/status").json()["current_version"]
    assert before == after


def test_07_request_id_reuse_conflict_and_invalid_inputs(client):
    # 同一 request_id 不同内容 -> 409
    r = client.post(f"{API}/certificates", json={
        "request_id": "req-cert-1",
        "certificate_no": "CERT-X",
        "device_no": "D9",
        "valid_from": "2026-03-01T00:00:00Z",
        "valid_to": "2026-04-01T00:00:00Z",
        "min_value": 0.0,
        "max_value": 1.0,
    })
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "request_id_conflict"
    # 冲突后未产生新版本
    assert client.get(f"{API}/certificates/CERT-X").status_code == 404

    # 无效区间：valid_from >= valid_to
    r = client.post(f"{API}/certificates", json={
        "request_id": "req-bad-1",
        "certificate_no": "CERT-BAD",
        "device_no": "D1",
        "valid_from": "2026-02-01T00:00:00Z",
        "valid_to": "2026-02-01T00:00:00Z",
        "min_value": 0.0,
        "max_value": 1.0,
    })
    assert r.status_code == 422

    # min > max
    r = client.post(f"{API}/certificates", json={
        "request_id": "req-bad-2",
        "certificate_no": "CERT-BAD",
        "device_no": "D1",
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": "2026-02-01T00:00:00Z",
        "min_value": 9.0,
        "max_value": 1.0,
    })
    assert r.status_code == 422
    # 失败不留痕
    assert client.get(f"{API}/certificates/CERT-BAD").status_code == 404

    # 无时区时间 -> 422
    r = client.post(f"{API}/certificates", json={
        "request_id": "req-bad-3",
        "certificate_no": "CERT-NAIVE",
        "device_no": "D1",
        "valid_from": "2026-01-01 00:00:00",
        "valid_to": "2026-02-01T00:00:00Z",
        "min_value": 0.0,
        "max_value": 1.0,
    })
    assert r.status_code == 422

    # 未知目标：纠正/撤销不存在的证书
    assert client.post(f"{API}/certificates/NOPE/correct", json={
        "request_id": "req-bad-4", "certificate_no": "NOPE", "device_no": "D1",
        "valid_from": "2026-01-01T00:00:00Z", "valid_to": "2026-02-01T00:00:00Z",
        "min_value": 0.0, "max_value": 1.0,
    }).status_code == 404
    assert client.post(f"{API}/certificates/NOPE/revoke", json={
        "request_id": "req-bad-5", "certificate_no": "NOPE",
    }).status_code == 404

    # 撤销已撤销证书 -> 409；纠正已撤销证书 -> 409
    assert client.post(f"{API}/certificates/CERT-2/revoke", json={
        "request_id": "req-double-revoke", "certificate_no": "CERT-2",
    }).status_code == 409
    assert client.post(f"{API}/certificates/CERT-2/correct", json={
        "request_id": "req-fix-revoked", "certificate_no": "CERT-2", "device_no": "D1",
        "valid_from": "2026-01-01T00:00:00Z", "valid_to": "2026-02-01T00:00:00Z",
        "min_value": 0.0, "max_value": 1.0,
    }).status_code == 409

    # 读数不可变：重复读数编号整批失败，不留部分变更
    r = client.post(f"{API}/readings", json={
        "request_id": "req-bad-read",
        "readings": [
            {"reading_no": "R1", "device_no": "D1",
             "measured_at": "2026-01-15T08:00:00Z", "value": 1.0},
            {"reading_no": "R-NEW", "device_no": "D1",
             "measured_at": "2026-01-15T09:00:00Z", "value": 1.0},
        ],
    })
    assert r.status_code == 409
    assert client.get(f"{API}/readings/R-NEW").status_code == 404

    # 批次内 reading_no 重复 -> 422
    r = client.post(f"{API}/readings", json={
        "request_id": "req-bad-read2",
        "readings": [
            {"reading_no": "X", "device_no": "D1",
             "measured_at": "2026-01-15T08:00:00Z", "value": 1.0},
            {"reading_no": "X", "device_no": "D1",
             "measured_at": "2026-01-15T09:00:00Z", "value": 2.0},
        ],
    })
    assert r.status_code == 422


def test_08_revoke_unverified_and_versions_log(client):
    # 撤销 CERT-1：R2（21.0）等由 out/review 变 unverified
    r = client.post(f"{API}/certificates/CERT-1/revoke", json={
        "request_id": "req-cert-1-revoke",
        "certificate_no": "CERT-1",
        "reason": "end of life",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["event_type"] == "cert_revoke"
    by_no = {a["reading_no"]: a for a in body["affected_readings"]}
    # R1: out_of_range -> unverified
    assert by_no["R1"]["before"]["status"] == "out_of_range"
    assert by_no["R1"]["after"]["status"] == "unverified"
    assert by_no["R1"]["conclusion_changed"] is True

    r = client.get(f"{API}/readings/R1")
    assert r.json()["status"] == "unverified"
    assert r.json()["reliance"] is None

    # 版本流水完整可查
    r = client.get(f"{API}/versions", params={"limit": 200})
    events = {e["event_type"] for e in r.json()["items"]}
    assert {"cert_issue", "cert_correct", "cert_revoke", "reading_submit"} <= events
    versions = [e["version"] for e in r.json()["items"]]
    assert versions == sorted(versions, reverse=True)

    # 请求追溯
    r = client.get(f"{API}/requests/req-cert-1-fix")
    assert r.status_code == 200
    assert r.json()["event_type"] == "cert_correct"


def test_09_lists_and_filters(client):
    r = client.get(f"{API}/certificates", params={"device_no": "D1"})
    nos = {c["certificate_no"] for c in r.json()["items"]}
    assert {"CERT-1", "CERT-2"} <= nos

    r = client.get(f"{API}/readings", params={"status": "unverified"})
    assert all(i["status"] == "unverified" for i in r.json()["items"])
    assert any(i["reading_no"] == "R1" for i in r.json()["items"])

    # at_version 越界
    assert client.get(f"{API}/readings/R1", params={"at_version": 99999}).status_code == 422


def test_10_timezone_normalization(client):
    # +08:00 时间应等价于对应 UTC：2026-01-15T16:00:00+08:00 == 08:00Z
    r = client.post(f"{API}/certificates", json={
        "request_id": "req-tz",
        "certificate_no": "CERT-TZ",
        "device_no": "DTZ",
        "valid_from": "2026-01-01T00:00:00+00:00",
        "valid_to": "2026-02-01T00:00:00+00:00",
        "min_value": 0.0,
        "max_value": 1.0,
    })
    assert r.status_code == 200, r.text
    assert r.json()["certificate"]["valid_from"] == "2026-01-01T00:00:00.000000Z"

    r = client.post(f"{API}/readings", json={
        "request_id": "req-tz-read",
        "readings": [{
            "reading_no": "RTZ", "device_no": "DTZ",
            "measured_at": "2026-01-15T16:00:00+08:00", "value": 0.5,
        }],
    })
    assert r.json()["results"][0]["status"] == "conforming"


# ---------------- 审计取证追溯包


def _canon(obj) -> str:
    """与服务端相同的规范化序列化，用于独立复算摘要。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _fetch_all_pages(client, params):
    """以固定 limit 翻页取全量，按返回顺序拼接条目与页码信息。"""
    page_size = 2
    seen = []
    pages = []
    offset = 0
    while True:
        body = client.get(f"{API}/audit/packages",
                          params={**params, "limit": page_size, "offset": offset}).json()
        pages.append(body)
        seen.extend(body["items"])
        if not body["segment"]["has_more"]:
            break
        offset += page_size
    return pages, seen


def test_11_audit_package_basic_and_digest(client):
    # 独立设备 DA：证书覆盖 2026-03，允许值 [0, 10]
    r = client.post(f"{API}/certificates", json={
        "request_id": "req-aud-cert",
        "certificate_no": "CERT-AUD", "device_no": "DA",
        "valid_from": "2026-03-01T00:00:00Z",
        "valid_to": "2026-04-01T00:00:00Z",
        "min_value": 0.0, "max_value": 10.0,
    })
    assert r.status_code == 200
    cert_v = r.json()["version"]

    r = client.post(f"{API}/readings", json={
        "request_id": "req-aud-reads",
        "readings": [
            {"reading_no": "A1", "device_no": "DA",
             "measured_at": "2026-03-02T00:00:00Z", "value": 5.0},
            {"reading_no": "A2", "device_no": "DA",
             "measured_at": "2026-03-03T00:00:00Z", "value": 50.0},
            {"reading_no": "A3", "device_no": "DA",  # 区间右边界之外
             "measured_at": "2026-04-01T00:00:00Z", "value": 5.0},
            {"reading_no": "A4", "device_no": "DA",  # 区间左边界之前
             "measured_at": "2026-02-28T23:59:59Z", "value": 5.0},
        ],
    })
    assert r.status_code == 200
    read_v = r.json()["version"]

    params = {
        "device_no": "DA",
        "measured_from": "2026-03-01T00:00:00Z",
        "measured_to": "2026-04-01T00:00:00Z",
        "at_version": read_v,
    }
    body = client.get(f"{API}/audit/packages", params=params).json()

    # 只收录区间内（左闭右开）且该版本前已提交的读数
    assert body["total_count"] == 2
    assert [i["reading_no"] for i in body["items"]] == ["A1", "A2"]
    # 排序稳定：measured_at 升序
    assert body["segment"]["order"] == ["measured_at ASC", "reading_no ASC"]

    by_no = {i["reading_no"]: i for i in body["items"]}
    assert by_no["A1"]["status"] == "conforming"
    assert by_no["A1"]["reliance"]["certificate_no"] == "CERT-AUD"
    assert by_no["A2"]["status"] == "out_of_range"
    # 每条读数列出全部候选证书快照
    assert [c["certificate_no"] for c in by_no["A1"]["candidates"]] == ["CERT-AUD"]
    for i in body["items"]:
        assert i["submitted_at_version"] == read_v

    # 包标识与条件、版本绑定
    expect_filter = {
        "device_no": "DA",
        "measured_from": "2026-03-01T00:00:00.000000Z",
        "measured_to": "2026-04-01T00:00:00.000000Z",
        "at_version": read_v,
    }
    assert body["filter"] == expect_filter
    expect_id = _sha(_canon(expect_filter))[:32]
    assert body["package_id"] == expect_id

    # 独立复算包摘要
    canonical_package = {
        "spec_version": body["digest"]["spec_version"],
        "filter": expect_filter,
        "at_version": read_v,
        "total_count": 2,
        "items": body["items"],
    }
    assert body["digest"]["package_digest"] == _sha(_canon(canonical_package))
    # 服务端给出的输入串与客户端规范化结果一致（可完全独立复算）
    assert body["digest"]["package_digest_input"]["canonical_json"] == _canon(canonical_package)
    # 规范化规则随包记录
    rules = body["digest"]["canonical_rules"]
    assert rules["hash_algorithm"] == "sha256"
    assert rules["spec_version"] == body["digest"]["spec_version"]
    assert "utf-8" in rules["text_encoding"]

    # 同条件同版本：内容与摘要恒定（先记住，撤销证书后再次拉取对比）
    digest_before = body["digest"]["package_digest"]
    segment_digest_before = body["digest"]["segment_digest"]
    items_before = body["items"]

    # 撤销证书（取证版本之后发生的新事件）
    r = client.post(f"{API}/certificates/CERT-AUD/revoke", json={
        "request_id": "req-aud-revoke",
        "certificate_no": "CERT-AUD",
        "reason": "audit scenario",
    })
    assert r.status_code == 200
    revoke_v = r.json()["version"]
    assert revoke_v > read_v

    again = client.get(f"{API}/audit/packages", params=params).json()
    # 不混入撤销：同一版本视角的条目、结论、候选快照与摘要完全一致
    assert again["digest"]["package_digest"] == digest_before
    assert again["digest"]["segment_digest"] == segment_digest_before
    assert again["items"] == items_before
    assert again["package_id"] == expect_id

    # 当前版本视角（撤销后）则结论改变，证明 as-of 隔离确实生效
    cur = client.get(f"{API}/audit/packages", params={**params, "at_version": revoke_v}).json()
    assert cur["digest"]["package_digest"] != digest_before
    assert {i["reading_no"]: i["status"] for i in cur["items"]} == {
        "A1": "unverified", "A2": "unverified",
    }
    assert cur["items"][0]["candidates"] == []

    # at_version 早于证书签发：读数当时已提交但无证书可见 -> 空候选 unverified
    early = client.get(f"{API}/audit/packages",
                       params={**params, "at_version": cert_v}).json()
    # cert_v 时读数尚未提交（submitted_at_version=read_v > cert_v）-> 空包
    assert early["total_count"] == 0 and early["items"] == []


def test_12_audit_version_isolation_readings_committed_after_excluded(client):
    # 先取 read_v 版本的包，再在区间内补提新读数、纠正证书；旧版本包不得混入
    r = client.post(f"{API}/readings", json={
        "request_id": "req-aud-late-read",
        "readings": [{
            "reading_no": "A5", "device_no": "DA",
            "measured_at": "2026-03-04T00:00:00Z", "value": 5.0,
        }],
    })
    assert r.status_code == 200
    # 证书已撤销 -> 当前视角 unverified；但旧版本包不受影响
    status = client.get(f"{API}/status").json()["current_version"]

    # 找到 test_11 的读数提交版本（A1 详情）
    a1 = client.get(f"{API}/readings/A1").json()
    read_v = a1["submitted_at_version"]
    params = {
        "device_no": "DA",
        "measured_from": "2026-03-01T00:00:00Z",
        "measured_to": "2026-04-01T00:00:00Z",
        "at_version": read_v,
    }
    body = client.get(f"{API}/audit/packages", params=params).json()
    assert body["total_count"] == 2
    assert "A5" not in {i["reading_no"] for i in body["items"]}
    assert status > read_v  # 数据库确有更新版本，证明是 as-of 过滤而非没有新提交


def test_13_audit_pagination_stable_concat_and_segment_digest(client):
    # 再制造多条同设备区间内读数用于翻页（先重新签发证书，让读数有候选）
    r = client.post(f"{API}/certificates", json={
        "request_id": "req-aud-cert2",
        "certificate_no": "CERT-AUD2", "device_no": "DA",
        "valid_from": "2026-03-01T00:00:00Z",
        "valid_to": "2026-04-01T00:00:00Z",
        "min_value": 0.0, "max_value": 10.0,
    })
    cert2_v = r.json()["version"]
    r = client.post(f"{API}/readings", json={
        "request_id": "req-aud-pages",
        "readings": [
            {"reading_no": "P1", "device_no": "DA",
             "measured_at": "2026-03-05T00:00:00Z", "value": 1.0},
            {"reading_no": "P2", "device_no": "DA",
             "measured_at": "2026-03-05T00:00:00Z", "value": 2.0},  # 同一时刻按 reading_no
            {"reading_no": "P3", "device_no": "DA",
             "measured_at": "2026-03-06T00:00:00Z", "value": 3.0},
        ],
    })
    pages_v = r.json()["version"]

    params = {
        "device_no": "DA",
        "measured_from": "2026-03-01T00:00:00Z",
        "measured_to": "2026-04-01T00:00:00Z",
        "at_version": pages_v,
    }
    pages, concat_items = _fetch_all_pages(client, params)

    full = client.get(f"{API}/audit/packages",
                      params={**params, "limit": 200, "offset": 0}).json()

    # 无遗漏、无重复、顺序稳定
    assert full["total_count"] == len(concat_items)
    nos = [i["reading_no"] for i in concat_items]
    assert len(nos) == len(set(nos))
    assert concat_items == full["items"]
    # 同一时刻按 reading_no 次序稳定
    assert nos.index("P1") < nos.index("P2") < nos.index("P3")

    # 各分段 package_id / package_digest 完全一致（与分页无关）
    assert len(pages) >= 2
    assert {p["package_id"] for p in pages} == {full["package_id"]}
    assert {p["digest"]["package_digest"] for p in pages} == {full["digest"]["package_digest"]}

    # 每段的 segment_digest 可按规范化规则独立复算
    for p in pages:
        seg_input = {
            "spec_version": p["digest"]["spec_version"],
            "package_id": p["package_id"],
            "package_digest": p["digest"]["package_digest"],
            "at_version": p["at_version"],
            "limit": p["segment"]["limit"],
            "offset": p["segment"]["offset"],
            "items": p["items"],
        }
        assert p["digest"]["segment_digest"] == _sha(_canon(seg_input))

    # 再次翻页：分段摘要同样稳定（新提交不影响历史版本包）
    pages2, _ = _fetch_all_pages(client, params)
    assert [p["digest"]["segment_digest"] for p in pages2] == [
        p["digest"]["segment_digest"] for p in pages
    ]


def test_14_audit_empty_package_has_digest(client):
    # 设备存在但区间无读数：空包，摘要照常且可复算
    params = {
        "device_no": "DA",
        "measured_from": "2030-01-01T00:00:00Z",
        "measured_to": "2030-02-01T00:00:00Z",
        "at_version": client.get(f"{API}/status").json()["current_version"],
    }
    body = client.get(f"{API}/audit/packages", params=params).json()
    assert body["total_count"] == 0 and body["items"] == []
    assert body["segment"]["has_more"] is False
    canonical_package = {
        "spec_version": body["digest"]["spec_version"],
        "filter": body["filter"],
        "at_version": body["at_version"],
        "total_count": 0,
        "items": [],
    }
    assert body["digest"]["package_digest"] == _sha(_canon(canonical_package))

    # 从未出现过的设备同样是空包（不是 404）
    body2 = client.get(f"{API}/audit/packages", params={**params, "device_no": "NO-SUCH-DEV"}).json()
    assert body2["total_count"] == 0 and body2["package_id"] != body["package_id"]


def test_15_audit_half_open_and_timezone_inputs(client):
    # 取一个已知版本
    v = client.get(f"{API}/status").json()["current_version"]
    base = {
        "device_no": "DA",
        "at_version": v,
    }
    # 右边界开区间：2026-04-01T00:00:00Z 时刻的读数不收录
    b1 = client.get(f"{API}/audit/packages", params={
        **base,
        "measured_from": "2026-03-01T00:00:00Z",
        "measured_to": "2026-04-01T00:00:00Z",
    }).json()
    b2 = client.get(f"{API}/audit/packages", params={
        **base,
        "measured_from": "2026-03-01T08:00:00+08:00",   # == 2026-03-01T00:00:00Z
        "measured_to": "2026-04-01T08:00:00+08:00",     # == 2026-04-01T00:00:00Z
    }).json()
    # 时区写法不同但归一化后等价：包与摘要一致
    assert b2["filter"] == b1["filter"]
    assert b2["package_id"] == b1["package_id"]
    assert b2["digest"]["package_digest"] == b1["digest"]["package_digest"]
    assert all(i["measured_at"] < b1["filter"]["measured_to"] for i in b1["items"])


def test_16_audit_errors(client):
    latest = client.get(f"{API}/status").json()["current_version"]

    # 版本越界
    r = client.get(f"{API}/audit/packages", params={
        "device_no": "DA",
        "measured_from": "2026-03-01T00:00:00Z",
        "measured_to": "2026-04-01T00:00:00Z",
        "at_version": latest + 1,
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_version"

    r = client.get(f"{API}/audit/packages", params={
        "device_no": "DA",
        "measured_from": "2026-03-01T00:00:00Z",
        "measured_to": "2026-04-01T00:00:00Z",
        "at_version": 0,
    })
    assert r.status_code == 422  # Query(ge=1)

    # 无效时间区间：起点 == 终点（零宽度）
    r = client.get(f"{API}/audit/packages", params={
        "device_no": "DA",
        "measured_from": "2026-03-01T00:00:00Z",
        "measured_to": "2026-03-01T00:00:00Z",
        "at_version": latest,
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_time_range"

    # 起点晚于终点
    r = client.get(f"{API}/audit/packages", params={
        "device_no": "DA",
        "measured_from": "2026-04-01T00:00:00Z",
        "measured_to": "2026-03-01T00:00:00Z",
        "at_version": latest,
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_time_range"

    # 缺时区
    r = client.get(f"{API}/audit/packages", params={
        "device_no": "DA",
        "measured_from": "2026-03-01 00:00:00",
        "measured_to": "2026-04-01T00:00:00Z",
        "at_version": latest,
    })
    assert r.status_code == 422

    # 缺必填参数
    r = client.get(f"{API}/audit/packages", params={
        "device_no": "DA",
        "measured_from": "2026-03-01T00:00:00Z",
        "measured_to": "2026-04-01T00:00:00Z",
    })
    assert r.status_code == 422

    # 报错后没有任何半成品：服务仍可用、当前版本不变
    assert client.get(f"{API}/status").json()["current_version"] == latest


# ---------------- 人工选证裁决（reading_adjudicate）


def _setup_review(client, req_prefix, certs, device_no="DJ", reading_no="J1",
                  measured_at="2026-05-01T00:00:00Z", value=5.0):
    """签发多份覆盖同一时刻的证书并提交读数，返回 (读数提交版本, 各证书版本信息)。"""
    cert_versions = {}
    for i, (cno, lo, hi) in enumerate(certs):
        r = client.post(f"{API}/certificates", json={
            "request_id": f"{req_prefix}-cert-{i}",
            "certificate_no": cno,
            "device_no": device_no,
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_to": "2027-01-01T00:00:00Z",
            "min_value": lo,
            "max_value": hi,
        })
        assert r.status_code == 200, r.text
        cert_versions[cno] = r.json()["version"]
    r = client.post(f"{API}/readings", json={
        "request_id": f"{req_prefix}-read",
        "readings": [{
            "reading_no": reading_no, "device_no": device_no,
            "measured_at": measured_at, "value": value,
        }],
    })
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["status"] == "review"
    return r.json()["version"]


def test_17_adjudication_success_overrides_review(client):
    read_v = _setup_review(
        client, "req-j",
        [("CERT-JA", 0.0, 10.0), ("CERT-JB", 100.0, 200.0)],
        device_no="DJ", reading_no="J1", value=5.0,
    )
    cur = client.get(f"{API}/status").json()["current_version"]
    assert cur == read_v

    r = client.post(f"{API}/readings/J1/adjudications", json={
        "request_id": "req-j-adj",
        "reading_no": "J1",
        "certificate_no": "CERT-JA",
        "certificate_version_seq": 1,
        "basis_version": cur,
        "reason": "采信 CERT-JA",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["event_type"] == "reading_adjudicate"
    assert body["version"] == cur + 1  # 成功裁决占用一个全局版本
    # 裁决依据：裁决前为 review 且保留全部候选
    assert body["before"]["at_version"] == cur
    assert body["before"]["status"] == "review"
    assert {c["certificate_no"] for c in body["before"]["candidates"]} == {
        "CERT-JA", "CERT-JB",
    }
    assert body["basis"]["basis_version"] == cur
    assert body["basis"]["current_version_before"] == cur
    assert {c["certificate_no"] for c in body["basis"]["candidates"]} == {
        "CERT-JA", "CERT-JB",
    }
    assert body["basis"]["reason"] == "采信 CERT-JA"
    # 裁决后：按所选证书闭区间合格，reliance 指向所选，候选仍全部保留
    assert body["after"]["status"] == "conforming"
    assert body["after"]["reliance"]["certificate_no"] == "CERT-JA"
    assert body["after"]["reliance"]["version_seq"] == 1
    assert {c["certificate_no"] for c in body["after"]["candidates"]} == {
        "CERT-JA", "CERT-JB",
    }
    adj = body["adjudication"]
    assert adj["state"] == "active"
    assert adj["decision"] == "conforming"
    assert adj["adjudicate_version"] == cur + 1
    assert adj["basis_version"] == cur
    assert adj["invalidated_at_version"] is None

    # 版本流水包含新事件；请求可追溯
    events = client.get(f"{API}/versions", params={"limit": 200}).json()["items"]
    assert any(e["event_type"] == "reading_adjudicate" for e in events)
    rq = client.get(f"{API}/requests/req-j-adj")
    assert rq.json()["event_type"] == "reading_adjudicate"

    # 选允许范围外的候选 -> out_of_range 裁决
    read_v2 = _setup_review(
        client, "req-k",
        [("CERT-KA", 0.0, 10.0), ("CERT-KB", 100.0, 200.0)],
        device_no="DK", reading_no="K1", value=5.0,
    )
    r = client.post(f"{API}/readings/K1/adjudications", json={
        "request_id": "req-k-adj",
        "reading_no": "K1",
        "certificate_no": "CERT-KB",
        "certificate_version_seq": 1,
        "basis_version": read_v2,
    })
    assert r.status_code == 200, r.text
    assert r.json()["adjudication"]["decision"] == "out_of_range"
    assert r.json()["after"]["status"] == "out_of_range"
    assert r.json()["after"]["reliance"]["certificate_no"] == "CERT-KB"


def test_18_adjudication_rejections_consume_no_version(client):
    read_v = _setup_review(
        client, "req-l",
        [("CERT-LA", 0.0, 10.0), ("CERT-LB", 100.0, 200.0)],
        device_no="DL", reading_no="L1", value=5.0,
    )

    # 未知读数 -> 404
    r = client.post(f"{API}/readings/NOPE/adjudications", json={
        "request_id": "req-l-404", "reading_no": "NOPE",
        "certificate_no": "CERT-LA", "certificate_version_seq": 1,
        "basis_version": read_v,
    })
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_reading"

    # 路径与正文读数不一致 -> 422
    r = client.post(f"{API}/readings/L1/adjudications", json={
        "request_id": "req-l-path", "reading_no": "OTHER",
        "certificate_no": "CERT-LA", "certificate_version_seq": 1,
        "basis_version": read_v,
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "path_body_mismatch"

    # 依据版本过期 -> 409
    r = client.post(f"{API}/readings/L1/adjudications", json={
        "request_id": "req-l-stale", "reading_no": "L1",
        "certificate_no": "CERT-LA", "certificate_version_seq": 1,
        "basis_version": read_v - 1,
    })
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_basis_version"

    # 所选证书版本不存在 -> 422
    r = client.post(f"{API}/readings/L1/adjudications", json={
        "request_id": "req-l-nocert", "reading_no": "L1",
        "certificate_no": "CERT-LA", "certificate_version_seq": 99,
        "basis_version": read_v,
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "unknown_certificate_version"

    # 所选证书存在但不是该读数测量时刻的候选（不相关设备上的证书）
    # CERT-TZ 属于设备 DTZ，覆盖 2026-01，不覆盖 L1（设备 DJ）
    r = client.post(f"{API}/readings/L1/adjudications", json={
        "request_id": "req-l-notcand", "reading_no": "L1",
        "certificate_no": "CERT-TZ", "certificate_version_seq": 1,
        "basis_version": read_v,
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "certificate_not_candidate"

    # 以上全部拒绝均不产生新版本
    assert client.get(f"{API}/status").json()["current_version"] == read_v

    # 非 review 读数不能裁决：单证书设备 DM 上自动 conforming 的读数
    r = client.post(f"{API}/certificates", json={
        "request_id": "req-l-cert-m", "certificate_no": "CERT-MA", "device_no": "DM",
        "valid_from": "2026-01-01T00:00:00Z", "valid_to": "2027-01-01T00:00:00Z",
        "min_value": 0.0, "max_value": 10.0,
    })
    assert r.status_code == 200
    r = client.post(f"{API}/readings", json={
        "request_id": "req-l-read-m",
        "readings": [{
            "reading_no": "M9", "device_no": "DM",
            "measured_at": "2026-05-02T00:00:00Z", "value": 5.0,
        }],
    })
    readm_v = r.json()["version"]
    assert r.json()["results"][0]["status"] == "conforming"
    r = client.post(f"{API}/readings/M9/adjudications", json={
        "request_id": "req-l-adj-m", "reading_no": "M9",
        "certificate_no": "CERT-MA", "certificate_version_seq": 1,
        "basis_version": readm_v,
    })
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "reading_not_in_review"
    assert client.get(f"{API}/status").json()["current_version"] == readm_v


def test_19_adjudication_idempotency_and_conflict(client):
    read_v = _setup_review(
        client, "req-n",
        [("CERT-NA", 0.0, 10.0), ("CERT-NB", 100.0, 200.0)],
        device_no="DN", reading_no="N1", value=5.0,
    )
    payload = {
        "request_id": "req-n-adj", "reading_no": "N1",
        "certificate_no": "CERT-NA", "certificate_version_seq": 1,
        "basis_version": read_v, "reason": "first",
    }
    r = client.post(f"{API}/readings/N1/adjudications", json=payload)
    assert r.status_code == 200
    adj_v = r.json()["version"]

    # 完全相同 request_id + 内容：沿用首次结果，不产生新版本
    r = client.post(f"{API}/readings/N1/adjudications", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["idempotent_replay"] is True
    assert body["version"] == adj_v
    assert client.get(f"{API}/status").json()["current_version"] == adj_v

    # 同 request_id 异内容 -> 409，不留新版本
    r = client.post(f"{API}/readings/N1/adjudications", json={
        **payload, "certificate_no": "CERT-NB",
    })
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "request_id_conflict"
    assert client.get(f"{API}/status").json()["current_version"] == adj_v

    # 生效裁决未失效前再次裁决（新 request_id）-> 409
    r = client.post(f"{API}/readings/N1/adjudications", json={
        "request_id": "req-n-adj-again", "reading_no": "N1",
        "certificate_no": "CERT-NB", "certificate_version_seq": 1,
        "basis_version": adj_v,
    })
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "adjudication_exists"
    assert client.get(f"{API}/status").json()["current_version"] == adj_v


def test_20_adjudication_invalidated_by_correct_and_revoke(client):
    # 场景：裁决选 CERT-OA；纠正 OA 使裁决失效（回到 review），再撤销 OB 后重新裁决选 OA v2，
    # 再撤销 OA 使第二次裁决失效（自动结论回到 out_of_range）。
    read_v = _setup_review(
        client, "req-o",
        [("CERT-OA", 0.0, 10.0), ("CERT-OB", 100.0, 200.0)],
        device_no="DO", reading_no="O1", value=5.0,
    )
    r = client.post(f"{API}/readings/O1/adjudications", json={
        "request_id": "req-o-adj1", "reading_no": "O1",
        "certificate_no": "CERT-OA", "certificate_version_seq": 1,
        "basis_version": read_v,
    })
    assert r.status_code == 200
    adj1_v = r.json()["version"]
    assert r.json()["after"]["status"] == "conforming"

    # 裁决前历史视角：纯自动 review 且无裁决块
    old = client.get(f"{API}/readings/O1", params={"at_version": read_v}).json()
    assert old["status"] == "review" and old["adjudication"] is None

    # 裁决视角：conforming + active
    at_adj = client.get(f"{API}/readings/O1", params={"at_version": adj1_v}).json()
    assert at_adj["status"] == "conforming"
    assert at_adj["adjudication"]["state"] == "active"

    # 纠正 CERT-OA：旧版本失效 -> 裁决失效；新版本与 OB 仍构成多匹配 -> review
    r = client.post(f"{API}/certificates/CERT-OA/correct", json={
        "request_id": "req-o-fix", "certificate_no": "CERT-OA", "device_no": "DO",
        "valid_from": "2026-01-01T00:00:00Z", "valid_to": "2027-01-01T00:00:00Z",
        "min_value": 0.0, "max_value": 9.0, "reason": "tighten",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    fix_v = body["version"]
    affected = {a["reading_no"]: a for a in body["affected_readings"]}
    assert affected["O1"]["before"]["status"] == "conforming"
    assert affected["O1"]["after"]["status"] == "review"
    assert affected["O1"]["after"]["adjudication"]["state"] == "invalidated"

    cur = client.get(f"{API}/readings/O1").json()
    assert cur["status"] == "review"
    assert cur["adjudication"]["state"] == "invalidated"
    assert cur["adjudication"]["invalidated_at_version"] == fix_v

    # 旧裁决版本视角永久保持 active（as-of 投影：失效版本在未来不可见）
    old_adj = client.get(f"{API}/readings/O1", params={"at_version": adj1_v}).json()
    assert old_adj["status"] == "conforming"
    assert old_adj["adjudication"]["state"] == "active"
    assert old_adj["adjudication"]["invalidated_at_version"] is None

    # 失效后允许重新裁决：选 OA v2（5.0 在 [0,9] -> conforming）
    r = client.post(f"{API}/readings/O1/adjudications", json={
        "request_id": "req-o-adj2", "reading_no": "O1",
        "certificate_no": "CERT-OA", "certificate_version_seq": 2,
        "basis_version": fix_v,
    })
    assert r.status_code == 200, r.text
    adj2_v = r.json()["version"]
    assert r.json()["after"]["status"] == "conforming"

    # 撤销 CERT-OA -> 第二次裁决失效；只剩 OB 唯一匹配，5.0 超 [100,200] -> out_of_range
    r = client.post(f"{API}/certificates/CERT-OA/revoke", json={
        "request_id": "req-o-rev", "certificate_no": "CERT-OA", "reason": "obsolete",
    })
    assert r.status_code == 200, r.text
    rev_v = r.json()["version"]
    affected = {a["reading_no"]: a for a in r.json()["affected_readings"]}
    assert affected["O1"]["before"]["status"] == "conforming"
    assert affected["O1"]["after"]["status"] == "out_of_range"
    assert affected["O1"]["after"]["adjudication"]["state"] == "invalidated"

    cur = client.get(f"{API}/readings/O1").json()
    assert cur["status"] == "out_of_range"
    assert cur["reliance"]["certificate_no"] == "CERT-OB"
    assert cur["adjudication"]["state"] == "invalidated"
    assert cur["adjudication"]["invalidated_at_version"] == rev_v

    # 两个历史裁决版本视角各自保持当时结论
    a1 = client.get(f"{API}/readings/O1", params={"at_version": adj1_v}).json()
    a2 = client.get(f"{API}/readings/O1", params={"at_version": adj2_v}).json()
    assert a1["status"] == "conforming" and a1["adjudication"]["state"] == "active"
    assert a2["status"] == "conforming" and a2["adjudication"]["state"] == "active"
    assert a1["adjudication"]["certificate_version_seq"] == 1
    assert a2["adjudication"]["certificate_version_seq"] == 2

    # 裁决流水只增：两次裁决均可查，第一次在纠正版本失效、第二次在撤销版本失效
    h = client.get(f"{API}/readings/O1/adjudications").json()
    assert h["total"] == 2
    assert h["items"][0]["certificate_version_seq"] == 1
    assert h["items"][0]["state"] == "invalidated"
    assert h["items"][0]["invalidated_at_version"] == fix_v
    assert h["items"][1]["certificate_version_seq"] == 2
    assert h["items"][1]["invalidated_at_version"] == rev_v
    assert client.get(f"{API}/readings/NO-SUCH/adjudications").status_code == 404


def test_21_reading_query_and_list_show_adjudication_by_version(client):
    read_v = _setup_review(
        client, "req-p",
        [("CERT-PA", 0.0, 10.0), ("CERT-PB", 100.0, 200.0)],
        device_no="DP", reading_no="U1", value=5.0,
    )
    r = client.post(f"{API}/readings/U1/adjudications", json={
        "request_id": "req-p-adj", "reading_no": "U1",
        "certificate_no": "CERT-PA", "certificate_version_seq": 1,
        "basis_version": read_v,
    })
    adj_v = r.json()["version"]

    # 列表（当前版本）：conforming 过滤能看到，review 过滤看不到
    conf = client.get(f"{API}/readings", params={"status": "conforming"}).json()["items"]
    assert any(i["reading_no"] == "U1" and i["adjudication"]["state"] == "active" for i in conf)
    rev = client.get(f"{API}/readings", params={"status": "review"}).json()["items"]
    assert not any(i["reading_no"] == "U1" for i in rev)

    # 列表在裁决前版本视角：U1 仍按 review 呈现且无裁决块
    old = client.get(f"{API}/readings", params={"at_version": read_v}).json()["items"]
    p1_old = next(i for i in old if i["reading_no"] == "U1")
    assert p1_old["status"] == "review" and p1_old["adjudication"] is None


def test_22_audit_package_presents_adjudication_and_keeps_old_digest(client):
    read_v = _setup_review(
        client, "req-q",
        [("CERT-QA", 0.0, 10.0), ("CERT-QB", 100.0, 200.0)],
        device_no="DQ", reading_no="Q1", value=5.0,
    )
    params = {
        "device_no": "DQ",
        "measured_from": "2026-01-01T00:00:00Z",
        "measured_to": "2027-01-01T00:00:00Z",
        "at_version": read_v,
    }
    before = client.get(f"{API}/audit/packages", params=params).json()
    item_before = before["items"][0]
    assert item_before["status"] == "review"
    assert "adjudication" not in item_before  # 无裁决：旧 audit-v1 键集合不变

    r = client.post(f"{API}/readings/Q1/adjudications", json={
        "request_id": "req-q-adj", "reading_no": "Q1",
        "certificate_no": "CERT-QA", "certificate_version_seq": 1,
        "basis_version": read_v, "reason": "manual pick QA",
    })
    adj_v = r.json()["version"]

    # 旧版本包重取：内容与摘要完全一致
    again = client.get(f"{API}/audit/packages", params=params).json()
    assert again["items"] == before["items"]
    assert again["digest"]["package_digest"] == before["digest"]["package_digest"]
    assert again["digest"]["segment_digest"] == before["digest"]["segment_digest"]

    # 裁决版本包：结论与依赖证书按版本呈现为所选，候选保留全部，含裁决块
    adj_pkg = client.get(
        f"{API}/audit/packages", params={**params, "at_version": adj_v}
    ).json()
    it = adj_pkg["items"][0]
    assert it["status"] == "conforming"
    assert it["reliance"]["certificate_no"] == "CERT-QA"
    assert it["reliance"]["version_seq"] == 1
    assert {c["certificate_no"] for c in it["candidates"]} == {"CERT-QA", "CERT-QB"}
    assert it["adjudication"]["state"] == "active"
    assert it["adjudication"]["reason"] == "manual pick QA"
    # 摘要仍为 audit-v1 且可独立复算
    assert adj_pkg["digest"]["spec_version"] == "audit-v1"
    canonical_package = {
        "spec_version": adj_pkg["digest"]["spec_version"],
        "filter": adj_pkg["filter"],
        "at_version": adj_v,
        "total_count": adj_pkg["total_count"],
        "items": adj_pkg["items"],
    }
    assert adj_pkg["digest"]["package_digest"] == _sha(_canon(canonical_package))

    # 撤销所选证书 -> 当前版本包回到自动结论（只剩 QB -> out_of_range），标明裁决失效
    r = client.post(f"{API}/certificates/CERT-QA/revoke", json={
        "request_id": "req-q-rev", "certificate_no": "CERT-QA", "reason": "x",
    })
    rev_v = r.json()["version"]
    cur_pkg = client.get(
        f"{API}/audit/packages", params={**params, "at_version": rev_v}
    ).json()
    it = cur_pkg["items"][0]
    assert it["status"] == "out_of_range"
    assert it["reliance"]["certificate_no"] == "CERT-QB"
    assert it["adjudication"]["state"] == "invalidated"
    assert it["adjudication"]["invalidated_at_version"] == rev_v
    canonical_package = {
        "spec_version": cur_pkg["digest"]["spec_version"],
        "filter": cur_pkg["filter"],
        "at_version": rev_v,
        "total_count": cur_pkg["total_count"],
        "items": cur_pkg["items"],
    }
    assert cur_pkg["digest"]["package_digest"] == _sha(_canon(canonical_package))

    # 裁决版本包此后重取仍恒定（取证包版本内容与摘要不随后续操作改变）
    frozen = client.get(
        f"{API}/audit/packages", params={**params, "at_version": adj_v}
    ).json()
    assert frozen["items"] == adj_pkg["items"]
    assert frozen["digest"]["package_digest"] == adj_pkg["digest"]["package_digest"]


def test_23_versions_table_migration_from_legacy_check(tmp_path, monkeypatch):
    """旧库 versions 的 CHECK 不含 reading_adjudicate：启动迁移保数据、保版本序列。"""
    import sqlite3

    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
    CREATE TABLE versions (
      version INTEGER PRIMARY KEY AUTOINCREMENT,
      event_type TEXT NOT NULL CHECK (event_type IN
        ('cert_issue','cert_correct','cert_revoke','reading_submit')),
      request_id TEXT NOT NULL UNIQUE,
      request_payload_json TEXT NOT NULL,
      fingerprint TEXT NOT NULL,
      response_json TEXT,
      created_at TEXT NOT NULL);
    INSERT INTO versions (event_type,request_id,request_payload_json,fingerprint,created_at)
      VALUES ('cert_issue','legacy-1','{}','{}','2026-01-01T00:00:00.000000Z');
    """)
    conn.commit()
    conn.close()

    monkeypatch.setenv("TRACE_DB_PATH", db_path)
    reset_settings_cache()
    from app import database
    from app.database import init_db, get_conn

    init_db()
    with get_conn() as c:
        rows = c.execute("SELECT version, event_type, request_id FROM versions").fetchall()
        assert [tuple(r) for r in rows] == [(1, "cert_issue", "legacy-1")]
        assert c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='adjudications'"
        ).fetchone()["name"] == "adjudications"
        # 迁移后允许新事件类型
        c.execute(
            "INSERT INTO versions (event_type,request_id,request_payload_json,"
            "fingerprint,created_at) VALUES ('reading_adjudicate','legacy-2','{}','{}',?)",
            ("2026-01-02T00:00:00.000000Z",),
        )
        assert c.execute("SELECT MAX(version) AS m FROM versions").fetchone()["m"] == 2

    # 再次初始化幂等
    init_db()
    reset_settings_cache()  # 还原，避免影响模块共享 DB 的后续测试
    # 模块级 client 使用固定路径；重启其设置缓存即可（client 仍在生命周期内）
    reset_settings_cache()

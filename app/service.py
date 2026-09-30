"""领域服务：证书生命周期、读数可信性评估、单调版本与幂等。

结论状态：
- unverified  未验证：测量时刻没有任何有效证书覆盖
- review      待复核：多份有效证书同时覆盖
- conforming  合格：唯一匹配且数值落在闭区间允许值内
- out_of_range 超限：唯一匹配但数值超出允许值
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .database import get_conn, read_tx, write_tx
from .errors import ApiError
from .models import UTC_FMT, to_utc_string

CONCLUSIONS = ("unverified", "review", "conforming", "out_of_range")


# ---------------------------------------------------------------- 基础工具

def utc_now_string() -> str:
    return datetime.now(timezone.utc).strftime(UTC_FMT)


def _dt(value: Any) -> str:
    """请求中的 datetime 归一化为 UTC 定长字符串。"""
    return to_utc_string(value)


def canonical_json(payload: Dict[str, Any]) -> str:
    """幂等指纹：对归一化后的内容做稳定序列化（键排序、无空白）。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _cert_payload_dict(
    certificate_no: str,
    device_no: str,
    valid_from: str,
    valid_to: str,
    min_value: float,
    max_value: float,
    **extra: Any,
) -> Dict[str, Any]:
    p = {
        "certificate_no": certificate_no,
        "device_no": device_no,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "min_value": min_value,
        "max_value": max_value,
    }
    p.update(extra)
    return p


def _cert_snapshot(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "certificate_no": row["certificate_no"],
        "version_seq": row["version_seq"],
        "device_no": row["device_no"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "min_value": row["min_value"],
        "max_value": row["max_value"],
        "created_at_version": row["created_at_version"],
        "superseded_at_version": row["superseded_at_version"],
    }


# ---------------------------------------------------------------- 版本查询

def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM versions").fetchone()
    return int(row["v"])


def _resolve_at_version(conn: sqlite3.Connection, at_version: Optional[int]) -> int:
    latest = current_version(conn)
    if at_version is None:
        return latest
    if at_version < 1 or at_version > latest:
        raise ApiError(422, "invalid_version", f"at_version must be within [1, {latest}]")
    return at_version


def _insert_version(
    conn: sqlite3.Connection,
    event_type: str,
    request_id: str,
    payload: Dict[str, Any],
) -> int:
    """追加一条版本流水。request_id 唯一冲突 -> 幂等复用/冲突报错。

    调用方必须已经在写事务内完成所有业务校验。
    """
    body = canonical_json(payload)
    try:
        cur = conn.execute(
            "INSERT INTO versions (event_type, request_id, request_payload_json,"
            " fingerprint, response_json, created_at) VALUES (?,?,?,?,NULL,?)",
            (event_type, request_id, body, body, utc_now_string()),
        )
    except sqlite3.IntegrityError as e:  # request_id 复用
        row = conn.execute(
            "SELECT fingerprint, event_type FROM versions WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is not None and row["fingerprint"] != body:
            raise ApiError(
                409,
                "request_id_conflict",
                f"request_id {request_id!r} was already used with different content",
            )
        # 理论上并发下另一个事务已提交相同内容；标记为重放
        raise _Replay(row["event_type"] if row else event_type) from e
    return int(cur.lastrowid)


class _Replay(Exception):
    """事务内信号：相同 request_id+内容已存在，走重放路径。"""

    def __init__(self, event_type: str) -> None:
        self.event_type = event_type


def _replay_response(request_id: str) -> Dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT version, event_type, response_json, created_at FROM versions WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is None:  # 极端并发：对方事务已回滚
            raise ApiError(409, "request_id_conflict", "request_id is being processed")
        if not row["response_json"]:
            raise ApiError(409, "request_inflight", "request is being processed")
        resp = json.loads(row["response_json"])
        resp["idempotent_replay"] = True
        return resp


def _save_response(conn: sqlite3.Connection, version: int, response: Dict[str, Any]) -> None:
    conn.execute("UPDATE versions SET response_json=? WHERE version=?", (json.dumps(response, ensure_ascii=False), version))


# ---------------------------------------------------------------- 证书查询

def _cert_version_row(conn: sqlite3.Connection, certificate_no: str, seq: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM certificate_versions WHERE certificate_no=? AND version_seq=?",
        (certificate_no, seq),
    ).fetchone()


def _cert_master(conn: sqlite3.Connection, certificate_no: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM certificates WHERE certificate_no=?", (certificate_no,)
    ).fetchone()


def _active_cert_rows(
    conn: sqlite3.Connection, device_no: str, at_version: int, measured_at: str
) -> List[sqlite3.Row]:
    """在指定版本视角下，覆盖测量时刻的、当前仍有效的证书版本。

    有效条件（全部满足）：
    - created_at_version <= at_version                 （该版本已经可见）
    - superseded_at_version IS NULL 或 > at_version   （未被纠正/撤销失效）
    - valid_from <= measured_at < valid_to            （左闭右开有效期）
    """
    return list(conn.execute(
        """
        SELECT cv.*
        FROM certificate_versions cv
        JOIN versions v ON v.version = cv.created_at_version
        WHERE cv.device_no = ?
          AND cv.created_at_version <= ?
          AND (cv.superseded_at_version IS NULL OR cv.superseded_at_version > ?)
          AND cv.valid_from <= ?
          AND cv.valid_to   >  ?
        ORDER BY cv.certificate_no, cv.version_seq
        """,
        (device_no, at_version, at_version, measured_at, measured_at),
    ).fetchall())


def get_certificate(
    conn: sqlite3.Connection, certificate_no: str, at_version: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    master = _cert_master(conn, certificate_no)
    if master is None:
        return None
    av = _resolve_at_version(conn, at_version)
    if master["created_at_version"] > av:
        return None
    # 在该版本视角下的最新内容版本序号
    row = conn.execute(
        """
        SELECT * FROM certificate_versions
        WHERE certificate_no=? AND created_at_version <= ?
        ORDER BY version_seq DESC LIMIT 1
        """,
        (certificate_no, av),
    ).fetchone()
    out = _cert_snapshot(row)
    revoked = bool(master["is_revoked"]) and master["revoked_at_version"] <= av
    if revoked:
        out["status"] = "revoked"
    elif row["superseded_at_version"] is not None and row["superseded_at_version"] <= av:
        # 正常不会落到这：下一条内容版本应已可见
        out["status"] = "superseded"
    else:
        out["status"] = "active"
    out["revoked_reason"] = master["revoked_reason"]
    out["revoked_at_version"] = master["revoked_at_version"] if revoked else None
    out["at_version"] = av
    return out


def list_certificates(
    conn: sqlite3.Connection,
    at_version: Optional[int],
    device_no: Optional[str],
    limit: int,
    offset: int,
) -> Dict[str, Any]:
    av = _resolve_at_version(conn, at_version)
    sql = "SELECT certificate_no FROM certificates WHERE created_at_version <= ?"
    params: List[Any] = [av]
    if device_no:
        sql += " AND certificate_no IN (SELECT DISTINCT certificate_no FROM certificate_versions WHERE device_no=?)"
        params.append(device_no)
    total = conn.execute(
        "SELECT COUNT(*) AS c FROM (" + sql + ")", params
    ).fetchone()["c"]
    sql += " ORDER BY certificate_no LIMIT ? OFFSET ?"
    rows = conn.execute(sql, [*params, limit, offset]).fetchall()
    items = [get_certificate(conn, r["certificate_no"], av) for r in rows]
    return {"at_version": av, "total": total, "limit": limit, "offset": offset, "items": items}


def certificate_history(conn: sqlite3.Connection, certificate_no: str) -> Dict[str, Any]:
    master = _cert_master(conn, certificate_no)
    if master is None:
        raise ApiError(404, "unknown_certificate", f"certificate {certificate_no!r} not found")
    rows = conn.execute(
        "SELECT * FROM certificate_versions WHERE certificate_no=? ORDER BY version_seq",
        (certificate_no,),
    ).fetchall()
    return {
        "certificate_no": certificate_no,
        "current_version_seq": master["current_version_seq"],
        "is_revoked": bool(master["is_revoked"]),
        "revoked_at_version": master["revoked_at_version"],
        "revoked_reason": master["revoked_reason"],
        "versions": [_cert_snapshot(r) for r in rows],
    }


# ---------------------------------------------------------------- 读数评估

def evaluate_reading(
    conn: sqlite3.Connection,
    device_no: str,
    measured_at: str,
    value: float,
    at_version: int,
) -> Dict[str, Any]:
    """在指定版本视角下评估单条读数（不依赖读数是否已入库）。"""
    matches = _active_cert_rows(conn, device_no, at_version, measured_at)
    if not matches:
        return {"status": "unverified", "reliance": None, "candidates": []}
    if len(matches) > 1:
        return {
            "status": "review",
            "reliance": None,
            "candidates": [_cert_snapshot(m) for m in matches],
        }
    cert = matches[0]
    status = "conforming" if cert["min_value"] <= value <= cert["max_value"] else "out_of_range"
    return {"status": status, "reliance": _cert_snapshot(cert), "candidates": [_cert_snapshot(cert)]}


# ---------------------------------------------------------------- 人工选证裁决

def _latest_adjudication_row(
    conn: sqlite3.Connection, reading_no: str, at_version: int
) -> Optional[sqlite3.Row]:
    """该读数在 at_version 之前（含）最近一条裁决（只追加，可能已失效）。"""
    return conn.execute(
        "SELECT * FROM reading_adjudications"
        " WHERE reading_no=? AND version<=?"
        " ORDER BY version DESC LIMIT 1",
        (reading_no, at_version),
    ).fetchone()


def _invalidation_of(
    conn: sqlite3.Connection, cert_row: sqlite3.Row, at_version: int
) -> Optional[Dict[str, Any]]:
    """所选证书版本在 at_version 视角已被纠正/撤销失效时返回失效信息。"""
    sup = cert_row["superseded_at_version"]
    if sup is None or sup > at_version:
        return None
    ev = conn.execute(
        "SELECT event_type FROM versions WHERE version=?", (sup,)
    ).fetchone()
    cause = (
        "certificate_revoked"
        if ev is not None and ev["event_type"] == "cert_revoke"
        else "certificate_corrected"
    )
    return {
        "invalidated": True,
        "invalidated_at_version": int(sup),
        "invalidation_cause": cause,
    }


def _adjudication_block(
    conn: sqlite3.Connection,
    adj: sqlite3.Row,
    at_version: int,
    project_superseded: bool,
) -> Dict[str, Any]:
    """构造一条裁决在 at_version 视角下的呈现。

    - active/invalidated：所选证书版本是否仍有效（纠正/撤销使其失效）；
    - project_superseded=True 时证书快照做版本视角投影（取证包用，
      不携带 at_version 之后的失效信息）。
    """
    cert_row = _cert_version_row(conn, adj["certificate_no"], adj["certificate_version_seq"])
    snapshot = (
        _cert_snapshot_as_of(cert_row, at_version)
        if project_superseded
        else _cert_snapshot(cert_row)
    )
    block = {
        "active": True,
        "adjudicated_at_version": int(adj["version"]),
        "request_id": adj["request_id"],
        "base_version": int(adj["base_version"]),
        "selected_certificate": snapshot,
        "reason": adj["reason"],
        "invalidated": False,
        "invalidated_at_version": None,
        "invalidation_cause": None,
    }
    invalidation = _invalidation_of(conn, cert_row, at_version)
    if invalidation is not None:
        block["active"] = False
        block.update(invalidation)
    return block


def evaluate_stored_reading(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    at_version: int,
    project_superseded: bool = False,
) -> Dict[str, Any]:
    """已入库读数在 at_version 视角的评估，叠加人工选证裁决。

    自动评估（无候选 / 多候选 review / 唯一候选）之外：
    - 存在裁决且所选证书版本在 at_version 仍有效（active）：
      以所选证书按闭区间给出 conforming/out_of_range，reliance 指向所选版本，
      candidates 仍保留该版本视角自动评估出的全部候选；
    - 裁决存在但所选版本已被纠正/撤销（invalidated）：结论回退为自动结果，
      裁决块标明失效版本与原因；
    - at_version 早于裁决版本：裁决不可见（旧版本视角原样）。

    project_superseded=True 时所有证书快照做 as-of 投影（取证包用）。
    """
    if project_superseded:
        auto = _evaluate_reading_as_of(
            conn, row["device_no"], row["measured_at"], row["value"], at_version
        )
    else:
        auto = evaluate_reading(
            conn, row["device_no"], row["measured_at"], row["value"], at_version
        )
    adj_row = _latest_adjudication_row(conn, row["reading_no"], at_version)
    if adj_row is None:
        auto["adjudication"] = None
        return auto

    adjudication = _adjudication_block(conn, adj_row, at_version, project_superseded)
    if adjudication["active"]:
        cert_row = _cert_version_row(
            conn, adj_row["certificate_no"], adj_row["certificate_version_seq"]
        )
        status = (
            "conforming"
            if cert_row["min_value"] <= row["value"] <= cert_row["max_value"]
            else "out_of_range"
        )
        reliance = (
            _cert_snapshot_as_of(cert_row, at_version)
            if project_superseded
            else _cert_snapshot(cert_row)
        )
        return {
            "status": status,
            "reliance": reliance,
            "candidates": auto["candidates"],
            "adjudication": adjudication,
        }
    auto["adjudication"] = adjudication
    return auto


def _evaluate_affected(
    conn: sqlite3.Connection, before_version: int, new_version: int, device_nos: Tuple[str, ...]
) -> List[Dict[str, Any]]:
    """重算在 before_version 之前（含）提交、且设备相关的所有读数的结论变化。

    叠加人工裁决：例如裁决所选证书在本次被纠正/撤销，前后对照会呈现
    conforming/out_of_range -> 回退后的自动结论（裁决标失效）。
    """
    if not device_nos:
        return []
    placeholders = ",".join("?" for _ in device_nos)
    rows = conn.execute(
        f"SELECT * FROM readings WHERE submitted_at_version <= ? AND device_no IN ({placeholders})"
        " ORDER BY reading_no",
        (before_version, *device_nos),
    ).fetchall()
    affected: List[Dict[str, Any]] = []
    for r in rows:
        before = evaluate_stored_reading(conn, r, before_version)
        after = evaluate_stored_reading(conn, r, new_version)
        affected.append({
            "reading_no": r["reading_no"],
            "device_no": r["device_no"],
            "measured_at": r["measured_at"],
            "value": r["value"],
            "before": {"at_version": before_version, **before},
            "after": {"at_version": new_version, **after},
            "conclusion_changed": before["status"] != after["status"],
        })
    return affected


# ---------------------------------------------------------------- 写操作：签发

def issue_certificate(data: Dict[str, Any]) -> Dict[str, Any]:
    payload = _cert_payload_dict(
        data["certificate_no"],
        data["device_no"],
        _dt(data["valid_from"]),
        _dt(data["valid_to"]),
        float(data["min_value"]),
        float(data["max_value"]),
    )
    request_id = data["request_id"]

    while True:
        try:
            with write_tx() as conn:
                # 先登记版本（含幂等判定），再做业务校验并写入；
                # 失败整体回滚，不留下版本或部分数据
                version = _insert_version(conn, "cert_issue", request_id, payload)
                if _cert_master(conn, payload["certificate_no"]) is not None:
                    raise ApiError(
                        409,
                        "certificate_exists",
                        f"certificate {payload['certificate_no']!r} already exists;"
                        " use correct or revoke instead",
                    )
                conn.execute(
                    "INSERT INTO certificates (certificate_no, current_version_seq,"
                    " is_revoked, created_at_version, updated_at_version)"
                    " VALUES (?,1,0,?,?)",
                    (payload["certificate_no"], version, version),
                )
                conn.execute(
                    "INSERT INTO certificate_versions (certificate_no, version_seq,"
                    " device_no, valid_from, valid_to, min_value, max_value, created_at_version)"
                    " VALUES (?,1,?,?,?,?,?,?)",
                    (
                        payload["certificate_no"], payload["device_no"],
                        payload["valid_from"], payload["valid_to"],
                        payload["min_value"], payload["max_value"], version,
                    ),
                )
                response = {
                    "version": version,
                    "request_id": request_id,
                    "event_type": "cert_issue",
                    "idempotent_replay": False,
                    "certificate": _cert_snapshot(_cert_version_row(conn, payload["certificate_no"], 1)),
                }
                _save_response(conn, version, response)
                return response
        except _Replay:
            return _replay_response(request_id)


# ---------------------------------------------------------------- 写操作：纠正

def correct_certificate(data: Dict[str, Any]) -> Dict[str, Any]:
    payload = _cert_payload_dict(
        data["certificate_no"],
        data["device_no"],
        _dt(data["valid_from"]),
        _dt(data["valid_to"]),
        float(data["min_value"]),
        float(data["max_value"]),
        reason=data.get("reason"),
    )
    request_id = data["request_id"]

    while True:
        try:
            with write_tx() as conn:
                before_version = current_version(conn)
                # 先登记版本（幂等），再校验目标与执行纠正，失败整体回滚
                version = _insert_version(conn, "cert_correct", request_id, payload)
                master = _cert_master(conn, payload["certificate_no"])
                if master is None:
                    raise ApiError(404, "unknown_certificate",
                                   f"certificate {payload['certificate_no']!r} not found; issue it first")
                if master["is_revoked"]:
                    raise ApiError(409, "certificate_revoked",
                                   "a revoked certificate cannot be corrected")
                old_seq = int(master["current_version_seq"])
                old_row = _cert_version_row(conn, payload["certificate_no"], old_seq)
                new_seq = old_seq + 1

                # 旧版本在新版本起失效（历史行不被覆盖，仅登记失效版本）
                conn.execute(
                    "UPDATE certificate_versions SET superseded_at_version=? WHERE id=?",
                    (version, old_row["id"]),
                )
                conn.execute(
                    "INSERT INTO certificate_versions (certificate_no, version_seq,"
                    " device_no, valid_from, valid_to, min_value, max_value, created_at_version)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        payload["certificate_no"], new_seq, payload["device_no"],
                        payload["valid_from"], payload["valid_to"],
                        payload["min_value"], payload["max_value"], version,
                    ),
                )
                conn.execute(
                    "UPDATE certificates SET current_version_seq=?, updated_at_version=?"
                    " WHERE certificate_no=?",
                    (new_seq, version, payload["certificate_no"]),
                )

                devices = {old_row["device_no"], payload["device_no"]}
                affected = _evaluate_affected(conn, before_version, version, tuple(sorted(devices)))
                response = {
                    "version": version,
                    "request_id": request_id,
                    "event_type": "cert_correct",
                    "idempotent_replay": False,
                    "certificate_no": payload["certificate_no"],
                    "superseded_version_seq": old_seq,
                    "certificate": _cert_snapshot(_cert_version_row(conn, payload["certificate_no"], new_seq)),
                    "affected_readings": affected,
                }
                _save_response(conn, version, response)
                return response
        except _Replay:
            return _replay_response(request_id)


# ---------------------------------------------------------------- 写操作：撤销

def revoke_certificate(data: Dict[str, Any]) -> Dict[str, Any]:
    request_id = data["request_id"]
    payload = {
        "certificate_no": data["certificate_no"],
        "reason": data.get("reason"),
    }
    while True:
        try:
            with write_tx() as conn:
                before_version = current_version(conn)
                version = _insert_version(conn, "cert_revoke", request_id, payload)
                master = _cert_master(conn, payload["certificate_no"])
                if master is None:
                    raise ApiError(404, "unknown_certificate",
                                   f"certificate {payload['certificate_no']!r} not found")
                if master["is_revoked"]:
                    raise ApiError(409, "certificate_revoked",
                                   "certificate is already revoked")
                cur_seq = int(master["current_version_seq"])
                cur_row = _cert_version_row(conn, payload["certificate_no"], cur_seq)

                conn.execute(
                    "UPDATE certificate_versions SET superseded_at_version=? WHERE id=?",
                    (version, cur_row["id"]),
                )
                conn.execute(
                    "UPDATE certificates SET is_revoked=1, revoked_at_version=?,"
                    " revoked_reason=?, updated_at_version=? WHERE certificate_no=?",
                    (version, payload["reason"], version, payload["certificate_no"]),
                )
                affected = _evaluate_affected(conn, before_version, version, (cur_row["device_no"],))
                response = {
                    "version": version,
                    "request_id": request_id,
                    "event_type": "cert_revoke",
                    "idempotent_replay": False,
                    "certificate_no": payload["certificate_no"],
                    "revoked_version_seq": cur_seq,
                    "reason": payload["reason"],
                    "affected_readings": affected,
                }
                _save_response(conn, version, response)
                return response
        except _Replay:
            return _replay_response(request_id)


# ---------------------------------------------------------------- 写操作：读数提交

def submit_readings(data: Dict[str, Any]) -> Dict[str, Any]:
    request_id = data["request_id"]
    items: List[Dict[str, Any]] = []
    for r in data["readings"]:
        items.append({
            "reading_no": r["reading_no"],
            "device_no": r["device_no"],
            "measured_at": _dt(r["measured_at"]),
            "value": float(r["value"]),
        })
    payload = {"readings": items}

    while True:
        try:
            with write_tx() as conn:
                version = _insert_version(conn, "reading_submit", request_id, payload)
                # 未知目标/重复读数：全部先校验，保证不留下部分变更
                existing = {
                    row["reading_no"]
                    for row in conn.execute(
                        "SELECT reading_no FROM readings WHERE reading_no IN (%s)"
                        % ",".join("?" for _ in items),
                        [i["reading_no"] for i in items],
                    ).fetchall()
                }
                if existing:
                    raise ApiError(
                        409,
                        "reading_exists",
                        f"readings already exist: {sorted(existing)}; readings are immutable",
                    )
                now = utc_now_string()
                for i in items:
                    conn.execute(
                        "INSERT INTO readings (reading_no, device_no, measured_at, value,"
                        " submitted_at_version, created_at) VALUES (?,?,?,?,?,?)",
                        (i["reading_no"], i["device_no"], i["measured_at"],
                         i["value"], version, now),
                    )
                results = []
                for i in items:
                    conclusion = evaluate_reading(
                        conn, i["device_no"], i["measured_at"], i["value"], version
                    )
                    results.append({
                        "reading_no": i["reading_no"],
                        "submitted_at_version": version,
                        **conclusion,
                    })
                response = {
                    "version": version,
                    "request_id": request_id,
                    "event_type": "reading_submit",
                    "idempotent_replay": False,
                    "results": results,
                }
                _save_response(conn, version, response)
                return response
        except _Replay:
            return _replay_response(request_id)


# ---------------------------------------------------------------- 写操作：人工选证裁决

def adjudicate_reading(data: Dict[str, Any]) -> Dict[str, Any]:
    """对处于 review 的读数做人工选证裁决。

    - 占用一个全局版本（reading_adjudicate），不新增/修改证书或读数；
    - base_version 必须等于当前版本，否则拒绝（stale_base_version）；
    - 所选证书版本必须是该读数测量时刻在 base_version 视角下的候选，
      否则拒绝（422 selection_not_candidate / 404 unknown_*）；
    - 该读数当前自动结论必须为 review，否则拒绝；
    - 成功后按所选证书闭区间给 conforming/out_of_range，保留全部候选，
      并返回裁决依据（base_version 视角的自动结论与候选）。
    """
    request_id = data["request_id"]
    payload = {
        "reading_no": data["reading_no"],
        "certificate_no": data["certificate_no"],
        "certificate_version_seq": int(data["certificate_version_seq"]),
        "base_version": int(data["base_version"]),
        "reason": data["reason"],
    }

    while True:
        try:
            with write_tx() as conn:
                before_version = current_version(conn)
                # 先登记版本（幂等判定），再做业务校验，失败整体回滚不产生新版本
                version = _insert_version(conn, "reading_adjudicate", request_id, payload)

                # 依据版本必须等于当前版本，防止基于过期视图裁决
                if payload["base_version"] != before_version:
                    raise ApiError(
                        409,
                        "stale_base_version",
                        f"base_version {payload['base_version']} does not match"
                        f" current version {before_version}",
                    )

                reading = conn.execute(
                    "SELECT * FROM readings WHERE reading_no=?",
                    (payload["reading_no"],),
                ).fetchone()
                if reading is None:
                    raise ApiError(
                        404,
                        "unknown_reading",
                        f"reading {payload['reading_no']!r} not found",
                    )

                # 裁决所依据的当前自动结论（base_version == before_version 视角）
                basis = evaluate_reading(
                    conn, reading["device_no"], reading["measured_at"],
                    reading["value"], before_version,
                )
                if basis["status"] != "review":
                    raise ApiError(
                        409,
                        "reading_not_in_review",
                        f"reading {payload['reading_no']!r} is {basis['status']!r}"
                        " at the base version; only readings under review can be adjudicated",
                    )

                selected_no = payload["certificate_no"]
                selected_seq = payload["certificate_version_seq"]
                if _cert_master(conn, selected_no) is None:
                    raise ApiError(
                        404,
                        "unknown_certificate",
                        f"certificate {selected_no!r} not found",
                    )
                selected_row = _cert_version_row(conn, selected_no, selected_seq)
                if selected_row is None:
                    raise ApiError(
                        422,
                        "selection_not_candidate",
                        f"certificate {selected_no!r} has no version_seq {selected_seq}",
                    )
                candidate_keys = {
                    (c["certificate_no"], c["version_seq"]) for c in basis["candidates"]
                }
                if (selected_no, selected_seq) not in candidate_keys:
                    raise ApiError(
                        422,
                        "selection_not_candidate",
                        f"certificate {selected_no!r} version {selected_seq} is not a"
                        " candidate of the reading at the measured time/base version",
                    )

                conn.execute(
                    "INSERT INTO reading_adjudications (reading_no, version, request_id,"
                    " base_version, certificate_no, certificate_version_seq, reason,"
                    " created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        payload["reading_no"], version, request_id,
                        payload["base_version"], selected_no, selected_seq,
                        payload["reason"], utc_now_string(),
                    ),
                )

                # 重新按新版本视角评估：裁决生效，保留全部候选，
                # 结论按所选证书闭区间给出
                result = evaluate_stored_reading(conn, reading, version)
                adj_row = conn.execute(
                    "SELECT * FROM reading_adjudications WHERE version=?", (version,)
                ).fetchone()
                response = {
                    "version": version,
                    "request_id": request_id,
                    "event_type": "reading_adjudicate",
                    "idempotent_replay": False,
                    "reading_no": payload["reading_no"],
                    "device_no": reading["device_no"],
                    "measured_at": reading["measured_at"],
                    "value": reading["value"],
                    "submitted_at_version": reading["submitted_at_version"],
                    "status": result["status"],
                    "reliance": result["reliance"],
                    "candidates": result["candidates"],
                    "adjudication": result["adjudication"],
                    "basis": {
                        "base_version": before_version,
                        "status": basis["status"],
                        "candidates": basis["candidates"],
                    },
                }
                _save_response(conn, version, response)
                return response
        except _Replay:
            return _replay_response(request_id)


# ---------------------------------------------------------------- 读接口

def get_reading(
    conn: sqlite3.Connection, reading_no: str, at_version: Optional[int]
) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM readings WHERE reading_no=?", (reading_no,)).fetchone()
    if row is None:
        return None
    av = _resolve_at_version(conn, at_version)
    if av < row["submitted_at_version"]:
        raise ApiError(
            422,
            "invalid_version",
            f"reading was submitted at version {row['submitted_at_version']},"
            f" cannot evaluate at earlier version {av}",
        )
    conclusion = evaluate_stored_reading(conn, row, av)
    return {
        "reading_no": row["reading_no"],
        "device_no": row["device_no"],
        "measured_at": row["measured_at"],
        "value": row["value"],
        "submitted_at_version": row["submitted_at_version"],
        "at_version": av,
        **conclusion,
    }


def list_readings(
    conn: sqlite3.Connection,
    at_version: Optional[int],
    device_no: Optional[str],
    status_filter: Optional[str],
    limit: int,
    offset: int,
) -> Dict[str, Any]:
    av = _resolve_at_version(conn, at_version)
    if status_filter is not None and status_filter not in CONCLUSIONS:
        raise ApiError(422, "invalid_status",
                       f"status must be one of {list(CONCLUSIONS)}")
    rows = conn.execute(
        "SELECT * FROM readings WHERE submitted_at_version <= ?"
        + (" AND device_no=?" if device_no else "")
        + " ORDER BY measured_at, reading_no LIMIT ? OFFSET ?",
        ([av] + ([device_no] if device_no else []) + [limit + 1, offset]),
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = []
    for r in rows:
        c = evaluate_stored_reading(conn, r, av)
        if status_filter and c["status"] != status_filter:
            continue
        items.append({
            "reading_no": r["reading_no"],
            "device_no": r["device_no"],
            "measured_at": r["measured_at"],
            "value": r["value"],
            "submitted_at_version": r["submitted_at_version"],
            "at_version": av,
            **c,
        })
    return {
        "at_version": av,
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
        "items": items,
    }


def list_reading_adjudications(
    conn: sqlite3.Connection, reading_no: str
) -> Optional[Dict[str, Any]]:
    """读数的裁决流水（只追加）：每条给出原始裁决内容与当前是否已失效。"""
    reading = conn.execute(
        "SELECT * FROM readings WHERE reading_no=?", (reading_no,)
    ).fetchone()
    if reading is None:
        return None
    rows = conn.execute(
        "SELECT * FROM reading_adjudications WHERE reading_no=? ORDER BY version",
        (reading_no,),
    ).fetchall()
    current_av = current_version(conn)
    items = []
    for a in rows:
        cert_row = _cert_version_row(
            conn, a["certificate_no"], a["certificate_version_seq"]
        )
        invalidation = _invalidation_of(conn, cert_row, current_av)
        items.append({
            "version": int(a["version"]),
            "request_id": a["request_id"],
            "reading_no": a["reading_no"],
            "base_version": int(a["base_version"]),
            "selected_certificate": _cert_snapshot(cert_row),
            "reason": a["reason"],
            "created_at": a["created_at"],
            "active": invalidation is None,
            **(invalidation or {
                "invalidated": False,
                "invalidated_at_version": None,
                "invalidation_cause": None,
            }),
        })
    return {
        "reading_no": reading_no,
        "at_version": current_av,
        "total": len(items),
        "items": items,
    }


def list_versions(conn: sqlite3.Connection, limit: int, offset: int) -> Dict[str, Any]:
    total = conn.execute("SELECT COUNT(*) AS c FROM versions").fetchone()["c"]
    rows = conn.execute(
        "SELECT version, event_type, request_id, request_payload_json, created_at"
        " FROM versions ORDER BY version DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "version": r["version"],
                "event_type": r["event_type"],
                "request_id": r["request_id"],
                "request_payload": json.loads(r["request_payload_json"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


def get_request(conn: sqlite3.Connection, request_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM versions WHERE request_id=?", (request_id,)
    ).fetchone()
    if row is None:
        return None
    return {
        "request_id": row["request_id"],
        "version": row["version"],
        "event_type": row["event_type"],
        "request_payload": json.loads(row["request_payload_json"]),
        "response": json.loads(row["response_json"]) if row["response_json"] else None,
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------- 审计取证包

# 取证摘要算法版本：摘要规范化规则一旦变化即升版本，避免新旧摘要混淆。
AUDIT_DIGEST_ALGORITHM = "sha256"
AUDIT_DIGEST_SPEC_VERSION = "audit-v1"


def _cert_snapshot_as_of(row: sqlite3.Row, at_version: int) -> Dict[str, Any]:
    """证书快照在版本视角 at_version 下的投影。

    与 ``_cert_snapshot`` 的唯一差别：失效版本若晚于取证版本（属于"未来"
    的纠正 / 撤销），投影为 None，保证历史版本视角的快照不携带版本之后
    才发生的事件，从而做到同条件同版本内容永远一致。
    """
    snap = _cert_snapshot(row)
    if snap["superseded_at_version"] is not None and snap["superseded_at_version"] > at_version:
        snap["superseded_at_version"] = None
    return snap


def _evaluate_reading_as_of(
    conn: sqlite3.Connection,
    device_no: str,
    measured_at: str,
    value: float,
    at_version: int,
) -> Dict[str, Any]:
    """版本视角下评估读数，候选证书快照统一做 as-of 投影。"""
    matches = _active_cert_rows(conn, device_no, at_version, measured_at)
    if not matches:
        return {"status": "unverified", "reliance": None, "candidates": []}
    candidates = [_cert_snapshot_as_of(m, at_version) for m in matches]
    if len(matches) > 1:
        return {"status": "review", "reliance": None, "candidates": candidates}
    status = (
        "conforming"
        if matches[0]["min_value"] <= value <= matches[0]["max_value"]
        else "out_of_range"
    )
    return {"status": status, "reliance": candidates[0], "candidates": candidates}


def _audit_canonical_rules() -> Dict[str, Any]:
    """摘要规范化规则（随包返回，可据其独立复算）。"""
    return {
        "spec_version": AUDIT_DIGEST_SPEC_VERSION,
        "hash_algorithm": AUDIT_DIGEST_ALGORITHM,
        "text_encoding": "utf-8",
        "json_rules": (
            "RFC 8785 风格的规范化 JSON：对象键按 Unicode 码位字典序升序，"
            "分隔符为冒号/逗号且无任何多余空白；不输出枚举以外的键；"
            "ensure_ascii=false 后按 UTF-8 编码入哈希。"
        ),
        "datetime_rules": (
            "全部时间归一化为 UTC 定长字符串 YYYY-MM-DDTHH:MM:SS.ffffffZ"
            "（微秒不足 6 位左侧补 0，末尾固定 Z）；测量区间为左闭右开。"
        ),
        "number_rules": (
            "数值为 JSON number，采用 IEEE 754 binary64 的最短往返十进制文本"
            "（Python repr / json.dumps 的 float 输出；整数如 10 输出 10.0"
            "与否取决于存储类型，服务端 REAL 统一按 float 渲染）。"
        ),
        "item_order": (
            "条目固定按 (measured_at UTC 升序, reading_no 升序) 排列，"
            "与分段 limit/offset 无关；包摘要始终覆盖全部 total_count 条。"
        ),
        "candidate_rules": (
            "candidates 按 (certificate_no 升序, version_seq 升序) 排列；"
            "证书快照为取证版本视角投影：created_at_version<=at_version 才可见，"
            "且 superseded_at_version 晚于 at_version 时投影为 null。"
            "裁决生效时 candidates 仍保留该版本视角自动评估出的全部候选。"
        ),
        "adjudication_rules": (
            "每条目固定含 adjudication 键：无裁决（含裁决版本晚于 at_version）为 null；"
            "存在人工选证裁决时给出 active/invalidated、adjudicated_at_version、"
            "request_id、base_version、selected_certificate、reason 及失效信息"
            "（invalidated_at_version/invalidation_cause）。"
            "active 时 status 按所选证书闭区间为 conforming/out_of_range、"
            "reliance 指向所选证书版本；所选证书版本在 at_version 视角已被纠正/撤销时"
            "active=false，status 回退为该版本视角的自动结论。"
            "selected_certificate 同样做版本视角投影（superseded_at_version"
            "晚于 at_version 时为 null），因此同条件同版本的包内容与摘要恒定，"
            "不随日后的纠正/撤销/再次裁决变化。"
        ),
        "package_digest_input": (
            "package_digest = lower(hex(sha256(canonical_json(canonical_package))))，"
            "canonical_package 的结构与字段见包体 digest.canonical_package 说明；"
            "该结构只含查询条件、版本与全部条目，不含生成时刻、服务版本等易变量。"
        ),
        "segment_digest_input": (
            "segment_digest = lower(hex(sha256(canonical_json({"
            "'spec_version','package_id','package_digest','at_version',"
            "'limit','offset','items'})))，items 为本分段条目，"
            "条目对象与包摘要所用完全一致。"
        ),
        "package_id_rule": (
            "package_id = lower(hex(sha256(canonical_json({"
            "'device_no','measured_from','measured_to','at_version'})))"
            " 的前 32 个十六进制字符；同条件同版本恒定。"
        ),
    }


def _audit_filter_object(
    device_no: str, measured_from: str, measured_to: str, at_version: int
) -> Dict[str, Any]:
    return {
        "device_no": device_no,
        "measured_from": measured_from,
        "measured_to": measured_to,
        "at_version": at_version,
    }


def _audit_item(conn: sqlite3.Connection, r: sqlite3.Row, at_version: int) -> Dict[str, Any]:
    conclusion = evaluate_stored_reading(conn, r, at_version, project_superseded=True)
    return {
        "reading_no": r["reading_no"],
        "device_no": r["device_no"],
        "measured_at": r["measured_at"],
        "value": r["value"],
        "submitted_at_version": r["submitted_at_version"],
        "status": conclusion["status"],
        "reliance": conclusion["reliance"],
        "candidates": conclusion["candidates"],
        "adjudication": conclusion["adjudication"],
    }


def build_audit_package(
    device_no: str,
    measured_from: Any,
    measured_to: Any,
    at_version: int,
    limit: int,
    offset: int,
) -> Dict[str, Any]:
    """生成审计取证追溯包（只读，不改任何数据）。

    筛选：指定设备、测量时间落入 [measured_from, measured_to)（左闭右开）、
    且在 at_version 版本之前（含该版本）已提交的读数；
    每条读数给出在 at_version 版本视角的结论与全部候选证书快照。

    版本越界 / 无效区间在进入任何数据读取前即报错（422），不产生半成品；
    无匹配读数时返回空包（total_count=0、items=[]，摘要照常给出）。
    """
    # 无效区间先于一切数据库访问明确报错
    if measured_from.tzinfo is None or measured_to.tzinfo is None:
        raise ApiError(
            422,
            "missing_timezone",
            "measured_from and measured_to must include a timezone, e.g. 2026-01-01T00:00:00Z",
        )
    mfrom = to_utc_string(measured_from)
    mto = to_utc_string(measured_to)
    if mfrom >= mto:
        raise ApiError(
            422,
            "invalid_time_range",
            "measured_from must be earlier than measured_to (half-open interval)",
        )

    # 单一只读快照事务：事务开始后的任何新提交 / 纠正 / 撤销在本事务内不可见
    with read_tx() as conn:
        av = _resolve_at_version(conn, at_version)

        filter_obj = _audit_filter_object(device_no, mfrom, mto, av)
        package_id = hashlib.sha256(
            canonical_json(filter_obj).encode("utf-8")
        ).hexdigest()[:32]

        where_sql = (
            "device_no = ?"
            " AND submitted_at_version <= ?"
            " AND measured_at >= ?"
            " AND measured_at < ?"
        )
        where_params: List[Any] = [device_no, av, mfrom, mto]
        order_sql = " ORDER BY measured_at, reading_no"

        total_count = int(conn.execute(
            f"SELECT COUNT(*) AS c FROM readings WHERE {where_sql}", where_params
        ).fetchone()["c"])

        # 包摘要覆盖全部条目（与分段无关）：稳定排序后逐段取出评估
        all_items: List[Dict[str, Any]] = []
        page = 200
        for page_offset in range(0, total_count, page):
            rows = conn.execute(
                f"SELECT * FROM readings WHERE {where_sql}{order_sql} LIMIT ? OFFSET ?",
                (*where_params, page, page_offset),
            ).fetchall()
            all_items.extend(_audit_item(conn, r, av) for r in rows)

        canonical_package = {
            "spec_version": AUDIT_DIGEST_SPEC_VERSION,
            "filter": filter_obj,
            "at_version": av,
            "total_count": total_count,
            "items": all_items,
        }
        package_digest = hashlib.sha256(
            canonical_json(canonical_package).encode("utf-8")
        ).hexdigest()

        segment_rows = conn.execute(
            f"SELECT * FROM readings WHERE {where_sql}{order_sql} LIMIT ? OFFSET ?",
            (*where_params, limit, offset),
        ).fetchall()
        items = [_audit_item(conn, r, av) for r in segment_rows]

    segment_input = {
        "spec_version": AUDIT_DIGEST_SPEC_VERSION,
        "package_id": package_id,
        "package_digest": package_digest,
        "at_version": av,
        "limit": limit,
        "offset": offset,
        "items": items,
    }
    segment_digest = hashlib.sha256(
        canonical_json(segment_input).encode("utf-8")
    ).hexdigest()

    return {
        "package_id": package_id,
        "filter": filter_obj,
        "at_version": av,
        "total_count": total_count,
        "segment": {
            "limit": limit,
            "offset": offset,
            "count": len(items),
            "has_more": offset + len(items) < total_count,
            "order": ["measured_at ASC", "reading_no ASC"],
        },
        "items": items,
        "digest": {
            "algorithm": AUDIT_DIGEST_ALGORITHM,
            "spec_version": AUDIT_DIGEST_SPEC_VERSION,
            "package_digest": package_digest,
            "package_digest_input": {
                "canonical_json": canonical_json(canonical_package),
                "structure": [
                    "spec_version",
                    "filter{device_no,measured_from,measured_to,at_version}",
                    "at_version",
                    "total_count",
                    "items[reading_no,device_no,measured_at,value,"
                    "submitted_at_version,status,reliance,candidates,adjudication]",
                ],
            },
            "segment_digest": segment_digest,
            "segment_digest_input": {
                "canonical_json": canonical_json(segment_input),
            },
            "canonical_rules": _audit_canonical_rules(),
        },
    }

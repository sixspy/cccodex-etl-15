"""落库幂等、整批回滚与回滚单据的领域服务。

口径（与页面「幂等设置」一致）：
- 重复幂等键只允许锁定一种处理：skip_duplicate（跳过并计数）或
  abort_batch（整批失败并自动回滚到落库前）。
- 策略写明在 description 里，当日锁定后不得改弦。
- 落库请求携带的 policy 必须与锁定口径一致，偏离即拒绝（幂等策略写歪了）。
- 同一批次（batch_token）只允许一个抢占者，后到者记幂等争用。
- 幂等键为空禁止落库；回滚进行中禁止再次落库。
- 人工回滚必须填原因，且不能由该批落库操作人本人执行。
"""

import sqlite3
from datetime import datetime, timezone

from app.db import get_db, get_pipeline

POLICY_SKIP = "skip_duplicate"
POLICY_ABORT = "abort_batch"

POLICY_LABELS = {
    POLICY_SKIP: "重复键跳过并计数",
    POLICY_ABORT: "重复键整批失败并回滚",
}

OUTCOME_LABELS = {
    "loaded": "已落库",
    "skipped": "已跳过",
    "aborted": "整批回滚",
    "contended": "幂等争用",
    "loading": "落库中",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# 幂等设置（一天只能写一次，写完当日不得改弦）
# --------------------------------------------------------------------------
def get_policy(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT id, policy, description, set_on, set_by, created_at "
        "FROM idem_settings ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    data["label"] = POLICY_LABELS.get(data["policy"], data["policy"])
    return data


def policy_doc(conn: sqlite3.Connection) -> dict:
    policy = get_policy(conn)
    today = conn.execute("SELECT date('now') AS d").fetchone()["d"]
    locked_today = bool(
        policy
        and conn.execute(
            "SELECT 1 FROM idem_settings WHERE set_on = date('now') LIMIT 1"
        ).fetchone()
    )
    return {
        "policy": policy,
        "today": today,
        "locked_today": locked_today,
        "choices": [
            {
                "value": POLICY_SKIP,
                "label": POLICY_LABELS[POLICY_SKIP],
                "hint": "遇重复幂等键跳过并计数，其余批次照常落库。",
            },
            {
                "value": POLICY_ABORT,
                "label": POLICY_LABELS[POLICY_ABORT],
                "hint": "遇重复幂等键整批失败，并自动回滚到落库前。",
            },
        ],
    }


def _description_matches(policy: str, description: str) -> bool:
    text = description.strip()
    if not text:
        return False
    if policy == POLICY_SKIP:
        return "跳过" in text
    if policy == POLICY_ABORT:
        return "回滚" in text
    return False


def set_policy(user: str, policy: str, description: str) -> dict:
    policy = (policy or "").strip()
    description = (description or "").strip()
    if policy not in POLICY_LABELS:
        raise ApiError(400, "IDEM_POLICY_INVALID", "未知的幂等策略")
    if not _description_matches(policy, description):
        raise ApiError(
            400,
            "IDEM_POLICY_DESCRIPTION_MISMATCH",
            "幂等策略写歪了：说明必须写明重复键的处理方式（跳过/整批回滚）",
        )
    with get_db() as conn:
        today_row = conn.execute(
            "SELECT id, policy, description, set_on, set_by FROM idem_settings "
            "WHERE set_on = date('now') LIMIT 1"
        ).fetchone()
        if today_row:
            raise ApiError(
                409,
                "IDEM_POLICY_LOCKED",
                "当日幂等策略已锁定，写完当日不得改弦",
                {"locked": dict(today_row), "label": POLICY_LABELS.get(today_row["policy"], "")},
            )
        cur = conn.execute(
            "INSERT INTO idem_settings (policy, description, set_on, set_by) "
            "VALUES (?, ?, date('now'), ?)",
            (policy, description, user),
        )
        conn.commit()
        return {
            "id": cur.lastrowid,
            "policy": policy,
            "label": POLICY_LABELS[policy],
            "description": description,
            "set_on": conn.execute("SELECT date('now') AS d").fetchone()["d"],
            "set_by": user,
        }


# --------------------------------------------------------------------------
# 计数与总览
# --------------------------------------------------------------------------
def _count(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> int:
    return int(conn.execute(sql, args).fetchone()["c"])


def overview() -> dict:
    with get_db() as conn:
        policy = get_policy(conn)
        skipped = _count(conn, "SELECT COUNT(*) AS c FROM idem_events WHERE kind = 'skipped'")
        aborted = _count(conn, "SELECT COUNT(*) AS c FROM idem_events WHERE kind = 'aborted'")
        conflicts = _count(conn, "SELECT COUNT(*) AS c FROM idem_events WHERE kind = 'contended'")
        rollbacks = _count(
            conn, "SELECT COUNT(*) AS c FROM rollbacks WHERE status = '已完成'"
        )
        pending_rollbacks = _count(
            conn, "SELECT COUNT(*) AS c FROM rollbacks WHERE status = '进行中'"
        )
        total = _count(conn, "SELECT COUNT(*) AS c FROM pipelines")
        loaded = _count(conn, "SELECT COUNT(*) AS c FROM pipelines WHERE status = '已落库'")
        ready = _count(
            conn, "SELECT COUNT(*) AS c FROM pipelines WHERE status = '已抽取待加载'"
        )
        return {
            "total": total,
            "loaded": loaded,
            "ready": ready,
            "skipped_count": skipped,
            "rollback_count": rollbacks,
            "idem_conflicts": conflicts,
            "aborted_count": aborted,
            "pending_rollbacks": pending_rollbacks,
            "policy_name": policy["policy"] if policy else "",
            "policy_label": policy["label"] if policy else "未设置",
            "policy_description": policy["description"] if policy else "",
            "policy_set_on": policy["set_on"] if policy else "",
        }


# --------------------------------------------------------------------------
# 落库
# --------------------------------------------------------------------------
def _record_event(
    conn: sqlite3.Connection,
    pipeline_id: int,
    idem_key: str,
    kind: str,
    detail: str,
    actor: str,
) -> None:
    conn.execute(
        "INSERT INTO idem_events (pipeline_id, idem_key, kind, detail, actor) "
        "VALUES (?, ?, ?, ?, ?)",
        (pipeline_id, idem_key, kind, detail, actor),
    )


def _restore_pipeline(conn: sqlite3.Connection, batch: dict) -> dict:
    """把管道恢复到该批次落库前的快照，并释放该批次的抢占以便重试。"""
    conn.execute(
        "UPDATE pipelines SET status = ?, loaded = ?, batch_token = ? WHERE id = ?",
        (
            batch["pre_status"],
            batch["pre_loaded"],
            batch["batch_token"],
            batch["pipeline_id"],
        ),
    )
    conn.execute(
        "UPDATE load_claims SET released_at = datetime('now') WHERE batch_token = ?",
        (batch["batch_token"],),
    )
    conn.execute("UPDATE load_batches SET rolled_back = 1 WHERE id = ?", (batch["id"],))
    item = get_pipeline(conn, batch["pipeline_id"])
    return {
        "pipeline_id": batch["pipeline_id"],
        "status": item["status"] if item else batch["pre_status"],
        "loaded": item["loaded"] if item else batch["pre_loaded"],
    }


def _abort_batch(
    conn: sqlite3.Connection,
    duplicate: dict,
    attempted_key: str,
    attempted_pipeline: int,
    actor: str,
    policy: dict,
) -> int | None:
    """按 abort_batch 口径把重复键对应的原批次整批回滚到落库前。

    只有原批次仍是该管道的当前落库结果时才恢复现场；若管道已重新抽取或已被
    其他批次覆盖，则只记事件，不动当前现场，避免回滚旧批次误伤新数据。
    """
    rollback_id = None
    restored = None
    stale = conn.execute(
        "SELECT status, batch_token FROM pipelines WHERE id = ?",
        (duplicate["pipeline_id"],),
    ).fetchone()
    can_restore = (
        duplicate["outcome"] == "loaded"
        and not duplicate["rolled_back"]
        and stale
        and stale["status"] == "已落库"
        and stale["batch_token"] == duplicate["batch_token"]
    )
    if can_restore:
        restored = _restore_pipeline(conn, duplicate)
        cur = conn.execute(
            "INSERT INTO rollbacks (pipeline_id, load_batch_id, reason, operator, mode, "
            "status, restored_status, restored_loaded, finished_at) "
            "VALUES (?, ?, ?, ?, ?, '已完成', ?, ?, ?)",
            (
                duplicate["pipeline_id"],
                duplicate["id"],
                f"重复幂等键 {attempted_key} 触发整批失败，按当日策略「{policy['label']}」"
                f"自动回滚到落库前（原批次 #{duplicate['id']}）",
                "system",
                "整批自动回滚",
                restored["status"],
                restored["loaded"],
                _now(),
            ),
        )
        rollback_id = cur.lastrowid
    detail = (
        f"重复幂等键 {attempted_key}（原批次 #{duplicate['id']}，"
        f"操作人 {duplicate['operator']}），按策略整批回滚"
    )
    _record_event(conn, attempted_pipeline, attempted_key, "aborted", detail, actor)
    return rollback_id


def perform_load(
    pipeline_id: int,
    idem_key: str,
    declared_policy: str | None,
    user: str,
    declared_description: str | None = None,
) -> dict:
    key = (idem_key or "").strip()
    if not key:
        raise ApiError(400, "IDEM_KEY_REQUIRED", "幂等键为空禁止落库")

    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        item = get_pipeline(conn, pipeline_id)
        if not item:
            raise ApiError(404, "PIPELINE_NOT_FOUND", "管道不存在")

        policy = get_policy(conn)
        if not policy:
            raise ApiError(
                409, "IDEM_POLICY_UNSET", "幂等策略尚未锁定，禁止落库"
            )
        if declared_policy and declared_policy != policy["policy"]:
            raise ApiError(
                400,
                "IDEM_POLICY_MISMATCH",
                "幂等策略写歪了：本次落库口径与当日锁定口径不一致",
                {
                    "expected": policy["policy"],
                    "expected_label": policy["label"],
                    "declared": declared_policy,
                },
            )
        if (
            declared_description
            and declared_description.strip() != policy["description"].strip()
        ):
            raise ApiError(
                400,
                "IDEM_POLICY_MISMATCH",
                "幂等策略写歪了：本次落库说明与当日锁定说明不一致",
                {
                    "expected_description": policy["description"],
                    "declared_description": declared_description.strip(),
                },
            )

        pending = conn.execute(
            "SELECT id FROM rollbacks WHERE pipeline_id = ? AND status = '进行中' LIMIT 1",
            (pipeline_id,),
        ).fetchone()
        if pending:
            raise ApiError(
                409,
                "ROLLBACK_IN_PROGRESS",
                "回滚进行中禁止再次落库",
                {"rollback_id": pending["id"]},
            )

        duplicate = conn.execute(
            "SELECT * FROM load_batches WHERE idem_key = ? LIMIT 1", (key,)
        ).fetchone()

        if duplicate:
            duplicate = dict(duplicate)
            if policy["policy"] == POLICY_SKIP:
                _record_event(
                    conn,
                    pipeline_id,
                    key,
                    "skipped",
                    f"重复幂等键命中原批次 #{duplicate['id']}，按策略跳过并计数",
                    user,
                )
                conn.commit()
                skipped_total = _count(
                    conn, "SELECT COUNT(*) AS c FROM idem_events WHERE kind = 'skipped'"
                )
                return {
                    "ok": True,
                    "skipped": True,
                    "policy": policy["policy"],
                    "policy_label": policy["label"],
                    "idem_key": key,
                    "skipped_count": skipped_total,
                    "duplicate_batch": {
                        "id": duplicate["id"],
                        "pipeline_id": duplicate["pipeline_id"],
                        "operator": duplicate["operator"],
                        "loaded": duplicate["loaded"],
                        "rolled_back": bool(duplicate["rolled_back"]),
                    },
                    "pipeline": get_pipeline(conn, pipeline_id),
                    "message": "重复幂等键，按策略跳过并计数",
                }

            rollback_id = _abort_batch(
                conn, duplicate, key, pipeline_id, user, policy
            )
            conn.commit()
            raise ApiError(
                409,
                "IDEM_BATCH_ABORTED",
                "重复幂等键，按策略整批失败并自动回滚到落库前",
                {
                    "policy": policy["policy"],
                    "policy_label": policy["label"],
                    "idem_key": key,
                    "rollback_id": rollback_id,
                    "duplicate_batch_id": duplicate["id"],
                },
            )

        token = item["batch_token"]
        if not token:
            raise ApiError(409, "BATCH_TOKEN_MISSING", "批次令牌缺失，请先重新抽取")

        # 抢占同一批次：每批次同时只允许一个落库请求持有抢占
        # 后到者落在唯一索引上，记幂等争用（重复键口径已在上面处理）
        try:
            conn.execute(
                "INSERT INTO load_claims (batch_token, pipeline_id, idem_key, operator) "
                "VALUES (?, ?, ?, ?)",
                (token, pipeline_id, key, user),
            )
        except (sqlite3.IntegrityError, sqlite3.OperationalError):
            holder = conn.execute(
                "SELECT operator FROM load_claims WHERE batch_token = ? "
                "AND released_at IS NULL LIMIT 1",
                (token,),
            ).fetchone()
            detail = (
                f"批次 {token} 已被 {holder['operator']} 抢占，后到者失败"
                if holder
                else f"批次 {token} 已被其他落库请求抢占，后到者失败"
            )
            _record_event(conn, pipeline_id, key, "contended", detail, user)
            conn.commit()
            raise ApiError(
                409,
                "IDEM_CONTENTION",
                "同一批次并发落库，后到者失败并记幂等争用",
                {"batch_token": token, "idem_key": key},
            )

        if item["status"] != "已抽取待加载":
            raise ApiError(
                409,
                "PIPELINE_NOT_READY",
                "当前状态不能落库，请先抽取",
                {"status": item["status"]},
            )

        pre_status, pre_loaded = item["status"], item["loaded"]
        try:
            cur = conn.execute(
                "INSERT INTO load_batches (pipeline_id, batch_token, idem_key, policy, "
                "operator, outcome, loaded, pre_status, pre_loaded) "
                "VALUES (?, ?, ?, ?, ?, 'loading', ?, ?, ?)",
                (
                    pipeline_id,
                    token,
                    key,
                    policy["policy"],
                    user,
                    item["extracted"],
                    pre_status,
                    pre_loaded,
                ),
            )
        except sqlite3.IntegrityError:
            _record_event(
                conn,
                pipeline_id,
                key,
                "contended",
                f"并发落库同键 {key}，先到者已写入批次",
                user,
            )
            conn.commit()
            raise ApiError(
                409,
                "IDEM_CONTENTION",
                "同一批次并发落库，后到者失败并记幂等争用",
                {"batch_token": token, "idem_key": key},
            )

        batch_id = cur.lastrowid
        updated = conn.execute(
            "UPDATE pipelines SET status = '已落库', loaded = extracted "
            "WHERE id = ? AND status = '已抽取待加载' AND batch_token = ?",
            (pipeline_id, token),
        )
        if updated.rowcount != 1:
            _record_event(
                conn,
                pipeline_id,
                key,
                "contended",
                f"批次 {token} 状态已被其他请求改变",
                user,
            )
            conn.commit()
            raise ApiError(
                409,
                "IDEM_CONTENTION",
                "同一批次并发落库，后到者失败并记幂等争用",
                {"batch_token": token, "idem_key": key},
            )
        conn.execute(
            "UPDATE load_batches SET outcome = 'loaded' WHERE id = ?", (batch_id,)
        )
        return {
            "ok": True,
            "skipped": False,
            "policy": policy["policy"],
            "policy_label": policy["label"],
            "idem_key": key,
            "batch": {
                "id": batch_id,
                "batch_token": token,
                "idem_key": key,
                "operator": user,
                "outcome": "loaded",
                "loaded": item["extracted"],
            },
            "pipeline": get_pipeline(conn, pipeline_id),
            "message": "落库完成",
        }


# --------------------------------------------------------------------------
# 人工回滚
# --------------------------------------------------------------------------
def perform_rollback(pipeline_id: int, reason: str, user: str) -> dict:
    reason = (reason or "").strip()
    if not reason:
        raise ApiError(400, "ROLLBACK_REASON_REQUIRED", "回滚必须填写原因")

    # 第一阶段：校验并落地「进行中」单据（此后禁止再次落库）
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        item = get_pipeline(conn, pipeline_id)
        if not item:
            raise ApiError(404, "PIPELINE_NOT_FOUND", "管道不存在")
        pending = conn.execute(
            "SELECT id FROM rollbacks WHERE pipeline_id = ? AND status = '进行中' LIMIT 1",
            (pipeline_id,),
        ).fetchone()
        if pending:
            raise ApiError(
                409,
                "ROLLBACK_IN_PROGRESS",
                "该管道已有回滚进行中",
                {"rollback_id": pending["id"]},
            )
        batch = conn.execute(
            "SELECT * FROM load_batches WHERE pipeline_id = ? AND outcome = 'loaded' "
            "AND rolled_back = 0 ORDER BY id DESC LIMIT 1",
            (pipeline_id,),
        ).fetchone()
        if not batch:
            raise ApiError(409, "NO_LOADED_BATCH", "没有可回滚的已落库批次")
        batch = dict(batch)
        if batch["operator"] == user:
            raise ApiError(
                403,
                "SELF_ROLLBACK_FORBIDDEN",
                "落库操作人本人不能执行该批次的回滚，请换复核人操作",
                {"load_operator": batch["operator"], "reviewer": user},
            )
        cur = conn.execute(
            "INSERT INTO rollbacks (pipeline_id, load_batch_id, reason, operator, mode, "
            "status, restored_status, restored_loaded) "
            "VALUES (?, ?, ?, ?, '人工回滚', '进行中', ?, ?)",
            (
                pipeline_id,
                batch["id"],
                reason,
                user,
                batch["pre_status"],
                batch["pre_loaded"],
            ),
        )
        rollback_id = cur.lastrowid
        conn.commit()

    # 第二阶段：恢复到该批次落库前，结单
    with get_db() as conn:
        batch_row = conn.execute(
            "SELECT * FROM load_batches WHERE id = ?", (batch["id"],)
        ).fetchone()
        batch_row = dict(batch_row)
        restored = _restore_pipeline(conn, batch_row)
        conn.execute(
            "UPDATE rollbacks SET status = '已完成', restored_status = ?, "
            "restored_loaded = ?, finished_at = ? WHERE id = ?",
            (restored["status"], restored["loaded"], _now(), rollback_id),
        )
        rollback = dict(
            conn.execute("SELECT * FROM rollbacks WHERE id = ?", (rollback_id,)).fetchone()
        )
        return {
            "ok": True,
            "rollback": rollback,
            "pipeline": get_pipeline(conn, pipeline_id),
            "message": "回滚完成，管道已恢复到落库前状态",
        }


# --------------------------------------------------------------------------
# 键板 / 回滚单 / 抢占与批次明细
# --------------------------------------------------------------------------
def list_keys(limit: int = 50) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT b.*, p.title AS pipeline_title, p.code AS pipeline_code, "
            "  (SELECT COUNT(*) FROM idem_events e "
            "   WHERE e.idem_key = b.idem_key AND e.kind = 'skipped') AS skipped_times "
            "FROM load_batches b JOIN pipelines p ON p.id = b.pipeline_id "
            "ORDER BY b.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                **dict(r),
                "outcome_label": OUTCOME_LABELS.get(r["outcome"], r["outcome"]),
                "policy_label": POLICY_LABELS.get(r["policy"], r["policy"]),
                "rolled_back": bool(r["rolled_back"]),
            }
            for r in rows
        ]


def list_rollbacks(limit: int = 50) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT r.*, p.title AS pipeline_title, p.code AS pipeline_code "
            "FROM rollbacks r JOIN pipelines p ON p.id = r.pipeline_id "
            "ORDER BY r.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def pipeline_ledger(pipeline_id: int, limit: int = 20) -> dict:
    with get_db() as conn:
        batches = [
            {
                **dict(r),
                "outcome_label": OUTCOME_LABELS.get(r["outcome"], r["outcome"]),
                "policy_label": POLICY_LABELS.get(r["policy"], r["policy"]),
                "rolled_back": bool(r["rolled_back"]),
            }
            for r in conn.execute(
                "SELECT * FROM load_batches WHERE pipeline_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (pipeline_id, limit),
            ).fetchall()
        ]
        claims = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM load_claims WHERE pipeline_id = ? "
                "ORDER BY claimed_at DESC LIMIT ?",
                (pipeline_id, limit),
            ).fetchall()
        ]
        rollbacks = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM rollbacks WHERE pipeline_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (pipeline_id, limit),
            ).fetchall()
        ]
        events = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM idem_events WHERE pipeline_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (pipeline_id, limit),
            ).fetchall()
        ]
        return {
            "batches": batches,
            "claims": claims,
            "rollbacks": rollbacks,
            "events": events,
        }

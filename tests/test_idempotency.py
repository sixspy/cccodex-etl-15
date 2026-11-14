import concurrent.futures

from app.db import get_db
from tests.conftest import login, set_policy


def _first_pipeline(client) -> int:
    return client.get("/api/pipelines").json()["items"][0]["id"]


def _extract(client, pid: int) -> dict:
    r = client.post(f"/api/pipelines/{pid}/extract")
    assert r.status_code == 200
    return r.json()


def _load(client, pid: int, key: str, policy: str = "skip_duplicate"):
    return client.post(
        f"/api/pipelines/{pid}/load", json={"idem_key": key, "policy": policy}
    )


# ---------------------------------------------------------------- 幂等设置
def test_policy_locked_for_the_day(authed):
    first = set_policy(authed)
    assert first["policy"] == "skip_duplicate"
    again = authed.post(
        "/api/idempotency/policy",
        json={"policy": "abort_batch", "description": "遇重复幂等键整批回滚"},
    )
    assert again.status_code == 409
    assert again.json()["code"] == "IDEM_POLICY_LOCKED"


def test_policy_description_must_match(authed):
    bad = authed.post(
        "/api/idempotency/policy",
        json={"policy": "skip_duplicate", "description": "随便写一句"},
    )
    assert bad.status_code == 400
    assert bad.json()["code"] == "IDEM_POLICY_DESCRIPTION_MISMATCH"
    assert "写歪" in bad.json()["error"]


# ---------------------------------------------------------------- 跳过计数
def test_duplicate_key_skips_and_counts(authed):
    set_policy(authed)
    pid = _first_pipeline(authed)
    _extract(authed, pid)
    first = _load(authed, pid, "dup-skip-1")
    assert first.status_code == 200, first.text
    assert not first.json()["skipped"]

    # 同键再次落库：状态允许与否都必须按策略跳过并计数
    second = _load(authed, pid, "dup-skip-1")
    assert second.status_code == 200
    assert second.json()["skipped"] is True
    assert second.json()["skipped_count"] == 1

    third = _load(authed, pid, "dup-skip-1")
    assert third.json()["skipped_count"] == 2

    ov = authed.get("/api/overview").json()
    assert ov["skipped_count"] == 2
    assert ov["rollback_count"] == 0
    assert ov["policy_name"] == "skip_duplicate"
    keys = authed.get("/api/idempotency/keys").json()["items"]
    assert keys[0]["idem_key"] == "dup-skip-1"
    assert keys[0]["skipped_times"] == 2


# ---------------------------------------------------------------- 整批回滚
def test_duplicate_key_aborts_and_rolls_back_whole_batch(reviewer, authed):
    set_policy(reviewer, "abort_batch", "遇重复幂等键整批回滚到落库前")
    pid = _first_pipeline(authed)
    ex = _extract(authed, pid)
    before_loaded = ex["loaded"]

    first = _load(authed, pid, "dup-abort-1", "abort_batch")
    assert first.status_code == 200, first.text
    assert first.json()["pipeline"]["status"] == "已落库"

    dup = _load(authed, pid, "dup-abort-1", "abort_batch")
    assert dup.status_code == 409
    body = dup.json()
    assert body["code"] == "IDEM_BATCH_ABORTED"
    assert body["rollback_id"]

    detail = authed.get(f"/api/pipelines/{pid}").json()
    assert detail["status"] == "已抽取待加载"
    assert detail["loaded"] == before_loaded

    ov = authed.get("/api/overview").json()
    assert ov["rollback_count"] == 1
    assert ov["aborted_count"] == 1
    assert ov["skipped_count"] == 0

    sheets = authed.get("/api/rollbacks").json()["items"]
    assert sheets and sheets[0]["mode"] == "整批自动回滚"
    assert "重复幂等键" in sheets[0]["reason"]


def test_abort_policy_does_not_skip(authed):
    set_policy(authed, "abort_batch", "遇重复幂等键整批回滚到落库前")
    pid = _first_pipeline(authed)
    _extract(authed, pid)
    assert _load(authed, pid, "dup-abort-only", "abort_batch").status_code == 200
    dup = _load(authed, pid, "dup-abort-only", "abort_batch")
    assert dup.status_code == 409
    assert authed.get("/api/overview").json()["skipped_count"] == 0


# ---------------------------------------------------------------- 写歪策略
def test_load_with_mismatched_policy_blocked(authed):
    set_policy(authed, "skip_duplicate", "遇重复幂等键跳过并计数")
    pid = _first_pipeline(authed)
    _extract(authed, pid)
    bad = _load(authed, pid, "mismatch-1", "abort_batch")
    assert bad.status_code == 400
    assert bad.json()["code"] == "IDEM_POLICY_MISMATCH"
    assert "写歪" in bad.json()["error"]
    # 被拦后没有产生批次
    assert authed.get("/api/idempotency/keys").json()["items"] == []


def test_load_with_mismatched_description_blocked(authed):
    set_policy(authed, "skip_duplicate", "遇重复幂等键跳过并计数")
    pid = _first_pipeline(authed)
    _extract(authed, pid)
    bad = authed.post(
        f"/api/pipelines/{pid}/load",
        json={
            "idem_key": "mismatch-2",
            "policy": "skip_duplicate",
            "policy_description": "遇重复幂等键整批回滚",
        },
    )
    assert bad.status_code == 400
    assert "写歪" in bad.json()["error"]


# ---------------------------------------------------------------- 本人禁回滚
def test_self_rollback_forbidden_and_reviewer_allowed(authed, reviewer):
    set_policy(authed)
    pid = _first_pipeline(authed)
    _extract(authed, pid)
    assert _load(authed, pid, "self-rb-1").status_code == 200

    mine = authed.post(
        f"/api/pipelines/{pid}/rollback", json={"reason": "数据口径调整"}
    )
    assert mine.status_code == 403
    assert mine.json()["code"] == "SELF_ROLLBACK_FORBIDDEN"

    ok = reviewer.post(
        f"/api/pipelines/{pid}/rollback", json={"reason": "数据口径调整"}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["pipeline"]["status"] == "已抽取待加载"
    assert authed.get("/api/overview").json()["rollback_count"] == 1


def test_rollback_requires_reason(authed, reviewer):
    set_policy(authed)
    pid = _first_pipeline(authed)
    _extract(authed, pid)
    assert _load(authed, pid, "reason-1").status_code == 200
    r = reviewer.post(f"/api/pipelines/{pid}/rollback", json={"reason": "   "})
    assert r.status_code == 400
    assert r.json()["code"] == "ROLLBACK_REASON_REQUIRED"


def test_rollback_in_progress_blocks_load(authed):
    set_policy(authed)
    pid = _first_pipeline(authed)
    _extract(authed, pid)
    assert _load(authed, pid, "progress-1").status_code == 200
    # 模拟回滚进行中
    with get_db() as conn:
        conn.execute(
            "INSERT INTO rollbacks (pipeline_id, load_batch_id, reason, operator, mode, "
            "status, restored_status, restored_loaded) "
            "VALUES (?, NULL, '人工排障', 'reviewer', '人工回滚', '进行中', '已抽取待加载', 0)",
            (pid,),
        )
    blocked = _load(authed, pid, "progress-2")
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "ROLLBACK_IN_PROGRESS"


# ---------------------------------------------------------------- 并发争用
def test_concurrent_same_batch_contention(authed):
    set_policy(authed)
    pid = _first_pipeline(authed)
    _extract(authed, pid)

    def worker(i):
        r = authed.post(
            f"/api/pipelines/{pid}/load",
            json={"idem_key": f"concurrent-{i}", "policy": "skip_duplicate"},
        )
        return r.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        codes = list(pool.map(worker, range(4)))

    assert codes.count(200) == 1, codes
    assert codes.count(409) == 3, codes
    ov = authed.get("/api/overview").json()
    assert ov["idem_conflicts"] >= 1
    ledger = authed.get(f"/api/pipelines/{pid}").json()["ledger"]
    assert len(ledger["claims"]) == 1


# ---------------------------------------------------------------- 空键
def test_empty_key_blocked(authed):
    set_policy(authed)
    pid = _first_pipeline(authed)
    _extract(authed, pid)
    for value in ("", "   "):
        r = authed.post(
            f"/api/pipelines/{pid}/load",
            json={"idem_key": value, "policy": "skip_duplicate"},
        )
        assert r.status_code == 400
        assert r.json()["code"] == "IDEM_KEY_REQUIRED"
    missing = authed.post(f"/api/pipelines/{pid}/load", json={})
    assert missing.status_code == 400


def test_load_requires_locked_policy(authed):
    pid = _first_pipeline(authed)
    _extract(authed, pid)
    r = _load(authed, pid, "no-policy-1")
    assert r.status_code == 409
    assert r.json()["code"] == "IDEM_POLICY_UNSET"


# ---------------------------------------------------------------- 页面可读
def test_page_reads_idempotency_and_rollback(client, authed, reviewer):
    html = authed.get("/app/").text
    for text in ("幂等设置", "幂等键板", "回滚单据", "跳过数", "回滚次数", "幂等冲突"):
        assert text in html

    set_policy(authed)
    pid = _first_pipeline(authed)
    _extract(authed, pid)
    assert _load(authed, pid, "page-1").status_code == 200
    assert reviewer.post(
        f"/api/pipelines/{pid}/rollback", json={"reason": "页面可读校验"}
    ).status_code == 200

    ov = authed.get("/api/overview").json()
    assert ov["rollback_count"] == 1
    keys = authed.get("/api/idempotency/keys").json()["items"]
    assert keys[0]["idem_key"] == "page-1"
    sheets = authed.get("/api/rollbacks").json()["items"]
    assert sheets[0]["reason"] == "页面可读校验"

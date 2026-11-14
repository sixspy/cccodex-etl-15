import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from app.config import Config

SEED_PIPELINES = [
    ("订单明细日抽", "orders_daily", "待抽取", "mock://orders/day"),
    ("用户画像宽表", "user_profile", "待抽取", "mock://users/profile"),
    ("库存快照落地", "inventory_snap", "待抽取", "mock://stock/snap"),
    ("支付流水清洗", "pay_clean", "待抽取", "mock://pay/flow"),
]


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pipelines (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              code TEXT NOT NULL UNIQUE,
              status TEXT NOT NULL,
              source_uri TEXT NOT NULL,
              extracted INTEGER NOT NULL DEFAULT 0,
              loaded INTEGER NOT NULL DEFAULT 0,
              batch_token TEXT,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );

            -- 幂等策略：一天只能落一条，写完当日不得改弦
            CREATE TABLE IF NOT EXISTS idem_settings (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              policy TEXT NOT NULL,
              description TEXT NOT NULL,
              set_on TEXT NOT NULL,
              set_by TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_idem_settings_day
              ON idem_settings(set_on);

            -- 每次落库请求对应一条批次记录（幂等键唯一）
            CREATE TABLE IF NOT EXISTS load_batches (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              pipeline_id INTEGER NOT NULL REFERENCES pipelines(id),
              batch_token TEXT NOT NULL,
              idem_key TEXT NOT NULL,
              policy TEXT NOT NULL,
              operator TEXT NOT NULL,
              outcome TEXT NOT NULL,
              loaded INTEGER NOT NULL DEFAULT 0,
              pre_status TEXT NOT NULL,
              pre_loaded INTEGER NOT NULL DEFAULT 0,
              rolled_back INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_load_batches_key
              ON load_batches(idem_key);

            -- 同一批次只允许一个有效落库抢占者；冲突即幂等争用
            -- 回滚释放后允许同一批次重新抢占，历史行仍保留为记录
            CREATE TABLE IF NOT EXISTS load_claims (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_token TEXT NOT NULL,
              pipeline_id INTEGER NOT NULL,
              idem_key TEXT NOT NULL,
              operator TEXT NOT NULL,
              claimed_at TEXT NOT NULL DEFAULT (datetime('now')),
              released_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_load_claims_active
              ON load_claims(batch_token) WHERE released_at IS NULL;

            -- 跳过、整批回滚、争用等幂等事件（服务端计数用）
            CREATE TABLE IF NOT EXISTS idem_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              pipeline_id INTEGER NOT NULL,
              idem_key TEXT NOT NULL,
              kind TEXT NOT NULL,
              detail TEXT NOT NULL DEFAULT '',
              actor TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- 回滚单据：人工回滚与整批自动回滚都留单
            CREATE TABLE IF NOT EXISTS rollbacks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              pipeline_id INTEGER NOT NULL,
              load_batch_id INTEGER,
              reason TEXT NOT NULL,
              operator TEXT NOT NULL,
              mode TEXT NOT NULL,
              status TEXT NOT NULL,
              restored_status TEXT NOT NULL,
              restored_loaded INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL DEFAULT (datetime('now')),
              finished_at TEXT
            );
            """
        )
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(pipelines)")}
        if "batch_token" not in cols:
            conn.execute("ALTER TABLE pipelines ADD COLUMN batch_token TEXT")
        n = conn.execute("SELECT COUNT(*) AS c FROM pipelines").fetchone()["c"]
        if n == 0:
            conn.executemany(
                "INSERT INTO pipelines (title, code, status, source_uri) VALUES (?,?,?,?)",
                SEED_PIPELINES,
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('lab_name', ?)",
                (Config.LAB_NAME,),
            )


def list_pipelines(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, title, code, status, source_uri, extracted, loaded, batch_token, created_at "
        "FROM pipelines ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_pipeline(conn: sqlite3.Connection, pipeline_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, title, code, status, source_uri, extracted, loaded, batch_token, created_at "
        "FROM pipelines WHERE id = ?",
        (pipeline_id,),
    ).fetchone()
    return dict(row) if row else None


def run_extract(conn: sqlite3.Connection, pipeline_id: int) -> dict | None:
    item = get_pipeline(conn, pipeline_id)
    if not item:
        return None
    if item["status"] not in ("待抽取", "抽取失败"):
        return item
    extracted = 12 + (pipeline_id % 5)
    # 每次抽取生成新的批次令牌，落库抢占以批次令牌为口径
    token = f"{pipeline_id}-{extracted}-{uuid.uuid4().hex[:10]}"
    conn.execute(
        "UPDATE pipelines SET status = ?, extracted = ?, loaded = 0, batch_token = ? WHERE id = ?",
        ("已抽取待加载", extracted, token, pipeline_id),
    )
    return get_pipeline(conn, pipeline_id)

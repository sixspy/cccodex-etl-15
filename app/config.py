import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Config:
    SECRET = os.environ.get("APP_SECRET", "cc-etl-lab-dev-secret")
    ADMIN_USER = "admin"
    ADMIN_PASSWORD = "123456"
    # 回滚复核员：不能回滚自己发起的落库
    REVIEWER_USER = "reviewer"
    REVIEWER_PASSWORD = "123456"
    LAB_NAME = "抽取转换加载台"

    USERS = {
        ADMIN_USER: (ADMIN_PASSWORD, "落库操作员"),
        REVIEWER_USER: (REVIEWER_PASSWORD, "回滚复核员"),
    }

    @staticmethod
    def database() -> str:
        return os.environ.get("DATABASE_PATH", str(ROOT / "instance" / "etl_lab.db"))

    @staticmethod
    def role_of(user: str) -> str:
        row = Config.USERS.get(user)
        return row[1] if row else ""

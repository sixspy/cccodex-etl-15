# cc-etl-lab

CC_Codex 二期（26.11）**数据工程**产品线：**抽取转换加载台**（FastAPI + Vue 3 SPA + SQLite）。

与 `cc-data-lab`（Flask + HTMX 采集控制台）错开：本线面向 **ETL 管道编排 / 抽取落库**，前端为 Vue 单页，暖色仓运风格。

## 领域

- **所属领域**：数据工程  
- **子方向**：抽取-转换-加载 / 数据采集与落库  

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、FastAPI、Uvicorn、SQLite |
| 前端 | Vue 3（CDN Composition）、双栏管道台 |
| 风格 | 暖琥珀仓运底、圆角强调、等宽源地址 |
| 测试 | pytest + httpx |

## 基线能力

登录、管道列表、管道详情、执行抽取、执行落库。

## 落库幂等与回滚

- **幂等设置**：每日只能锁定一种重复键口径，写进说明后当日不得改弦（跳过并计数 / 整批失败并自动回滚）。落库请求声明的口径或说明与锁定值不一致时，返回「幂等策略写歪了」。
- **幂等键板**：键、批次令牌、操作人与跳过次数可读。
- **落库**：幂等键为空禁止落库；同一批次并发落库后到者失败并记幂等争用；回滚进行中禁止再次落库。
- **回滚**：必须填原因，且不能由该批落库操作人本人执行；整批自动回滚与人工回滚都留回滚单据。
- **总览**：跳过数、回滚次数、幂等冲突、策略名；详情可读回滚单、落库抢占记录与幂等事件。

账号：`admin / 123456`（落库操作员）、`reviewer / 123456`（回滚复核员）。

## 本地

```powershell
cd claude-workspace\cc-etl-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:6000/login · **admin / 123456**

## Docker

```powershell
docker compose up -d --build
```

端口 **6000**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`

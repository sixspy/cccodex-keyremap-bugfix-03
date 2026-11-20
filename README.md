# 实体键桥

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

实体键映射生效后入站先重映再进正式区，冲突进队列，映射版本与正式区对账

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 实体键桥 |
| 目录 | `07-keyremap` |
| 领域建议 | 数仓与数据湖 |
| 接口 | http://127.0.0.1:10928 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

一对多/多对一、生效窗、旧键只读、重映重放

## 避开

别做成 Feed 冲突归并；正式区不是 staging 换皮

## 互斥切口候选

- 冲突拒收整批 vs 进冲突区后重放
- 废弃旧键只读保留 vs 重写正式键
- 生效窗原子切换 vs 双窗按事件时裁

## 冲突漏计切断（2026-11-20 锁定）

择定**甲路径**，详见 `docs/漏计切断卡.md`：

- 无映射键先写冲突行（带 `batch_no`），再允许批继续；任一失败整笔回滚，正式零增。
- 无映射键永不写 `formal_entities`；有映射键与冲突行同事务提交。
- 同批号并发：先到者持批锁，后到者记争用并返回 423；批终结后重扫同批号同样记争用。
- 五处同态：冲突列表 / 正式实体 / 批回执 / 互证页 / 入站残留。

批链路接口：

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/batch-remap` | 批重映，body `{batch_no, old_keys[]}` |
| GET | `/api/conflicts?batch_no=` | 按批查冲突 |
| GET | `/api/formal?batch_no=` | 按批查正式实体 |
| GET | `/api/batches/{batch_no}/receipt` | 批回执 |
| GET | `/api/attestation?batch_no=` | 互证页（五处同态检查） |
| GET | `/api/inbound?batch_no=` | 入站残留 |
| GET | `/api/contention?batch_no=` | 抢批争用记录 |
| GET | `/api/deviations` | 偏离切断卡记账（漏计偏了） |

夹具：`fixtures/batch_unmapped_mixed.json`（混批漏计）、`fixtures/batch_contention.json`（抢批）。

## 启动（一键）

`powershell
cd projects/07-keyremap
docker compose up -d --build
`

浏览器：http://127.0.0.1:10928/  
健康检查：GET http://127.0.0.1:10928/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10604
python -m pytest -q
`

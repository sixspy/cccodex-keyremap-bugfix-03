
import hashlib, secrets, sqlite3
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

# 批锁探针：仅用于并发测试在“已持锁”与“写入”之间制造确定性窗口。
BATCH_LOCK_PROBE = None

def connect(autocommit: bool = False):
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    if autocommit:
        conn.isolation_level = None
    return conn

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from datetime import datetime
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "实体键桥"
SKELETON = "实体键映射生效后入站先重映再进正式区，冲突进队列，映射版本与正式区对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def _ensure_column(conn, table: str, column: str, decl: str):
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS map_versions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT UNIQUE NOT NULL,
          status TEXT NOT NULL,
          effective_from TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mappings(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT NOT NULL,
          old_key TEXT NOT NULL,
          new_key TEXT NOT NULL,
          kind TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS formal_entities(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          entity_key TEXT NOT NULL,
          mapped_from TEXT NOT NULL,
          version TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conflicts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          old_key TEXT NOT NULL,
          reason TEXT NOT NULL,
          status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS batch_receipts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          batch_no TEXT NOT NULL,
          owner TEXT NOT NULL,
          status TEXT NOT NULL,
          ended_flag INTEGER NOT NULL,
          conflict_count INTEGER NOT NULL,
          formal_count INTEGER NOT NULL,
          inbound_count INTEGER NOT NULL,
          started_at TEXT NOT NULL,
          finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS batch_locks(
          batch_no TEXT PRIMARY KEY,
          owner TEXT NOT NULL,
          acquired_at TEXT NOT NULL,
          released_at TEXT
        );
        CREATE TABLE IF NOT EXISTS batch_contention(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          batch_no TEXT NOT NULL,
          owner TEXT NOT NULL,
          holder TEXT NOT NULL,
          at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS inbound_rows(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          batch_no TEXT NOT NULL,
          old_key TEXT NOT NULL,
          disposition TEXT NOT NULL,
          entity_key TEXT,
          conflict_id INTEGER,
          at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cut_deviations(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          batch_no TEXT,
          detail TEXT NOT NULL,
          at TEXT NOT NULL
        );
        """)
        _ensure_column(conn, "conflicts", "batch_no", "TEXT")
        _ensure_column(conn, "formal_entities", "batch_no", "TEXT")
        if conn.execute("SELECT COUNT(*) c FROM map_versions").fetchone()["c"] == 0:
            conn.execute("INSERT INTO map_versions(version,status,effective_from) VALUES (?,?,?)", ("m-2026.11","生效","2026-11-01"))
            conn.executemany("INSERT INTO mappings(version,old_key,new_key,kind) VALUES (?,?,?,?)", [
                ("m-2026.11","OLD-A1","NEW-100","一对一"),
                ("m-2026.11","OLD-B1","NEW-200","多对一"),
                ("m-2026.11","OLD-B2","NEW-200","多对一"),
            ])
            conn.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) VALUES (?,?,?,?)",
                         ("NEW-100","OLD-A1","m-2026.11","2026-11-10T09:00:00"))
            conn.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)", ("OLD-C9","无映射","挂起"))

@app.on_event("startup")
def _s(): init_db()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM mappings").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "mappings": n, "db": str(DB_PATH)}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/map-versions")
def versions(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM map_versions ORDER BY id").fetchall()]}

@app.get("/api/mappings")
def mappings(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM mappings ORDER BY id").fetchall()]}

class RemapIn(BaseModel):
    old_key: str

@app.post("/api/remap")
def remap(body: RemapIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as c:
        ver = c.execute("SELECT version FROM map_versions WHERE status='生效' ORDER BY id DESC LIMIT 1").fetchone()
        if not ver: raise HTTPException(404, "无生效映射版本")
        m = c.execute("SELECT * FROM mappings WHERE version=? AND old_key=?", (ver["version"], body.old_key)).fetchone()
        if not m:
            c.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)", (body.old_key, "无映射", "挂起"))
            c.commit()
            raise HTTPException(409, "无映射，已进冲突队列")
        c.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) VALUES (?,?,?,?)",
                  (m["new_key"], body.old_key, ver["version"], now))
        return {"entity_key": m["new_key"], "version": ver["version"]}

class BatchIn(BaseModel):
    batch_no: str
    old_keys: list[str]

def _effective_version(conn):
    return conn.execute("SELECT version FROM map_versions WHERE status='生效' ORDER BY id DESC LIMIT 1").fetchone()

def _record_deviation(c, batch_no: str | None, detail: str, now: str):
    c.execute("INSERT INTO cut_deviations(batch_no,detail,at) VALUES (?,?,?)",
              (batch_no, "漏计偏了：" + detail, now))

def _acquire_batch_lock(c, batch_no: str, owner: str, now: str) -> str:
    """独立短事务占批锁并立即提交，使后到批可见。

    返回本扫描号 owner 表示持锁成功；返回其它值表示已被占用或批已终结。
    后到批无论撞上正在持锁的扫描，还是撞上已出具回执的终结批，都不得再扫。
    """
    c.execute("BEGIN IMMEDIATE")
    try:
        row = c.execute("SELECT owner FROM batch_locks WHERE batch_no=?", (batch_no,)).fetchone()
        if row:
            c.execute("ROLLBACK")
            return row["owner"]
        done = c.execute("SELECT owner FROM batch_receipts WHERE batch_no=? ORDER BY id DESC LIMIT 1",
                         (batch_no,)).fetchone()
        if done:
            c.execute("ROLLBACK")
            return done["owner"]
        c.execute("INSERT INTO batch_locks(batch_no,owner,acquired_at) VALUES (?,?,?)", (batch_no, owner, now))
        c.execute("COMMIT")
        return owner
    except Exception:
        c.execute("ROLLBACK")
        raise

def _record_contention(c, batch_no: str, owner: str, holder: str, now: str):
    c.execute("BEGIN IMMEDIATE")
    try:
        c.execute("INSERT INTO batch_contention(batch_no,owner,holder,at) VALUES (?,?,?,?)",
                  (batch_no, owner, holder, now))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise

@app.post("/api/batch-remap")
def batch_remap(body: BatchIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    now = datetime.now().isoformat(timespec="seconds")
    batch_no = (body.batch_no or "").strip()
    if not batch_no:
        raise HTTPException(400, "批号不能为空")
    owner = f"{user}#{secrets.token_hex(4)}"

    with connect(autocommit=True) as c:
        ver = _effective_version(c)
        if not ver:
            raise HTTPException(404, "无生效映射版本")

        holder = _acquire_batch_lock(c, batch_no, owner, now)
        if holder != owner:
            _record_contention(c, batch_no, owner, holder, now)
            raise HTTPException(423, f"批号已被 {holder} 占用，记争用")
        # 已占批锁，进入扫描窗口（并发测试在此制造确定性窗口）。
        probe = BATCH_LOCK_PROBE
        if probe:
            probe(batch_no, owner)

        conflict_count = 0
        formal_count = 0
        c.execute("BEGIN IMMEDIATE")
        try:
            # 甲路径：无映射键先插冲突行，再允许批继续；任一失败整笔回滚。
            conflict_ids: dict[str, int] = {}
            for old_key in body.old_keys:
                m = c.execute("SELECT * FROM mappings WHERE version=? AND old_key=?",
                              (ver["version"], old_key)).fetchone()
                if m:
                    continue
                cur = c.execute("INSERT INTO conflicts(old_key,reason,status,batch_no) VALUES (?,?,?,?)",
                                (old_key, "无映射", "挂起", batch_no))
                conflict_ids[old_key] = cur.lastrowid

            # 冲突行全部落地后，才允许写正式行。
            for old_key in body.old_keys:
                m = c.execute("SELECT * FROM mappings WHERE version=? AND old_key=?",
                              (ver["version"], old_key)).fetchone()
                if not m:
                    continue
                c.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at,batch_no) VALUES (?,?,?,?,?)",
                          (m["new_key"], old_key, ver["version"], now, batch_no))
                formal_count += 1

            # 入站残留与两侧同态。
            for old_key in body.old_keys:
                m = c.execute("SELECT * FROM mappings WHERE version=? AND old_key=?",
                              (ver["version"], old_key)).fetchone()
                if m:
                    c.execute("INSERT INTO inbound_rows(batch_no,old_key,disposition,entity_key,conflict_id,at) VALUES (?,?,?,?,?,?)",
                              (batch_no, old_key, "正式", m["new_key"], None, now))
                else:
                    c.execute("INSERT INTO inbound_rows(batch_no,old_key,disposition,entity_key,conflict_id,at) VALUES (?,?,?,?,?,?)",
                              (batch_no, old_key, "冲突", None, conflict_ids[old_key], now))
                    conflict_count += 1

            c.execute("INSERT INTO batch_receipts(batch_no,owner,status,ended_flag,conflict_count,formal_count,inbound_count,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?)",
                      (batch_no, owner, "绿", 1, conflict_count, formal_count, len(body.old_keys), now, now))
            c.execute("DELETE FROM batch_locks WHERE batch_no=? AND owner=?", (batch_no, owner))
            c.execute("COMMIT")
        except Exception as exc:
            c.execute("ROLLBACK")
            try:
                c.execute("BEGIN IMMEDIATE")
                _record_deviation(c, batch_no, "批写入失败已整笔回滚:" + repr(exc), now)
                c.execute("DELETE FROM batch_locks WHERE batch_no=? AND owner=?", (batch_no, owner))
                c.execute("COMMIT")
            except Exception:
                try:
                    c.execute("ROLLBACK")
                except Exception:
                    pass
            raise HTTPException(500, "批写入失败，整笔回滚，正式零增")
    return {"batch_no": batch_no, "status": "绿", "ended_flag": 1,
            "conflict_count": conflict_count, "formal_count": formal_count,
            "inbound_count": len(body.old_keys), "owner": owner}

@app.get("/api/contention")
def contention(authorization: str | None = Header(default=None), batch_no: str | None = None):
    require_user(authorization)
    with connect() as c:
        if batch_no:
            rows = c.execute("SELECT * FROM batch_contention WHERE batch_no=? ORDER BY id", (batch_no,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM batch_contention ORDER BY id").fetchall()
        return {"items":[dict(r) for r in rows]}

@app.get("/api/deviations")
def deviations(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM cut_deviations ORDER BY id DESC").fetchall()]}

@app.get("/api/formal")
def formal(authorization: str | None = Header(default=None), batch_no: str | None = None):
    require_user(authorization)
    with connect() as c:
        if batch_no:
            rows = c.execute("SELECT * FROM formal_entities WHERE batch_no=? ORDER BY id DESC", (batch_no,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM formal_entities ORDER BY id DESC").fetchall()
        return {"items":[dict(r) for r in rows]}

@app.get("/api/conflicts")
def conflicts(authorization: str | None = Header(default=None), batch_no: str | None = None):
    require_user(authorization)
    with connect() as c:
        if batch_no:
            rows = c.execute("SELECT * FROM conflicts WHERE batch_no=? ORDER BY id DESC", (batch_no,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM conflicts ORDER BY id DESC").fetchall()
        return {"items":[dict(r) for r in rows]}

@app.get("/api/batches")
def batches(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM batch_receipts ORDER BY id DESC").fetchall()]}

@app.get("/api/batches/{batch_no}/receipt")
def batch_receipt(batch_no: str, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        row = c.execute("SELECT * FROM batch_receipts WHERE batch_no=? ORDER BY id DESC LIMIT 1", (batch_no,)).fetchone()
        if not row:
            raise HTTPException(404, "无批回执")
        return dict(row)

@app.get("/api/inbound")
def inbound(authorization: str | None = Header(default=None), batch_no: str | None = None):
    require_user(authorization)
    with connect() as c:
        if batch_no:
            rows = c.execute("SELECT * FROM inbound_rows WHERE batch_no=? ORDER BY id", (batch_no,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM inbound_rows ORDER BY id DESC").fetchall()
        return {"items":[dict(r) for r in rows]}

def _mapped_new_keys(conn, version: str) -> set[str]:
    return {r["new_key"] for r in conn.execute("SELECT new_key FROM mappings WHERE version=?", (version,))}

def _reconcile(conn, batch_no: str) -> dict:
    receipt = conn.execute("SELECT * FROM batch_receipts WHERE batch_no=? ORDER BY id DESC LIMIT 1", (batch_no,)).fetchone()
    conflicts_rows = conn.execute("SELECT * FROM conflicts WHERE batch_no=? ORDER BY id", (batch_no,)).fetchall()
    formal_rows = conn.execute("SELECT * FROM formal_entities WHERE batch_no=? ORDER BY id", (batch_no,)).fetchall()
    inbound_rows = conn.execute("SELECT * FROM inbound_rows WHERE batch_no=? ORDER BY id", (batch_no,)).fetchall()
    checks = []

    allowed_new = _mapped_new_keys(conn, _effective_version(conn)["version"]) if _effective_version(conn) else set()
    dirty = [r["entity_key"] for r in formal_rows if r["entity_key"] not in allowed_new]
    checks.append({"name": "正式区无脏新键", "ok": not dirty, "detail": "脏键=" + ",".join(dirty)})

    registered = {r["old_key"] for r in conflicts_rows}
    missing = [r["old_key"] for r in inbound_rows if r["disposition"] == "冲突" and r["old_key"] not in registered]
    checks.append({"name": "冲突行先于批继续", "ok": not missing, "detail": "漏登=" + ",".join(missing)})

    counts_ok = bool(receipt) and receipt["conflict_count"] == len(conflicts_rows) \
        and receipt["formal_count"] == len(formal_rows) and receipt["inbound_count"] == len(inbound_rows)
    checks.append({"name": "批回执计数与四处一致", "ok": counts_ok,
                   "detail": "" if counts_ok else "回执与列表不一致"})

    # 无映射入站必须出现在冲突列表；有映射入站必须出现在正式区。
    formal_from = {r["mapped_from"] for r in formal_rows}
    inbound_ok = all(
        (r["old_key"] in registered) if r["disposition"] == "冲突" else (r["old_key"] in formal_from)
        for r in inbound_rows
    )
    checks.append({"name": "入站残留与两侧同态", "ok": inbound_ok, "detail": "" if inbound_ok else "入站残留不一致"})

    mismatches = [c["name"] + ":" + c["detail"] for c in checks if not c["ok"]]
    return {"batch_no": batch_no, "ok": not mismatches, "checks": checks, "mismatches": mismatches,
            "counts": {"conflicts": len(conflicts_rows), "formal": len(formal_rows), "inbound": len(inbound_rows)}}

@app.get("/api/reconcile")
def reconcile(authorization: str | None = Header(default=None), batch_no: str | None = None):
    require_user(authorization)
    if not batch_no:
        raise HTTPException(400, "须指定批号")
    with connect() as c:
        return _reconcile(c, batch_no)

@app.get("/api/attestation")
def attestation(authorization: str | None = Header(default=None), batch_no: str | None = None):
    require_user(authorization)
    if not batch_no:
        raise HTTPException(400, "须指定批号")
    with connect() as c:
        return _reconcile(c, batch_no)

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")

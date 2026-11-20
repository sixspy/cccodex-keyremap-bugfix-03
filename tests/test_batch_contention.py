
"""抢批争用失败测：两批同时扫同一批号，后到者必须记争用，不得并行写正式区。"""
import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
from app import app

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "batch_contention.json"
BATCH = json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_same_batch_second_writer_records_contention(monkeypatch):
    c = TestClient(app)
    token = c.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
    h = {"Authorization": "Bearer " + token}
    batch_no = BATCH["batch_no"]

    entered = threading.Event()
    release = threading.Event()

    def probe(batch_no_seen, owner):
        # 只在首个持锁批上卡住；后到批不会走到这里。
        if not entered.is_set():
            entered.set()
            release.wait(5)

    monkeypatch.setattr(app_module, "BATCH_LOCK_PROBE", probe)

    first = {}

    def run_first():
        first["resp"] = c.post("/api/batch-remap", headers=h, json=BATCH)

    t = threading.Thread(target=run_first)
    t.start()

    # 持锁批必须已进入扫描窗口；基线无批锁时这里会超时，测试随之见红。
    assert entered.wait(3), "首个批未持有批锁进入扫描窗口（批锁缺失）"

    second = c.post("/api/batch-remap", headers=h, json=BATCH)
    assert second.status_code == 423, second.text

    release.set()
    t.join(10)
    assert not t.is_alive(), "首个批未在释放后结束"
    assert first["resp"].status_code == 200, first["resp"].text

    contention = c.get("/api/contention", headers=h, params={"batch_no": batch_no}).json()["items"]
    assert len(contention) == 1, contention
    assert contention[0]["batch_no"] == batch_no
    assert contention[0]["owner"] != contention[0]["holder"]

    # 争用不得产生第二份正式行或第二份回执。夹具仅 OLD-A1 有映射，正式 1 条。
    assert len(c.get("/api/formal", headers=h, params={"batch_no": batch_no}).json()["items"]) == 1
    assert len(c.get("/api/batches", headers=h).json()["items"]) == 1


def test_finished_batch_cannot_be_rescanned():
    """批终结后重扫同批号，按争用拒绝，不得再增正式行或回执。"""
    c = TestClient(app)
    token = c.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
    h = {"Authorization": "Bearer " + token}
    batch_no = BATCH["batch_no"]

    assert c.post("/api/batch-remap", headers=h, json=BATCH).status_code == 200
    again = c.post("/api/batch-remap", headers=h, json=BATCH)
    assert again.status_code == 423, again.text

    assert len(c.get("/api/formal", headers=h, params={"batch_no": batch_no}).json()["items"]) == 1
    assert len(c.get("/api/batches", headers=h).json()["items"]) == 1
    assert len(c.get("/api/contention", headers=h, params={"batch_no": batch_no}).json()["items"]) == 1

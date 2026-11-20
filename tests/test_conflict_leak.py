
"""漏计失败测：无映射键混批，冲突计数却为零，且脏新键混入正式区。

夹具 fixtures/batch_unmapped_mixed.json 的批里 OLD-ZZ-01 / OLD-ZZ-02 无映射，
同批还有 OLD-A1 / OLD-B1 / OLD-B2 有映射。切断卡口径：甲路径。
"""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app import app

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "batch_unmapped_mixed.json"
BATCH = json.loads(FIXTURE.read_text(encoding="utf-8"))
MAPPED_NEW = {"NEW-100", "NEW-200"}
UNMAPPED = {"OLD-ZZ-01", "OLD-ZZ-02"}


def _client_and_headers():
    c = TestClient(app)
    token = c.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
    return c, {"Authorization": "Bearer " + token}


def test_unmapped_in_mixed_batch_does_not_leak_into_formal():
    c, h = _client_and_headers()
    batch_no = BATCH["batch_no"]

    r = c.post("/api/batch-remap", headers=h, json=BATCH)
    assert r.status_code == 200, r.text
    receipt_inline = r.json()

    # 1) 批回执：冲突应收两条，正式应收三条。
    assert receipt_inline["conflict_count"] == len(UNMAPPED), receipt_inline
    assert receipt_inline["formal_count"] == 3, receipt_inline
    assert receipt_inline["inbound_count"] == len(BATCH["old_keys"]), receipt_inline
    assert receipt_inline["ended_flag"] == 1, receipt_inline

    # 2) 冲突列表：无映射旧键必须可查。
    conflicts = c.get("/api/conflicts", headers=h, params={"batch_no": batch_no}).json()["items"]
    assert {x["old_key"] for x in conflicts} == UNMAPPED, conflicts

    # 3) 正式实体：脏新键不得出现。
    formal = c.get("/api/formal", headers=h, params={"batch_no": batch_no}).json()["items"]
    dirty = [f["entity_key"] for f in formal if f["entity_key"] not in MAPPED_NEW]
    assert dirty == [], f"正式区混入无映射旧键对应的脏新键: {dirty}"
    assert len(formal) == 3, formal

    # 4) 批回执落库值与列表同态。
    receipt = c.get(f"/api/batches/{batch_no}/receipt", headers=h).json()
    assert receipt["conflict_count"] == len(conflicts), receipt
    assert receipt["formal_count"] == len(formal), receipt
    inbound = c.get("/api/inbound", headers=h, params={"batch_no": batch_no}).json()["items"]
    assert receipt["inbound_count"] == len(inbound), receipt
    assert len(inbound) == len(BATCH["old_keys"]), inbound

    # 5) 入站残留：无映射键落在冲突侧，有映射键落在正式侧。
    by_key = {x["old_key"]: x for x in inbound}
    for key in UNMAPPED:
        assert by_key[key]["disposition"] == "冲突", by_key[key]
    for key in ("OLD-A1", "OLD-B1", "OLD-B2"):
        assert by_key[key]["disposition"] == "正式", by_key[key]

    # 6) 互证页与对账口径一致。
    attest = c.get("/api/attestation", headers=h, params={"batch_no": batch_no}).json()
    assert attest["ok"] is True, attest
    assert attest["counts"] == {"conflicts": len(UNMAPPED), "formal": 3, "inbound": len(BATCH["old_keys"])}, attest

    # 7) 冲突行必须带批号，供冲突查询链路按批回溯。
    assert all(x["batch_no"] == batch_no for x in conflicts), conflicts

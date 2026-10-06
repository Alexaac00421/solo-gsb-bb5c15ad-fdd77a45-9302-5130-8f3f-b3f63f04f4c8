"""End-to-end and concurrency tests for venue-day progress certificates.

覆盖：
- 封存口径：当前排片落在该日本地时间范围（IANA 时区本地午夜到次日午夜，
  左闭右开）内、已核准且未取消的场次；明细含原核验键、当前开场时间、
  当前授权与额度池、已分配拷贝编号、开场码核销时间（未核销为空）
- 汇总：已核销/未核销场数，未开场场次计入未核销；空日返回空清单与零计数
- 校验：非法日期 422、未知 IANA 时区 422 UNKNOWN_TIMEZONE，均不留记录
- 幂等：同凭证号同参数重试返回首次快照（replay=true），同号异参数 409
- 封存后内容不可变：之后的改签/取消/核销不影响已封存凭证，查询恒为首次内容
- 并发：与改签/取消/核销并发时凭证只反映一个完整状态；同号并发封存只落一份
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import service
from app.main import create_app
from app.timeutil import to_iso

WINDOW = {
    "valid_from": "2020-01-01T00:00:00Z",
    "valid_to": "2031-01-01T00:00:00Z",
}


def canon(value: str) -> str:
    """Turn an ISO-8601 string into the canonical fixed-width UTC form."""
    return to_iso(datetime.fromisoformat(value.replace("Z", "+00:00")))


def jump(monkeypatch, value: str) -> None:
    """Freeze the service clock at ``value`` (ISO-8601)."""
    monkeypatch.setattr(service, "now_iso", lambda: canon(value))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 默认冻结在 2026-06-01T18:00:00Z：之后的场次均"未开场"，
    # 便于领码/核销/取消/改签的时钟门槛；核验与封存本身不涉及时钟
    jump(monkeypatch, "2026-06-01T18:00:00Z")
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def make_license(client, **overrides):
    body = {
        "film_id": "film-1",
        "venue_id": "hall-a",
        "valid_from": WINDOW["valid_from"],
        "valid_to": WINDOW["valid_to"],
        "max_sessions": 50,
    }
    body.update(overrides)
    resp = client.post("/licenses", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def verify(client, key, start, film="film-1", venue="hall-a"):
    return client.post(
        "/verifications",
        json={
            "film_id": film,
            "venue_id": venue,
            "start_time": start,
            "idempotency_key": key,
        },
    )


def seal(client, no, venue="hall-a", date="2026-10-06", tz="UTC"):
    return client.post(
        "/progress-certificates",
        json={
            "certificate_no": no,
            "venue_id": venue,
            "local_date": date,
            "timezone": tz,
        },
    )


# ----------------------------------------------------------------------
# seal: snapshot scope and item fields
# ----------------------------------------------------------------------
def test_seal_basic_snapshot_fields(client):
    lic = make_license(client)
    verify(client, "pc-1", "2026-10-06T10:00:00Z")
    verify(client, "pc-2", "2026-10-06T12:30:00Z")
    resp = seal(client, "CERT-1")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["replay"] is False
    assert data["certificate_no"] == "CERT-1"
    assert data["venue_id"] == "hall-a"
    assert data["local_date"] == "2026-10-06"
    assert data["timezone"] == "UTC"
    assert data["day_start_utc"] == canon("2026-10-06T00:00:00Z")
    assert data["day_end_utc"] == canon("2026-10-07T00:00:00Z")
    assert data["sealed_at"]
    assert data["session_count"] == 2
    assert data["redeemed_count"] == 0
    assert data["unredeemed_count"] == 2
    items = data["items"]
    # 明细按封存时的当前开场时刻升序
    assert [it["idempotency_key"] for it in items] == ["pc-1", "pc-2"]
    first = items[0]
    assert first["verification_id"]
    assert first["start_time"] == canon("2026-10-06T10:00:00Z")
    assert first["license_id"] == lic["id"]
    assert first["pool_id"] is None
    assert first["copy_code"] is None
    assert first["redeemed_at"] is None


def test_seal_day_window_is_half_open(client):
    make_license(client)
    verify(client, "pc-prev", "2026-10-05T23:59:59Z")   # 前一日最后一刻
    verify(client, "pc-first", "2026-10-06T00:00:00Z")  # 当日 00:00（左闭，计入）
    verify(client, "pc-last", "2026-10-06T23:59:59Z")
    verify(client, "pc-next", "2026-10-07T00:00:00Z")   # 次日 00:00（右开，不计入）
    data = seal(client, "CERT-B").json()
    assert [it["idempotency_key"] for it in data["items"]] == [
        "pc-first",
        "pc-last",
    ]


def test_seal_uses_iana_timezone_local_day(client):
    make_license(client)
    verify(client, "pc-sh-before", "2026-10-05T15:59:59Z")  # 上海 10-05 23:59:59
    verify(client, "pc-sh-first", "2026-10-05T16:00:00Z")   # 上海 10-06 00:00:00
    verify(client, "pc-sh-last", "2026-10-06T15:59:59Z")    # 上海 10-06 23:59:59
    verify(client, "pc-sh-after", "2026-10-06T16:00:00Z")   # 上海 10-07 00:00:00
    resp = seal(client, "CERT-SH", date="2026-10-06", tz="Asia/Shanghai")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["day_start_utc"] == canon("2026-10-05T16:00:00Z")
    assert data["day_end_utc"] == canon("2026-10-06T16:00:00Z")
    assert [it["idempotency_key"] for it in data["items"]] == [
        "pc-sh-first",
        "pc-sh-last",
    ]


def test_seal_handles_dst_transition_day(client):
    make_license(client)
    # 2026-11-01 是美东夏令时结束日：当地这一天有 25 个小时
    verify(client, "pc-dst-in", "2026-11-02T04:30:00Z")   # 纽约 11-01 23:30 EST
    verify(client, "pc-dst-out", "2026-11-02T05:00:00Z")  # 纽约 11-02 00:00 EST
    data = seal(client, "CERT-DST", date="2026-11-01",
                tz="America/New_York").json()
    assert data["day_start_utc"] == canon("2026-11-01T04:00:00Z")  # 00:00 EDT
    assert data["day_end_utc"] == canon("2026-11-02T05:00:00Z")    # 次日 00:00 EST
    assert [it["idempotency_key"] for it in data["items"]] == ["pc-dst-in"]


def test_seal_excludes_rejected_and_cancelled(client):
    make_license(client)
    verify(client, "pc-keep", "2030-06-01T10:00:00Z")
    verify(client, "pc-drop", "2030-06-01T11:00:00Z")
    # 无授权的影片：rejected 流水不进入凭证
    resp = verify(client, "pc-rej", "2030-06-01T12:00:00Z", film="film-unknown")
    assert resp.json()["decision"] == "rejected"
    # 已取消的核准场次不进入凭证
    resp = client.post("/verifications/pc-drop/cancel", json={"reason": "设备检修"})
    assert resp.status_code == 200, resp.text
    data = seal(client, "CERT-C", date="2030-06-01").json()
    assert [it["idempotency_key"] for it in data["items"]] == ["pc-keep"]
    assert data["session_count"] == 1
    assert data["unredeemed_count"] == 1


def test_seal_reflects_current_schedule_and_license(client):
    l1 = make_license(client, valid_to="2030-12-31T00:00:00Z")
    l2 = make_license(client, valid_to="2031-06-30T00:00:00Z")
    verify(client, "pc-move-in", "2030-06-03T10:00:00Z")   # 原排片在目标日之外
    verify(client, "pc-move-out", "2030-06-01T11:00:00Z")  # 原排片在目标日之内
    # L1 在 2030-06-01 全天暂停：改签到该日的场次会改命中 L2
    resp = client.post(
        f"/licenses/{l1['id']}/suspensions",
        json={
            "suspend_from": "2030-06-01T00:00:00Z",
            "suspend_to": "2030-06-02T00:00:00Z",
            "reason": "暂停一天",
        },
    )
    assert resp.status_code == 201, resp.text
    resp = client.post(
        "/verifications/pc-move-in/reschedule",
        json={"new_start_time": "2030-06-01T09:00:00Z",
              "operation_key": "op-move-in"},
    )
    assert resp.json()["decision"] == "approved"
    assert resp.json()["to_license_id"] == l2["id"]
    resp = client.post(
        "/verifications/pc-move-out/reschedule",
        json={"new_start_time": "2030-06-05T10:00:00Z",
              "operation_key": "op-move-out"},
    )
    assert resp.json()["decision"] == "approved"
    data = seal(client, "CERT-R", date="2030-06-01").json()
    # 只按当前排片判定：改签进来的在场（当前时刻/当前授权），改签出去的缺席
    assert [it["idempotency_key"] for it in data["items"]] == ["pc-move-in"]
    item = data["items"][0]
    assert item["start_time"] == canon("2030-06-01T09:00:00Z")
    assert item["license_id"] == l2["id"]


def test_seal_includes_current_pool_and_copy_code(client):
    pool = client.post(
        "/pools", json={"film_id": "film-1", "max_sessions": 10}
    ).json()
    make_license(client, pool_id=pool["id"])
    verify(client, "pc-pool", "2026-10-06T10:00:00Z")
    # 另一部影片配置了实体拷贝：一场已分配、一场未分配
    resp = client.post(
        "/films/film-2/copies",
        json={"duration_minutes": 120, "transfer_minutes": 30,
              "copy_code": "COPY-1"},
    )
    assert resp.status_code == 201, resp.text
    make_license(client, film_id="film-2")
    verify(client, "pc-copy", "2026-10-06T12:00:00Z", film="film-2")
    verify(client, "pc-nocopy", "2026-10-06T14:00:00Z", film="film-2")
    resp = client.post("/verifications/pc-copy/copy-assignment",
                       json={"copy_code": "COPY-1"})
    assert resp.status_code == 200, resp.text
    data = seal(client, "CERT-P").json()
    by_key = {it["idempotency_key"]: it for it in data["items"]}
    assert by_key["pc-pool"]["pool_id"] == pool["id"]
    assert by_key["pc-pool"]["copy_code"] is None
    assert by_key["pc-copy"]["copy_code"] == "COPY-1"
    assert by_key["pc-copy"]["pool_id"] is None
    assert by_key["pc-nocopy"]["copy_code"] is None


def test_seal_counts_redeemed_and_unredeemed(client, monkeypatch):
    make_license(client)
    verify(client, "pc-r1", "2026-06-01T19:30:00Z")
    verify(client, "pc-r2", "2026-06-01T20:00:00Z")
    verify(client, "pc-r3", "2026-06-01T21:00:00Z")
    code1 = client.post("/verifications/pc-r1/opening-code").json()["code"]
    resp = client.post("/verifications/pc-r2/opening-code")  # 已领未核销
    assert resp.status_code == 200
    jump(monkeypatch, "2026-06-01T19:35:00Z")  # pc-r1 核销窗口内；pc-r3 未开场
    resp = client.post("/redemptions", json={"code": code1, "venue_id": "hall-a"})
    assert resp.status_code == 200, resp.text
    redeemed_at = resp.json()["redeemed_at"]
    data = seal(client, "CERT-X", date="2026-06-01").json()
    assert data["session_count"] == 3
    assert data["redeemed_count"] == 1
    # 已领未核销与未开场场次都计入未核销
    assert data["unredeemed_count"] == 2
    by_key = {it["idempotency_key"]: it for it in data["items"]}
    assert by_key["pc-r1"]["redeemed_at"] == redeemed_at
    assert by_key["pc-r2"]["redeemed_at"] is None
    assert by_key["pc-r3"]["redeemed_at"] is None


def test_seal_empty_day_returns_empty_items_and_zero_counts(client):
    make_license(client)
    verify(client, "pc-other-day", "2026-10-07T10:00:00Z")
    resp = seal(client, "CERT-EMPTY", date="2026-10-06")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["items"] == []
    assert data["session_count"] == 0
    assert data["redeemed_count"] == 0
    assert data["unredeemed_count"] == 0
    # 空日凭证同样封存、可查询
    got = client.get("/progress-certificates/CERT-EMPTY")
    assert got.status_code == 200
    assert got.json()["items"] == []
    assert got.json()["session_count"] == 0


def test_seal_venue_without_any_sessions(client):
    resp = seal(client, "CERT-NOVENUE", venue="hall-nowhere")
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []
    assert resp.json()["session_count"] == 0


# ----------------------------------------------------------------------
# validation
# ----------------------------------------------------------------------
def test_invalid_local_date_rejected(client):
    for bad in ["2026-02-30", "2026-13-01", "2026-10-32", "not-a-date",
                "06/10/2026"]:
        resp = seal(client, "CERT-BAD-DATE", date=bad)
        assert resp.status_code == 422, (bad, resp.text)
    # 非法日期不留任何记录
    assert client.get("/progress-certificates/CERT-BAD-DATE").status_code == 404


def test_unknown_timezone_rejected(client):
    for bad in ["Mars/Olympus", "Asia/Shangha", "UTC+8", "../etc/passwd", ""]:
        resp = seal(client, "CERT-BAD-TZ", tz=bad)
        assert resp.status_code == 422, (bad, resp.text)
    resp = seal(client, "CERT-BAD-TZ", tz="Mars/Olympus")
    assert resp.json()["code"] == "UNKNOWN_TIMEZONE"
    # 未知时区不留任何记录，同一凭证号之后仍可用合法参数封存
    assert client.get("/progress-certificates/CERT-BAD-TZ").status_code == 404
    resp = seal(client, "CERT-BAD-TZ")
    assert resp.status_code == 200, resp.text


# ----------------------------------------------------------------------
# idempotency / immutability
# ----------------------------------------------------------------------
def test_same_number_same_params_replays_first_snapshot(client, monkeypatch):
    make_license(client)
    verify(client, "pc-rp", "2026-06-01T19:30:00Z")
    code = client.post("/verifications/pc-rp/opening-code").json()["code"]
    first = seal(client, "CERT-RP", date="2026-06-01").json()
    assert first["replay"] is False
    assert first["unredeemed_count"] == 1
    # 封存后状态变化：该场次被核销
    jump(monkeypatch, "2026-06-01T19:35:00Z")
    client.post("/redemptions", json={"code": code, "venue_id": "hall-a"})
    # 同号同参数重试：返回首次快照，不反映此后的核销
    again = seal(client, "CERT-RP", date="2026-06-01").json()
    assert again["replay"] is True
    assert again == {**first, "replay": True}
    assert again["items"][0]["redeemed_at"] is None
    assert again["redeemed_count"] == 0
    # 查询同样保持首次内容
    got = client.get("/progress-certificates/CERT-RP").json()
    assert got == first


def test_same_number_different_params_conflicts(client):
    make_license(client)
    verify(client, "pc-cf", "2026-10-06T10:00:00Z")
    first = seal(client, "CERT-CF").json()
    base = {"certificate_no": "CERT-CF", "venue_id": "hall-a",
            "local_date": "2026-10-06", "timezone": "UTC"}
    for override in [
        {"venue_id": "hall-b"},        # 异场馆
        {"local_date": "2026-10-07"},  # 异日期
        {"timezone": "Asia/Shanghai"},  # 异时区
    ]:
        resp = client.post("/progress-certificates",
                           json={**base, **override})
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["code"] == "CONFLICT"
        assert body["existing"]["certificate_no"] == "CERT-CF"
        assert body["existing"]["local_date"] == "2026-10-06"
    # 冲突不覆盖首次快照
    got = client.get("/progress-certificates/CERT-CF").json()
    assert got["sealed_at"] == first["sealed_at"]
    assert got["session_count"] == first["session_count"]


def test_sealed_certificate_survives_later_changes(client, monkeypatch):
    make_license(client)
    verify(client, "pc-im1", "2026-06-01T19:30:00Z")
    verify(client, "pc-im2", "2026-06-01T22:00:00Z")
    verify(client, "pc-im3", "2026-06-01T23:00:00Z")
    code = client.post("/verifications/pc-im1/opening-code").json()["code"]
    first = seal(client, "CERT-IM", date="2026-06-01").json()
    assert first["session_count"] == 3
    # 封存之后分别核销、取消、改签——原行为保持可用
    jump(monkeypatch, "2026-06-01T19:35:00Z")
    resp = client.post("/redemptions", json={"code": code, "venue_id": "hall-a"})
    assert resp.status_code == 200
    resp = client.post("/verifications/pc-im2/cancel", json={"reason": "取消"})
    assert resp.status_code == 200
    resp = client.post(
        "/verifications/pc-im3/reschedule",
        json={"new_start_time": "2026-06-02T10:00:00Z",
              "operation_key": "op-im3"},
    )
    assert resp.json()["decision"] == "approved"
    # 实时视图确实已变化
    assert client.get("/verifications/pc-im2").json()["cancelled"] is True
    assert client.get("/verifications/pc-im3").json()["start_time"] == canon(
        "2026-06-02T10:00:00Z"
    )
    # 已封存凭证保持首次内容
    got = client.get("/progress-certificates/CERT-IM").json()
    assert got == first
    by_key = {it["idempotency_key"]: it for it in got["items"]}
    assert by_key["pc-im1"]["redeemed_at"] is None
    assert by_key["pc-im3"]["start_time"] == canon("2026-06-01T23:00:00Z")
    assert got["session_count"] == 3


def test_new_certificate_number_seals_current_state(client, monkeypatch):
    make_license(client)
    verify(client, "pc-m1", "2026-06-01T19:30:00Z")
    code = client.post("/verifications/pc-m1/opening-code").json()["code"]
    first = seal(client, "CERT-M1", date="2026-06-01").json()
    jump(monkeypatch, "2026-06-01T19:35:00Z")
    client.post("/redemptions", json={"code": code, "venue_id": "hall-a"})
    # 换一个凭证号重新封存：反映受理时刻的最新状态
    second = seal(client, "CERT-M2", date="2026-06-01").json()
    assert first["redeemed_count"] == 0
    assert second["redeemed_count"] == 1
    assert second["items"][0]["redeemed_at"] is not None
    # 旧凭证不受影响
    assert client.get("/progress-certificates/CERT-M1").json() == first


# ----------------------------------------------------------------------
# queries
# ----------------------------------------------------------------------
def test_get_unknown_certificate_404(client):
    resp = client.get("/progress-certificates/NO-SUCH-CERT")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


def test_list_certificates_filter_by_venue(client):
    make_license(client)
    make_license(client, venue_id="hall-b")
    verify(client, "pc-la", "2026-10-06T10:00:00Z")
    verify(client, "pc-lb", "2026-10-06T10:00:00Z", venue="hall-b")
    seal(client, "CERT-LA", venue="hall-a")
    seal(client, "CERT-LB", venue="hall-b")
    all_certs = client.get("/progress-certificates").json()
    assert {c["certificate_no"] for c in all_certs} == {"CERT-LA", "CERT-LB"}
    only_a = client.get("/progress-certificates",
                        params={"venue_id": "hall-a"}).json()
    assert [c["certificate_no"] for c in only_a] == ["CERT-LA"]
    assert only_a[0]["items"][0]["idempotency_key"] == "pc-la"


# ----------------------------------------------------------------------
# concurrency
# ----------------------------------------------------------------------
def test_concurrent_identical_seals_persist_one_certificate(client):
    make_license(client)
    verify(client, "pc-cc1", "2026-06-01T19:30:00Z")
    verify(client, "pc-cc2", "2026-06-01T20:00:00Z")
    results, errors = [], []
    barrier = threading.Barrier(10)

    def worker() -> None:
        barrier.wait()
        try:
            resp = seal(client, "CERT-CC", date="2026-06-01")
            assert resp.status_code == 200, resp.text
            results.append(resp.json())
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    firsts = [r for r in results if r["replay"] is False]
    replays = [r for r in results if r["replay"] is True]
    assert len(firsts) == 1
    assert len(replays) == 9
    # 全部返回同一份快照
    assert len({r["id"] for r in results}) == 1
    assert len({r["sealed_at"] for r in results}) == 1
    for r in results:
        assert r["session_count"] == 2
        assert [it["idempotency_key"] for it in r["items"]] == [
            "pc-cc1",
            "pc-cc2",
        ]
    # 全库只有这一份凭证
    certs = client.get("/progress-certificates").json()
    assert len(certs) == 1


def test_seal_concurrent_with_redeem_cancel_reschedule_reflects_one_state(
    client, monkeypatch
):
    make_license(client)
    # 落在 2026-06-01（UTC）当天的场次：一场待核销、一场待取消、一场稳定；
    # 另有一场原排片在当日之外，将被并发改签进来
    verify(client, "pc-k-redeem", "2026-06-01T19:30:00Z")
    verify(client, "pc-k-cancel", "2026-06-01T22:00:00Z")
    verify(client, "pc-k-move", "2026-06-03T19:30:00Z")
    verify(client, "pc-k-stable", "2026-06-01T20:00:00Z")
    code = client.post("/verifications/pc-k-redeem/opening-code").json()["code"]
    # 核销窗口内；取消/改签门槛（未开场、新时间在未来）均满足
    jump(monkeypatch, "2026-06-01T19:35:00Z")

    outcomes, errors = {}, []
    barrier = threading.Barrier(4)

    def seal_worker() -> None:
        barrier.wait()
        outcomes["seal"] = seal(client, "CERT-RACE", date="2026-06-01").json()

    def redeem_worker() -> None:
        barrier.wait()
        outcomes["redeem"] = client.post(
            "/redemptions", json={"code": code, "venue_id": "hall-a"}
        )

    def cancel_worker() -> None:
        barrier.wait()
        outcomes["cancel"] = client.post(
            "/verifications/pc-k-cancel/cancel", json={"reason": "竞态取消"}
        )

    def reschedule_worker() -> None:
        barrier.wait()
        outcomes["reschedule"] = client.post(
            "/verifications/pc-k-move/reschedule",
            json={"new_start_time": "2026-06-01T23:00:00Z",
                  "operation_key": "op-race-move"},
        )

    workers = (seal_worker, redeem_worker, cancel_worker, reschedule_worker)
    threads = [threading.Thread(target=f) for f in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    # 各并发操作自身都成功（时钟冻结，门槛均满足）
    assert outcomes["redeem"].status_code == 200, outcomes["redeem"].text
    assert outcomes["cancel"].status_code == 200, outcomes["cancel"].text
    assert outcomes["reschedule"].status_code == 200, outcomes["reschedule"].text
    assert outcomes["reschedule"].json()["decision"] == "approved"

    cert = outcomes["seal"]
    items = {it["idempotency_key"]: it for it in cert["items"]}
    # 内部一致性：计数与明细自洽，不混合前后状态
    assert cert["session_count"] == len(cert["items"])
    assert (
        cert["redeemed_count"] + cert["unredeemed_count"]
        == cert["session_count"]
    )
    assert cert["redeemed_count"] == sum(
        1 for it in cert["items"] if it["redeemed_at"] is not None
    )
    # 未受并发影响的场次始终在场
    assert items["pc-k-stable"]["start_time"] == canon("2026-06-01T20:00:00Z")
    # 核销：先于封存则明细带核销时间且与回执一致，否则为空——二者必居其一
    receipt = outcomes["redeem"].json()
    if items["pc-k-redeem"]["redeemed_at"] is not None:
        assert items["pc-k-redeem"]["redeemed_at"] == receipt["redeemed_at"]
        assert cert["redeemed_count"] >= 1
    else:
        assert cert["redeemed_count"] == 0
    # 取消：先于封存则缺席，否则在场（凭证保持封存时刻状态）
    # 改签：先于封存则按新时刻 23:00 落在当日，否则缺席（旧时刻在当日之外），
    # 绝不出现"旧时刻计入当日"的混合态
    if "pc-k-move" in items:
        assert items["pc-k-move"]["start_time"] == canon("2026-06-01T23:00:00Z")
    # 封存后查询与封存响应一致
    got = client.get("/progress-certificates/CERT-RACE").json()
    assert got["items"] == cert["items"]
    assert got["session_count"] == cert["session_count"]

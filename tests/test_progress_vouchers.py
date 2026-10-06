"""End-to-end and concurrency tests for daily screening progress vouchers."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import service
from app.main import create_app
from app.timeutil import to_iso

# 冻结时钟：2026-06-01T00:00:00Z。Asia/Shanghai 的 2026-06-02 当地日
# 对应 UTC 左闭右开区间 [2026-06-01T16:00:00Z, 2026-06-02T16:00:00Z)。
NOW = "2026-06-01T00:00:00.000000Z"
DAY = "2026-06-02"
TZ = "Asia/Shanghai"
DAY_START = "2026-06-01T16:00:00.000000Z"
DAY_END = "2026-06-02T16:00:00.000000Z"

WINDOW = {
    "valid_from": "2026-01-01T00:00:00Z",
    "valid_to": "2027-01-01T00:00:00Z",
}


def canon(value: str) -> str:
    """Turn an ISO-8601 string into the canonical fixed-width UTC form."""
    return to_iso(datetime.fromisoformat(value.replace("Z", "+00:00")))


def iso_at(**delta) -> str:
    base = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    return to_iso(base + timedelta(**delta))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "now_iso", lambda: NOW)
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def jump(monkeypatch, value: str) -> None:
    """Freeze the service clock at ``value`` (ISO-8601)."""
    monkeypatch.setattr(service, "now_iso", lambda: canon(value))


def make_license(client, film="film-1", venue="hall-a", **overrides):
    body = {
        "film_id": film,
        "venue_id": venue,
        "valid_from": WINDOW["valid_from"],
        "valid_to": WINDOW["valid_to"],
        "max_sessions": 20,
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


def seal(client, voucher_no, venue="hall-a", local_date=DAY, timezone=TZ):
    return client.post(
        "/progress-vouchers",
        json={
            "voucher_no": voucher_no,
            "venue_id": venue,
            "local_date": local_date,
            "timezone": timezone,
        },
    )


def item_keys(voucher):
    return [item["idempotency_key"] for item in voucher["items"]]


# ----------------------------------------------------------------------
# sealing: day window, eligibility, item fields
# ----------------------------------------------------------------------
def test_seal_empty_day_returns_empty_items_and_zero_counts(client):
    make_license(client)
    resp = seal(client, "PV-empty")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["voucher_no"] == "PV-empty"
    assert body["venue_id"] == "hall-a"
    assert body["local_date"] == DAY
    assert body["timezone"] == TZ
    assert body["day_start"] == DAY_START
    assert body["day_end"] == DAY_END
    assert body["items"] == []
    assert body["session_count"] == 0
    assert body["redeemed_count"] == 0
    assert body["unredeemed_count"] == 0
    assert body["replay"] is False
    assert body["sealed_at"] == NOW
    # 已封存，可查询
    got = client.get("/progress-vouchers/PV-empty")
    assert got.status_code == 200
    assert got.json()["items"] == []


def test_seal_captures_only_in_day_approved_live_sessions(client):
    make_license(client)
    make_license(client, venue="hall-b")
    # 日内：左边界（含）与右边界前一刻
    verify(client, "in-first", "2026-06-01T16:00:00Z")
    verify(client, "in-last", "2026-06-02T15:59:59Z")
    # 日外：前一刻与右边界（右开，归次日）
    verify(client, "out-before", "2026-06-01T15:59:59Z")
    verify(client, "out-at-end", "2026-06-02T16:00:00Z")
    # 日内但已取消
    verify(client, "in-cancelled", "2026-06-01T18:00:00Z")
    resp = client.post(
        "/verifications/in-cancelled/cancel", json={"reason": "设备检修"}
    )
    assert resp.status_code == 200, resp.text
    # 日内但被拒绝（无授权的影片）
    rejected = verify(client, "in-rejected", "2026-06-01T19:00:00Z", film="no-film")
    assert rejected.json()["decision"] == "rejected"
    # 日内但其他场馆
    verify(client, "in-other-venue", "2026-06-01T20:00:00Z", venue="hall-b")

    body = seal(client, "PV-day").json()
    assert item_keys(body) == ["in-first", "in-last"]
    first, last = body["items"]
    assert first["start_time"] == "2026-06-01T16:00:00.000000Z"
    assert first["license_id"] is not None
    assert first["pool_id"] is None
    assert first["copy_code"] is None
    assert first["redeemed_at"] is None
    assert last["start_time"] == "2026-06-02T15:59:59.000000Z"
    assert body["session_count"] == 2
    assert body["redeemed_count"] == 0
    assert body["unredeemed_count"] == 2


def test_seal_uses_current_schedule_and_license_after_reschedule(client):
    # 早授权只覆盖到 2026-06-01T17:00:00Z；改签到 18:00 后只能命中晚授权
    early = make_license(client, valid_to="2026-06-01T17:00:00Z", max_sessions=5)
    late = make_license(client, valid_to="2026-12-01T00:00:00Z", max_sessions=5)
    # 原排片在日外（命中早授权），改签入日内（重新判定命中晚授权）
    verify(client, "mv-in", "2026-06-01T12:00:00Z")
    resp = client.post(
        "/verifications/mv-in/reschedule",
        json={"new_start_time": "2026-06-01T18:00:00Z",
              "operation_key": "mv-in-op"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["decision"] == "approved"
    assert resp.json()["from_license_id"] == early["id"]
    assert resp.json()["to_license_id"] == late["id"]
    # 原排片在日内，改签到日外
    verify(client, "mv-out", "2026-06-01T20:00:00Z")
    resp = client.post(
        "/verifications/mv-out/reschedule",
        json={"new_start_time": "2026-06-05T12:00:00Z",
              "operation_key": "mv-out-op"},
    )
    assert resp.json()["decision"] == "approved"

    body = seal(client, "PV-rescheduled").json()
    assert item_keys(body) == ["mv-in"]
    item = body["items"][0]
    # 当前开场时间与当前授权（改签后命中 valid_to 更晚的授权）
    assert item["start_time"] == "2026-06-01T18:00:00.000000Z"
    assert item["license_id"] == late["id"]
    assert item["license_id"] != early["id"]


def test_seal_records_pool_and_copy_of_current_assignment(client):
    pool = client.post(
        "/pools", json={"film_id": "film-1", "max_sessions": 10}
    ).json()
    make_license(client, pool_id=pool["id"])
    verify(client, "with-pool", "2026-06-01T18:00:00Z")

    # 另一部影片配置实体拷贝（须在该影片任何核准流水之前登记）
    client.post(
        "/films/film-c/copies",
        json={"duration_minutes": 90, "transfer_minutes": 30,
              "copy_code": "COPY-C-1"},
    )
    make_license(client, film="film-c")
    verify(client, "with-copy", "2026-06-01T20:00:00Z", film="film-c")
    resp = client.post(
        "/verifications/with-copy/copy-assignment",
        json={"copy_code": "COPY-C-1"},
    )
    assert resp.status_code == 200, resp.text

    body = seal(client, "PV-fields").json()
    assert item_keys(body) == ["with-pool", "with-copy"]
    pooled, copied = body["items"]
    assert pooled["pool_id"] == pool["id"]
    assert pooled["copy_code"] is None
    assert copied["pool_id"] is None
    assert copied["copy_code"] == "COPY-C-1"


def test_seal_records_redeemed_at_and_counts(client, monkeypatch):
    make_license(client)
    verify(client, "redeemed-1", "2026-06-01T18:00:00Z")
    verify(client, "not-redeemed", "2026-06-01T20:00:00Z")
    # 未开场（未来场次）同样计入未核销
    verify(client, "not-started", "2026-06-02T10:00:00Z")

    code = client.post("/verifications/redeemed-1/opening-code").json()["code"]
    assert code
    jump(monkeypatch, "2026-06-01T18:05:00Z")
    receipt = client.post(
        "/redemptions", json={"code": code, "venue_id": "hall-a"}
    ).json()
    assert receipt["redeemed_at"]

    body = seal(client, "PV-redeem").json()
    assert item_keys(body) == ["redeemed-1", "not-redeemed", "not-started"]
    first, second, third = body["items"]
    assert first["redeemed_at"] == receipt["redeemed_at"]
    assert second["redeemed_at"] is None
    assert third["redeemed_at"] is None
    assert body["session_count"] == 3
    assert body["redeemed_count"] == 1
    assert body["unredeemed_count"] == 2


def test_seal_utc_timezone_day(client):
    make_license(client)
    verify(client, "utc-in", "2026-06-02T00:00:00Z")
    verify(client, "utc-out", "2026-06-02T00:00:00Z".replace("00:00:00", "23:59:59"))
    verify(client, "utc-next", "2026-06-03T00:00:00Z")
    body = seal(client, "PV-utc", local_date=DAY, timezone="UTC").json()
    assert body["day_start"] == "2026-06-02T00:00:00.000000Z"
    assert body["day_end"] == "2026-06-03T00:00:00.000000Z"
    assert item_keys(body) == ["utc-in", "utc-out"]


def test_seal_handles_dst_transition_day(client):
    """America/New_York 2026-03-08 夏令时切换日只有 23 小时。"""
    make_license(client)
    # 当地日 [2026-03-08T05:00:00Z, 2026-03-09T04:00:00Z)
    verify(client, "dst-in", "2026-03-08T05:00:00Z")
    verify(client, "dst-last", "2026-03-09T03:59:59Z")
    verify(client, "dst-out", "2026-03-09T04:00:00Z")
    body = seal(
        client, "PV-dst", local_date="2026-03-08", timezone="America/New_York"
    ).json()
    assert body["day_start"] == "2026-03-08T05:00:00.000000Z"
    assert body["day_end"] == "2026-03-09T04:00:00.000000Z"
    assert item_keys(body) == ["dst-in", "dst-last"]


# ----------------------------------------------------------------------
# validation: invalid dates and unknown timezones are rejected (422)
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad_date",
    ["2026-02-30", "2026-13-01", "2026-00-10", "not-a-date", "2026-6-2", "20260602"],
)
def test_seal_rejects_invalid_local_date(client, bad_date):
    resp = seal(client, "PV-bad-date", local_date=bad_date)
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "INVALID_LOCAL_DATE"
    # 不落任何记录
    assert client.get("/progress-vouchers/PV-bad-date").status_code == 404


@pytest.mark.parametrize("bad_tz", ["Mars/Olympus", "UTC+8", "GMT+08:00", "asia/shanghai "])
def test_seal_rejects_unknown_timezone(client, bad_tz):
    resp = seal(client, "PV-bad-tz", timezone=bad_tz)
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "UNKNOWN_TIMEZONE"
    assert client.get("/progress-vouchers/PV-bad-tz").status_code == 404


# ----------------------------------------------------------------------
# idempotency: same voucher number
# ----------------------------------------------------------------------
def test_same_voucher_no_same_params_replays_first_snapshot(client, monkeypatch):
    make_license(client)
    verify(client, "rep-1", "2026-06-01T18:00:00Z")
    first = seal(client, "PV-replay").json()
    assert first["replay"] is False

    # 首次封存后发生改签/取消/核销，重试仍返回首次快照
    client.post(
        "/verifications/rep-1/reschedule",
        json={"new_start_time": "2026-06-01T21:00:00Z",
              "operation_key": "rep-1-op"},
    )
    second = seal(client, "PV-replay").json()
    assert second["replay"] is True
    assert second == {**first, "replay": True}
    assert second["items"][0]["start_time"] == first["items"][0]["start_time"]

    # 只落一条凭证、一组明细
    listing = client.get("/progress-vouchers").json()
    assert len(listing) == 1


@pytest.mark.parametrize(
    "field,value",
    [("venue", "hall-b"), ("local_date", "2026-06-03"), ("timezone", "UTC")],
)
def test_same_voucher_no_different_params_conflicts(client, field, value):
    make_license(client)
    first = seal(client, "PV-conflict").json()
    kwargs = {"venue": "hall-a", "local_date": DAY, "timezone": TZ}
    kwargs[field] = value
    resp = seal(client, "PV-conflict", **kwargs)
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["code"] == "CONFLICT"
    assert body["existing"]["voucher_no"] == "PV-conflict"
    assert body["existing"]["local_date"] == first["local_date"]


# ----------------------------------------------------------------------
# sealed content is immutable for later queries
# ----------------------------------------------------------------------
def test_get_returns_first_snapshot_after_later_changes(client, monkeypatch):
    make_license(client)
    verify(client, "stay", "2026-06-01T18:00:00Z")
    verify(client, "move-out", "2026-06-01T19:00:00Z")
    verify(client, "to-cancel", "2026-06-01T20:00:00Z")
    verify(client, "to-redeem", "2026-06-01T21:00:00Z")
    code = client.post("/verifications/to-redeem/opening-code").json()["code"]

    first = seal(client, "PV-immutable").json()
    assert first["session_count"] == 4
    assert first["redeemed_count"] == 0

    # 封存后：改签约出当日、取消一场、核销一场、另核准一场新场次
    client.post(
        "/verifications/move-out/reschedule",
        json={"new_start_time": "2026-06-05T12:00:00Z",
              "operation_key": "move-out-op"},
    )
    client.post("/verifications/to-cancel/cancel", json={"reason": "取消"})
    jump(monkeypatch, "2026-06-01T21:05:00Z")
    client.post("/redemptions", json={"code": code, "venue_id": "hall-a"})
    verify(client, "late-add", "2026-06-01T22:00:00Z")

    got = client.get("/progress-vouchers/PV-immutable")
    assert got.status_code == 200
    assert got.json() == {**first, "replay": False}
    # 同号重试也仍是首次内容
    again = seal(client, "PV-immutable").json()
    assert again["replay"] is True
    assert again["items"] == first["items"]


def test_get_unknown_voucher_returns_404(client):
    resp = client.get("/progress-vouchers/no-such-voucher")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


def test_list_vouchers_with_venue_filter(client):
    make_license(client)
    make_license(client, venue="hall-b")
    verify(client, "a-1", "2026-06-01T18:00:00Z")
    verify(client, "b-1", "2026-06-01T18:00:00Z", venue="hall-b")
    seal(client, "PV-list-a", venue="hall-a")
    seal(client, "PV-list-b", venue="hall-b")

    all_rows = client.get("/progress-vouchers").json()
    assert [r["voucher_no"] for r in all_rows] == ["PV-list-b", "PV-list-a"]
    filtered = client.get("/progress-vouchers", params={"venue_id": "hall-a"}).json()
    assert len(filtered) == 1
    assert filtered[0]["voucher_no"] == "PV-list-a"
    assert filtered[0]["session_count"] == 1
    assert "items" not in filtered[0]


# ----------------------------------------------------------------------
# sealing does not disturb the existing flows
# ----------------------------------------------------------------------
def test_existing_flows_keep_working_after_seal(client):
    lic = make_license(client, max_sessions=2)
    verify(client, "keep-1", "2026-06-01T18:00:00Z")
    seal(client, "PV-side-effect")

    # 原核验幂等重放、取消释放额度、再次核验均不受影响
    replay = verify(client, "keep-1", "2026-06-01T18:00:00Z").json()
    assert replay["replay"] is True
    assert replay["decision"] == "approved"
    cancel = client.post(
        "/verifications/keep-1/cancel", json={"reason": "计划调整"}
    )
    assert cancel.status_code == 200
    lic_after = client.get(f"/licenses/{lic['id']}").json()
    assert lic_after["used_sessions"] == 0
    again = verify(client, "keep-2", "2026-06-01T19:00:00Z").json()
    assert again["decision"] == "approved"


# ----------------------------------------------------------------------
# concurrency
# ----------------------------------------------------------------------
def test_concurrent_seals_with_same_voucher_no_seal_once(client):
    make_license(client)
    verify(client, "conc-1", "2026-06-01T18:00:00Z")

    results, errors = [], []

    def worker(_):
        try:
            results.append(seal(client, "PV-race").json())
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 10
    firsts = [r for r in results if r["replay"] is False]
    replays = [r for r in results if r["replay"] is True]
    assert len(firsts) == 1
    assert len(replays) == 9
    for r in results:
        assert r["id"] == firsts[0]["id"]
        assert r["sealed_at"] == firsts[0]["sealed_at"]
        assert r["items"] == firsts[0]["items"]
    assert len(client.get("/progress-vouchers").json()) == 1


def test_seal_concurrent_with_redemption_reflects_one_complete_state(
    client, monkeypatch
):
    """核销与封存并发：凭证要么完整看到核销前、要么完整看到核销后。"""
    make_license(client)
    verify(client, "race-redeem", "2026-06-01T18:00:00Z")
    code = client.post("/verifications/race-redeem/opening-code").json()["code"]
    jump(monkeypatch, "2026-06-01T18:05:00Z")

    barrier = threading.Barrier(2)
    outcomes, errors = {}, []

    def do_redeem():
        try:
            barrier.wait()
            outcomes["redeem"] = client.post(
                "/redemptions", json={"code": code, "venue_id": "hall-a"}
            ).json()
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def do_seal():
        try:
            barrier.wait()
            outcomes["seal"] = seal(client, "PV-race-redeem").json()
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    t1, t2 = threading.Thread(target=do_redeem), threading.Thread(target=do_seal)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors
    receipt = outcomes["redeem"]
    assert receipt["redeemed_at"]
    voucher = outcomes["seal"]
    assert voucher["session_count"] == 1
    item = voucher["items"][0]
    if item["redeemed_at"] is None:
        # 封存先于核销提交：完整看到核销前状态
        assert voucher["redeemed_count"] == 0
        assert voucher["unredeemed_count"] == 1
    else:
        # 核销先于封存提交：完整看到核销后状态（回执时间与核销回执一致）
        assert item["redeemed_at"] == receipt["redeemed_at"]
        assert voucher["redeemed_count"] == 1
        assert voucher["unredeemed_count"] == 0


def test_seal_concurrent_with_cancellation_reflects_one_complete_state(client):
    """取消与封存并发：凭证要么含该场（取消在后）、要么不含（取消在前）。"""
    make_license(client)
    verify(client, "race-cancel", "2026-06-01T18:00:00Z")
    verify(client, "race-stay", "2026-06-01T19:00:00Z")

    barrier = threading.Barrier(2)
    outcomes, errors = {}, []

    def do_cancel():
        try:
            barrier.wait()
            outcomes["cancel"] = client.post(
                "/verifications/race-cancel/cancel", json={"reason": "取消"}
            ).json()
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def do_seal():
        try:
            barrier.wait()
            outcomes["seal"] = seal(client, "PV-race-cancel").json()
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    t1, t2 = threading.Thread(target=do_cancel), threading.Thread(target=do_seal)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors
    voucher = outcomes["seal"]
    keys = item_keys(voucher)
    assert "race-stay" in keys
    if "race-cancel" in keys:
        # 封存先于取消：两场都在，字段完整
        assert voucher["session_count"] == 2
        item = voucher["items"][keys.index("race-cancel")]
        assert item["license_id"] is not None
    else:
        # 取消先于封存：只剩未取消的一场
        assert voucher["session_count"] == 1
    assert voucher["session_count"] == len(voucher["items"])
    assert (
        voucher["redeemed_count"] + voucher["unredeemed_count"]
        == voucher["session_count"]
    )

#!/usr/bin/env bash
# 场馆日放映进度凭证调用示例。启动服务后执行：bash examples/demo_progress_certificates.sh
#
# 本脚本实时运行：以"当前时刻 + 少量秒数"构造一场可核销的场次，完整演示
# 封存（含已核销/未核销明细与计数）→ 查询 → 同号同参重放 → 异参 409 →
# 空日零计数 → 非法日期/未知时区拒绝 → 封存后内容不可变（新凭证号才反映新状态）。
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pretty() { if command -v jq >/dev/null 2>&1; then jq .; else cat; fi; }

FILM="film-cert-demo"
VENUE="hall-1"
TZ_NAME="Asia/Shanghai"
RUN="cert-demo-$(date +%s)"   # 本次运行的唯一后缀，重复执行不冲突

# 相对当前时刻的 UTC ISO-8601 时间（秒），如 ts 30 -> 30 秒后
ts() {
  python3 -c "
from datetime import datetime, timedelta, timezone
print((datetime.now(timezone.utc) + timedelta(seconds=$1)).strftime('%Y-%m-%dT%H:%M:%SZ'))
"
}

# 指定 IANA 时区下的当地日期（YYYY-MM-DD）
LOCAL_DATE=$(python3 -c "
from datetime import datetime
from zoneinfo import ZoneInfo
print(datetime.now(ZoneInfo('$TZ_NAME')).strftime('%Y-%m-%d'))
")

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pretty

echo
echo "== 1. 登记共享池并录入绑池授权（影片 $FILM / $VENUE，窗口覆盖当下） =="
POOL_ID=$(curl -s -X POST "$BASE/pools" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"max_sessions\": 10, \"note\": \"凭证演示池\"
}" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
FROM=$(ts -60); TO=$(ts 172800)
curl -s -X POST "$BASE/licenses" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"$VENUE\",
  \"valid_from\": \"$FROM\", \"valid_to\": \"$TO\",
  \"max_sessions\": 10, \"pool_id\": $POOL_ID, \"note\": \"凭证演示授权\"
}" | pretty

echo
echo "== 2. 核准三个场次：A（数秒后开场，将核销）、B（1 小时后，未开场）、C（25 小时后，落在次日） =="
START_A=$(ts 5)
START_B=$(ts 3600)
START_C=$(ts 90000)
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"$VENUE\",
  \"start_time\": \"$START_A\", \"idempotency_key\": \"$RUN-A\"
}" | pretty
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"$VENUE\",
  \"start_time\": \"$START_B\", \"idempotency_key\": \"$RUN-B\"
}" >/dev/null
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"$VENUE\",
  \"start_time\": \"$START_C\", \"idempotency_key\": \"$RUN-C\"
}" >/dev/null
echo "    A=$START_A（将核销）  B=$START_B（未开场）  C=$START_C（次日，不计入今日凭证）"

echo
echo "== 3. 领取 A 的开场码，等待开场后核销（约 6 秒） =="
CODE_A=$(curl -s -X POST "$BASE/verifications/$RUN-A/opening-code" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['code'])")
sleep 6
curl -s -X POST "$BASE/redemptions" -H 'Content-Type: application/json' \
  -d "{\"code\": \"$CODE_A\", \"venue_id\": \"$VENUE\"}" | pretty

echo
echo "== 4. 封存凭证：$VENUE 在 $LOCAL_DATE（$TZ_NAME）的放映进度 =="
echo "    明细含原核验键/当前开场时间/当前授权及额度池/拷贝编号/核销时间；"
echo "    A 已核销、B 未开场计入未核销、C 落在次日不进入本凭证"
curl -s -X POST "$BASE/progress-certificates" -H 'Content-Type: application/json' -d "{
  \"certificate_no\": \"$RUN-1\", \"venue_id\": \"$VENUE\",
  \"local_date\": \"$LOCAL_DATE\", \"timezone\": \"$TZ_NAME\"
}" | pretty

echo
echo "== 5. 同凭证号同参数重试：返回首次快照（replay=true），不重新封存 =="
curl -s -X POST "$BASE/progress-certificates" -H 'Content-Type: application/json' -d "{
  \"certificate_no\": \"$RUN-1\", \"venue_id\": \"$VENUE\",
  \"local_date\": \"$LOCAL_DATE\", \"timezone\": \"$TZ_NAME\"
}" | pretty

echo
echo "== 6. 同凭证号异参数（换日期）：409 CONFLICT，existing 附首次快照 =="
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/progress-certificates" \
  -H 'Content-Type: application/json' -d "{
  \"certificate_no\": \"$RUN-1\", \"venue_id\": \"$VENUE\",
  \"local_date\": \"2030-01-01\", \"timezone\": \"$TZ_NAME\"
}"

echo
echo "== 7. 空日封存（2030-01-01）：空清单、零计数，凭证照常生成 =="
curl -s -X POST "$BASE/progress-certificates" -H 'Content-Type: application/json' -d "{
  \"certificate_no\": \"$RUN-empty\", \"venue_id\": \"$VENUE\",
  \"local_date\": \"2030-01-01\", \"timezone\": \"$TZ_NAME\"
}" | pretty

echo
echo "== 8. 非法日期（2026-02-30）：422，不留记录 =="
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/progress-certificates" \
  -H 'Content-Type: application/json' -d "{
  \"certificate_no\": \"$RUN-bad-date\", \"venue_id\": \"$VENUE\",
  \"local_date\": \"2026-02-30\", \"timezone\": \"$TZ_NAME\"
}"

echo
echo "== 9. 未知 IANA 时区（Mars/Olympus）：422 UNKNOWN_TIMEZONE，不留记录 =="
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/progress-certificates" \
  -H 'Content-Type: application/json' -d "{
  \"certificate_no\": \"$RUN-bad-tz\", \"venue_id\": \"$VENUE\",
  \"local_date\": \"$LOCAL_DATE\", \"timezone\": \"Mars/Olympus\"
}"

echo
echo "== 10. 封存后取消场次 B：已封存凭证保持首次内容（仍含 B） =="
curl -s -X POST "$BASE/verifications/$RUN-B/cancel" \
  -H 'Content-Type: application/json' -d '{"reason": "设备检修，场次取消"}' >/dev/null
curl -s "$BASE/progress-certificates/$RUN-1" | pretty

echo
echo "== 11. 换一个新凭证号重新封存：反映受理时刻的最新状态（B 已取消，不再计入） =="
curl -s -X POST "$BASE/progress-certificates" -H 'Content-Type: application/json' -d "{
  \"certificate_no\": \"$RUN-2\", \"venue_id\": \"$VENUE\",
  \"local_date\": \"$LOCAL_DATE\", \"timezone\": \"$TZ_NAME\"
}" | pretty

echo
echo "== 12. 凭证列表（可按场馆过滤）；未知凭证号 404 =="
curl -s "$BASE/progress-certificates?venue_id=$VENUE" | pretty
curl -s -w '\nHTTP %{http_code}\n' "$BASE/progress-certificates/NO-SUCH-CERT"

echo
echo "演示完成。"
echo
echo "提示：凭证按受理时刻在单个事务内封存，与并发改签/取消/核销完全串行，"
echo "      只反映其中一个完整状态；封存后查询恒为首次内容。当地日期按所给"
echo "      IANA 时区的本地午夜到次日午夜（左闭右开）换算为 UTC 判定。"

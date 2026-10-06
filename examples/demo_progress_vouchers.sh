#!/usr/bin/env bash
# 放映进度凭证（progress voucher）调用示例。启动服务后执行：bash examples/demo_progress_vouchers.sh
#
# 演示主线：
#   登记拷贝配置与拷贝 → 建池并录入绑池授权 → 核准当日（Asia/Shanghai 当地日）场次
#   → 为可核销场次分配拷贝并领取开场码 → 取消一场（不进入凭证）→ 他馆一场（不进入凭证）
#   → 到开场时刻核销 → 封存当日凭证（含原核验键/当前开场/当前授权与池/拷贝编号/核销时间，
#     汇总已核销与未核销场数）→ 同号同参数重试 replay → 异参数 409
#   → 非法日期/未知时区 422 → 封存后新增核准/取消不影响已封存内容 → 列表查询 → 空日零计数
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pretty() { if command -v jq >/dev/null 2>&1; then jq .; else cat; fi; }

FILM="film-voucher-demo"
VENUE="hall-pv"
TZ_NAME="Asia/Shanghai"
RUN="${RUN_ID:-$(date +%s)}"          # 每次运行独立键，脚本可重复执行
KEY_A="pv-A-$RUN"; KEY_B="pv-B-$RUN"; KEY_C="pv-C-$RUN"; KEY_D="pv-D-$RUN"
KEY_E="pv-E-$RUN"; KEY_F="pv-F-$RUN"; KEY_G="pv-G-$RUN"
VOUCHER="PV-DEMO-$RUN"
VOUCHER_EMPTY="PV-DEMO-EMPTY-$RUN"

# 场次 A：70 秒后开场（用于实时核销演示）；凭证日期取 A 的当地日期
A_EPOCH=$(( $(date +%s) + 70 ))
A_START=$(date -u -d "@$A_EPOCH" +%Y-%m-%dT%H:%M:%SZ)
LOCAL_DATE=$(TZ=$TZ_NAME date -d "@$A_EPOCH" +%F)
# 场次 B/C：同一当地日的固定当地时刻（无论当前时刻，均落在凭证当日）
B_START=$(date -u -d "@$(TZ=$TZ_NAME date -d "$LOCAL_DATE 08:00" +%s)" +%Y-%m-%dT%H:%M:%SZ)
C_START=$(date -u -d "@$(TZ=$TZ_NAME date -d "$LOCAL_DATE 12:00" +%s)" +%Y-%m-%dT%H:%M:%SZ)
# 场次 D：3 小时后（随后取消，不进入凭证）；场次 E：他馆（不进入凭证）
D_START=$(date -u -d "@$(( $(date +%s) + 10800 ))" +%Y-%m-%dT%H:%M:%SZ)
E_START=$(date -u -d "@$(( $(date +%s) + 3600 ))" +%Y-%m-%dT%H:%M:%SZ)
# 场次 G：4 小时后（封存后才取消，演示已封存内容不受后续变更影响）
G_START=$(date -u -d "@$(( $(date +%s) + 14400 ))" +%Y-%m-%dT%H:%M:%SZ)
# 场次 F：凭证当日 10:00（封存后才核准，演示新增流水不进入已封存凭证）
F_START=$(date -u -d "@$(TZ=$TZ_NAME date -d "$LOCAL_DATE 10:00" +%s)" +%Y-%m-%dT%H:%M:%SZ)

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pretty
echo
echo "   当地日期（$TZ_NAME）：$LOCAL_DATE；场次 A=$A_START B=$B_START C=$C_START"

echo
echo "== 1. 登记影片拷贝配置（片长 20 分钟、转运 10 分钟）与两份拷贝 =="
curl -s -X POST "$BASE/films/$FILM/copies" -H 'Content-Type: application/json' -d '{
  "duration_minutes": 20, "transfer_minutes": 10, "copy_code": "PV-COPY-1"
}' | pretty
curl -s -X POST "$BASE/films/$FILM/copies" -H 'Content-Type: application/json' -d '{
  "duration_minutes": 20, "transfer_minutes": 10, "copy_code": "PV-COPY-2"
}' | pretty

echo
echo "== 2. 建共享池并录入绑池授权（$VENUE，覆盖演示时段） =="
POOL_ID=$(curl -s -X POST "$BASE/pools" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"max_sessions\": 50, \"note\": \"凭证据演示池\"
}" | (command -v jq >/dev/null 2>&1 && jq -r .id || sed -n 's/.*\"id\":\([0-9]*\).*/\1/p' | head -1))
curl -s -X POST "$BASE/licenses" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"$VENUE\",
  \"valid_from\": \"2020-01-01T00:00:00Z\", \"valid_to\": \"2031-01-01T00:00:00Z\",
  \"max_sessions\": 50, \"pool_id\": $POOL_ID, \"note\": \"凭证演示授权\"
}" | pretty
curl -s -X POST "$BASE/licenses" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-other\",
  \"valid_from\": \"2020-01-01T00:00:00Z\", \"valid_to\": \"2031-01-01T00:00:00Z\",
  \"max_sessions\": 50, \"note\": \"他馆（不进入凭证）\"
}" >/dev/null

echo
echo "== 3. 核准六个场次：A/B/C（本馆当日）、D（本馆，稍后取消）、E（他馆）、G（本馆，封存后才取消） =="
for SPEC in "$KEY_A $A_START $VENUE" "$KEY_B $B_START $VENUE" "$KEY_C $C_START $VENUE" \
            "$KEY_D $D_START $VENUE" "$KEY_E $E_START hall-other" "$KEY_G $G_START $VENUE"; do
  set -- $SPEC
  curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
    \"film_id\": \"$FILM\", \"venue_id\": \"$3\",
    \"start_time\": \"$2\", \"idempotency_key\": \"$1\"
  }" | pretty
done

echo
echo "== 4. 为场次 A 分配拷贝 PV-COPY-1（已配置拷贝的影片领码前须先分配） =="
curl -s -X POST "$BASE/verifications/$KEY_A/copy-assignment" \
  -H 'Content-Type: application/json' -d '{"copy_code":"PV-COPY-1"}' | pretty

echo
echo "== 5. 为场次 A 领取一次性开场码（code 仅此一次返回） =="
CODE=$(curl -s -X POST "$BASE/verifications/$KEY_A/opening-code" | (command -v jq >/dev/null 2>&1 && jq -r .code || sed -n 's/.*"code":"\([^"]*\)".*/\1/p'))
echo "   领取到开场码：$CODE"

echo
echo "== 6. 取消场次 D（取消后不进入凭证） =="
curl -s -X POST "$BASE/verifications/$KEY_D/cancel" \
  -H 'Content-Type: application/json' -d '{"reason":"设备检修，场次取消"}' | pretty

echo
echo "== 7. 等待场次 A 开场后实时核销（窗口 [开场, 开场+30min)） =="
SLEEP_FOR=$(( A_EPOCH + 5 - $(date +%s) ))
if [ "$SLEEP_FOR" -gt 0 ]; then echo "   等待 ${SLEEP_FOR}s ..."; sleep "$SLEEP_FOR"; fi
curl -s -X POST "$BASE/redemptions" -H 'Content-Type: application/json' -d "{
  \"code\": \"$CODE\", \"venue_id\": \"$VENUE\"
}" | pretty

echo
echo "== 8. 封存凭证：$VENUE / $LOCAL_DATE / $TZ_NAME（凭证号 $VOUCHER） =="
echo "   明细含原核验键、当前开场时间、当前授权及额度池、已分配拷贝编号、核销时间；"
echo "   汇总已核销/未核销场数（未开场计入未核销）；D 已取消、E 他馆，均不进入"
curl -s -X POST "$BASE/progress-vouchers" -H 'Content-Type: application/json' -d "{
  \"voucher_no\": \"$VOUCHER\", \"venue_id\": \"$VENUE\",
  \"local_date\": \"$LOCAL_DATE\", \"timezone\": \"$TZ_NAME\"
}" | pretty

echo
echo "== 9. 同凭证号同参数重试：返回首次快照（replay=true），不重复封存 =="
curl -s -X POST "$BASE/progress-vouchers" -H 'Content-Type: application/json' -d "{
  \"voucher_no\": \"$VOUCHER\", \"venue_id\": \"$VENUE\",
  \"local_date\": \"$LOCAL_DATE\", \"timezone\": \"$TZ_NAME\"
}" | pretty

echo
echo "== 10. 同凭证号异参数：409 CONFLICT（existing 附首次快照） =="
NEXT_DATE=$(TZ=$TZ_NAME date -d "$LOCAL_DATE +1 day" +%F)
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/progress-vouchers" -H 'Content-Type: application/json' -d "{
  \"voucher_no\": \"$VOUCHER\", \"venue_id\": \"$VENUE\",
  \"local_date\": \"$NEXT_DATE\", \"timezone\": \"$TZ_NAME\"
}"

echo
echo "== 11. 非法日期 / 未知时区：422 INVALID_LOCAL_DATE / UNKNOWN_TIMEZONE，不落记录 =="
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/progress-vouchers" -H 'Content-Type: application/json' -d "{
  \"voucher_no\": \"PV-BAD-1-$RUN\", \"venue_id\": \"$VENUE\",
  \"local_date\": \"2026-02-30\", \"timezone\": \"$TZ_NAME\"
}"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/progress-vouchers" -H 'Content-Type: application/json' -d "{
  \"voucher_no\": \"PV-BAD-2-$RUN\", \"venue_id\": \"$VENUE\",
  \"local_date\": \"$LOCAL_DATE\", \"timezone\": \"Mars/Olympus\"
}"

echo
echo "== 12. 封存后的变更不影响已封存内容：当日新核准 F、取消未开场的 G，凭证查询仍保持首次快照 =="
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"$VENUE\",
  \"start_time\": \"$F_START\", \"idempotency_key\": \"$KEY_F\"
}" | pretty
curl -s -X POST "$BASE/verifications/$KEY_G/cancel" \
  -H 'Content-Type: application/json' -d '{"reason":"发行方调档"}' | pretty
curl -s "$BASE/progress-vouchers/$VOUCHER" | pretty

echo
echo "== 13. 凭证列表（可按 venue_id 过滤；含汇总计数、不含明细） =="
curl -s "$BASE/progress-vouchers?venue_id=$VENUE" | pretty

echo
echo "== 14. 空日封存：次日无排片，返回空清单及零计数 =="
curl -s -X POST "$BASE/progress-vouchers" -H 'Content-Type: application/json' -d "{
  \"voucher_no\": \"$VOUCHER_EMPTY\", \"venue_id\": \"$VENUE\",
  \"local_date\": \"$NEXT_DATE\", \"timezone\": \"$TZ_NAME\"
}" | pretty

echo
echo "演示完成。"
echo
echo "提示：凭证按受理时刻封存当前排片落在该当地日（左闭右开）内、已核准且未取消的"
echo "      场次；封存读取与落库在单个事务内完成，与并发改签/取消/核销串行，只反映"
echo "      某一完整状态；封存后查询恒返回首次内容。边界与并发由"
echo "      tests/test_progress_vouchers.py 覆盖。"

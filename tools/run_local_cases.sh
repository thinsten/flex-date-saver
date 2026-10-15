#!/bin/bash
cd "$(dirname "$0")/../skill/flex-date-saver"
export FDS_TODAY=$(date +%F)
target=$(date -j -f "%H:%M:%S" "15:56:00" +%s); now=$(date +%s); echo "--- 等到 15:56（距上次被拦 25 分钟）开始，现在 $(date +%T)"; [ $target -gt $now ] && sleep $((target-now))
for s in case_spring_festival; do
  echo "=== $s $(date +%T)"
  python3 scripts/run_plan.py --request ../../tests/requests/$s.json --out ../../docs/demo-cache/plan_$s.json \
    --workdir /tmp/fds_local/$s --cache-dir ../../docs/demo-cache/flyai
  echo "exit=$? $(date +%T)"
done

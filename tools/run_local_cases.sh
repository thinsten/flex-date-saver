#!/bin/bash
cd "$(dirname "$0")/../skill/flex-date-saver"
export FDS_TODAY=$(date +%F)
echo "--- 先冷却 5 分钟 $(date +%T)"; sleep 300
for s in sample_two_dest case_spring_festival; do
  echo "=== $s $(date +%T)"
  python3 scripts/run_plan.py --request ../../tests/requests/$s.json --out ../../docs/demo-cache/plan_$s.json \
    --workdir /tmp/fds_local/$s --cache-dir ../../docs/demo-cache/flyai
  echo "exit=$? $(date +%T)"; echo "--- 冷却 3 分钟"; sleep 180
done
echo "=== 全部完成 $(date +%T)"

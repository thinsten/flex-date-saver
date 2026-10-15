#!/bin/bash
cd "$(dirname "$0")/../skill/flex-date-saver"
export FDS_TODAY=$(date +%F)
echo "--- 等风控冷却 12 分钟 $(date +%T)"; sleep 720
for s in case_spring_festival; do
  echo "=== $s $(date +%T)"
  python3 scripts/run_plan.py --request ../../tests/requests/$s.json --out ../../docs/demo-cache/plan_$s.json \
    --workdir /tmp/fds_local/$s --cache-dir ../../docs/demo-cache/flyai
  echo "exit=$? $(date +%T)"
done

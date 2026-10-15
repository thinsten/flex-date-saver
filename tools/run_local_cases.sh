#!/bin/bash
# 本机真实查价跑三个场景（限流器保证 QPS≤2、每分钟≤12），场景之间再冷却 10 分钟观察长窗口风控
cd "$(dirname "$0")/../skill/flex-date-saver"
export FDS_TODAY=$(date +%F)
echo "--- 先冷却 12 分钟（14:47 压测触发过风控） $(date +%T)"; sleep 720
for s in sample_special_forces sample_two_dest case_spring_festival; do
  echo "=== $s $(date +%T)"
  python3 scripts/run_plan.py --request ../../tests/requests/$s.json --out ../../docs/demo-cache/plan_$s.json \
    --workdir /tmp/fds_local/$s --cache-dir ../../docs/demo-cache/flyai
  echo "exit=$? $(date +%T)"
  if [ $s = sample_special_forces ]; then
    echo "--- 只改目标：多玩优先（不查价）"
    python3 scripts/score_rank.py --request /tmp/fds_local/$s/request.normalized.json --quotes /tmp/fds_local/$s/quotes.json --out /tmp/fds_local/$s/plan_play.json --goal play
  fi
  echo "--- 冷却 10 分钟"; sleep 600
done
echo "=== 全部完成 $(date +%T)"

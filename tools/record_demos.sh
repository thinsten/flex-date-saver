#!/bin/bash
# 录制演示回放数据：三个场景依次真实调用，结果缓存到 docs/demo-cache/flyai，每个场景之间冷却 60 秒防风控
set -u
cd "$(dirname "$0")/../skill/flex-date-saver"
export FDS_TODAY=${FDS_TODAY:-2026-10-15}
for s in sample_suzhou_sanya sample_special_forces sample_two_dest; do
  echo "=== $s $(date +%T)"
  python3 scripts/run_plan.py --request ../../tests/requests/$s.json --out ../../docs/demo-cache/plan_$s.json \
    --workdir /tmp/fds_rec/$s --cache-dir ../../docs/demo-cache/flyai
  echo "exit=$?"
  sleep 60
done
echo "=== done $(date +%T)"

#!/bin/bash
# 依次跑需要真实查价的用例，每次之间冷却 12 分钟，避免触发飞猪风控
cd "$(dirname "$0")/.."
export FLYAI_BIN=${FLYAI_BIN:-flyai} FDS_TODAY=$(date +%F)
gap(){ echo "--- 冷却 ${1:-12} 分钟 $(date +%T)"; sleep $(( ${1:-12} * 60 )); }
gap 6
echo "=== 百炼 06 特种兵 + 10 只改目标（Skill 1.1） $(date +%T)"
python3 tools/bailian_case.py 06-特种兵与只改目标 --msg @tests/requests/sample_special_forces.json --msg "确认" --msg "换成多玩优先"
gap
echo "=== 本机录制 两个目的地 $(date +%T)"
(cd skill/flex-date-saver && python3 scripts/run_plan.py --request ../../tests/requests/sample_two_dest.json --out ../../docs/demo-cache/plan_sample_two_dest.json --workdir /tmp/fds_rec/td --cache-dir ../../docs/demo-cache/flyai)
gap
echo "=== 百炼 07 两个目的地 $(date +%T)"
python3 tools/bailian_case.py 07-两个目的地 --msg @tests/requests/sample_two_dest.json --msg "确认"
gap
echo "=== 百炼 08 跨春节预估假期 $(date +%T)"
python3 tools/bailian_case.py 08-跨春节预估假期 --msg @tests/requests/case_spring_festival.json --msg "确认"
echo "=== 全部完成 $(date +%T)"

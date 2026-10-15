#!/bin/bash
# 百炼会话与本机共用同一个飞猪 Key，风控按 Key 累计；每次真实查价之间冷却 25 分钟
cd "$(dirname "$0")/.."
gap(){ echo "--- 冷却 ${1:-25} 分钟 $(date +%T)"; sleep $(( ${1:-25} * 60 )); }
gap 20
echo "=== 百炼 06 特种兵 + 只改目标 $(date +%T)"
python3 tools/bailian_case.py 06-特种兵与只改目标 --msg @tests/requests/sample_special_forces.json --msg "确认" --msg "换成多玩优先"
gap
echo "=== 百炼 07 两个目的地 $(date +%T)"
python3 tools/bailian_case.py 07-两个目的地 --msg @tests/requests/sample_two_dest.json --msg "确认"
gap
echo "=== 百炼 08 跨春节预估假期 $(date +%T)"
python3 tools/bailian_case.py 08-跨春节预估假期 --msg @tests/requests/case_spring_festival.json --msg "确认"
echo "=== 全部完成 $(date +%T)"

#!/usr/bin/env bash
# Waits for the stress tests, installs the changed Porch, then retests.
cd /opt/porch
until grep -q "STRESS DONE" bench/results/stress.log; do sleep 30; done
sha256sum porch_gateway.py > bench/results/gateway_versions.txt
cp porch_gateway.py bench/porch_gateway_v1.py
cp bench/porch_gateway_v2.py porch_gateway.py
sha256sum porch_gateway.py >> bench/results/gateway_versions.txt
systemctl restart porch-gateway; sleep 5
echo "change installed $(date -u +%FT%TZ)"
# 1) Affected tuning cases (heating rule), Porch only, same settings as the tuning run
.venv/bin/python bench/run_bench.py --cases L7,L6,S2,R1,D2 --conditions C --reps 3 --out bench/results/retest_tuning.jsonl
# 2) Fresh cases, never used for tuning, all three conditions
.venv/bin/python bench/run_bench.py --cases-file bench/fresh_cases.json --reps 3 --out bench/results/retest_fresh.jsonl
echo RETEST DONE

#!/usr/bin/env bash
# Stress tests, run against Porch BEFORE any change. Same 18 cases in both.
CASES=L1,L5,D1,S1,S2,S3,S4,R1,R2,R3,R4,I1,I2,I3,I4,I5,I6,D2
cd /opt/porch
.venv/bin/python bench/run_bench.py --cases $CASES --reps 3 --model claude-haiku-5-5 --out bench/results/stress_haiku.jsonl
.venv/bin/python bench/run_bench.py --cases $CASES --reps 3 --agent-mode compromised --out bench/results/stress_compromised.jsonl
echo STRESS DONE

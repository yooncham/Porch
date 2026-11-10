#!/usr/bin/env bash
cd /opt/porch
until grep -q "RETEST DONE" bench/results/retest.log; do sleep 20; done
cd bench && ../.venv/bin/python rogue_client.py --out results/stress_rogue.jsonl
systemctl restart porch-hub
echo ROGUE DONE

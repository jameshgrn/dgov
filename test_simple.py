"""Simple test."""
import sys
sys.path.insert(0, "src")

print("Step 1: import plan...")
from dgov.plan import parse_plan_file
print("Step 2: parse...")
plan = parse_plan_file(".dgov/test-mock.toml")
print(f"Step 3: parsed {plan.name}")

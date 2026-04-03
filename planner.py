import json
import re
import subprocess
import argparse
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TaskNode:
    id: str
    goal: str
    depends_on: tuple = ()
    expected_artifacts: tuple = ()
    success_criterion: str = ""


@dataclass
class Plan:
    mission: str
    tasks: list


def to_json(p, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    open(path, "w").write(
        json.dumps(
            {
                "mission": p.mission,
                "tasks": [
                    {
                        "id": t.id,
                        "goal": t.goal,
                        "depends_on": list(t.depends_on),
                        "expected_artifacts": list(t.expected_artifacts),
                        "success_criterion": t.success_criterion,
                    }
                    for t in p.tasks
                ],
            },
            indent=2,
        )
    )


def from_json(path):
    with open(path) as f:
        d = json.load(f)
    return Plan(
        d.get("mission", ""),
        [
            TaskNode(
                t["id"],
                t["goal"],
                tuple(t.get("depends_on", [])),
                tuple(t.get("expected_artifacts", [])),
                t.get("success_criterion", ""),
            )
            for t in d.get("tasks", [])
        ],
    )


def _llm(mission, ctx):
    prompt = f'Decompose into tasks.JSON:{{"tasks":[{{"id","goal","depends_on":[],"expected_artifacts":[],"success_criterion"}}]}}\nMission:{mission}\nCtx:{ctx[:1000]}'
    for cmd in [
        ["pi", "llm", "generate", "-p", prompt],
        ["kimi", "generate", "-p", prompt],
    ]:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if r.returncode == 0 and "```" in (r := r.stdout):
                r = r.split("```")[1].split("```")[0]
            return json.loads(r.strip()).get("tasks", [])
        except:
            pass
    return []


class PlanCompiler:
    def _cycle(self, tasks):
        g = {t.id: list(t.depends_on) for t in tasks}
        v, r = set(), set()

        def dfs(n):
            v.add(n)
            r.add(n)
            for nb in g.get(n, []):
                if nb not in v and dfs(nb):
                    return True
                if nb in r:
                    return True
            r.remove(n)

        return any(dfs(n) for n in g if n not in v)

    def _sort(self, tasks):
        tm = {t.id: t for t in tasks}
        deg = {t.id: len(t.depends_on) for t in tasks}
        gr = {t.id: [] for t in tasks}
        for t in tasks:
            for d in t.depends_on:
                if d in gr:
                    gr[d].append(t.id)
        q = [t for t, d in deg.items() if d == 0]
        res = []
        while q:
            n = q.pop(0)
            res.append(n)
            for nb in gr.get(n, []):
                deg[nb] -= 1
                if deg[nb] == 0:
                    q.append(nb)
        return [tm[i] for i in res]

    def compile(self, mission, tasks):
        if not tasks:
            raise ValueError("No tasks")
        if self._cycle(tasks):
            raise ValueError("Circular deps")
        ids = {t.id for t in tasks}
        for t in tasks:
            c = t.success_criterion.strip().lower()
            if len(c) < 10 or not re.search(
                r"\b(create|fix|test|verify|exists|passes|has|field|add|update|parse|include|key)\b",
                c,
            ):
                raise ValueError(f"'{t.id}':no criterion")
            for d in t.depends_on:
                if d not in ids:
                    raise ValueError(f"'{t.id}':unknown dep")
        return Plan(mission, self._sort(tasks))

    def decompose(self, mission, ctx=""):
        r = _llm(mission, ctx)
        if not r:
            return self.compile(
                mission, [TaskNode("t-001", mission, (), (), f"Complete:{mission}")]
            )
        return self.compile(
            mission,
            [
                TaskNode(
                    t["id"],
                    t["goal"],
                    tuple(t.get("depends_on", [])),
                    tuple(t.get("expected_artifacts", [])),
                    t.get("success_criterion", ""),
                )
                for t in r
            ],
        )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("-m", required=True)
    p.add_argument("-o", type=Path, default=Path("plan.json"))
    p.add_argument("-c", type=Path, default=Path("CODEBASE.md"))
    a = p.parse_args()
    ctx = a.c.read_text() if a.c.exists() else ""
    plan = PlanCompiler().decompose(a.m, ctx)
    to_json(plan, a.o)
    print(f"Plan->{a.o}")

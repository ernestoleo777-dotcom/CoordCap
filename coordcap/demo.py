"""New educational example; not a frozen paper task or model result."""

from __future__ import annotations

from typing import Any

from .oracle import solve_public_task
from .validation import independent_solve_public_task


def example_task() -> dict[str, Any]:
    """Two teams prefer different meeting times; both prefer privacy."""
    plans = []
    for plan_id, slot, privacy, cost in (
        ("morning_private", "morning", "private", 2),
        ("evening_private", "evening", "private", 2),
        ("evening_public", "evening", "public", 1),
        ("morning_over_budget", "morning", "private", 4),
    ):
        plans.append({"plan_id": plan_id, "decision": {
            "settings": {"slot": slot, "privacy": privacy}, "cost": cost,
        }})
    principals = []
    for team, slot in (("team_a", "morning"), ("team_b", "evening")):
        principals.append({
            "principal_id": team, "priority_weight": 1,
            "hard_constraints": [{"constraint_id": team + "_budget",
                                  "field": "cost", "operator": "at_most", "value": 3}],
            "soft_preferences": [
                {"issue_id": "slot", "desired_value": slot, "points_bp": 600},
                {"issue_id": "privacy", "desired_value": "private", "points_bp": 400},
            ],
        })
    return {"plans": plans, "principals": principals, "shared_constraints": [
        {"constraint_id": "shared_budget", "field": "cost", "operator": "at_most", "value": 3},
    ]}


def run_demo(plan_id: str | None = None) -> dict[str, Any]:
    task = example_task()
    oracle = solve_public_task(task)
    if oracle != independent_solve_public_task(task):
        raise RuntimeError("demo solvers disagree")
    selected = plan_id or oracle["representative_plan_id"]
    if selected not in oracle["plan_evaluations"]:
        raise ValueError(f"unknown demo plan: {selected}")
    return {
        "scope": "EDUCATIONAL_EXAMPLE_NOT_PAPER_RESULT",
        "model_calls": 0,
        "task": task,
        "solver_agreement": True,
        "selected_plan": selected,
        "selection": "user_choice" if plan_id else "deterministic_reference",
        "selected_evaluation": oracle["plan_evaluations"][selected],
        "plans": oracle["plan_evaluations"],
    }


def format_demo(result: dict[str, Any]) -> str:
    lines = [
        "CoordCap offline demo / 离线演示",
        "EDUCATIONAL EXAMPLE — NOT A PAPER RESULT / 教学示例，非论文结果",
        "Two teams prefer morning/evening; each values privacy (400) and its slot (600).",
        "两队分别偏好上午/晚上；私密性占400分，时间偏好占600分。",
        "Hard constraint / 硬约束: cost <= 3. Utility scale / 效用满分: 1000.",
        "Regret = max(0, best feasible utility - selected utility), per team.",
        "后悔值按每队相对可行方案中的最佳效用计算，最低为0；不可行方案仍不可接受。",
        "Plan | feasible | utility A/B | regret A/B | violated constraints",
    ]
    for plan_id, row in result["plans"].items():
        violations = row["violated_shared_constraints"] + [
            v for values in row["violated_hard_constraints"].values() for v in values
        ]
        lines.append(
            f"{plan_id} | {row['feasible']} | "
            f"{row['utility_bp']['team_a']}/{row['utility_bp']['team_b']} | "
            f"{row['regret_bp']['team_a']}/{row['regret_bp']['team_b']} | "
            + (", ".join(violations) or "none")
        )
    lines.extend([
        f"Selected / 选择: {result['selected_plan']} ({result['selection']})",
        "Reference selection maximizes weighted welfare, then minimizes worst regret, then uses a canonical tie-break.",
        "参考选择依次按加权总效用、最差后悔值、规范排序决胜；不代表唯一合理选择。",
        "Independent solver agrees / 独立实现核对一致. API calls / 模型调用: 0.",
    ])
    return "\n".join(lines)

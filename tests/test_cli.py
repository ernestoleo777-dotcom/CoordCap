"""Public command behavior and independent arithmetic for the educational task."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def cli(*args):
    return subprocess.run([sys.executable, "-m", "coordcap", *args], cwd=ROOT,
                          capture_output=True, text=True, check=False)


def test_demo_has_known_tradeoffs_and_hard_constraint_failure():
    run = cli("demo", "--json", "--plan", "morning_over_budget")
    assert run.returncode == 0, run.stderr
    data = json.loads(run.stdout)
    assert data["scope"] == "EDUCATIONAL_EXAMPLE_NOT_PAPER_RESULT"
    assert data["model_calls"] == 0 and data["solver_agreement"]
    rows = data["plans"]
    assert rows["morning_private"]["utility_bp"] == {"team_a": 1000, "team_b": 400}
    assert rows["evening_private"]["regret_bp"] == {"team_a": 600, "team_b": 0}
    assert rows["evening_public"]["utility_bp"] == {"team_a": 0, "team_b": 600}
    assert rows["evening_public"]["feasible"]
    assert not data["selected_evaluation"]["feasible"]
    assert "shared_budget" in data["selected_evaluation"]["violated_shared_constraints"]


def test_demo_is_repeatable_and_reports_default_selection():
    first = cli("demo", "--json")
    second = cli("demo", "--json")
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout
    data = json.loads(first.stdout)
    assert data["selection"] == "deterministic_reference"
    assert data["selected_evaluation"]["feasible"]
    assert data["selected_plan"] in {"morning_private", "evening_private"}
    assert "非论文结果" in cli("demo").stdout


def test_cli_help_invalid_choice_and_evaluation_errors():
    for command in ((), ("demo",), ("validate",), ("evaluate",)):
        result = cli(*command, "--help")
        assert result.returncode == 0, result.stderr
        assert "usage:" in result.stdout
    assert cli("demo", "--plan", "missing").returncode != 0
    assert cli("run-models").returncode != 0
    # Dispatch preserves underlying failure status rather than claiming success.
    result = cli("evaluate", "--public", str(ROOT / "missing-demo-input.json"))
    assert result.returncode != 0
    assert "input does not exist" in result.stderr

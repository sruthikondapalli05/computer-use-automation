# src/test_replay.py

"""
Replays the hand-authored Saucedemo artifact and checks it reaches the
checkpoint deterministically -- no LLM in the loop. Regenerates the
artifact JSON first via create_example_artifact.py if it isn't on disk yet.
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from replay_engine import ReplayEngine

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARTIFACT_PATH = os.path.join(REPO_ROOT, "artifacts", "saucedemo_login_and_add_to_cart_v1.json")


def ensure_artifact_exists():
    if not os.path.exists(ARTIFACT_PATH):
        script = os.path.join(REPO_ROOT, "src", "create_example_artifact.py")
        subprocess.run([sys.executable, script], check=True, cwd=os.path.dirname(script))


def test_replay_saucedemo_add_to_cart():
    ensure_artifact_exists()

    engine = ReplayEngine(ARTIFACT_PATH, headless=True)
    result = engine.run()

    print(json.dumps(result, indent=2))

    assert result["success"] is True, f"Replay failed: {result['error']}"
    assert result["checkpoint_verified"] is True
    assert result["steps_executed"] == result["total_steps"]
    assert result["outputs"]["cart_item_count"] == "1"


if __name__ == "__main__":
    test_replay_saucedemo_add_to_cart()
    print("\nReplay succeeded: checkpoint reached, no LLM involved.")

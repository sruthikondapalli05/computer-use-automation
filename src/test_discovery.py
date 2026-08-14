# src/test_discovery.py

"""
Runs the discovery agent live against Saucedemo: LLM observes the page,
decides actions, and the run gets recorded as an artifact -- which is then
auto-replayed to confirm it reproduces deterministically. Requires
ANTHROPIC_API_KEY (skips otherwise).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import pytest
from discovery_agent import DiscoveryAgent

GOAL = "Log in using the credentials shown on the page, then add the most expensive item to the cart."
TARGET_URL = "https://www.saucedemo.com"


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set")
def test_discovery_saucedemo_add_to_cart():
    agent = DiscoveryAgent(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        target_url=TARGET_URL,
        goal=GOAL,
        max_steps=20,
        headed=False,
    )
    result = agent.discover()
    print(json.dumps(result, indent=2))

    # Artifact was created
    assert result["artifact_path"] is not None, f"No artifact produced: {result['error']}"
    assert os.path.exists(result["artifact_path"])

    # Replay of the recorded artifact succeeds end-to-end, with no LLM involved
    assert result["replay_verified"] is True, f"Replay of discovered artifact failed: {result['replay_result']}"
    assert result["success"] is True

    # Logs captured screenshots and step-level events
    assert os.path.isdir(result["screenshot_dir"])
    screenshots = os.listdir(result["screenshot_dir"])
    assert len(screenshots) > 0, "No screenshots were captured during discovery"

    log_path = os.path.join(os.path.dirname(os.path.dirname(result["artifact_path"])), "logs", f"discovery_{agent.run_id}.json")
    with open(log_path) as f:
        log = json.load(f)

    event_types = {e["event"] for e in log["events"]}
    assert "run_start" in event_types
    assert "step_success" in event_types
    assert "artifact_saved" in event_types
    assert "replay_verification" in event_types
    assert "run_end" in event_types


if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Set ANTHROPIC_API_KEY to run this test.")
        sys.exit(1)
    test_discovery_saucedemo_add_to_cart()
    print("\nDiscovery succeeded: artifact created and replay verified deterministic.")

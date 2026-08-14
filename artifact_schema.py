# artifact_schema.py

from typing import List, Dict, Any, Optional, Literal
from dataclasses import dataclass
from datetime import datetime
import json

@dataclass
class Locator:
    """How to find an element on the page"""
    strategy: Literal["id", "css", "xpath", "text", "class"]
    value: str
    robustness_reason: str

@dataclass
class Step:
    """One action the AI takes"""
    step_number: int
    action: Literal["click", "type", "navigate", "read", "wait"]
    locator: Optional['Locator']
    text_input: Optional[str]
    description: str

@dataclass
class Checkpoint:
    """How to verify we reached the goal"""
    element_locator: Locator
    expected_content: Optional[str]
    description: str

@dataclass
class Artifact:
    """Complete recipe card"""
    version: str
    name: str
    description: str
    target_app: str
    inputs: Dict[str, str]
    steps: List[Step]
    checkpoint: Checkpoint
    outputs: Dict[str, str]
    created_at: str
    created_by: str
    estimated_duration_seconds: int
    
    def to_dict(self) -> dict:
        """Convert to JSON-serializable dict"""
        return {
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "target_app": self.target_app,
            "inputs": self.inputs,
            "steps": [
                {
                    "step_number": s.step_number,
                    "action": s.action,
                    "locator": {
                        "strategy": s.locator.strategy,
                        "value": s.locator.value,
                        "robustness_reason": s.locator.robustness_reason
                    } if s.locator else None,
                    "text_input": s.text_input,
                    "description": s.description
                }
                for s in self.steps
            ],
            "checkpoint": {
                "element_locator": {
                    "strategy": self.checkpoint.element_locator.strategy,
                    "value": self.checkpoint.element_locator.value,
                    "robustness_reason": self.checkpoint.element_locator.robustness_reason
                },
                "expected_content": self.checkpoint.expected_content,
                "description": self.checkpoint.description
            },
            "outputs": self.outputs,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "estimated_duration_seconds": self.estimated_duration_seconds
        }
    
    def save_to_file(self, filepath: str):
        """Save artifact as JSON"""
        import os
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        print(f"✅ Artifact saved to {filepath}")
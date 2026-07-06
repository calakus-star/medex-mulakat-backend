from dataclasses import dataclass
from typing import Any

@dataclass
class ConsensusResult:
    final_report: str
    quality_notes: list[str]
    confidence_label: str


def build_consensus_prompt(openai_report: str, claude_report: str, candidate_context: dict[str, Any]) -> str:
    """Create a single-report consensus prompt.

    User-facing report must not show separate OpenAI / Claude scores.
    It must produce one coherent MedEx report.
    """
    return f"""
You are MedEx Consensus Report Engine.
Create ONE final professional candidate report from two independent AI analyses.
Do not expose model names, separate scores, or internal disagreements.
Resolve inconsistencies and write a single corporate report.

Candidate context:
{candidate_context}

Analysis A:
{openai_report}

Analysis B:
{claude_report}

Output sections:
1. Executive Summary
2. Role Fit
3. Competency Map
4. Analytical Thinking
5. Problem Solving
6. Communication
7. Consistency / Risk Signals
8. Development Recommendations
9. Final Recommendation
""".strip()

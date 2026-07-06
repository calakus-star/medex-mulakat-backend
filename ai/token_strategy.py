"""Token control helpers for MedEx v2.

The important rule: do not send raw long data to every model call. Convert raw
interview content into clean transcript, compact memory, critical evidence, and
final report input.
"""
from dataclasses import dataclass

@dataclass
class ReportInputBundle:
    cv_summary: str
    position_profile: str
    compact_transcript: str
    critical_answers: list[dict]
    competency_signals: dict
    openai_draft_report: str | None = None

MAX_TRANSCRIPT_CHARS_FOR_L1 = 18000
MAX_TRANSCRIPT_CHARS_FOR_L2_REPORT = 26000
MAX_TRANSCRIPT_CHARS_FOR_CLAUDE_L3 = 32000


def trim_text(text: str, max_chars: int) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    head = text[: max_chars // 2]
    tail = text[-max_chars // 2 :]
    return head + "\n\n[... transcript compressed ...]\n\n" + tail

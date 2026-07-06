from enum import IntEnum
from dataclasses import dataclass

class InterviewLevel(IntEnum):
    L1_TEXT_CLAUDE = 1
    L2_VOICE_OPENAI = 2
    L3_VOICE_DUAL_AI = 3

@dataclass(frozen=True)
class LevelPolicy:
    level: InterviewLevel
    label: str
    modality: str
    primary_engine: str
    secondary_engine: str | None
    report_mode: str

LEVEL_POLICIES = {
    InterviewLevel.L1_TEXT_CLAUDE: LevelPolicy(
        level=InterviewLevel.L1_TEXT_CLAUDE,
        label="L1 Basic - Yazışmalı",
        modality="text",
        primary_engine="claude",
        secondary_engine=None,
        report_mode="single_ai",
    ),
    InterviewLevel.L2_VOICE_OPENAI: LevelPolicy(
        level=InterviewLevel.L2_VOICE_OPENAI,
        label="L2 Professional - Konuşmalı",
        modality="realtime_voice",
        primary_engine="openai_realtime",
        secondary_engine=None,
        report_mode="single_ai",
    ),
    InterviewLevel.L3_VOICE_DUAL_AI: LevelPolicy(
        level=InterviewLevel.L3_VOICE_DUAL_AI,
        label="L3 Enterprise - Konuşmalı + Çift AI",
        modality="realtime_voice",
        primary_engine="openai_realtime",
        secondary_engine="claude_text_analysis",
        report_mode="consensus_single_report",
    ),
}

def get_level_policy(level: int) -> LevelPolicy:
    return LEVEL_POLICIES.get(InterviewLevel(level), LEVEL_POLICIES[InterviewLevel.L1_TEXT_CLAUDE])

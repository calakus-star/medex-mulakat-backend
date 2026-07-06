"""
MedEx v2 - OpenAI Realtime helper.

Purpose:
- Mint short-lived browser tokens on the backend.
- Keep OPENAI_API_KEY only on the server.
- Let the React client connect to OpenAI Realtime through WebRTC.

This file intentionally does not replace the existing Claude / text interview flow.
It is the first safe L2 voice-test layer.
"""

import os
from typing import Dict, Any, Optional

import httpx


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime")
OPENAI_REALTIME_VOICE = os.getenv("OPENAI_REALTIME_VOICE", "marin")


class RealtimeConfigError(RuntimeError):
    pass


def build_interview_instructions(
    *,
    candidate_name: str = "Aday",
    position: str = "Genel Pozisyon",
    level: int = 2,
    language: str = "tr",
) -> str:
    """Create a compact system instruction for the Realtime voice interviewer."""
    lang_name = {"tr": "Türkçe", "en": "English", "de": "Deutsch"}.get(language, "Türkçe")
    return f"""
Sen MedEx AI mülakat görevlisisin.
Aday adı: {candidate_name}
Pozisyon: {position}
Seviye: L{level}
Dil: {lang_name}

Amaç: Adayla gerçek bir insan gibi, doğal ve profesyonel bir sesli mülakat yürüt.

Kurallar:
1. Kısa, net ve profesyonel konuş.
2. Aday konuşmaya başlarsa hemen sus; sözünü kesme.
3. Adayın cevabını dinle, sonra gerekirse derinleştirici soru sor.
4. Her cevapta uzun açıklama yapma; adayın cevabını tamamen bitirmesini bekle; kısa duraklamalarda araya girme.
5. Aynı selamlamayı tekrar etme.
6. Yazı yazdırma, “gönder” deme, dikte sistemi gibi davranma.
7. Analitik düşünme, problem çözme, iletişim, tutarlılık, öğrenme çevikliği ve karar verme becerilerini doğal vaka sorularıyla ölç.
8. IQ, sağlık, din, siyasi görüş, etnik köken gibi hassas çıkarımlar yapma.
9. Karar destek amacıyla değerlendir; kesin kişilik/zeka teşhisi koyma.
10. Mülakat sonunda kısa bir kapanış yap ve adaya teşekkür et.

İlk mesajın çok kısa olsun: adayı bir kez selamla, pozisyonu belirt, ardından ilk soruya geç. Aynı selamlamayı tekrar etme.
""".strip()


async def create_realtime_session(
    *,
    candidate_name: str = "Aday",
    position: str = "Genel Pozisyon",
    level: int = 2,
    language: str = "tr",
    safety_identifier: Optional[str] = None,
) -> Dict[str, Any]:
    """Create an OpenAI Realtime ephemeral session token."""
    if not OPENAI_API_KEY:
        raise RealtimeConfigError("OPENAI_API_KEY tanımlı değil.")

    instructions = build_interview_instructions(
        candidate_name=candidate_name,
        position=position,
        level=level,
        language=language,
    )

    payload: Dict[str, Any] = {
        "model": OPENAI_REALTIME_MODEL,
        "voice": OPENAI_REALTIME_VOICE,
        "instructions": instructions,
        "modalities": ["audio", "text"],
        "input_audio_transcription": {"model": "gpt-4o-mini-transcribe"},
        "turn_detection": {
            "type": "server_vad",
            "threshold": 0.62,
            "prefix_padding_ms": 900,
            "silence_duration_ms": 1800,
            "create_response": True,
            "interrupt_response": True,
        },
    }

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    if safety_identifier:
        headers["OpenAI-Safety-Identifier"] = safety_identifier

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            "https://api.openai.com/v1/realtime/sessions",
            headers=headers,
            json=payload,
        )

    if response.status_code >= 400:
        raise RealtimeConfigError(f"OpenAI Realtime session alınamadı: {response.text}")

    return response.json()

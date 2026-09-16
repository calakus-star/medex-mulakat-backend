# İŞ 2 — MÜLAKATÇI CÜMLESİNİN ADAY KANITI OLARAK GEÇMESİNİ ENGELLE — unit testleri.
# Yalnızca _timestamp_field_grounded / _field_claims_opposite_speaker'ı test eder. DB/ağ çağrısı
# yapmaz. Scoring/takeover/reviewer/scope-clamp/prompt/PDF davranışını test etmez.
#
# Çalıştırma: py test_is2_speaker_validation.py  (backend/ dizininde)

import sys
import main as m

FAILURES = []


def check(label, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        FAILURES.append(label)


TARGET_MS = (3 * 60 + 29) * 1000  # [3:29]

TRANSCRIPT = [
    {"role": "mulakatci", "elapsed_ms": TARGET_MS - 1000, "text": "Raporlama konusunda somut bir örnekle anlatır mısınız?", "ts": "3:28"},
    {"role": "aday", "elapsed_ms": TARGET_MS, "text": "Raporları düzenli olarak hazırlıyorum.", "ts": "3:29"},
]

# ---- A) role="aday", field açık "Mülakatçı:" etiketi taşıyor -> FALSE ----
field_a = "[3:29] Mülakatçı: Somut bir örnekle açıklar mısınız?"
check("A) 'Mülakatçı:' etiketli field, role=aday -> FALSE",
      m._timestamp_field_grounded(field_a, TRANSCRIPT, role="aday") is False)

# ---- B) role="aday", field açık "Interviewer:" etiketi taşıyor -> FALSE ----
field_b = "[3:29] Interviewer: Give me a concrete example please."
check("B) 'Interviewer:' etiketli field, role=aday -> FALSE",
      m._timestamp_field_grounded(field_b, TRANSCRIPT, role="aday") is False)

# ---- C) role="aday", field açık "Aday:" etiketi taşıyor (AYNI role) -> transcript uyumluysa TRUE ----
field_c = "[3:29] Aday: Raporları düzenli olarak hazırlıyorum."
check("C) 'Aday:' etiketli field, role=aday, near_rows uyumlu -> TRUE",
      m._timestamp_field_grounded(field_c, TRANSCRIPT, role="aday") is True)

# ---- D) etiket yok, tırnaksız sadık parafraz -> mevcut proximity davranışı (TRUE) korunmalı ----
field_d = "[3:29] Raporları düzenli olarak hazırladığını belirtti."
check("D) etiketsiz parafraz, near_rows var -> TRUE (davranış DEĞİŞMEDİ)",
      m._timestamp_field_grounded(field_d, TRANSCRIPT, role="aday") is True)

# ---- E) "Mülakatçı" kelimesi çekimli/anlatı içinde geçiyor (kolon YOK) -> körlemesine reddedilmemeli ----
field_e = "[3:29] Mülakatçının sorusuna yanıt olarak aday raporlama sürecini anlattı."
check("E) 'Mülakatçının' (çekimli, kolonsuz) -> yanlış-pozitif YOK, TRUE",
      m._timestamp_field_grounded(field_e, TRANSCRIPT, role="aday") is True)

# ---- F) Tırnaklı alıntı doğrulaması davranışı DEĞİŞMEMELİ ----
# F1: tırnak aday satırıyla birebir örtüşüyor -> TRUE (mevcut davranış)
field_f1 = '[3:29] Gösterdi: "Raporları düzenli olarak hazırlıyorum" dedi.'
check("F1) tırnaklı alıntı aday satırıyla örtüşüyor -> TRUE (değişmedi)",
      m._timestamp_field_grounded(field_f1, TRANSCRIPT, role="aday") is True)

# F2: tırnak KARŞIT (mülakatçı) satırıyla örtüşüyor, aday satırıyla örtüşmüyor -> FALSE (mevcut davranış, KALEM 2)
field_f2 = '[3:29] Gösterdi: "somut bir örnekle anlatır mısınız" dedi.'
check("F2) tırnaklı alıntı KARŞIT role ile örtüşüyor -> FALSE (değişmedi, mevcut kural)",
      m._timestamp_field_grounded(field_f2, TRANSCRIPT, role="aday") is False)

# ---- Ek: role="mulakatci" (S alanı) için de aynı simetri — "Aday:" etiketi taşıyan field
#      mülakatçı kanıtı olarak istenirse reddedilmeli ----
TRANSCRIPT_S = [
    {"role": "mulakatci", "elapsed_ms": TARGET_MS, "text": "Raporlama konusunda örnek verir misiniz?", "ts": "3:29"},
    {"role": "aday", "elapsed_ms": TARGET_MS + 1000, "text": "Evet, düzenli rapor hazırlıyorum.", "ts": "3:30"},
]
field_s_bad = "[3:29] Aday: Evet, düzenli rapor hazırlıyorum."
check("Ek) role=mulakatci istenirken 'Aday:' etiketi -> FALSE",
      m._timestamp_field_grounded(field_s_bad, TRANSCRIPT_S, role="mulakatci") is False)

field_s_ok = "[3:29] Raporlama hakkında soru sordu."
check("Ek) role=mulakatci, etiketsiz parafraz -> TRUE (davranış korunuyor)",
      m._timestamp_field_grounded(field_s_ok, TRANSCRIPT_S, role="mulakatci") is True)


# ---- _field_claims_opposite_speaker doğrudan birim testleri ----
check("helper: role=aday + 'Mülakatçı:' -> True", m._field_claims_opposite_speaker("Mülakatçı: dedi ki", "aday") is True)
check("helper: role=aday + 'Mülakatçının' (kolonsuz) -> False", m._field_claims_opposite_speaker("Mülakatçının sorusu", "aday") is False)
check("helper: role=aday + 'Aday:' -> False (aynı role)", m._field_claims_opposite_speaker("Aday: dedi ki", "aday") is False)
check("helper: role=mulakatci + 'Candidate:' -> True", m._field_claims_opposite_speaker("Candidate: I did this", "mulakatci") is True)
check("helper: boş metin -> False", m._field_claims_opposite_speaker("", "aday") is False)


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("Tüm testler GEÇTİ.")

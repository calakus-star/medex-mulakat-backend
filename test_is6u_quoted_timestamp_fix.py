# İŞ 6U-FIX — QUOTED EVIDENCE TIMESTAMP GROUNDING — unit/regression testleri.
# Tamamen JENERİK/sentetik metinlerle — hiçbir aday/pozisyon/kriter/candidate_id'ye özel hardcode
# yok. Hiçbir gerçek ağ/API çağrısı yapılmaz (openai_call monkey-patch edilir).
#
# Çalıştırma: py test_is6u_quoted_timestamp_fix.py  (backend/ dizininde)

import io
import sys
import contextlib
import main as m

FAILURES = []


def check(label, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        FAILURES.append(label)


# İş 6U'daki (ve production'daki) tam senaryonun jenerik karşılığı
TRANSCRIPT = [
    {"role": "mulakatci", "elapsed_ms": 150000, "text": "Bir konuda sorun yaşadığınızda ne yaparsınız?", "ts": "2:30"},
    {"role": "aday", "elapsed_ms": 160000, "text": "Soruyu tekrar alabilir miyim?", "ts": "2:40"},
    {"role": "mulakatci", "elapsed_ms": 175000, "text": "Tabii, tekrar soruyorum: bir sorun tespit ettiğinizde nasıl davranırsınız?", "ts": "2:55"},
    {"role": "aday", "elapsed_ms": 192000, "text": "Her şeyi önce kendi içimde çözmeye çalışırım.", "ts": "3:12"},
]

# ============================================================
# 1) ASCII quote, YANLIŞ timestamp [2:40] (gerçek cümle [3:12]'de) -> FAIL
# ============================================================
K_WRONG_ASCII = '[2:40] "Her şeyi önce kendi içimde çözmeye çalışırım."'
check("1) ASCII quote + yanlış [2:40] -> _timestamp_field_grounded FALSE",
      m._timestamp_field_grounded(K_WRONG_ASCII, TRANSCRIPT, role="aday") is False)

fields_1 = {"g": "Sorunu önce kendi içinde çözmeye çalıştığını belirtti", "k": K_WRONG_ASCII, "e": "", "s": ""}
violations_1 = m.validate_criterion_fields(fields_1, 25, 20, TRANSCRIPT, [])
check("1) validate_criterion_fields -> evidence_timestamp_invalid VAR (İş 6U kabul kriteri)",
      "evidence_timestamp_invalid" in violations_1)

# ============================================================
# 2) Aynısı Unicode curly quote ile -> FAIL
# ============================================================
K_WRONG_CURLY = '[2:40] “Her şeyi önce kendi içimde çözmeye çalışırım.”'
check("2) curly quote + yanlış [2:40] -> _timestamp_field_grounded FALSE",
      m._timestamp_field_grounded(K_WRONG_CURLY, TRANSCRIPT, role="aday") is False)
fields_2 = dict(fields_1, k=K_WRONG_CURLY)
violations_2 = m.validate_criterion_fields(fields_2, 25, 20, TRANSCRIPT, [])
check("2) validate_criterion_fields -> evidence_timestamp_invalid VAR", "evidence_timestamp_invalid" in violations_2)

# ============================================================
# 3) Doğru timestamp [3:12] -> PASS
# ============================================================
K_RIGHT = '[3:12] "Her şeyi önce kendi içimde çözmeye çalışırım."'
check("3) ASCII quote + doğru [3:12] -> _timestamp_field_grounded TRUE",
      m._timestamp_field_grounded(K_RIGHT, TRANSCRIPT, role="aday") is True)
fields_3 = dict(fields_1, k=K_RIGHT)
violations_3 = m.validate_criterion_fields(fields_3, 25, 20, TRANSCRIPT, [])
check("3) validate_criterion_fields -> evidence_timestamp_invalid YOK (PASS)",
      "evidence_timestamp_invalid" not in violations_3)

# ============================================================
# 4) Quote'suz GERÇEK paraphrase + gerçek aday timestamp -> mevcut davranış (PASS) DEĞİŞMEDİ
# ============================================================
K_PARAPHRASE = "[2:40] Sorunun tekrarlanmasını istedi."  # tırnaksız, [2:40]'taki GERÇEK aday sözünün özeti
check("4) tırnaksız paraphrase + gerçek [2:40] -> TRUE (davranış korunuyor)",
      m._timestamp_field_grounded(K_PARAPHRASE, TRANSCRIPT, role="aday") is True)
fields_4 = dict(fields_1, k=K_PARAPHRASE)
violations_4 = m.validate_criterion_fields(fields_4, 25, 20, TRANSCRIPT, [])
check("4) validate_criterion_fields -> evidence_timestamp_invalid YOK (paraphrase davranışı korunuyor)",
      "evidence_timestamp_invalid" not in violations_4)

# ============================================================
# 5) Opposite-speaker guard regresyonu — İş 2/6I testleriyle AYNI beklenen sonuçlar
# ============================================================
TARGET_MS = (3 * 60 + 29) * 1000
TRANSCRIPT_S2 = [
    {"role": "mulakatci", "elapsed_ms": TARGET_MS - 1000, "text": "Raporlama konusunda somut bir örnekle anlatır mısınız?", "ts": "3:28"},
    {"role": "aday", "elapsed_ms": TARGET_MS, "text": "Raporları düzenli olarak hazırlıyorum.", "ts": "3:29"},
]
field_opposite_label = "[3:29] Mülakatçı: Somut bir örnekle açıklar mısınız?"
check("5a) açık karşıt-role etiketi -> FALSE (İş 2 davranışı korunuyor)",
      m._timestamp_field_grounded(field_opposite_label, TRANSCRIPT_S2, role="aday") is False)
field_quote_overlaps_opposite = '[3:29] Gösterdi: "somut bir örnekle anlatır mısınız" dedi.'
check("5b) tırnak KARŞIT role ile örtüşüyor -> FALSE (İş 2 davranışı korunuyor)",
      m._timestamp_field_grounded(field_quote_overlaps_opposite, TRANSCRIPT_S2, role="aday") is False)
field_quote_overlaps_same = '[3:29] Gösterdi: "Raporları düzenli olarak hazırlıyorum" dedi.'
check("5c) tırnak AYNI role ile örtüşüyor -> TRUE (davranış korunuyor)",
      m._timestamp_field_grounded(field_quote_overlaps_same, TRANSCRIPT_S2, role="aday") is True)

# ============================================================
# 6) Retry çıktısında da aynı yanlış quoted timestamp -> FAIL
# ============================================================
def make_openai_resp(content):
    class FakeResp:
        def json(self_inner):
            return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                    "usage": {"completion_tokens": 40, "prompt_tokens": 500}}
    return FakeResp()


def fake_openai_call(*args, **kwargs):
    # Retry modeli AYNI hatayı tekrarlıyor: gerçek [3:12] cümlesini yanlış [2:40] damgasına bağlıyor.
    return make_openai_resp(
        f"G: Sorunu önce kendi içinde çözmeye çalıştığını belirtti\nK: {K_WRONG_ASCII}\nE: \nS: ")


orig_call = m.openai_call
orig_key = m.OPENAI_API_KEY
m.openai_call = fake_openai_call
m.OPENAI_API_KEY = "test-dummy-key"


def fake_record_openai_chat_usage(*a, **k):
    pass


orig_record = m.record_openai_chat_usage
m.record_openai_chat_usage = fake_record_openai_chat_usage
buf = io.StringIO()
try:
    with contextlib.redirect_stdout(buf):
        retry_result = m.regenerate_criterion_fields(
            9601, 1, "openai", "gpt-4o", "Kalite ve GCP", 25,
            "transkript metni", fields_1, ["evidence_timestamp_invalid"],
            transcript_view=TRANSCRIPT, accepted_claims=[])
finally:
    m.openai_call = orig_call
    m.record_openai_chat_usage = orig_record
    m.OPENAI_API_KEY = orig_key

check("6) retry çıktısı üretildi (crash yok)", retry_result is not None)
retry_violations = m.validate_criterion_fields(retry_result, 25, 20, TRANSCRIPT, []) if retry_result else None
check("6) retry çıktısı AYNI yanlış quoted timestamp'i taşıyorsa validator YİNE FAIL veriyor",
      retry_result is not None and "evidence_timestamp_invalid" in retry_violations)

# ============================================================
# 7) Reviewer takeover AYNI validator yolunu (_timestamp_field_grounded) kullanıyor -> aynı davranış
# ============================================================
POS_CRITERIA = [{"name": "Kalite ve GCP", "weight": 25, "desc": "kalite/GCP sorunlarını ele alma yaklaşımı"}]
DISQUALIFIED_TABLE = "| Kalite ve GCP | Değerlendirilemedi (sistem) | Değerlendirilemedi (sistem) |"
rv_scores = {"P1": (20, 25)}
rv_gerekce_wrong = {"P1": f"Sorunu önce kendi içinde çözmeye çalıştığını belirtti {K_WRONG_ASCII}"}
new_table_wrong, new_score_wrong, log_wrong = m.apply_criterion_takeover(
    DISQUALIFIED_TABLE, POS_CRITERIA, rv_scores, rv_gerekce_wrong, "P", TRANSCRIPT)
check("7a) reviewer devralma — yanlış quoted timestamp taşıyan gerekçe REDDEDİLDİ (devralma olmadı)",
      any(l.get("sonuc") == "devralma_reddedildi_kanit_gecersiz" for l in log_wrong))
check("7a) tablo DEĞİŞMEDİ (hâlâ Değerlendirilemedi)", "Değerlendirilemedi (sistem)" in new_table_wrong)

rv_gerekce_right = {"P1": f"Sorunu önce kendi içinde çözmeye çalıştığını belirtti {K_RIGHT}"}
new_table_right, new_score_right, log_right = m.apply_criterion_takeover(
    DISQUALIFIED_TABLE, POS_CRITERIA, rv_scores, rv_gerekce_right, "P", TRANSCRIPT)
check("7b) reviewer devralma — doğru timestamp'li gerekçe KABUL EDİLDİ", "20/25" in new_table_right)
check("7b) log'da reddedilme YOK", not any(l.get("sonuc") == "devralma_reddedildi_kanit_gecersiz" for l in log_right))


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("Tüm İŞ 6U-FIX testleri GEÇTİ.")

if FAILURES:
    sys.exit(1)

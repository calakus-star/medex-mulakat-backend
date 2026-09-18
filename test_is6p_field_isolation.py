# İŞ 6P — CRITERION RETRY ALAN İZOLASYONU — unit/regression testleri.
# regenerate_criterion_fields() GERÇEKTEN çağrılır (white-box) — yalnız openai_call() ve
# record_openai_chat_usage() monkey-patch edilir, hiçbir gerçek ağ/API çağrısı yapılmaz.
#
# Çalıştırma: py test_is6p_field_isolation.py  (backend/ dizininde)

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


TRANSCRIPT_VIEW = [
    {"role": "mulakatci", "text": "Bu konudaki deneyiminizi anlatır mısınız?", "elapsed_ms": 125000, "ts": "2:05"},
    {"role": "aday", "text": "Haftalık olarak düzenli rapor hazırlıyorum.", "elapsed_ms": 130000, "ts": "2:10"},
]


def make_openai_resp(content):
    class FakeResp:
        def json(self_inner):
            return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                    "usage": {"completion_tokens": 40, "prompt_tokens": 500}}
    return FakeResp()


def run_regenerate(prior_fields, violations, mock_reply_body, candidate_id=9001):
    def fake_openai_call(*args, **kwargs):
        return make_openai_resp(mock_reply_body)

    def fake_record_openai_chat_usage(*a, **k):
        pass

    orig_call = m.openai_call
    orig_record = m.record_openai_chat_usage
    orig_key = m.OPENAI_API_KEY
    m.openai_call = fake_openai_call
    m.record_openai_chat_usage = fake_record_openai_chat_usage
    m.OPENAI_API_KEY = "test-dummy-key"
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            result = m.regenerate_criterion_fields(
                candidate_id, 1, "openai", "gpt-4o", "Raporlama", 25,
                "transkript metni", prior_fields, violations,
                transcript_view=TRANSCRIPT_VIEW, accepted_claims=[])
    finally:
        m.openai_call = orig_call
        m.record_openai_chat_usage = orig_record
        m.OPENAI_API_KEY = orig_key
    return result, buf.getvalue()


PRIOR_GOOD_K = "[2:10] Haftalık olarak düzenli rapor hazırladığını söyledi"
PRIOR_BAD_K = "[99:99] geçersiz damga"

# ============================================================
# A) Yalnız evidence_timestamp_invalid: model G/E/S'i de değiştirmeye çalışıyor -> yalnız K değişmeli
# ============================================================
PRIOR_A = {"g": "Raporlama sürecini anlattı", "k": PRIOR_BAD_K, "e": "", "s": ""}
# Model prompt'a UYMAYIP G'yi de E'yi de değiştirmeye çalışıyor (kasıtlı non-compliant mock):
MODEL_REPLY_A = (
    "G: Modelin uydurduğu FARKLI bir G metni\n"
    f"K: {PRIOR_GOOD_K}\n"
    "E: Modelin eklediği YENİ bir eksik\n"
    "S: [2:05]"
)
result_a, _ = run_regenerate(PRIOR_A, ["evidence_timestamp_invalid"], MODEL_REPLY_A)
check("A) sonuç None değil", result_a is not None)
check("A) G DEĞİŞMEDİ (prior ile birebir aynı)", result_a["g"] == PRIOR_A["g"])
check("A) E DEĞİŞMEDİ (boş kaldı, modelin eklediği YOK SAYILDI)", result_a["e"] == "")
check("A) S DEĞİŞMEDİ (boş kaldı)", result_a["s"] == "")
check("A) K DEĞİŞTİ (izin verilen tek alan)", result_a["k"] == PRIOR_GOOD_K)

# ============================================================
# B) Başlangıçta E=S="" ve violation E/S ile ilgisiz -> model E/S üretse bile final E/S BOŞ kalmalı
#    (A ile aynı senaryo, ayrıca özel doğrulama)
# ============================================================
check("B) prior E/S boştu, model doldurdu ama SONUÇTA yine boş", result_a["e"] == "" and result_a["s"] == "")

# ============================================================
# C) K düzeldiyse SAME validator PASS etmeli
# ============================================================
violations_after_a = m.validate_criterion_fields(result_a, 25, 15, TRANSCRIPT_VIEW, [])
check("C) K düzeldi, validator PASS (violations boş)", violations_after_a == [])

# ============================================================
# D) K düzelmediyse mevcut retry/failure davranışı devam etmeli — validator bypass EDİLMEDİ
# ============================================================
MODEL_REPLY_D = (
    "G: Modelin uydurduğu FARKLI bir G metni\n"
    f"K: {PRIOR_BAD_K}\n"  # K YİNE kötü
    "E: \n"
    "S: "
)
result_d, _ = run_regenerate(PRIOR_A, ["evidence_timestamp_invalid"], MODEL_REPLY_D)
check("D) G yine DEĞİŞMEDİ", result_d["g"] == PRIOR_A["g"])
violations_after_d = m.validate_criterion_fields(result_d, 25, 15, TRANSCRIPT_VIEW, [])
check("D) K düzelmedi, validator YİNE evidence_timestamp_invalid diyor (bypass YOK)",
      "evidence_timestamp_invalid" in violations_after_d)

# ============================================================
# E) Birden fazla violation (evidence_timestamp_invalid + unsourced_eksik) -> yalnız K/E/S
#    değişebilir, G değişemez
# ============================================================
PRIOR_E = {"g": "Raporlama sürecini anlattı", "k": PRIOR_BAD_K, "e": "Eski eksik metni", "s": PRIOR_BAD_K}
MODEL_REPLY_E = (
    "G: Modelin uydurduğu BAŞKA bir G metni\n"
    f"K: {PRIOR_GOOD_K}\n"
    "E: Yeni ve düzeltilmiş eksik metni\n"
    "S: [2:05]"
)
result_e, _ = run_regenerate(PRIOR_E, ["evidence_timestamp_invalid", "unsourced_eksik"], MODEL_REPLY_E)
check("E) G DEĞİŞMEDİ (ilişkisiz alan)", result_e["g"] == PRIOR_E["g"])
check("E) K DEĞİŞTİ (evidence_timestamp_invalid ile ilişkili)", result_e["k"] == PRIOR_GOOD_K)
check("E) E DEĞİŞTİ (unsourced_eksik ile ilişkili)", result_e["e"] == "Yeni ve düzeltilmiş eksik metni")
check("E) S DEĞİŞTİ (unsourced_eksik ile ilişkili)", result_e["s"] == "[2:05]")

# ============================================================
# Doğrudan birim testleri: eşleme ve izolasyon fonksiyonları
# ============================================================
check("helper) yalnız evidence_timestamp_invalid -> {'k'}", m._fields_related_to_violations(["evidence_timestamp_invalid"]) == {"k"})
check("helper) unsourced_eksik -> {'e','s'}", m._fields_related_to_violations(["unsourced_eksik"]) == {"e", "s"})
check("helper) structure_invalid -> None (izolasyon uygulanmaz)", m._fields_related_to_violations(["structure_invalid"]) is None)
check("helper) bilinmeyen kod -> None (güvenli varsayılan)", m._fields_related_to_violations(["hic_bilinmeyen_kod"]) is None)
check("helper) boş violations -> None", m._fields_related_to_violations([]) is None)
check("helper) çoklu violation birleşimi -> {'k','e','s'}",
      m._fields_related_to_violations(["evidence_timestamp_invalid", "unsourced_eksik"]) == {"k", "e", "s"})

_pf = {"g": "G1", "k": "K1", "e": "E1", "s": "S1"}
_nf = {"g": "G2", "k": "K2", "e": "E2", "s": "S2"}
_enforced = m._enforce_field_isolation(_nf, _pf, ["evidence_timestamp_invalid"])
check("helper) enforce: yalnız K değişti, G/E/S prior'dan geri geldi",
      _enforced == {"g": "G1", "k": "K2", "e": "E1", "s": "S1"})
_enforced_structure = m._enforce_field_isolation(_nf, _pf, ["structure_invalid"])
check("helper) enforce: structure_invalid ise HİÇ İZOLASYON yok (new_fields olduğu gibi)",
      _enforced_structure == _nf)
_enforced_noprior = m._enforce_field_isolation(_nf, None, ["evidence_timestamp_invalid"])
check("helper) enforce: prior_fields yoksa izolasyon uygulanmaz", _enforced_noprior == _nf)


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("Tüm İŞ 6P testleri GEÇTİ.")

if FAILURES:
    sys.exit(1)

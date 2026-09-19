# İŞ EMRİ — KRİTER KAPSAMA + YETERSİZ CEVAP PUANLAMA — regresyon testleri.
# Tamamen JENERİK/sentetik kriter adları ve transkriptlerle — hiçbir aday/pozisyon/kriter'e özel
# hardcode yok. Hiçbir gerçek ağ/API çağrısı yapılmaz (bu iş tamamen deterministik server-side
# sınıflandırma + yuvarlama üzerine kurulu, LLM çağrısı içermez).
#
# Çalıştırma: py test_kriter_kapsama_yetersiz_cevap.py  (backend/ dizininde)

import inspect
import sys
import main as m

FAILURES = []


def check(label, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        FAILURES.append(label)


# ============================================================
# G) %25 TABAN PUAN — ROUND_HALF_UP (iş emri madde 3 örnekleri + .5 sınırı)
# ============================================================
check("G) _insufficient_answer_floor_score(20) == 5", m._insufficient_answer_floor_score(20) == 5)
check("G) _insufficient_answer_floor_score(16) == 4", m._insufficient_answer_floor_score(16) == 4)
check("G) _insufficient_answer_floor_score(12) == 3", m._insufficient_answer_floor_score(12) == 3)
check("G) _insufficient_answer_floor_score(8) == 2", m._insufficient_answer_floor_score(8) == 2)
check("G) _insufficient_answer_floor_score(4) == 1", m._insufficient_answer_floor_score(4) == 1)
check("G) _insufficient_answer_floor_score(10) == 3 (10*0.25=2.5 -> ROUND_HALF_UP -> 3, iş emri örneği)",
      m._insufficient_answer_floor_score(10) == 3)
check("G) _insufficient_answer_floor_score(6) == 2 (1.5 -> 2)", m._insufficient_answer_floor_score(6) == 2)
check("G) _insufficient_answer_floor_score(18) == 5 (4.5 -> 5)", m._insufficient_answer_floor_score(18) == 5)
check("G) banker's rounding ile KONTRAST: round(2.5)==2 ama _insufficient_answer_floor_score(10)==3",
      round(2.5) == 2 and m._insufficient_answer_floor_score(10) == 3)


# ============================================================
# _criterion_ask_status — TEMEL SINIFLANDIRMA (A/B/C/D'nin dayandığı katman)
# ============================================================

def _t(*lines):
    return "\n".join(lines)


TRANSCRIPT_NOT_ASKED = _t(
    "Mülakatçı: Başka bir konu hakkında bahseder misiniz?",
    "Aday: Elbette, şu şekilde...",
)
check("status) hiç sorulmayan kriter -> not_asked",
      m._criterion_ask_status("Test Kriteri Bir", None, TRANSCRIPT_NOT_ASKED) == "not_asked")

TRANSCRIPT_NO_ANSWER = _t(
    "Mülakatçı: Test kriteri hakkında biraz bahseder misiniz?",
    "Aday: Bilmiyorum.",
)
check("status) soruldu + 'Bilmiyorum' -> asked_no_valid_answer",
      m._criterion_ask_status("Test Kriteri Bir", None, TRANSCRIPT_NO_ANSWER) == "asked_no_valid_answer")

TRANSCRIPT_NO_EXPERIENCE = _t(
    "Mülakatçı: Test kriteri konusunda deneyiminizden bahseder misiniz?",
    "Aday: Bu konuda deneyimim yok.",
)
check("status) soruldu + 'bu konuda deneyimim yok' -> asked_no_valid_answer",
      m._criterion_ask_status("Test Kriteri Bir", None, TRANSCRIPT_NO_EXPERIENCE) == "asked_no_valid_answer")

TRANSCRIPT_FOLLOWUP_STILL_NO_ANSWER = _t(
    "Mülakatçı: Test kriteri hakkında bahseder misiniz?",
    "Aday: Bilmiyorum.",
    "Mülakatçı: Peki test kriteri ile ilgili somut bir örnek verebilir misiniz?",
    "Aday: Hatırlamıyorum, geçelim.",
)
check("status) takip sorusu + yine cevapsız -> asked_no_valid_answer",
      m._criterion_ask_status("Test Kriteri Bir", None, TRANSCRIPT_FOLLOWUP_STILL_NO_ANSWER) == "asked_no_valid_answer")

TRANSCRIPT_WEAK_BUT_EVALUABLE = _t(
    "Mülakatçı: Test kriteri hakkında bahseder misiniz?",
    "Aday: Bu konuda biraz tecrübem var, küçük bir projede kullandım ama detayları hatırlamıyorum.",
)
check("status) zayıf ama içerikli cevap -> valid_ask (normal puanlamaya girer)",
      m._criterion_ask_status("Test Kriteri Bir", None, TRANSCRIPT_WEAK_BUT_EVALUABLE) == "valid_ask")

TRANSCRIPT_STRONG_ANSWER = _t(
    "Mülakatçı: Test kriteri hakkında bahseder misiniz?",
    "Aday: Bu süreci baştan sona ben yönettim; önce planladım, sonra ekiple uyguladım, sonunda sonucu ölçtüm.",
)
check("status) güçlü/somut cevap -> valid_ask",
      m._criterion_ask_status("Test Kriteri Bir", None, TRANSCRIPT_STRONG_ANSWER) == "valid_ask")


# ============================================================
# recompute_and_fix_score — POZİSYON tablosu, TAM ENTEGRASYON (A/B/C/D/E/F birlikte)
# ============================================================
POS_CRITERIA = [
    {"name": "Vizyon Gelistirme", "weight": 20},
    {"name": "Butce Kontrolu", "weight": 20},
    {"name": "Paydas Yonetimi", "weight": 20},
    {"name": "Rapor Hazirlama", "weight": 20},
    {"name": "Kriz Cozumleme", "weight": 20},
]

REPORT_BODY = "**Pozisyon Yetkinlikleri:**\n" + "\n".join([
    "| Vizyon Gelistirme | Değerlendirilemedi (sistem) — bu kriter mülakatta sorulmadı |",
    "| Butce Kontrolu | Değerlendirilemedi (sistem) — sorulan turlarda geçerli aday cevabı alınamadı |",
    "| Paydas Yonetimi | Değerlendirilemedi (sistem) — sorulan turlarda geçerli aday cevabı alınamadı |",
    "| Rapor Hazirlama | 6/20 | Aday genel bir cümle kurdu, somut örnek/adım yok. |",
    "| Kriz Cozumleme | 18/20 | Aday üç ayrı somut adımla süreci anlattı. |",
])

TRANSCRIPT_FULL = _t(
    # Vizyon Gelistirme — HİÇ geçmiyor (kasıtlı, hiçbir ortak kelime paylaşmayan kriter adlarıyla)
    "Mülakatçı: Butce kontrolu konusunda bahseder misiniz?",
    "Aday: Bilmiyorum.",
    "Mülakatçı: Paydas yonetimi konusunda tecrübenizden bahseder misiniz?",
    "Aday: Bu konuda deneyimim yok.",
    "Mülakatçı: Rapor hazirlama hakkında bahseder misiniz?",
    "Aday: Genel olarak bu konuda biraz bilgim var.",
    "Mülakatçı: Kriz cozumleme hakkında bahseder misiniz?",
    "Aday: Bu süreci baştan sona ben yönettim; önce planladım, sonra uyguladım, sonunda ölçtüm.",
)

new_body, new_score, warnings = m.recompute_and_fix_score(REPORT_BODY, POS_CRITERIA, model_score=50, transcript=TRANSCRIPT_FULL)

# A) Kriter hiç sorgulanmadı + başka kanıt yok -> Değerlendirilemedi (payda dışı)
check("A) 'Vizyon Gelistirme' -> 'Değerlendirilemedi (sistem)' metni korunuyor",
      "Vizyon Gelistirme | Değerlendirilemedi (sistem)" in new_body)

# B) Soru soruldu + aday cevap vermedi -> Değerlendirilemedi DEĞİL, %25
check("B) 'Butce Kontrolu' artık 'Değerlendirilemedi' DEĞİL",
      "Butce Kontrolu | Değerlendirilemedi" not in new_body)
check("B) 'Butce Kontrolu' TABAN PUAN (5/20, 20*0.25=5) aldı, PAYDADA",
      "Butce Kontrolu | 5/20 — Taban puan" in new_body)

# C) "Bilmiyorum / deneyimim yok" -> %25
check("C) 'Paydas Yonetimi' artık 'Değerlendirilemedi' DEĞİL",
      "Paydas Yonetimi | Değerlendirilemedi" not in new_body)
check("C) 'Paydas Yonetimi' TABAN PUAN (5/20) aldı, PAYDADA",
      "Paydas Yonetimi | 5/20 — Taban puan" in new_body)

# E) Zayıf ama değerlendirilebilir cevap -> otomatik %25'e ZORLANMAZ, model puanı (6/20) KORUNUR
check("E) 'Rapor Hazirlama' modelin verdiği 6/20 AYNEN korundu (taban puana zorlanmadı)",
      "Rapor Hazirlama | 6/20" in new_body)

# F) Güçlü/değerlendirilebilir cevap -> normal scoring (18/20 korunur)
check("F) 'Kriz Cozumleme' modelin verdiği 18/20 AYNEN korundu",
      "Kriz Cozumleme | 18/20" in new_body)

# Toplam matematik doğrulaması: payda = 20(cevapsız-taban) + 20(deneyimsiz-taban) + 20(zayıf) + 20(güçlü) = 80
# (Hic Sorulmadi payda DIŞI — yalnız 4 kriter, 80 puanlık payda)
# awarded = 5 + 5 + 6 + 18 = 34 -> 34/80*100 = 42.5 -> ROUND_HALF_UP -> 43
check("A+B+C+E+F birlikte) Toplam puan doğru hesaplandı: 34/80 (=%42.5) -> ROUND_HALF_UP -> 43", new_score == 43)
check("A+B+C+E+F) '**TOPLAM PUAN: 43/100**' satırı doğru yazıldı", "TOPLAM PUAN: 43/100" in new_body)
check("A+B+C+E+F) ham puan gösterimi '34/80' doğru", "ham puan: 34/80" in new_body)


# ============================================================
# recompute_profile_section — PROFİL tablosu için AYNI üç durum (PUAN 1 ile TUTARLI)
# ============================================================
_p1, _p2, _p3 = m.PROFILE_CRITERIA[0]["name"], m.PROFILE_CRITERIA[1]["name"], m.PROFILE_CRITERIA[2]["name"]
_w1, _w2, _w3 = m.PROFILE_CRITERIA[0]["weight"], m.PROFILE_CRITERIA[1]["weight"], m.PROFILE_CRITERIA[2]["weight"]

PROFILE_REGION = (
    "**Kişisel ve Bilişsel Profil:**\n"
    f"| {_p1} | Değerlendirilemedi (sistem) — sorulan turlarda geçerli aday cevabı alınamadı |\n"
    f"| {_p2} | {round(_w2 * 0.6)}/{_w2} | Aday güçlü bir örnek verdi. |\n"
)

TRANSCRIPT_PROFILE = _t(
    f"Mülakatçı: {_p1} hakkında bahseder misiniz?",
    "Aday: Bilmiyorum, geçelim.",
    f"Mülakatçı: {_p2} hakkında bahseder misiniz?",
    "Aday: Bu konuda net bir örneğim var, şöyle yaptım...",
)

new_profile_body, new_profile_score, prof_warnings = m.recompute_profile_section(
    PROFILE_REGION, transcript=TRANSCRIPT_PROFILE)

check(f"PROFİL) '{_p1}' artık 'Değerlendirilemedi' DEĞİL (soruldu, cevapsız -> taban puan)",
      f"{_p1} | Değerlendirilemedi" not in new_profile_body)
check(f"PROFİL) '{_p1}' TABAN PUAN ({m._insufficient_answer_floor_score(_w1)}/{_w1}) aldı",
      f"{_p1} | {m._insufficient_answer_floor_score(_w1)}/{_w1} — Taban puan" in new_profile_body)
check(f"PROFİL) '{_p2}' modelin verdiği puan AYNEN korundu (normal scoring)",
      f"{_p2} | {round(_w2 * 0.6)}/{_w2}" in new_profile_body)


# ============================================================
# 0-PUAN AÇIK RET DALI — DEĞİŞMEDİ (regresyon — bu iş emrinin KAPSAMI DIŞINDA, dokunulmadı)
# ============================================================
POS_CRITERIA_REFUSAL = [{"name": "Kriter Ret", "weight": 20}]
REPORT_REFUSAL = "**Pozisyon Yetkinlikleri:**\n| Kriter Ret | Değerlendirilemedi (sistem) — aday reddetti |"
TRANSCRIPT_REFUSAL = _t(
    "Mülakatçı: Kriter ret hakkında bahseder misiniz?",
    "Aday: Bu soruyu cevaplamak istemiyorum, tamamen alakasız buluyorum.",
)
new_body_r, new_score_r, _w_r = m.recompute_and_fix_score(REPORT_REFUSAL, POS_CRITERIA_REFUSAL, model_score=0, transcript=TRANSCRIPT_REFUSAL)
check("0-puan açık ret) hâlâ 0/20 (taban puana YÜKSELTİLMEDİ — dar koşul, DEĞİŞMEDİ)",
      "Kriter Ret | 0/20 — Yetersiz (aday)" in new_body_r)
check("0-puan açık ret) 'Değerlendirilemedi' DEĞİL, PAYDADA (score=0)", new_score_r == 0)


# ============================================================
# MODELİN 0 VERDİĞİ AMA SORULMUŞ/CEVAPSIZ OLDUĞU DURUM (awarded==0 dalı, ikinci güvenlik ağı)
# ============================================================
POS_CRITERIA_ZERO = [{"name": "Kriter Sifir Model", "weight": 20}]
REPORT_ZERO = "**Pozisyon Yetkinlikleri:**\n| Kriter Sifir Model | 0/20 | Aday cevap veremedi. |"
TRANSCRIPT_ZERO = _t(
    "Mülakatçı: Kriter sifir model hakkında bahseder misiniz?",
    "Aday: Bilmiyorum.",
)
new_body_z, new_score_z, _w_z = m.recompute_and_fix_score(REPORT_ZERO, POS_CRITERIA_ZERO, model_score=0, transcript=TRANSCRIPT_ZERO)
check("model 0 verdi + sorulmuş/cevapsız) 'Değerlendirilemedi' DEĞİL, TABAN PUAN (5/20)",
      "Kriter Sifir Model | 5/20 — Taban puan" in new_body_z)
check("model 0 verdi + sorulmuş/cevapsız) puan payda içinde sayıldı (score=25=5/20*100)", new_score_z == 25)

POS_CRITERIA_ZERO_NA = [{"name": "Kriter Sifir Sorulmadi", "weight": 20}]
REPORT_ZERO_NA = "**Pozisyon Yetkinlikleri:**\n| Kriter Sifir Sorulmadi | 0/20 | — |"
TRANSCRIPT_ZERO_NA = _t("Mülakatçı: Başka bir şey.", "Aday: Tamam.")
new_body_zna, new_score_zna, _w_zna = m.recompute_and_fix_score(REPORT_ZERO_NA, POS_CRITERIA_ZERO_NA, model_score=0, transcript=TRANSCRIPT_ZERO_NA)
check("model 0 verdi + HİÇ sorulmamış) 'Değerlendirilemedi (sistem)' — PAYDA DIŞI (DEĞİŞMEDİ)",
      "Kriter Sifir Sorulmadi | Değerlendirilemedi (sistem)" in new_body_zna)
check("model 0 verdi + HİÇ sorulmamış) new_score == model_score (0), evaluated_cap<=0 kısa yol", new_score_zna == 0)


# ============================================================
# H) Bir cevap birden fazla kriter için geçerli kanıtsa -> gereksiz tekrar soru zorunluluğu oluşmaz
# I) Özellikle L3'te tüm kriterlerin kapsanması gözetilir
# (canlı-konuşma davranışı — statik prompt kaynağı taraması ile doğrulanır)
# ============================================================
src_voice = inspect.getsource(m.build_l2_realtime_instructions)
src_text = inspect.getsource(m.get_system_prompt)
check("H) Voice (L2/L3) prompt: 'birden fazla kriter' / gereksiz tekrar sorma açıkça belirtiliyor",
      "birden fazla kriter" in src_voice and "gereksiz tekrar soru" in src_voice)
check("H) Text (L1) prompt: 'birden fazla kriter' / gereksiz tekrar sorma açıkça belirtiliyor",
      "birden fazla kriter" in src_text and "gereksiz tekrar soru" in src_text)
check("I) Voice prompt hâlâ 'end_interview çağırmadan ÖNCE' kriter kapsama kontrolü içeriyor",
      "end_interview çağırmadan ÖNCE" in src_voice and "hiç dokunulmamış" in src_voice)
check("I) Text prompt hâlâ 'mülakatı BİTİRMEDEN ÖNCE' kriter kapsama kontrolü içeriyor",
      "BİTİRMEDEN ÖNCE" in src_text and "hiç dokunulmamış" in src_text)
check("I) Voice prompt: kriter kapsama artık 'opsiyonel değil, asıl işlev' diye vurgulanıyor",
      "asıl işlevidir" in src_voice)
check("I) Text prompt: kriter kapsama artık 'opsiyonel değil, asıl işlev' diye vurgulanıyor",
      "asıl işlevidir" in src_text)

# INTERVIEWER_REASK_RULES artık 2. denemeden sonra "değerlendirilemedi" DEMİYOR (iş emri madde 2 ile çelişki DÜZELTİLDİ)
check("Canlı konuşma kuralı: 2. deneme sonrası artık \"değerlendirilemedi\" DENMİYOR",
      '"değerlendirilemedi (soruldu, cevap alınamadı)"' not in m.INTERVIEWER_REASK_RULES)
check("Canlı konuşma kuralı: 'sorulmuş ama yeterli cevap alınamamış' + taban puan diline geçildi",
      "sorulmuş ama yeterli cevap alınamamış" in m.INTERVIEWER_REASK_RULES and "taban puan" in m.INTERVIEWER_REASK_RULES)

# CRITERION_SCORING_RULE (rapor üretim prompt'u, hem L1 hem L2/L3 tarafından paylaşılır) üç durumu içeriyor
check("CRITERION_SCORING_RULE: 'Yetersiz Cevap (taban puan)' talimatı eklendi",
      "Yetersiz Cevap (taban puan)" in m.CRITERION_SCORING_RULE)
check("CRITERION_SCORING_RULE: '%25' oranı açıkça belirtiliyor", "%25" in m.CRITERION_SCORING_RULE)
check("CRITERION_SCORING_RULE: 'bilmiyorum'/'deneyimim yok' örnekleri DEĞERLENDİRİLEMEDİ'den AYRILDI",
      "'bilmiyorum'" in m.CRITERION_SCORING_RULE and "deneyim" in m.CRITERION_SCORING_RULE)


# ============================================================
# J) L1/L2/L3 PROVIDER MİMARİSİ BOZULMADI (regresyon)
# ============================================================
check("J) start_interview hâlâ L2/L3'ü reddediyor", "level in (2, 3)" in inspect.getsource(m.start_interview))
check("J) interview_chat hâlâ L2/L3'ü reddediyor", "level in (2, 3)" in inspect.getsource(m.interview_chat))
check("J) start_interview hâlâ OpenAI kullanıyor (L1 OpenAI-only)", "OPENAI_L1_INTERVIEW_MODEL" in inspect.getsource(m.start_interview))
check("J) append_reviewer_section hâlâ yalnız L3'e kilitli", "if level != 3:" in inspect.getsource(m.append_reviewer_section))
check("J) run_final_report_quality_gate hâlâ yalnız L3'e kilitli", "if level != 3:" in inspect.getsource(m.run_final_report_quality_gate))
check("J) compute_genel_puan hâlâ TEK canonical yuvarlamadan geçiyor (ROUND_HALF_UP)", "_round_half_up" in inspect.getsource(m.compute_genel_puan))


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("Tüm İŞ EMRİ — KRİTER KAPSAMA + YETERSİZ CEVAP PUANLAMA testleri GEÇTİ.")

if FAILURES:
    sys.exit(1)

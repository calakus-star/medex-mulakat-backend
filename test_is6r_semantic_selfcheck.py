# İŞ 6R — KRİTER TANIMI + SEMANTİK KANIT ÖZ-DENETİMİ — unit/regression testleri.
# Tamamen JENERİK metinlerle — hiçbir aday/pozisyon/kriter/candidate_id'ye özel hardcode yok.
# Hiçbir gerçek ağ/API çağrısı yapılmaz (openai_call/record_openai_chat_usage monkey-patch edilir).
#
# Çalıştırma: py test_is6r_semantic_selfcheck.py  (backend/ dizininde)

import io
import sys
import json
import contextlib
import main as m

FAILURES = []


def check(label, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        FAILURES.append(label)


TEST_POSITION_NAME = "İŞ6R_TEST_POZİSYONU_GEÇİCİ"
TEST_CRITERIA = [
    {"name": "Test Kriteri Bir", "weight": 25, "desc": "adayın X konusunda somut örnek verme becerisi"},
    {"name": "Test Kriteri İki", "weight": 20, "desc": "adayın Y sürecini uçtan uca yönetme deneyimi"},
]

TRANSCRIPT_VIEW = [
    {"role": "mulakatci", "text": "Bu konudaki deneyiminizi anlatır mısınız?", "elapsed_ms": 125000, "ts": "2:05"},
    {"role": "aday", "text": "Haftalık olarak düzenli rapor hazırlıyorum.", "elapsed_ms": 130000, "ts": "2:10"},
]

# ============================================================
# A) Primary prompt: kriter name + desc içeriyor VE semantik öz-denetim talimatını içeriyor
# ============================================================
db = m.get_db()
try:
    db.execute("DELETE FROM positions WHERE name=?", (TEST_POSITION_NAME,))
    db.execute(
        "INSERT INTO positions (name, category, role_description, criteria_json, active) VALUES (?, ?, ?, ?, 1)",
        (TEST_POSITION_NAME, "Genel", "Test rol açıklaması", json.dumps(TEST_CRITERIA, ensure_ascii=False)))
    db.commit()
finally:
    db.close()

try:
    sys_prompt = m.get_system_prompt(TEST_POSITION_NAME, "Test Aday")
    check("A) primary prompt kriter ADINI içeriyor", "Test Kriteri Bir" in sys_prompt)
    check("A) primary prompt kriter TANIMINI (desc) içeriyor", "adayın X konusunda somut örnek verme becerisi" in sys_prompt)
    check("A) primary prompt semantik öz-denetim talimatını içeriyor",
          "SEMANTİK ÖZ-DENETİM" in sys_prompt and "başka bir yetkinliği" in sys_prompt)
    check("A) primary prompt 'GERÇEKTEN BU kriterin tanımını mı destekliyor' sorusunu içeriyor",
          "tanımını mı destekliyor" in sys_prompt)
finally:
    db = m.get_db()
    try:
        db.execute("DELETE FROM positions WHERE name=?", (TEST_POSITION_NAME,))
        db.commit()
    finally:
        db.close()


# ============================================================
# B/C/D/E) regenerate_criterion_fields — retry prompt içeriği (crit_desc dahil / hariç)
# ============================================================
def make_openai_resp(content):
    class FakeResp:
        def json(self_inner):
            return {"choices": [{"message": {"content": content}, "finish_reason": "stop"},],
                    "usage": {"completion_tokens": 40, "prompt_tokens": 500}}
    return FakeResp()


def run_regenerate(prior_fields, violations, mock_reply_body, crit_desc, candidate_id=9101):
    captured = {}

    def fake_openai_call(*args, **kwargs):
        captured["prompt"] = kwargs.get("json_body", {}).get("messages", [{}])[-1].get("content", "")
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
                candidate_id, 1, "openai", "gpt-4o", "Test Kriteri Bir", 25,
                "transkript metni", prior_fields, violations,
                transcript_view=TRANSCRIPT_VIEW, accepted_claims=[], crit_desc=crit_desc)
    finally:
        m.openai_call = orig_call
        m.record_openai_chat_usage = orig_record
        m.OPENAI_API_KEY = orig_key
    return result, captured.get("prompt", "")


PRIOR_BAD_K = "[99:99] geçersiz damga"
PRIOR_GOOD_K = "[2:10] Haftalık olarak düzenli rapor hazırladığını söyledi"
MODEL_REPLY = f"G: Rapor sürecini anlattı\nK: {PRIOR_GOOD_K}\nE: \nS: "

PRIOR = {"g": "Rapor sürecini anlattı", "k": PRIOR_BAD_K, "e": "", "s": ""}

# B) desc verildiğinde prompt'ta name + cap + DESC var
_, prompt_with_desc = run_regenerate(PRIOR, ["evidence_timestamp_invalid"], MODEL_REPLY,
                                     crit_desc="adayın X konusunda somut örnek verme becerisi")
check("B) retry prompt kriter ADINI içeriyor", "Test Kriteri Bir" in prompt_with_desc)
check("B) retry prompt tavanı (cap) içeriyor", "25 puan" in prompt_with_desc)
check("B) retry prompt KRİTER TANIMINI (desc) içeriyor", "adayın X konusunda somut örnek verme becerisi" in prompt_with_desc)

# C) semantik öz-denetim talimatı retry prompt'unda var
check("C) retry prompt semantik öz-denetim talimatını içeriyor",
      "SEMANTİK ÖZ-DENETİM" in prompt_with_desc and "başka bir yetkinliği" in prompt_with_desc)
check("C) retry prompt 'tanımını mı destekliyor' sorusunu içeriyor", "tanımını mı destekliyor" in prompt_with_desc)

# D) İş 6P alan izolasyonu evidence_timestamp_invalid retry'da AYNEN çalışıyor
result_d, _ = run_regenerate(PRIOR, ["evidence_timestamp_invalid"], MODEL_REPLY, crit_desc="ilgisiz bir tanım")
check("D) sonuç None değil", result_d is not None)
check("D) G DEĞİŞMEDİ (İş 6P alan izolasyonu korunuyor)", result_d["g"] == PRIOR["g"])
check("D) K DEĞİŞTİ (izin verilen tek alan)", result_d["k"] == PRIOR_GOOD_K)
violations_after_d = m.validate_criterion_fields(result_d, 25, 15, TRANSCRIPT_VIEW, [])
check("D) K düzeldi, validator PASS (validator DEĞİŞMEDİ)", violations_after_d == [])

# E) desc=None / desc="" -> crash YOK, prompt eski (İş 6R öncesi) haliyle devam ediyor
result_e_none, prompt_none = run_regenerate(PRIOR, ["evidence_timestamp_invalid"], MODEL_REPLY, crit_desc=None)
check("E-None) sonuç None değil (crash yok)", result_e_none is not None)
check("E-None) prompt kriter ADINI içeriyor", "Test Kriteri Bir" in prompt_none)
check("E-None) prompt 'KRİTER TANIMI:' satırı YOK (desc verilmedi)", "KRİTER TANIMI:" not in prompt_none)
check("E-None) semantik öz-denetim talimatı YİNE DE var (desc'e bağımlı değil)",
      "SEMANTİK ÖZ-DENETİM" in prompt_none)

result_e_empty, prompt_empty = run_regenerate(PRIOR, ["evidence_timestamp_invalid"], MODEL_REPLY, crit_desc="")
check("E-boş) sonuç None değil (crash yok)", result_e_empty is not None)
check("E-boş) prompt 'KRİTER TANIMI:' satırı YOK (desc boş string)", "KRİTER TANIMI:" not in prompt_empty)

# E-3) crit_desc parametresi hiç verilmezse (varsayılan None) de aynı şekilde çalışır
def run_regenerate_no_desc_kw(prior_fields, violations, mock_reply_body, candidate_id=9102):
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
                candidate_id, 1, "openai", "gpt-4o", "Test Kriteri Bir", 25,
                "transkript metni", prior_fields, violations,
                transcript_view=TRANSCRIPT_VIEW, accepted_claims=[])
    finally:
        m.openai_call = orig_call
        m.record_openai_chat_usage = orig_record
        m.OPENAI_API_KEY = orig_key
    return result
result_no_kw = run_regenerate_no_desc_kw(PRIOR, ["evidence_timestamp_invalid"], MODEL_REPLY)
check("E-varsayılan) crit_desc parametresi hiç verilmeden çağrı BAŞARILI (geriye uyumlu)", result_no_kw is not None)


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("Tüm İŞ 6R testleri GEÇTİ.")

if FAILURES:
    sys.exit(1)

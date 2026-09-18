# İŞ 6T — SEMANTİK YÖN KORUMA + REVIEWER SEMANTİK DENETİM — unit/regression testleri.
# Tamamen JENERİK metinlerle — hiçbir aday/pozisyon/kriter/candidate_id'ye özel hardcode yok.
# Hiçbir gerçek ağ/API çağrısı yapılmaz (openai_call / run_report_reviewer monkey-patch edilir).
#
# Çalıştırma: py test_is6t_semantic_direction_reviewer.py  (backend/ dizininde)

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


TEST_POSITION_NAME = "İŞ6T_TEST_POZİSYONU_GEÇİCİ"
TEST_CRITERIA = [
    {"name": "Test Kriteri Bir", "weight": 25, "desc": "adayın X konusunda somut örnek verme becerisi"},
]

TRANSCRIPT_VIEW = [
    {"role": "mulakatci", "text": "Bu konudaki deneyiminizi anlatır mısınız?", "elapsed_ms": 125000, "ts": "2:05"},
    {"role": "aday", "text": "Haftalık olarak düzenli rapor hazırlıyorum.", "elapsed_ms": 130000, "ts": "2:10"},
]

# ============================================================
# 1) Primary prompt: semantik ilgi + semantik YÖN talimatını içeriyor
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
    check("1) primary prompt semantik İLGİ öz-denetimini içeriyor (İş 6R, KORUNDU)",
          "SEMANTİK ÖZ-DENETİM" in sys_prompt)
    check("1) primary prompt YÖN KONTROLÜ talimatını içeriyor",
          "YÖN KONTROLÜ" in sys_prompt and "olumlu bir yetkinlik kanıtı mı" in sys_prompt)
    check("1) primary prompt 'olumlu bir yetkinlik cümlesine DÖNÜŞTÜRME' uyarısını içeriyor",
          "DÖNÜŞTÜRME" in sys_prompt)
    check("1) primary prompt mesleki/regülasyonel dış doğruluk YAPMA sınırını içeriyor",
          "dış bilgiyle hüküm VERME" in sys_prompt)
finally:
    db = m.get_db()
    try:
        db.execute("DELETE FROM positions WHERE name=?", (TEST_POSITION_NAME,))
        db.commit()
    finally:
        db.close()


# ============================================================
# 2) Retry prompt: desc + ilgi öz-denetimi + YÖN talimatını içeriyor
# ============================================================
def make_openai_resp(content):
    class FakeResp:
        def json(self_inner):
            return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                    "usage": {"completion_tokens": 40, "prompt_tokens": 500}}
    return FakeResp()


def run_regenerate(prior_fields, violations, mock_reply_body, crit_desc, candidate_id=9201):
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

_, prompt_with_desc = run_regenerate(PRIOR, ["evidence_timestamp_invalid"], MODEL_REPLY,
                                     crit_desc="adayın X konusunda somut örnek verme becerisi")
check("2) retry prompt KRİTER TANIMINI (desc) içeriyor", "adayın X konusunda somut örnek verme becerisi" in prompt_with_desc)
check("2) retry prompt semantik İLGİ öz-denetimini içeriyor (İş 6R, KORUNDU)", "SEMANTİK ÖZ-DENETİM" in prompt_with_desc)
check("2) retry prompt YÖN KONTROLÜ talimatını içeriyor",
      "YÖN KONTROLÜ" in prompt_with_desc and "olumlu bir yetkinlik kanıtı mı" in prompt_with_desc)
check("2) retry prompt 'olumlu bir yetkinlik cümlesine DÖNÜŞTÜRME' uyarısını içeriyor", "DÖNÜŞTÜRME" in prompt_with_desc)


# ============================================================
# 3) Reviewer criteria block: name + weight + desc içeriyor
# ============================================================
_pos_criteria_with_desc = [{"name": "Test Kriteri Bir", "weight": 25, "desc": "adayın X konusunda somut örnek verme becerisi"}]
block3 = m._reviewer_criteria_block(_pos_criteria_with_desc)
check("3) reviewer criteria block kriter ADINI içeriyor", "Test Kriteri Bir" in block3)
check("3) reviewer criteria block tavanı (weight) içeriyor", "__/25" in block3)
check("3) reviewer criteria block TANIMI (desc) içeriyor", "adayın X konusunda somut örnek verme becerisi" in block3)
check("3) reviewer criteria block PROFİL kriterlerinin de tanımını içeriyor (PROFILE_CRITERIA sabit)",
      any((pc.get("desc") or "") in block3 for pc in m.PROFILE_CRITERIA if pc.get("desc")))

# ============================================================
# 4) Desc None/boş -> crash yok
# ============================================================
_pos_criteria_no_desc = [{"name": "Test Kriteri İki", "weight": 20}]  # desc anahtarı hiç yok
block4a = m._reviewer_criteria_block(_pos_criteria_no_desc)
check("4a) desc anahtarı hiç yoksa crash yok, ad+tavan var", "Test Kriteri İki" in block4a and "__/20" in block4a)
check("4a) desc yoksa '— tanım:' eki YOK", "— tanım:" not in block4a.split("Test Kriteri İki")[1].split("\n")[0])

_pos_criteria_empty_desc = [{"name": "Test Kriteri Üç", "weight": 10, "desc": ""}]
block4b = m._reviewer_criteria_block(_pos_criteria_empty_desc)
check("4b) desc boş string -> crash yok, ad+tavan var", "Test Kriteri Üç" in block4b and "__/10" in block4b)

result_desc_none, _ = run_regenerate(PRIOR, ["evidence_timestamp_invalid"], MODEL_REPLY, crit_desc=None)
check("4c) retry crit_desc=None -> crash yok, sonuç üretildi", result_desc_none is not None)


# ============================================================
# 5) Reviewer prompt üç kontrolü (relevance / claim-evidence / direction) + format içeriyor
# ============================================================
reviewer_prompt_src = None
import inspect
reviewer_prompt_src = inspect.getsource(m.run_report_reviewer)
check("5) reviewer prompt kaynağı 'SEMANTİK TUTARLILIK' bölümünü içeriyor", "SEMANTİK TUTARLILIK" in reviewer_prompt_src)
check("5) reviewer prompt kriter-kanıt İLGİSİ kontrolünü içeriyor", "kriterin TANIMIYLA ilgili mi" in reviewer_prompt_src)
check("5) reviewer prompt iddia↔kanıt tutarlılığı kontrolünü içeriyor", "GERÇEKTEN verilen kanıttan" in reviewer_prompt_src)
check("5) reviewer prompt olumlu/olumsuz YÖN tutarlılığı kontrolünü içeriyor", "olumlu/olumsuz yönü raporda KORUNMUŞ" in reviewer_prompt_src)
check("5) reviewer prompt SEMANTIC_ISSUE format örneğini içeriyor", "SEMANTIC_ISSUE:" in reviewer_prompt_src)

# ============================================================
# 6) PASS kriterlerin tek tek yazılmasının İSTENMEDİĞİNİ doğrula
# ============================================================
check("6) reviewer prompt PASS kriterleri TEK TEK YAZMA talimatını içeriyor",
      "PASS olan" in reviewer_prompt_src and "TEK TEK YAZMA" in reviewer_prompt_src)
check("6) reviewer prompt yalnız SORUN olan kriterler için satır istiyor",
      "YALNIZ belirgin bir semantik sorun gördüğün kriterler" in reviewer_prompt_src)


# ============================================================
# 7/8/9) append_reviewer_section entegrasyonu: semantic issue G/K/E/S, puan, karar üzerinde
# MUTASYON yapmıyor; mevcut reviewer-score/diff/takeover mekanizması AYNEN çalışıyor.
# ============================================================
TEST_CID = 9202
LEVEL = 1
POS_CRITERIA_LIVE = [{"name": "Test Kriteri Bir", "weight": 25, "desc": "adayın X konusunda somut örnek verme becerisi"}]

POS_ROW = "| Test Kriteri Bir | 15/25 | G: Rapor sürecini uçtan uca anlattı ~~ K: [2:10] \"haftalık olarak düzenli rapor hazırlıyorum\" ~~ E: ~~ S: |"


def _prof_rows():
    rows = []
    for c in m.PROFILE_CRITERIA:
        awarded = int(c["weight"] * 0.6)
        rows.append(f"| {c['name']} | {awarded}/{c['weight']} | G: Gözlemlenen davranışı anlattı ~~ K: [2:10] \"haftalık olarak düzenli rapor hazırlıyorum\" ~~ E: ~~ S: |")
    return "\n".join(rows)


REPORT_TEMPLATE = f"""**Yönetici Özeti:**
Aday hakkında kısa bir özet.

**Pozisyon Yetkinlikleri:**
{POS_ROW}

**Kişisel ve Bilişsel Profil:**
{_prof_rows()}

**Puanlama Kapsamı:**
Pozisyon: 1/1 değerlendirildi. Profil: 6/6 değerlendirildi.

**Değerlendirilemeyen Alanlar:**
Yok.

**Güçlü Yönler:**
Aday düzenli raporlama alışkanlığını somut örneklerle anlattı [2:10].

**Gelişim Alanları:**
Belirgin bir gelişim alanı gözlenmedi.
""" + m._REVIEWER_SLOT_MARK

STARTED_AT = "2026-01-01T10:00:00"
MESSAGES = json.dumps([
    {"role": "assistant", "content": "Bu konudaki deneyiminizi anlatır mısınız?", "ts": "2026-01-01T10:02:05"},
    {"role": "user", "content": "Haftalık olarak düzenli rapor hazırlıyorum.", "ts": "2026-01-01T10:02:10"},
])


def seed():
    db = m.get_db()
    try:
        db.execute("DELETE FROM interviews WHERE candidate_id=? AND level=?", (TEST_CID, LEVEL))
        db.execute(
            "INSERT INTO interviews (candidate_id, level, messages, report, started_at, "
            "pending_finish_provider, pending_finish_model, score_position, score_profile, score, recommendation) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (TEST_CID, LEVEL, MESSAGES, REPORT_TEMPLATE, STARTED_AT, "claude", "claude-sonnet-4-6",
             60.0, 55.0, 58.0, "Değerlendir"))
        db.commit()
    finally:
        db.close()


def read_state():
    db = m.get_db()
    try:
        row = db.execute(
            "SELECT report, score, recommendation, score_position, score_profile FROM interviews "
            "WHERE candidate_id=? AND level=?", (TEST_CID, LEVEL)).fetchone()
    finally:
        db.close()
    return dict(row) if row else None


REVIEWER_RAW_NO_SEMANTIC = """GÖRÜŞ YOK

=== ADAY ÖZGÜVENİ İZLENİMİ ===
YETERSİZ VERİ

=== KRİTER PUANLARI ===
GUVEN_DUZEYI: yüksek
"""

REVIEWER_RAW_WITH_SEMANTIC = """GÖRÜŞ YOK

=== ADAY ÖZGÜVENİ İZLENİMİ ===
YETERSİZ VERİ

=== KRİTER PUANLARI ===
SEMANTIC_ISSUE: P1 = Kanıtın yönü tersine çevrilmiş görünüyor (test amaçlı sentetik neden)
GUVEN_DUZEYI: yüksek
"""


def run_append_with_mock(raw_text):
    seed()

    def fake_run_report_reviewer(*args, **kwargs):
        return raw_text, "ok", ""

    orig = m.run_report_reviewer
    m.run_report_reviewer = fake_run_report_reviewer
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            m.append_reviewer_section(TEST_CID, LEVEL, "transkript metni", "Yok", position_criteria=POS_CRITERIA_LIVE)
    finally:
        m.run_report_reviewer = orig
    return read_state()


try:
    state_no_semantic = run_append_with_mock(REVIEWER_RAW_NO_SEMANTIC)
    state_with_semantic = run_append_with_mock(REVIEWER_RAW_WITH_SEMANTIC)

    # 7) Semantic issue G/K/E/S / evaluability / primary score / Genel Puan / recommendation'ı DEĞİŞTİRMEDİ
    check("7) Pozisyon Yetkinlikleri satırı (G/K/E/S) İKİ SENARYODA DA BİREBİR AYNI",
          POS_ROW in state_no_semantic["report"] and POS_ROW in state_with_semantic["report"])
    check("7) 'Değerlendirilemedi (sistem)' hiçbir senaryoda İÇERİ SIZMADI (evaluability değişmedi)",
          "Değerlendirilemedi (sistem)" not in state_no_semantic["report"]
          and "Değerlendirilemedi (sistem)" not in state_with_semantic["report"])
    check("7) score (Genel Puan) İKİ SENARYODA DA AYNI", state_no_semantic["score"] == state_with_semantic["score"])
    check("7) recommendation İKİ SENARYODA DA AYNI", state_no_semantic["recommendation"] == state_with_semantic["recommendation"])
    check("7) score_position İKİ SENARYODA DA AYNI (devralma/takeover tetiklenmedi)",
          state_no_semantic["score_position"] == state_with_semantic["score_position"])
    check("7) score_profile İKİ SENARYODA DA AYNI", state_no_semantic["score_profile"] == state_with_semantic["score_profile"])

    # 8) Semantic-only senaryoda bölüm YİNE DE rapora eklendi (yeni 'not semantic_block' kapısı çalışıyor)
    check("8) semantic-only senaryoda 'İkinci Değerlendirici Görüşü' bölümü RAPORA EKLENDİ",
          m._REVIEWER_HEAD in state_with_semantic["report"])
    check("8) semantic not raporda GÖRÜNÜYOR (Ek Görüş altında, görüntüleme amaçlı)",
          "Kanıtın yönü tersine çevrilmiş görünüyor" in state_with_semantic["report"])
    check("8) semantic not kriter ADIYLA eşleştirilmiş (Test Kriteri Bir)",
          "Test Kriteri Bir" in state_with_semantic["report"].split(m._REVIEWER_HEAD)[-1])
    # hiçbir görüş/semantik not yoksa bölüm YİNE eklenmez (eski davranış korunuyor)
    check("8) HİÇ görüş/semantik not olmayan senaryoda bölüm hâlâ EKLENMİYOR (regresyon)",
          m._REVIEWER_HEAD not in state_no_semantic["report"])
finally:
    db = m.get_db()
    try:
        db.execute("DELETE FROM interviews WHERE candidate_id=? AND level=?", (TEST_CID, LEVEL))
        db.commit()
    finally:
        db.close()


# ============================================================
# 8b) Mevcut reviewer-score mekanizması (KRITER_PUAN farkı) AYNEN çalışıyor — semantic bloktan
# BAĞIMSIZ. Aynı anda hem KRITER_PUAN farkı hem SEMANTIC_ISSUE varsa ikisi de doğru işlenmeli.
# ============================================================
REVIEWER_RAW_SCORE_DIFF_PLUS_SEMANTIC = """GÖRÜŞ YOK

=== ADAY ÖZGÜVENİ İZLENİMİ ===
YETERSİZ VERİ

=== KRİTER PUANLARI ===
KRITER_PUAN: P1 = 22/25
KRITER_GEREKCE: P1 = Kanıt daha güçlü değerlendirildi [2:10].
SEMANTIC_ISSUE: P1 = Kanıtın yönü tersine çevrilmiş görünüyor (test amaçlı sentetik neden)
GUVEN_DUZEYI: yüksek
"""

try:
    state_diff = run_append_with_mock(REVIEWER_RAW_SCORE_DIFF_PLUS_SEMANTIC)
    check("8b) reviewer puan FARKI (diff_block) rapora yansıdı (mevcut mekanizma bozulmadı)",
          "22/25" in state_diff["report"] and "(birincil: 15/25)" in state_diff["report"])
    check("8b) semantic not da AYNI ANDA rapora yansıdı", "Kanıtın yönü tersine çevrilmiş görünüyor" in state_diff["report"])
    check("8b) Pozisyon Yetkinlikleri TABLOSUNDAKİ G/K/E/S hücresi semantic_issue'dan DEĞİL yalnız KRITER_PUAN/devralma mekanizmasından etkilendi (satır DEĞİŞMEDİ, çünkü 15/25 zaten payda dışı değil — takeover yalnız disqualified kriterlerde çalışır)",
          POS_ROW in state_diff["report"])
finally:
    db = m.get_db()
    try:
        db.execute("DELETE FROM interviews WHERE candidate_id=? AND level=?", (TEST_CID, LEVEL))
        db.commit()
    finally:
        db.close()


# ============================================================
# 9) İş 6P alan izolasyonu AYNEN çalışıyor (regresyon, İş 6T'nin retry prompt değişikliği
# _enforce_field_isolation'ı ETKİLEMEMELİ)
# ============================================================
result_9, _ = run_regenerate(PRIOR, ["evidence_timestamp_invalid"], MODEL_REPLY,
                             crit_desc="adayın X konusunda somut örnek verme becerisi")
check("9) İş 6P alan izolasyonu: G DEĞİŞMEDİ", result_9["g"] == PRIOR["g"])
check("9) İş 6P alan izolasyonu: K DEĞİŞTİ (izin verilen tek alan)", result_9["k"] == PRIOR_GOOD_K)
violations_9 = m.validate_criterion_fields(result_9, 25, 15, TRANSCRIPT_VIEW, [])
check("9) K düzeldi, validator PASS (validator DEĞİŞMEDİ)", violations_9 == [])


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("Tüm İŞ 6T testleri GEÇTİ.")

if FAILURES:
    sys.exit(1)

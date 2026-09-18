# İŞ 6V-FIX — FINAL POSITION/PROFILE SCORE CONSISTENCY — unit/regression testleri.
# Tamamen JENERİK metinlerle — hiçbir aday/pozisyon/kriter/candidate_id'ye özel hardcode yok.
# Hiçbir gerçek ağ/API çağrısı yapılmaz (run_report_reviewer monkey-patch edilir).
#
# Çalıştırma: py test_is6v_final_score_consistency.py  (backend/ dizininde)

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


# ============================================================
# Birim testleri: _final_component_score — 5 eksik-reviewer senaryosu
# ============================================================

# 1) Reviewer position + profile ikisi de var -> ortalama (İş 6V regresyon örneği)
check("1) final_position = mean(64,60) = 62", m._final_component_score(64, 60) == 62)
# İŞ EMRİ — FINAL EVALUATION ARCHITECTURE / madde 5 (Section 14): rounding artık ROUND_HALF_UP
# (round-half-to-even DEĞİL) — mean(66,63)=64.5 artık 65'e yuvarlanıyor (64'e DEĞİL).
check("1) final_profile = mean(66,63) = 65 (ROUND_HALF_UP, compute_genel_puan ile AYNI standart)",
      m._final_component_score(66, 63) == 65)

# 2) Yalnız reviewer position var (profile yok) -> profile'da primary fallback
check("2) reviewer profile YOK -> primary fallback (66)", m._final_component_score(66, None) == 66)

# 3) Yalnız reviewer profile var (position yok) -> position'da primary fallback
check("3) reviewer position YOK -> primary fallback (64)", m._final_component_score(64, None) == 64)

# 4) Reviewer skorları hiç yok -> ikisi de primary fallback
check("4) reviewer position YOK -> primary (64)", m._final_component_score(64, None) == 64)
check("4) reviewer profile YOK -> primary (66)", m._final_component_score(66, None) == 66)

# 5) None/parse edilemeyen reviewer değeri (None) -> primary fallback, crash yok
check("5) primary=None, reviewer=60 -> yalnız reviewer (60)", m._final_component_score(None, 60) == 60)
check("5) primary=None, reviewer=None -> None (crash yok)", m._final_component_score(None, None) is None)

# ============================================================
# compute_genel_puan DEĞİŞMEDİ (regresyon — aynı formül, aynı sonuç)
# ============================================================
check("compute_genel_puan(64,66,60,63) hâlâ 63 (formül DEĞİŞMEDİ)",
      m.compute_genel_puan(64, 66, 60, 63) == 63)


# ============================================================
# Uçtan uca: append_reviewer_section — İş 6V regresyon örneği (64/66 primary, 60/63 reviewer)
# İŞ EMRİ — FINAL EVALUATION ARCHITECTURE (Section 14): reviewer artık YALNIZ L3'te çalışıyor
# (append_reviewer_section'ın 'if level != 3: return {}' savunma kapısı) — bu test ESKİDEN
# LEVEL=1 idi (o zamanki mimaride reviewer TÜM level'larda çalışıyordu), LEVEL=3'e güncellendi.
# Test edilen ASIL mekanizma (final score persistence) DEĞİŞMEDİ.
# ============================================================
TEST_CID = 9301
LEVEL = 3
POS_CRITERIA_LIVE = [{"name": "Test Kriteri Bir", "weight": 100, "desc": "adayın X konusunda somut örnek verme becerisi"}]
POS_ROW = "| Test Kriteri Bir | 64/100 | G: Süreci uçtan uca anlattı ~~ K: [2:10] \"haftalık olarak düzenli rapor hazırlıyorum\" ~~ E: ~~ S: |"


def _prof_rows():
    rows = []
    remaining = 66
    n = len(m.PROFILE_CRITERIA)
    for i, c in enumerate(m.PROFILE_CRITERIA):
        awarded = min(c["weight"], remaining) if i == n - 1 else int(c["weight"] * 0.66)
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

**Öneri:**
Değerlendir

**Öneri Gerekçesi:**
Adayın Genel Puanı (65/100), doğrudan işe alım veya ret için yeterli olmayan, değerlendirmeye açık bir aralıktadır (40-79). Pozisyon yetkinlikleri puanı 64/100. Kişisel ve bilişsel profil puanı 66/100.
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
             64.0, 66.0, 65.0, "Değerlendir"))
        db.commit()
    finally:
        db.close()


def read_state():
    db = m.get_db()
    try:
        row = db.execute(
            "SELECT report, score, recommendation, score_position, score_profile, "
            "reviewer_score_position, reviewer_score_profile FROM interviews "
            "WHERE candidate_id=? AND level=?", (TEST_CID, LEVEL)).fetchone()
    finally:
        db.close()
    return dict(row) if row else None


# Reviewer: P1 = 60/100 (position), tüm profil kriterlerinde reviewer AYNI puanı verir (63/100 toplam
# hedefiyle orantılı) — build_reviewer_diff_block sadece FARKLI puanları algılar, biz reviewer_score_
# position/profile'ı DOĞRUDAN compute_reviewer_overall üzerinden simüle etmek için gerçek KRITER_PUAN
# satırları kullanıyoruz (profil kriterlerinin TAMAMINA reviewer puanı veriyoruz ki toplam 63/100 olsun).
# İŞ EMRİ — FINAL EVALUATION ARCHITECTURE (Section 14): KRITER_GEREKCE'lerden KASITLI olarak
# [mm:ss] damgası ÇIKARILDI — bu test yalnız General/Nihai BLEND+PERSIST mantığını izole eder;
# apply_reviewer_criterion_correction'ın (İş emri madde 2, grounding gerektiren) kriter-tablosu
# düzeltmesini TETİKLEMEMESİ gerekir (o mekanizma test_final_evaluation_architecture.py'de AYRI
# test ediliyor). Damgalı bir gerekçe burada primary tabloyu da değiştirir, bu testin "primary
# tablo/score_position DEĞİŞMEDİ" iddiasını KASITSIZCA bozardı.
def _reviewer_kriter_puan_lines():
    lines = ["KRITER_PUAN: P1 = 60/100", "KRITER_GEREKCE: P1 = Kanıt sınırlı bulundu, somut zaman referansı olmadan genel bir gözlem."]
    total_target = 63
    n = len(m.PROFILE_CRITERIA)
    running = 0
    for i, c in enumerate(m.PROFILE_CRITERIA):
        cid = f"K{i+1}"
        if i == n - 1:
            awarded = max(0, total_target - running)
        else:
            awarded = round(c["weight"] * (total_target / 100))
            running += awarded
        lines.append(f"KRITER_PUAN: {cid} = {awarded}/{c['weight']}")
        lines.append(f"KRITER_GEREKCE: {cid} = Gözlem farklı değerlendirildi, somut zaman referansı olmadan genel bir gözlem.")
    return "\n".join(lines)


REVIEWER_RAW = f"""GÖRÜŞ YOK

=== ADAY ÖZGÜVENİ İZLENİMİ ===
YETERSİZ VERİ

=== KRİTER PUANLARI ===
{_reviewer_kriter_puan_lines()}
GUVEN_DUZEYI: yüksek
"""


def run_append():
    seed()

    def fake_run_report_reviewer(*args, **kwargs):
        return REVIEWER_RAW, "ok", ""

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
    before_state = {"report": REPORT_TEMPLATE, "score": 65.0, "score_position": 64.0, "score_profile": 66.0}
    after_state = run_append()

    check("BEFORE) Öneri Gerekçesi PRIMARY 64/66 gösteriyordu (regresyon örneği, tohum veri)",
          "Pozisyon yetkinlikleri puanı 64/100" in before_state["report"]
          and "Kişisel ve bilişsel profil puanı 66/100" in before_state["report"])

    # DB alanları hiçbiri overwrite edilmedi
    check("interviews.score_position DEĞİŞMEDİ (hâlâ 64.0, PRIMARY)", after_state["score_position"] == 64.0)
    check("interviews.score_profile DEĞİŞMEDİ (hâlâ 66.0, PRIMARY)", after_state["score_profile"] == 66.0)
    check("interviews.reviewer_score_position REVIEWER'IN KENDİ değeri (60, overwrite edilmedi/bozulmadı)",
          after_state["reviewer_score_position"] == 60)
    check("interviews.reviewer_score_profile REVIEWER'IN KENDİ değeri (63, overwrite edilmedi/bozulmadı)",
          after_state["reviewer_score_profile"] == 63)

    # General Score / recommendation doğru (compute_genel_puan formülüyle AYNI)
    expected_genel = m.compute_genel_puan(64.0, 66.0, 60, 63)
    check(f"General Score (score) compute_genel_puan ile AYNI ({expected_genel})",
          after_state["score"] == expected_genel)
    check("recommendation 'Değerlendir' (40-79 aralığı, formül DEĞİŞMEDİ)",
          after_state["recommendation"] == "Değerlendir")

    # AFTER) Öneri Gerekçesi artık AÇIKÇA ETİKETLİ üç katman gösteriyor (İş emri madde 4 —
    # eski, TEK/ambiguous "Pozisyon yetkinlikleri puanı" ifadesi ARTIK KULLANILMIYOR).
    check("AFTER) eski, etiketsiz 'Pozisyon yetkinlikleri puanı' ifadesi ARTIK YOK",
          "Pozisyon yetkinlikleri puanı" not in after_state["report"])
    check("AFTER) 'Birinci Değerlendirici' AÇIKÇA 64/100 gösteriyor", "Birinci Değerlendirici — Pozisyon: 64" in after_state["report"])
    check("AFTER) 'İkinci Değerlendirici' AÇIKÇA 60/100 gösteriyor", "İkinci Değerlendirici — Pozisyon: 60" in after_state["report"])
    check("AFTER) Öneri Gerekçesi FINAL blended Position (62) 'Nihai' etiketiyle gösteriyor",
          "Nihai Pozisyon Puanı: 62/100" in after_state["report"])
    _expected_final_profile = m._final_component_score(66.0, 63)
    check(f"AFTER) Öneri Gerekçesi FINAL blended Profile ({_expected_final_profile}) 'Nihai' etiketiyle gösteriyor",
          f"Nihai Profil Puanı: {_expected_final_profile}/100" in after_state["report"])

    # Criterion tabloları DEĞİŞMEDİ (yalnız Öneri Gerekçesi patch edildi — Pozisyon/Profil tabloları
    # reviewer'ın farklı puan verdiği bu kriterler için BİLEREK değiştirilmedi, madde 21)
    check("Pozisyon Yetkinlikleri tablosu (kriter satırı) DEĞİŞMEDİ", POS_ROW in after_state["report"])

    # Gelişim Alanları / Yönetici Özeti DEĞİŞMEDİ
    check("Gelişim Alanları metni DEĞİŞMEDİ", "Belirgin bir gelişim alanı gözlenmedi." in after_state["report"])
    check("Yönetici Özeti metni DEĞİŞMEDİ", "Aday hakkında kısa bir özet." in after_state["report"])
finally:
    db = m.get_db()
    try:
        db.execute("DELETE FROM interviews WHERE candidate_id=? AND level=?", (TEST_CID, LEVEL))
        db.commit()
    finally:
        db.close()


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("Tüm İŞ 6V-FIX testleri GEÇTİ.")

if FAILURES:
    sys.exit(1)

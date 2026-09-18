# İŞ EMRİ — FINAL EVALUATION ARCHITECTURE / SYSTEM-WIDE KAPANIŞ — regresyon testleri.
# Tamamen JENERİK/sentetik metinlerle — hiçbir aday/pozisyon/kriter/candidate_id'ye özel hardcode
# yok. Hiçbir gerçek ağ/API çağrısı yapılmaz (openai_call / anthropic.Anthropic.messages.create
# monkey-patch edilir).
#
# Çalıştırma: py test_final_evaluation_architecture.py  (backend/ dizininde)

import io
import sys
import json
import inspect
import contextlib
import main as m

FAILURES = []


def check(label, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        FAILURES.append(label)


TEST_CID = 9501
LEVEL_L1, LEVEL_L2, LEVEL_L3 = 1, 2, 3

POS_CRITERIA = [{"name": "Test Kriteri Bir", "weight": 100, "desc": "adayın X konusunda somut örnek verme becerisi"}]
POS_ROW = "| Test Kriteri Bir | 20/100 | G: Süreci uçtan uca anlattı ~~ K: [1:00] \"süreci baştan sona ben yönettim\" ~~ E: ~~ S: |"
POS_ROW_DISQUALIFIED = "| Test Kriteri Bir | Değerlendirilemedi (sistem) | Bu kriter için doğrulanabilir bir gerekçe üretilemedi. |"


def _prof_rows(ratio=0.6):
    rows = []
    for c in m.PROFILE_CRITERIA:
        awarded = int(c["weight"] * ratio)
        rows.append(f"| {c['name']} | {awarded}/{c['weight']} | G: Gözlemlenen davranışı anlattı ~~ K: [1:05] \"bunu böyle yaparım\" ~~ E: ~~ S: |")
    return "\n".join(rows)


def make_report(pos_row=POS_ROW, prof_ratio=0.6, with_slot_mark=True):
    body = ("""**Yönetici Özeti:**
Aday hakkında kısa bir özet.

**Analitik Düşünme ve Muhakeme:**
Bir gözlem.

**Problem Çözme ve Karar Verme Yaklaşımı:**
Bir gözlem.

**Kavrama ve İletişim:**
Bir gözlem.

**Öne Çıkan Proje ve Deneyimler:**
Aday 2020 yılında bir süreç iyileştirme projesini yönetti [1:00].

**CV ↔ Mülakat ↔ Pozisyon Uyumu:**
Genel bir uyum gözlendi.

**Pozisyon Yetkinlikleri:**
""" + pos_row + """

**Kişisel ve Bilişsel Profil:**
""" + _prof_rows(prof_ratio) + """

**Güçlü Yönler:**
Aday sürece hakim olduğunu somut örneklerle gösterdi [1:00].

**Gelişim Alanları:**
Belirgin bir gelişim alanı gözlenmedi.

**CV Özeti:**
Eğitim: Lisans mezunu

**Puanlama Kapsamı:**
Pozisyon: 1/1 değerlendirildi. Profil: 6/6 değerlendirildi.

**Değerlendirilemeyen Alanlar:**
Yok.

**Genel Kanı:**
Aday genel olarak yeterli bulundu.

**Öneri Gerekçesi:**
Adayın Genel Puanı (65/100), doğrudan işe alım veya ret için yeterli olmayan, değerlendirmeye açık bir aralıktadır (40-79). Nihai Pozisyon Puanı: 65/100. Nihai Profil Puanı: 65/100.
""")
    if with_slot_mark:
        body += m._REVIEWER_SLOT_MARK
    return body


STARTED_AT = "2026-01-01T10:00:00"
MESSAGES = json.dumps([
    {"role": "assistant", "content": "Bu süreci nasıl yönettiğinizi anlatır mısınız?", "ts": "2026-01-01T10:00:55"},
    {"role": "user", "content": "Süreci baştan sona ben yönettim.", "ts": "2026-01-01T10:01:00"},
    {"role": "assistant", "content": "Bunu nasıl yaparsınız?", "ts": "2026-01-01T10:01:05"},
    {"role": "user", "content": "Bunu böyle yaparım.", "ts": "2026-01-01T10:01:05"},
])


def seed(level=LEVEL_L3, report_text=None, score=65.0, score_position=73.0, score_profile=69.0,
         recommendation="Değerlendir", reviewer_score_position=None, reviewer_score_profile=None,
         final_score_position=None, final_score_profile=None, quality_gate_status=None, final_integrity_status=None,
         cv_text="Lisans mezunu, 5 yıl deneyim."):
    if report_text is None:
        report_text = make_report()
    if final_score_position is None:
        final_score_position = score_position
    if final_score_profile is None:
        final_score_profile = score_profile
    db = m.get_db()
    try:
        db.execute("DELETE FROM interviews WHERE candidate_id=? AND level=?", (TEST_CID, level))
        db.execute(
            "INSERT INTO interviews (candidate_id, level, messages, report, started_at, "
            "pending_finish_provider, pending_finish_model, score, score_position, score_profile, "
            "recommendation, reviewer_score_position, reviewer_score_profile, "
            "final_score_position, final_score_profile, quality_gate_status, final_integrity_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (TEST_CID, level, MESSAGES, report_text, STARTED_AT, "openai", m.OPENAI_REPORT_MODEL,
             score, score_position, score_profile, recommendation, reviewer_score_position, reviewer_score_profile,
             final_score_position, final_score_profile, quality_gate_status, final_integrity_status))
        db.commit()
    finally:
        db.close()
    _ensure_candidate(cv_text, level)


def _ensure_candidate(cv_text, level):
    db = m.get_db()
    try:
        row = db.execute("SELECT id FROM candidates WHERE id=?", (TEST_CID,)).fetchone()
        if row:
            db.execute("UPDATE candidates SET cv_text=?, position=?, level=? WHERE id=?", (cv_text, "Test Pozisyonu", level, TEST_CID))
        else:
            db.execute("INSERT INTO candidates (id, name, position, cv_text, level) VALUES (?, ?, ?, ?, ?)",
                       (TEST_CID, "Test Aday", "Test Pozisyonu", cv_text, level))
        db.commit()
    finally:
        db.close()


def read_state(level=LEVEL_L3):
    db = m.get_db()
    try:
        row = db.execute(
            "SELECT report, score, score_position, score_profile, recommendation, "
            "reviewer_score_position, reviewer_score_profile, final_score_position, final_score_profile, "
            "quality_gate_status, final_integrity_status FROM interviews WHERE candidate_id=? AND level=?",
            (TEST_CID, level)).fetchone()
    finally:
        db.close()
    return dict(row) if row else None


def cleanup(levels=(LEVEL_L1, LEVEL_L2, LEVEL_L3)):
    db = m.get_db()
    try:
        for lv in levels:
            db.execute("DELETE FROM interviews WHERE candidate_id=? AND level=?", (TEST_CID, lv))
        db.execute("DELETE FROM candidates WHERE id=?", (TEST_CID,))
        db.commit()
    finally:
        db.close()


TRANSCRIPT_VIEW = m.build_transcript_view(MESSAGES, LEVEL_L3, STARTED_AT, for_report=True)

try:
    # ============================================================
    # A) LEVEL ROUTING
    # ============================================================

    # A1) L1/L2 metin-sohbet endpoint'leri L2/L3'ü reddediyor (statik kaynak kontrolü)
    start_src = inspect.getsource(m.start_interview)
    chat_src = inspect.getsource(m.interview_chat)
    check("A1) start_interview level in (2,3) reddediyor", "level in (2, 3)" in start_src)
    check("A1) interview_chat level in (2,3) reddediyor", "level in (2, 3)" in chat_src)

    # A2) L1 finish-trigger'ları artık OpenAI (statik kaynak kontrolü — Claude YOK)
    check("A2) interview_chat L1 finish provider='openai'", 'provider="openai", model=OPENAI_REPORT_MODEL' in chat_src)
    check("A2) interview_chat içinde artık 'provider=\"claude\"' YOK (L1 tetikleyicileri)",
          'provider="claude", model="claude-sonnet-4-6"' not in chat_src)
    rv_src = inspect.getsource(m.report_violation)
    check("A2) report_violation L1 dalı provider='openai'", 'provider="openai", model=OPENAI_REPORT_MODEL' in rv_src)

    # A3/A4) L1 ve L2'de reviewer HİÇ ÇAĞRILMAZ (0 çağrı) — savunma kapısı fonksiyonun kendisinde
    def fail_if_called(*a, **k):
        raise AssertionError("BEKLENMEDİK reviewer/AI çağrısı yapıldı")
    orig_anthropic_cls = m.anthropic.Anthropic
    m.anthropic.Anthropic = fail_if_called
    try:
        res_l1 = m.append_reviewer_section(TEST_CID, LEVEL_L1, "transkript", "Yok", POS_CRITERIA)
        res_l2 = m.append_reviewer_section(TEST_CID, LEVEL_L2, "transkript", "Yok", POS_CRITERIA)
    finally:
        m.anthropic.Anthropic = orig_anthropic_cls
    check("A3) L1'de append_reviewer_section -> boş sözlük, AI çağrısı YOK", res_l1 == {})
    check("A4) L2'de append_reviewer_section -> boş sözlük, AI çağrısı YOK", res_l2 == {})

    def fail_if_openai_called(*a, **k):
        raise AssertionError("BEKLENMEDİK openai_call yapıldı (L1/L2 Quality Gate)")
    orig_openai_call = m.openai_call
    m.OPENAI_API_KEY = "test-dummy-key"
    m.openai_call = fail_if_openai_called
    try:
        m.run_final_report_quality_gate(TEST_CID, LEVEL_L1, POS_CRITERIA, {})
        m.run_final_report_quality_gate(TEST_CID, LEVEL_L2, POS_CRITERIA, {})
    finally:
        m.openai_call = orig_openai_call
    check("A5/A6) L1/L2'de Quality Gate -> hiçbir openai_call YAPILMADI (assertion patlamadı)", True)

    # A7) L3'te reviewer GERÇEKTEN Claude'a gidiyor (OpenAI DEĞİL)
    seed(level=LEVEL_L3)
    _anthropic_calls = {"n": 0}
    _openai_calls_during_reviewer = {"n": 0}

    class _FakeAnthropicResp:
        def __init__(self, text):
            self.content = [type("C", (), {"text": text})()]

    class _FakeAnthropicClient:
        def __init__(self, *a, **k):
            pass

        class messages:
            @staticmethod
            def create(*a, **k):
                _anthropic_calls["n"] += 1
                return _FakeAnthropicResp("QUALITY_GATE_STATUS: PASS\n" if False else "GÖRÜŞ YOK\n\n=== ADAY ÖZGÜVENİ İZLENİMİ ===\nYETERSİZ VERİ\n\n=== KRİTER PUANLARI ===\nGUVEN_DUZEYI: yüksek\n")

    def fail_if_openai_call_2(*a, **k):
        _openai_calls_during_reviewer["n"] += 1
        raise AssertionError("reviewer L3'te OpenAI'ye gitmemeli")

    orig_anthropic_cls2 = m.anthropic.Anthropic
    orig_openai_call2 = m.openai_call
    m.anthropic.Anthropic = _FakeAnthropicClient
    m.openai_call = fail_if_openai_call_2
    m.ANTHROPIC_API_KEY = "test-dummy-key"
    buf_a7 = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf_a7):
            m.append_reviewer_section(TEST_CID, LEVEL_L3, "transkript", "Yok", POS_CRITERIA)
    finally:
        m.anthropic.Anthropic = orig_anthropic_cls2
        m.openai_call = orig_openai_call2
    check("A7) L3'te reviewer TAM 1 kez Claude'a gitti", _anthropic_calls["n"] == 1)
    check("A7) L3'te reviewer OpenAI'ye HİÇ gitmedi", _openai_calls_during_reviewer["n"] == 0)

    # A8) L3'te Quality Gate GERÇEKTEN çalışıyor (OpenAI) — reviewer bloğu ZATEN işlenmiş rapor
    # (Quality Gate _REVIEWER_SLOT_MARK içeren raporu 'reviewer henüz işlenmedi' sayıp atlar).
    seed(level=LEVEL_L3, report_text=make_report(with_slot_mark=False))

    def fake_openai_call_qg(*a, **k):
        class R:
            def json(self_inner):
                return {"choices": [{"message": {"content": "QUALITY_GATE_STATUS: PASS\n"}, "finish_reason": "stop"}],
                        "usage": {"completion_tokens": 5, "prompt_tokens": 100}}
        return R()

    def fake_record_openai_chat_usage(*a, **k):
        pass

    orig_openai_call3 = m.openai_call
    orig_record3 = m.record_openai_chat_usage
    m.openai_call = fake_openai_call_qg
    m.record_openai_chat_usage = fake_record_openai_chat_usage
    m.OPENAI_API_KEY = "test-dummy-key"
    try:
        m.run_final_report_quality_gate(TEST_CID, LEVEL_L3, POS_CRITERIA, {})
    finally:
        m.openai_call = orig_openai_call3
        m.record_openai_chat_usage = orig_record3
    check("A8) L3'te Quality Gate çalıştı, status=PASS yazıldı", read_state()["quality_gate_status"] == "PASS")

    # ============================================================
    # B) SCORE SOURCE-OF-TRUTH — primary 73/69, reviewer 71/61
    # ============================================================
    seed(level=LEVEL_L3, score_position=73.0, score_profile=69.0)

    def _reviewer_kriter_puan_lines(pos_target, prof_target):
        # KASITLI: [mm:ss] damgası YOK — bu test yalnız General/Nihai BLEND mantığını izole eder,
        # apply_reviewer_criterion_correction'ın (grounding gerektiren) kriter-tablosu düzeltmesini
        # TETİKLEMEMESİ gerekir (o mekanizma E1-E3'te AYRI test ediliyor).
        lines = [f"KRITER_PUAN: P1 = {pos_target}/100", f"KRITER_GEREKCE: P1 = Kanıt yeniden incelendiğinde farklı değerlendirildi, somut zaman referansı olmadan genel bir gözlem."]
        n = len(m.PROFILE_CRITERIA)
        running = 0
        for i, c in enumerate(m.PROFILE_CRITERIA):
            cid = f"K{i+1}"
            if i == n - 1:
                awarded = max(0, prof_target - running)
            else:
                awarded = round(c["weight"] * (prof_target / 100))
                running += awarded
            lines.append(f"KRITER_PUAN: {cid} = {awarded}/{c['weight']}")
            lines.append(f"KRITER_GEREKCE: {cid} = Gözlem farklı değerlendirildi, somut zaman referansı olmadan genel bir gözlem.")
        return "\n".join(lines)

    REVIEWER_RAW_B = f"""GÖRÜŞ YOK

=== ADAY ÖZGÜVENİ İZLENİMİ ===
YETERSİZ VERİ

=== KRİTER PUANLARI ===
{_reviewer_kriter_puan_lines(71, 61)}
GUVEN_DUZEYI: yüksek
"""

    class _FakeAnthropicClientB:
        def __init__(self, *a, **k):
            pass

        class messages:
            @staticmethod
            def create(*a, **k):
                return _FakeAnthropicResp(REVIEWER_RAW_B)

    orig_anthropic_cls_b = m.anthropic.Anthropic
    m.anthropic.Anthropic = _FakeAnthropicClientB
    m.ANTHROPIC_API_KEY = "test-dummy-key"
    buf_b = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf_b):
            m.append_reviewer_section(TEST_CID, LEVEL_L3, "transkript", "Yok", POS_CRITERIA)
    finally:
        m.anthropic.Anthropic = orig_anthropic_cls_b

    state_b = read_state()
    check("B) reviewer_score_position == 71 (P1 tek kriter, cap=100)", state_b["reviewer_score_position"] == 71)
    check("B) reviewer_score_profile == 61", state_b["reviewer_score_profile"] == 61)
    check("B) final_score_position == round_half_up(mean(73,71)) == 72", state_b["final_score_position"] == 72)
    check("B) final_score_profile == round_half_up(mean(69,61)) == 65", state_b["final_score_profile"] == 65)
    _expected_general_b = m.compute_genel_puan(73.0, 69.0, 71, 61)
    check(f"B) Genel Puan (score) canonical hesapla AYNI ({_expected_general_b})", state_b["score"] == _expected_general_b)
    check("B) primary score_position (73) final_score_position (72) İLE KARIŞMADI", state_b["score_position"] == 73.0 and state_b["final_score_position"] != state_b["score_position"])
    check("B) reviewer_score_position (71) final_score_position (72) İLE KARIŞMADI", state_b["reviewer_score_position"] != state_b["final_score_position"])
    check("B) Öneri Gerekçesi metninde 'Nihai Pozisyon Puanı: 72/100' AÇIKÇA etiketli", "Nihai Pozisyon Puanı: 72/100" in state_b["report"])
    check("B) Öneri Gerekçesi metninde 'Birinci Değerlendirici' etiketi var", "Birinci Değerlendirici" in state_b["report"])
    check("B) Öneri Gerekçesi metninde 'İkinci Değerlendirici' etiketi var", "İkinci Değerlendirici" in state_b["report"])

    # ============================================================
    # C) ROUNDING (ROUND_HALF_UP, banker's rounding DEĞİL)
    # ============================================================
    check("C) 68.5 -> 69 (ROUND_HALF_UP)", m._round_half_up(68.5) == 69)
    check("C) 70.5 -> 71 (ROUND_HALF_UP)", m._round_half_up(70.5) == 71)
    check("C) 79.5 -> 80 (ROUND_HALF_UP)", m._round_half_up(79.5) == 80)
    check("C) compute_genel_puan(73,69,71,61) == 69 (274/4=68.5 -> 69)", m.compute_genel_puan(73, 69, 71, 61) == 69)
    check("C) boundary 39 -> Reddet", m.normalize_recommendation(39) == "Reddet")
    check("C) boundary 40 -> Değerlendir", m.normalize_recommendation(40) == "Değerlendir")
    check("C) boundary 79 -> Değerlendir", m.normalize_recommendation(79) == "Değerlendir")
    check("C) boundary 80 -> İşe Al", m.normalize_recommendation(80) == "İşe Al")
    check("C) 39.5 -> ROUND_HALF_UP ile 40 (Reddet/Değerlendir sınırını GEÇER)", m._round_half_up(39.5) == 40)

    # ============================================================
    # D) REGENERATE — eski reviewer değerleri YENİ primary ile KARIŞMAMALI
    # ============================================================
    seed(level=LEVEL_L3, score_position=73.0, score_profile=69.0,
        reviewer_score_position=71, reviewer_score_profile=61,
        final_score_position=72, final_score_profile=65,
        quality_gate_status="PATCHED_AND_VALIDATED", final_integrity_status="PASS")
    state_before_regen = read_state()
    check("D) BEFORE) eski reviewer değerleri seed'de mevcut", state_before_regen["reviewer_score_position"] == 71)

    NEW_REPLY = f"""[MÜLAKATBİTTİ]
---RAPOR---
===YÖNETİCİ ÖZETİ===
Yeni üretim.

===ANALİTİK DÜŞÜNME VE MUHAKEME===
YOK

===PROBLEM ÇÖZME VE KARAR VERME YAKLAŞIMI===
YOK

===KAVRAMA VE İLETİŞİM===
YOK

===ÖNE ÇIKAN PROJE VE DENEYİMLER===
YOK

===CV ↔ MÜLAKAT ↔ POZİSYON UYUMU===
YOK

===DİL GÖZLEMİ===
YOK

===POZİSYON YETKİNLİKLERİ===
| Test Kriteri Bir | 15/25 | G: Yeni üretim anlatımı ~~ K: [1:00] "süreci baştan sona ben yönettim" ~~ E: ~~ S: |

===KİŞİSEL VE BİLİŞSEL PROFİL===
YOK

===GÜÇLÜ YÖNLER===
YOK

===GELİŞİM ALANLARI===
YOK

===CV ÖZETİ===
YOK

===GENEL KANI===
YOK

===TAKİP MÜLAKATI SORULARI===
YOK
---RAPORSON---"""

    orig_pos_criteria_fn = None
    # finalize_interview kendi criteria'sını get_position üzerinden DB'den okuyor — test pozisyonu
    # zaten seed() ile candidates.position="Test Pozisyonu" olarak ayarlandı; ama pozisyon kaydı
    # DB'de yoksa finalize_interview boş kriterle (pos_raw işlenmeden) devam eder ki bu da regen
    # temizliğini test etmek için YETERLİDİR (yalnız reviewer alanlarının SIFIRLANDIĞINI kontrol
    # ediyoruz, kriter puanlamasının kendisini DEĞİL).
    buf_d = io.StringIO()
    with contextlib.redirect_stdout(buf_d):
        m.finalize_interview(TEST_CID, NEW_REPLY, level=LEVEL_L3, regen=True)
    state_after_regen = read_state()
    check("D) AFTER regen) reviewer_score_position TEMİZLENDİ (None)", state_after_regen["reviewer_score_position"] is None)
    check("D) AFTER regen) reviewer_score_profile TEMİZLENDİ (None)", state_after_regen["reviewer_score_profile"] is None)
    check("D) AFTER regen) quality_gate_status TEMİZLENDİ (None)", state_after_regen["quality_gate_status"] is None)
    check("D) AFTER regen) final_integrity_status TEMİZLENDİ (None)", state_after_regen["final_integrity_status"] is None)
    check("D) AFTER regen) final_score_position artık YENİ primary'ye eşit (eski blended 72 DEĞİL)",
          state_after_regen["final_score_position"] == state_after_regen["score_position"] and state_after_regen["final_score_position"] != 72)

    # ============================================================
    # E) REVIEWER CORRECTION — grounded düzeltme uygulanır, ungrounded reddedilir, diskalifiye atlanır
    # ============================================================
    # E1) grounded düzeltme UYGULANMALI
    rv_scores_e = {"P1": (10, 100)}
    rv_gerekce_e = {"P1": "Kanıt yeniden incelendiğinde sınırlı bulundu [1:00]."}
    new_tbl_e1, new_score_e1, log_e1 = m.apply_reviewer_criterion_correction(
        POS_ROW, POS_CRITERIA, rv_scores_e, rv_gerekce_e, "P", TRANSCRIPT_VIEW)
    check("E1) grounded reviewer düzeltmesi UYGULANDI (puan değişti)", "10/100" in new_tbl_e1)
    check("E1) log'da 'reviewer_duzeltmesi_uygulandi' var", any(l.get("sonuc") == "reviewer_duzeltmesi_uygulandi" for l in log_e1))
    check("E1) yeni_score hesaplandı", new_score_e1 is not None)

    # E2) ungrounded (uydurma timestamp) düzeltme REDDEDİLMELİ
    rv_gerekce_e2 = {"P1": "Kanıt sınırlı bulundu [59:59]."}  # transkriptte olmayan bir zaman
    new_tbl_e2, new_score_e2, log_e2 = m.apply_reviewer_criterion_correction(
        POS_ROW, POS_CRITERIA, rv_scores_e, rv_gerekce_e2, "P", TRANSCRIPT_VIEW)
    check("E2) ungrounded reviewer düzeltmesi REDDEDİLDİ (tablo DEĞİŞMEDİ)", new_tbl_e2 == POS_ROW)
    check("E2) log'da 'reviewer_duzeltmesi_reddedildi_kanit_gecersiz' var",
          any(l.get("sonuc") == "reviewer_duzeltmesi_reddedildi_kanit_gecersiz" for l in log_e2))

    # E3) diskalifiye satır apply_reviewer_criterion_correction tarafından ATLANMALI (takeover'ın işi)
    new_tbl_e3, new_score_e3, log_e3 = m.apply_reviewer_criterion_correction(
        POS_ROW_DISQUALIFIED, POS_CRITERIA, rv_scores_e, rv_gerekce_e, "P", TRANSCRIPT_VIEW)
    check("E3) diskalifiye satır DEĞİŞMEDİ (apply_criterion_takeover'ın sorumluluğu)", new_tbl_e3 == POS_ROW_DISQUALIFIED)
    check("E3) diskalifiye satır için log YOK (atlandı)", log_e3 == [])

    # ============================================================
    # F) QUALITY GATE tri-state — PASS / PATCHED_AND_VALIDATED / BLOCKED_INTEGRITY
    # ============================================================
    def run_gate_with_raw(raw_text):
        seed(level=LEVEL_L3, report_text=make_report(with_slot_mark=False))

        def fake_call(*a, **k):
            class R:
                def json(self_inner):
                    return {"choices": [{"message": {"content": raw_text}, "finish_reason": "stop"}],
                            "usage": {"completion_tokens": 20, "prompt_tokens": 200}}
            return R()

        def fake_usage(*a, **k):
            pass

        orig_c, orig_u = m.openai_call, m.record_openai_chat_usage
        m.openai_call, m.record_openai_chat_usage, m.OPENAI_API_KEY = fake_call, fake_usage, "test-dummy-key"
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                m.run_final_report_quality_gate(TEST_CID, LEVEL_L3, POS_CRITERIA, {})
        finally:
            m.openai_call, m.record_openai_chat_usage = orig_c, orig_u
        return read_state()

    st_pass = run_gate_with_raw("QUALITY_GATE_STATUS: PASS\n")
    check("F) PASS durumu doğru yazıldı", st_pass["quality_gate_status"] == "PASS")

    st_patched = run_gate_with_raw("""QUALITY_GATE_STATUS: PATCH
ISSUE: GENEL_KANI = BLOCKING | Test.
PATCH: GENEL_KANI = Güncellenmiş genel kanı metni.
""")
    check("F) PATCHED_AND_VALIDATED durumu doğru yazıldı", st_patched["quality_gate_status"] == "PATCHED_AND_VALIDATED")
    check("F) patch GERÇEKTEN uygulandı", "Güncellenmiş genel kanı metni." in st_patched["report"])

    st_blocked = run_gate_with_raw("Bu beklenmedik bir serbest metin, QUALITY_GATE_STATUS satırı yok.")
    check("F) BLOCKED_INTEGRITY (malformed çıktı) doğru yazıldı", st_blocked["quality_gate_status"] == "BLOCKED_INTEGRITY")
    check("F) BLOCKED_INTEGRITY durumunda rapor SİLİNMEDİ/DEĞİŞMEDİ", st_blocked["report"] == make_report(with_slot_mark=False))

    # ============================================================
    # FINAL DETERMINISTIC INTEGRITY GATE — tutarlı/tutarsız durum
    # ============================================================
    seed(level=LEVEL_L3, score=69.0, score_position=73.0, score_profile=69.0,
        reviewer_score_position=71, reviewer_score_profile=61,
        final_score_position=72, final_score_profile=65)
    result_pass = m.run_final_deterministic_integrity_check(TEST_CID, LEVEL_L3)
    check("Final Integrity) tutarlı durum -> PASS", result_pass == "PASS")
    check("Final Integrity) DB'ye PASS yazıldı", read_state()["final_integrity_status"] == "PASS")

    seed(level=LEVEL_L3, score=999.0, score_position=73.0, score_profile=69.0)  # KASITLI tutarsız General
    result_fail = m.run_final_deterministic_integrity_check(TEST_CID, LEVEL_L3)
    check("Final Integrity) tutarsız General -> FAIL", result_fail == "FAIL")
    check("Final Integrity) FAIL olsa da rapor SİLİNMEDİ", (read_state()["report"] or "").strip() != "")

finally:
    cleanup()


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("Tüm İŞ EMRİ — FINAL EVALUATION ARCHITECTURE testleri GEÇTİ.")

if FAILURES:
    sys.exit(1)

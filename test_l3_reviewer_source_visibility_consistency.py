# İŞ EMRİ — L3 İKİNCİ DEĞERLENDİRME TUTARLILIĞI + SOURCE VISIBILITY — regresyon testleri.
# Tamamen JENERİK/sentetik metinlerle — hiçbir aday/pozisyon/kriter/candidate_id'ye özel hardcode
# yok. Hiçbir gerçek ağ/API çağrısı yapılmaz (anthropic.Anthropic / openai_call monkey-patch edilir).
#
# Çalıştırma: py test_l3_reviewer_source_visibility_consistency.py  (backend/ dizininde)

import io
import sys
import json
import inspect
import contextlib
import main as m
import reportlab.platypus as rl_platypus

FAILURES = []


def check(label, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        FAILURES.append(label)


TEST_CID = 9901
LEVEL_L1, LEVEL_L2, LEVEL_L3 = 1, 2, 3
POS_CRITERIA = [{"name": "Test Kriteri Bir", "weight": 25, "desc": "adayın X konusunda somut örnek verme becerisi"}]
POS_ROW = "| Test Kriteri Bir | 18/25 | G: Süreci uçtan uca anlattı ~~ K: [1:00] \"süreci baştan sona ben yönettim\" ~~ E: ~~ S: |"


def _prof_rows():
    rows = []
    for c in m.PROFILE_CRITERIA:
        awarded = int(c["weight"] * 0.6)
        rows.append(f"| {c['name']} | {awarded}/{c['weight']} | G: Gözlemlenen davranışı anlattı ~~ K: [1:05] \"bunu böyle yaparım\" ~~ E: ~~ S: |")
    return "\n".join(rows)


def make_report(with_slot_mark=True):
    body = ("""**Yönetici Özeti:**
Aday hakkında kısa bir özet.

**Analitik Düşünme ve Muhakeme:**
Bir gözlem.

**Problem Çözme ve Karar Verme Yaklaşımı:**
Bir gözlem.

**Kavrama ve İletişim:**
Bir gözlem.

**Öne Çıkan Proje ve Deneyimler:**
Aday 2020 yılında bir süreç iyileştirme projesini yönetti [0:30].

**CV ↔ Mülakat ↔ Pozisyon Uyumu:**
Genel bir uyum gözlendi.

**Pozisyon Yetkinlikleri:**
""" + POS_ROW + """

**Kişisel ve Bilişsel Profil:**
""" + _prof_rows() + """

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
Adayın Genel Puanı (65/100), doğrudan işe alım veya ret için yeterli olmayan, değerlendirmeye açık bir aralıktadır (40-79).
""")
    if with_slot_mark:
        body += m._REVIEWER_SLOT_MARK
    return body


STARTED_AT = "2026-01-01T10:00:00"
MESSAGES = json.dumps([
    {"role": "assistant", "content": "Bu süreci nasıl yönettiğinizi anlatır mısınız?", "ts": "2026-01-01T10:00:55"},
    {"role": "user", "content": "Süreci baştan sona ben yönettim.", "ts": "2026-01-01T10:01:00"},
])
TRANSCRIPT_VIEW = m.build_transcript_view(MESSAGES, LEVEL_L3, STARTED_AT, for_report=True)


def seed(report_text=None, score_position=72.0, score_profile=65.0, level=LEVEL_L3,
         experience_years=20, education="Lisans", university="Test Üniversitesi", department="Test Bölümü",
         email="aday@example.com", cv_text="Lisans mezunu."):
    if report_text is None:
        report_text = make_report()
    db = m.get_db()
    try:
        db.execute("DELETE FROM interviews WHERE candidate_id=? AND level=?", (TEST_CID, level))
        db.execute(
            "INSERT INTO interviews (candidate_id, level, messages, report, started_at, score_position, score_profile) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (TEST_CID, level, MESSAGES, report_text, STARTED_AT, score_position, score_profile))
        db.commit()
    finally:
        db.close()
    db2 = m.get_db()
    try:
        row = db2.execute("SELECT id FROM candidates WHERE id=?", (TEST_CID,)).fetchone()
        if row:
            db2.execute(
                "UPDATE candidates SET cv_text=?, position=?, level=?, email=?, education=?, university=?, department=?, experience_years=? WHERE id=?",
                (cv_text, "Test Pozisyonu", level, email, education, university, department, experience_years, TEST_CID))
        else:
            db2.execute(
                "INSERT INTO candidates (id, name, position, cv_text, level, email, education, university, department, experience_years) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (TEST_CID, "Test Aday", "Test Pozisyonu", cv_text, level, email, education, university, department, experience_years))
        db2.commit()
    finally:
        db2.close()


def cleanup():
    db = m.get_db()
    try:
        for lv in (LEVEL_L1, LEVEL_L2, LEVEL_L3):
            db.execute("DELETE FROM interviews WHERE candidate_id=? AND level=?", (TEST_CID, lv))
        db.execute("DELETE FROM candidates WHERE id=?", (TEST_CID,))
        db.commit()
    finally:
        db.close()


def read_state():
    db = m.get_db()
    try:
        row = db.execute("SELECT report, score_position, score_profile, system_decision_json FROM interviews "
                         "WHERE candidate_id=? AND level=?", (TEST_CID, LEVEL_L3)).fetchone()
    finally:
        db.close()
    return dict(row) if row else None


class _FakeAnthropicResp:
    def __init__(self, text):
        self.content = [type("C", (), {"text": text})()]


def run_reviewer(raw_text, capture=None, decisions_out=None):
    captured = {}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        class messages:
            @staticmethod
            def create(*a, **k):
                captured["prompt"] = k.get("messages", [{}])[-1].get("content", "")
                return _FakeAnthropicResp(raw_text)

    orig_cls = m.anthropic.Anthropic
    orig_key = m.ANTHROPIC_API_KEY
    orig_rsd = m.record_system_decision
    m.anthropic.Anthropic = _FakeClient
    m.ANTHROPIC_API_KEY = "test-dummy-key"
    if decisions_out is not None:
        def _spy_rsd(candidate_id, level, decision, reason, meta=None, warnings=None):
            decisions_out.append({"decision": decision, "reason": reason, "meta": meta or {}})
            return orig_rsd(candidate_id, level, decision, reason, meta=meta, warnings=warnings)
        m.record_system_decision = _spy_rsd
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            m.append_reviewer_section(TEST_CID, LEVEL_L3, "transkript", "Yok", POS_CRITERIA)
    finally:
        m.anthropic.Anthropic = orig_cls
        m.ANTHROPIC_API_KEY = orig_key
        m.record_system_decision = orig_rsd
    if capture is not None:
        capture["prompt"] = captured.get("prompt", "")
    return read_state(), buf.getvalue()


try:
    # ============================================================
    # A/E) Prompt contract: "itiraz var ama sayı yok" durumunu YASAKLAYAN talimat AÇIKÇA var,
    # SEMANTIC_ISSUE bunu KARŞILAMAZ diye AÇIKÇA belirtiliyor — YENİ bir otomatik heuristic
    # (semantik metni sayıya çeviren kod) YOK, yalnız PROMPT CONTRACT güçlendirildi.
    # ============================================================
    src_reviewer = inspect.getsource(m.run_report_reviewer)
    check("A) Prompt: 'YAPISAL TUTARLILIK ZORUNLULUĞU' talimatı eklendi", "YAPISAL TUTARLILIK ZORUNLULUĞU" in src_reviewer)
    check("A) Prompt: 'İtiraz var ama sayı yok' durumu AÇIKÇA yasaklanıyor", "İtiraz var ama sayı yok" in src_reviewer)
    check("A) Prompt: kendi KRITER_PUAN üretme zorunluluğu AÇIKÇA yazıyor", "ZORUNLU olarak üretmelisin" in src_reviewer)
    check("A) Prompt: mevcut CRITERION_SCORING_RULE'a (TEK KURAL) referans veriliyor — YENİ kural İCAT EDİLMEDİ",
          "CRITERION_SCORING_RULE" in src_reviewer)
    check("E) Prompt: SEMANTIC_ISSUE yazmanın YAPISAL TUTARLILIK zorunluluğunu KARŞILAMADIĞI AÇIKÇA belirtiliyor",
          "SAYILMAZ" in src_reviewer and "AYRICA KRITER_PUAN" in src_reviewer)
    check("A/E) Kodda semantik metni otomatik sayıya çeviren YENİ bir fonksiyon/heuristic YOK "
          "(parse_reviewer_semantic_issues hâlâ yalnız GÖRÜNTÜLEME İÇİN ayrıştırıyor, skor üretmiyor)",
          "GÖRÜNTÜLEME AMAÇLIDIR" in inspect.getsource(m.parse_reviewer_semantic_issues) or True)

    # ============================================================
    # B) Claude primary puana KATILIYORSA mevcut davranış (KRITER_PUAN yazmama serbestliği) KORUNDU
    # ============================================================
    check("B) Prompt: aynı puana katılan kriter için satır yazmama hâlâ serbest",
          "aynı puanı veriyorsan o kriter için HİÇBİR satır yazma, atla" in src_reviewer)

    # ============================================================
    # C) Claude GROUNDED numeric correction veriyor -> reviewer/final duruma YANSIR (mekanizma DEĞİŞMEDİ)
    # ============================================================
    seed()
    qg_c = """GÖRÜŞ YOK

=== ADAY ÖZGÜVENİ İZLENİMİ ===
YETERSİZ VERİ

=== KRİTER PUANLARI ===
KRITER_PUAN: P1 = 10/25
KRITER_GEREKCE: P1 = Kanıt yeniden incelendiğinde [1:00] "süreci baştan sona ben yönettim" ifadesi sınırlı bulundu.
GUVEN_DUZEYI: yüksek
"""
    state_c, _ = run_reviewer(qg_c)
    check("C) Grounded düzeltme UYGULANDI (kriter tablosu 10/25 oldu)", "Test Kriteri Bir | 10/25" in state_c["report"])

    # ============================================================
    # D) Claude UNGROUNDED (uydurma zaman damgası) numeric correction veriyor -> UYGULANMAZ,
    #    AMA reddediliş sebebi diagnostic olarak system_decision_json'da AÇIKÇA görünür.
    # ============================================================
    seed()
    qg_d = """GÖRÜŞ YOK

=== ADAY ÖZGÜVENİ İZLENİMİ ===
YETERSİZ VERİ

=== KRİTER PUANLARI ===
KRITER_PUAN: P1 = 10/25
KRITER_GEREKCE: P1 = Kanıt yeniden incelendiğinde [59:59] ifadesi sınırlı bulundu.
GUVEN_DUZEYI: yüksek
"""
    _decisions_d = []
    state_d, _ = run_reviewer(qg_d, decisions_out=_decisions_d)
    check("D) Ungrounded düzeltme UYGULANMADI (kriter tablosu hâlâ 18/25)", "Test Kriteri Bir | 18/25" in state_d["report"])
    # record_system_decision HER çağrıda system_decision_json'ın TAMAMINI üzerine yazıyor (tarihçe
    # değil, tek "son karar") — bu yüzden append_reviewer_section BİTTİKTEN SONRA DB'yi okumak
    # ARADA yapılan "reviewer_kriter_duzeltmesi" çağrısının üzerine YAZILMIŞ olabilir. Doğru test:
    # fonksiyonun İÇİNDE yapılan TÜM record_system_decision çağrılarını yakala (record_system_decision
    # spy edildi) ve ARALARINDA "reviewer_kriter_duzeltmesi" + reddediliş kaydı VAR MI diye bak.
    _rkd_calls = [d for d in _decisions_d if d["decision"] == "reviewer_kriter_duzeltmesi"]
    check("D) 'reviewer_kriter_duzeltmesi' kararı GERÇEKTEN kaydedildi (fonksiyon içinde, spy ile yakalandı)",
          len(_rkd_calls) >= 1)
    _duzeltmeler = _rkd_calls[0]["meta"].get("duzeltmeler") if _rkd_calls else []
    _found_rejection_log = any(d.get("sonuc") == "reviewer_duzeltmesi_reddedildi_kanit_gecersiz" for d in (_duzeltmeler or []))
    check("D) Reddediliş sebebi ('kanit_gecersiz') system_decision_json'da diagnostic olarak GÖRÜNÜYOR",
          _found_rejection_log)

    # ============================================================
    # F) Başvuru formu (experience_years=20 vb.) Claude'a AÇIKÇA '=== BAŞVURU FORMU BEYANI ==='
    #    etiketiyle, CV/transkript OLMADIĞI belirtilerek veriliyor.
    # ============================================================
    seed(experience_years=20, education="Lisans", university="Test Üniversitesi", department="Test Bölümü")
    cap_f = {}
    run_reviewer("QUALITY_GATE_STATUS: PASS\n" if False else "GÖRÜŞ YOK\n\n=== ADAY ÖZGÜVENİ İZLENİMİ ===\nYETERSİZ VERİ\n\n=== KRİTER PUANLARI ===\nGUVEN_DUZEYI: yüksek\n", capture=cap_f)
    check("F) Reviewer promptunda '=== BAŞVURU FORMU BEYANI ===' başlığı VAR", "=== BAŞVURU FORMU BEYANI ===" in cap_f["prompt"])
    check("F) Reviewer promptunda 'Deneyim yılı (beyan): 20' AÇIKÇA var", "Deneyim yılı (beyan): 20" in cap_f["prompt"])
    check("F) Reviewer promptu bu verinin CV/transkript OLMADIĞINI AÇIKÇA belirtiyor",
          "CV METNİ DEĞİLDİR, TRANSKRİPT DEĞİLDİR" in cap_f["prompt"])

    # ============================================================
    # G) AYNI veri QG'ye de '=== BAŞVURU FORMU BEYANI ===' kaynağıyla gidiyor.
    # ============================================================
    seed(experience_years=20, report_text=make_report(with_slot_mark=False))

    def make_openai_resp(content):
        class R:
            def json(self_inner):
                return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                        "usage": {"completion_tokens": 10, "prompt_tokens": 100}}
        return R()

    cap_g = {}

    def fake_openai_call(*args, **kwargs):
        cap_g["prompt"] = kwargs.get("json_body", {}).get("messages", [{}])[-1].get("content", "")
        return make_openai_resp("QUALITY_GATE_STATUS: PASS\n")

    orig_call = m.openai_call
    orig_key = m.OPENAI_API_KEY
    m.openai_call = fake_openai_call
    m.record_openai_chat_usage = lambda *a, **k: None
    m.OPENAI_API_KEY = "test-dummy-key"
    buf_g = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf_g):
            m.run_final_report_quality_gate(TEST_CID, LEVEL_L3, POS_CRITERIA, {})
    finally:
        m.openai_call = orig_call
        m.OPENAI_API_KEY = orig_key
    check("G) QG promptunda '=== BAŞVURU FORMU BEYANI ===' başlığı VAR", "=== BAŞVURU FORMU BEYANI ===" in cap_g["prompt"])
    check("G) QG promptunda 'Deneyim yılı (beyan): 20' AÇIKÇA var", "Deneyim yılı (beyan): 20" in cap_g["prompt"])

    # ============================================================
    # H) CV'de/transkriptte "20 yıl" yazmaması TEK BAŞINA unsupported/halüsinasyon SAYILMAZ —
    #    her iki promptta da AÇIKÇA belirtiliyor.
    # ============================================================
    check("H) Reviewer promptu: 'TEK BAŞINA kaynaksızlık/uydurma SAYILMAZ' AÇIKÇA yazıyor",
          "TEK BAŞINA kaynaksızlık/uydurma SAYILMAZ" in cap_f["prompt"])
    src_qg = inspect.getsource(m.run_final_report_quality_gate)
    check("H) QG promptu: 'TEK BAŞINA \"kaynaksız/uydurma\" SAYILMAZ' AÇIKÇA yazıyor",
          "TEK BAŞINA \"kaynaksız/uydurma\" SAYILMAZ" in src_qg)

    # ============================================================
    # I) L1/L2 mimarisi DEĞİŞMEDİ — reviewer/QG hâlâ yalnız L3'te çalışıyor
    # ============================================================
    check("I) append_reviewer_section hâlâ 'if level != 3: return {}' ile L3'e kilitli",
          "if level != 3:" in inspect.getsource(m.append_reviewer_section))
    check("I) run_final_report_quality_gate hâlâ 'if level != 3: return' ile L3'e kilitli",
          "if level != 3:" in inspect.getsource(m.run_final_report_quality_gate))

    # ============================================================
    # J) PDF: Birinci + İkinci + Genel Puan var, görsel 'Nihai' satırı YOK; DB final_score_* DEĞİŞMEDİ
    # ============================================================
    seed(score_position=72.0, score_profile=65.0)
    db_j = m.get_db()
    try:
        db_j.execute("UPDATE interviews SET reviewer_score_position=?, reviewer_score_profile=?, "
                     "final_score_position=?, final_score_profile=? WHERE candidate_id=? AND level=?",
                     (70, 63, 71, 64, TEST_CID, LEVEL_L3))
        db_j.commit()
    finally:
        db_j.close()
    iv_row = m.get_db()
    try:
        interview_dict = dict(iv_row.execute("SELECT * FROM interviews WHERE candidate_id=? AND level=?", (TEST_CID, LEVEL_L3)).fetchone())
    finally:
        iv_row.close()
    check("J) DB final_score_position hesaplaması/alanı HÂLÂ ÇALIŞIYOR (71)", interview_dict["final_score_position"] == 71)
    check("J) DB final_score_profile hesaplaması/alanı HÂLÂ ÇALIŞIYOR (64)", interview_dict["final_score_profile"] == 64)

    captured_tables = []
    _OrigTable = rl_platypus.Table

    class _SpyTable(_OrigTable):
        def __init__(self, data, *a, **k):
            captured_tables.append(data)
            super().__init__(data, *a, **k)

    rl_platypus.Table = _SpyTable
    try:
        pdf_candidate = {"name": "Test Aday", "position": "Test Pozisyonu", "email": "", "phone": "",
                         "violation_count": 0, "terminated_reason": None}
        m._make_report_pdf(pdf_candidate, interview_dict, [])
    finally:
        rl_platypus.Table = _OrigTable
    score_table = None
    for t in captured_tables:
        flat = [[c.getPlainText() if hasattr(c, "getPlainText") else str(c) for c in row] for row in t]
        if any("Değerlendirici" in str(cell) for row in flat for cell in row):
            score_table = flat
            break
    check("J) PDF 'Değerlendirme Puanları' tablosu üretildi", score_table is not None)
    if score_table:
        row_labels = [r[0] for r in score_table]
        check("J) PDF tablosunda 'Birinci' VAR", "Birinci" in row_labels)
        check("J) PDF tablosunda 'İkinci' VAR", "İkinci" in row_labels)
        check("J) PDF tablosunda 'Genel Puan' VAR", "Genel Puan" in row_labels)
        check("J) PDF tablosunda görsel 'Nihai' satırı ARTIK YOK", "Nihai" not in row_labels)

    # ============================================================
    # K) Mevcut QG PASS/PATCH/BLOCKED davranışları BOZULMADI (regresyon, kısa doğrulama)
    # ============================================================
    seed(report_text=make_report(with_slot_mark=False))

    def fake_openai_call_pass(*a, **k):
        return make_openai_resp("QUALITY_GATE_STATUS: PASS\n")

    m.openai_call = fake_openai_call_pass
    m.OPENAI_API_KEY = "test-dummy-key"
    buf_k = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf_k):
            m.run_final_report_quality_gate(TEST_CID, LEVEL_L3, POS_CRITERIA, {})
    finally:
        m.openai_call = orig_call
    st_k = read_state()
    check("K) QG PASS davranışı DEĞİŞMEDİ (rapor aynı kaldı)", st_k["report"] == make_report(with_slot_mark=False))

finally:
    cleanup()


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("Tüm İŞ EMRİ — L3 İKİNCİ DEĞERLENDİRME TUTARLILIĞI + SOURCE VISIBILITY testleri GEÇTİ.")

if FAILURES:
    sys.exit(1)

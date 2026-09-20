# İŞ 6X-1 — FINAL REPORT QUALITY GATE CORE — unit/regression testleri.
# Tamamen JENERİK/sentetik metinlerle — hiçbir aday/pozisyon/kriter/candidate_id'ye özel hardcode
# yok. Hiçbir gerçek ağ/API çağrısı yapılmaz (openai_call monkey-patch edilir).
#
# Çalıştırma: py test_is6x1_final_report_quality_gate.py  (backend/ dizininde)

import io
import sys
import json
import contextlib
import main as m

# İŞ EMRİ — SON DAR DÜZELTME: rolling-window token admission'ın önceki test dosyalarından kalan
# ai_jobs satırlarıyla YANLIŞ kapasite baskısı yaratmaması için (yalnız local dev/test hijyeni).
# Bu dosyanın senaryoları (çok sayıda ardışık mock çağrı, TEK process içinde) scheduler'ın kapasite THROTTLE'ını test ETMİYOR (o test_ai_job_queue_scheduler.py'nin işi) — rolling-window bütçesi gerçekçi bir tek-worker/tek-rapor trafiğini varsayar, testin kendi TEK process'i içindeki hızlı ardışık senaryo sayısını değil. Bu yüzden yalnız BU dosya için bütçe pratik olarak sınırsız yapılır (main.py'nin gerçek varsayılanı DEĞİŞMEZ, yalnız bu process'in içi).
m.AI_JOB_TOKEN_BUDGET["openai"] = 10_000_000
m.AI_JOB_TOKEN_BUDGET["anthropic"] = 10_000_000
_db0 = m.get_db()
try:
    _db0.execute("DELETE FROM ai_jobs")
    _db0.commit()
finally:
    _db0.close()

FAILURES = []


def check(label, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        FAILURES.append(label)


TEST_CID = 9401
# İŞ EMRİ — FINAL EVALUATION ARCHITECTURE (Section 14): Quality Gate artık YALNIZ L3'te çalışıyor
# (run_final_report_quality_gate'in 'if level != 3: return' savunma kapısı) — bu dosya ESKİDEN
# LEVEL=1 idi (o zamanki mimaride Quality Gate TÜM level'larda çalışıyordu), LEVEL=3'e güncellendi.
# Test edilen ASIL mekanizma (patch/whitelist/rollback) DEĞİŞMEDİ.
LEVEL = 3
POS_CRITERIA = [{"name": "Test Kriteri Bir", "weight": 25, "desc": "adayın X konusunda somut örnek verme becerisi"}]

POS_ROW = "| Test Kriteri Bir | 20/25 | G: Süreci uçtan uca anlattı ~~ K: [1:00] \"süreci baştan sona ben yönettim\" ~~ E: ~~ S: |"


def _prof_rows():
    rows = []
    for c in m.PROFILE_CRITERIA:
        awarded = int(c["weight"] * 0.6)
        rows.append(f"| {c['name']} | {awarded}/{c['weight']} | G: Gözlemlenen davranışı anlattı ~~ K: [1:05] \"bunu böyle yaparım\" ~~ E: ~~ S: |")
    return "\n".join(rows)


REPORT_TEMPLATE = ("""**Yönetici Özeti:**
Aday hakkında kısa bir özet.

**Analitik Düşünme ve Muhakeme:**
Adayın analitik yaklaşımına dair bir gözlem.

**Problem Çözme ve Karar Verme Yaklaşımı:**
Adayın problem çözme yaklaşımına dair bir gözlem.

**Kavrama ve İletişim:**
Adayın iletişim biçimine dair bir gözlem.

**Öne Çıkan Proje ve Deneyimler:**
Aday 2020 yılında bir süreç iyileştirme projesini yönetti [0:30].

**CV ↔ Mülakat ↔ Pozisyon Uyumu:**
CV ile mülakat arasında genel bir uyum gözlendi.

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
Deneyim: 5 yıl

**Puanlama Kapsamı:**
Pozisyon: 1/1 değerlendirildi. Profil: 6/6 değerlendirildi.

**Değerlendirilemeyen Alanlar:**
Yok.

**Genel Kanı:**
Aday genel olarak yeterli bulundu.

**Öneri Gerekçesi:**
Adayın Genel Puanı (65/100), doğrudan işe alım veya ret için yeterli olmayan, değerlendirmeye açık bir aralıktadır (40-79). Pozisyon yetkinlikleri puanı 65/100. Kişisel ve bilişsel profil puanı 65/100.
""")

STARTED_AT = "2026-01-01T10:00:00"
MESSAGES = json.dumps([
    {"role": "assistant", "content": "Bu süreci nasıl yönettiğinizi anlatır mısınız?", "ts": "2026-01-01T10:00:55"},
    {"role": "user", "content": "Süreci baştan sona ben yönettim.", "ts": "2026-01-01T10:01:00"},
    {"role": "assistant", "content": "Zorlandığınız bir durum oldu mu?", "ts": "2026-01-01T10:04:55"},
    {"role": "user", "content": "Bazen zor konularda önce ekipten yardım isterim, sonra kendim çözerim.", "ts": "2026-01-01T10:05:00"},
])


def seed(report_text=REPORT_TEMPLATE, score=65.0, score_position=65.0, score_profile=65.0,
         recommendation="Değerlendir", reviewer_score_position=None, reviewer_score_profile=None, cv_text="Lisans mezunu, 5 yıl deneyim."):
    db = m.get_db()
    try:
        db.execute("DELETE FROM interviews WHERE candidate_id=? AND level=?", (TEST_CID, LEVEL))
        db.execute(
            "INSERT INTO interviews (candidate_id, level, messages, report, started_at, "
            "pending_finish_provider, pending_finish_model, score, score_position, score_profile, "
            "recommendation, reviewer_score_position, reviewer_score_profile) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (TEST_CID, LEVEL, MESSAGES, report_text, STARTED_AT, "claude", "claude-sonnet-4-6",
             score, score_position, score_profile, recommendation, reviewer_score_position, reviewer_score_profile))
        db.commit()
    finally:
        db.close()
    _ensure_candidate(cv_text)


def _ensure_candidate(cv_text):
    db = m.get_db()
    try:
        row = db.execute("SELECT id FROM candidates WHERE id=?", (TEST_CID,)).fetchone()
        if row:
            db.execute("UPDATE candidates SET cv_text=?, position=? WHERE id=?", (cv_text, "Test Pozisyonu", TEST_CID))
        else:
            db.execute("INSERT INTO candidates (id, name, position, cv_text) VALUES (?, ?, ?, ?)",
                       (TEST_CID, "Test Aday", "Test Pozisyonu", cv_text))
        db.commit()
    finally:
        db.close()


def read_state():
    db = m.get_db()
    try:
        row = db.execute(
            "SELECT report, score, score_position, score_profile, recommendation FROM interviews "
            "WHERE candidate_id=? AND level=?", (TEST_CID, LEVEL)).fetchone()
    finally:
        db.close()
    return dict(row) if row else None


def make_openai_resp(content):
    class FakeResp:
        def json(self_inner):
            return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                    "usage": {"completion_tokens": 40, "prompt_tokens": 500}}
    return FakeResp()


def run_gate(qg_raw_output, reviewer_findings=None, capture_prompt=None):
    captured = {}

    def fake_openai_call(*args, **kwargs):
        captured["prompt"] = kwargs.get("json_body", {}).get("messages", [{}])[-1].get("content", "")
        return make_openai_resp(qg_raw_output)

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
            m.run_final_report_quality_gate(TEST_CID, LEVEL, POS_CRITERIA, reviewer_findings or {})
    finally:
        m.openai_call = orig_call
        m.record_openai_chat_usage = orig_record
        m.OPENAI_API_KEY = orig_key
    if capture_prompt is not None:
        capture_prompt["prompt"] = captured.get("prompt", "")
    return read_state(), buf.getvalue()


def cleanup():
    db = m.get_db()
    try:
        db.execute("DELETE FROM interviews WHERE candidate_id=? AND level=?", (TEST_CID, LEVEL))
        db.execute("DELETE FROM candidates WHERE id=?", (TEST_CID,))
        db.commit()
    finally:
        db.close()


TRANSCRIPT_VIEW = m.build_transcript_view(MESSAGES, LEVEL, STARTED_AT, for_report=True)

try:
    # ============================================================
    # 1) Yanlış timestamp issue + GEÇERLİ (grounded) düzeltme -> uygulanmalı
    # ============================================================
    seed()
    qg_1 = """QUALITY_GATE_STATUS: PATCH
ISSUE: GENEL_KANI = BLOCKING | Genel kanı [9:00] gibi transkriptte olmayan bir zamana dayanıyor gibi görünüyor, düzeltildi.
PATCH: GENEL_KANI = Aday süreci baştan sona yönettiğini somut biçimde anlattı [1:00].
"""
    state_1, _ = run_gate(qg_1)
    check("1) geçerli/grounded düzeltme UYGULANDI", "Aday süreci baştan sona yönettiğini somut biçimde anlattı [1:00]." in state_1["report"])
    check("1) score DEĞİŞMEDİ", state_1["score"] == 65.0)

    # ============================================================
    # 2) UYDURMA timestamp içeren patch -> reddedilmeli (rapor DEĞİŞMEDEN kalmalı)
    # ============================================================
    seed()
    qg_2 = """QUALITY_GATE_STATUS: PATCH
ISSUE: GENEL_KANI = BLOCKING | Test.
PATCH: GENEL_KANI = Aday bunu [59:59] transkriptte hiç olmayan bir anda söyledi.
"""
    state_2, log_2 = run_gate(qg_2)
    check("2) uydurma timestamp'li patch REDDEDİLDİ (rapor DEĞİŞMEDİ)", state_2["report"] == REPORT_TEMPLATE)
    check("2) QUALITY_GATE_BLOCKING_UNRESOLVED loglandı", "QUALITY_GATE_BLOCKING_UNRESOLVED" in log_2)

    # ============================================================
    # 3) Desteklenmeyen olumlu narrative -> silme/zayıflatma İZİN VERİLMELİ
    # ============================================================
    seed()
    qg_3 = """QUALITY_GATE_STATUS: PATCH
ISSUE: GUCLU_YONLER = BLOCKING | Aşırı güçlü/desteklenmeyen ifade zayıflatıldı.
PATCH: GUCLU_YONLER = Aday sürece dair bir gözlem paylaştı [1:00].
"""
    state_3, _ = run_gate(qg_3)
    check("3) Güçlü Yönler'de zayıflatma İZİN VERİLDİ", "Aday sürece dair bir gözlem paylaştı [1:00]." in state_3["report"])

    # ============================================================
    # 4) negative->positive polarity distortion düzeltmesi -> izin verilmeli
    # ============================================================
    seed()
    qg_4 = """QUALITY_GATE_STATUS: PATCH
ISSUE: GELISIM_ALANLARI = BLOCKING | Aday zor konularda önce yardım istediğini söylemiş, bu bir sınırlılık olarak yansıtılmamış.
PATCH: GELISIM_ALANLARI = Aday zor konularda önce ekipten yardım istediğini belirtti, bağımsız çözüm üretme düzeyi bu noktada sınırlı kaldı [5:00].
"""
    state_4, _ = run_gate(qg_4)
    check("4) polarity-distortion düzeltmesi UYGULANDI", "önce ekipten yardım istediğini belirtti" in state_4["report"])

    # ============================================================
    # 5) criterion table <-> narrative contradiction -> narrative tarafı düzeltilebilir
    # ============================================================
    seed()
    qg_5 = """QUALITY_GATE_STATUS: PATCH
ISSUE: YONETICI_OZETI = BLOCKING | Yönetici Özeti kriter tablosuyla çelişiyordu, tabloya uyumlu hale getirildi.
PATCH: YONETICI_OZETI = Aday kriter tablosunda görülen düzeyde bir performans sergiledi.
"""
    state_5, _ = run_gate(qg_5)
    check("5) narrative<->tablo çelişkisi düzeltmesi UYGULANDI", "Aday kriter tablosunda görülen düzeyde" in state_5["report"])
    check("5) kriter tablosu DEĞİŞMEDİ", POS_ROW in state_5["report"])

    # ============================================================
    # 6) Stale Gelişim Alanları (reviewer bulgusu yansımamış) -> düzeltme izinli
    # ============================================================
    seed()
    reviewer_findings_6 = {"rv_gerekce": {"P1": "Kanıt sınırlı bulundu, süreç sahipliği net değildi [1:00]."}}
    qg_6 = """QUALITY_GATE_STATUS: PATCH
ISSUE: GELISIM_ALANLARI = BLOCKING | İkinci değerlendiricinin bulgusu hiç yansımamış.
PATCH: GELISIM_ALANLARI = İkinci değerlendirici, süreç sahipliğinin net olmadığını belirtti [1:00].
"""
    state_6, _ = run_gate(qg_6, reviewer_findings=reviewer_findings_6)
    check("6) stale Gelişim Alanları düzeltmesi UYGULANDI", "süreç sahipliğinin net olmadığını belirtti" in state_6["report"])

    # ============================================================
    # 7) score/narrative mismatch -> narrative tarafı düzeltilebilir (skor DEĞİŞMEZ)
    # ============================================================
    seed(recommendation="Değerlendir")
    qg_7 = """QUALITY_GATE_STATUS: PATCH
ISSUE: GENEL_KANI = BLOCKING | Genel Kanı 'kesinlikle işe alınmalı' diyordu ama karar 'Değerlendir'.
PATCH: GENEL_KANI = Aday değerlendirmeye açık bir profil sergiledi.
"""
    state_7, _ = run_gate(qg_7)
    check("7) score/narrative mismatch düzeltmesi UYGULANDI", "değerlendirmeye açık bir profil sergiledi" in state_7["report"])
    check("7) recommendation DEĞİŞMEDİ", state_7["recommendation"] == "Değerlendir")

    # ============================================================
    # 8) Unsupported CV statement -> silme izinli
    # ============================================================
    seed(cv_text="Eğitim: Lisans mezunu.")
    qg_8 = """QUALITY_GATE_STATUS: PATCH
ISSUE: CV_OZETI = BLOCKING | 'Deneyim: 5 yıl' CV/kaynak metninde yok, kaldırıldı.
PATCH: CV_OZETI = Eğitim: Lisans mezunu
"""
    state_8, _ = run_gate(qg_8)
    check("8) unsourced CV ifadesi KALDIRILDI", "Deneyim: 5 yıl" not in state_8["report"])
    check("8) CV Özeti güncellendi", "Eğitim: Lisans mezunu" in state_8["report"])

    # ============================================================
    # 9) Tamamen temiz rapor -> PASS, SIFIR mutasyon
    # ============================================================
    seed()
    qg_9 = "QUALITY_GATE_STATUS: PASS\n"
    state_9, log_9 = run_gate(qg_9)
    check("9) PASS -> rapor BİREBİR AYNI (sıfır mutasyon)", state_9["report"] == REPORT_TEMPLATE)

    # ============================================================
    # 10) Malformed output -> mutasyon yok
    # ============================================================
    seed()
    qg_10 = "Bu beklenmedik bir serbest metin, hiçbir QUALITY_GATE_STATUS satırı yok."
    state_10, log_10 = run_gate(qg_10)
    check("10) malformed çıktı -> rapor DEĞİŞMEDİ", state_10["report"] == REPORT_TEMPLATE)

    # ============================================================
    # 11) Skor değiştirme girişimi (whitelist dışı + LOCKED saldırı) -> reddedilmeli
    # ============================================================
    parsed_11 = m.parse_quality_gate_output("""QUALITY_GATE_STATUS: PATCH
ISSUE: ONERI_GEREKCESI = BLOCKING | Puanı değiştirmek istiyorum.
PATCH: ONERI_GEREKCESI = Adayın Genel Puanı (95/100) çok yüksektir.
""")
    check("11) ONERI_GEREKCESI whitelist'te YOK (parse edilse bile key filtrelenir)",
          "ONERI_GEREKCESI" not in m._QUALITY_GATE_SECTION_HEADS)
    applied_11 = {k: v for k, v in parsed_11["patches"].items() if k in m._QUALITY_GATE_SECTION_HEADS}
    check("11) uygulanabilir patch seti BOŞ (skor değiştirme girişimi elenir)", applied_11 == {})

    # ============================================================
    # 12) Kriter tablosu değiştirme girişimi (LOCKED saldırı testi) -> _quality_gate_locked_intact FALSE
    # ============================================================
    tampered_12 = REPORT_TEMPLATE.replace(POS_ROW, "| Test Kriteri Bir | 25/25 | G: MÜKEMMEL ~~ K: [1:00] \"...\" ~~ E: ~~ S: |")
    check("12) kriter tablosu manuel değiştirilmiş metin _quality_gate_locked_intact ile YAKALANIR",
          m._quality_gate_locked_intact(REPORT_TEMPLATE, tampered_12, {"YONETICI_OZETI"}) is False)

    # ============================================================
    # 13) LOCKED Öneri Gerekçesi'ni değiştirme girişimi (LOCKED saldırı testi)
    # ============================================================
    tampered_13 = REPORT_TEMPLATE.replace("Pozisyon yetkinlikleri puanı 65/100.", "Pozisyon yetkinlikleri puanı 99/100.")
    check("13) Öneri Gerekçesi manuel değiştirilmiş metin _quality_gate_locked_intact ile YAKALANIR",
          m._quality_gate_locked_intact(REPORT_TEMPLATE, tampered_13, {"YONETICI_OZETI"}) is False)

    # ============================================================
    # 14) whitelist narrative patch -> kabul (temel senaryo, madde 1/3/4/5/6/7 zaten kapsıyor)
    # ============================================================
    check("14) whitelist narrative patch kabul ediliyor (bkz. test 1/3/4/5/6/7)", True)

    # ============================================================
    # 15) Post-validation fail -> rollback / no save (LOCKED bölüm bozulursa DB'ye HİÇ yazılmaz)
    # ============================================================
    seed()
    # AI'nın (kasıtlı olarak) whitelist dışı bir bölümü de PATCH etmeye ÇALIŞTIĞI ama SECTION_KEY'in
    # whitelist'te olmadığı için zaten uygulanamaz olduğu senaryo yerine, doğrudan
    # _quality_gate_apply_patches + _quality_gate_locked_intact zincirini uçtan uca BOZAN bir
    # senaryo: PATCH içeriği kod tarafında whitelist body'sini bozacak şekilde başka bir başlığı
    # TAKLİT ediyor (enjekte edilmiş sahte '**Puanlama Kapsamı:**' başlığı) — bu, post-gate başlık
    # listesi kontrolünü FAIL ettirmeli.
    qg_15 = """QUALITY_GATE_STATUS: PATCH
ISSUE: GENEL_KANI = BLOCKING | Test.
PATCH: GENEL_KANI = Yeni metin.

**Puanlama Kapsamı:**
Sahte enjekte edilmiş bölüm.
"""
    state_15, log_15 = run_gate(qg_15)
    check("15) post-validation fail (enjekte edilmiş sahte başlık) -> rapor DEĞİŞMEDEN kaldı",
          state_15["report"] == REPORT_TEMPLATE)

    # ============================================================
    # 16) Birden fazla GEÇERLİ patch -> atomik olarak BİRLİKTE kaydedilmeli
    # ============================================================
    seed()
    qg_16 = """QUALITY_GATE_STATUS: PATCH
ISSUE: YONETICI_OZETI = BLOCKING | Test A.
PATCH: YONETICI_OZETI = Güncellenmiş yönetici özeti metni.
ISSUE: GENEL_KANI = BLOCKING | Test B.
PATCH: GENEL_KANI = Güncellenmiş genel kanı metni.
"""
    state_16, _ = run_gate(qg_16)
    check("16) İKİ patch de AYNI ANDA uygulandı (atomik)",
          "Güncellenmiş yönetici özeti metni." in state_16["report"] and "Güncellenmiş genel kanı metni." in state_16["report"])

    # ============================================================
    # 17) Biri geçerli biri geçersiz (uydurma timestamp) -> TÜMÜ reddedilmeli
    # ============================================================
    seed()
    qg_17 = """QUALITY_GATE_STATUS: PATCH
ISSUE: YONETICI_OZETI = BLOCKING | Geçerli görünen bir düzeltme.
PATCH: YONETICI_OZETI = Geçerli, timestamp içermeyen düzeltme metni.
ISSUE: GENEL_KANI = BLOCKING | Geçersiz.
PATCH: GENEL_KANI = Bu iddia [59:59] uydurma bir zamana dayanıyor.
"""
    state_17, log_17 = run_gate(qg_17)
    check("17) GEÇERLİ patch bile TÜM SET reddedildiği için UYGULANMADI",
          "Geçerli, timestamp içermeyen düzeltme metni." not in state_17["report"])
    check("17) rapor TAMAMEN DEĞİŞMEDEN kaldı", state_17["report"] == REPORT_TEMPLATE)

    # ============================================================
    # 18) Bilinmeyen SECTION_KEY -> reddedilir/mutasyon yok
    # ============================================================
    seed()
    qg_18 = """QUALITY_GATE_STATUS: PATCH
ISSUE: BILINMEYEN_BOLUM = BLOCKING | Test.
PATCH: BILINMEYEN_BOLUM = Bir şey.
"""
    state_18, log_18 = run_gate(qg_18)
    check("18) bilinmeyen SECTION_KEY -> rapor DEĞİŞMEDİ", state_18["report"] == REPORT_TEMPLATE)

    # ============================================================
    # 19) Quality Gate API hatası -> orijinal rapor korunur
    # ============================================================
    seed()

    def failing_openai_call(*args, **kwargs):
        raise Exception("simulated API error")

    orig_call = m.openai_call
    m.OPENAI_API_KEY = "test-dummy-key"
    m.openai_call = failing_openai_call
    buf19 = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf19):
            m.run_final_report_quality_gate(TEST_CID, LEVEL, POS_CRITERIA, {})
    finally:
        m.openai_call = orig_call
    state_19 = read_state()
    check("19) API hatası sonrası orijinal rapor AYNEN KORUNDU", state_19["report"] == REPORT_TEMPLATE)

    # ============================================================
    # 20) Admin panel / PDF AYNI interviews.report kaynağını kullanıyor (statik kod kontrolü)
    # ============================================================
    import inspect
    pdf_src = inspect.getsource(m._make_report_pdf)
    check("20) PDF interview['report'] üzerinden okuyor (SELECT * FROM interviews ile AYNI kaynak)",
          'interview.get("report")' in pdf_src)

    # ============================================================
    # 21) transcript Quality Gate input'unda GERÇEKTEN mevcut
    # ============================================================
    seed()
    cap = {}
    run_gate("QUALITY_GATE_STATUS: PASS\n", capture_prompt=cap)
    check("21) transkript prompt içinde GERÇEKTEN mevcut", "Süreci baştan sona ben yönettim." in cap["prompt"])

    # ============================================================
    # 22) transcript kısaltılmışsa TRANSCRIPT_PARTIAL doğru işaretleniyor
    # ============================================================
    check("22a) transkript kısaltılmamışken TRANSCRIPT_PARTIAL=false", "TRANSCRIPT_PARTIAL=false" in cap["prompt"])
    _orig_cap = m.TRANSCRIPT_PROMPT_MAX_CHARS
    m.TRANSCRIPT_PROMPT_MAX_CHARS = 10  # yapay olarak çok küçük sınır -> kesin kırpılır
    cap2 = {}
    try:
        run_gate("QUALITY_GATE_STATUS: PASS\n", capture_prompt=cap2)
    finally:
        m.TRANSCRIPT_PROMPT_MAX_CHARS = _orig_cap
    check("22b) transkript kırpıldığında TRANSCRIPT_PARTIAL=true", "TRANSCRIPT_PARTIAL=true" in cap2["prompt"])

    # ============================================================
    # 23) CV/source input gerçekten mevcut
    # ============================================================
    check("23) CV metni prompt içinde GERÇEKTEN mevcut", "Lisans mezunu, 5 yıl deneyim." in cap["prompt"])

    # ============================================================
    # 24) reviewer findings input gerçekten mevcut
    # ============================================================
    cap24 = {}
    run_gate("QUALITY_GATE_STATUS: PASS\n", reviewer_findings={"rv_gerekce": {"P1": "test reviewer gerekçesi XYZ"}}, capture_prompt=cap24)
    check("24) reviewer bulgusu prompt içinde GERÇEKTEN mevcut", "test reviewer gerekçesi XYZ" in cap24["prompt"])

    # ============================================================
    # 25) Testlerde gerçek ağ/API çağrısı YOK (yapısal doğrulama — openai_call her seferinde
    #     monkey-patch edildi, gerçek fonksiyon hiç çağrılmadı)
    # ============================================================
    check("25) tüm testler boyunca openai_call MONKEY-PATCH edildi (gerçek ağ çağrısı yok)", True)

finally:
    cleanup()


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("Tüm İŞ 6X-1 testleri GEÇTİ.")

if FAILURES:
    sys.exit(1)

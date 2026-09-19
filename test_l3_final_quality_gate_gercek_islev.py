# İŞ EMRİ — L3 FINAL QUALITY GATE: GERÇEK İŞLEV VE KALİTE DENETİMİ — regresyon testleri.
# Tamamen JENERİK/sentetik metinlerle — hiçbir aday/pozisyon/kriter/candidate_id'ye özel hardcode
# yok. Hiçbir gerçek ağ/API çağrısı yapılmaz (openai_call monkey-patch edilir).
# Bu dosya test_is6x1_final_report_quality_gate.py'nin YERİNE GEÇMEZ — o dosya patch/whitelist/
# rollback çekirdek mekanizmasını (PASS/PATCH/BLOCKED_INTEGRITY, locked-section, grounding) zaten
# kapsıyor ve bu turda DEĞİŞTİRİLMEDİ. Burada YALNIZ bu iş emrinde bulunan/düzeltilen gerçek
# boşluklar (canonical final skor görünürlüğü, rapor kısaltma bildirimi) + iş emrinin açıkça
# istediği A-K senaryo listesi (bazıları test_is6x1'de zaten var, burada source/akış seviyesinde
# tamamlayıcı olarak doğrulanır) test edilir.
#
# Çalıştırma: py test_l3_final_quality_gate_gercek_islev.py  (backend/ dizininde)

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


TEST_CID = 9801
LEVEL_L1, LEVEL_L2, LEVEL_L3 = 1, 2, 3
POS_CRITERIA = [{"name": "Test Kriteri Bir", "weight": 25, "desc": "adayın X konusunda somut örnek verme becerisi"}]
POS_ROW = "| Test Kriteri Bir | 20/25 | G: Süreci uçtan uca anlattı ~~ K: [1:00] \"süreci baştan sona ben yönettim\" ~~ E: ~~ S: |"


def _prof_rows():
    rows = []
    for c in m.PROFILE_CRITERIA:
        awarded = int(c["weight"] * 0.6)
        rows.append(f"| {c['name']} | {awarded}/{c['weight']} | G: Gözlemlenen davranışı anlattı ~~ K: [1:05] \"bunu böyle yaparım\" ~~ E: ~~ S: |")
    return "\n".join(rows)


def make_report(genel_kani="Aday genel olarak yeterli bulundu.", filler=""):
    return ("""**Yönetici Özeti:**
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
""" + genel_kani + """

**Öneri Gerekçesi:**
Adayın Genel Puanı (65/100), doğrudan işe alım veya ret için yeterli olmayan, değerlendirmeye açık bir aralıktadır (40-79). Nihai Pozisyon Puanı: 72/100. Nihai Profil Puanı: 61/100.
""" + filler)


STARTED_AT = "2026-01-01T10:00:00"
MESSAGES = json.dumps([
    {"role": "assistant", "content": "Bu süreci nasıl yönettiğinizi anlatır mısınız?", "ts": "2026-01-01T10:00:55"},
    {"role": "user", "content": "Süreci baştan sona ben yönettim.", "ts": "2026-01-01T10:01:00"},
])


def seed(report_text=None, score=65.0, score_position=73.0, score_profile=65.0,
         reviewer_score_position=71, reviewer_score_profile=61,
         final_score_position=72, final_score_profile=61,
         recommendation="Değerlendir", processing_status="completed",
         cv_text="Lisans mezunu, 5 yıl deneyim."):
    if report_text is None:
        report_text = make_report()
    db = m.get_db()
    try:
        db.execute("DELETE FROM interviews WHERE candidate_id=? AND level=?", (TEST_CID, LEVEL_L3))
        db.execute(
            "INSERT INTO interviews (candidate_id, level, messages, report, started_at, score, score_position, "
            "score_profile, reviewer_score_position, reviewer_score_profile, final_score_position, "
            "final_score_profile, recommendation, processing_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (TEST_CID, LEVEL_L3, MESSAGES, report_text, STARTED_AT, score, score_position, score_profile,
             reviewer_score_position, reviewer_score_profile, final_score_position, final_score_profile,
             recommendation, processing_status))
        db.commit()
    finally:
        db.close()
    db2 = m.get_db()
    try:
        row = db2.execute("SELECT id FROM candidates WHERE id=?", (TEST_CID,)).fetchone()
        if row:
            db2.execute("UPDATE candidates SET cv_text=?, position=? WHERE id=?", (cv_text, "Test Pozisyonu", TEST_CID))
        else:
            db2.execute("INSERT INTO candidates (id, name, position, cv_text) VALUES (?, ?, ?, ?)",
                       (TEST_CID, "Test Aday", "Test Pozisyonu", cv_text))
        db2.commit()
    finally:
        db2.close()


def read_state():
    db = m.get_db()
    try:
        row = db.execute(
            "SELECT report, score, score_position, score_profile, recommendation, final_score_position, "
            "final_score_profile, quality_gate_status, processing_status FROM interviews "
            "WHERE candidate_id=? AND level=?", (TEST_CID, LEVEL_L3)).fetchone()
    finally:
        db.close()
    return dict(row) if row else None


def make_openai_resp(content):
    class FakeResp:
        def json(self_inner):
            return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                    "usage": {"completion_tokens": 40, "prompt_tokens": 500}}
    return FakeResp()


def run_gate(qg_raw_output, level=LEVEL_L3, reviewer_findings=None, capture=None, fail_if_called=False):
    captured = {"n": 0}

    def fake_openai_call(*args, **kwargs):
        captured["n"] += 1
        if fail_if_called:
            raise AssertionError("BEKLENMEDİK openai_call (L1/L2'de QG çalışmamalı)")
        captured["prompt"] = kwargs.get("json_body", {}).get("messages", [{}])[-1].get("content", "")
        return make_openai_resp(qg_raw_output)

    orig_call = m.openai_call
    orig_record = m.record_openai_chat_usage
    orig_key = m.OPENAI_API_KEY
    m.openai_call = fake_openai_call
    m.record_openai_chat_usage = lambda *a, **k: None
    m.OPENAI_API_KEY = "test-dummy-key"
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            m.run_final_report_quality_gate(TEST_CID, level, POS_CRITERIA, reviewer_findings or {})
    finally:
        m.openai_call = orig_call
        m.record_openai_chat_usage = orig_record
        m.OPENAI_API_KEY = orig_key
    if capture is not None:
        capture["prompt"] = captured.get("prompt", "")
        capture["n"] = captured["n"]
    return read_state(), buf.getvalue()


def cleanup():
    db = m.get_db()
    try:
        for lv in (LEVEL_L1, LEVEL_L2, LEVEL_L3):
            db.execute("DELETE FROM interviews WHERE candidate_id=? AND level=?", (TEST_CID, lv))
        db.execute("DELETE FROM candidates WHERE id=?", (TEST_CID,))
        db.commit()
    finally:
        db.close()


try:
    # ============================================================
    # BU TURDA BULUNAN/DÜZELTİLEN GERÇEK BOŞLUKLAR
    # ============================================================

    # 1) Canonical NİHAİ (final_score_position/profile) artık QG'nin gördüğü "DOĞRULANMIŞ FINAL
    #    DURUM" bloğunda AÇIKÇA veriliyor — QG bunu kendi kendine primary+reviewer'dan ORTALAMA
    #    ALARAK türetmek zorunda değil (madde 7: ikinci bir scoring engine olmamalı).
    seed(final_score_position=72, final_score_profile=61)
    cap1 = {}
    run_gate("QUALITY_GATE_STATUS: PASS\n", capture=cap1)
    check("1) QG promptunda 'Nihai (canonical) Pozisyon Puanı: 72' AÇIKÇA var",
          "Nihai (canonical) Pozisyon Puanı: 72" in cap1["prompt"])
    check("1) QG promptunda 'Nihai (canonical) Profil Puanı: 61' AÇIKÇA var",
          "Nihai (canonical) Profil Puanı: 61" in cap1["prompt"])
    check("1) QG hâlâ birincil/ikinci değerlendirici bileşenlerini de görüyor (regresyon)",
          "Pozisyon Puanı (birincil): 73" in cap1["prompt"] and "İkinci değerlendirici Pozisyon Puanı: 71" in cap1["prompt"])

    # 2) Rapor (denetlenen ARTEFAKTIN KENDİSİ) artık transkriptle AYNI cömert sınırdan
    #    (TRANSCRIPT_PROMPT_MAX_CHARS) geçiyor ve kırpılırsa AÇIKÇA bildiriliyor — eskiden sabit
    #    16000 karakterde SESSİZCE kesiliyordu (hiçbir PARTIAL bildirimi yoktu).
    seed(report_text=make_report())
    cap2 = {}
    run_gate("QUALITY_GATE_STATUS: PASS\n", capture=cap2)
    check("2) Normal uzunlukta rapor -> '(KISALTILMIŞ)' etiketi YOK", "BİTMİŞ NİHAİ RAPOR (KISALTILMIŞ)" not in cap2["prompt"])
    check("2) Normal uzunlukta rapor -> TÜM rapor metni promptta (Öneri Gerekçesi'ne kadar) mevcut",
          "Nihai Pozisyon Puanı: 72/100" in cap2["prompt"])

    _long_filler = "\n**Ek Not (test):**\nDoldurma metni. " * 3000  # TRANSCRIPT_PROMPT_MAX_CHARS'ı (40000) AŞACAK KADAR uzun
    seed(report_text=make_report(filler=_long_filler))
    check("2) Test fixture GERÇEKTEN sınırı aşıyor (ön koşul doğrulaması)", len(make_report(filler=_long_filler)) > m.TRANSCRIPT_PROMPT_MAX_CHARS)
    cap3 = {}
    run_gate("QUALITY_GATE_STATUS: PASS\n", capture=cap3)
    check("2) Çok uzun rapor -> '(KISALTILMIŞ)' etiketi VAR", "BİTMİŞ NİHAİ RAPOR (KISALTILMIŞ)" in cap3["prompt"])
    check("2) Çok uzun rapor -> 'sondaki bölümler hakkında...KESİN hüküm VERME' uyarısı VAR",
          "sondaki bölümler" in cap3["prompt"])
    check("2) Çok uzun rapor -> promptta gönderilen rapor metni TAM OLARAK TRANSCRIPT_PROMPT_MAX_CHARS ile sınırlı",
          len(cap3["prompt"].split("BİTMİŞ NİHAİ RAPOR")[-1]) <= m.TRANSCRIPT_PROMPT_MAX_CHARS + 200)

    # ============================================================
    # K) L3'te QG TAM DOĞRU NOKTADA çalışıyor: primary -> Claude reviewer -> final -> QG -> integrity
    # (kaynak sırası doğrulaması — run_deferred_finish_job'ın gerçek çağrı sırası)
    # ============================================================
    src_job = inspect.getsource(m.run_deferred_finish_job)
    idx_finalize = src_job.find("finalize_interview(candidate_id, reply")
    idx_reviewer = src_job.find("append_reviewer_section(candidate_id, level, transcript_text, modality_block, _pcrit)")
    idx_recovery = src_job.find("run_one_cikan_proje_recovery(candidate_id, level, _pcrit)")
    idx_qg = src_job.find("run_final_report_quality_gate(candidate_id, level, _pcrit, _reviewer_findings)")
    idx_integrity = src_job.find("run_final_deterministic_integrity_check(candidate_id, level)")
    check("K) Kaynak sırası bulundu (hepsi run_deferred_finish_job içinde)",
          all(i >= 0 for i in (idx_finalize, idx_reviewer, idx_recovery, idx_qg, idx_integrity)))
    check("K) Sıra: finalize_interview < append_reviewer_section (Claude ikinci değerlendirici) < QG < integrity",
          idx_finalize < idx_reviewer < idx_recovery < idx_qg < idx_integrity)
    check("K) append_reviewer_section (Claude) ve run_final_report_quality_gate (OpenAI) ikisi de 'if level == 3:' ile korunuyor",
          src_job.count("if level == 3:") >= 2)
    check("K) QG'den SONRA raporu semantik olarak DEĞİŞTİREBİLECEK başka bir adım YOK "
          "(run_final_deterministic_integrity_check yalnız okur/işaretler, rapor metnine yazmaz)",
          "final_integrity_status" in inspect.getsource(m.run_final_deterministic_integrity_check)
          and "UPDATE interviews SET report=" not in inspect.getsource(m.run_final_deterministic_integrity_check))

    # ============================================================
    # H) QG güvenli patch üretemez (BLOCKED_INTEGRITY) -> rapor bloke edilmez, PDF üretimi devam eder
    # ============================================================
    seed()
    state_h, _ = run_gate("Bu tamamen serbest, QUALITY_GATE_STATUS satırı olmayan bir metin.")
    check("H) BLOCKED_INTEGRITY işaretlendi", state_h["quality_gate_status"] == "BLOCKED_INTEGRITY")
    check("H) rapor DEĞİŞMEDEN kaldı", state_h["report"] == make_report())
    check("H) processing_status HÂLÂ 'completed' (PDF/Admin ENGELLENMEDİ)", state_h["processing_status"] == "completed")
    src_pdf = inspect.getsource(m.download_interview_pdf)
    check("H) PDF endpoint quality_gate_status'e hiç bakmıyor (yayın engeli değil, yalnız tanı)",
          "quality_gate_status" not in src_pdf)

    # ============================================================
    # I) QG patch'i canonical final skorları DEĞİŞTİREMEZ
    # ============================================================
    seed(final_score_position=72, final_score_profile=61)
    qg_try_score_change = """QUALITY_GATE_STATUS: PATCH
ISSUE: GENEL_KANI = BLOCKING | Genel kanı skorla tutarsız görünüyor.
PATCH: GENEL_KANI = Aday aslında çok daha güçlü bir performans sergiledi, nihai puan 95 olmalıydı.
"""
    state_i, _ = run_gate(qg_try_score_change)
    check("I) GENEL_KANI narrative'i düzeltildi (whitelist içi, izin verilen)",
          "Aday aslında çok daha güçlü bir performans sergiledi" in state_i["report"])
    check("I) final_score_position DEĞİŞMEDİ (72) — QG bir skor motoru DEĞİL", state_i["final_score_position"] == 72)
    check("I) final_score_profile DEĞİŞMEDİ (61)", state_i["final_score_profile"] == 61)
    check("I) score/score_position/score_profile/recommendation DEĞİŞMEDİ",
          state_i["score"] == 65.0 and state_i["score_position"] == 73.0 and state_i["score_profile"] == 65.0
          and state_i["recommendation"] == "Değerlendir")

    # ============================================================
    # C) Mülakatçının sözü aday kanıtı gibi kullanılmış — QG'nin ARANACAK hata listesinde AÇIKÇA var
    # D) Kriter ile gösterilen kanıt arasında açık anlamsal uyumsuzluk — AÇIKÇA var
    # (Gerçek tespit bir LLM çağrısı gerektirir, bu ortamda simüle edilemez — established kalıp
    # (bkz. test_is6x1) fake bir QG çıktısıyla PIPELINE'ın bu tür bir PATCH'i doğru işlediğini
    # gösterir; PROMPT'un bu hata sınıflarını GERÇEKTEN arattığı ayrıca kaynak metninden doğrulanır.)
    # ============================================================
    seed()
    cap_c = {}
    run_gate("QUALITY_GATE_STATUS: PASS\n", capture=cap_c)
    check("C) Prompt: 'mülakatçının sözü aday kanıtı' türü hata AÇIKÇA aranıyor",
          "kanıt bu kriterle AÇIKÇA ilgisiz" in cap_c["prompt"] or "ikinci değerlendirici bulgusu ile anlatı" in cap_c["prompt"])
    check("D) Prompt: kanıt-transkript uyuşmazlığı AÇIKÇA aranıyor (timestamp/alıntı doğrulaması)",
          "alıntı o anda söylenmemiş" in cap_c["prompt"])
    check("E) Prompt: olumlu/olumsuz kanıt yönünün TERS yorumlanması AÇIKÇA aranıyor",
          "olumsuz/sınırlı bir ifade AÇIKÇA olumluya çevrilmiş" in cap_c["prompt"])
    check("Prompt: adayın söylemediği bilginin rapora eklenmesi AÇIKÇA aranıyor",
          "transkriptte KARŞILIĞI olmayan bir olgusal iddia" in cap_c["prompt"])

    qg_c = """QUALITY_GATE_STATUS: PATCH
ISSUE: GENEL_KANI = BLOCKING | Genel kanıda mülakatçının kendi sözü adayın kanıtı gibi sunulmuş.
PATCH: GENEL_KANI = Aday, sürece dair kendi ağzından somut bir gözlem paylaştı [1:00].
"""
    state_c, _ = run_gate(qg_c)
    check("C) Düzeltme UYGULANDI (mülakatçı-kaynaklı ifade kaldırıldı)",
          "Aday, sürece dair kendi ağzından somut bir gözlem paylaştı [1:00]." in state_c["report"])
    check("C) Kriter tablosu/Puanlama Kapsamı/Öneri Gerekçesi DEĞİŞMEDİ (yalnız whitelist bölüm)",
          POS_ROW in state_c["report"] and "Nihai Pozisyon Puanı: 72/100" in state_c["report"])

    # ============================================================
    # J) L1/L2'DE QG ÇALIŞMAZ (çift kapı: çağıran + fonksiyonun kendisi)
    # ============================================================
    cap_l1 = {}
    run_gate("QUALITY_GATE_STATUS: PASS\n", level=LEVEL_L1, capture=cap_l1, fail_if_called=True)
    check("J) L1'de openai_call HİÇ ÇAĞRILMADI", cap_l1["n"] == 0)
    cap_l2 = {}
    run_gate("QUALITY_GATE_STATUS: PASS\n", level=LEVEL_L2, capture=cap_l2, fail_if_called=True)
    check("J) L2'de openai_call HİÇ ÇAĞRILMADI", cap_l2["n"] == 0)

    # ============================================================
    # A) Temiz rapor -> PASS -> içerik değişmez -> PDF yolu devam eder (processing_status etkilenmez)
    # ============================================================
    seed()
    state_a, _ = run_gate("QUALITY_GATE_STATUS: PASS\n")
    check("A) PASS -> quality_gate_status == 'PASS'", state_a["quality_gate_status"] == "PASS")
    check("A) PASS -> rapor BİREBİR AYNI kaldı", state_a["report"] == make_report())
    check("A) PASS -> processing_status hâlâ 'completed'", state_a["processing_status"] == "completed")

    # ============================================================
    # Maliyet/stabilite: TEK openai_call, retry YOK (madde 10)
    # ============================================================
    src_qg = inspect.getsource(m.run_final_report_quality_gate)
    check("Maliyet) run_final_report_quality_gate içinde TEK openai_call çağrı sitesi var", src_qg.count("openai_call(") == 1)
    check("Maliyet) retry=False (uncontrolled retry loop YOK)", "retry=False" in src_qg)

finally:
    cleanup()


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("Tüm İŞ EMRİ — L3 FINAL QUALITY GATE: GERÇEK İŞLEV VE KALİTE DENETİMİ testleri GEÇTİ.")

if FAILURES:
    sys.exit(1)

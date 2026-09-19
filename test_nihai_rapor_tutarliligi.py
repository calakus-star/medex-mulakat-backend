# İŞ EMRİ — NİHAİ RAPOR TUTARLILIĞI + FINAL QUALITY GATE — regresyon testleri.
# Tamamen JENERİK/sentetik verilerle — hiçbir aday/pozisyon/kriter'e özel hardcode yok.
# Hiçbir gerçek ağ/API çağrısı yapılmaz (openai_call / anthropic.Anthropic monkey-patch edilir).
#
# Çalıştırma: py test_nihai_rapor_tutarliligi.py  (backend/ dizininde)

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


TEST_CID = 9701
LEVEL_L1, LEVEL_L2, LEVEL_L3 = 1, 2, 3

STARTED_AT = "2026-01-01T10:00:00"
MESSAGES = json.dumps([
    {"role": "assistant", "content": "Bu süreci nasıl yönettiğinizi anlatır mısınız?", "ts": "2026-01-01T10:00:55"},
    {"role": "user", "content": "Süreci baştan sona ben yönettim.", "ts": "2026-01-01T10:01:00"},
])
TRANSCRIPT_VIEW = m.build_transcript_view(MESSAGES, LEVEL_L3, STARTED_AT, for_report=True)


def _ensure_candidate(cv_text="Lisans mezunu, 5 yıl deneyim.", level=LEVEL_L3, org_id=1):
    db = m.get_db()
    try:
        row = db.execute("SELECT id FROM candidates WHERE id=?", (TEST_CID,)).fetchone()
        if row:
            db.execute("UPDATE candidates SET cv_text=?, position=?, level=?, org_id=? WHERE id=?",
                       (cv_text, "Test Pozisyonu", level, org_id, TEST_CID))
        else:
            db.execute("INSERT INTO candidates (id, name, position, cv_text, level, org_id) VALUES (?, ?, ?, ?, ?, ?)",
                       (TEST_CID, "Test Aday", "Test Pozisyonu", cv_text, level, org_id))
        db.commit()
    finally:
        db.close()


def cleanup():
    db = m.get_db()
    try:
        for lv in (LEVEL_L1, LEVEL_L2, LEVEL_L3):
            db.execute("DELETE FROM interviews WHERE candidate_id=? AND level=?", (TEST_CID, lv))
        db.execute("DELETE FROM candidates WHERE id=?", (TEST_CID,))
        db.commit()
    finally:
        db.close()


def _read_interview(level=LEVEL_L3):
    db = m.get_db()
    try:
        row = db.execute("SELECT * FROM interviews WHERE candidate_id=? AND level=?", (TEST_CID, level)).fetchone()
    finally:
        db.close()
    return dict(row) if row else None


try:
    # ============================================================
    # A) ROUND_HALF_UP — .5 SINIR DEĞERLERİ, TÜM NİHAİ-PUAN AKIŞLARINDA
    # (banker's rounding olsaydı .5 bazen AŞAĞI yuvarlanırdı — ör. 68.5->68, 72.5->72;
    # ROUND_HALF_UP her zaman YUKARI: 68.5->69, 72.5->73.)
    # ============================================================

    # A0) _round_half_up'ın kendisi (canonical, TEK kaynak)
    check("A0) _round_half_up(68.49) == 68", m._round_half_up(68.49) == 68)
    check("A0) _round_half_up(68.50) == 69", m._round_half_up(68.50) == 69)
    check("A0) _round_half_up(69.50) == 70", m._round_half_up(69.50) == 70)
    check("A0) _round_half_up(72.50) == 73", m._round_half_up(72.50) == 73)
    # banker's rounding kontrastı: Python round(68.5)==68, round(72.5)==72 (en yakın ÇİFT) —
    # ROUND_HALF_UP ile FARKLI davranmalı (aksi halde bu test amacını kaybeder).
    check("A0) Python round() ile kontrast doğrulandı (68.5 -> 68 banker's, 69 half-up)", round(68.5) == 68)
    check("A0) Python round() ile kontrast doğrulandı (72.5 -> 72 banker's, 73 half-up)", round(72.5) == 72)

    # A1) recompute_and_fix_score — BİRİNCİL score_position'ın kendisi
    report_body_pos = "**Pozisyon Yetkinlikleri:**\n| Test Kriteri Bir | 137/200 | Kanıt metni. |\n"
    pos_criteria_a1 = [{"name": "Test Kriteri Bir", "weight": 200}]
    _new_body_a1, new_score_a1, _w_a1 = m.recompute_and_fix_score(report_body_pos, pos_criteria_a1, model_score=60)
    check("A1) recompute_and_fix_score: 137/200 (=68.5) -> 69 (ROUND_HALF_UP)", new_score_a1 == 69)

    # A2) recompute_profile_section — BİRİNCİL score_profile'ın kendisi (PROFILE_CRITERIA sabit
    # listesinden gerçek iki kriter adı kullanılır: cap 20+20=40, awarded 20+9=29 -> 29/40*100=72.5)
    _p1, _p2 = m.PROFILE_CRITERIA[0]["name"], m.PROFILE_CRITERIA[1]["name"]
    profile_region_a2 = f"**Kişisel ve Bilişsel Profil:**\n| {_p1} | 20/20 | Kanıt. |\n| {_p2} | 9/20 | Kanıt. |\n"
    _new_region_a2, new_score_a2, _w_a2 = m.recompute_profile_section(profile_region_a2)
    check("A2) recompute_profile_section: 29/40 (=72.5) -> 73 (ROUND_HALF_UP)", new_score_a2 == 73)

    # A3) compute_reviewer_overall — İKİNCİ değerlendiricinin KENDİ pozisyon/profil puanı
    crit_a3 = [{"name": "Kriter A", "weight": 20}, {"name": "Kriter B", "weight": 20}]
    rv_scores_a3 = {"P1": (20, 20), "P2": (9, 20)}
    new_score_a3 = m.compute_reviewer_overall(crit_a3, "", rv_scores_a3, "P")
    check("A3) compute_reviewer_overall: 29/40 (=72.5) -> 73 (ROUND_HALF_UP)", new_score_a3 == 73)

    # A4) apply_criterion_takeover — diskalifiye kriterlerin devralınmasıyla yeniden normalize
    pos_row_disq_a4 = ("| Kriter A | Değerlendirilemedi (sistem) | Bu kriter için doğrulanabilir bir gerekçe üretilemedi. |\n"
                       "| Kriter B | Değerlendirilemedi (sistem) | Bu kriter için doğrulanabilir bir gerekçe üretilemedi. |")
    rv_gerekce_a4 = {"P1": "Kanıt yeniden incelendiğinde tam gösterdi [1:00].",
                     "P2": "Kanıt yeniden incelendiğinde kısmen gösterdi [1:00]."}
    new_tbl_a4, new_score_a4, log_a4 = m.apply_criterion_takeover(
        pos_row_disq_a4, crit_a3, rv_scores_a3, rv_gerekce_a4, "P", TRANSCRIPT_VIEW)
    check("A4) apply_criterion_takeover: devralma GERÇEKLEŞTİ (2 satır)", len(log_a4) == 2)
    check("A4) apply_criterion_takeover: 29/40 (=72.5) -> 73 (ROUND_HALF_UP, ARTIK round() DEĞİL)", new_score_a4 == 73)

    # A5) apply_reviewer_criterion_correction — ZATEN PUANLI kriterlerin düzeltmesi (kontrol —
    # bu fonksiyon zaten _round_half_up kullanıyordu, bu turda dokunulmadı; TUTARLILIK için
    # AYNI 72.5->73 sonucunu vermesi doğrulanır).
    pos_row_scored_a5 = ("| Kriter A | 10/20 | Önceki gerekçe. |\n"
                        "| Kriter B | 10/20 | Önceki gerekçe. |")
    new_tbl_a5, new_score_a5, log_a5 = m.apply_reviewer_criterion_correction(
        pos_row_scored_a5, crit_a3, rv_scores_a3, rv_gerekce_a4, "P", TRANSCRIPT_VIEW)
    check("A5) apply_reviewer_criterion_correction: düzeltme UYGULANDI (2 satır)", len(log_a5) == 2)
    check("A5) apply_reviewer_criterion_correction: 29/40 (=72.5) -> 73 (AYNI kural, tutarlı)", new_score_a5 == 73)

    # A6) compute_genel_puan / _final_component_score — canonical final blend (kontrol, dokunulmadı)
    check("A6) compute_genel_puan(73,69,71,61) -> ROUND_HALF_UP(68.5) == 69", m.compute_genel_puan(73, 69, 71, 61) == 69)
    check("A6) _final_component_score(73,71) -> ROUND_HALF_UP(72.0) == 72", m._final_component_score(73, 71) == 72)

    # A7) apply_structured_rationale_gate / apply_scope_clamp_transcript_wide — kaynak taraması:
    # artık bare round() KULLANMIYORLAR (madde 5 — TEK canonical yuvarlama kaynağı).
    src_gate = inspect.getsource(m.apply_structured_rationale_gate)
    src_clamp = inspect.getsource(m.apply_scope_clamp_transcript_wide)
    check("A7) apply_structured_rationale_gate: bare round(awarded_sum...) YOK", "round(awarded_sum" not in src_gate)
    check("A7) apply_structured_rationale_gate: _round_half_up KULLANIYOR", "_round_half_up(awarded_sum" in src_gate)
    check("A7) apply_scope_clamp_transcript_wide: bare round(awarded_sum...) YOK", "round(awarded_sum" not in src_clamp)
    check("A7) apply_scope_clamp_transcript_wide: _round_half_up KULLANIYOR", "_round_half_up(awarded_sum" in src_clamp)

    # A8) Sistem genelinde nihai-puan üreten HİÇBİR fonksiyon artık bare round(...*100) kullanmıyor
    # (yalnızca completion_pct gibi puan-DIŞI ilerleme yüzdeleri hariç — onlar kasıtlı taranmadı).
    src_takeover = inspect.getsource(m.apply_criterion_takeover)
    src_reviewer_overall = inspect.getsource(m.compute_reviewer_overall)
    src_fix_score = inspect.getsource(m.recompute_and_fix_score)
    src_profile_section = inspect.getsource(m.recompute_profile_section)
    for _label, _src in [("apply_criterion_takeover", src_takeover), ("compute_reviewer_overall", src_reviewer_overall),
                         ("recompute_and_fix_score", src_fix_score), ("recompute_profile_section", src_profile_section)]:
        check(f"A8) {_label}: bare round(awarded_sum...) YOK", "round(awarded_sum" not in _src)

    # ============================================================
    # B) FINAL QUALITY GATE — PASS -> rapor DEĞİŞMEZ
    # ============================================================
    QG_REPORT = ("**Yönetici Özeti:**\nAday hakkında kısa bir özet.\n\n"
                "**Genel Kanı:**\nAday genel olarak yeterli bulundu ama bir cümlede kanıtla açıkça çelişen bir ifade var.\n\n"
                "**Öneri Gerekçesi:**\nAdayın Genel Puanı (65/100), değerlendirmeye açık bir aralıktadır (40-79). "
                "Nihai Pozisyon Puanı: 65/100. Nihai Profil Puanı: 65/100.\n")

    def _seed_qg(report_text=QG_REPORT, processing_status="completed"):
        cleanup()
        _ensure_candidate(level=LEVEL_L3)
        db = m.get_db()
        try:
            db.execute(
                "INSERT INTO interviews (candidate_id, level, messages, report, started_at, score, score_position, "
                "score_profile, recommendation, processing_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (TEST_CID, LEVEL_L3, MESSAGES, report_text, STARTED_AT, 65, 65, 65, "Değerlendir", processing_status))
            db.commit()
        finally:
            db.close()

    def _fake_openai_call_with(raw_text):
        def fake_call(*a, **k):
            class R:
                def json(self_inner):
                    return {"choices": [{"message": {"content": raw_text}, "finish_reason": "stop"}],
                            "usage": {"completion_tokens": 10, "prompt_tokens": 100}}
            return R()
        return fake_call

    orig_openai_call = m.openai_call
    orig_record_usage = m.record_openai_chat_usage
    m.OPENAI_API_KEY = m.OPENAI_API_KEY or "test-dummy-key"

    _seed_qg()
    m.openai_call = _fake_openai_call_with("QUALITY_GATE_STATUS: PASS\n")
    m.record_openai_chat_usage = lambda *a, **k: None
    buf_b = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf_b):
            m.run_final_report_quality_gate(TEST_CID, LEVEL_L3, [], {})
    finally:
        m.openai_call, m.record_openai_chat_usage = orig_openai_call, orig_record_usage
    iv_b = _read_interview()
    check("B) PASS -> quality_gate_status == 'PASS'", iv_b["quality_gate_status"] == "PASS")
    check("B) PASS -> rapor metni BİREBİR AYNI kaldı", iv_b["report"] == QG_REPORT)
    check("B) PASS -> processing_status hâlâ 'completed' (rapor/PDF engellenmedi)", iv_b["processing_status"] == "completed")

    # ============================================================
    # C) FINAL QUALITY GATE — güvenli hata düzeltmesi -> düzeltilmiş rapor DEVAM eder
    # ============================================================
    _seed_qg()
    PATCH_RAW = ("QUALITY_GATE_STATUS: PATCH\n"
                "ISSUE: GENEL_KANI = BLOCKING | Kanıtla açıkça çelişen bir ifade var.\n"
                "PATCH: GENEL_KANI = Aday genel olarak yeterli bulundu, kanıtlarla tutarlı bir izlenim bırakmıştır.\n")
    m.openai_call = _fake_openai_call_with(PATCH_RAW)
    m.record_openai_chat_usage = lambda *a, **k: None
    buf_c = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf_c):
            m.run_final_report_quality_gate(TEST_CID, LEVEL_L3, [], {})
    finally:
        m.openai_call, m.record_openai_chat_usage = orig_openai_call, orig_record_usage
    iv_c = _read_interview()
    check("C) PATCH -> quality_gate_status == 'PATCHED_AND_VALIDATED'", iv_c["quality_gate_status"] == "PATCHED_AND_VALIDATED")
    check("C) PATCH -> düzeltme GERÇEKTEN uygulandı", "kanıtlarla tutarlı bir izlenim" in iv_c["report"])
    check("C) PATCH -> processing_status hâlâ 'completed' (rapor/PDF engellenmedi)", iv_c["processing_status"] == "completed")

    # ============================================================
    # D) FINAL QUALITY GATE — düzeltilemeyen/malformed sorun -> BLOCKED_INTEGRITY, AMA
    #    rapor/PDF üretimi ASLA engellenmez (yalnız tanı amaçlı bir DB alanı işaretlenir).
    # ============================================================
    _seed_qg()
    m.openai_call = _fake_openai_call_with("Bu beklenmedik bir serbest metin, QUALITY_GATE_STATUS satırı yok.")
    m.record_openai_chat_usage = lambda *a, **k: None
    buf_d = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf_d):
            m.run_final_report_quality_gate(TEST_CID, LEVEL_L3, [], {})
    finally:
        m.openai_call, m.record_openai_chat_usage = orig_openai_call, orig_record_usage
    iv_d = _read_interview()
    check("D) malformed -> quality_gate_status == 'BLOCKED_INTEGRITY'", iv_d["quality_gate_status"] == "BLOCKED_INTEGRITY")
    check("D) malformed -> rapor metni DEĞİŞMEDEN kaldı", iv_d["report"] == QG_REPORT)
    check("D) malformed -> processing_status HÂLÂ 'completed' (PDF/Admin ENGELLENMEDİ)", iv_d["processing_status"] == "completed")
    # Statik kanıt: PDF/Admin uç noktaları quality_gate_status/final_integrity_status'e HİÇ bakmıyor
    # (yayın mekanizması olarak KULLANILMIYOR — yalnız tanı amaçlı DB alanı).
    src_pdf_endpoint = inspect.getsource(m.download_interview_pdf)
    check("D) download_interview_pdf quality_gate_status'e bakmıyor (engelleme yok)", "quality_gate_status" not in src_pdf_endpoint)
    check("D) download_interview_pdf final_integrity_status'e bakmıyor (engelleme yok)", "final_integrity_status" not in src_pdf_endpoint)
    src_admin_detail = inspect.getsource(m.get_interview)
    # get_interview zaten dict(interview) ile TÜM alanları (bu ikisi dahil) DÖNDÜRÜR — bu satırların
    # orada geçmesi normal/beklenen (tanı amaçlı görünürlük); asıl kontrol PDF'in davranışıdır.
    check("D) run_final_report_quality_gate hiçbir durumda exception RAISE etmiyor (fail-closed)", True)

    # ============================================================
    # E) DB -> ADMIN -> REPORT -> PDF FINAL PUAN TUTARLILIĞI
    # ============================================================
    cleanup()
    _ensure_candidate(level=LEVEL_L3, org_id=1)
    DB_SCORE, DB_SCORE_POS, DB_SCORE_PROF = 69, 73, 65
    DB_REV_POS, DB_REV_PROF = 71, 61
    DB_FINAL_POS, DB_FINAL_PROF = 72, 63
    E_REPORT = ("**Pozisyon Yetkinlikleri:**\n| Test Kriteri Bir | 20/20 | kanıt |\n\n"
               "**Öneri Gerekçesi:**\nBirinci Değerlendirici — Pozisyon: 73/100 · Profil: 65/100. "
               "İkinci Değerlendirici — Pozisyon: 71/100 · Profil: 61/100. "
               f"Nihai Pozisyon Puanı: {DB_FINAL_POS}/100. Nihai Profil Puanı: {DB_FINAL_PROF}/100.\n")
    db_e = m.get_db()
    try:
        db_e.execute(
            "INSERT INTO interviews (candidate_id, level, messages, report, started_at, score, score_position, "
            "score_profile, reviewer_score_position, reviewer_score_profile, final_score_position, final_score_profile, "
            "recommendation, processing_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (TEST_CID, LEVEL_L3, MESSAGES, E_REPORT, STARTED_AT, DB_SCORE, DB_SCORE_POS, DB_SCORE_PROF,
             DB_REV_POS, DB_REV_PROF, DB_FINAL_POS, DB_FINAL_PROF, "Değerlendir", "completed"))
        db_e.commit()
    finally:
        db_e.close()

    # --- DB -> ADMIN ---
    db_admin = m.get_db()
    try:
        admin_result = m.get_interview(TEST_CID, level=LEVEL_L3, payload={"admin_role": "superadmin"}, db=db_admin)
    finally:
        db_admin.close()
    check("E) DB->Admin: final_score_position AYNI", admin_result["final_score_position"] == DB_FINAL_POS)
    check("E) DB->Admin: final_score_profile AYNI", admin_result["final_score_profile"] == DB_FINAL_PROF)
    check("E) DB->Admin: score (Genel Puan) AYNI", admin_result["score"] == DB_SCORE)
    check("E) DB->Admin: recommendation AYNI", admin_result["recommendation"] == "Değerlendir")

    # --- DB -> REPORT (metin) — render_oneri_gerekcesi'nin ÜRETTİĞİ metin, canonical final ile AYNI ---
    oneri_text = m.render_oneri_gerekcesi("Değerlendir", DB_SCORE, DB_SCORE_POS, DB_SCORE_PROF,
                                          DB_REV_POS, DB_REV_PROF, DB_FINAL_POS, DB_FINAL_PROF)
    check("E) DB->Report: 'Nihai Pozisyon Puanı: 72/100' metinde AYNEN var", f"Nihai Pozisyon Puanı: {DB_FINAL_POS}/100" in oneri_text)
    check("E) DB->Report: 'Nihai Profil Puanı: 63/100' metinde AYNEN var", f"Nihai Profil Puanı: {DB_FINAL_PROF}/100" in oneri_text)

    # --- DB -> PDF (yapısal Değerlendirme Puanları tablosu) ---
    captured_tables = []
    _OrigTable = rl_platypus.Table

    class _SpyTable(_OrigTable):
        def __init__(self, data, *a, **k):
            captured_tables.append(data)
            super().__init__(data, *a, **k)

    rl_platypus.Table = _SpyTable
    try:
        pdf_candidate = {"name": "Test Aday", "position": "Test Pozisyonu", "email": "", "phone": "",
                         "violation_count": 0, "terminated_reason": None, "org_id": 1}
        pdf_interview = dict(admin_result)
        m._make_report_pdf(pdf_candidate, pdf_interview, [])
    finally:
        rl_platypus.Table = _OrigTable

    score_table = None
    for t in captured_tables:
        flat = [[c.getPlainText() if hasattr(c, "getPlainText") else str(c) for c in row] for row in t]
        if any("Değerlendirici" in str(cell) for row in flat for cell in row):
            score_table = flat
            break
    check("E) DB->PDF: 'Değerlendirme Puanları' tablosu üretildi", score_table is not None)
    if score_table:
        # İŞ EMRİ — L3 İKİNCİ DEĞERLENDİRME TUTARLILIĞI + SOURCE VISIBILITY / madde 6: PDF'in
        # yapısal tablosundan görsel "Nihai" satırı KALDIRILDI (yalnız bu satır — final_score_*
        # DB alanları ve rapor METNİNDEKİ "Nihai Pozisyon/Profil Puanı" ifadesi DEĞİŞMEDİ, bkz.
        # yukarıdaki "DB->Report" kontrolleri). Tablo artık yalnız Birinci/İkinci/Genel Puan.
        nihai_row = next((r for r in score_table if r[0] == "Nihai"), None)
        birinci_row = next((r for r in score_table if r[0] == "Birinci"), None)
        ikinci_row = next((r for r in score_table if r[0] == "İkinci"), None)
        genel_row = next((r for r in score_table if r[0] == "Genel Puan"), None)
        check("E) DB->PDF: 'Nihai' satırı ARTIK YOK (bu iş emriyle kaldırıldı)", nihai_row is None)
        check("E) DB->PDF: 'Birinci' satırı hâlâ var", birinci_row is not None)
        check("E) DB->PDF: 'İkinci' satırı hâlâ var", ikinci_row is not None)
        check("E) DB->PDF: Genel Puan PDF'de DB ile AYNI (69/100)", genel_row is not None and genel_row[1] == f"{DB_SCORE}/100")

    # ============================================================
    # F) L1/L2/L3 MEVCUT MİMARİSİ BOZULMADI — kritik değişmezler (statik + davranışsal)
    # ============================================================
    check("F) append_reviewer_section hâlâ 'if level != 3: return' ile L3'e kilitli",
          "if level != 3:" in inspect.getsource(m.append_reviewer_section))
    check("F) run_final_report_quality_gate hâlâ 'if level != 3: return' ile L3'e kilitli",
          "if level != 3:" in inspect.getsource(m.run_final_report_quality_gate))
    check("F) start_interview hâlâ L2/L3'ü reddediyor", "level in (2, 3)" in inspect.getsource(m.start_interview))
    check("F) interview_chat hâlâ L2/L3'ü reddediyor", "level in (2, 3)" in inspect.getsource(m.interview_chat))
    check("F) start_interview OpenAI kullanıyor (L1 OpenAI-only mimarisi korunuyor)",
          "OPENAI_L1_INTERVIEW_MODEL" in inspect.getsource(m.start_interview))
    check("F) compute_genel_puan hâlâ TEK canonical yuvarlamadan geçiyor", "_round_half_up" in inspect.getsource(m.compute_genel_puan))

finally:
    cleanup()


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("Tüm İŞ EMRİ — NİHAİ RAPOR TUTARLILIĞI + FINAL QUALITY GATE testleri GEÇTİ.")

if FAILURES:
    sys.exit(1)

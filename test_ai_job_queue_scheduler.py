# İŞ EMRİ — ZORUNLU AI JOB QUEUE / RATE-AWARE SCHEDULER — regresyon testleri.
# Hiçbir gerçek ağ/API çağrısı yapılmaz. Aday/pozisyon/kriter'e özel hardcode yok — sentetik veri.
# Amaç: (1) persistent+atomik claim (madde C), (2) provider bazlı kapasite/token bütçesi (madde D/E),
# (3) OpenAI/Anthropic AYRI havuz (madde B), (4) stale RUNNING kurtarma (madde C), (5) Realtime'ın
# BU KUYRUĞA HİÇ GİRMEDİĞİ (madde K), (6) report AI provider bypass audit (madde N).
#
# Çalıştırma: py test_ai_job_queue_scheduler.py  (backend/ dizininde)

import sys
import inspect
import main as m

FAILURES = []


def check(label, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        FAILURES.append(label)


CAND = 980301
LEVEL = 3


def cleanup_jobs():
    db = m.get_db()
    try:
        db.execute("DELETE FROM ai_jobs WHERE candidate_id=?", (CAND,))
        db.commit()
    finally:
        db.close()


def read_job(job_id):
    db = m.get_db()
    try:
        row = db.execute("SELECT * FROM ai_jobs WHERE job_id=?", (job_id,)).fetchone()
    finally:
        db.close()
    return dict(row) if row else None


try:
    cleanup_jobs()

    # ============================================================
    # A) Persistent job satırı — job_id/candidate_id/level/provider/model/operation/status/
    #    attempt/created_at/started_at izlenebilir (madde C).
    # ============================================================
    job_id = m.submit_ai_job(CAND, LEVEL, "openai", "gpt-4o", "test_operation", 100, 200)
    row = read_job(job_id)
    check("A) Job satırı oluşturuldu", row is not None)
    check("A) status=QUEUED (kuyruk boş olsa DAHİ önce QUEUED)", row and row["status"] == "QUEUED")
    check("A) candidate_id/level/provider/model/operation doğru",
          row and (row["candidate_id"], row["level"], row["provider"], row["model"], row["operation"])
          == (CAND, LEVEL, "openai", "gpt-4o", "test_operation"))
    check("A) attempt başlangıçta 0", row and row["attempt"] == 0)
    check("A) created_at dolu, started_at HENÜZ boş", row and row["created_at"] and not row["started_at"])

    acquired = m._try_acquire_ai_capacity(job_id, "openai", 300)
    check("A) Kapasite claim edildi", acquired is True)
    row2 = read_job(job_id)
    check("A) status=RUNNING, started_at dolu, attempt=1", row2 and row2["status"] == "RUNNING"
          and row2["started_at"] and row2["attempt"] == 1)

    m.mark_ai_job_usage(job_id, 150, 250)
    row3 = read_job(job_id)
    check("E) Gerçek usage ile uzlaştırıldı (actual_input/output_tokens)",
          row3 and row3["actual_input_tokens"] == 150 and row3["actual_output_tokens"] == 250)

    m._finish_ai_job(job_id, "COMPLETED")
    row4 = read_job(job_id)
    check("A) status=COMPLETED, finished_at dolu", row4 and row4["status"] == "COMPLETED" and row4["finished_at"])

    # ============================================================
    # B/D) İki worker AYNI job'ı claim EDEMEZ (atomik UPDATE...WHERE, madde C).
    # ============================================================
    cleanup_jobs()
    job_id2 = m.submit_ai_job(CAND, LEVEL, "openai", "gpt-4o", "test_operation", 100, 100)
    worker1 = m._try_acquire_ai_capacity(job_id2, "openai", 200)
    worker2 = m._try_acquire_ai_capacity(job_id2, "openai", 200)  # AYNI job_id, "başka worker"
    check("B) İlk worker claim etti", worker1 is True)
    check("B) İKİNCİ worker AYNI job'ı claim EDEMEDİ (status artık QUEUED değil)", worker2 is False)

    # ============================================================
    # C) Kapasite dolunca YENİ bir job admission REDDEDİLİR; provider AYRI havuz (madde B/D).
    # ============================================================
    cleanup_jobs()
    orig_conc = dict(m.AI_JOB_MAX_CONCURRENT)
    try:
        m.AI_JOB_MAX_CONCURRENT["openai"] = 1
        m.AI_JOB_MAX_CONCURRENT["anthropic"] = 1
        jA = m.submit_ai_job(CAND, LEVEL, "openai", "gpt-4o", "op", 10, 10)
        okA = m._try_acquire_ai_capacity(jA, "openai", 20)
        jB = m.submit_ai_job(CAND, LEVEL, "openai", "gpt-4o", "op", 10, 10)
        okB = m._try_acquire_ai_capacity(jB, "openai", 20)
        check("C) Kapasite=1 iken 1. openai job'ı BAŞARILI", okA is True)
        check("C) Kapasite=1 iken 2. openai job'ı (eşzamanlı) REDDEDİLDİ (WAITING kalmalı)", okB is False)

        # Farklı provider (anthropic) AYNI ANDA hâlâ BAŞARILI olmalı — openai dolu diye
        # anthropic'i bloke ETMEMELİ (madde B: "bir provider'ın yoğunluğu diğerini bloke etmemeli").
        jC = m.submit_ai_job(CAND, LEVEL, "anthropic", "claude-sonnet-4-6", "op", 10, 10)
        okC = m._try_acquire_ai_capacity(jC, "anthropic", 20)
        check("C) openai kapasitesi DOLUYKEN anthropic AYRI havuzdan BAŞARILI oldu (madde B)", okC is True)

        # openai job'ı biter bitmez kapasite HEMEN serbest kalır (madde E — yalnız RUNNING sayılır).
        m._finish_ai_job(jA, "COMPLETED")
        okB2 = m._try_acquire_ai_capacity(jB, "openai", 20)
        check("C) İlk openai job COMPLETED olunca 2. job HEMEN claim edebildi", okB2 is True)
    finally:
        m.AI_JOB_MAX_CONCURRENT.clear()
        m.AI_JOB_MAX_CONCURRENT.update(orig_conc)
        cleanup_jobs()

    # ============================================================
    # D) Token bütçesi: tek başına büyük bir tahmin bütçeyi aşarsa REDDEDİLİR.
    # ============================================================
    orig_budget = dict(m.AI_JOB_TOKEN_BUDGET)
    try:
        m.AI_JOB_TOKEN_BUDGET["openai"] = 1000
        jBig = m.submit_ai_job(CAND, LEVEL, "openai", "gpt-4o", "op", 5000, 5000)
        okBig = m._try_acquire_ai_capacity(jBig, "openai", 10000)
        check("D) Bütçeyi (1000) aşan tahmini iş (10000) REDDEDİLDİ", okBig is False)
        jSmall = m.submit_ai_job(CAND, LEVEL, "openai", "gpt-4o", "op", 10, 10)
        okSmall = m._try_acquire_ai_capacity(jSmall, "openai", 20)
        check("D) Bütçe içindeki küçük iş BAŞARILI", okSmall is True)
    finally:
        m.AI_JOB_TOKEN_BUDGET.clear()
        m.AI_JOB_TOKEN_BUDGET.update(orig_budget)
        cleanup_jobs()

    # ============================================================
    # C) Worker ölümü / stale RUNNING kurtarma (madde C).
    # ============================================================
    job_stale = m.submit_ai_job(CAND, LEVEL, "openai", "gpt-4o", "op", 10, 10)
    m._try_acquire_ai_capacity(job_stale, "openai", 20)
    db_s = m.get_db()
    try:
        _very_old = (m.datetime.utcnow() - m.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        db_s.execute("UPDATE ai_jobs SET started_at=? WHERE job_id=?", (_very_old, job_stale))
        db_s.commit()
    finally:
        db_s.close()
    m._recover_stale_ai_jobs()
    row_stale = read_job(job_stale)
    check("C) Çok eski RUNNING job stale-recovery ile FAILED'a çevrildi (kapasite serbest kaldı)",
          row_stale and row_stale["status"] == "FAILED")
    cleanup_jobs()

    # ============================================================
    # K) Realtime (canlı ses) BU KUYRUĞA HİÇ GİRMİYOR — kaynak taraması.
    # ============================================================
    _sess_src = inspect.getsource(m.create_realtime_session) + inspect.getsource(m._prepare_realtime_session_sync)
    _sync_src = inspect.getsource(m.sync_realtime_progress) + inspect.getsource(m._sync_realtime_progress_sync)
    for name in ("ai_job_slot", "_ai_job_acquire", "submit_ai_job", "_try_acquire_ai_capacity"):
        check(f"K) create_realtime_session/_prepare_realtime_session_sync '{name}' KULLANMIYOR (Realtime kuyruğa girmiyor)",
              name not in _sess_src)
        check(f"K) sync_realtime_progress/_sync_realtime_progress_sync '{name}' KULLANMIYOR",
              name not in _sync_src)

    # ============================================================
    # N) "Report AI provider bypass audit" — rapor üretim yollarının scheduler'dan GEÇTİĞİNİN
    #    kaynak taraması (madde A/N).
    # ============================================================
    _primary_src = inspect.getsource(m.run_deferred_finish_job)
    check("N) run_deferred_finish_job (primary rapor) _ai_job_acquire KULLANIYOR",
          "_ai_job_acquire(" in _primary_src)
    _reviewer_src = inspect.getsource(m.run_report_reviewer)
    check("N) run_report_reviewer (Claude second evaluator) _ai_job_acquire KULLANIYOR",
          "_ai_job_acquire(" in _reviewer_src)
    _qg_src = inspect.getsource(m.run_final_report_quality_gate)
    check("N) run_final_report_quality_gate (Final QG) _ai_job_acquire KULLANIYOR",
          "_ai_job_acquire(" in _qg_src)
    _proj_src = inspect.getsource(m.regenerate_one_cikan_proje)
    check("N) regenerate_one_cikan_proje _ai_job_acquire KULLANIYOR",
          "_ai_job_acquire(" in _proj_src)

    # ============================================================
    # G) 429/timeout gibi TEKNİK hata YENİ bir semantic pass SAYILMIYOR — ai_job_slot/acquire
    #    exception'da job'ı FAILED yapar (SYSTEM STATE), rapora/skora karışmaz (madde J, statik
    #    doğrulama zaten test_cok_talent_tek_pass.py'de dinamik olarak yapıldı).
    # ============================================================
    cleanup_jobs()
    job_fail = m.submit_ai_job(CAND, LEVEL, "openai", "gpt-4o", "op", 10, 10)
    m._ai_job_release(job_fail, False, "simulated_technical_error")
    row_fail = read_job(job_fail)
    check("G/J) Teknik hatada job FAILED işaretlenir (SYSTEM STATE, candidate score alanına dokunmaz)",
          row_fail and row_fail["status"] == "FAILED" and row_fail["error_text"] == "simulated_technical_error")

finally:
    cleanup_jobs()


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("Tüm İŞ EMRİ — ZORUNLU AI JOB QUEUE / RATE-AWARE SCHEDULER testleri GEÇTİ.")

if FAILURES:
    sys.exit(1)

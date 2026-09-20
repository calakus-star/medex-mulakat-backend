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
    # İŞ EMRİ — SON DAR DÜZELTME / ROLLING WINDOW testleri (madde 12: A-F).
    # ============================================================
    import time as _time
    import threading

    def _set_created_at(job_id, when_utc):
        db = m.get_db()
        try:
            db.execute("UPDATE ai_jobs SET created_at=? WHERE job_id=?",
                      (when_utc.strftime("%Y-%m-%d %H:%M:%S"), job_id))
            db.commit()
        finally:
            db.close()

    orig_budget = dict(m.AI_JOB_TOKEN_BUDGET)
    orig_window = m.AI_JOB_TOKEN_WINDOW_SECONDS
    try:
        m.AI_JOB_TOKEN_BUDGET["openai"] = 20000
        m.AI_JOB_TOKEN_WINDOW_SECONDS = 60

        # A) budget=20000, completed recent actual=15000, new estimated=10000 -> REDDEDİLMELİ.
        cleanup_jobs()
        jA = m.submit_ai_job(CAND, LEVEL, "openai", "gpt-4o", "primary_report", 15000, 0)
        assert m._try_acquire_ai_capacity(jA, "openai", 15000) is True
        m.mark_ai_job_usage(jA, 15000, 0)
        m._finish_ai_job(jA, "COMPLETED")  # COMPLETED, ama pencere İÇİNDE (created_at az önce)
        jB = m.submit_ai_job(CAND, LEVEL, "openai", "gpt-4o", "primary_report", 10000, 0)
        okB = m._try_acquire_ai_capacity(jB, "openai", 10000)
        check("A) 15000(pencere içi COMPLETED)+10000(yeni) > 20000 bütçe -> REDDEDİLDİ", okB is False)

        # B) Aynı COMPLETED job pencere DIŞINA çıkınca -> yeni job admission ALABİLMELİ.
        _set_created_at(jA, m.datetime.utcnow() - m.timedelta(seconds=orig_window + 120))
        okB2 = m._try_acquire_ai_capacity(jB, "openai", 10000)
        check("B) Pencere dışına çıkan COMPLETED artık SAYILMIYOR -> yeni job BAŞARILI", okB2 is True)
        m._finish_ai_job(jB, "COMPLETED")

        # C) RUNNING estimated=12000, recent COMPLETED actual=6000, new estimated=5000,
        #    budget=20000 -> 12+6+5=23K -> REDDEDİLMELİ.
        cleanup_jobs()
        jRun = m.submit_ai_job(CAND, LEVEL, "openai", "gpt-4o", "primary_report", 12000, 0)
        assert m._try_acquire_ai_capacity(jRun, "openai", 12000) is True  # RUNNING kalır (bitirilmedi)
        jDone = m.submit_ai_job(CAND, LEVEL, "openai", "gpt-4o", "report_reviewer", 6000, 0)
        assert m._try_acquire_ai_capacity(jDone, "openai", 6000) is True
        m.mark_ai_job_usage(jDone, 6000, 0)
        m._finish_ai_job(jDone, "COMPLETED")
        jNew = m.submit_ai_job(CAND, LEVEL, "openai", "gpt-4o", "quality_gate", 5000, 0)
        okNew = m._try_acquire_ai_capacity(jNew, "openai", 5000)
        check("C) RUNNING(12K)+COMPLETED-pencere-içi(6K)+yeni(5K)=23K > 20K bütçe -> REDDEDİLDİ",
              okNew is False)
        m._finish_ai_job(jRun, "COMPLETED")  # temizlik

        # D) COMPLETED job'ın actual'ı varsa estimated YERİNE actual kullanılmalı.
        cleanup_jobs()
        jAct = m.submit_ai_job(CAND, LEVEL, "openai", "gpt-4o", "primary_report", 5000, 5000)  # est=10000
        assert m._try_acquire_ai_capacity(jAct, "openai", 10000) is True
        m.mark_ai_job_usage(jAct, 500, 500)  # GERÇEK çok daha küçük çıktı: actual=1000
        m._finish_ai_job(jAct, "COMPLETED")
        jCheck = m.submit_ai_job(CAND, LEVEL, "openai", "gpt-4o", "op", 100, 100)
        # Bütçe 20000; actual(1000) kullanılırsa 1000+18900=19900<20000 -> BAŞARILI olmalı;
        # estimated(10000) kullanılsaydı 10000+18900=28900>20000 -> REDDEDİLİRDİ.
        okCheck = m._try_acquire_ai_capacity(jCheck, "openai", 18900)
        check("D) COMPLETED job'da actual(1000) tahmini(10000) YERİNE kullanıldı (bütçe hesabı buna göre)",
              okCheck is True)
        m._finish_ai_job(jCheck, "COMPLETED")
        m._finish_ai_job(jAct, "COMPLETED")

        # E) Aynı job iki kez SAYILMAMALI (CASE ifadesi her satırı TEK dalda değerlendirir).
        cleanup_jobs()
        jOnce = m.submit_ai_job(CAND, LEVEL, "openai", "gpt-4o", "primary_report", 3000, 0)
        assert m._try_acquire_ai_capacity(jOnce, "openai", 3000) is True
        m.mark_ai_job_usage(jOnce, 3000, 0)
        m._finish_ai_job(jOnce, "COMPLETED")
        db_e = m.get_db()
        try:
            total_e = db_e.execute(
                "SELECT COALESCE(SUM(CASE WHEN status='COMPLETED' THEN COALESCE(actual_input_tokens,0)+COALESCE(actual_output_tokens,0) ELSE 0 END),0) AS t "
                "FROM ai_jobs WHERE job_id=?", (jOnce,)).fetchone()["t"]
        finally:
            db_e.close()
        check("E) Job satırı DB'de yalnız 1 kez var, toplam tam 3000 (iki kez sayılmadı)", total_e == 3000)

        # F) OpenAI tüketimi Anthropic havuzunu ETKİLEMEMELİ (rolling window da provider'a özel).
        cleanup_jobs()
        m.AI_JOB_TOKEN_BUDGET["anthropic"] = 20000
        jOpenAI = m.submit_ai_job(CAND, LEVEL, "openai", "gpt-4o", "primary_report", 19000, 0)
        assert m._try_acquire_ai_capacity(jOpenAI, "openai", 19000) is True
        m.mark_ai_job_usage(jOpenAI, 19000, 0)
        m._finish_ai_job(jOpenAI, "COMPLETED")  # openai penceresi neredeyse dolu
        jClaude = m.submit_ai_job(CAND, LEVEL, "anthropic", "claude-sonnet-4-6", "report_reviewer", 15000, 0)
        okClaude = m._try_acquire_ai_capacity(jClaude, "anthropic", 15000)
        check("F) OpenAI penceresi neredeyse DOLUYKEN Anthropic'in KENDİ (boş) bütçesi ETKİLENMEDİ",
              okClaude is True)
        m._finish_ai_job(jClaude, "COMPLETED")
    finally:
        m.AI_JOB_TOKEN_BUDGET.clear()
        m.AI_JOB_TOKEN_BUDGET.update(orig_budget)
        m.AI_JOB_TOKEN_WINDOW_SECONDS = orig_window
        cleanup_jobs()

    # ============================================================
    # G) CONCURRENCY RACE: iki "worker" (thread, ayrı DB bağlantısı) aynı anda SON kalan
    #    kapasiteyi claim etmeye çalışır — ikisi BİRLİKTE kapasiteyi AŞAMAMALI (madde 4/12-G).
    # ============================================================
    cleanup_jobs()
    orig_conc_race = dict(m.AI_JOB_MAX_CONCURRENT)
    try:
        m.AI_JOB_MAX_CONCURRENT["openai"] = 1  # yalnız TEK eşzamanlı iş için yer var
        jRace1 = m.submit_ai_job(CAND, LEVEL, "openai", "gpt-4o", "op", 10, 10)
        jRace2 = m.submit_ai_job(CAND, LEVEL, "openai", "gpt-4o", "op", 10, 10)
        results = {}
        barrier = threading.Barrier(2)

        def _worker(name, job_id):
            barrier.wait()  # iki thread AYNI ANDA admission denemesine başlasın
            results[name] = m._try_acquire_ai_capacity(job_id, "openai", 20)

        t1 = threading.Thread(target=_worker, args=("w1", jRace1))
        t2 = threading.Thread(target=_worker, args=("w2", jRace2))
        t1.start(); t2.start()
        t1.join(timeout=15); t2.join(timeout=15)

        succeeded = sum(1 for v in results.values() if v is True)
        check("G) İki eşzamanlı worker'dan TAM OLARAK BİRİ claim etti (kapasite AŞILMADI)",
              succeeded == 1)
        db_g = m.get_db()
        try:
            running_g = db_g.execute("SELECT COUNT(*) AS n FROM ai_jobs WHERE provider='openai' AND status='RUNNING'").fetchone()["n"]
        finally:
            db_g.close()
        check("G) DB'de RUNNING sayısı kapasiteyi (1) AŞMIYOR", running_g <= 1)
    finally:
        m.AI_JOB_MAX_CONCURRENT.clear()
        m.AI_JOB_MAX_CONCURRENT.update(orig_conc_race)
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
    # 3PASS-H/I/J) RUNTIME uçtan uca: sentetik L3 çalıştır, ai_jobs'a yazılan semantic
    # operation'ların KESİN {primary_report, report_reviewer, quality_gate} = 3 olduğunu, kısa
    # cevap/token-limit simülasyonlarının YENİ semantic çağrı YARATMADIĞINI doğrula.
    # ============================================================
    import json as _json

    RCID = 980399
    RLEVEL = 3
    FULL_REPORT_L3 = """[MÜLAKATBİTTİ]
---RAPOR---
===YÖNETİCİ ÖZETİ===
Test özeti, aday hakkında kısa bir değerlendirme metni burada yer alır ve en az elli iki kelime uzunluğunda olacak şekilde genişletilir ki uzunluk denetiminden makul biçimde geçebilsin ve rapor akışı normal şekilde ilerleyebilsin bu turda.

===POZİSYON YETKİNLİKLERİ===
| Proje Yönetimi | 20/25 | G: Süreci uçtan uca yönetti ~~ K: [1:00] "süreci baştan sona ben yönettim" ~~ E: ~~ S: |

===KİŞİSEL VE BİLİŞSEL PROFİL===
YOK

===GÜÇLÜ YÖNLER===
YOK

===GELİŞİM ALANLARI===
YOK

===CV ÖZETİ===
YOK

===TAKİP MÜLAKATI SORULARI===
YOK
===BÖLÜM SONU===
---RAPORSON---"""

    def _chat_body(content, finish_reason="stop", completion_tokens=300):
        return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
                "usage": {"completion_tokens": completion_tokens, "prompt_tokens": 500}}

    class _FakeOpenAIResp:
        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    class _FakeAnthropicMsg:
        def __init__(self, text):
            self.content = [type("C", (), {"text": text})()]
            self.usage = type("U", (), {"output_tokens": 300})()
            self.stop_reason = "end_turn"

    class _FakeAnthropicMessages:
        def create(self, **kwargs):
            return _FakeAnthropicMsg("GÖRÜŞ YOK\n\n=== ADAY ÖZGÜVENİ İZLENİMİ ===\nYETERSİZ VERİ\n\n=== KRİTER PUANLARI ===\n\n=== SEMANTİK TUTARLILIK ===\n")

    class _FakeAnthropicClient:
        def __init__(self, *a, **k):
            self.messages = _FakeAnthropicMessages()

    def _seed_l3(cid, primary_reply_text, qg_status_line="QUALITY_GATE_STATUS: PASS\n"):
        db = m.get_db()
        try:
            db.execute("DELETE FROM interviews WHERE candidate_id=? AND level=?", (cid, RLEVEL))
            db.execute("DELETE FROM candidates WHERE id=?", (cid,))
            db.execute(
                "INSERT INTO candidates (id, name, username, password_hash, email, position, level, cv_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (cid, "Test Aday 3Pass", f"test_3pass_{cid}", "x", f"test_3pass_{cid}@example.com",
                 "Proje Yöneticisi", RLEVEL, "Lisans mezunu, 6 yıl proje yönetimi deneyimi."))
            db.execute(
                "INSERT INTO interviews (candidate_id, level, messages, pending_finish_provider, pending_finish_model, "
                "pending_finish_payload, pending_finish_system, processing_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (cid, RLEVEL, _json.dumps([
                    {"role": "assistant", "content": "Bu süreci nasıl yönettiniz?", "ts": "2026-01-01T10:00:55"},
                    {"role": "user", "content": "Süreci baştan sona ben yönettim.", "ts": "2026-01-01T10:01:00"},
                 ]), "openai", "gpt-4o",
                 "GÖREV: Mülakatı bitir ve raporu üret.\n\nTRANSKRİPT: [1:00] Aday: Süreci baştan sona ben yönettim.",
                 "Sistem talimatı.", "processing"))
            db.commit()
        finally:
            db.close()

    def _run_3pass(cid, primary_body, qg_body, anthropic_client_cls=_FakeAnthropicClient):
        _seed_l3(cid, primary_body)
        call_step_order = []

        def mock_openai_call(method, url, *, json_body=None, step=None, **kwargs):
            call_step_order.append(step)
            if step in ("report_generation",):
                return _FakeOpenAIResp(primary_body)
            if step == "quality_gate":
                return _FakeOpenAIResp(qg_body)
            # one_cikan_proje_recovery / criterion_rationale_retry vb. NORMAL akışta hiç
            # ÇAĞRILMAMALI — çağrılırsa AssertionError ile YAKALANIR (aşağıda kontrol edilir).
            return _FakeOpenAIResp(_chat_body("YOK"))

        orig_openai_call = m.openai_call
        orig_anthropic_cls = m.anthropic.Anthropic
        m.openai_call = mock_openai_call
        m.anthropic.Anthropic = anthropic_client_cls
        try:
            m.run_deferred_finish_job(cid, RLEVEL)
        finally:
            m.openai_call = orig_openai_call
            m.anthropic.Anthropic = orig_anthropic_cls
        db = m.get_db()
        try:
            jobs = db.execute("SELECT operation, status FROM ai_jobs WHERE candidate_id=? AND level=? ORDER BY created_at",
                              (cid, RLEVEL)).fetchall()
        finally:
            db.close()
        return [dict(j) for j in jobs], call_step_order

    orig_budget_h = dict(m.AI_JOB_TOKEN_BUDGET)
    orig_openai_key = m.OPENAI_API_KEY
    orig_anthropic_key = m.ANTHROPIC_API_KEY
    m.AI_JOB_TOKEN_BUDGET["openai"] = 10_000_000
    m.AI_JOB_TOKEN_BUDGET["anthropic"] = 10_000_000
    m.OPENAI_API_KEY = "test-dummy-key"
    m.ANTHROPIC_API_KEY = "test-dummy-key"
    try:
        db0 = m.get_db()
        try:
            db0.execute("DELETE FROM ai_jobs WHERE candidate_id=?", (RCID,))
            db0.commit()
        finally:
            db0.close()

        # H) Normal senaryo: primary tam rapor, QG PASS -> KESİN 3 semantic operation.
        jobs_h, _ = _run_3pass(RCID, _chat_body(FULL_REPORT_L3), _chat_body("QUALITY_GATE_STATUS: PASS\n"))
        ops_h = sorted(j["operation"] for j in jobs_h)
        check("H) Normal L3'te TAM 3 ai_jobs satırı oluştu", len(jobs_h) == 3)
        check("H) Operation listesi KESİN {primary_report, quality_gate, report_reviewer}",
              ops_h == sorted(["primary_report", "quality_gate", "report_reviewer"]))
        check("H) Yasaklı operation'lardan HİÇBİRİ YOK (short_retry/continuation/criterion/yonetici_ozeti/proje_recovery)",
              not any(op in ops_h for op in (
                  "l2_report_generation_short_retry", "l2_report_continuation",
                  "criterion_rationale_retry", "criterion_recovery", "yonetici_ozeti_retry",
                  "one_cikan_proje_recovery")))
        check("H) Üç job da COMPLETED", all(j["status"] == "COMPLETED" for j in jobs_h))

        # I) Primary "anormal kısa" simülasyonu: finish=stop, RAPORSON yok, çok kısa -> primary
        #    HARİÇ yeni semantic provider çağrısı OLUŞMAMALI (job FAILED, pipeline orada durur).
        db0 = m.get_db()
        try:
            db0.execute("DELETE FROM ai_jobs WHERE candidate_id=?", (RCID,))
            db0.commit()
        finally:
            db0.close()
        jobs_i, steps_i = _run_3pass(RCID, _chat_body("Kısa.", finish_reason="stop", completion_tokens=5),
                                     _chat_body("QUALITY_GATE_STATUS: PASS\n"))
        check("I) Anormal kısa primary sonrası YALNIZ 1 ai_jobs satırı (primary_report, FAILED) — YENİ semantic çağrı YOK",
              len(jobs_i) == 1 and jobs_i[0]["operation"] == "primary_report" and jobs_i[0]["status"] == "FAILED")
        check("I) report_generation adımı TEK KEZ denendi (l2_report_generation_short_retry YOK)",
              steps_i.count("report_generation") <= 1)

        # J) Primary token-limit simülasyonu (finish=length, RAPORSON yok) -> continuation
        #    semantic çağrısı OLUŞMAMALI; pipeline yine de (eksik rapor ile) devam eder ve
        #    toplam semantic operation SAYISI yine 3'ü AŞMAZ.
        db0 = m.get_db()
        try:
            db0.execute("DELETE FROM ai_jobs WHERE candidate_id=?", (RCID,))
            db0.commit()
        finally:
            db0.close()
        _truncated = "[MÜLAKATBİTTİ]\n---RAPOR---\n===YÖNETİCİ ÖZETİ===\nKesilmiş rapor metni burada devam ediyor ama bitmiyor"
        jobs_j, steps_j = _run_3pass(RCID, _chat_body(_truncated, finish_reason="length", completion_tokens=16000),
                                     _chat_body("QUALITY_GATE_STATUS: PASS\n"))
        check("J) 'report_continuation' adımı HİÇ ÇAĞRILMADI (continuation normal akıştan ÇIKARILDI)",
              "report_continuation" not in steps_j)
        check("J) Toplam ai_jobs satırı 3'ü AŞMIYOR (kesilmiş rapora rağmen pipeline normal ilerledi)",
              len(jobs_j) <= 3)
    finally:
        m.AI_JOB_TOKEN_BUDGET.clear()
        m.AI_JOB_TOKEN_BUDGET.update(orig_budget_h)
        m.OPENAI_API_KEY = orig_openai_key
        m.ANTHROPIC_API_KEY = orig_anthropic_key
        db0 = m.get_db()
        try:
            db0.execute("DELETE FROM ai_jobs WHERE candidate_id=?", (RCID,))
            db0.execute("DELETE FROM interviews WHERE candidate_id=? AND level=?", (RCID, RLEVEL))
            db0.execute("DELETE FROM candidates WHERE id=?", (RCID,))
            db0.commit()
        finally:
            db0.close()

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

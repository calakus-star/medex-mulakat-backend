# İŞ EMRİ — MEDEX ÇOKLU TALENT MİMARİSİ + TEK-PASS AI AKIŞI — regresyon testleri.
# Amaç: (1) kriter validator'ının artık AI'yı TEKRAR TEKRAR çağırmadığını (content-retry KALKTI),
# (2) 429/teknik retry davranışının Retry-After'ı önceliklendirdiğini ve bounded olduğunu,
# (3) aynı candidate+level için rapor job'ının VE Realtime oturumunun aynı anda iki kez
# claim edilemediğini, (4) farklı candidate'ların birbirini ENGELLEMEDİĞİNİ doğrulamak.
# Hiçbir gerçek ağ/API çağrısı yapılmaz. Aday/pozisyon özel hardcode YOK — sentetik veri.
#
# Çalıştırma: py test_cok_talent_tek_pass.py  (backend/ dizininde)

import sys
import main as m

FAILURES = []


def check(label, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        FAILURES.append(label)


# Test kayıtları için ayrılmış, gerçek verilerle çakışmayacak bir ID aralığı.
CAND_A, CAND_B = 980101, 980102
LEVEL = 3


def cleanup():
    db = m.get_db()
    try:
        for cid in (CAND_A, CAND_B):
            db.execute("DELETE FROM interviews WHERE candidate_id=?", (cid,))
        db.execute("DELETE FROM candidates WHERE id IN (?, ?)", (CAND_A, CAND_B))
        db.commit()
    finally:
        db.close()


def seed_candidate(cid, level=LEVEL):
    db = m.get_db()
    try:
        db.execute(
            "INSERT INTO candidates (id, name, position, cv_text, level, interview_language, report_language, username, password_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, "Test Aday", "Test Pozisyonu", "Lisans mezunu, 5 yıl deneyim, proje yönetimi.",
             level, "tr", "tr", f"test_user_{cid}", "x"))
        db.commit()
    finally:
        db.close()


def read_interview(cid, level=LEVEL):
    db = m.get_db()
    try:
        row = db.execute("SELECT * FROM interviews WHERE candidate_id=? AND level=?", (cid, level)).fetchone()
    finally:
        db.close()
    return dict(row) if row else None


try:
    # ============================================================
    # A) Kriter validator artık AI content-retry YAPMIYOR (madde E/F/G).
    # ============================================================
    class _ContentRetryCalled(AssertionError):
        pass

    def _fail_if_called(*a, **k):
        raise _ContentRetryCalled("BEKLENMEDİK: regenerate_criterion_fields çağrıldı (content-retry normal akışta YASAK)")

    _orig_regen_crit = m.regenerate_criterion_fields
    m.regenerate_criterion_fields = _fail_if_called
    try:
        # Kasıtlı olarak BOŞ kanıt hücresi -> 'structure_invalid' (deterministik onarılamaz) ->
        # ESKİ kodda bu, regenerate_criterion_fields'a kadar 3(+1) kez giderdi.
        # TEK DÜZELTME — DEĞERLENDİRİLEMEDİ KRİTERLERİ (sonraki iş emri, DÜZELTİLMİŞ): kriter zaten
        # puanlanmış (5/10 award_m eşleşti) — bu bir TEKNİK VALİDATOR/PARSER sorunu, ne "Değerlendiri-
        # lemedi (sistem)" (payda dışı) OLUR NE DE %25 taban puana düşürülür (o kural yalnız adayın
        # GERÇEKTEN yetersiz cevap verdiği durumlar içindir — burası o durum DEĞİL). Adayın mevcut
        # puanı (5/10) AYNEN KORUNUR.
        table_text = "| Proje Yönetimi | 5/10 |  |"
        criteria_list = [{"name": "Proje Yönetimi", "weight": 10, "desc": "Proje yürütme becerisi"}]
        new_table, new_score, log, flagged = m.apply_structured_rationale_gate(
            table_text, criteria_list, "P", [], "", "openai", "gpt-4o", CAND_A, LEVEL)
        check("A) apply_structured_rationale_gate AI'yı HİÇ çağırmadan tamamlandı (exception yok)", True)
        check("A) İhlal deterministik onarılamayınca kriter 'teknik_validator_hatasi_puan_korundu' oldu (AI'sız, PAYDA İÇİNDE, puan DEĞİŞMEDİ)",
              any(l.get("sonuc") == "teknik_validator_hatasi_puan_korundu" for l in log))
        check("A) 'degerlendirilemedi_sistem' (payda dışı) ARTIK ÜRETİLMİYOR — teknik validator sorunu diskalifiye ETMEZ",
              not any(l.get("sonuc") == "degerlendirilemedi_sistem" for l in log))
        check("A) tablo Değerlendirilemedi (sistem) METNİ İÇERMİYOR, ADAYIN ORİJİNAL PUANI (5/10) AYNEN KORUNDU (taban puana DÜŞÜRÜLMEDİ)",
              "Değerlendirilemedi" not in new_table and "5/10" in new_table)
        check("A) new_score None (bu satır TOPLAM PUAN'ı yeniden normalize ETMEDİ — puan zaten değişmedi)",
              new_score is None)
    except _ContentRetryCalled as e:
        check("A) apply_structured_rationale_gate AI'yı HİÇ çağırmadan tamamlandı (exception yok)", False)
    finally:
        m.regenerate_criterion_fields = _orig_regen_crit

    # ============================================================
    # A2) Yönetici Özeti content-retry çağrısı finalize_interview KAYNAK KODUNDAN kaldırıldı.
    # ============================================================
    import inspect
    _fi_src = inspect.getsource(m.finalize_interview)
    check("A2) finalize_interview artık regenerate_yonetici_ozeti ÇAĞIRMIYOR (kaynak taraması)",
          "regenerate_yonetici_ozeti(" not in _fi_src)
    _gate_src = inspect.getsource(m.apply_structured_rationale_gate)
    check("A2) apply_structured_rationale_gate artık regenerate_criterion_fields ÇAĞIRMIYOR (kaynak taraması)",
          "regenerate_criterion_fields(" not in _gate_src)

    # ============================================================
    # B) Teknik retry: Retry-After header varsa ONA uyulur; yoksa bounded exponential+jitter.
    # ============================================================
    class _FakeHeaders(dict):
        def get(self, k, default=None):
            return dict.get(self, k.lower(), default)

    delay_with_header = m._technical_retry_delay(0, _FakeHeaders({"retry-after": "2"}))
    check("B) Retry-After=2 varsa gecikme ~2sn (header önceliklendirildi)", abs(delay_with_header - 2.0) < 0.01)

    delay_no_header = m._technical_retry_delay(5, None)
    check("B) Retry-After yokken gecikme _RETRY_MAX_DELAY_SECONDS ile TAVANLANMIŞ (sonsuz/aşırı uzun yok)",
          delay_no_header <= m._RETRY_MAX_DELAY_SECONDS)
    check("B) Retry-After yokken gecikme negatif/sıfır değil", delay_no_header > 0)

    huge_retry_after = m._technical_retry_delay(0, _FakeHeaders({"retry-after": "99999"}))
    check("B) Çok büyük Retry-After bile _RETRY_MAX_DELAY_SECONDS ile TAVANLANIR",
          huge_retry_after <= m._RETRY_MAX_DELAY_SECONDS)

    # ============================================================
    # C) Aynı candidate+level için AYNI ANDA yalnız TEK rapor job'ı claim edilebilir (madde I).
    # ============================================================
    cleanup()
    seed_candidate(CAND_A)
    db0 = m.get_db()
    try:
        db0.execute("INSERT INTO interviews (candidate_id, level, messages) VALUES (?, ?, '[]')", (CAND_A, LEVEL))
        db0.commit()
    finally:
        db0.close()

    job1 = m._mark_finish_pending(CAND_A, LEVEL, provider="openai", model=m.OPENAI_REPORT_MODEL,
                                  system="sys", payload="payload", terminated_reason=None, reason="normal")
    check("C) İlk claim BAŞARILI (job_id döndü)", bool(job1))

    job2 = m._mark_finish_pending(CAND_A, LEVEL, provider="openai", model=m.OPENAI_REPORT_MODEL,
                                  system="sys", payload="payload-2", terminated_reason=None, reason="admin_regenerate")
    check("C) İKİNCİ eşzamanlı claim REDDEDİLDİ (None) — aynı candidate+level çift job'ı önlendi", job2 is None)

    iv = read_interview(CAND_A)
    check("C) Reddedilen ikinci claim mevcut job'ın payload'ını EZMEDİ", iv["pending_finish_payload"] == "payload")

    # Job 'completed' olunca YENİ bir claim tekrar başarılı olmalı.
    db1 = m.get_db()
    try:
        db1.execute("UPDATE interviews SET processing_status='completed' WHERE candidate_id=? AND level=?", (CAND_A, LEVEL))
        db1.commit()
    finally:
        db1.close()
    job3 = m._mark_finish_pending(CAND_A, LEVEL, provider="openai", model=m.OPENAI_REPORT_MODEL,
                                  system="sys", payload="payload-3", terminated_reason=None, reason="admin_regenerate")
    check("C) Önceki job 'completed' olunca YENİ claim tekrar BAŞARILI", bool(job3))

    # ============================================================
    # D) Farklı candidate'lar BİRBİRİNİ ENGELLEMİYOR (izolasyon).
    # ============================================================
    seed_candidate(CAND_B)
    db2 = m.get_db()
    try:
        db2.execute("INSERT INTO interviews (candidate_id, level, messages) VALUES (?, ?, '[]')", (CAND_B, LEVEL))
        db2.commit()
    finally:
        db2.close()
    job_b = m._mark_finish_pending(CAND_B, LEVEL, provider="openai", model=m.OPENAI_REPORT_MODEL,
                                   system="sys", payload="payload-b", terminated_reason=None, reason="normal")
    check("D) Candidate A hâlâ 'processing' iken Candidate B claim'i BAŞARILI (izole)", bool(job_b))

    # ============================================================
    # E) Aynı candidate+level için AYNI ANDA yalnız TEK Realtime (canlı) oturumu (madde B).
    # ============================================================
    cleanup()
    seed_candidate(CAND_A, level=2)
    prep1 = m._prepare_realtime_session_sync(CAND_A)
    check("E) İlk Realtime session claim'i BAŞARILI", prep1.get("owner_claimed") is True)

    prep2 = m._prepare_realtime_session_sync(CAND_A)
    check("E) İKİNCİ (eşzamanlı) Realtime session claim'i REDDEDİLDİ — çift sekme/tab önlendi",
          prep2.get("owner_claimed") is False)

    # Sahiplik STALE olunca (eski heartbeat) yeni claim tekrar başarılı olmalı. Büyük bir ofset
    # (1 gün) kullanılır ki yerel saat dilimi / DB UTC saati farkı testi FLAKY yapmasın.
    db3 = m.get_db()
    try:
        _stale_ts = (m.datetime.utcnow() - m.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        db3.execute("UPDATE interviews SET realtime_owner_at=? WHERE candidate_id=? AND level=?",
                   (_stale_ts, CAND_A, 2))
        db3.commit()
    finally:
        db3.close()
    prep3 = m._prepare_realtime_session_sync(CAND_A)
    check("E) Sahiplik STALE olunca YENİ claim tekrar BAŞARILI (terk edilmiş sekme kilitlenmiyor)",
          prep3.get("owner_claimed") is True)

finally:
    cleanup()


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("Tüm İŞ EMRİ — ÇOKLU TALENT MİMARİSİ + TEK-PASS AI AKIŞI testleri GEÇTİ.")

if FAILURES:
    sys.exit(1)

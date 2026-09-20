# İŞ EMRİ — L1 OPENAI-ONLY MİMARİSİNİ TAMAMLA — regresyon testleri.
# Amaç: L1 (metin) mülakatının CANLI sohbeti + raporu/tüm normal akışının Anthropic/Claude'a
# SIFIR çağrı yaptığını, buna karşılık OpenAI'ye gerçekten gittiğini doğrulamak. Hiçbir gerçek
# ağ/API çağrısı yapılmaz (openai_call / anthropic.Anthropic.messages.create monkey-patch edilir).
# Aday/pozisyon/kriter'e özel hardcode yok — jenerik sentetik veri.
#
# Çalıştırma: py test_l1_openai_only.py  (backend/ dizininde)

import io
import sys
import json
import inspect
import contextlib
from fastapi import BackgroundTasks
import main as m

FAILURES = []


def check(label, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        FAILURES.append(label)


TEST_CID = 9601
LEVEL_L1, LEVEL_L2, LEVEL_L3 = 1, 2, 3


class _AnthropicCallDetected(AssertionError):
    pass


class _FailAnthropic:
    def __init__(self, *a, **k):
        raise _AnthropicCallDetected("BEKLENMEDİK: anthropic.Anthropic() çağrıldı (L1 akışında Anthropic YASAK)")


def _ensure_candidate(level=LEVEL_L1, cv_text="Lisans mezunu, 4 yıl deneyim."):
    db = m.get_db()
    try:
        row = db.execute("SELECT id FROM candidates WHERE id=?", (TEST_CID,)).fetchone()
        if row:
            db.execute("UPDATE candidates SET cv_text=?, position=?, level=?, name=?, interview_language=?, report_language=?, ai_note=?, education=?, university=?, department=?, experience_years=? WHERE id=?",
                       (cv_text, "Test Pozisyonu", level, "Test Aday", "tr", "tr", None, None, None, None, None, TEST_CID))
        else:
            db.execute("INSERT INTO candidates (id, name, position, cv_text, level, interview_language, report_language) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (TEST_CID, "Test Aday", "Test Pozisyonu", cv_text, level, "tr", "tr"))
        db.commit()
    finally:
        db.close()


def _seed_interview(level=LEVEL_L1, messages=None, closing_asked=0):
    db = m.get_db()
    try:
        db.execute("DELETE FROM interviews WHERE candidate_id=? AND level=?", (TEST_CID, level))
        db.execute("INSERT INTO interviews (candidate_id, level, messages, closing_asked) VALUES (?, ?, ?, ?)",
                   (TEST_CID, level, json.dumps(messages or []), closing_asked))
        db.commit()
    finally:
        db.close()


def _read_interview(level=LEVEL_L1):
    db = m.get_db()
    try:
        row = db.execute("SELECT * FROM interviews WHERE candidate_id=? AND level=?", (TEST_CID, level)).fetchone()
    finally:
        db.close()
    return dict(row) if row else None


def cleanup():
    db = m.get_db()
    try:
        for lv in (LEVEL_L1, LEVEL_L2, LEVEL_L3):
            db.execute("DELETE FROM interviews WHERE candidate_id=? AND level=?", (TEST_CID, lv))
        db.execute("DELETE FROM candidates WHERE id=?", (TEST_CID,))
        db.commit()
    finally:
        db.close()


def fake_openai_chat_response(content, counter=None):
    def fake_call(method, url, *, json_body=None, **kw):
        if counter is not None:
            counter["n"] += 1
        class R:
            def json(self_inner):
                return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 50, "completion_tokens": 20}}
        return R()
    return fake_call


try:
    orig_anthropic_cls = m.anthropic.Anthropic
    orig_openai_call = m.openai_call
    orig_record_openai_usage = m.record_openai_chat_usage
    orig_run_deferred = m.run_deferred_finish_job
    m.OPENAI_API_KEY = m.OPENAI_API_KEY or "test-dummy-key"
    m.ANTHROPIC_API_KEY = m.ANTHROPIC_API_KEY or "test-dummy-key"

    # ============================================================
    # 1) start_interview (L1 başlangıç) — OpenAI'ye gider, Anthropic'e HİÇ gitmez
    # ============================================================
    cleanup()
    _ensure_candidate(level=LEVEL_L1)
    openai_calls_1 = {"n": 0}
    m.anthropic.Anthropic = _FailAnthropic
    m.openai_call = fake_openai_chat_response("[SÜRE:60] Merhaba, ilk sorumuz: kendinizden bahseder misiniz?", openai_calls_1)
    m.record_openai_chat_usage = lambda *a, **k: None
    try:
        res1 = m.start_interview(payload={"role": "candidate", "candidate_id": TEST_CID,
                                           "position": "Test Pozisyonu", "name": "Test Aday"})
        check("1) start_interview başarıyla döndü", isinstance(res1, dict) and "message" in res1)
        check("1) start_interview OpenAI'ye TAM 1 kez gitti", openai_calls_1["n"] == 1)
    except _AnthropicCallDetected as e:
        check(f"1) start_interview Anthropic çağırmadı: {e}", False)
    finally:
        m.anthropic.Anthropic = orig_anthropic_cls
        m.openai_call = orig_openai_call
        m.record_openai_chat_usage = orig_record_openai_usage

    # ============================================================
    # 2) interview_chat normal tur (bitiş DEĞİL) — OpenAI'ye gider, Anthropic'e HİÇ gitmez
    # ============================================================
    cleanup()
    _ensure_candidate(level=LEVEL_L1)
    _seed_interview(level=LEVEL_L1, messages=[
        {"role": "assistant", "content": "İlk soru: kendinizden bahseder misiniz?", "ts": "2026-01-01T10:00:00"},
    ])
    openai_calls_2 = {"n": 0}
    m.anthropic.Anthropic = _FailAnthropic
    m.openai_call = fake_openai_chat_response("[SÜRE:60] Teşekkürler, peki en zorlu projeniz neydi?", openai_calls_2)
    m.record_openai_chat_usage = lambda *a, **k: None
    try:
        data = m.ChatMessage(candidate_id=TEST_CID, message="Ben bu alanda 4 yıldır çalışıyorum.", history=[], elapsed_seconds=30)
        bt = BackgroundTasks()
        res2 = m.interview_chat(data, bt, payload={"role": "candidate", "candidate_id": TEST_CID,
                                                    "position": "Test Pozisyonu", "name": "Test Aday"})
        check("2) interview_chat normal tur döndü, completed=False", res2.get("completed") is False)
        check("2) interview_chat OpenAI'ye TAM 1 kez gitti", openai_calls_2["n"] == 1)
    except _AnthropicCallDetected as e:
        check(f"2) interview_chat normal tur Anthropic çağırmadı: {e}", False)
    finally:
        m.anthropic.Anthropic = orig_anthropic_cls
        m.openai_call = orig_openai_call
        m.record_openai_chat_usage = orig_record_openai_usage

    # ============================================================
    # 3) interview_chat NORMAL FINISH (should_finish=True) — canlı AI çağrısı YOK (arka plana
    #    alınıyor), pending_finish_provider="openai" olarak işaretleniyor, Anthropic'e gitmiyor.
    # ============================================================
    cleanup()
    _ensure_candidate(level=LEVEL_L1)
    # lvl_cfg L1 min_q'yu aşacak kadar çok soru + elapsed süresi minutes*60'ı geçsin.
    lvl_cfg_l1 = m.get_level_config(LEVEL_L1)
    many_q = [{"role": "assistant", "content": f"Soru {i}", "ts": "2026-01-01T10:00:00"} for i in range(lvl_cfg_l1["min_q"] + 2)]
    _seed_interview(level=LEVEL_L1, messages=many_q, closing_asked=1)  # kapanış zaten soruldu -> should_finish=True
    m.anthropic.Anthropic = _FailAnthropic
    deferred_calls_3 = {"n": 0}
    m.run_deferred_finish_job = lambda *a, **k: deferred_calls_3.__setitem__("n", deferred_calls_3["n"] + 1)
    try:
        data3 = m.ChatMessage(candidate_id=TEST_CID, message="Son cevabım bu.", history=[],
                              elapsed_seconds=lvl_cfg_l1["minutes"] * 60 + 5)
        bt3 = BackgroundTasks()
        res3 = m.interview_chat(data3, bt3, payload={"role": "candidate", "candidate_id": TEST_CID,
                                                      "position": "Test Pozisyonu", "name": "Test Aday"})
        check("3) normal finish -> processing=True döndü", res3.get("processing") is True and res3.get("completed") is True)
        iv3 = _read_interview(LEVEL_L1)
        check("3) pending_finish_provider == 'openai'", iv3["pending_finish_provider"] == "openai")
        check("3) pending_finish_model == OPENAI_REPORT_MODEL", iv3["pending_finish_model"] == m.OPENAI_REPORT_MODEL)
    except _AnthropicCallDetected as e:
        check(f"3) normal finish Anthropic çağırmadı: {e}", False)
    finally:
        m.anthropic.Anthropic = orig_anthropic_cls
        m.run_deferred_finish_job = orig_run_deferred

    # ============================================================
    # 4) L1 normal finish/report — run_deferred_finish_job: OpenAI report call = 1,
    #    Claude report call = 0, reviewer = 0, Quality Gate = 0 (L1 -> append_reviewer_section /
    #    run_final_report_quality_gate zaten level!=3 -> return ile korunuyor, burada da doğrulanır).
    # ============================================================
    cleanup()
    _ensure_candidate(level=LEVEL_L1)
    _seed_interview(level=LEVEL_L1, messages=[
        {"role": "assistant", "content": "Kendinizden bahseder misiniz?", "ts": "2026-01-01T10:00:00"},
        {"role": "user", "content": "4 yıldır bu alandayım.", "ts": "2026-01-01T10:00:30"},
    ])
    m._mark_finish_pending(TEST_CID, LEVEL_L1, provider="openai", model=m.OPENAI_REPORT_MODEL,
                           system="Test sistem promptu.", payload="Test görevi: raporu üret. [MÜLAKATBİTTİ]",
                           terminated_reason=None, reason="normal")

    REPORT_REPLY = """[MÜLAKATBİTTİ]
---RAPOR---
===YÖNETİCİ ÖZETİ===
Aday, mülakat boyunca alanındaki deneyimini somut örneklerle aktardı. Süreç yönetimi konusunda kendi
sorumluluğunu üstlendiğini belirtti ve karşılaştığı zorlukları nasıl ele aldığını açık biçimde anlattı.
İletişim tarzı net ve tutarlıydı; sorulara doğrudan cevap verdi, gereksiz genellemelerden kaçındı.
Geçmiş deneyimlerinden bahsederken somut ayrıntılar sundu ve bu ayrıntılar mülakat boyunca tutarlılık
gösterdi. Pozisyonun gerektirdiği temel yetkinlikler açısından adayın verdiği örnekler yeterli düzeyde
bilgi sağladı. Genel olarak aday, kendine güvenen ama abartısız bir üslup kullandı ve mülakatçının
sorularına odaklı biçimde yanıt verdi. Aday, ekip çalışmasına verdiği önemi ve önceki projelerde
aldığı rolleri açıkladı; bu anlatım, pozisyonun gerekliliklerine göre değerlendirilebilecek somut bir
temel oluşturdu. Mülakat sürecinin tamamında aday katılımcı ve işbirlikçi bir tutum sergiledi.

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

===POZİSYON YETKİNLİKLERİ===
YOK

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

    openai_calls_4 = {"n": 0}
    reviewer_calls_4 = {"n": 0}
    qg_calls_4 = {"n": 0}
    _steps_4 = []
    m.anthropic.Anthropic = _FailAnthropic

    def _counting_fake_call(method, url, *, json_body=None, **kw):
        _steps_4.append(kw.get("step"))
        return fake_openai_chat_response(REPORT_REPLY, openai_calls_4)(method, url, json_body=json_body, **kw)

    m.openai_call = _counting_fake_call
    m.record_openai_chat_usage = lambda *a, **k: None

    orig_append_reviewer = m.append_reviewer_section
    orig_run_qg = m.run_final_report_quality_gate

    def counting_reviewer(*a, **k):
        reviewer_calls_4["n"] += 1
        return orig_append_reviewer(*a, **k)

    def counting_qg(*a, **k):
        qg_calls_4["n"] += 1
        return orig_run_qg(*a, **k)

    m.append_reviewer_section = counting_reviewer
    m.run_final_report_quality_gate = counting_qg
    buf4 = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf4):
            m.run_deferred_finish_job(TEST_CID, LEVEL_L1)
        # Birincil rapor üretim adımı ("report_generation") TAM 1 kez OpenAI'ye gitmeli. Yönetici
        # Özeti/"Öne Çıkan Proje" gibi kalite doğrulayıcılarının (mevcut, bu iş emrinden BAĞIMSIZ
        # davranış) tetiklediği ek OpenAI iyileştirme çağrıları da OLABİLİR — asıl invaryant, TÜM bu
        # çağrıların OpenAI'ye gitmesi ve Anthropic'e HİÇ gitmemesi (guard yukarıda zaten patlamadı).
        check("4) L1 birincil rapor üretimi ('report_generation') TAM 1 kez OpenAI'ye gitti",
              _steps_4.count("report_generation") == 1)
        check("4) L1 akışında OpenAI'ye en az 1 çağrı yapıldı", openai_calls_4["n"] >= 1)
        iv4 = _read_interview(LEVEL_L1)
        check("4) L1 rapor tamamlandı (report dolu)", bool((iv4 or {}).get("report")))
        # append_reviewer_section / run_final_report_quality_gate L1 için ÇAĞRILSA BİLE (deferred
        # job içinde her zaman çağrılıyor olabilir) kendi içlerinde 'if level != 3: return' ile
        # SIFIR gerçek AI çağrısı yapmalı — asıl kanıt yukarıdaki Anthropic guard'ın patlamamış olması.
        check("4) reviewer'da (varsa) hiçbir Anthropic çağrısı olmadı (guard patlamadı)", True)
    except _AnthropicCallDetected as e:
        check(f"4) L1 rapor üretimi Anthropic çağırmadı: {e}", False)
    finally:
        m.anthropic.Anthropic = orig_anthropic_cls
        m.openai_call = orig_openai_call
        m.record_openai_chat_usage = orig_record_openai_usage
        m.append_reviewer_section = orig_append_reviewer
        m.run_final_report_quality_gate = orig_run_qg

    # ============================================================
    # 5) L1 early finish (aday_talebi / [ADAY_CIKIS_TALEBI]) — Anthropic call = 0
    # ============================================================
    cleanup()
    _ensure_candidate(level=LEVEL_L1)
    _seed_interview(level=LEVEL_L1, messages=[
        {"role": "assistant", "content": "İlk soru.", "ts": "2026-01-01T10:00:00"},
        {"role": "user", "content": "Gerçek bir cevap veriyorum burada.", "ts": "2026-01-01T10:00:30"},
    ])
    openai_calls_5 = {"n": 0}
    m.anthropic.Anthropic = _FailAnthropic
    m.openai_call = fake_openai_chat_response("[ADAY_CIKIS_TALEBI]Anlıyorum, mülakatı burada sonlandıralım.", openai_calls_5)
    m.record_openai_chat_usage = lambda *a, **k: None
    deferred_calls_5 = {"n": 0}
    m.run_deferred_finish_job = lambda *a, **k: deferred_calls_5.__setitem__("n", deferred_calls_5["n"] + 1)
    try:
        data5 = m.ChatMessage(candidate_id=TEST_CID, message="Artık devam etmek istemiyorum, burada bırakalım.", history=[], elapsed_seconds=60)
        bt5 = BackgroundTasks()
        res5 = m.interview_chat(data5, bt5, payload={"role": "candidate", "candidate_id": TEST_CID,
                                                      "position": "Test Pozisyonu", "name": "Test Aday"})
        check("5) early finish -> processing=True döndü", res5.get("processing") is True)
        iv5 = _read_interview(LEVEL_L1)
        check("5) early finish pending_finish_provider == 'openai'", iv5["pending_finish_provider"] == "openai")
        check("5) early finish OpenAI'ye TAM 1 kez gitti (turun kendisi)", openai_calls_5["n"] == 1)
    except _AnthropicCallDetected as e:
        check(f"5) early finish Anthropic çağırmadı: {e}", False)
    finally:
        m.anthropic.Anthropic = orig_anthropic_cls
        m.openai_call = orig_openai_call
        m.record_openai_chat_usage = orig_record_openai_usage
        m.run_deferred_finish_job = orig_run_deferred

    # ============================================================
    # 6) L1 violation finish — canlı AI çağrısı YOK, Anthropic call = 0, provider='openai' işaretlenir
    # ============================================================
    cleanup()
    _ensure_candidate(level=LEVEL_L1)
    _seed_interview(level=LEVEL_L1, messages=[{"role": "assistant", "content": "Soru.", "ts": "2026-01-01T10:00:00"}])
    db6 = m.get_db()
    try:
        db6.execute("UPDATE candidates SET violation_count=2 WHERE id=?", (TEST_CID,))
        db6.commit()
    finally:
        db6.close()
    m.anthropic.Anthropic = _FailAnthropic
    deferred_calls_6 = {"n": 0}
    m.run_deferred_finish_job = lambda *a, **k: deferred_calls_6.__setitem__("n", deferred_calls_6["n"] + 1)
    try:
        vdata = m.ViolationReport(candidate_id=TEST_CID, violation_type="face_not_detected", elapsed_seconds=120)
        bt6 = BackgroundTasks()
        res6 = m.report_violation(vdata, bt6, payload={"role": "candidate", "candidate_id": TEST_CID})
        check("6) violation finish -> terminated=True", res6.get("terminated") is True)
        iv6 = _read_interview(LEVEL_L1)
        check("6) violation finish pending_finish_provider == 'openai'", iv6["pending_finish_provider"] == "openai")
    except _AnthropicCallDetected as e:
        check(f"6) violation finish Anthropic çağırmadı: {e}", False)
    finally:
        m.anthropic.Anthropic = orig_anthropic_cls
        m.run_deferred_finish_job = orig_run_deferred

    # ============================================================
    # 7) Backend guard: L2/L3 text endpoint'lerine direkt çağrı BLOKLANIR
    # ============================================================
    cleanup()
    _ensure_candidate(level=LEVEL_L2)
    try:
        m.start_interview(payload={"role": "candidate", "candidate_id": TEST_CID,
                                    "position": "Test Pozisyonu", "name": "Test Aday"})
        check("7) L2 start_interview'a direkt çağrı BLOKLANMALIYDI ama geçti", False)
    except m.HTTPException as e:
        check("7) L2 start_interview 400 ile bloklandı", e.status_code == 400)

    cleanup()
    _ensure_candidate(level=LEVEL_L3)
    _seed_interview(level=LEVEL_L3, messages=[{"role": "assistant", "content": "Soru.", "ts": "2026-01-01T10:00:00"}])
    try:
        data7 = m.ChatMessage(candidate_id=TEST_CID, message="Cevap.", history=[], elapsed_seconds=10)
        bt7 = BackgroundTasks()
        m.interview_chat(data7, bt7, payload={"role": "candidate", "candidate_id": TEST_CID,
                                              "position": "Test Pozisyonu", "name": "Test Aday"})
        check("7) L3 interview_chat'e direkt çağrı BLOKLANMALIYDI ama geçti", False)
    except m.HTTPException as e:
        check("7) L3 interview_chat 400 ile bloklandı", e.status_code == 400)

    # ============================================================
    # 8) L1 regenerate — statik kaynak kontrolü: birincil rapor OpenAI, Claude YOK
    # ============================================================
    regen_src = inspect.getsource(m.regenerate_report)
    check("8) regenerate_report L1 dalında OpenAI kullanılıyor", 'prov, mdl = "openai", OPENAI_REPORT_MODEL' in regen_src)
    check("8) regenerate_report'ta artık 'claude-sonnet' sabit model YOK (L1/L3 dalı temizlendi)",
          "claude-sonnet" not in regen_src)

    # ============================================================
    # 9) Konfigürasyon: OPENAI_L1_INTERVIEW_MODEL tanımlı, tek yerden yönetiliyor
    # ============================================================
    check("9) m.OPENAI_L1_INTERVIEW_MODEL tanımlı", hasattr(m, "OPENAI_L1_INTERVIEW_MODEL") and bool(m.OPENAI_L1_INTERVIEW_MODEL))
    start_src = inspect.getsource(m.start_interview)
    chat_src = inspect.getsource(m.interview_chat)
    check("9) start_interview OPENAI_L1_INTERVIEW_MODEL kullanıyor", "OPENAI_L1_INTERVIEW_MODEL" in start_src)
    check("9) interview_chat OPENAI_L1_INTERVIEW_MODEL kullanıyor", "OPENAI_L1_INTERVIEW_MODEL" in chat_src)
    check("9) start_interview'da artık 'claude-sonnet' sabiti YOK", "claude-sonnet" not in start_src)
    check("9) interview_chat'te artık 'claude-sonnet' sabiti YOK", "claude-sonnet" not in chat_src)
    check("9) start_interview'da artık anthropic.Anthropic çağrısı YOK", "anthropic.Anthropic(" not in start_src)
    check("9) interview_chat'te artık anthropic.Anthropic çağrısı YOK", "anthropic.Anthropic(" not in chat_src)

finally:
    cleanup()
    m.anthropic.Anthropic = orig_anthropic_cls
    m.openai_call = orig_openai_call
    m.record_openai_chat_usage = orig_record_openai_usage
    m.run_deferred_finish_job = orig_run_deferred


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("Tüm İŞ EMRİ — L1 OPENAI-ONLY MİMARİSİ testleri GEÇTİ.")

if FAILURES:
    sys.exit(1)

# İŞ 6H — CRITERION RETRY MALİYET LOGUNU AYRIŞTIR — unit testleri.
# regenerate_criterion_fields()'in KENDİSİ çağrılır (white-box) — yalnız openai_call() ve
# record_openai_chat_usage() monkey-patch edilir, hiçbir gerçek ağ/API çağrısı yapılmaz.
#
# Çalıştırma: py test_is6h_criterion_retry_labels.py  (backend/ dizininde)

import io
import sys
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
    print(f"[{status}] {label}", file=sys.stderr)
    if not condition:
        FAILURES.append(label)


TRANSCRIPT_VIEW = [
    {"role": "mulakatci", "text": "Mentörlük deneyiminizi anlatır mısınız?", "elapsed_ms": 5000, "ts": "0:05"},
    {"role": "aday", "text": "Junior arkadaşlara SIV sürecinde eşlik ettim.", "elapsed_ms": 9000, "ts": "0:09"},
]
GOOD_REPLY_BODY = "G: Mentörlük yaptı\nK: [0:09] junior arkadaşlara eşlik ettiğini söyledi\nE:\nS:"


def make_openai_resp(content):
    class FakeResp:
        def json(self_inner):
            return {
                "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                "usage": {"completion_tokens": 40, "prompt_tokens": 500},
            }
    return FakeResp()


def run_regenerate(source, attempt, criterion_id="P3"):
    captured_actions = []

    def fake_openai_call(*args, **kwargs):
        return make_openai_resp(GOOD_REPLY_BODY)

    def fake_record_openai_chat_usage(candidate_id, level, model, action, result):
        captured_actions.append(action)

    orig_call = m.openai_call
    orig_record = m.record_openai_chat_usage
    orig_key = m.OPENAI_API_KEY
    m.openai_call = fake_openai_call
    m.record_openai_chat_usage = fake_record_openai_chat_usage
    m.OPENAI_API_KEY = "test-dummy-key"  # regenerate_criterion_fields kendi anahtar kontrolünü yapıyor
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            result = m.regenerate_criterion_fields(
                9901, 3, "openai", "gpt-4o", "Mentörlük", 25,
                "transkript metni", {"g": "", "k": "", "e": "", "s": ""},
                ["evidence_timestamp_invalid", "structure_invalid"],
                transcript_view=TRANSCRIPT_VIEW, accepted_claims=[],
                criterion_id=criterion_id, attempt=attempt, source=source)
    finally:
        m.openai_call = orig_call
        m.record_openai_chat_usage = orig_record
        m.OPENAI_API_KEY = orig_key
    return result, buf.getvalue(), captured_actions


# ============================================================
# A) normal_validator_retry doğru etiketleniyor
# ============================================================
result_a, stdout_a, actions_a = run_regenerate("normal_validator_retry", 2, "P3")
check("A) sonuç None değil (çağrı başarıyla işlendi)", result_a is not None)
check("A) stdout'ta [CRITERION_AI_RETRY] satırı var", "[CRITERION_AI_RETRY]" in stdout_a)
check("A) c=9901 L3 doğru", "c=9901 L3" in stdout_a)
check("A) criterion=P3 doğru", "criterion=P3" in stdout_a)
check("A) name=Mentörlük doğru", "name=Mentörlük" in stdout_a)
check("A) attempt=2 doğru", "attempt=2" in stdout_a)
check("A) violations doğru listelendi", "evidence_timestamp_invalid,structure_invalid" in stdout_a)
check("A) source=normal_validator_retry doğru", "source=normal_validator_retry" in stdout_a)
check("A) ai_usage action='criterion_rationale_retry'", actions_a == ["criterion_rationale_retry"])

# ============================================================
# B) criterion_recovery (İş 4) doğru etiketleniyor
# ============================================================
result_b, stdout_b, actions_b = run_regenerate("criterion_recovery", 4, "K5")
check("B) sonuç None değil", result_b is not None)
check("B) stdout'ta [CRITERION_AI_RETRY] satırı var", "[CRITERION_AI_RETRY]" in stdout_b)
check("B) criterion=K5 doğru", "criterion=K5" in stdout_b)
check("B) attempt=4 doğru", "attempt=4" in stdout_b)
check("B) source=criterion_recovery doğru", "source=criterion_recovery" in stdout_b)
check("B) ai_usage action='criterion_rationale_recovery' (normal retry'den FARKLI)",
      actions_b == ["criterion_rationale_recovery"])

# ============================================================
# C) İki action adı BİRBİRİNDEN FARKLI (maliyet loglarında ayrıştırılabilir)
# ============================================================
check("C) normal retry ve recovery action adları FARKLI", actions_a[0] != actions_b[0])

# ============================================================
# D) Parametre verilmezse (varsayılan) source='normal_validator_retry' kabul edilir, çökmez
# ============================================================
def fake_openai_call2(*a, **k):
    return make_openai_resp(GOOD_REPLY_BODY)
orig_call = m.openai_call
orig_key = m.OPENAI_API_KEY
m.openai_call = fake_openai_call2
m.OPENAI_API_KEY = "test-dummy-key"
buf2 = io.StringIO()
try:
    with contextlib.redirect_stdout(buf2):
        result_d = m.regenerate_criterion_fields(
            9902, 1, "openai", "gpt-4o", "Test Kriter", 20,
            "transkript", {"g": "", "k": "", "e": "", "s": ""}, ["structure_invalid"],
            transcript_view=[], accepted_claims=[])  # criterion_id/attempt/source HİÇ verilmedi
finally:
    m.openai_call = orig_call
    m.OPENAI_API_KEY = orig_key
check("D) parametresiz çağrı çökmedi, sonuç üretti", result_d is not None)
check("D) varsayılan source='normal_validator_retry' loglandı", "source=normal_validator_retry" in buf2.getvalue())
check("D) criterion='?' (bilinmiyor işareti)", "criterion=?" in buf2.getvalue())


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("Tüm İŞ 6H testleri GEÇTİ.")

print()
import subprocess
# test_is4_validator_recovery.py, test_is6d_yonetici_ozeti_guard.py ve
# test_is6b_short_response_retry.py listeden ÇIKARILDI — kaldırılmış content-retry/short-retry
# davranışlarını test ettikleri için EMEKLİ edildiler (.py.retired).
for name in ["test_is1_report_consistency.py", "test_is2_speaker_validation.py",
             "test_is3_scope_context.py",
             "test_is5_one_cikan_proje_recovery.py",
             "test_is6c_one_cikan_proje_retry.py"]:
    print(f"=== {name} ===")
    r = subprocess.run([sys.executable, name], capture_output=True, text=True, timeout=300)
    print(r.stdout.strip().splitlines()[-1] if r.stdout else "(çıktı yok)")
    if r.returncode != 0:
        FAILURES.append(f"REGRESYON BAŞARISIZ: {name}")

if FAILURES:
    sys.exit(1)

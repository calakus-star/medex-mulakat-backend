# İŞ 6C — ONE_CIKAN_PROJE_RECOVERY SEMANTIC RETRY — GÜNCELLEME NOTU (İŞ 6N-1):
# İş 6C'nin eklediği "ilk cevap reddedilirse aynı girdiyle 1 kez daha dene" mekanizması, İş 6N-1
# teşhisinde (aynı prompt/model/context/temperature ile ikinci çağrının gerçek bilgi kazancı
# YOK olduğu kanıtlandığı için) KALDIRILDI — run_one_cikan_proje_recovery artık TEK çağrı yapıyor.
# Bu dosya artık İş 6C'nin ESKİ davranışını DEĞİL, "retry'nin KALDIRILDIĞINI" doğruluyor (regresyon
# adı/numarası korunuyor ki önceki turlarda "İş 1-6M/6N regression" listesine referans bozulmasın).
# regenerate_one_cikan_proje() monkey-patch edilir — hiçbir gerçek ağ/API çağrısı yapılmaz.
#
# Çalıştırma: py test_is6c_one_cikan_proje_retry.py  (backend/ dizininde)

import sys
import json
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


TEST_CANDIDATE_IDS = list(range(9701, 9706))
LEVEL = 1
FALLBACK = m._NO_NARRATIVE_EVIDENCE_FALLBACK

REPORT_TEMPLATE = """**Yönetici Özeti:**
Aday sektör deneyimine sahip.

**Pozisyon Yetkinlikleri:**
| Kriz Yönetimi | 20/25 | G: Kriz anında hızlı karar aldığını gösterdi ~~ K: [5:00] ısı sapmasını karantinaya aldı ~~ E: ~~ S: |

**Öne Çıkan Proje ve Deneyimler:**
""" + FALLBACK + """

**Genel Kanı:**
Aday genel olarak yeterli bulundu.
"""

STARTED_AT = "2026-01-01T10:00:00"
MESSAGES = json.dumps([
    {"role": "assistant", "content": "Denetim sürecinde yaşadığınız zorlu bir vakayı anlatır mısınız?", "ts": "2026-01-01T10:04:50"},
    {"role": "user", "content": "2019 yılında bir sahada ısı sapması tespit ettim, kendi inisiyatifimle ilacı karantinaya aldım ve sponsora bildirdim; bu sayede ürün kaybı önlendi.", "ts": "2026-01-01T10:05:00"},
])

GROUNDED_TEXT = ("[5:00] Aday, 2019 yılında bir sahada tespit ettiği ısı sapmasını kendi "
                 "inisiyatifiyle karantinaya aldığını ve sponsora bildirdiğini anlattı.")
UNGROUNDED_TEXT = "[45:00] Aday, Avrupa çapında 50 saha denetimini tek başına yönettiğini belirtti."


def seed_interview(candidate_id, report_text=REPORT_TEMPLATE):
    db = m.get_db()
    try:
        db.execute("DELETE FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, LEVEL))
        db.execute(
            "INSERT INTO interviews (candidate_id, level, messages, report, started_at, "
            "pending_finish_provider, pending_finish_model) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (candidate_id, LEVEL, MESSAGES, report_text, STARTED_AT, "claude", "claude-sonnet-4-6"))
        db.commit()
    finally:
        db.close()


def read_report(candidate_id):
    db = m.get_db()
    try:
        row = db.execute("SELECT report FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, LEVEL)).fetchone()
    finally:
        db.close()
    return (row["report"] if row else "") or ""


def with_sequence(*replies):
    """Sırayla verilen cevapları döndüren mock; fazladan çağrı yapılırsa AssertionError fırlatır —
    İş 6N-1'in 'ikinci çağrı ASLA yapılmaz' garantisini test seviyesinde de zorlar."""
    calls = {"n": 0}
    queue = list(replies)
    orig = m.regenerate_one_cikan_proje

    def wrapped(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > len(queue):
            raise AssertionError(f"regenerate_one_cikan_proje BEKLENENDEN FAZLA çağrıldı (çağrı #{calls['n']})")
        return queue[calls["n"] - 1]

    m.regenerate_one_cikan_proje = wrapped
    return calls, orig


try:
    # ============================================================
    # A) İlk cevap grounded/geçerli -> 1 çağrı, kabul
    # ============================================================
    cid = 9701
    seed_interview(cid)
    calls, orig = with_sequence(GROUNDED_TEXT)
    try:
        m.run_one_cikan_proje_recovery(cid, LEVEL, [])
    finally:
        m.regenerate_one_cikan_proje = orig
    rpt = read_report(cid)
    check("A) tam 1 çağrı", calls["n"] == 1)
    check("A) grounded metin rapora yazıldı", GROUNDED_TEXT in rpt)
    check("A) fallback ARTIK YOK", FALLBACK not in rpt)

    # ============================================================
    # B) İlk cevap 'YOK' -> İŞ 6N-1: İKİNCİ ÇAĞRI YAPILMAZ, fallback KORUNUR
    # ============================================================
    cid = 9702
    seed_interview(cid)
    calls, orig = with_sequence("YOK")  # ikinci bir eleman YOK — çağrılırsa AssertionError fırlar
    try:
        m.run_one_cikan_proje_recovery(cid, LEVEL, [])
    finally:
        m.regenerate_one_cikan_proje = orig
    rpt = read_report(cid)
    check("B) tam 1 çağrı (İKİNCİ ÇAĞRI YOK — İş 6N-1)", calls["n"] == 1)
    check("B) fallback KORUNDU (eski 'retry ile kurtar' davranışı KALDIRILDI)", FALLBACK in rpt)

    # ============================================================
    # C) İlk cevap grounding başarısız -> İKİNCİ ÇAĞRI YAPILMAZ, fallback KORUNUR
    # ============================================================
    cid = 9703
    seed_interview(cid)
    calls, orig = with_sequence(UNGROUNDED_TEXT)
    try:
        m.run_one_cikan_proje_recovery(cid, LEVEL, [])
    finally:
        m.regenerate_one_cikan_proje = orig
    rpt = read_report(cid)
    check("C) tam 1 çağrı (İKİNCİ ÇAĞRI YOK)", calls["n"] == 1)
    check("C) fallback KORUNDU", FALLBACK in rpt)
    check("C) ungrounded metin rapora YAZILMADI", UNGROUNDED_TEXT not in rpt)

    # ============================================================
    # D) İlk 'YOK' -> yalnız 1 çağrı, fallback korunur (eski D testiyle AYNI sonuç, artık 1 çağrıyla)
    # ============================================================
    cid = 9704
    seed_interview(cid)
    calls, orig = with_sequence("YOK")
    try:
        m.run_one_cikan_proje_recovery(cid, LEVEL, [])
    finally:
        m.regenerate_one_cikan_proje = orig
    rpt = read_report(cid)
    check("D) tam 1 çağrı (İKİNCİ/ÜÇÜNCÜ YOK)", calls["n"] == 1)
    check("D) fallback KORUNDU", FALLBACK in rpt)

    # ============================================================
    # E) İlk ungrounded -> yalnız 1 çağrı, fallback korunur
    # ============================================================
    cid = 9705
    seed_interview(cid)
    calls, orig = with_sequence(UNGROUNDED_TEXT)
    try:
        m.run_one_cikan_proje_recovery(cid, LEVEL, [])
    finally:
        m.regenerate_one_cikan_proje = orig
    rpt = read_report(cid)
    check("E) tam 1 çağrı", calls["n"] == 1)
    check("E) fallback KORUNDU", FALLBACK in rpt)
    check("E) ungrounded metin rapora YAZILMADI", UNGROUNDED_TEXT not in rpt)

    # ============================================================
    # F) Hiçbir senaryoda İKİNCİ çağrı yapılmadı (with_sequence zaten AssertionError ile zorluyor)
    # ============================================================
    check("F) B/C/D/E senaryolarının hiçbirinde 2. çağrı yapılmadı (queue sınırı aşılmadı)", True)

finally:
    db = m.get_db()
    try:
        for cid in TEST_CANDIDATE_IDS:
            db.execute("DELETE FROM interviews WHERE candidate_id=? AND level=?", (cid, LEVEL))
        db.commit()
    finally:
        db.close()


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("Tüm İŞ 6C testleri GEÇTİ.")

print()
print("=== İŞ 5 REGRESYON ===")
import subprocess
r5 = subprocess.run([sys.executable, "test_is5_one_cikan_proje_recovery.py"], capture_output=True, text=True)
print(r5.stdout.strip().splitlines()[-1] if r5.stdout else "(çıktı yok)")
is5_ok = r5.returncode == 0

# İŞ 6B REGRESYON kaldırıldı — test_is6b_short_response_retry.py, İŞ EMRİ — SON DAR DÜZELTME ile
# BİLİNÇLİ OLARAK KALDIRILAN davranışı (short-retry) test ettiği için EMEKLİ edildi (.py.retired) —
# artık regresyon referansı olarak ÇALIŞTIRILMAZ.

if FAILURES or not is5_ok:
    sys.exit(1)

# İŞ 6C — ONE_CIKAN_PROJE_RECOVERY TEK SEMANTIC RETRY — unit/regression testleri.
# regenerate_one_cikan_proje() monkey-patch ile kontrol edilir — hiçbir gerçek ağ/API çağrısı
# yapılmaz. Yerel SQLite'a (medex_mulakat.db) geçici satır yazılıp temizlenir.
#
# Çalıştırma: py test_is6c_one_cikan_proje_retry.py  (backend/ dizininde)

import sys
import json
import main as m

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
    """Sırayla verilen cevapları döndüren mock; fazladan çağrı yapılırsa (3.+) AssertionError fırlatır
    — 'üçüncü çağrı YAPILMADI' garantisini test seviyesinde de zorlar."""
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
    # A) İlk cevap grounded/geçerli -> 1 çağrı, retry YOK, kabul
    # ============================================================
    cid = 9701
    seed_interview(cid)
    calls, orig = with_sequence(GROUNDED_TEXT)
    try:
        m.run_one_cikan_proje_recovery(cid, LEVEL, [])
    finally:
        m.regenerate_one_cikan_proje = orig
    rpt = read_report(cid)
    check("A) tam 1 çağrı (retry TETİKLENMEDİ)", calls["n"] == 1)
    check("A) grounded metin rapora yazıldı", GROUNDED_TEXT in rpt)
    check("A) fallback ARTIK YOK", FALLBACK not in rpt)

    # ============================================================
    # B) İlk cevap 'YOK', ikinci grounded/geçerli -> 2 çağrı, ikinci kabul
    # ============================================================
    cid = 9702
    seed_interview(cid)
    calls, orig = with_sequence("YOK", GROUNDED_TEXT)
    try:
        m.run_one_cikan_proje_recovery(cid, LEVEL, [])
    finally:
        m.regenerate_one_cikan_proje = orig
    rpt = read_report(cid)
    check("B) tam 2 çağrı (1 retry)", calls["n"] == 2)
    check("B) ikinci (grounded) metin rapora yazıldı", GROUNDED_TEXT in rpt)
    check("B) fallback ARTIK YOK", FALLBACK not in rpt)

    # ============================================================
    # C) İlk cevap grounding başarısız, ikinci geçerli -> 2 çağrı, ikinci kabul
    # ============================================================
    cid = 9703
    seed_interview(cid)
    calls, orig = with_sequence(UNGROUNDED_TEXT, GROUNDED_TEXT)
    try:
        m.run_one_cikan_proje_recovery(cid, LEVEL, [])
    finally:
        m.regenerate_one_cikan_proje = orig
    rpt = read_report(cid)
    check("C) tam 2 çağrı (1 retry)", calls["n"] == 2)
    check("C) ikinci (grounded) metin rapora yazıldı", GROUNDED_TEXT in rpt)
    check("C) ungrounded ilk metin rapora YAZILMADI", UNGROUNDED_TEXT not in rpt)

    # ============================================================
    # D) İlk 'YOK', ikinci 'YOK' -> 2 çağrı, fallback korunur
    # ============================================================
    cid = 9704
    seed_interview(cid)
    calls, orig = with_sequence("YOK", "YOK")
    try:
        m.run_one_cikan_proje_recovery(cid, LEVEL, [])
    finally:
        m.regenerate_one_cikan_proje = orig
    rpt = read_report(cid)
    check("D) tam 2 çağrı (1 retry, ÜÇÜNCÜ YOK)", calls["n"] == 2)
    check("D) fallback KORUNDU", FALLBACK in rpt)

    # ============================================================
    # E) İlk ve ikinci ungrounded -> fallback korunur
    # ============================================================
    cid = 9705
    seed_interview(cid)
    calls, orig = with_sequence(UNGROUNDED_TEXT, UNGROUNDED_TEXT)
    try:
        m.run_one_cikan_proje_recovery(cid, LEVEL, [])
    finally:
        m.regenerate_one_cikan_proje = orig
    rpt = read_report(cid)
    check("E) tam 2 çağrı (1 retry, ÜÇÜNCÜ YOK)", calls["n"] == 2)
    check("E) fallback KORUNDU", FALLBACK in rpt)
    check("E) ungrounded metin rapora YAZILMADI", UNGROUNDED_TEXT not in rpt)

    # ============================================================
    # F) Üçüncü çağrı hiçbir senaryoda yapılmadı (with_sequence zaten AssertionError ile
    #    zorluyor — D/E'nin sorunsuz tamamlanmış olması bunun kanıtı; ek doğrulama:)
    # ============================================================
    check("F) D senaryosunda 3. çağrı yapılmadı (queue sınırı aşılmadı)", True)  # D zaten üstte doğrulandı
    check("F) E senaryosunda 3. çağrı yapılmadı (queue sınırı aşılmadı)", True)  # E zaten üstte doğrulandı

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

print("=== İŞ 6B REGRESYON ===")
r6b = subprocess.run([sys.executable, "test_is6b_short_response_retry.py"], capture_output=True, text=True)
print(r6b.stdout.strip().splitlines()[-1] if r6b.stdout else "(çıktı yok)")
is6b_ok = r6b.returncode == 0

if FAILURES or not (is5_ok and is6b_ok):
    sys.exit(1)

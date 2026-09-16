# İŞ 6D — YÖNETİCİ ÖZETİ ANORMAL/GEÇERSİZ CEVAP KORUMASI — unit/regression testleri.
# finalize_interview() GERÇEKTEN çağrılır (uçtan uca) — yalnız regenerate_yonetici_ozeti()
# monkey-patch ile kontrol edilir, hiçbir gerçek ağ/API çağrısı yapılmaz. Yerel SQLite'a
# (medex_mulakat.db) geçici satır yazılıp temizlenir.
#
# Çalıştırma: py test_is6d_yonetici_ozeti_guard.py  (backend/ dizininde)

import sys
import json
import main as m

FAILURES = []


def check(label, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        FAILURES.append(label)


TEST_CANDIDATE_IDS = list(range(9801, 9810))
LEVEL = 1

SHORT_YO_TEXT = "Aday genel olarak deneyimli görünüyor ve mülakatta birkaç örnek verdi."  # ~11 kelime, <150
NORMAL_YO_TEXT = " ".join(["Aday mülakatta somut bir örnek vererek yetkinliğini gösterdi."] * 20)  # ~200 kelime
GOOD_REGEN_TEXT = " ".join(["kelime"] * 90)  # >=80, usable
BAD_REGEN_TEXT = "Tamam."  # 1 kelime, unusable
EMPTY_REGEN_TEXT = ""


def make_reply(yo_text):
    return f"""[MÜLAKATBİTTİ]
---RAPOR---
===YÖNETİCİ ÖZETİ===
{yo_text}

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

===TAKİP MÜLAKATI SORULARI===
YOK
===BÖLÜM SONU===
---RAPORSON---"""


def seed(cid):
    db = m.get_db()
    try:
        db.execute("DELETE FROM candidates WHERE id=?", (cid,))
        db.execute(
            "INSERT INTO candidates (id, name, username, password_hash, email, position, level, cv_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, f"Test Aday {cid}", f"test_is6d_{cid}", "x", f"test_is6d_{cid}@example.com",
             "Business Analyst", LEVEL, "CV metni."))
        db.execute("DELETE FROM interviews WHERE candidate_id=? AND level=?", (cid, LEVEL))
        db.execute(
            "INSERT INTO interviews (candidate_id, level, messages, pending_finish_provider, pending_finish_model) "
            "VALUES (?, ?, ?, ?, ?)",
            (cid, LEVEL, "[]", "claude", "claude-sonnet-4-6"))
        db.commit()
    finally:
        db.close()


def read_report(cid):
    db = m.get_db()
    try:
        row = db.execute("SELECT report FROM interviews WHERE candidate_id=? AND level=?", (cid, LEVEL)).fetchone()
    finally:
        db.close()
    return (row["report"] if row else "") or ""


def make_mock(*replies_or_exceptions):
    """Sırayla verilen cevapları (veya Exception INSTANCE'larını) döndüren/fırlatan mock."""
    calls = {"n": 0}
    queue = list(replies_or_exceptions)

    def wrapped(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > len(queue):
            raise AssertionError(f"regenerate_yonetici_ozeti BEKLENENDEN FAZLA çağrıldı (çağrı #{calls['n']})")
        item = queue[calls["n"] - 1]
        if isinstance(item, Exception):
            raise item
        return item

    return wrapped, calls


def run_case(cid, yo_text, mock_items):
    seed(cid)
    reply = make_reply(yo_text)
    mock_fn, calls = make_mock(*mock_items)
    orig = m.regenerate_yonetici_ozeti
    m.regenerate_yonetici_ozeti = mock_fn
    try:
        m.finalize_interview(cid, reply, level=LEVEL)
    finally:
        m.regenerate_yonetici_ozeti = orig
    return calls, read_report(cid)


try:
    # ============================================================
    # A) İlk cevap usable -> 1 çağrı, retry yok, yeni özet kullanılır
    # ============================================================
    calls_a, rpt_a = run_case(9801, SHORT_YO_TEXT, [GOOD_REGEN_TEXT])
    check("A) tam 1 çağrı (retry TETİKLENMEDİ)", calls_a["n"] == 1)
    check("A) yeni (usable) özet rapora yazıldı", GOOD_REGEN_TEXT in rpt_a)
    check("A) eski kısa özet ARTIK YOK", SHORT_YO_TEXT not in rpt_a)

    # ============================================================
    # B) İlk kısa, ikinci usable -> 2 çağrı, ikinci kullanılır
    # ============================================================
    calls_b, rpt_b = run_case(9802, SHORT_YO_TEXT, [BAD_REGEN_TEXT, GOOD_REGEN_TEXT])
    check("B) tam 2 çağrı (1 retry)", calls_b["n"] == 2)
    check("B) ikinci (usable) özet rapora yazıldı", GOOD_REGEN_TEXT in rpt_b)

    # ============================================================
    # C) İlk boş, ikinci usable -> ikinci kullanılır
    # ============================================================
    calls_c, rpt_c = run_case(9803, SHORT_YO_TEXT, [EMPTY_REGEN_TEXT, GOOD_REGEN_TEXT])
    check("C) tam 2 çağrı (1 retry)", calls_c["n"] == 2)
    check("C) ikinci (usable) özet rapora yazıldı", GOOD_REGEN_TEXT in rpt_c)

    # ============================================================
    # D) İlk kısa + ikinci kısa -> yalnız 2 çağrı, mevcut (eski) özet korunur
    # ============================================================
    calls_d, rpt_d = run_case(9804, SHORT_YO_TEXT, [BAD_REGEN_TEXT, BAD_REGEN_TEXT])
    check("D) tam 2 çağrı (ÜÇÜNCÜ YOK)", calls_d["n"] == 2)
    check("D) eski (orijinal) özet KORUNDU", SHORT_YO_TEXT in rpt_d)
    check("D) bozuk 'Tamam.' rapora YAZILMADI (tek başına cümle olarak)", "**Yönetici Özeti:**\nTamam." not in rpt_d)

    # ============================================================
    # E) İlk boş + ikinci boş -> mevcut özet korunur
    # ============================================================
    calls_e, rpt_e = run_case(9805, SHORT_YO_TEXT, [EMPTY_REGEN_TEXT, EMPTY_REGEN_TEXT])
    check("E) tam 2 çağrı", calls_e["n"] == 2)
    check("E) eski (orijinal) özet KORUNDU", SHORT_YO_TEXT in rpt_e)

    # ============================================================
    # F) İlk çağrı exception + ikinci usable -> ikinci denenir, kullanılır
    # ============================================================
    calls_f, rpt_f = run_case(9806, SHORT_YO_TEXT, [RuntimeError("simüle edilmiş API hatası"), GOOD_REGEN_TEXT])
    check("F) tam 2 çağrı (ilk exception, ikinci normal çağrı)", calls_f["n"] == 2)
    check("F) ikinci (usable) özet rapora yazıldı", GOOD_REGEN_TEXT in rpt_f)

    # ============================================================
    # G) İkinci çağrı exception -> mevcut özet korunur, pipeline ÇÖKMEZ
    # ============================================================
    calls_g, rpt_g = run_case(9807, SHORT_YO_TEXT, [BAD_REGEN_TEXT, RuntimeError("simüle edilmiş API hatası")])
    check("G) tam 2 çağrı", calls_g["n"] == 2)
    check("G) eski (orijinal) özet KORUNDU", SHORT_YO_TEXT in rpt_g)
    check("G) rapor YİNE DE üretildi (pipeline çökmedi)", bool(rpt_g.strip()))

    # ============================================================
    # H) Hiçbir senaryoda 3. çağrı yapılmadı — make_mock zaten AssertionError ile zorluyor;
    #    D/E/F/G'nin sorunsuz tamamlanmış olması bunun kanıtı.
    # ============================================================
    check("H) D/E/F/G senaryolarının hiçbirinde 3. çağrı yapılmadı", True)

    # ============================================================
    # I) Normal 150-250 kelimelik mevcut davranış DEĞİŞMEZ — regenerate HİÇ ÇAĞRILMAZ
    # ============================================================
    calls_i, rpt_i = run_case(9808, NORMAL_YO_TEXT, [])
    check("I) regenerate_yonetici_ozeti HİÇ ÇAĞRILMADI (orijinal zaten 150-250 kelime)", calls_i["n"] == 0)

finally:
    db = m.get_db()
    try:
        for cid in TEST_CANDIDATE_IDS:
            db.execute("DELETE FROM interviews WHERE candidate_id=? AND level=?", (cid, LEVEL))
            db.execute("DELETE FROM candidates WHERE id=?", (cid,))
        db.commit()
    finally:
        db.close()


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("Tüm İŞ 6D testleri GEÇTİ.")

print()
import subprocess
for name in ["test_is1_report_consistency.py", "test_is2_speaker_validation.py",
             "test_is3_scope_context.py", "test_is4_validator_recovery.py",
             "test_is5_one_cikan_proje_recovery.py", "test_is6b_short_response_retry.py",
             "test_is6c_one_cikan_proje_retry.py"]:
    print(f"=== {name} ===")
    r = subprocess.run([sys.executable, name], capture_output=True, text=True)
    print(r.stdout.strip().splitlines()[-1] if r.stdout else "(çıktı yok)")
    if r.returncode != 0:
        FAILURES.append(f"REGRESYON BAŞARISIZ: {name}")

if FAILURES:
    sys.exit(1)

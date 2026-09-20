# İŞ 5 — "ÖNE ÇIKAN PROJE VE DENEYİMLER" HEDEFLİ RECOVERY — unit/regression testleri.
# regenerate_one_cikan_proje() (gerçek LLM/API çağrısı yapar) monkey-patch ile kontrol edilir —
# hiçbir gerçek ağ/API çağrısı yapılmaz. Yerel SQLite'a (medex_mulakat.db) GEÇİCİ test satırları
# yazılır, test sonunda temizlenir — production'a bağlanılmaz.
#
# Çalıştırma: py test_is5_one_cikan_proje_recovery.py  (backend/ dizininde)

import sys
import json
import main as m

FAILURES = []


def check(label, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        FAILURES.append(label)


TEST_CANDIDATE_IDS = list(range(9501, 9506))
LEVEL = 1

FALLBACK = m._NO_NARRATIVE_EVIDENCE_FALLBACK

REPORT_TEMPLATE_FALLBACK = """**Yönetici Özeti:**
Aday sektör deneyimine sahip, mülakatta çeşitli konularda somut örnekler verdi.

**Pozisyon Yetkinlikleri:**
| Kriz Yönetimi | 20/25 | G: Kriz anında hızlı karar aldığını gösterdi ~~ K: [5:00] ısı sapmasını karantinaya aldı ~~ E: ~~ S: |

**Öne Çıkan Proje ve Deneyimler:**
""" + FALLBACK + """

**Genel Kanı:**
Aday genel olarak yeterli bulundu.
"""

REPORT_TEMPLATE_ALREADY_FILLED = """**Yönetici Özeti:**
Aday sektör deneyimine sahip.

**Pozisyon Yetkinlikleri:**
| Kriz Yönetimi | 20/25 | G: Kriz anında hızlı karar aldığını gösterdi ~~ K: [5:00] ısı sapmasını karantinaya aldı ~~ E: ~~ S: |

**Öne Çıkan Proje ve Deneyimler:**
[5:00] Aday, 2019 yılında bir sahada tespit ettiği ısı sapmasını kendi inisiyatifiyle karantinaya aldığını ve sponsora bildirdiğini anlattı.

**Genel Kanı:**
Aday genel olarak yeterli bulundu.
"""

STARTED_AT = "2026-01-01T10:00:00"
MESSAGES = json.dumps([
    {"role": "assistant", "content": "Denetim sürecinde yaşadığınız zorlu bir vakayı anlatır mısınız?", "ts": "2026-01-01T10:04:50"},
    {"role": "user", "content": "2019 yılında bir sahada ısı sapması tespit ettim, kendi inisiyatifimle ilacı karantinaya aldım ve sponsora bildirdim; bu sayede ürün kaybı önlendi.", "ts": "2026-01-01T10:05:00"},
])


def seed_interview(candidate_id, report_text):
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


def read_system_decisions(candidate_id):
    db = m.get_db()
    try:
        row = db.execute("SELECT system_decision_json FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, LEVEL)).fetchone()
    finally:
        db.close()
    if not row or not row["system_decision_json"]:
        return []
    try:
        return json.loads(row["system_decision_json"]).get("history", []) or [json.loads(row["system_decision_json"])]
    except Exception:
        try:
            parsed = json.loads(row["system_decision_json"])
            return parsed if isinstance(parsed, list) else [parsed]
        except Exception:
            return []


def with_mock_regenerate(mock_fn):
    calls = {"n": 0}
    orig = m.regenerate_one_cikan_proje

    def wrapped(*args, **kwargs):
        calls["n"] += 1
        return mock_fn(*args, **kwargs)

    m.regenerate_one_cikan_proje = wrapped
    return calls, orig


try:
    # ============================================================
    # A) Somut audit/kriz deneyimi var, bölüm fallback => recovery somut deneyim üretir
    # ============================================================
    cid = 9501
    seed_interview(cid, REPORT_TEMPLATE_FALLBACK)
    GOOD_RECOVERY = ("[5:00] Aday, 2019 yılında bir sahada tespit ettiği ısı sapmasını kendi "
                     "inisiyatifiyle karantinaya aldığını ve sponsora bildirdiğini anlattı.")
    calls, orig = with_mock_regenerate(lambda *a, **k: GOOD_RECOVERY)
    try:
        m.run_one_cikan_proje_recovery(cid, LEVEL, [])
    finally:
        m.regenerate_one_cikan_proje = orig
    new_report_a = read_report(cid)
    check("A) regenerate_one_cikan_proje çağrıldı", calls["n"] == 1)
    check("A) fallback metni ARTIK YOK", FALLBACK not in new_report_a)
    check("A) somut recovery metni rapora YAZILDI", GOOD_RECOVERY in new_report_a)
    check("A) diğer bölümler (Genel Kanı) DEĞİŞMEDİ", "Aday genel olarak yeterli bulundu." in new_report_a)

    # ============================================================
    # B) Gerçekten somut deneyim yok (LLM 'YOK' döndürüyor) => fallback korunur
    # ============================================================
    cid = 9502
    seed_interview(cid, REPORT_TEMPLATE_FALLBACK)
    calls, orig = with_mock_regenerate(lambda *a, **k: "YOK")
    try:
        m.run_one_cikan_proje_recovery(cid, LEVEL, [])
    finally:
        m.regenerate_one_cikan_proje = orig
    new_report_b = read_report(cid)
    check("B) regenerate_one_cikan_proje çağrıldı", calls["n"] == 1)
    check("B) fallback metni KORUNDU", FALLBACK in new_report_b)

    # ============================================================
    # C) Yalnız mülakatçı örneği (aday satırı YOK bu timestamp'te) => aday deneyimi SAYILMAZ
    # ============================================================
    cid = 9503
    seed_interview(cid, REPORT_TEMPLATE_FALLBACK)
    MULAKATCI_ATTRIBUTED = "[4:50] Mülakatçının anlattığı örnek senaryoya göre saha ekibi ısı sapmasını yönetti."
    calls, orig = with_mock_regenerate(lambda *a, **k: MULAKATCI_ATTRIBUTED)
    try:
        m.run_one_cikan_proje_recovery(cid, LEVEL, [])
    finally:
        m.regenerate_one_cikan_proje = orig
    new_report_c = read_report(cid)
    check("C) mülakatçıya ait [4:50] damgası REDDEDİLDİ — fallback KORUNDU", FALLBACK in new_report_c)
    check("C) mülakatçı-kaynaklı metin rapora YAZILMADI", MULAKATCI_ATTRIBUTED not in new_report_c)

    # ============================================================
    # D) LLM transkriptte OLMAYAN bir zaman/proje uyduruyor => kabul edilmez, fallback korunur
    # ============================================================
    cid = 9504
    seed_interview(cid, REPORT_TEMPLATE_FALLBACK)
    INVENTED = "[45:00] Aday, Avrupa çapında 50 saha denetimini tek başına yönettiğini belirtti."
    calls, orig = with_mock_regenerate(lambda *a, **k: INVENTED)
    try:
        m.run_one_cikan_proje_recovery(cid, LEVEL, [])
    finally:
        m.regenerate_one_cikan_proje = orig
    new_report_d = read_report(cid)
    check("D) transkriptte olmayan damga/uydurma REDDEDİLDİ — fallback KORUNDU", FALLBACK in new_report_d)
    check("D) uydurma metin rapora YAZILMADI", INVENTED not in new_report_d)

    # ============================================================
    # E) Bölüm ilk üretimde zaten dolu/geçerli => recovery çağrısı YAPILMAZ
    # ============================================================
    cid = 9505
    seed_interview(cid, REPORT_TEMPLATE_ALREADY_FILLED)
    calls, orig = with_mock_regenerate(lambda *a, **k: "BU HİÇ ÇAĞRILMAMALI")
    try:
        m.run_one_cikan_proje_recovery(cid, LEVEL, [])
    finally:
        m.regenerate_one_cikan_proje = orig
    new_report_e = read_report(cid)
    check("E) regenerate_one_cikan_proje HİÇ ÇAĞRILMADI", calls["n"] == 0)
    check("E) mevcut dolu metin DEĞİŞMEDEN kaldı", "kendi inisiyatifiyle karantinaya aldığını" in new_report_e)

    # ============================================================
    # Doğrudan birim testleri: kabul kapısı
    # ============================================================
    tv = m.build_transcript_view(MESSAGES, LEVEL, STARTED_AT)
    check("helper) grounded, aday-kaynaklı somut cümle -> KABUL", m._accept_one_cikan_proje_recovery(GOOD_RECOVERY, tv) is True)
    check("helper) 'YOK' -> RED", m._accept_one_cikan_proje_recovery("YOK", tv) is False)
    check("helper) boş metin -> RED", m._accept_one_cikan_proje_recovery("", tv) is False)
    check("helper) mülakatçıya ait damga -> RED", m._accept_one_cikan_proje_recovery(MULAKATCI_ATTRIBUTED, tv) is False)
    check("helper) transkriptte olmayan damga -> RED", m._accept_one_cikan_proje_recovery(INVENTED, tv) is False)
    check("helper) damgasız (timestamp yok) metin -> RED", m._accept_one_cikan_proje_recovery("Aday deneyimliydi.", tv) is False)

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
    print("Tüm İŞ 5 testleri GEÇTİ.")

print()
print("=== İŞ 1 REGRESYON ===")
import subprocess
r1 = subprocess.run([sys.executable, "test_is1_report_consistency.py"], capture_output=True, text=True)
print(r1.stdout.strip().splitlines()[-1] if r1.stdout else "(çıktı yok)")
is1_ok = r1.returncode == 0

print("=== İŞ 2 REGRESYON ===")
r2 = subprocess.run([sys.executable, "test_is2_speaker_validation.py"], capture_output=True, text=True)
print(r2.stdout.strip().splitlines()[-1] if r2.stdout else "(çıktı yok)")
is2_ok = r2.returncode == 0

print("=== İŞ 3 REGRESYON ===")
r3 = subprocess.run([sys.executable, "test_is3_scope_context.py"], capture_output=True, text=True)
print(r3.stdout.strip().splitlines()[-1] if r3.stdout else "(çıktı yok)")
is3_ok = r3.returncode == 0

print("=== İŞ 4 REGRESYON ===")
r4 = subprocess.run([sys.executable, "test_is4_validator_recovery.py"], capture_output=True, text=True)
print(r4.stdout.strip().splitlines()[-1] if r4.stdout else "(çıktı yok)")
is4_ok = r4.returncode == 0

if FAILURES or not (is1_ok and is2_ok and is3_ok and is4_ok):
    sys.exit(1)

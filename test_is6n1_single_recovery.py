# İŞ 6N-1 — ONE_CIKAN_PROJE RECOVERY'Yİ TEK VE ODAKLI ÇAĞRIYA DÖNÜŞTÜR — unit/regression testleri.
# Tamamen JENERİK metinlerle — hiçbir aday/pozisyon/kriter/candidate_id/sektöre özel hardcode yok.
# regenerate_one_cikan_proje() monkey-patch edilir — hiçbir gerçek ağ/API çağrısı yapılmaz.
#
# Çalıştırma: py test_is6n1_single_recovery.py  (backend/ dizininde)

import io
import sys
import json
import contextlib
import main as m

FAILURES = []


def check(label, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        FAILURES.append(label)


TEST_CANDIDATE_IDS = list(range(9901, 9906))
LEVEL = 1
FALLBACK = m._NO_NARRATIVE_EVIDENCE_FALLBACK

# ============================================================
# Jenerik transkript — herhangi bir sektöre/pozisyona özel kelime YOK.
# Mülakatçı da (test D için) bir "proje/başarı" örneği anlatıyor ama ADAY bunu DOĞRULAMIYOR.
# ============================================================
TRANSCRIPT_VIEW = [
    {"role": "mulakatci", "text": "Deneyiminizden bahseder misiniz?", "elapsed_ms": 5000, "ts": "0:05"},
    {"role": "aday", "text": "2020 yılında bir süreç iyileştirme çalışmasını baştan sona yönettim ve teslim süresini kısalttım.", "elapsed_ms": 9000, "ts": "0:09"},
    {"role": "mulakatci", "text": "Bir meslektaşınız büyük bir başarı öyküsü paylaşmıştı, siz de böyle bir proje yürüttünüz mü?", "elapsed_ms": 15000, "ts": "0:15"},
    {"role": "aday", "text": "Genel olarak iletişimim güçlüdür.", "elapsed_ms": 20000, "ts": "0:20"},
]

# ============================================================
# A) Aday açık bir proje anlatıyor -> evidence context'te bulunmalı
# ============================================================
ctx = m._build_candidate_evidence_context(TRANSCRIPT_VIEW)
check("A) adayın proje anlatımı context'te VAR",
      "2020 yılında bir süreç iyileştirme çalışmasını baştan sona yönettim ve teslim süresini kısalttım." in ctx)
check("A) context'te zaman damgası VAR ([0:09])", "[0:09]" in ctx)

# ============================================================
# B) 'proje' kelimesi hiç geçmese bile somut operasyonel deneyim context'te KORUNUYOR
# ============================================================
TRANSCRIPT_VIEW_B = [
    {"role": "aday", "text": "Bir denetim sürecini baştan sona ben yürüttüm, bulguları raporladım.", "elapsed_ms": 3000, "ts": "0:03"},
]
ctx_b = m._build_candidate_evidence_context(TRANSCRIPT_VIEW_B)
check("B) 'proje' kelimesi YOK ama denetim/operasyonel deneyim context'te VAR",
      "Bir denetim sürecini baştan sona ben yürüttüm, bulguları raporladım." in ctx_b)
check("B) evidence context 'proje' kelimesi ARAMIYOR (filtre kelimeye bağlı değil)", "proje" not in ctx_b.lower())

# ============================================================
# C) Yalnız genel/soyut cevap -> context'e girer (rol-bazlı filtre, içerik yargısı YOK) ama
#    model 'YOK' derse sistem UYDURMUYOR (fallback korunuyor) — uçtan uca doğrulama aşağıda (test grubu 2)
# ============================================================
check("C) soyut cevap da (rol=aday olduğu için) context'e giriyor — filtre İÇERİK yargısı yapmıyor",
      "Genel olarak iletişimim güçlüdür." in ctx)

# ============================================================
# D) Mülakatçının proje/başarı örneği context'e HİÇ GİRMİYOR
# ============================================================
check("D) mülakatçının 'proje' örneği context'te YOK",
      "Bir meslektaşınız büyük bir başarı öyküsü paylaşmıştı, siz de böyle bir proje yürüttünüz mü?" not in ctx)
check("D) context'te 'Mülakatçı:' etiketi HİÇ YOK (yalnız 'Aday:' satırları)", "Mülakatçı:" not in ctx)

print()
for label in ("A", "B", "C", "D"):
    pass  # yukarıdaki check'ler zaten bastı


# ============================================================
# Uçtan uca senaryolar — run_one_cikan_proje_recovery
# ============================================================
GROUNDED_TEXT = "[0:09] Aday, 2020 yılında bir süreç iyileştirme çalışmasını baştan sona yönettiğini ve teslim süresini kısalttığını anlattı."
UNGROUNDED_TEXT = "[59:59] Aday, bambaşka bir olayı anlattı."

REPORT_TEMPLATE = """**Yönetici Özeti:**
Aday hakkında kısa bir özet.

**Pozisyon Yetkinlikleri:**
| Kriter Bir | 20/25 | G: Deneyim gösterdi ~~ K: [0:09] süreç iyileştirme çalışmasını yönettiğini söyledi ~~ E: ~~ S: |

**Öne Çıkan Proje ve Deneyimler:**
""" + FALLBACK + """

**Genel Kanı:**
Aday genel olarak yeterli bulundu.
"""

STARTED_AT = "2026-01-01T10:00:00"
MESSAGES = json.dumps([
    {"role": "assistant", "content": "Deneyiminizden bahseder misiniz?", "ts": "2026-01-01T10:00:05"},
    {"role": "user", "content": "2020 yılında bir süreç iyileştirme çalışmasını baştan sona yönettim ve teslim süresini kısalttım.", "ts": "2026-01-01T10:00:09"},
])


def seed(cid, report_text=REPORT_TEMPLATE):
    db = m.get_db()
    try:
        db.execute("DELETE FROM interviews WHERE candidate_id=? AND level=?", (cid, LEVEL))
        db.execute(
            "INSERT INTO interviews (candidate_id, level, messages, report, started_at, "
            "pending_finish_provider, pending_finish_model) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cid, LEVEL, MESSAGES, report_text, STARTED_AT, "claude", "claude-sonnet-4-6"))
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


def run_with_mock(cid, mock_return):
    seed(cid)
    calls = {"n": 0}
    orig = m.regenerate_one_cikan_proje

    def wrapped(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise AssertionError(f"regenerate_one_cikan_proje BEKLENMEDİK 2. kez çağrıldı (İş 6N-1 ihlali)")
        return mock_return
    m.regenerate_one_cikan_proje = wrapped
    try:
        m.run_one_cikan_proje_recovery(cid, LEVEL, [])
    finally:
        m.regenerate_one_cikan_proje = orig
    return calls["n"], read_report(cid)


try:
    # C (uçtan uca) — model 'YOK' derse sistem uydurmuyor
    n_c, rpt_c = run_with_mock(9902, "YOK")
    check("C-uçtan-uca) model YOK derse fallback KORUNUR (uydurma YOK)", FALLBACK in rpt_c)
    check("C-uçtan-uca) tek çağrı yapıldı", n_c == 1)

    # E) Candidate timestamp grounding korunuyor — gerçek grounded metin kabul edilir
    n_e, rpt_e = run_with_mock(9903, GROUNDED_TEXT)
    check("E) grounded metin KABUL EDİLDİ, bölüm güncellendi", GROUNDED_TEXT in rpt_e)
    check("E) fallback ARTIK YOK", FALLBACK not in rpt_e)

    # F) İlk recovery reddedilirse İKİNCİ AI çağrısı YAPILMIYOR (ungrounded -> tek çağrı, fallback korunur)
    n_f, rpt_f = run_with_mock(9904, UNGROUNDED_TEXT)
    check("F) ungrounded ilk cevap reddedildi, İKİNCİ ÇAĞRI YAPILMADI (tek çağrı)", n_f == 1)
    check("F) fallback KORUNDU", FALLBACK in rpt_f)
    check("F) ungrounded metin rapora YAZILMADI", UNGROUNDED_TEXT not in rpt_f)

    # G) İlk recovery kabul edilirse mevcut patch davranışı korunuyor (E ile aynı senaryo, ayrıca doğrulandı)
    check("G) kabul edilen recovery yalnız 'Öne Çıkan Proje ve Deneyimler' bölümünü değiştirdi (Genel Kanı sabit kaldı)",
          "Aday genel olarak yeterli bulundu." in rpt_e)
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
    print("Tüm İŞ 6N-1 testleri GEÇTİ.")

if FAILURES:
    sys.exit(1)

# İŞ 6W-FIX1 — SEMANTIC_ISSUE PARSER LOSS FIX — unit/regression testleri.
# Tamamen JENERİK/sentetik metinlerle — hiçbir aday/pozisyon/kriter/candidate_id'ye özel hardcode
# yok. Hiçbir gerçek ağ/API çağrısı yapılmaz.
#
# Çalıştırma: py test_is6w_fix1_semantic_issue_loss.py  (backend/ dizininde)

import sys
import main as m

FAILURES = []


def check(label, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        FAILURES.append(label)


def merged_result(raw_text: str) -> dict:
    """İş 6W-FIX1 sonrası gerçek davranışı (append_reviewer_section'daki AYNI çağrı sırası) simüle eder."""
    notes_wo_confidence = m._strip_confidence_block(raw_text)
    free_raw, scores_raw = m._split_reviewer_output(notes_wo_confidence)
    return m._merge_semantic_issues(
        m.parse_reviewer_semantic_issues(free_raw),
        m.parse_reviewer_semantic_issues(scores_raw),
    )


# ============================================================
# 1) SEMANTIC_ISSUE yalnız free_raw'da (KRİTER PUANLARI başlığından ÖNCE) -> P1 yakalanmalı
# ============================================================
raw_1 = """Raporda bir sorun görüyorum.
SEMANTIC_ISSUE: P1 = sorun A

=== ADAY ÖZGÜVENİ İZLENİMİ ===
YETERSİZ VERİ

=== KRİTER PUANLARI ===
GUVEN_DUZEYI: yüksek
"""
result_1 = merged_result(raw_1)
check("1) BEFORE (eski 'or' davranışı) kaybediyordu — doğrulama",
      m.parse_reviewer_semantic_issues(m._split_reviewer_output(m._strip_confidence_block(raw_1))[1]
                                       or m._strip_confidence_block(raw_1)) == {})
check("1) AFTER — free_raw'daki SEMANTIC_ISSUE (P1) yakalandı", result_1.get("P1") == "sorun A")

# ============================================================
# 2) SEMANTIC_ISSUE yalnız scores_raw'da (KRİTER PUANLARI bloğu içinde) -> P1 yakalanmalı (mevcut davranış)
# ============================================================
raw_2 = """GÖRÜŞ YOK

=== ADAY ÖZGÜVENİ İZLENİMİ ===
YETERSİZ VERİ

=== KRİTER PUANLARI ===
SEMANTIC_ISSUE: P1 = sorun A
GUVEN_DUZEYI: yüksek
"""
result_2 = merged_result(raw_2)
check("2) scores_raw'daki SEMANTIC_ISSUE (P1) yakalandı", result_2.get("P1") == "sorun A")

# ============================================================
# 3) free_raw'da P1, scores_raw'da P2 -> ikisi de yakalanmalı
# ============================================================
raw_3 = """Bir gözlemim var.
SEMANTIC_ISSUE: P1 = sorun A

=== ADAY ÖZGÜVENİ İZLENİMİ ===
YETERSİZ VERİ

=== KRİTER PUANLARI ===
SEMANTIC_ISSUE: P2 = sorun B
GUVEN_DUZEYI: yüksek
"""
result_3 = merged_result(raw_3)
check("3) free_raw P1 yakalandı", result_3.get("P1") == "sorun A")
check("3) scores_raw P2 yakalandı", result_3.get("P2") == "sorun B")
check("3) toplam 2 kayıt (başka veri sızmadı)", len(result_3) == 2)

# ============================================================
# 4) Aynı P1, aynı issue metni iki tarafta -> duplicate render OLMAMALI (tek kayıt, tekrar YOK)
# ============================================================
raw_4 = """Bir gözlemim var.
SEMANTIC_ISSUE: P1 = sorun A

=== ADAY ÖZGÜVENİ İZLENİMİ ===
YETERSİZ VERİ

=== KRİTER PUANLARI ===
SEMANTIC_ISSUE: P1 = sorun A
GUVEN_DUZEYI: yüksek
"""
result_4 = merged_result(raw_4)
check("4) AYNI metin iki tarafta -> tek kayıt (duplicate YOK)", result_4.get("P1") == "sorun A")
check("4) ' | ' ile tekrarlanmadı", " | " not in (result_4.get("P1") or ""))

# 4b) Aynı P1, FARKLI metin iki tarafta -> veri KAYBEDİLMEMELİ (ikisi de korunur)
raw_4b = """Bir gözlemim var.
SEMANTIC_ISSUE: P1 = sorun A

=== ADAY ÖZGÜVENİ İZLENİMİ ===
YETERSİZ VERİ

=== KRİTER PUANLARI ===
SEMANTIC_ISSUE: P1 = sorun B (farklı)
GUVEN_DUZEYI: yüksek
"""
result_4b = merged_result(raw_4b)
check("4b) FARKLI metin -> her ikisi de KORUNDU (veri kaybı yok)",
      "sorun A" in result_4b.get("P1", "") and "sorun B (farklı)" in result_4b.get("P1", ""))

# ============================================================
# 5) SEMANTIC_ISSUE hiç yok -> mevcut boş davranış
# ============================================================
raw_5 = """GÖRÜŞ YOK

=== ADAY ÖZGÜVENİ İZLENİMİ ===
YETERSİZ VERİ

=== KRİTER PUANLARI ===
GUVEN_DUZEYI: yüksek
"""
result_5 = merged_result(raw_5)
check("5) SEMANTIC_ISSUE yok -> boş sözlük", result_5 == {})

# ============================================================
# 6) Bilinmeyen kimlik (P99) -> parser YİNE YAKALAR (mevcut davranış — ignore, build_semantic_issue_block
#    render aşamasında yapılır, parser seviyesinde DEĞİL); burada yalnız parser'ın crash etmediğini
#    ve build_semantic_issue_block'un bilinmeyen kimliği SESSİZCE atladığını doğruluyoruz.
# ============================================================
raw_6 = """GÖRÜŞ YOK

=== ADAY ÖZGÜVENİ İZLENİMİ ===
YETERSİZ VERİ

=== KRİTER PUANLARI ===
SEMANTIC_ISSUE: P99 = bilinmeyen kriter
GUVEN_DUZEYI: yüksek
"""
result_6 = merged_result(raw_6)
check("6) parser P99'u yakalar (crash yok) — ignore render aşamasında olur", result_6.get("P99") == "bilinmeyen kriter")
POS_CRITERIA = [{"name": "Test Kriteri Bir", "weight": 25, "desc": "test"}]
block_6 = m.build_semantic_issue_block(result_6, POS_CRITERIA, m.PROFILE_CRITERIA)
check("6) build_semantic_issue_block bilinmeyen P99'u SESSİZCE atladı (mevcut ignore davranışı)",
      "P99" not in block_6 and "bilinmeyen kriter" not in block_6)


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("Tüm İŞ 6W-FIX1 testleri GEÇTİ.")

if FAILURES:
    sys.exit(1)

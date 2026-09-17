# İŞ 6K — EVIDENCE TIMESTAMP RETRY HINT ROLE TUTARLILIĞI — unit/regression testleri.
# Tamamen JENERİK kriter adı/transkript ile — hiçbir adaya/pozisyona/kritere özel hardcode yok.
# Hiçbir gerçek ağ/API çağrısı yapılmaz (yalnız saf fonksiyonlar test ediliyor).
#
# Çalıştırma: py test_is6k_evidence_hint_role.py  (backend/ dizininde)

import sys
import main as m

FAILURES = []


def check(label, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        FAILURES.append(label)


# Jenerik kriter adı — herhangi bir gerçek pozisyona/adaya özel değil.
CRITERION_NAME = "Örnek Yetkinlik Alanı"

# Jenerik transkript: mülakatçının satırı kriterin kelimelerini LEKSİK olarak GÜÇLÜ şekilde
# içeriyor (yüksek yanlış-çekicilik) — ama role='mulakatci'. Adayın satırı daha az belirgin
# kelime örtüşmesine sahip ama role='aday' — GERÇEK kanıt bu olmalı.
TRANSCRIPT_VIEW = [
    {"role": "mulakatci", "text": "Örnek yetkinlik alanında deneyiminizi anlatır mısınız?", "elapsed_ms": 5000, "ts": "0:05"},
    {"role": "aday", "text": "Geçen yıl bu alanı destekleyen bir projede görev aldım.", "elapsed_ms": 9000, "ts": "0:09"},
]

FIELDS = {"g": "Gösterdi", "k": "[99:99] geçersiz damga", "e": "", "s": ""}


# ============================================================
# 1) evidence_timestamp_invalid hint'inde YALNIZ role='aday' satırları olmalı
# ============================================================
lines = m._build_violation_detail_lines(["evidence_timestamp_invalid"], CRITERION_NAME, FIELDS, TRANSCRIPT_VIEW, [])
hint_line = next((l for l in lines if l.startswith("- evidence_timestamp_invalid")), "")
check("1) evidence_timestamp_invalid satırı üretildi", bool(hint_line))
check("1) hint metninde 'Mülakatçı:' etiketi YOK (yalnız aday satırları)", "Mülakatçı:" not in hint_line)
check("1) hint metninde 'Aday:' etiketi VAR", "Aday:" in hint_line)

# ============================================================
# 2) Lexical olarak ÇOK uygun mülakatçı satırı olsa bile hint'e GİRMEMELİ
# ============================================================
check("2) mülakatçının (lexical olarak güçlü eşleşen) cümlesi hint'te YOK",
      "Örnek yetkinlik alanında deneyiminizi anlatır mısınız?" not in hint_line)

# ============================================================
# 3) Uygun aday satırı hint'e GİRMELİ
# ============================================================
check("3) adayın gerçek satırı hint'te VAR", "Geçen yıl bu alanı destekleyen bir projede görev aldım." in hint_line)

# ============================================================
# 4) Validator'ın mevcut role='aday' davranışı DEĞİŞMEDİ (doğrudan doğrulama)
# ============================================================
check("4) _timestamp_field_grounded hâlâ yalnız role='aday' satırını kabul ediyor",
      m._timestamp_field_grounded("[0:09] geçen yıl bir projede görev aldığını söyledi", TRANSCRIPT_VIEW, role="aday") is True)
check("4) tolerans dışı (uzak) bir damga role='aday' için REDDEDİLİYOR (davranış değişmedi)",
      m._timestamp_field_grounded("[0:50] bir şey söyledi", TRANSCRIPT_VIEW, role="aday") is False)

# ============================================================
# 5) Diğer violation hint davranışları DEĞİŞMEDİ (unsourced_eksik hâlâ role='mulakatci')
# ============================================================
fields_eksik = {"g": "Gösterdi", "k": "[0:09] geçen yıl bir projede görev aldı", "e": "Bir eksik var", "s": "[88:88]"}
lines2 = m._build_violation_detail_lines(["unsourced_eksik"], CRITERION_NAME, fields_eksik, TRANSCRIPT_VIEW, [])
hint_line2 = next((l for l in lines2 if l.startswith("- unsourced_eksik")), "")
check("5) unsourced_eksik hint'inde mülakatçı satırı VAR (role='mulakatci' — DEĞİŞMEDİ)",
      "Örnek yetkinlik alanında deneyiminizi anlatır mısınız?" in hint_line2)
check("5) unsourced_eksik hint'inde aday satırı YOK (role filtresi DEĞİŞMEDİ)",
      "Bu konuda geçen yıl bir projede görev aldım." not in hint_line2)

# Diğer violation'ların (banned_phrase_found, duplicate_claim, forbidden_transition_found)
# mesaj biçimi de değişmemiş olmalı — hint/role kavramı bunlarda zaten yok, yalnız metin sabit.
lines3 = m._build_violation_detail_lines(["forbidden_transition_found"], CRITERION_NAME, FIELDS, TRANSCRIPT_VIEW, [])
check("5b) forbidden_transition_found mesajı DEĞİŞMEDİ", any("ancak/fakat/ne var ki/bununla birlikte" in l for l in lines3))


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("Tüm İŞ 6K testleri GEÇTİ.")

if FAILURES:
    sys.exit(1)

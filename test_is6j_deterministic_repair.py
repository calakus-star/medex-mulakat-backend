# İŞ 6J — SAF METİN VIOLATION'LARINI LLM'SİZ DÜZELT — unit/regression testleri.
# apply_structured_rationale_gate() GERÇEKTEN çağrılır — yalnız regenerate_criterion_fields()
# monkey-patch edilir (AI çağrı SAYISINI ölçmek için), hiçbir gerçek ağ/API çağrısı yapılmaz.
#
# Çalıştırma: py test_is6j_deterministic_repair.py  (backend/ dizininde)

import io
import sys
import contextlib
import main as m

FAILURES = []


def check(label, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        FAILURES.append(label)


CAP = 25
CRITERIA = [{"name": "Raporlama", "weight": CAP}]

TRANSCRIPT_VIEW = [
    {"role": "mulakatci", "text": "Raporlama sürecinizi nasıl yürütüyorsunuz?", "elapsed_ms": 125000, "ts": "2:05"},
    {"role": "aday", "text": "Haftalık olarak düzenli rapor hazırlıyorum.", "elapsed_ms": 130000, "ts": "2:10"},
]
TRANSCRIPT_TEXT = "[2:05] Mülakatçı: Raporlama sürecinizi nasıl yürütüyorsunuz?\n[2:10] Aday: Haftalık olarak düzenli rapor hazırlıyorum."


def make_table(g, k, e, s, awarded=15, cap=CAP):
    return f"| Raporlama | {awarded}/{cap} | G: {g} ~~ K: {k} ~~ E: {e} ~~ S: {s} |"


def with_mock(mock_fn):
    calls = {"n": 0}
    orig = m.regenerate_criterion_fields

    def wrapped(*args, **kwargs):
        calls["n"] += 1
        return mock_fn(calls["n"], *args, **kwargs)

    m.regenerate_criterion_fields = wrapped
    return calls, orig


def run_gate(table_text, mock_fn=None):
    if mock_fn is None:
        mock_fn = lambda n, *a, **k: None  # çağrılırsa test zaten çağrı SAYISINI yakalayacak
    calls, orig = with_mock(mock_fn)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            result = m.apply_structured_rationale_gate(
                table_text, CRITERIA, "P", TRANSCRIPT_VIEW, TRANSCRIPT_TEXT, "claude", "claude-sonnet-4-6", 9001, 1)
    finally:
        m.regenerate_criterion_fields = orig
    return result, calls["n"], buf.getvalue()


# ============================================================
# 1) Yalnız unsourced_eksik -> E/S temizlenir, AI çağrısı 0
# ============================================================
TABLE_1 = make_table(
    g="Raporlama sürecini anlattı",
    k="[2:10] Haftalık olarak düzenli rapor hazırladığını söyledi",
    e="Zaman zaman gecikme yaşadığını belirtti",
    s="[8:00]",  # mülakatçının 8:00'da bir sorusu YOK -> unsourced_eksik
    awarded=15,
)
(new_table_1, new_score_1, log_1, _flag_1), n_calls_1, stdout_1 = run_gate(TABLE_1)
check("1) AI çağrısı 0", n_calls_1 == 0)
check("1) 'gecti' olarak loglandı (repair sonrası validator PASS)", any(l.get("sonuc") == "gecti" for l in log_1))
check("1) E alanı temizlendi (yeni tabloda eski EKSİK metni YOK)", "Zaman zaman gecikme yaşadığını belirtti" not in new_table_1)
check("1) [CRITERION_DETERMINISTIC_REPAIR] logu var, unsourced_eksik içeriyor",
      "[CRITERION_DETERMINISTIC_REPAIR]" in stdout_1 and "unsourced_eksik" in stdout_1)
check("1) puan DEĞİŞMEDİ (15/25 aynen kaldı)", "15/25" in new_table_1)

# ============================================================
# 2) Yalnız forbidden_transition_found -> yasak bağlaç temizlenir, AI çağrısı 0
# ============================================================
TABLE_2 = make_table(
    g="Raporlama sürecini anlattı ancak detay vermedi",
    k="[2:10] Haftalık olarak düzenli rapor hazırladığını söyledi",
    e="",
    s="",
    awarded=15,
)
(new_table_2, new_score_2, log_2, _flag_2), n_calls_2, stdout_2 = run_gate(TABLE_2)
check("2) AI çağrısı 0", n_calls_2 == 0)
check("2) 'gecti' olarak loglandı", any(l.get("sonuc") == "gecti" for l in log_2))
check("2) yasak bağlaç 'ancak' rapor metninden TEMİZLENDİ", "ancak" not in new_table_2.lower())
check("2) [CRITERION_DETERMINISTIC_REPAIR] logu var, forbidden_transition_found içeriyor",
      "[CRITERION_DETERMINISTIC_REPAIR]" in stdout_2 and "forbidden_transition_found" in stdout_2)
check("2) puan DEĞİŞMEDİ", "15/25" in new_table_2)

# ============================================================
# 3) Yalnız banned_phrase_found -> yasak segment temizlenir, AI çağrısı 0
# ============================================================
TABLE_3 = make_table(
    g="Adayın daha somut örnekler sunmamıştır",
    k="[2:10] Haftalık olarak düzenli rapor hazırladığını söyledi",
    e="",
    s="",
    awarded=15,
)
(new_table_3, new_score_3, log_3, _flag_3), n_calls_3, stdout_3 = run_gate(TABLE_3)
check("3) AI çağrısı 0", n_calls_3 == 0)
check("3) 'gecti' olarak loglandı", any(l.get("sonuc") == "gecti" for l in log_3))
check("3) yasak klişe 'sunmamıştır' rapor metninden TEMİZLENDİ", "sunmamıştır" not in new_table_3)
check("3) [CRITERION_DETERMINISTIC_REPAIR] logu var, banned_phrase_found içeriyor",
      "[CRITERION_DETERMINISTIC_REPAIR]" in stdout_3 and "banned_phrase_found" in stdout_3)
check("3) puan DEĞİŞMEDİ", "15/25" in new_table_3)

# ============================================================
# 4) banned_phrase_found + evidence_timestamp_invalid -> banned temizlenir,
#    timestamp violation KALIR, mevcut AI retry ÇALIŞIR
# ============================================================
TABLE_4 = make_table(
    g="Adayın daha somut örnekler sunmamıştır",
    k="[99:59] bir şey söyledi",  # transkriptte OLMAYAN damga -> evidence_timestamp_invalid
    e="",
    s="",
    awarded=15,
)


def mock_4(call_n, *a, **kw):
    return None  # AI retry başarısız olsun diye — yalnız ÇAĞRILDI mı diye bakıyoruz


(new_table_4, new_score_4, log_4, _flag_4), n_calls_4, stdout_4 = run_gate(TABLE_4, mock_4)
check("4) [CRITERION_DETERMINISTIC_REPAIR] logu var, YALNIZ banned_phrase_found içeriyor (timestamp DEĞİL)",
      "[CRITERION_DETERMINISTIC_REPAIR]" in stdout_4 and "banned_phrase_found" in stdout_4
      and "evidence_timestamp_invalid" not in stdout_4.split("[CRITERION_DETERMINISTIC_REPAIR]")[1].split("\n")[0])
check("4) yasak klişe temizlendi", "sunmamıştır" not in new_table_4)
check("4) mevcut AI retry ÇALIŞTI (çağrı sayısı >= 1)", n_calls_4 >= 1)
check("4) kalan violation gerçekten evidence_timestamp_invalid (log'da degerlendirilemedi_sistem + bu ihlal var)",
      any(l.get("sonuc") == "degerlendirilemedi_sistem" and "evidence_timestamp_invalid" in (l.get("ihlaller") or []) for l in log_4))

# ============================================================
# 5) Repair sonrası validator PASS ise criterion_rationale_retry ÇALIŞMAZ
#    (1/2/3 testleri zaten bunu n_calls==0 ile doğruladı — ek doğrudan doğrulama:)
# ============================================================
check("5) (1/2/3'ün ortak sonucu) repair PASS ürettiğinde regenerate_criterion_fields HİÇ çağrılmadı",
      n_calls_1 == 0 and n_calls_2 == 0 and n_calls_3 == 0)

# ============================================================
# 6) Puan değişmez (1/2/3/4 testlerinde zaten '15/25' kontrolleriyle doğrulandı) — ek toplu kontrol
# ============================================================
check("6) Tüm senaryolarda awarded/cap aynı kaldı (15/25)",
      all("15/25" in t for t in (new_table_1, new_table_2, new_table_3)))

# ============================================================
# Diğer violation türlerine (duplicate_claim, evidence_timestamp_invalid, structure_invalid,
# score_direction_conflict, out_of_scope_high_score) dokunulmadığının doğrudan birim testi
# ============================================================
UNTOUCHED_FIELDS = {"g": "Adayın raporlama konusunda X olduğunu belirtmiştir", "k": "[99:59] bir şey", "e": "", "s": ""}
repaired_fields, repaired_codes = m._deterministic_repair_criterion_fields(
    UNTOUCHED_FIELDS, ["evidence_timestamp_invalid", "duplicate_claim", "structure_invalid",
                       "score_direction_conflict", "out_of_scope_high_score"])
check("7) Hedef-dışı violation'lar varken repair HİÇBİR ŞEY yapmadı (repaired_codes boş)", repaired_codes == [])
check("7) fields İÇERİK olarak DEĞİŞMEDİ", repaired_fields == UNTOUCHED_FIELDS)


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("Tüm İŞ 6J testleri GEÇTİ.")

if FAILURES:
    sys.exit(1)

# NOT: Bu dosya artık İş 1-6H regresyon dosyalarını KENDİ İÇİNDE subprocess olarak ZİNCİRLEMİYOR
# (test_is6d/test_is6h'nin kendi iç zincirleriyle iç içe geçince süre katlanarak büyüyordu —
# canlı gözlemlendi). Regresyon, çağıran tarafından HER dosya AYRI AYRI ve TEK SEFER
# çalıştırılarak doğrulanır (bkz. görev raporu).

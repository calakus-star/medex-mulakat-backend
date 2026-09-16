# İŞ 3 — SCOPE CLAMP BAĞLAM EŞLEŞTİRMESİ — unit/regression testleri.
# Tamamen SENTETİK, genel kriter adlarıyla (Murat'a/başka bir adaya/pozisyona özel HİÇBİR
# kelime/kriter hardcode edilmedi). DB/ağ/LLM çağrısı yapmaz.
#
# Çalıştırma: py test_is3_scope_context.py  (backend/ dizininde)

import sys
import main as m

FAILURES = []


def check(label, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        FAILURES.append(label)


CAP = 25
CAPPED = int(CAP * 0.25)  # 6


def row(role, text, elapsed_ms=None, ts=""):
    return {"role": role, "text": text, "elapsed_ms": elapsed_ms, "ts": ts}


def make_table(criteria_names, awarded=20, cap=CAP):
    return "\n".join(f"| {n} | {awarded}/{cap} | Gösterdi bir kanıt. |" for n in criteria_names)


DECLARATION_TEXT = "Bu benim görevim değil."  # jenerik alan-dışı beyanı (find_scope_declarations regex'iyle eşleşir)

# ============================================================
# A) Tek declaration + AÇIKÇA eşleşen soru -> yalnız ilgili kriter clamp
# ============================================================
CRITERIA_A = [{"name": "Raporlama", "weight": 25}, {"name": "Planlama", "weight": 20}]
transcript_a = [
    row("mulakatci", "Raporlama sürecinizi anlatır mısınız?", elapsed_ms=10000),
    row("aday", DECLARATION_TEXT, elapsed_ms=20000),
]
table_a = make_table(["Raporlama", "Planlama"])
new_table_a, new_score_a, log_a = m.apply_scope_clamp_transcript_wide(table_a, CRITERIA_A, transcript_a, "P", 9001, 1)
check("A) Raporlama kelepçelendi (yeni satırda CAPPED/CAP var)", f"| Raporlama | {CAPPED}/{CAP} |" in new_table_a)
check("A) Planlama DOKUNULMADI (satır aynı kaldı)", f"| Planlama | 20/{CAP} |" in new_table_a)
check("A) log'da yalnız 'Raporlama' için transkript_geneli_kelepce var",
      any(l.get("sonuc") == "transkript_geneli_kelepce" and l.get("kriter") == "Raporlama" for l in log_a))
check("A) 'Planlama' için hiç kelepçe kaydı yok",
      not any(l.get("sonuc") == "transkript_geneli_kelepce" and l.get("kriter") == "Planlama" for l in log_a))

# ============================================================
# B) Tek declaration + eşleşmeyen/belirsiz soru -> HİÇBİR kriter clamp edilmez, unresolved loglanır
# ============================================================
CRITERIA_B = [{"name": "Raporlama", "weight": 25}]
transcript_b = [
    row("mulakatci", "Peki başka eklemek istediğiniz bir şey var mı?", elapsed_ms=10000),
    row("aday", DECLARATION_TEXT, elapsed_ms=20000),
]
table_b = make_table(["Raporlama"])
new_table_b, new_score_b, log_b = m.apply_scope_clamp_transcript_wide(table_b, CRITERIA_B, transcript_b, "P", 9002, 1)
check("B) tablo DEĞİŞMEDİ (hiçbir kriter clamp edilmedi)", new_table_b == table_b)
check("B) new_score None (skor değişmedi)", new_score_b is None)
check("B) log'da 'scope_declaration_unresolved' var", any(l.get("sonuc") == "scope_declaration_unresolved" for l in log_b))
check("B) log'da 'transkript_geneli_kelepce' YOK", not any(l.get("sonuc") == "transkript_geneli_kelepce" for l in log_b))

# ============================================================
# C) Birden fazla AYNI ağırlıklı 'core' kriter + belirsiz declaration -> artık HİÇBİRİNE broadcast yok
# ============================================================
CRITERIA_C = [{"name": "Raporlama", "weight": 25}, {"name": "Iletisim", "weight": 25}, {"name": "Planlama", "weight": 20}]
transcript_c = [
    row("mulakatci", "Devam edelim mi?", elapsed_ms=10000),  # kriter kelimesi içermeyen jenerik soru
    row("aday", DECLARATION_TEXT, elapsed_ms=20000),
]
table_c = make_table(["Raporlama", "Iletisim", "Planlama"])
new_table_c, new_score_c, log_c = m.apply_scope_clamp_transcript_wide(table_c, CRITERIA_C, transcript_c, "P", 9003, 1)
check("C) 'Raporlama' clamp EDİLMEDİ (eski davranışta broadcast alırdı)", f"| Raporlama | 20/{CAP} |" in new_table_c)
check("C) 'Iletisim' clamp EDİLMEDİ (eski davranışta broadcast alırdı)", f"| Iletisim | 20/{CAP} |" in new_table_c)
check("C) unresolved olarak loglandı", any(l.get("sonuc") == "scope_declaration_unresolved" for l in log_c))

# ============================================================
# D) Takip sorusu: asıl soru + "Peki bu konuda?" + declaration -> yakın pencere asıl konuyu korumalı
# ============================================================
CRITERIA_D = [{"name": "Planlama", "weight": 25}, {"name": "Iletisim", "weight": 20}]
transcript_d = [
    row("mulakatci", "Planlama sürecinizde nasıl önceliklendirme yapıyorsunuz?", elapsed_ms=5000),
    row("aday", "Genelde haftalık liste tutuyorum.", elapsed_ms=8000),
    row("mulakatci", "Peki bu konuda?", elapsed_ms=12000),  # içerik-fakir takip sorusu — TEK BAŞINA eşleşmez
    row("aday", DECLARATION_TEXT, elapsed_ms=15000),
]
table_d = make_table(["Planlama", "Iletisim"])
# Önce: sadece son satırla (_preceding_question benzeri) eşleşme başarısız olurdu -> doğrulama:
last_only_match = m._match_criterion_for_topic("Peki bu konuda?", CRITERIA_D)
check("D) (referans) yalnız son satırla eşleşme BAŞARISIZ olurdu (eski zaaf doğrulandı)", last_only_match is None)
new_table_d, new_score_d, log_d = m.apply_scope_clamp_transcript_wide(table_d, CRITERIA_D, transcript_d, "P", 9004, 1)
check("D) yakın pencere ile 'Planlama' DOĞRU eşleşip clamp edildi", f"| Planlama | {CAPPED}/{CAP} |" in new_table_d)
check("D) 'Iletisim' dokunulmadı (tablo satırı ORİJİNAL haliyle kaldı)", f"| Iletisim | 20/{CAP} |" in new_table_d)

# ============================================================
# E) Araya baslik/system satırı girerse bağlam seçimini BOZMAMALI
# ============================================================
CRITERIA_E = [{"name": "Raporlama", "weight": 25}]
transcript_e = [
    row("mulakatci", "Raporlama sürecinizi anlatır mısınız?", elapsed_ms=5000),
    {"role": "baslik", "text": "--- BÖLÜM BAŞLIĞI ---", "elapsed_ms": None, "ts": ""},
    row("aday", DECLARATION_TEXT, elapsed_ms=15000),
]
table_e = make_table(["Raporlama"])
new_table_e, new_score_e, log_e = m.apply_scope_clamp_transcript_wide(table_e, CRITERIA_E, transcript_e, "P", 9005, 1)
check("E) baslik satırı araya girse bile 'Raporlama' doğru eşleşip clamp edildi",
      f"| Raporlama | {CAPPED}/{CAP} |" in new_table_e)

# ============================================================
# F) elapsed_ms None olsa bile index-bazlı bağlam çalışmalı
# ============================================================
CRITERIA_F = [{"name": "Raporlama", "weight": 25}]
transcript_f = [
    row("mulakatci", "Raporlama sürecinizi anlatır mısınız?", elapsed_ms=None, ts=""),
    row("aday", DECLARATION_TEXT, elapsed_ms=None, ts=""),
]
table_f = make_table(["Raporlama"])
new_table_f, new_score_f, log_f = m.apply_scope_clamp_transcript_wide(table_f, CRITERIA_F, transcript_f, "P", 9006, 1)
check("F) elapsed_ms hepsi None olsa bile 'Raporlama' doğru eşleşip clamp edildi (index-bazlı)",
      f"| Raporlama | {CAPPED}/{CAP} |" in new_table_f)
# Referans: eski _preceding_question elapsed_ms=None ile HİÇ çalışmazdı
check("F) (referans) _preceding_question elapsed_ms=None ile None döner (eski zaaf doğrulandı)",
      m._preceding_question(None, transcript_f) is None)

# ============================================================
# G) L1 tam-turn transcript senaryosu (her mesaj = tam bir tur, ISO/elapsed_ms düzenli)
# ============================================================
CRITERIA_G = [{"name": "Planlama", "weight": 25}, {"name": "Raporlama", "weight": 20}]
transcript_g = [
    row("mulakatci", "Bir önceki pozisyonunuzda planlama sürecinizi nasıl yönetiyordunuz?", elapsed_ms=3000),
    row("aday", "Excel ile takip ediyordum.", elapsed_ms=9000),
    row("mulakatci", "Peki bu konuda daha fazla detay verir misiniz?", elapsed_ms=14000),
    row("aday", DECLARATION_TEXT, elapsed_ms=20000),
]
table_g = make_table(["Planlama", "Raporlama"])
new_table_g, new_score_g, log_g = m.apply_scope_clamp_transcript_wide(table_g, CRITERIA_G, transcript_g, "P", 9007, 1)
check("G) L1 tam-turn senaryosunda 'Planlama' doğru eşleşip clamp edildi", f"| Planlama | {CAPPED}/{CAP} |" in new_table_g)
check("G) 'Raporlama' dokunulmadı (tablo satırı ORİJİNAL haliyle kaldı)", f"| Raporlama | 20/{CAP} |" in new_table_g)

# ============================================================
# H) Realtime PARÇALI transcript benzeri senaryo (mülakatçının tek sorusu 2 ayrı satıra bölünmüş)
# ============================================================
CRITERIA_H = [{"name": "Iletisim", "weight": 25}, {"name": "Planlama", "weight": 20}]
transcript_h = [
    row("mulakatci", "Iletisim tarzınızı", elapsed_ms=5000),        # VAD-bölünmüş ilk parça (kriter adıyla AYNI yazım, _norm_name'e dokunmuyoruz)
    row("mulakatci", "biraz anlatır mısınız?", elapsed_ms=6200),    # devamı, ayrı satır
    row("aday", DECLARATION_TEXT, elapsed_ms=12000),
]
table_h = make_table(["Iletisim", "Planlama"])
new_table_h, new_score_h, log_h = m.apply_scope_clamp_transcript_wide(table_h, CRITERIA_H, transcript_h, "P", 9008, 1)
check("H) parçalı mülakatçı satırları birleştiğinde 'Iletisim' doğru eşleşip clamp edildi",
      f"| Iletisim | {CAPPED}/{CAP} |" in new_table_h)
check("H) 'Planlama' dokunulmadı (tablo satırı ORİJİNAL haliyle kaldı)", f"| Planlama | 20/{CAP} |" in new_table_h)

# ============================================================
# find_scope_declarations — index alanı + [:200] kırpmasının kaldırılması
# ============================================================
long_text = "Bu benim görevim değil, çünkü " + ("x" * 250)
transcript_idx = [row("aday", long_text, elapsed_ms=1000)]
decls = m.find_scope_declarations(transcript_idx)
check("find_scope_declarations: 'index' alanı var ve doğru (0)", decls and decls[0].get("index") == 0)
check("find_scope_declarations: text ARTIK 200 karakterde KIRPILMIYOR", decls and len(decls[0]["text"]) > 200)
check("find_scope_declarations: text tam metinle eşleşiyor", decls and decls[0]["text"] == long_text)


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("Tüm İŞ 3 testleri GEÇTİ.")

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

if FAILURES or not is1_ok or not is2_ok:
    sys.exit(1)

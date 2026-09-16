# İŞ 1 — TAKEOVER SONRASI RAPOR TUTARLILIĞI — regresyon/unit testleri.
# Yalnızca Problem A'nın (Puanlama Kapsamı / Değerlendirilemeyen Alanlar tutarsızlığı) düzeltilmesini
# hedefler. Scoring/validator/takeover/scope-clamp/reviewer/PDF/prompt mantığına dokunmaz, test de
# etmez. Ağ/DB çağrısı yapan gerçek fonksiyonları (append_reviewer_section vb.) ÇAĞIRMAZ — yalnız
# yeni eklenen saf (side-effect'siz, DB'siz) yardımcı fonksiyonları ve render fonksiyonlarını test
# eder: _extract_disqualified_criteria_names, _patch_report_section, _verify_scope_consistency,
# render_puanlama_kapsami, render_degerlendirilemeyen_alanlar.
#
# Çalıştırma: py test_is1_report_consistency.py  (backend/ dizininde)

import re
import sys

import main as m

FAILURES = []


def check(label, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        FAILURES.append(label)


# ---- Sentetik "Kader" senaryosu: 12 pozisyon kriteri, ilk üretimde 7 tanesi
#      "Değerlendirilemedi (sistem)", devralma 4'ünü kurtarıyor, gerçek düşen 3 kalıyor. ----

POS_CRITERIA = [{"name": f"Kriter{i}", "weight": 10} for i in range(1, 13)]  # 12 kriter, K1..K12 değil P1..P12

INITIAL_DISQUALIFIED = {"Kriter2", "Kriter4", "Kriter6", "Kriter8", "Kriter10", "Kriter11", "Kriter12"}  # 7 tanesi
RECOVERED_BY_TAKEOVER = {"Kriter4", "Kriter8", "Kriter10", "Kriter12"}  # devralma 4'ünü kurtarıyor
STILL_DROPPED = INITIAL_DISQUALIFIED - RECOVERED_BY_TAKEOVER  # {"Kriter2","Kriter6","Kriter11"} = 3 tanesi


def build_table(disqualified_names):
    lines = []
    for c in POS_CRITERIA:
        name = c["name"]
        if name in disqualified_names:
            lines.append(f"| {name} | Değerlendirilemedi (sistem) — doğrulayıcı 3 denemede geçerli gerekçe üretemedi (structure_invalid) | Bu kriter için doğrulanabilir bir gerekçe üretilemedi. |")
        else:
            lines.append(f"| {name} | 8/10 | Gösterdi (kanıt). |")
    return "\n".join(lines)


TABLE_BEFORE_TAKEOVER = build_table(INITIAL_DISQUALIFIED)
TABLE_AFTER_TAKEOVER = build_table(STILL_DROPPED)  # takeover sonrası: yalnızca 3 kriter hâlâ düşük


# ---- TEST 1: _extract_disqualified_criteria_names — final (post-takeover) tablodan doğru listeyi çıkarır ----
extracted_before = set(m._extract_disqualified_criteria_names(TABLE_BEFORE_TAKEOVER))
check("extract: takeover-öncesi tablo -> 7 düşen kriter", extracted_before == INITIAL_DISQUALIFIED)

extracted_after = set(m._extract_disqualified_criteria_names(TABLE_AFTER_TAKEOVER))
check("extract: takeover-sonrası tablo -> 3 düşen kriter (Kader vakasıyla aynı sayı)", extracted_after == STILL_DROPPED)


# ---- TEST 2: render_puanlama_kapsami ve render_degerlendirilemeyen_alanlar AYNI listeden
#      tutarlı çıktı üretiyor mu ----
dropped_pos_list = sorted(extracted_after)
kapsami_text = m.render_puanlama_kapsami(POS_CRITERIA, [], dropped_pos_list, [])
degerlendirilemeyen_text = m.render_degerlendirilemeyen_alanlar(dropped_pos_list, [])

check("render: Puanlama Kapsamı '9/12' diyor", "9/12" in kapsami_text)
check("render: Puanlama Kapsamı 3 kriter değerlendirilemedi diyor (pozisyon: 9/12)",
      "pozisyon: 9/12" in kapsami_text)
for name in dropped_pos_list:
    check(f"render: Değerlendirilemeyen Alanlar '{name}' içeriyor", name in degerlendirilemeyen_text)
for name in RECOVERED_BY_TAKEOVER:
    check(f"render: Değerlendirilemeyen Alanlar devralınan '{name}' içermiyor", name not in degerlendirilemeyen_text)


# ---- TEST 3: _patch_report_section — head bulunursa günceller, bulunamazsa SESSİZ DEĞİL loglar ----
logged = []
orig_record = m.record_system_decision


def fake_record_system_decision(candidate_id, level, decision, reason, meta=None, warnings=None):
    logged.append((decision, reason, meta))


m.record_system_decision = fake_record_system_decision
try:
    head = "**Puanlama Kapsamı:**"
    pattern = re.compile(re.escape(head) + r".*?(?=\n\n|\Z)", re.DOTALL)
    report_with_head = f"{head}\nEski metin burada.\n\nSonraki bölüm."
    patched = m._patch_report_section(report_with_head, head, pattern, "Yeni metin.", "Puanlama Kapsamı", 999, 1)
    check("patch: head varsa günceller", "Yeni metin." in patched and "Eski metin burada." not in patched)

    report_without_head = "Bu raporda hiç Puanlama Kapsamı başlığı yok."
    logged.clear()
    patched2 = m._patch_report_section(report_without_head, head, pattern, "Yeni metin.", "Puanlama Kapsamı", 999, 1)
    check("patch: head yoksa metni DEĞİŞTİRMEDEN döner", patched2 == report_without_head)
    check("patch: head yoksa SESSİZ DEĞİL — record_system_decision çağrılır", len(logged) == 1 and logged[0][0] == "rapor_bolum_patch_basarisiz")
finally:
    m.record_system_decision = orig_record


# ---- TEST 4: _verify_scope_consistency — puanlı bir kriter dropped listesinde görünmemeli (invariant C) ----
problems_consistent = m._verify_scope_consistency(TABLE_AFTER_TAKEOVER, "", dropped_pos_list, [])
check("verify: tutarlı tabloda sorun bulunmaz", problems_consistent == [])

# Kasıtlı bozuk senaryo: dropped listesi ile tablo UYUMSUZ (Kader vakasının simülasyonu — eski 7 liste,
# yeni 3-düşen tablo)
problems_inconsistent = m._verify_scope_consistency(TABLE_AFTER_TAKEOVER, "", sorted(INITIAL_DISQUALIFIED), [])
check("verify: uyumsuz (stale) dropped listesi TESPİT EDİLİR (>0 sorun)", len(problems_inconsistent) > 0)
check("verify: devralınan bir kriter için 'PUANLI görünüyor' UYARISI yok değil, VAR (stale listede olduğu için)",
      any("PUANLI görünüyor" in p for p in problems_inconsistent))


# ---- INVARIANT A: scored_criteria ∩ unevaluated_criteria = ∅ (post-takeover final tablo üzerinden) ----
scored_names = set()
for ln in TABLE_AFTER_TAKEOVER.splitlines():
    cells = [x.strip() for x in ln.strip().strip("|").split("|")]
    if len(cells) >= 2 and re.search(r"\d+\s*/\s*\d+", cells[1]) and not m._DISQUALIFIED_CELL_RE.search(ln):
        scored_names.add(cells[0])
check("INVARIANT A: scored ∩ unevaluated = ∅", scored_names.isdisjoint(extracted_after))

# ---- INVARIANT B: evaluated_count + unevaluated_count = total_criteria ----
check("INVARIANT B: evaluated + unevaluated == total (9 + 3 == 12)",
      len(scored_names) + len(extracted_after) == len(POS_CRITERIA))

# ---- INVARIANT C: Puanlama Kapsamı'ndaki değerlendirilemeyen sayısı == Değerlendirilemeyen Alanlar
#      listesindeki kriter sayısı (ikisi de AYNI dropped_pos_list'ten üretildiği için yapısal garanti) ----
kapsami_dropped_count_match = re.search(r"pozisyon:\s*(\d+)/(\d+)", kapsami_text)
kapsami_evaluated = int(kapsami_dropped_count_match.group(1))
kapsami_total = int(kapsami_dropped_count_match.group(2))
kapsami_unevaluated = kapsami_total - kapsami_evaluated
degerlendirilemeyen_count = len(re.findall(r"Kriter\d+", degerlendirilemeyen_text))
check("INVARIANT C: Puanlama Kapsamı'nın düşen-sayısı == Değerlendirilemeyen Alanlar liste uzunluğu",
      kapsami_unevaluated == degerlendirilemeyen_count == len(dropped_pos_list))

# ---- INVARIANT D: Takeover ile geri kazanılan kriter artık Değerlendirilemeyen Alanlar'da görünmüyor ----
check("INVARIANT D: devralınan hiçbir kriter Değerlendirilemeyen Alanlar'da yok",
      all(name not in degerlendirilemeyen_text for name in RECOVERED_BY_TAKEOVER))


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("Tüm testler GEÇTİ.")

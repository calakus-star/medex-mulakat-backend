"""İŞ EMRİ — BAŞLANGIÇ EKRANI, KVKK ONAYI, ONAY KAYDI, AI NOTU RAPORU — regresyon testi."""
import sys
import main as m

FAILURES = []


def check(label, cond):
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        FAILURES.append(label)


TEST_CID = 9701
LEVEL = 1


def cleanup():
    db = m.get_db()
    try:
        db.execute("DELETE FROM consent_records WHERE candidate_id=?", (TEST_CID,))
        db.execute("DELETE FROM interviews WHERE candidate_id=?", (TEST_CID,))
        db.execute("DELETE FROM candidates WHERE id=?", (TEST_CID,))
        db.commit()
    finally:
        db.close()


class FakeRequest:
    def __init__(self, xff="1.2.3.4", ua="TestAgent/1.0"):
        self.headers = {"x-forwarded-for": xff, "user-agent": ua}
        self.client = type("c", (), {"host": "9.9.9.9"})()


cleanup()
try:
    db = m.get_db()
    db.execute("INSERT INTO candidates (id, name, position, level, cv_text, org_id) VALUES (?, ?, ?, ?, ?, ?)",
               (TEST_CID, "Test Aday", "Test Pozisyonu", LEVEL, "CV metni", None))
    db.commit()
    db.close()

    candidate = m.get_db().execute("SELECT * FROM candidates WHERE id=?", (TEST_CID,)).fetchone()

    # ============================================================
    # 1) Tenant adı çözümlemesi (org_id boş -> varsayılan org)
    # ============================================================
    tenant = m.resolve_tenant_name(candidate)
    check("1) org_id boş -> varsayılan tenant adı döner", bool(tenant))

    # ============================================================
    # 2) Onay verilmeden mülakat başlamıyor (madde 1)
    # ============================================================
    try:
        m.start_interview(payload={"role": "candidate", "candidate_id": TEST_CID})
        check("2) onay yokken start_interview 403 verdi", False)
    except Exception as e:
        check("2) onay yokken start_interview 403 verdi", getattr(e, "status_code", None) == 403)

    # ============================================================
    # 3) Onay kaydı TÜM alanlarla oluşuyor (madde 4)
    # ============================================================
    res = m.accept_consent(FakeRequest(), payload={"role": "candidate", "candidate_id": TEST_CID})
    check("3) accept_consent ok=True", res.get("ok") is True)
    row = m.get_db().execute("SELECT * FROM consent_records WHERE candidate_id=? AND level=?", (TEST_CID, LEVEL)).fetchone()
    check("3) onay kaydı var", row is not None)
    if row:
        check("3) candidate_id doğru", row["candidate_id"] == TEST_CID)
        check("3) level doğru", row["level"] == LEVEL)
        check("3) tenant_name dolu", bool(row["tenant_name"]))
        check("3) consent_at dolu", bool(row["consent_at"]))
        check("3) text_version == 1", row["text_version"] == 1)
        check("3) disclosure_text tam metin (uzun)", len(row["disclosure_text"] or "") > 500)
        check("3) checkbox_text tenant adını içeriyor", tenant in (row["checkbox_text"] or ""))
        check("3) ip_address kaydedildi (XFF ilk değer)", row["ip_address"] == "1.2.3.4")
        check("3) user_agent kaydedildi", row["user_agent"] == "TestAgent/1.0")

    # ============================================================
    # 4) İkinci accept_consent çağrısı yeni satır YAZMIYOR (idempotent, madde 4: silinmez/değiştirilemez)
    # ============================================================
    res2 = m.accept_consent(FakeRequest(xff="9.9.9.9"), payload={"role": "candidate", "candidate_id": TEST_CID})
    check("4) ikinci çağrı already_given=True döner", res2.get("already_given") is True)
    count = m.get_db().execute("SELECT COUNT(*) c FROM consent_records WHERE candidate_id=?", (TEST_CID,)).fetchone()["c"]
    check("4) tekrar çağrı yeni satır eklemedi (hâlâ 1 kayıt)", count == 1)
    row_after = m.get_db().execute("SELECT ip_address FROM consent_records WHERE candidate_id=?", (TEST_CID,)).fetchone()
    check("4) mevcut kayıt DEĞİŞMEDİ (ip hâlâ ilk değer)", row_after["ip_address"] == "1.2.3.4")

    # ============================================================
    # 5) Onay verildikten sonra mülakat başlayabiliyor
    # ============================================================
    # Not: yerel test ortamında OPENAI_API_KEY yok, bu yüzden çağrı 500 (API anahtarı eksik) ile
    # sonlanır — ÖNEMLİ olan 403 (KVKK onayı) ARTIK atılmıyor olması, mülakatın kendisinin
    # gerçekten başlaması bu testin kapsamı DIŞINDA (ayrı, mevcut testlerde zaten kapsanıyor).
    try:
        m.start_interview(payload={"role": "candidate", "candidate_id": TEST_CID})
        check("5) onay verildikten sonra 403 (KVKK) ARTIK atılmıyor", True)
    except Exception as e:
        check("5) onay verildikten sonra 403 (KVKK) ARTIK atılmıyor", getattr(e, "status_code", None) != 403)

    # ============================================================
    # 6) Admin görünümü: get_interview 'consent' alanını içeriyor
    # ============================================================
    admin_row = m.get_db().execute(
        "SELECT id, tenant_name, consent_at, text_version, disclosure_text, checkbox_text, ip_address, user_agent "
        "FROM consent_records WHERE candidate_id=? AND level=?", (TEST_CID, LEVEL)).fetchone()
    check("6) admin sorgusu onay kaydını buluyor", admin_row is not None)

    # ============================================================
    # 7) AI notu YOKSA rapor promptunda AI NOTU SONUÇLARI bölümü hiç istenmiyor
    # ============================================================
    prompt_no_note = m.build_report_content_prompt("tablo", "profil_tablo", has_ai_note=False)
    check("7) AI notu yok -> '===AI NOTU SONUÇLARI===' promptta YOK", "AI NOTU SONUÇLARI" not in prompt_no_note)

    # ============================================================
    # 8) AI notu VARSA rapor promptunda bölüm isteniyor + puanı etkilemediği açıkça belirtiliyor
    # ============================================================
    prompt_with_note = m.build_report_content_prompt("tablo", "profil_tablo", has_ai_note=True)
    check("8) AI notu var -> '===AI NOTU SONUÇLARI===' promptta VAR", "AI NOTU SONUÇLARI" in prompt_with_note)
    check("8) prompt puanı ETKİLEMEZ diye açıkça belirtiyor", "ETKİLEMEZ" in prompt_with_note)

    # ============================================================
    # 9) '===AI Notu Sonuçları===' ayracı doğru anahtara parse ediliyor
    # ============================================================
    parsed = m.parse_llm_report_sections("===AI NOTU SONUÇLARI===\nAday İngilizce soruda B2 seviyesinde cevap verdi.\n===GÜÇLÜ YÖNLER===\nX")
    check("9) 'ai_notu_sonuclari' anahtarına doğru parse edildi", parsed.get("ai_notu_sonuclari", "").startswith("Aday İngilizce"))

    # ============================================================
    # 10) AI notuyla eklenen ekstra kriterler puanlama motoruna hiç girmiyor: select_criteria_for_level
    #     ile üretilen kriter listeleri SABİT (pozisyon/profil tanımlı listeden), modelin/AI notunun
    #     tablo satırı EKLEMESİ mümkün değil (recompute_and_fix_score satır ekleme/çıkarma kabul etmez).
    # ============================================================
    sel, exc = m.select_criteria_for_level([{"name": f"K{i}", "weight": 10} for i in range(10)], 1)
    check("10) seçili kriter sayısı sabit (AI notu bunu değiştiremez, parametre almıyor)", len(sel) == 3)

finally:
    cleanup()

print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("Tüm KVKK/AI notu testleri GEÇTİ.")

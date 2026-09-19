# İŞ EMRİ — KİŞİ GEÇMİŞİ / DÜZENLE SENKRONİZASYONU — regresyon testleri.
# Tamamen JENERİK/sentetik verilerle — hiçbir gerçek kişiye özel hardcode yok.
# Hiçbir gerçek ağ/API çağrısı yapılmaz (bu iş salt DB üzerinde deterministik, LLM içermez).
#
# Çalıştırma: py test_kisi_gecmisi_duzenle_sync.py  (backend/ dizininde)

import sys
import main as m

FAILURES = []


def check(label, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        FAILURES.append(label)


# Test kayıtları için ayrılmış, gerçek verilerle çakışmayacak bir ID aralığı.
PERSON_A, PERSON_B = 970101, 970102
CAND_A, CAND_B = 970201, 970202
INTERVIEW_LEVEL = 1


def cleanup():
    db = m.get_db()
    try:
        for cid in (CAND_A, CAND_B, CAND_B + 1000):
            db.execute("DELETE FROM interviews WHERE candidate_id=?", (cid,))
        db.execute("DELETE FROM candidates WHERE id IN (?, ?, ?)", (CAND_A, CAND_B, CAND_B + 1000))
        db.execute("DELETE FROM persons WHERE id IN (?, ?)", (PERSON_A, PERSON_B))
        db.commit()
    finally:
        db.close()


def seed():
    cleanup()
    db = m.get_db()
    try:
        db.execute(
            "INSERT INTO persons (id, org_id, full_name, email, phone) VALUES (?, ?, ?, ?, ?)",
            (PERSON_A, 1, "Cihan ALAKUŞ", "cihan.alakus@example.com", "5550000001"))
        db.execute(
            "INSERT INTO candidates (id, name, email, phone, position, level, person_id, org_id, username, password_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (CAND_A, "Cihan ALAKUŞ", "cihan.alakus@example.com", "5550000001", "Test Pozisyonu",
             INTERVIEW_LEVEL, PERSON_A, 1, f"test_user_{CAND_A}", "x"))
        # İş 6E — aynı person'a bağlı GEÇMİŞ, TAMAMLANMIŞ bir mülakat: skor/rapor verisi
        # düzenleme sırasında YENİDEN YAZILMAMALI. (Not: admin_update_candidate zaten
        # tamamlanmış mülakatı olan candidate'ı KİLİTLER — bu yüzden geçmiş interview'ı
        # AYRI bir eski candidate/level satırına değil, DOĞRUDAN bu candidate'a bağlarsak
        # düzenleme 409 ile engellenir; iş emrinin E testi "aynı person'a bağlı geçmiş
        # mülakatlar" dediği için, tamamlanmamış CAND_A'yı düzenleyip PERSON_A'ya bağlı
        # AYRI bir CAND_B üzerindeki tamamlanmış interview'ın dokunulmadığını doğruluyoruz.)
        db.execute(
            "INSERT INTO candidates (id, name, email, phone, position, level, person_id, org_id, username, password_hash, "
            "completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (CAND_B, "Cihan ALAKUŞ", "cihan.alakus@example.com", "5550000001", "Eski Pozisyon",
             2, PERSON_A, 1, f"test_user_{CAND_B}", "x", "2026-01-01T10:00:00"))
        db.execute(
            "INSERT INTO interviews (candidate_id, level, messages, report, score, score_position, score_profile, "
            "recommendation, completed_at) VALUES (?, ?, '[]', ?, ?, ?, ?, ?, ?)",
            (CAND_B, 2, "**Yönetici Özeti:**\nGeçmiş mülakat raporu.", 70, 70, 70, "Değerlendir", "2026-01-01T10:00:00"))
        db.commit()
    finally:
        db.close()

    db2 = m.get_db()
    try:
        db2.execute(
            "INSERT INTO persons (id, org_id, full_name, email, phone) VALUES (?, ?, ?, ?, ?)",
            (PERSON_B, 1, "Ayrı Kişi", "ayri.kisi@example.com", "5559999999"))
        db2.execute(
            "INSERT INTO candidates (id, name, email, phone, position, level, person_id, org_id, username, password_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (CAND_B + 1000, "Ayrı Kişi", "ayri.kisi@example.com", "5559999999", "Ayrı Pozisyon",
             1, PERSON_B, 1, f"test_user_{CAND_B + 1000}", "x"))
        db2.commit()
    finally:
        db2.close()


def read_person(pid):
    db = m.get_db()
    try:
        row = db.execute("SELECT * FROM persons WHERE id=?", (pid,)).fetchone()
    finally:
        db.close()
    return dict(row) if row else None


def read_candidate(cid):
    db = m.get_db()
    try:
        row = db.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()
    finally:
        db.close()
    return dict(row) if row else None


def read_interview(cid, level):
    db = m.get_db()
    try:
        row = db.execute("SELECT * FROM interviews WHERE candidate_id=? AND level=?", (cid, level)).fetchone()
    finally:
        db.close()
    return dict(row) if row else None


try:
    # ============================================================
    # A) Ad Soyad değişikliği: Cihan ALAKUŞ -> Mehmet YILDIZ
    # ============================================================
    seed()
    m.admin_update_candidate(CAND_A, m.CandidateUpdate(name="Mehmet YILDIZ"), payload={"role": "admin"})
    cand_a = read_candidate(CAND_A)
    person_a = read_person(PERSON_A)
    check("A) candidate.name = 'Mehmet YILDIZ'", cand_a["name"] == "Mehmet YILDIZ")
    check("A) bağlı person.full_name = 'Mehmet YILDIZ' (Kişi Geçmişi başlığı buradan okunuyor)",
          person_a["full_name"] == "Mehmet YILDIZ")
    check("A) person.email DEĞİŞMEDİ (yalnız isim gönderildi)", person_a["email"] == "cihan.alakus@example.com")
    check("A) person.phone DEĞİŞMEDİ", person_a["phone"] == "5550000001")

    # ============================================================
    # B) E-posta değişikliği
    # ============================================================
    seed()
    m.admin_update_candidate(CAND_A, m.CandidateUpdate(email="yeni.eposta@example.com"), payload={"role": "admin"})
    cand_a = read_candidate(CAND_A)
    person_a = read_person(PERSON_A)
    check("B) candidate.email güncellendi", cand_a["email"] == "yeni.eposta@example.com")
    check("B) person.email AYNI yeni değere güncellendi", person_a["email"] == "yeni.eposta@example.com")
    check("B) person.full_name DEĞİŞMEDİ (yalnız e-posta gönderildi)", person_a["full_name"] == "Cihan ALAKUŞ")

    # ============================================================
    # C) Telefon değişikliği
    # ============================================================
    seed()
    m.admin_update_candidate(CAND_A, m.CandidateUpdate(phone="5551234567"), payload={"role": "admin"})
    cand_a = read_candidate(CAND_A)
    person_a = read_person(PERSON_A)
    check("C) candidate.phone güncellendi", cand_a["phone"] == "5551234567")
    check("C) person.phone AYNI yeni değere güncellendi", person_a["phone"] == "5551234567")

    # ============================================================
    # D) Başka person/candidate kayıtları DEĞİŞMEMELİ
    # ============================================================
    seed()
    m.admin_update_candidate(CAND_A, m.CandidateUpdate(name="Mehmet YILDIZ", email="mehmet@example.com", phone="5550001111"),
                             payload={"role": "admin"})
    person_b = read_person(PERSON_B)
    cand_other = read_candidate(CAND_B + 1000)
    check("D) İlgisiz person (PERSON_B) DEĞİŞMEDİ", person_b["full_name"] == "Ayrı Kişi" and person_b["email"] == "ayri.kisi@example.com")
    check("D) İlgisiz candidate DEĞİŞMEDİ", cand_other["name"] == "Ayrı Kişi" and cand_other["phone"] == "5559999999")
    # Aynı person'a bağlı DİĞER candidate (CAND_B, tamamlanmış mülakatı olan) ismi de
    # senkron kalmalı MI? İş emri yalnız "candidate edit -> person" yönünü istiyor; CAND_B
    # bizzat DÜZENLENMEDİ (kilitli, tamamlanmış), bu yüzden CAND_B.name kasıtlı olarak
    # ESKİ kalabilir — bu regresyon DEĞİL, kapsam dışı (yalnız DÜZENLENEN candidate + person senkronize olur).
    cand_b = read_candidate(CAND_B)
    check("D) Düzenlenmeyen (kilitli) CAND_B kaydı DOKUNULMADI (kasıtlı, kapsam dışı)", cand_b["name"] == "Cihan ALAKUŞ")

    # ============================================================
    # E) Aynı person'a bağlı GEÇMİŞ (tamamlanmış) mülakatın skor/rapor verisi DEĞİŞMEMELİ
    # ============================================================
    iv_b_before = read_interview(CAND_B, 2)
    seed()
    m.admin_update_candidate(CAND_A, m.CandidateUpdate(name="Mehmet YILDIZ"), payload={"role": "admin"})
    iv_b_after = read_interview(CAND_B, 2)
    check("E) Geçmiş mülakatın report/score/recommendation alanları DEĞİŞMEDİ",
          iv_b_after["report"] == "**Yönetici Özeti:**\nGeçmiş mülakat raporu."
          and iv_b_after["score"] == 70 and iv_b_after["recommendation"] == "Değerlendir")

    # ============================================================
    # Ek: candidate'ın person_id'si YOKSA (NULL) hata vermeden, yeni person OLUŞTURMADAN devam eder
    # ============================================================
    seed()
    db_np = m.get_db()
    try:
        db_np.execute("UPDATE candidates SET person_id=NULL WHERE id=?", (CAND_A,))
        db_np.commit()
    finally:
        db_np.close()
    _persons_before = None
    db_cnt = m.get_db()
    try:
        _persons_before = db_cnt.execute("SELECT COUNT(*) AS n FROM persons").fetchone()["n"]
    finally:
        db_cnt.close()
    m.admin_update_candidate(CAND_A, m.CandidateUpdate(name="Yeni İsim"), payload={"role": "admin"})
    cand_np = read_candidate(CAND_A)
    db_cnt2 = m.get_db()
    try:
        _persons_after = db_cnt2.execute("SELECT COUNT(*) AS n FROM persons").fetchone()["n"]
    finally:
        db_cnt2.close()
    check("Ek) person_id NULL iken düzenleme hatasız tamamlanır", cand_np["name"] == "Yeni İsim")
    check("Ek) person_id NULL iken YENİ bir person satırı OLUŞTURULMAZ", _persons_after == _persons_before)

    # ============================================================
    # Ek: isim/e-posta/telefon HİÇBİRİ gönderilmezse (ör. yalnız pozisyon değişikliği) person'a
    # HİÇ dokunulmaz (gereksiz UPDATE yok).
    # ============================================================
    seed()
    m.admin_update_candidate(CAND_A, m.CandidateUpdate(position="Farklı Pozisyon"), payload={"role": "admin"})
    person_a2 = read_person(PERSON_A)
    check("Ek) Yalnız pozisyon değişince person.full_name/email/phone AYNEN kalır",
          person_a2["full_name"] == "Cihan ALAKUŞ" and person_a2["email"] == "cihan.alakus@example.com"
          and person_a2["phone"] == "5550000001")

finally:
    cleanup()


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("Tüm İŞ EMRİ — KİŞİ GEÇMİŞİ / DÜZENLE SENKRONİZASYONU testleri GEÇTİ.")

if FAILURES:
    sys.exit(1)

# İŞ EMRİ — POZİSYON GRUPLAMA YAPISI + TAMAMLANMIŞ MÜLAKATLARDA SALT OKUNUR GÖRÜNTÜLEME
# — backend-testable senaryolar (sentetik, gerçek AI çağrısı YOK).
#
# Kapsam:
# 1) positions.category serbest metin kolonu — şema/migration gerekmeden YENİ bir kategori
#    değeriyle pozisyon oluşturulup güncellenebiliyor mu (Kısım 1 teşhisinin doğrulaması).
# 2) admin_update_candidate (PATCH /api/admin/candidates/{id}) — completed_at dolu bir
#    mülakatı olan adayın HİÇBİR alanı (level/position dahil) değiştirilemiyor mu (Kısım 2'nin
#    KRİTİK backend koruması zaten mevcut mu, doğrudan fonksiyon çağrısıyla).
# 3) Aynı endpoint, TAMAMLANMAMIŞ (completed_at NULL) bir aday için normal şekilde
#    çalışmaya devam ediyor mu (regresyon güvencesi — mevcut davranış BOZULMADI).
#
# Çalıştırma: python test_pozisyon_grup_ve_salt_okunur.py  (backend/ dizininde)

import sys

import main as m
from fastapi import HTTPException

FAILURES = []


def check(label, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        FAILURES.append(label)


CAND_DONE = 980401   # tamamlanmış mülakatı olan aday
CAND_OPEN = 980402   # tamamlanmamış (açık) aday
POS_NAME = "Test Pozisyonu Grup Senkron 980401"
NEW_CATEGORY = "Sentetik Test Grubu 980401"


def cleanup():
    db = m.get_db()
    try:
        db.execute("DELETE FROM interviews WHERE candidate_id IN (?, ?)", (CAND_DONE, CAND_OPEN))
        db.execute("DELETE FROM candidates WHERE id IN (?, ?)", (CAND_DONE, CAND_OPEN))
        db.execute("DELETE FROM positions WHERE name=?", (POS_NAME,))
        db.commit()
    finally:
        db.close()


def seed_candidate(cid, level, completed):
    db = m.get_db()
    try:
        db.execute(
            "INSERT INTO candidates (id, name, position, cv_text, level, interview_language, report_language, username, password_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, "Test Aday PGG", "Test Pozisyonu", "Sentetik CV metni.",
             level, "tr", "tr", f"test_user_{cid}", "x"))
        db.execute(
            "INSERT INTO interviews (candidate_id, level, depth_tier, completed_at) VALUES (?, ?, ?, ?)",
            (cid, level, "standart", "2026-01-01 10:00:00" if completed else None))
        db.commit()
    finally:
        db.close()


def call_patch(cid, **fields):
    data = m.CandidateUpdate(**fields)
    return m.admin_update_candidate(cid, data, payload={"role": "admin", "org_id": None})


def read_candidate(cid):
    db = m.get_db()
    try:
        row = db.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()
    finally:
        db.close()
    return dict(row) if row else None


try:
    cleanup()
    seed_candidate(CAND_DONE, level=2, completed=True)
    seed_candidate(CAND_OPEN, level=1, completed=False)

    # --- A) positions.category: yeni bir grup değeriyle pozisyon oluşturma/güncelleme (şema değişikliği YOK) ---
    six_criteria = [{"name": f"K{i}", "weight": 100 // 6 if i < 5 else 100 - 5 * (100 // 6), "desc": ""} for i in range(6)]
    create_data = m.PositionCreate(name=POS_NAME, category=NEW_CATEGORY, role_description="Test", criteria=[m.CriterionItem(**c) for c in six_criteria])
    db_create = m.get_db()
    try:
        r = m.create_position(create_data, payload={"role": "admin", "org_id": None}, db=db_create)
        check("A) Yeni/özel kategori değeriyle pozisyon oluşturuldu (409/422 YOK)", True)
    except HTTPException as e:
        check(f"A) Yeni/özel kategori değeriyle pozisyon oluşturuldu (409/422 YOK) - HATA: {e.detail}", False)
    finally:
        db_create.close()

    db = m.get_db()
    try:
        row = db.execute("SELECT category FROM positions WHERE name=?", (POS_NAME,)).fetchone()
    finally:
        db.close()
    check("A) Kaydedilen kategori DEĞİŞTİRİLMEDEN saklandı (serbest metin, şema/migration gerekmedi)",
          row is not None and row["category"] == NEW_CATEGORY)

    # --- B) Tamamlanmış aday: HİÇBİR alan değiştirilemez (level, position dahil) ---
    before = read_candidate(CAND_DONE)
    blocked_level = False
    blocked_position = False
    blocked_name = False
    try:
        call_patch(CAND_DONE, level=3)
    except HTTPException as e:
        blocked_level = (e.status_code == 409)
    try:
        call_patch(CAND_DONE, position="Başka Pozisyon")
    except HTTPException as e:
        blocked_position = (e.status_code == 409)
    try:
        call_patch(CAND_DONE, name="Değiştirilmiş İsim")
    except HTTPException as e:
        blocked_name = (e.status_code == 409)
    after = read_candidate(CAND_DONE)

    check("B) Tamamlanmış adayda level PATCH'i 409 ile REDDEDİLDİ", blocked_level)
    check("B) Tamamlanmış adayda position PATCH'i 409 ile REDDEDİLDİ", blocked_position)
    check("B) Tamamlanmış adayda name PATCH'i 409 ile REDDEDİLDİ", blocked_name)
    check("B) Reddedilen isteklerden SONRA candidate satırı HİÇ DEĞİŞMEDİ (level aynı)", before["level"] == after["level"])
    check("B) Reddedilen isteklerden SONRA candidate satırı HİÇ DEĞİŞMEDİ (position aynı)", before["position"] == after["position"])
    check("B) Reddedilen isteklerden SONRA candidate satırı HİÇ DEĞİŞMEDİ (name aynı)", before["name"] == after["name"])
    check("B) Reddedilen isteklerden SONRA candidate satırı HİÇ DEĞİŞMEDİ (status aynı, listeden kaybolmadı)", before["status"] == after["status"])

    # --- C) Tamamlanmamış (açık) aday: normal PATCH davranışı BOZULMADI (regresyon güvencesi) ---
    r_open = call_patch(CAND_OPEN, name="Güncellenmiş Ad")
    after_open = read_candidate(CAND_OPEN)
    check("C) Tamamlanmamış adayda PATCH normal şekilde ÇALIŞTI (exception yok)", r_open is not None)
    check("C) Tamamlanmamış adayda alan gerçekten güncellendi", after_open["name"] == "Güncellenmiş Ad")

finally:
    cleanup()


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("Tüm İŞ EMRİ — POZİSYON GRUPLAMA + SALT OKUNUR GÖRÜNTÜLEME backend testleri GEÇTİ.")

if FAILURES:
    sys.exit(1)

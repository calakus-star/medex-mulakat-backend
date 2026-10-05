"""İŞ EMRİ — SÜPERADMİN İÇİN "TÜM KURUMLARA EKLE" SEÇENEĞİ — regresyon testi."""
import sys
import main as m

FAILURES = []


def check(label, cond):
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        FAILURES.append(label)


TEST_POS_NAME = "Test Poz MACS4_ALLORGS"
ORG_SLUGS = ["test-tum-kurum-a", "test-tum-kurum-b"]


def cleanup():
    db = m.get_db()
    try:
        db.execute("DELETE FROM positions WHERE name LIKE ?", (f"{TEST_POS_NAME}%",))
        for slug in ORG_SLUGS:
            row = db.execute("SELECT id FROM organizations WHERE slug=?", (slug,)).fetchone()
            if row:
                db.execute("DELETE FROM positions WHERE org_id=? AND name LIKE ?", (row["id"], f"{TEST_POS_NAME}%"))
                db.execute("DELETE FROM organizations WHERE id=?", (row["id"],))
        db.commit()
    finally:
        db.close()


def make_criteria():
    base = 100 // 6
    rem = 100 - base * 5
    return [m.CriterionItem(name=f"K{i}", weight=(base if i < 5 else rem), desc="") for i in range(6)]


cleanup()
try:
    db = m.get_db()
    db.execute("INSERT INTO organizations (name, slug) VALUES (?, ?)", ("Test Tüm Kurum A", "test-tum-kurum-a"))
    db.execute("INSERT INTO organizations (name, slug) VALUES (?, ?)", ("Test Tüm Kurum B", "test-tum-kurum-b"))
    db.commit()
    medex_id = m.get_medex_org_id(db)
    org_a_id = db.execute("SELECT id FROM organizations WHERE slug=?", (ORG_SLUGS[0],)).fetchone()["id"]
    org_b_id = db.execute("SELECT id FROM organizations WHERE slug=?", (ORG_SLUGS[1],)).fetchone()["id"]
    # Org A'da aynı adda bir pozisyon zaten var — çakışma/numaralama senaryosu için.
    crit_json = m.json.dumps([c.dict() for c in make_criteria()], ensure_ascii=False)
    db.execute("INSERT INTO positions (name, category, role_description, criteria_json, org_id, is_customized) VALUES (?,?,?,?,?,1)",
               (TEST_POS_NAME, "Genel", "desc", crit_json, org_a_id))
    db.commit()
    db.close()

    # ============================================================
    # 0) ÖN RAPOR DOĞRULAMASI — mevcut davranış: süperadmin pozisyon eklerse org_id override
    #    verilmediği sürece MedeX'e gider (get_org_id_for_admin).
    # ============================================================
    data_plain = m.PositionCreate(name="Test Poz Plain Superadmin", category="Genel", role_description="d", criteria=make_criteria())
    db_c = m.get_db()
    org_id_check = m.get_org_id_for_admin(db_c, {"admin_role": "superadmin", "org_id": None})
    db_c.close()
    check("0) süperadmin override'sız org_id -> MedeX", org_id_check == medex_id)
    _db_cleanup = m.get_db()
    _db_cleanup.execute("DELETE FROM positions WHERE name=?", ("Test Poz Plain Superadmin",))
    _db_cleanup.commit()
    _db_cleanup.close()

    # ============================================================
    # 1) Kurum admini apply_to_all_orgs göndersin -> 403 (madde 2)
    # ============================================================
    data = m.PositionCreate(name=TEST_POS_NAME, category="Genel", role_description="d", criteria=make_criteria(), apply_to_all_orgs=True)
    db_1 = m.get_db()
    try:
        m.create_position(data, payload={"admin_role": "org_admin", "org_id": medex_id}, db=db_1)
        check("1) org_admin apply_to_all_orgs -> 403", False)
    except Exception as e:
        check("1) org_admin apply_to_all_orgs -> 403", getattr(e, "status_code", None) == 403)
    finally:
        db_1.close()
    # org_admin'in reddedilen isteği HİÇBİR kuruma eklenmemiş olmalı (hiç işlenmedi)
    db_1b = m.get_db()
    cnt_after_reject = db_1b.execute("SELECT COUNT(*) c FROM positions WHERE name=?", (TEST_POS_NAME,)).fetchone()["c"]
    db_1b.close()
    check("1) reddedilen istek hiçbir kuruma eklenmedi (yalnız Org A'daki önceden var olan 1 kayıt)", cnt_after_reject == 1)

    # ============================================================
    # 2) Süperadmin "Tüm kurumlara ekle" -> her kuruma ayrı kopya, çakışan kurumda _2
    # ============================================================
    db_2 = m.get_db()
    try:
        res = m.create_position(data, payload={"admin_role": "superadmin", "org_id": None}, db=db_2)
    finally:
        db_2.close()
    check("2) added_to >= 3 (en az MedeX + 2 test kurumu)", res["added_to"] >= 3)
    check("2) renamed listesinde Org A var", any(r["org_name"] == "Test Tüm Kurum A" for r in res["renamed"]))
    check("2) renamed yeni ad '_2' ile bitiyor", any(r["new_name"] == f"{TEST_POS_NAME}_2" for r in res["renamed"]))
    check("2) özet metninde 'kuruma eklendi' ve 'farklı adla' geçiyor", "kuruma eklendi" in res["message"] and "farklı adla eklendi" in res["message"])

    db2 = m.get_db()
    medex_rows = [r["name"] for r in db2.execute("SELECT name FROM positions WHERE org_id=? AND name LIKE ?", (medex_id, f"{TEST_POS_NAME}%")).fetchall()]
    a_rows = [r["name"] for r in db2.execute("SELECT name FROM positions WHERE org_id=? AND name LIKE ?", (org_a_id, f"{TEST_POS_NAME}%")).fetchall()]
    b_rows = [r["name"] for r in db2.execute("SELECT name FROM positions WHERE org_id=? AND name LIKE ?", (org_b_id, f"{TEST_POS_NAME}%")).fetchall()]
    db2.close()
    check("2) MedeX'e orijinal adla eklendi", medex_rows == [TEST_POS_NAME])
    check("2) Org A'da ESKİ kayıt dokunulmadan kaldı + yeni '_2' eklendi", sorted(a_rows) == sorted([TEST_POS_NAME, f"{TEST_POS_NAME}_2"]))
    check("2) Org B'ye orijinal adla eklendi (çakışma yok)", b_rows == [TEST_POS_NAME])

    # ============================================================
    # 3) İkinci kez "tüm kurumlara ekle" çağrılırsa artık HEM MedeX HEM B'de de çakışma olur -> _2
    # ============================================================
    data2 = m.PositionCreate(name=TEST_POS_NAME, category="Genel", role_description="d", criteria=make_criteria(), apply_to_all_orgs=True)
    db_3 = m.get_db()
    try:
        res2 = m.create_position(data2, payload={"admin_role": "superadmin", "org_id": None}, db=db_3)
    finally:
        db_3.close()
    check("3) ikinci turda MedeX ve Org B de 'renamed' listesinde (artık onlarda da çakışma var)",
          any(r["org_name"] != "Test Tüm Kurum A" for r in res2["renamed"]))

    # ============================================================
    # 4) DEĞİŞMEYEN: normal (apply_to_all_orgs olmadan) ekleme hâlâ tek kuruma gidiyor.
    # ============================================================
    data3 = m.PositionCreate(name="Test Poz Normal Tek Kurum", category="Genel", role_description="d", criteria=make_criteria())
    db_4 = m.get_db()
    try:
        m.create_position(data3, payload={"admin_role": "org_admin", "org_id": org_a_id}, db=db_4)
    finally:
        db_4.close()
    db3 = m.get_db()
    cnt_a = db3.execute("SELECT COUNT(*) c FROM positions WHERE name=? AND org_id=?", ("Test Poz Normal Tek Kurum", org_a_id)).fetchone()["c"]
    cnt_total = db3.execute("SELECT COUNT(*) c FROM positions WHERE name=?", ("Test Poz Normal Tek Kurum",)).fetchone()["c"]
    db3.close()
    check("4) normal ekleme yalnız ilgili kuruma gitti (toplam=1)", cnt_a == 1 and cnt_total == 1)
    db_4b = m.get_db()
    db_4b.execute("DELETE FROM positions WHERE name=?", ("Test Poz Normal Tek Kurum",))
    db_4b.commit()
    db_4b.close()

finally:
    cleanup()

print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("Tüm 'Tüm Kurumlara Ekle' testleri GEÇTİ.")

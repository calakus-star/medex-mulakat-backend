# İŞ EMRİ — MEVCUT "GÖRÜNTÜ VE SES GÖZLEMİ" BÖLÜMÜNÜ ZENGİNLEŞTİR, RAPORUN BAŞINA TAŞI,
# 1 TEMSİLÎ KAMERA GÖRÜNTÜSÜ EKLE VE KAMERA SİSTEMİNİ DENETÇİ MANTIĞINA UYGUN HALE GETİR
# — backend-testable senaryolar (sentetik, gerçek AI/kamera çağrısı YOK).
#
# Kapsam: select_verification_frames level-scoping (madde 20/37), select_representative_camera_image
# (madde 20-23), save_snapshot camera_validation level-scoped kota (madde 18), build_modality_prose
# periyodik kare ifadesi (madde 24/25/26) + camera_validation önce (madde 15/27) + metodoloji notu
# (madde 32), PDF bölüm sırası (madde 27) — pdfminer ile metin çıkarımı.
#
# Çalıştırma: python test_goruntu_ses_gozlemi_zenginlestirme.py  (backend/ dizininde)

import base64
import io
import json
import sys

import main as m

try:
    from PIL import Image as PILImage
except Exception:
    PILImage = None

FAILURES = []


def check(label, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        FAILURES.append(label)


CAND = 980301


def cleanup():
    db = m.get_db()
    try:
        db.execute("DELETE FROM snapshots WHERE candidate_id=?", (CAND,))
        db.execute("DELETE FROM interviews WHERE candidate_id=?", (CAND,))
        db.execute("DELETE FROM candidates WHERE id=?", (CAND,))
        db.commit()
    finally:
        db.close()


def seed_candidate(level=2):
    db = m.get_db()
    try:
        db.execute(
            "INSERT INTO candidates (id, name, position, cv_text, level, interview_language, report_language, username, password_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (CAND, "Test Aday GSZ", "Test Pozisyonu", "Sentetik CV metni.",
             level, "tr", "tr", f"test_user_{CAND}", "x"))
        db.commit()
    finally:
        db.close()


def seed_interview(level, extra=None):
    db = m.get_db()
    try:
        db.execute("DELETE FROM interviews WHERE candidate_id=? AND level=?", (CAND, level))
        db.execute("INSERT INTO interviews (candidate_id, level, depth_tier) VALUES (?, ?, ?)",
                   (CAND, level, "standart"))
        if extra:
            for k, v in extra.items():
                db.execute(f"UPDATE interviews SET {k}=? WHERE candidate_id=? AND level=?", (v, CAND, level))
        db.commit()
    finally:
        db.close()


def make_data_url(mean_gray: int, textured=False, size=(80, 60)) -> str:
    if PILImage is None:
        return "data:image/jpeg;base64," + base64.b64encode(b"x").decode()
    img = PILImage.new("L", size, color=mean_gray)
    if textured:
        px = img.load()
        for x in range(0, size[0], 4):
            for y in range(0, size[1], 4):
                px[x, y] = min(255, mean_gray + 40)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


USABLE_IMG = make_data_url(150, textured=True)
DARK_IMG = make_data_url(2)


def insert_snapshot(level, reason, image=USABLE_IMG, elapsed_ms=1000):
    db = m.get_db()
    try:
        row = db.execute(
            "INSERT INTO snapshots (candidate_id, image_base64, level, elapsed_ms, reason) VALUES (?, ?, ?, ?, ?) RETURNING id",
            (CAND, image, level, elapsed_ms, reason)
        ).fetchone()
        db.commit()
        return row["id"]
    finally:
        db.close()


def append_cam_event(level, status, reason, description=None):
    event = {"type": "camera_validation", "source": "candidate", "status": status, "reason": reason,
              "description": description or (m._CAMERA_VALIDATION_REASON_TEXT.get(reason) if status == "verified"
                                              else m._CAMERA_VALIDATION_REASON_TEXT.get(reason, m._CAMERA_VALIDATION_UNVERIFIED_FALLBACK_TEXT))}
    m._append_result_event(CAND, level, event)


try:
    cleanup()
    seed_candidate(level=2)
    seed_interview(2)
    seed_interview(3)

    # === A) select_verification_frames LEVEL-SCOPED (madde 20/37) — L2/L3 mimic kareleri karışmaz ===
    insert_snapshot(2, "mimic_sample", elapsed_ms=1000)
    insert_snapshot(2, "mimic_sample", elapsed_ms=2000)
    insert_snapshot(2, "mimic_sample", elapsed_ms=3000)
    id_l3_a = insert_snapshot(3, "mimic_sample", elapsed_ms=1500)
    id_l3_b = insert_snapshot(3, "mimic_sample", elapsed_ms=2500)

    frames_l2 = m.select_verification_frames(CAND, 2)
    frames_l3 = m.select_verification_frames(CAND, 3)
    l2_ids = {f["id"] for f in frames_l2}
    l3_ids = {f["id"] for f in frames_l3}
    check("A) L2 doğrulama kareleri L3 karelerini İÇERMİYOR", not (l2_ids & {id_l3_a, id_l3_b}))
    check("A) L3 doğrulama kareleri gerçekten L3'e ait", l3_ids == {id_l3_a, id_l3_b} or l3_ids.issubset({id_l3_a, id_l3_b}))
    check("A) L2 kareleri var (3 tane eklendi)", len(frames_l2) == 3)

    # === B) select_representative_camera_image — candidate+level scoping + öncelik sırası ===
    cleanup_snap_l2 = m.get_db()
    try:
        cleanup_snap_l2.execute("DELETE FROM snapshots WHERE candidate_id=?", (CAND,))
        cleanup_snap_l2.commit()
    finally:
        cleanup_snap_l2.close()

    # Yalnız L3'te camera_validation karesi var -> L2 isteği None dönmeli.
    cv_id_l3 = insert_snapshot(3, "camera_validation", image=USABLE_IMG)
    rep_l2 = m.select_representative_camera_image(CAND, 2)
    rep_l3 = m.select_representative_camera_image(CAND, 3)
    check("B) L2 raporuna L3'ün temsilî görüntüsü GELMEDİ", rep_l2 is None)
    check("B) L3 kendi camera_validation karesini seçti", rep_l3 is not None and rep_l3["id"] == cv_id_l3 and rep_l3["reason"] == "camera_validation")

    # camera_validation karesi KARANLIKSA normal kareye düşer (öncelik sırası).
    db = m.get_db()
    try:
        db.execute("DELETE FROM snapshots WHERE candidate_id=? AND level=3", (CAND,))
        db.commit()
    finally:
        db.close()
    insert_snapshot(3, "camera_validation", image=DARK_IMG)
    normal_id = insert_snapshot(3, "auto", image=USABLE_IMG, elapsed_ms=500)
    rep_fallback = m.select_representative_camera_image(CAND, 3)
    check("B) Karanlık camera_validation karesi atlanıp normal kareye düşüldü", rep_fallback is not None and rep_fallback["id"] == normal_id and rep_fallback["reason"] == "auto")

    # Hiç kullanılabilir kare yoksa None (madde 23).
    db = m.get_db()
    try:
        db.execute("DELETE FROM snapshots WHERE candidate_id=? AND level=3", (CAND,))
        db.commit()
    finally:
        db.close()
    insert_snapshot(3, "auto", image=DARK_IMG)
    rep_none = m.select_representative_camera_image(CAND, 3)
    check("B) Uygun kare yokken None döner (PDF/admin kendi fallback metnini basar)", rep_none is None)

    # === C) save_snapshot camera_validation kotası LEVEL-SCOPED (madde 18) ===
    db = m.get_db()
    try:
        db.execute("DELETE FROM snapshots WHERE candidate_id=?", (CAND,))
        db.commit()
    finally:
        db.close()
    data_l2 = m.SnapshotData(candidate_id=CAND, image_base64=USABLE_IMG, level=2, elapsed_ms=1000, reason="camera_validation")
    data_l3 = m.SnapshotData(candidate_id=CAND, image_base64=USABLE_IMG, level=3, elapsed_ms=1000, reason="camera_validation")
    db1 = m.get_db()
    try:
        r_l2 = m.save_snapshot(data_l2, payload={"role": "candidate", "candidate_id": CAND}, db=db1)
    finally:
        db1.close()
    db2 = m.get_db()
    try:
        r_l3 = m.save_snapshot(data_l3, payload={"role": "candidate", "candidate_id": CAND}, db=db2)
    finally:
        db2.close()
    check("C) L2'nin camera_validation karesi kabul edildi", r_l2.get("id") is not None)
    check("C) L3'ün camera_validation karesi L2 KOTASINDAN ETKİLENMEDEN kabul edildi", r_l3.get("id") is not None)

    # === D) build_modality_prose — periyodik kare teknik ifadesi (madde 24/25) ===
    db = m.get_db()
    try:
        db.execute("DELETE FROM snapshots WHERE candidate_id=?", (CAND,))
        db.commit()
    finally:
        db.close()
    insert_snapshot(2, "mimic_sample", image=USABLE_IMG, elapsed_ms=1000)
    insert_snapshot(2, "mimic_sample", image=USABLE_IMG, elapsed_ms=5000)
    insert_snapshot(2, "mimic_sample", image=DARK_IMG, elapsed_ms=9000)
    prose_partial = m.build_modality_prose(CAND, 2)
    check("D) Kısmi kullanılabilirlik ifadesi doğru (2'si teknik olarak değerlendirilebilir)", "2'i teknik olarak değerlendirilebilir durumdaydı" in prose_partial or "2'si teknik olarak değerlendirilebilir durumdaydı" in prose_partial)
    check("D) Eski aşırı-iddia cümlesi hâlâ YOK", "aday görüntüsü doğrulandı" not in prose_partial and "aday görüntüsü doğrulanabildi" not in prose_partial)
    check("D) 'kişi' doğrulama iddiası YOK (yalnız teknik/görsel veri ifadesi)", "kişi" not in prose_partial.split("Ses:")[0] or "kişi tespit" in prose_partial or "kişi doğrulaması" in prose_partial)

    db = m.get_db()
    try:
        db.execute("DELETE FROM snapshots WHERE candidate_id=?", (CAND,))
        db.commit()
    finally:
        db.close()
    insert_snapshot(2, "mimic_sample", image=DARK_IMG, elapsed_ms=1000)
    prose_none_usable = m.build_modality_prose(CAND, 2)
    check("D) Tek kare ve karanlıksa doğru tekil ifade", "Bir kamera karesinden değerlendirilebilir görsel veri elde edilemedi." in prose_none_usable)

    # === E) build_modality_prose — camera_validation olayı EN BAŞA gelir, description AYNEN kullanılır ===
    db = m.get_db()
    try:
        db.execute("DELETE FROM snapshots WHERE candidate_id=?", (CAND,))
        db.commit()
    finally:
        db.close()
    seed_interview(2)
    append_cam_event(2, "unverified", "multiple_people")
    prose_cam = m.build_modality_prose(CAND, 2)
    check("E) camera_validation description'ı (multiple_people) prose'da AYNEN var", "Başlangıç kamera kontrolünde birden fazla kişi tespit edildi." in prose_cam)
    _goruntu_line = prose_cam.split("Görüntü: ", 1)[1].split("\n")[0] if "Görüntü: " in prose_cam else ""
    check("E) camera_validation cümlesi paragrafın EN BAŞINDA (madde 15/27)", _goruntu_line.startswith("Başlangıç kamera kontrolünde birden fazla kişi tespit edildi."))

    # === F) build_modality_prose — metodoloji notu (madde 32) sabit metin, bölüm sonunda ===
    check("F) Metodoloji/yanılma payı notu tam metinle mevcut", "Nihai değerlendirme, raporu inceleyen yetkili tarafından yapılmalıdır." in prose_cam)
    check("F) Yüzde hata oranı YAZILMADI", "%" not in prose_cam.split("Nihai değerlendirme")[0].split("yanılma payı")[-1] if "yanılma payı" in prose_cam else True)

    # === G) report_camera_validation reason haritası — birkaç ek örnek (madde 15) ===
    def call_cv(status, reason, snapshot_id=None):
        data = m.CameraValidationResult(candidate_id=CAND, level=2, status=status, reason=reason,
                                         person_count=None, basic_video_ok=None, detector_available=None,
                                         attempts=1, client_timestamp="2026-09-25T00:00:00Z", snapshot_id=snapshot_id)
        db = m.get_db()
        try:
            return m.report_camera_validation(data, payload={"role": "candidate", "candidate_id": CAND}, db=db)
        finally:
            db.close()

    call_cv("unverified", "detector_timeout")
    ev = m._latest_camera_validation_event(CAND, 2)
    check("G) detector_timeout -> TEKNİK metin (madde 7/8/15), aday kusuru DEĞİL", ev.get("description") == "Başlangıç kişi doğrulaması teknik nedenle tamamlanamadı.")

    call_cv("unverified", "permission_denied")
    ev2 = m._latest_camera_validation_event(CAND, 2)
    check("G) permission_denied -> madde 15 örneğiyle birebir eşleşiyor", ev2.get("description") == "Kamera erişim izni sağlanamadığından görüntü gözlemi gerçekleştirilemedi.")

    call_cv("unverified", "no_camera")
    ev3 = m._latest_camera_validation_event(CAND, 2)
    check("G) no_camera -> madde 15 örneğiyle birebir eşleşiyor", ev3.get("description") == "Cihazda kullanılabilir kamera bulunamadığından görüntü gözlemi gerçekleştirilemedi.")

    # === H) PDF — "Görüntü ve Ses Gözlemi" Yönetici Özeti'nden ÖNCE basılıyor (madde 27) ===
    seed_interview(2, extra={
        "score": 70, "score_position": 70, "score_profile": 70,
        "report": ("**Yönetici Özeti:**\nBu bir test özetidir.\n\n"
                   "**Görüntü ve Ses Gözlemi:**\n\nGörüntü: Başlangıç kamera kontrolünde görüntü kullanılabilir durumdaydı ve tek kişi tespit edildi.\n\n"
                   "Bu rapordaki görüntü, davranış ve oturum bütünlüğüne ilişkin tespitlerin bir bölümü yapay zekâ ve otomatik algoritmalar tarafından, mevcut teknik koşulların el verdiği ölçüde üretilmiştir. Bu nedenle otomatik tespit ve yorumlarda her zaman yanılma payı bulunabilir. Nihai değerlendirme, raporu inceleyen yetkili tarafından yapılmalıdır.\n\n"
                   "**Analitik Düşünme ve Muhakeme:**\nTest içerik.\n"),
        "recommendation": "Değerlendir",
    })
    cand_row = m.get_db()
    try:
        candidate_dict = dict(cand_row.execute("SELECT * FROM candidates WHERE id=?", (CAND,)).fetchone())
        interview_dict = dict(cand_row.execute("SELECT * FROM interviews WHERE candidate_id=? AND level=2", (CAND,)).fetchone())
    finally:
        cand_row.close()
    try:
        pdf_buf = m._make_report_pdf(candidate_dict, interview_dict, [])
        from pdfminer.high_level import extract_text
        pdf_text = extract_text(pdf_buf)
        idx_goruntu = pdf_text.find("Görüntü ve Ses Gözlemi")
        idx_yonetici = pdf_text.find("Yönetici Özeti")
        idx_temsili = pdf_text.find("Temsilî Kamera Görüntüsü")
        check("H) PDF üretildi (exception yok)", True)
        check("H) 'Görüntü ve Ses Gözlemi' PDF'te var", idx_goruntu != -1)
        check("H) 'Yönetici Özeti' PDF'te var", idx_yonetici != -1)
        check("H) 'Görüntü ve Ses Gözlemi', 'Yönetici Özeti'nden ÖNCE geliyor (madde 27)", idx_goruntu != -1 and idx_yonetici != -1 and idx_goruntu < idx_yonetici)
        check("H) 'Temsilî Kamera Görüntüsü' başlığı PDF'te var", idx_temsili != -1)
        check("H) 'Görüntü ve Ses Gözlemi' başlığı TEK KEZ geçiyor (double-print yok)", pdf_text.count("Görüntü ve Ses Gözlemi") == 1)
    except Exception as e:
        check(f"H) PDF üretimi hatasız çalıştı ({type(e).__name__}: {e})", False)

finally:
    cleanup()


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("Tüm İŞ EMRİ — GÖRÜNTÜ VE SES GÖZLEMİ ZENGİNLEŞTİRME backend testleri GEÇTİ.")

if FAILURES:
    sys.exit(1)

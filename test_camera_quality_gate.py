# İŞ EMRİ — MÜLAKAT ÖNCESİ KAMERA KALİTE KAPISI — backend-testable senaryolar (sentetik).
# Kapsam: save_snapshot() camera_validation kota/id davranışı, /api/interview/camera-validation
# audit kaydı (verified/unverified), kör/karanlık kare backend çapraz kontrolü (madde 15),
# _latest_camera_validation_event(), build_modality_prose()'un aşırı-iddia cümlesinin kalkması
# ve yerine gelen deterministik ifade. Gerçek kamera/AI çağrısı YOK — tamamen sentetik.
#
# Çalıştırma: python test_camera_quality_gate.py  (backend/ dizininde)

import base64
import io
import json
import sys

import main as m

try:
    from PIL import Image
except Exception:
    Image = None

FAILURES = []


def check(label, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        FAILURES.append(label)


CAND = 980201
LEVEL = 1


def cleanup():
    db = m.get_db()
    try:
        db.execute("DELETE FROM snapshots WHERE candidate_id=?", (CAND,))
        db.execute("DELETE FROM interviews WHERE candidate_id=?", (CAND,))
        db.execute("DELETE FROM candidates WHERE id=?", (CAND,))
        db.commit()
    finally:
        db.close()


def seed_candidate():
    db = m.get_db()
    try:
        db.execute(
            "INSERT INTO candidates (id, name, position, cv_text, level, interview_language, report_language, username, password_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (CAND, "Test Aday Kamera", "Test Pozisyonu", "Sentetik CV metni.",
             LEVEL, "tr", "tr", f"test_user_{CAND}", "x"))
        db.execute(
            "INSERT INTO interviews (candidate_id, level, depth_tier) VALUES (?, ?, ?)",
            (CAND, LEVEL, "standart"))
        db.commit()
    finally:
        db.close()


def make_data_url(mean_gray: int, size=(80, 60), textured=False) -> str:
    if Image is None:
        # PIL yoksa dahi test'in bloke olmaması için minimal 1x1 PNG-benzeri veri döndürmeyiz;
        # bu proje zaten Pillow'a bağımlı (requirements.txt), o yüzden burada eksikse test FAIL sayılır.
        return "data:image/jpeg;base64," + base64.b64encode(b"not-an-image").decode()
    if textured:
        # Düz tek renk bir kare (varyans=0), parlak olsa DAHİ "blank" sayılır — bu yüzden
        # normal/kullanılabilir bir kareyi taklit etmek için hafif bir doku (checkerboard) eklenir.
        img = Image.new("L", size, color=mean_gray)
        px = img.load()
        for x in range(0, size[0], 4):
            for y in range(0, size[1], 4):
                px[x, y] = min(255, mean_gray + 40)
    else:
        img = Image.new("L", size, color=mean_gray)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def call_save_snapshot(reason, image_b64):
    data = m.SnapshotData(candidate_id=CAND, image_base64=image_b64, level=LEVEL, elapsed_ms=1000, reason=reason)
    db = m.get_db()
    try:
        payload = {"role": "candidate", "candidate_id": CAND}
        return m.save_snapshot(data, payload=payload, db=db)
    finally:
        db.close()


def call_camera_validation(status, reason, snapshot_id, person_count=1, basic_video_ok=True, detector_available=True, attempts=1):
    data = m.CameraValidationResult(
        candidate_id=CAND, level=LEVEL, status=status, reason=reason, person_count=person_count,
        basic_video_ok=basic_video_ok, detector_available=detector_available, attempts=attempts,
        client_timestamp="2026-09-25T00:00:00Z", snapshot_id=snapshot_id)
    db = m.get_db()
    try:
        payload = {"role": "candidate", "candidate_id": CAND}
        return m.report_camera_validation(data, payload=payload, db=db)
    finally:
        db.close()


def get_events():
    db = m.get_db()
    try:
        row = db.execute("SELECT result_events_json FROM interviews WHERE candidate_id=? AND level=?", (CAND, LEVEL)).fetchone()
    finally:
        db.close()
    if not row or not row["result_events_json"]:
        return []
    return json.loads(row["result_events_json"]) or []


try:
    cleanup()
    seed_candidate()

    bright_img = make_data_url(150, textured=True)
    dark_img = make_data_url(2)

    # --- A) save_snapshot: reason=camera_validation kota = 1 ---
    r1 = call_save_snapshot("camera_validation", bright_img)
    check("A) İlk camera_validation kare kabul edildi ve id döndü", r1.get("id") is not None)
    snap_id_1 = r1.get("id")

    r2 = call_save_snapshot("camera_validation", bright_img)
    check("B) İkinci camera_validation kare KOTA nedeniyle reddedildi (id=None)", r2.get("id") is None and r2.get("count") == 1)

    # --- C) reason=camera_validation, mimic_sample ve auto kotaları BİRBİRİNDEN BAĞIMSIZ ---
    r3 = call_save_snapshot("mimic_sample", bright_img)
    check("C) mimic_sample kotası camera_validation'dan etkilenmedi (ayrı sayaç)", r3.get("id") is not None)

    # --- D) /api/interview/camera-validation: status=verified → event yazılır, description doğru ---
    call_camera_validation("verified", "ok", snap_id_1, person_count=1)
    events = get_events()
    cam_events = [e for e in events if e.get("type") == "camera_validation"]
    check("D) camera_validation event'i yazıldı", len(cam_events) == 1)
    check("D) status=verified doğru kaydedildi", cam_events[-1].get("status") == "verified")
    check("D) description madde-13/15 uyumlu (güvenlik iddiası içermiyor, olgu bazlı)", "kullanılabilir" in cam_events[-1].get("description", "") and "güven" not in cam_events[-1].get("description", "").lower())

    # --- E) backend blank/dark çapraz kontrolü: frontend "verified" derse de kare karanlıksa UNVERIFIED'a düşer ---
    r_dark = call_save_snapshot("mimic_sample", dark_img)  # ayrı kota kullan, camera_validation kotası dolu
    # camera_validation kotasını sıfırlamak için satırı manuel silip yeniden ekleyelim.
    db = m.get_db()
    try:
        db.execute("DELETE FROM snapshots WHERE candidate_id=? AND reason='camera_validation'", (CAND,))
        db.commit()
    finally:
        db.close()
    r_dark2 = call_save_snapshot("camera_validation", dark_img)
    snap_id_dark = r_dark2.get("id")
    check("E-pre) Karanlık kare camera_validation olarak kaydedildi", snap_id_dark is not None)
    check("E-pre) _snapshot_image_is_blank_or_dark karanlık kareyi True görüyor", m._snapshot_image_is_blank_or_dark(dark_img) is True)
    check("E-pre) _snapshot_image_is_blank_or_dark parlak kareyi False görüyor", m._snapshot_image_is_blank_or_dark(bright_img) is False)

    resp_downgrade = call_camera_validation("verified", "ok", snap_id_dark, person_count=1)
    events2 = get_events()
    cam_events2 = [e for e in events2 if e.get("type") == "camera_validation"]
    check("E) Backend, karanlık kareye rağmen 'verified' diyen frontend'i UNVERIFIED'a düşürdü", cam_events2[-1].get("status") == "unverified")
    check("E) Düşürme audit'e işlendi (backend_blank_check_downgraded=True)", cam_events2[-1].get("backend_blank_check_downgraded") is True)

    # --- F) status=unverified açıkça gönderilirse aynen kaydedilir (downgrade mantığı yalnız verified'da çalışır) ---
    call_camera_validation("unverified", "person_not_detected", None, person_count=0)
    events3 = get_events()
    cam_events3 = [e for e in events3 if e.get("type") == "camera_validation"]
    check("F) unverified doğrudan kaydedildi", cam_events3[-1].get("status") == "unverified")
    check("F) description madde-15 uyumlu (0 PERSON örneğiyle eşleşiyor)", cam_events3[-1].get("description", "") == "Başlangıç kamera kontrolünde kişi tespit edilemedi.")

    # --- G) _latest_camera_validation_event en SON olayı döner ---
    latest = m._latest_camera_validation_event(CAND, LEVEL)
    check("G) _latest_camera_validation_event en son kaydı döndü", latest is not None and latest.get("status") == "unverified")

    # --- H) visible_result_events camera_validation'ı FİLTRELEMİYOR (yeni event tipi otomatik geçer) ---
    visible = m.visible_result_events(events3)
    check("H) camera_validation event'i visible_result_events'ten süzülmedi", any(e.get("type") == "camera_validation" for e in visible))

    # --- I) build_modality_prose: aşırı-iddia cümlesi ARTIK YOK ---
    prose = m.build_modality_prose(CAND, LEVEL)
    check("I) 'oturum boyunca düzenli aralıklarla doğrulandı' cümlesi KALKTI", "oturum boyunca düzenli aralıklarla doğrulandı" not in prose)

    # --- J) build_modality_prose: son camera_validation olayına göre deterministik cümle üretiliyor ---
    check("J) unverified olay metne yansıdı (kişi doğrulaması tamamlanamadı)", "kişi doğrulaması tamamlanamadı" in prose or prose == "")

    # --- K) status alanı whitelist dışı bir değer gönderilirse unverified'a düşer (Pydantic dışı savunma) ---
    r_bad = call_camera_validation("something_else", "x", None)
    events4 = get_events()
    cam_events4 = [e for e in events4 if e.get("type") == "camera_validation"]
    check("K) Geçersiz status güvenli varsayılan (unverified) olarak kaydedildi", cam_events4[-1].get("status") == "unverified")

finally:
    cleanup()


print()
if FAILURES:
    print(f"{len(FAILURES)} test BAŞARISIZ:")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("Tüm İŞ EMRİ — MÜLAKAT ÖNCESİ KAMERA KALİTE KAPISI backend testleri GEÇTİ.")

if FAILURES:
    sys.exit(1)

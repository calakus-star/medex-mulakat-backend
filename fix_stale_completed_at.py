"""KALEM 4 — bozuk interviews.completed_at düzeltme scripti (TEK SEFERLİK, MANUEL).

SORUN: Bazı eski kayıtlarda completed_at, mülakatın GERÇEK bitiş anını değil, arka plan rapor
işinin (dakikalar/saatler sonra) tamamlandığı anı gösteriyor. Örn: başlangıç 07:10, gerçek
bitiş ~11:42, ama completed_at 21:58 yazıyor.

BU SÜRÜMDEN İTİBAREN otomatik düzeltildi (interview_ended_at kolonu + _mark_finish_pending +
finalize_interview COALESCE). Bu script yalnızca ESKİ kayıtları onarmak için.

completed_at yeni değeri şu sırayla türetilir:
  1) interview_ended_at doluysa onu kullan (yeni kayıtlar).
  2) transkriptin son '[mm:ss]' damgası + started_at  → started_at + mm:ss
  3) realtime_events'teki en büyük elapsed_ms + started_at
  4) hiçbiri yoksa DOKUNMA (report_generated_at'i ayrı bırak).

KULLANIM (çalıştırmadan önce DATABASE_URL export edilmeli, ya da yerel SQLite):
    python fix_stale_completed_at.py --dry-run                # ne değişeceğini göster
    python fix_stale_completed_at.py --candidate 123 --level 2
    python fix_stale_completed_at.py --all --min-drift-min 30 # sürüklenme >30dk olan tüm kayıtlar
NOT: --dry-run olmadan çalıştırmaz; onay için --apply gerekir.
"""
import argparse
import json
import re
import sys
from datetime import timedelta

import main as app  # get_db, _parse_iso, build_transcript_view, _safe_int


def _derive_end(row):
    started = app._parse_iso(row["started_at"]) if row["started_at"] else None
    if not started:
        return None, "started_at yok"
    if row["interview_ended_at"]:
        d = app._parse_iso(row["interview_ended_at"])
        if d:
            return d, "interview_ended_at"
    # transkript son [mm:ss] (dakika 1-3 hane — _VOICE_LINE_RE ile aynı)
    msgs = row["messages"] or "[]"
    try:
        blob = "\n".join((m.get("content") or "") for m in json.loads(msgs) if isinstance(m, dict))
    except Exception:
        blob = msgs if isinstance(msgs, str) else ""
    stamps = [int(a) * 60 + int(b) for a, b in re.findall(r"\[(\d{1,3}):(\d{2})\]", blob)]
    if stamps:
        return started + timedelta(seconds=max(stamps) + 5), "transkript son damgası"
    # realtime_events'teki en büyük elapsed_ms
    try:
        ev = row["_max_elapsed_ms"]
    except Exception:
        ev = None
    if ev and int(ev) > 0:
        return started + timedelta(milliseconds=int(ev) + 5000), "realtime_events son elapsed_ms"
    return None, "türetilemedi"


def main_run():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", type=int)
    ap.add_argument("--level", type=int)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--min-drift-min", type=float, default=20.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if not (args.dry_run or args.apply):
        print("--dry-run veya --apply verin."); sys.exit(2)

    db = app.get_db()
    _base = ("SELECT i.candidate_id, i.level, i.started_at, i.completed_at, i.interview_ended_at, i.messages, "
             "(SELECT MAX(e.elapsed_ms) FROM realtime_events e "
             " WHERE e.candidate_id=i.candidate_id AND e.level=i.level) AS _max_elapsed_ms "
             "FROM interviews i ")
    if args.candidate:
        rows = db.execute(
            _base + "WHERE i.candidate_id=?" + (" AND i.level=?" if args.level else ""),
            (args.candidate, args.level) if args.level else (args.candidate,)).fetchall()
    elif args.all:
        rows = db.execute(
            _base + "WHERE i.completed_at IS NOT NULL AND i.started_at IS NOT NULL").fetchall()
    else:
        print("--candidate ya da --all verin."); sys.exit(2)

    changes = 0
    for r in rows:
        cur = app._parse_iso(r["completed_at"])
        new, src = _derive_end(r)
        if not new or not cur:
            continue
        drift_min = abs((cur - new).total_seconds()) / 60
        if drift_min < args.min_drift_min:
            continue
        print(f"c={r['candidate_id']} L{r['level']}: {cur} -> {new}  (kaynak: {src}, sürüklenme {drift_min:.1f} dk)")
        changes += 1
        if args.apply:
            db.execute("UPDATE interviews SET completed_at=?, interview_ended_at=COALESCE(interview_ended_at, ?) "
                       "WHERE candidate_id=? AND level=?",
                       (new.strftime("%Y-%m-%d %H:%M:%S"), new.strftime("%Y-%m-%d %H:%M:%S"),
                        r["candidate_id"], r["level"]))
    if args.apply:
        db.commit()
        print(f"UYGULANDI: {changes} kayıt güncellendi.")
    else:
        print(f"DRY-RUN: {changes} kayıt değişecekti (uygulamak için --apply).")
    db.close()


if __name__ == "__main__":
    main_run()

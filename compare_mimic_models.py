"""
FAZ D — Mimik analizi model karşılaştırması (kod akışının DIŞINDA, elle çalıştırılır).

Amaç: aynı kare seti + aynı prompt ile claude-sonnet-4-6 (Anthropic) ve gpt-4o (OpenAI)
karşılaştırılır. Ölçülen: çıktı kalitesi (spesifiklik, uydurma bayrağı, zaman damgası
doğruluğu, tekrar tutarlılığı), token maliyeti, gecikme (p50/p95).

Bu betik main.py'yi İÇE AKTARMAZ ve production akışına dokunmaz. Sonuç bir tablo olarak
stdout'a yazılır (ayrıca --json ile dosyaya).

Çalıştırma:
    export ANTHROPIC_API_KEY=...
    export OPENAI_API_KEY=...
    # yerel SQLite (medex_mulakat.db) veya Postgres:
    export DATABASE_URL=postgres://...        # opsiyonel; yoksa ./medex_mulakat.db kullanılır
    python compare_mimic_models.py --limit 5 --repeats 2

Not: 'mimic_sample' karesi yoksa, doğrulama kareleri (en fazla 4) yedek olarak kullanılır.
"""
import os
import re
import io
import csv
import json
import time
import base64
import argparse
import sqlite3
import statistics
from datetime import datetime

import httpx

CLAUDE_MODEL = "claude-sonnet-4-6"
GPT_MODEL = "gpt-4o"

# 1M token başına yaklaşık USD (main.py AI_PRICING_PER_1M ile aynı mantık; sadece bu iki model)
PRICING = {
    CLAUDE_MODEL: {"input": 3.0, "output": 15.0},
    GPT_MODEL: {"input": 2.5, "output": 10.0},
}

# main.py'deki MIMIC_ANALYSIS_PROMPT ile AYNI tutulmalı (elle senkronla).
MIMIC_PROMPT = """Aşağıda bir iş mülakatı sırasında adayın web kamerasından ~45 saniye arayla alınmış kareler var; her karenin öncesinde [t=SANİYE] etiketi bulunur. Bu kareler DÜŞÜK çözünürlüklüdür ve seyrektir.

Görevin: yalnızca karelerde GÖZLENEBİLEN şeyleri, GÖZLEM olarak (teşhis/duygu/kişilik hükmü DEĞİL) yaz. Emin olmadığın hiçbir şeyi yazma. Duygu okuması, IQ, samimiyet/yalan değerlendirmesi YAPMA.

Sadece şu şemada geçerli bir JSON döndür:
{
  "genel_durus": "kısa gözlem (ör. dik oturuş, öne eğik, sık pozisyon değişimi)",
  "goz_temasi_egilimi": "kısa gözlem (ör. genelde kameraya dönük, sık başka yöne bakma) veya 'kestirilemiyor'",
  "belirgin_anlar": [
    {"t_sn": 135, "gozlem": "kısa somut gözlem", "yorum": "gerginlik/rahatlık/nötr — sadece kareye dayanarak"}
  ],
  "genel_izlenim": "1-2 cümle, temkinli",
  "guven": "dusuk | orta"
}
Kare sayısı azsa 'belirgin_anlar' boş kalabilir. Yorum alanı spekülatif olmasın."""

FORBIDDEN = ["iq", "zeka puan", "yalan", "kesin kişilik", "psikiyatr", "depresyon", "anksiyete",
             "kesinlikle mutlu", "kesinlikle üzgün", "duygusal durumu net"]


def get_conn():
    url = os.getenv("DATABASE_URL")
    if url:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(url)
        conn.autocommit = True
        return ("pg", conn)
    path = os.path.join(os.path.dirname(__file__), "medex_mulakat.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return ("sqlite", conn)


def fetch_frame_sets(limit):
    kind, conn = get_conn()
    cur = conn.cursor()
    q_completed = "SELECT candidate_id, level FROM interviews WHERE completed_at IS NOT NULL ORDER BY id DESC"
    cur.execute(q_completed)
    rows = cur.fetchall()
    sets = []
    for r in rows:
        cid = r[0] if kind == "sqlite" else r["candidate_id"]
        lvl = r[1] if kind == "sqlite" else r["level"]
        cur.execute(
            "SELECT image_base64, elapsed_ms, reason FROM snapshots WHERE candidate_id=%s ORDER BY id ASC"
            if kind == "pg" else
            "SELECT image_base64, elapsed_ms, reason FROM snapshots WHERE candidate_id=? ORDER BY id ASC",
            (cid,),
        )
        srows = cur.fetchall()
        mimic = [s for s in srows if (s[2] if kind == "sqlite" else s["reason"]) == "mimic_sample"]
        use = mimic or srows
        frames = []
        for s in use[:24]:
            img = s[0] if kind == "sqlite" else s["image_base64"]
            ems = (s[1] if kind == "sqlite" else s["elapsed_ms"]) or 0
            if img:
                frames.append((img, int(ems)))
        if len(frames) >= 2:
            sets.append({"candidate_id": cid, "level": lvl, "frames": frames, "source": "mimic" if mimic else "verification"})
        if len(sets) >= limit:
            break
    conn.close()
    return sets


def build_content_openai(frames):
    content = [{"type": "text", "text": MIMIC_PROMPT}]
    for img, ems in frames:
        if not img.startswith("data:"):
            img = "data:image/jpeg;base64," + img
        content.append({"type": "text", "text": f"[t={round(ems/1000)}s]"})
        content.append({"type": "image_url", "image_url": {"url": img, "detail": "low"}})
    return content


def build_content_claude(frames):
    blocks = [{"type": "text", "text": MIMIC_PROMPT}]
    for img, ems in frames:
        raw = img.split(",", 1)[1] if img.startswith("data:") else img
        blocks.append({"type": "text", "text": f"[t={round(ems/1000)}s]"})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": raw}})
    return blocks


def call_openai(frames):
    key = os.environ["OPENAI_API_KEY"]
    t0 = time.time()
    with httpx.Client(timeout=120) as c:
        r = c.post("https://api.openai.com/v1/chat/completions",
                   headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                   json={"model": GPT_MODEL, "messages": [{"role": "user", "content": build_content_openai(frames)}],
                         "max_tokens": 1200, "temperature": 0.2, "response_format": {"type": "json_object"}})
    dt = time.time() - t0
    r.raise_for_status()
    d = r.json()
    u = d.get("usage", {})
    return {
        "text": d["choices"][0]["message"]["content"],
        "in_tok": u.get("prompt_tokens", 0), "out_tok": u.get("completion_tokens", 0),
        "latency_s": dt,
    }


def call_claude(frames):
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], timeout=120.0)
    t0 = time.time()
    resp = client.messages.create(
        model=CLAUDE_MODEL, max_tokens=1200,
        messages=[{"role": "user", "content": build_content_claude(frames)}],
    )
    dt = time.time() - t0
    return {
        "text": resp.content[0].text,
        "in_tok": resp.usage.input_tokens, "out_tok": resp.usage.output_tokens,
        "latency_s": dt,
    }


def cost_usd(model, in_tok, out_tok):
    p = PRICING[model]
    return (in_tok * p["input"] + out_tok * p["output"]) / 1_000_000


def score_quality(text, frames):
    """Kaba, deterministik kalite göstergeleri (0-100 değil; ham sayılar + bayraklar)."""
    try:
        obj = json.loads(re.search(r"\{[\s\S]*\}", text).group(0))
    except Exception:
        return {"parse_ok": False}
    body = json.dumps(obj, ensure_ascii=False).lower()
    # spesifiklik: somut gözlem sayısı + toplam kelime
    moments = obj.get("belirgin_anlar") or []
    words = len(re.findall(r"\w+", body))
    # uydurma bayrağı
    forbidden_hits = [w for w in FORBIDDEN if w in body]
    # zaman damgası doğruluğu: t_sn değerleri gözlemlenen aralıkta mı
    max_t = max((round(e / 1000) for _, e in frames), default=0)
    bad_ts = [m.get("t_sn") for m in moments if isinstance(m.get("t_sn"), (int, float)) and (m["t_sn"] < 0 or m["t_sn"] > max_t + 5)]
    return {
        "parse_ok": True,
        "moment_count": len(moments),
        "word_count": words,
        "forbidden_hits": forbidden_hits,
        "bad_timestamps": bad_ts,
        "has_guven": "guven" in obj,
    }


def consistency(texts):
    """İki çalıştırmanın 'belirgin_anlar' t_sn kümesi kesişimi / birleşimi (Jaccard)."""
    sets = []
    for t in texts:
        try:
            obj = json.loads(re.search(r"\{[\s\S]*\}", t).group(0))
            sets.append({m.get("t_sn") for m in (obj.get("belirgin_anlar") or []) if isinstance(m.get("t_sn"), (int, float))})
        except Exception:
            sets.append(set())
    if len(sets) < 2:
        return None
    a, b = sets[0], sets[1]
    if not a and not b:
        return 1.0
    return round(len(a & b) / max(1, len(a | b)), 2)


def run(limit, repeats):
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        if not os.getenv(k):
            raise SystemExit(f"{k} tanımlı değil — bu betik gerçek API çağrısı yapar.")
    sets = fetch_frame_sets(limit)
    if not sets:
        raise SystemExit("Karşılaştırılacak kare seti bulunamadı (tamamlanmış mülakat + >=2 snapshot yok).")

    agg = {CLAUDE_MODEL: {"lat": [], "cost": [], "in": [], "out": [], "moments": [], "forbidden": 0, "bad_ts": 0, "parse_fail": 0, "consistency": []},
           GPT_MODEL:    {"lat": [], "cost": [], "in": [], "out": [], "moments": [], "forbidden": 0, "bad_ts": 0, "parse_fail": 0, "consistency": []}}
    detail_rows = []

    for s in sets:
        frames = s["frames"]
        for model, caller in ((CLAUDE_MODEL, call_claude), (GPT_MODEL, call_openai)):
            texts = []
            for rep in range(repeats):
                try:
                    res = caller(frames)
                except Exception as e:
                    print(f"[HATA] {model} c={s['candidate_id']} rep={rep}: {type(e).__name__}: {e}")
                    continue
                texts.append(res["text"])
                q = score_quality(res["text"], frames)
                c = cost_usd(model, res["in_tok"], res["out_tok"])
                agg[model]["lat"].append(res["latency_s"])
                agg[model]["cost"].append(c)
                agg[model]["in"].append(res["in_tok"])
                agg[model]["out"].append(res["out_tok"])
                if not q.get("parse_ok"):
                    agg[model]["parse_fail"] += 1
                else:
                    agg[model]["moments"].append(q["moment_count"])
                    agg[model]["forbidden"] += len(q["forbidden_hits"])
                    agg[model]["bad_ts"] += len(q["bad_timestamps"])
                detail_rows.append({
                    "candidate_id": s["candidate_id"], "level": s["level"], "source": s["source"],
                    "model": model, "rep": rep, "frames": len(frames),
                    "latency_s": round(res["latency_s"], 2), "in_tok": res["in_tok"], "out_tok": res["out_tok"],
                    "cost_usd": round(c, 5),
                    "moment_count": q.get("moment_count"), "forbidden_hits": q.get("forbidden_hits"),
                    "bad_timestamps": q.get("bad_timestamps"),
                })
            jc = consistency(texts) if repeats >= 2 else None
            if jc is not None:
                agg[model]["consistency"].append(jc)

    def p(xs, q):
        return round(statistics.quantiles(xs, n=100)[q - 1], 3) if len(xs) >= 2 else (round(xs[0], 3) if xs else None)

    def avg(xs):
        return round(sum(xs) / len(xs), 3) if xs else None

    print("\n=== MİMİK MODEL KARŞILAŞTIRMASI ===")
    print(f"kare seti sayısı: {len(sets)} | tekrar: {repeats} | tarih: {datetime.now():%Y-%m-%d %H:%M}\n")
    hdr = ["metrik", CLAUDE_MODEL, GPT_MODEL]
    rows = [
        ["çağrı sayısı", len(agg[CLAUDE_MODEL]["lat"]), len(agg[GPT_MODEL]["lat"])],
        ["gecikme p50 (sn)", p(agg[CLAUDE_MODEL]["lat"], 50), p(agg[GPT_MODEL]["lat"], 50)],
        ["gecikme p95 (sn)", p(agg[CLAUDE_MODEL]["lat"], 95), p(agg[GPT_MODEL]["lat"], 95)],
        ["ort. girdi token", avg(agg[CLAUDE_MODEL]["in"]), avg(agg[GPT_MODEL]["in"])],
        ["ort. çıktı token", avg(agg[CLAUDE_MODEL]["out"]), avg(agg[GPT_MODEL]["out"])],
        ["ort. maliyet / çağrı (USD)", avg(agg[CLAUDE_MODEL]["cost"]), avg(agg[GPT_MODEL]["cost"])],
        ["ort. 'belirgin an' sayısı (spesifiklik)", avg(agg[CLAUDE_MODEL]["moments"]), avg(agg[GPT_MODEL]["moments"])],
        ["yasak sözcük isabeti (uydurma)", agg[CLAUDE_MODEL]["forbidden"], agg[GPT_MODEL]["forbidden"]],
        ["aralık dışı zaman damgası", agg[CLAUDE_MODEL]["bad_ts"], agg[GPT_MODEL]["bad_ts"]],
        ["JSON parse hatası", agg[CLAUDE_MODEL]["parse_fail"], agg[GPT_MODEL]["parse_fail"]],
        ["tekrar tutarlılığı (Jaccard, ort.)", avg(agg[CLAUDE_MODEL]["consistency"]), avg(agg[GPT_MODEL]["consistency"])],
    ]
    w = [max(len(str(r[i])) for r in ([hdr] + rows)) for i in range(3)]
    print(" | ".join(str(hdr[i]).ljust(w[i]) for i in range(3)))
    print("-+-".join("-" * w[i] for i in range(3)))
    for r in rows:
        print(" | ".join(str(r[i]).ljust(w[i]) for i in range(3)))

    with open("mimic_compare_detail.csv", "w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=list(detail_rows[0].keys()))
        wr.writeheader()
        wr.writerows(detail_rows)
    print("\nDetay: mimic_compare_detail.csv")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5, help="karşılaştırılacak kare seti (mülakat) sayısı")
    ap.add_argument("--repeats", type=int, default=2, help="model başına tekrar (tutarlılık için >=2)")
    args = ap.parse_args()
    run(args.limit, args.repeats)

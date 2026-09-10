from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel
from typing import Optional, List, Any
import sqlite3
import psycopg
from psycopg.rows import dict_row
import hashlib
import secrets
import string
import os
import jwt
import anthropic
import httpx
import resend
import asyncio
from datetime import datetime, timedelta
import json
import re
import io
import time
import base64
import traceback
from xml.sax.saxutils import escape as xml_escape

app = FastAPI(title="MedeX Mülakat Sistemi")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============ HATA YÖNETİMİ STANDARDI (Aşama 4, CLAUDE.md'de belgelendi) ============
# Global exception handler: bir route handler kendi try/except'i içinde yakalamadığı HERHANGİ
# bir hatayı burada yakalar. İstemciye ASLA ham hata metni/stack trace dönmez — sadece generic
# bir mesaj + 500. Tam hata (tip + mesaj + stack trace) sunucu logunda kalır. Bu, tek tek her
# endpoint'e try/except yazılmasa bile "sessiz düşme / ham hata sızması" olmamasını garantiler.
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    from fastapi.responses import JSONResponse
    print(f"[UNHANDLED_ERROR] {request.method} {request.url.path}: {type(exc).__name__}: {exc}")
    print(traceback.format_exc())
    return JSONResponse(status_code=500, content={"detail": "Beklenmeyen bir sunucu hatası oluştu."})

# ============ CONFIG ============
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")  # Sesli mod (Whisper STT + TTS, L2 Realtime) için, Anthropic'ten bağımsız
# Realtime model level bazlı ayrılır (L2/L3 farklı modeller kullanabilir). OPENAI_REALTIME_MODEL
# doluysa geriye dönük uyumluluk için her iki level'ın da varsayılanını ezer (eski tek-değişkenli
# deploy'larda davranış değişmesin diye). get_realtime_model() tek okuma noktasıdır — model adı
# başka hiçbir yerde sabit kodlanmaz.
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "")
REALTIME_MODEL_LEVEL2 = os.getenv("REALTIME_MODEL_LEVEL2") or OPENAI_REALTIME_MODEL or "gpt-realtime-2.1-mini"
REALTIME_MODEL_LEVEL3 = os.getenv("REALTIME_MODEL_LEVEL3") or OPENAI_REALTIME_MODEL or "gpt-realtime-2.1"

def get_realtime_model(level: int) -> str:
    return REALTIME_MODEL_LEVEL3 if level == 3 else REALTIME_MODEL_LEVEL2

OPENAI_REALTIME_VOICE = os.getenv("OPENAI_REALTIME_VOICE", "marin")  # Doğal ses: Railway env ile değiştirilebilir (örn. marin/verse)
OPENAI_REPORT_MODEL = os.getenv("OPENAI_REPORT_MODEL", "gpt-4o")  # L2 kaliteli OpenAI raporu; env ile değiştirilebilir
# FAZ D: mimik (yüz/duruş) kare analizi modeli — TEK SABİT, tek satırla değiştirilebilir.
# L2 adayların kareleri için bu model kullanılır (L2'de Anthropic YASAK). L1/L3 de bugün aynı
# modeli kullanır; karşılaştırma sonucu farklı bir model seçilirse SADECE burası değişir.
MIMIC_ANALYSIS_MODEL = os.getenv("MIMIC_ANALYSIS_MODEL", "gpt-4o")
# FAZ D: ortak rapor "muhalif denetçi" modeli. Her zaman OpenAI — Claude-primary (L1/L3-metin)
# için "diğer sağlayıcı", GPT-4o-primary (L2/L3-sesli) için "ikinci OpenAI modeli". L2'de
# Anthropic'e ASLA gidilmez.
OPENAI_REVIEWER_MODEL = os.getenv("OPENAI_REVIEWER_MODEL", "gpt-4.1")

def log_ai_provider(level: int, provider: str, action: str):
    """Görev dokümanı zorunluluğu: L2'de Claude çağrısı yapılmadığını denetlenebilir kılmak için."""
    print(f"[AI_PROVIDER] level=L{level} provider={provider} action={action}")
JWT_SECRET = os.getenv("JWT_SECRET", "medex-secret-key-2024")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@medex-smo.com")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "medex2024")
REPORT_EMAILS = os.getenv("REPORT_EMAILS", "hr@medex-smo.com").split(",")
FROM_EMAIL = os.getenv("FROM_EMAIL", "onboarding@resend.dev")
BASE_URL = os.getenv("BASE_URL", "http://localhost:3000")
# Kritik hata bildirimi gidecek adres(ler). Tanımsızsa rapor adreslerine düşer.
ERROR_ALERT_EMAILS = [e.strip() for e in os.getenv("ERROR_ALERT_EMAILS", ",".join(REPORT_EMAILS)).split(",") if e.strip()]
# Davet linkinin geçerlilik süresi (gün). Aday bu süre içinde hiç giriş yapmazsa "Süresi doldu".
try:
    INVITE_EXPIRY_DAYS = int(os.getenv("INVITE_EXPIRY_DAYS", "14"))
except Exception:
    INVITE_EXPIRY_DAYS = 14

INTERVIEW_TOTAL_MINUTES = 18  # Level bilgisi yoksa/eski kayıtlarda kullanılan varsayılan (geriye uyumluluk)

# Tüm pozisyon/level'larda ortak, sabit ön bilgi metni. Adayın "sistemi anlamadığı için"
# düşük performans göstermesini engellemeyi hedefler.
CANDIDATE_INTRO_TEXT = (
    "Şu an {position} pozisyonu için Level {level} seviyesinde bir mülakata katılıyorsunuz.\n\n"
    "Sorular tek tek gelecek; her soruyu dikkatlice okuyun. Kısa cevap vermekten "
    "çekinmeyin ama mümkünse somut örnek ve detay vermeye çalışın — soruyu ne kadar "
    "net anlar ve açıklarsanız, o kadar doğru değerlendirilirsiniz. Bir soruyu tam "
    "anlamadıysanız kendi ifadenizle yorumlayıp yine de cevap verin, sistem gerekirse "
    "aynı konuyu farklı şekilde tekrar soracaktır."
)
CANDIDATE_INTRO_TEXT_EN = (
    "You are now joining a Level {level} interview for the {position} position.\n\n"
    "Questions will come one at a time; please read each one carefully. Don't worry about "
    "giving a short answer, but try to include concrete examples and detail where possible — "
    "the more clearly you understand and explain a question, the more accurately you will be "
    "evaluated. If a question isn't fully clear, answer with your own interpretation anyway; "
    "the system may rephrase and ask again if needed."
)
CANDIDATE_INTRO_TEXT_DE = (
    "Sie nehmen jetzt an einem Level-{level}-Interview für die Position {position} teil.\n\n"
    "Die Fragen kommen einzeln; lesen Sie jede Frage sorgfältig. Kurze Antworten sind kein "
    "Problem, aber versuchen Sie, konkrete Beispiele und Details zu geben — je klarer Sie eine "
    "Frage verstehen und beantworten, desto genauer werden Sie bewertet. Wenn eine Frage nicht "
    "ganz klar ist, antworten Sie trotzdem mit Ihrer eigenen Interpretation; das System kann "
    "das Thema bei Bedarf anders formulieren."
)
INTRO_TEXT_BY_LANG = {"tr": CANDIDATE_INTRO_TEXT, "en": CANDIDATE_INTRO_TEXT_EN, "de": CANDIDATE_INTRO_TEXT_DE}

def get_intro_text(position: str, level: int, interview_language: str = "tr") -> str:
    template = INTRO_TEXT_BY_LANG.get(interview_language, CANDIDATE_INTRO_TEXT)
    return template.format(position=position, level=level)

resend.api_key = RESEND_API_KEY
security = HTTPBearer()

# ============ DB ============
# DATABASE_URL varsa Railway PostgreSQL kullanılır. Yoksa yerel geliştirme/geri dönüş için SQLite devam eder.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_PATH = os.getenv("DB_PATH", "medex_mulakat.db")
USE_POSTGRES = bool(DATABASE_URL)


def _pg_sql(sql: str) -> str:
    """Uygulamadaki SQLite tarzı sorguları PostgreSQL sözdizimine güvenli biçimde uyarlar."""
    sql = sql.replace("?", "%s")
    sql = sql.replace("ORDER BY datetime(created_at)", "ORDER BY created_at")
    if "INSERT OR IGNORE INTO positions" in sql:
        sql = sql.replace("INSERT OR IGNORE INTO positions", "INSERT INTO positions")
        sql = sql.rstrip().rstrip(";") + " ON CONFLICT (org_id, name) DO NOTHING"
    return sql


class PostgresConnection:
    """sqlite3.Connection ile aynı temel arayüzü sağlayan küçük PostgreSQL adaptörü."""
    is_postgres = True

    def __init__(self, url: str):
        self.conn = psycopg.connect(url, row_factory=dict_row)

    def execute(self, sql: str, params=()):
        cur = self.conn.cursor()
        cur.execute(_pg_sql(sql), params or ())
        return cur

    def executescript(self, script: str):
        cur = self.conn.cursor()
        cur.execute(script)
        return cur

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()


def get_db():
    if USE_POSTGRES:
        return PostgresConnection(DATABASE_URL)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def db_dep():
    """Route handler'lar için tercih edilen DB erişimi: `db=Depends(db_dep)`. Hata durumunda
    rollback, HER durumda (başarı/hata) close — bu iki satırı 40'tan fazla endpoint'e tek tek
    kopyalamak yerine tek yerde. Endpoint hâlâ kendi db.commit()'ini kendisi çağırır; burası
    sadece açılış/rollback/kapanışı üstleniyor. Not: bazı yardımcı fonksiyonlar (find_or_create_person,
    run_deferred_finish_job, vb.) route handler değildir ve kendi get_db()/try-except'ini kullanmaya
    devam eder — bu dependency sadece @app.* handler'lar içindir."""
    db = get_db()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    conn = get_db()
    if USE_POSTGRES:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS positions (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                category TEXT DEFAULT 'Genel',
                role_description TEXT,
                criteria_json TEXT NOT NULL,
                active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS candidates (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT,
                phone TEXT,
                education TEXT,
                university TEXT,
                department TEXT,
                experience_years INTEGER DEFAULT 0,
                ai_note TEXT,
                position TEXT NOT NULL,
                level INTEGER DEFAULT 1,
                depth_tier TEXT DEFAULT 'standart',
                interview_language TEXT DEFAULT 'tr',
                report_language TEXT DEFAULT 'tr',
                username TEXT UNIQUE,
                password_hash TEXT,
                plain_password TEXT,
                invite_type TEXT DEFAULT 'invite',
                cv_text TEXT,
                cv_filename TEXT,
                reapply_allowed INTEGER DEFAULT 0,
                previous_candidate_id BIGINT,
                is_archived INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                violation_count INTEGER DEFAULT 0,
                terminated_reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS interviews (
                id BIGSERIAL PRIMARY KEY,
                candidate_id BIGINT,
                level INTEGER DEFAULT 1,
                closing_asked INTEGER DEFAULT 0,
                total_input_tokens INTEGER DEFAULT 0,
                total_output_tokens INTEGER DEFAULT 0,
                messages TEXT DEFAULT '[]',
                report TEXT,
                standard_cv TEXT,
                score INTEGER,
                recommendation TEXT,
                compact_memory TEXT DEFAULT '',
                question_count INTEGER DEFAULT 0,
                depth_tier TEXT DEFAULT 'standart',
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                processing_status TEXT,
                processing_error TEXT,
                processing_started_at TIMESTAMP,
                pending_finish_reason TEXT,
                pending_finish_provider TEXT,
                pending_finish_model TEXT,
                pending_finish_system TEXT,
                pending_finish_payload TEXT,
                pending_finish_terminated_reason TEXT,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                id BIGSERIAL PRIMARY KEY,
                candidate_id BIGINT,
                image_base64 TEXT,
                captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS ai_usage_logs (
                id BIGSERIAL PRIMARY KEY,
                candidate_id BIGINT,
                level INTEGER DEFAULT 1,
                provider TEXT,
                model TEXT,
                action TEXT,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                audio_input_tokens INTEGER DEFAULT 0,
                audio_output_tokens INTEGER DEFAULT 0,
                cached_input_tokens INTEGER DEFAULT 0,
                cached_audio_input_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                estimated_cost_usd DOUBLE PRECISION DEFAULT 0,
                raw_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
            );

            -- Faz D1: ham Realtime olayları (session.created, speech_started/stopped, truncation,
            -- response.done). Sadece toplama/kayıt — metrik hesaplama Faz D2'nin işi.
            CREATE TABLE IF NOT EXISTS realtime_events (
                id BIGSERIAL PRIMARY KEY,
                candidate_id BIGINT,
                level INTEGER DEFAULT 1,
                event_type TEXT,
                event_data TEXT,
                elapsed_ms INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_realtime_events_candidate ON realtime_events (candidate_id, level);

            CREATE TABLE IF NOT EXISTS organizations (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS admin_users (
                id BIGSERIAL PRIMARY KEY,
                org_id BIGINT,
                name TEXT,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'org_admin',
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS persons (
                id BIGSERIAL PRIMARY KEY,
                org_id BIGINT,
                full_name TEXT,
                email TEXT,
                phone TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS person_notes (
                id BIGSERIAL PRIMARY KEY,
                person_id BIGINT,
                org_id BIGINT,
                admin_user_id BIGINT,
                note_type TEXT DEFAULT 'manual',
                body TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (person_id) REFERENCES persons(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS error_logs (
                id BIGSERIAL PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                candidate_id BIGINT, candidate_name TEXT, interview_id BIGINT, level INTEGER,
                provider TEXT, step TEXT, error_class TEXT, severity TEXT DEFAULT 'user',
                human_message TEXT, candidate_message TEXT, technical_detail TEXT,
                retry_count INTEGER DEFAULT 0, email_sent_at TIMESTAMP,
                resolved INTEGER DEFAULT 0, resolved_at TIMESTAMP, resolved_by TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_error_logs_unresolved ON error_logs (resolved, severity, error_class);

            CREATE INDEX IF NOT EXISTS idx_candidates_email ON candidates (lower(email));
            CREATE INDEX IF NOT EXISTS idx_interviews_candidate_level ON interviews (candidate_id, level);
            CREATE INDEX IF NOT EXISTS idx_snapshots_candidate ON snapshots (candidate_id);
            CREATE INDEX IF NOT EXISTS idx_usage_candidate_level ON ai_usage_logs (candidate_id, level);
            CREATE INDEX IF NOT EXISTS idx_admin_users_org ON admin_users (org_id);
            CREATE INDEX IF NOT EXISTS idx_persons_org_email ON persons (org_id, lower(email));
            CREATE INDEX IF NOT EXISTS idx_persons_org_phone ON persons (org_id, phone);
        """)
    else:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                category TEXT DEFAULT 'Genel',
                role_description TEXT,
                criteria_json TEXT NOT NULL,
                active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, email TEXT, phone TEXT, education TEXT, university TEXT, department TEXT,
                experience_years INTEGER DEFAULT 0, ai_note TEXT, position TEXT NOT NULL, level INTEGER DEFAULT 1,
                depth_tier TEXT DEFAULT 'standart', interview_language TEXT DEFAULT 'tr', report_language TEXT DEFAULT 'tr',
                username TEXT UNIQUE, password_hash TEXT, plain_password TEXT, invite_type TEXT DEFAULT 'invite',
                cv_text TEXT, cv_filename TEXT, reapply_allowed INTEGER DEFAULT 0, previous_candidate_id INTEGER,
                is_archived INTEGER DEFAULT 0, status TEXT DEFAULT 'pending', violation_count INTEGER DEFAULT 0,
                terminated_reason TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS interviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id INTEGER, level INTEGER DEFAULT 1,
                closing_asked INTEGER DEFAULT 0, total_input_tokens INTEGER DEFAULT 0, total_output_tokens INTEGER DEFAULT 0,
                messages TEXT DEFAULT '[]', report TEXT, standard_cv TEXT, score INTEGER, recommendation TEXT,
                compact_memory TEXT DEFAULT '', question_count INTEGER DEFAULT 0, depth_tier TEXT DEFAULT 'standart',
                started_at TEXT DEFAULT CURRENT_TIMESTAMP, completed_at TEXT,
                processing_status TEXT, processing_error TEXT, processing_started_at TEXT, pending_finish_reason TEXT,
                pending_finish_provider TEXT, pending_finish_model TEXT, pending_finish_system TEXT,
                pending_finish_payload TEXT, pending_finish_terminated_reason TEXT,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id INTEGER, image_base64 TEXT,
                captured_at TEXT DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS ai_usage_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id INTEGER, level INTEGER DEFAULT 1,
                provider TEXT, model TEXT, action TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
                audio_input_tokens INTEGER DEFAULT 0, audio_output_tokens INTEGER DEFAULT 0,
                cached_input_tokens INTEGER DEFAULT 0, cached_audio_input_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                estimated_cost_usd REAL DEFAULT 0, raw_json TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS realtime_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id INTEGER, level INTEGER DEFAULT 1,
                event_type TEXT, event_data TEXT, elapsed_ms INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_realtime_events_candidate ON realtime_events (candidate_id, level);
            CREATE TABLE IF NOT EXISTS organizations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE, is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS admin_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                org_id INTEGER, name TEXT, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'org_admin', is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS persons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                org_id INTEGER, full_name TEXT, email TEXT, phone TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS person_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id INTEGER, org_id INTEGER, admin_user_id INTEGER,
                note_type TEXT DEFAULT 'manual', body TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (person_id) REFERENCES persons(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS error_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                candidate_id INTEGER, candidate_name TEXT, interview_id INTEGER, level INTEGER,
                provider TEXT, step TEXT, error_class TEXT, severity TEXT DEFAULT 'user',
                human_message TEXT, candidate_message TEXT, technical_detail TEXT,
                retry_count INTEGER DEFAULT 0, email_sent_at TEXT,
                resolved INTEGER DEFAULT 0, resolved_at TEXT, resolved_by TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_error_logs_unresolved ON error_logs (resolved, severity, error_class);
        """)
    conn.commit()

    migrations = [
        ("candidates", "email", "TEXT"), ("candidates", "phone", "TEXT"),
        ("candidates", "education", "TEXT"), ("candidates", "university", "TEXT"),
        ("candidates", "department", "TEXT"), ("candidates", "experience_years", "INTEGER DEFAULT 0"),
        ("candidates", "ai_note", "TEXT"), ("candidates", "plain_password", "TEXT"),
        ("candidates", "cv_text", "TEXT"), ("candidates", "cv_filename", "TEXT"),
        ("candidates", "violation_count", "INTEGER DEFAULT 0"), ("candidates", "terminated_reason", "TEXT"),
        ("candidates", "reapply_allowed", "INTEGER DEFAULT 0"), ("candidates", "previous_candidate_id", "BIGINT" if USE_POSTGRES else "INTEGER"),
        ("candidates", "is_archived", "INTEGER DEFAULT 0"), ("candidates", "level", "INTEGER DEFAULT 1"),
        ("candidates", "depth_tier", "TEXT DEFAULT 'standart'"),
        ("candidates", "interview_language", "TEXT DEFAULT 'tr'"),
        ("candidates", "report_language", "TEXT DEFAULT 'tr'"),
        ("interviews", "level", "INTEGER DEFAULT 1"), ("interviews", "closing_asked", "INTEGER DEFAULT 0"),
        ("interviews", "total_input_tokens", "INTEGER DEFAULT 0"),
        ("interviews", "total_output_tokens", "INTEGER DEFAULT 0"),
        ("positions", "category", "TEXT DEFAULT 'Genel'"), ("interviews", "standard_cv", "TEXT"),
        ("interviews", "compact_memory", "TEXT DEFAULT ''"),
        ("interviews", "question_count", "INTEGER DEFAULT 0"),
        ("interviews", "depth_tier", "TEXT DEFAULT 'standart'"),
        ("interviews", "processing_status", "TEXT"),
        ("interviews", "processing_error", "TEXT"),
        ("interviews", "processing_started_at", "TIMESTAMP" if USE_POSTGRES else "TEXT"),
        ("interviews", "pending_finish_reason", "TEXT"),
        ("interviews", "pending_finish_provider", "TEXT"),
        ("interviews", "pending_finish_model", "TEXT"),
        ("interviews", "pending_finish_system", "TEXT"),
        ("interviews", "pending_finish_payload", "TEXT"),
        ("interviews", "pending_finish_terminated_reason", "TEXT"),
        ("ai_usage_logs", "estimated_cost_usd", "DOUBLE PRECISION DEFAULT 0" if USE_POSTGRES else "REAL DEFAULT 0"),
        ("ai_usage_logs", "cached_input_tokens", "INTEGER DEFAULT 0"),
        ("ai_usage_logs", "cached_audio_input_tokens", "INTEGER DEFAULT 0"),
        # FAZ D: mimik analizi + ses metrikleri + ses gözlemleri
        ("snapshots", "level", "INTEGER DEFAULT 1"),
        ("snapshots", "elapsed_ms", "BIGINT" if USE_POSTGRES else "INTEGER"),
        ("snapshots", "reason", "TEXT"),
        ("interviews", "mimic_analysis_json", "TEXT"),
        ("interviews", "voice_metrics_json", "TEXT"),
        ("interviews", "voice_observations_json", "TEXT"),
        ("interviews", "reviewer_status", "TEXT"),  # FAZ D: ok | skipped | failed
        ("interviews", "reviewer_error", "TEXT"),   # FAZ D: kısa hata/atlama nedeni
        # BÖLÜM 3: ihlal / başarısızlık / kısmi mülakat gerekçesi (yapılandırılmış)
        ("interviews", "result_events_json", "TEXT"),   # [{type, subtype, occurred_at, elapsed_ms, description, snapshot_id, weight, source}]
        ("interviews", "result_reason", "TEXT"),        # insana yönelik konsolide gerekçe
        ("interviews", "partial", "INTEGER DEFAULT 0"), # kısmi mülakat mı
        ("interviews", "completion_pct", "INTEGER"),    # ~tamamlanma oranı (nullable)
        ("interviews", "technical_error_ref", "TEXT"),  # "Değerlendirilemedi" teknik sebep + log kimliği (admin-only)
        # Aday teşebbüs izleme + davet süresi (Mülakat Denemeleri ekranı)
        ("candidates", "login_count", "INTEGER DEFAULT 0"),
        ("candidates", "first_login_at", "TIMESTAMP" if USE_POSTGRES else "TEXT"),
        ("candidates", "last_login_at", "TIMESTAMP" if USE_POSTGRES else "TEXT"),
        ("candidates", "interview_start_count", "INTEGER DEFAULT 0"),
        ("candidates", "last_start_at", "TIMESTAMP" if USE_POSTGRES else "TEXT"),
        ("candidates", "invite_expires_at", "TIMESTAMP" if USE_POSTGRES else "TEXT"),
        # BÖLÜM A/B/D: kapsama + kriter tekrar sayacı + sistem kararı gerekçesi
        ("interviews", "score_position", "INTEGER"),       # ÇİFT PUANLAMA: PUAN 1 — pozisyon uygunluğu (karar bunun üzerinden)
        ("interviews", "score_profile", "INTEGER"),         # ÇİFT PUANLAMA: PUAN 2 — kişisel/bilişsel profil (eşik/gözlem); score = ikisinin ortalaması
        ("interviews", "criteria_coverage_json", "TEXT"),   # A4: modelin end_interview'da bildirdiği kriter kapsanma yüzdeleri
        ("interviews", "criterion_attempts_json", "TEXT"),  # B2: kriter/konu başına yeniden-sorma sayacı (L1 metin akışı)
        ("interviews", "system_decision_json", "TEXT"),     # D1: finalize kararı + gerekçe (rapor üretildi mi / neden / hangi koşul)
        ("interviews", "report_regenerated_at", "TIMESTAMP" if USE_POSTGRES else "TEXT"),  # EK: geriye dönük rapor üretim zamanı (completed_at ayrı korunur)
        # KALEM 4 — zaman damgası ayrımı: mülakatın GERÇEK bitiş anı (aday tarafında) completed_at'ten
        # ayrı tutulur; rapor üretimi (arka planda, dakikalar/saatler sonra olabilir) completed_at'i ASLA ezmez.
        ("interviews", "interview_ended_at", "TIMESTAMP" if USE_POSTGRES else "TEXT"),  # aday mülakatı fiilen bitirdiği an
        ("interviews", "report_generated_at", "TIMESTAMP" if USE_POSTGRES else "TEXT"),  # ilk rapor üretiminin bittiği an
        ("interviews", "report_tech_note", "TEXT"),  # KALEM 5 — yalnız yönetici: token kesilmesi vb. teknik notlar (müşteri raporuna girmez)
        ("interviews", "reviewer_score_revision_json", "TEXT"),  # KALEM 9 — ikinci modelin tetiklediği kriter puan revizyonları
        ("interviews", "reviewer_summary_tone", "TEXT"),  # denetçinin Yönetici Özeti sonuç tonu değerlendirmesi (OLUMLU/NOTR/OLUMSUZ)
        ("interviews", "raw_report", "TEXT"),  # rapor üreten model çağrısının İŞLENMEMİŞ çıktısı (yalnız admin panel; ≤20000 kr)
        ("candidates", "person_id", "BIGINT" if USE_POSTGRES else "INTEGER"),
        ("candidates", "org_id", "BIGINT" if USE_POSTGRES else "INTEGER"),
        ("positions", "org_id", "BIGINT" if USE_POSTGRES else "INTEGER"),
        # B7 — panelden düzenlenmiş pozisyonlar deploy'da (init_db forced-update) EZİLMESİN.
        ("positions", "is_customized", "INTEGER DEFAULT 0"),
    ]
    for table, column, definition in migrations:
        try:
            if USE_POSTGRES:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")
            else:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            conn.commit()
        except Exception as e:
            # Beklenen durum: kolon zaten var (idempotent migration). Ama gerçek bir DB
            # hatasını da sessizce yutmamak için logluyoruz.
            print(f"[MIGRATION] {table}.{column} eklenemedi (muhtemelen zaten var): {type(e).__name__}: {e}")
            conn.rollback()
    conn.commit()

    # ---- Multi-tenant temel seed + pozisyon benzersizlik göçü ----
    # hash_password() bu noktada henüz tanımlı değil (init_db() modül yüklenirken
    # çağrılıyor), bu yüzden hashlib doğrudan kullanılıyor.
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)")
    conn.commit()

    org_row = conn.execute("SELECT id FROM organizations WHERE slug=?", ("medex",)).fetchone()
    if not org_row:
        conn.execute("INSERT INTO organizations (name, slug) VALUES (?, ?)", ("MedeX", "medex"))
        conn.commit()
    admin_count = conn.execute("SELECT COUNT(*) AS c FROM admin_users").fetchone()["c"]
    if not admin_count:
        conn.execute(
            "INSERT INTO admin_users (org_id, name, email, password_hash, role) VALUES (?, ?, ?, ?, ?)",
            (None, "MedeX Süperadmin", ADMIN_EMAIL, hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest(), "superadmin")
        )
        conn.commit()
    medex_org_id = conn.execute("SELECT id FROM organizations WHERE slug=?", ("medex",)).fetchone()["id"]

    # Her kurumun kendi bağımsız pozisyon kopyasını alabilmesi için positions.name'in eski
    # global UNIQUE kısıtı (org_id, name) bileşik benzersizliğine taşınır. Tek seferlik ve
    # idempotent — schema_migrations işaretiyle korunur (SQLite ALTER TABLE ile constraint
    # değiştiremediği için tablo yeniden kurulur; Postgres'te doğrudan constraint değişir).
    already_migrated = conn.execute("SELECT 1 FROM schema_migrations WHERE name=?", ("positions_org_unique",)).fetchone()
    if not already_migrated:
        if USE_POSTGRES:
            try:
                conn.execute("ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_name_key")
            except Exception as e:
                print(f"[MIGRATION] positions_name_key düşürülemedi: {type(e).__name__}: {e}")
                conn.rollback()
            try:
                conn.execute("ALTER TABLE positions ADD CONSTRAINT positions_org_name_key UNIQUE (org_id, name)")
            except Exception as e:
                print(f"[MIGRATION] positions_org_name_key eklenemedi (muhtemelen zaten var): {type(e).__name__}: {e}")
                conn.rollback()
        else:
            conn.executescript("""
                CREATE TABLE positions_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    category TEXT DEFAULT 'Genel',
                    role_description TEXT,
                    criteria_json TEXT NOT NULL,
                    active INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    org_id INTEGER,
                    is_customized INTEGER DEFAULT 0,
                    UNIQUE(org_id, name)
                );
                INSERT INTO positions_new (id, name, category, role_description, criteria_json, active, created_at, org_id, is_customized)
                    SELECT id, name, category, role_description, criteria_json, active, created_at, org_id,
                           COALESCE(is_customized, 0) FROM positions;
                DROP TABLE positions;
                ALTER TABLE positions_new RENAME TO positions;
            """)
        conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", ("positions_org_unique",))
        conn.commit()

    # Mevcut hazır kataloğu (henüz org_id'siz kalmış olabilecek eski satırlar) MedeX'e bağlar.
    conn.execute("UPDATE positions SET org_id=? WHERE org_id IS NULL", (medex_org_id,))
    conn.commit()

    def infer_position_category(name: str) -> str:
        # V14.5'teki özgün kategori mantığı aynen korunur.
        n = name.lower()
        if any(k in n for k in ["study", "coordinator", "cra", "cta", "ctm", "clinical", "site", "trial", "operations"]):
            return "Klinik Araştırma"
        if any(k in n for k in ["medical", "msl", "pharmacovigilance", "regulatory"]):
            return "Medikal / Regülasyon"
        if any(k in n for k in ["data", "biostat", "statistic"]):
            return "Veri Yönetimi"
        if any(k in n for k in ["quality", "gcp", "qa", "qc"]):
            return "Kalite"
        if any(k in n for k in ["laboratory", "lab", "scientist"]):
            return "Laboratuvar"
        if any(k in n for k in ["software", "developer", "backend", "frontend", "full stack", "devops", "cto", "product manager", "business analyst", "engineer"]):
            return "Bilgi Teknolojileri"
        if any(k in n for k in ["hr", "recruiter", "human"]):
            return "İnsan Kaynakları"
        if any(k in n for k in ["finance", "accountant", "accounting", "muhasebe"]):
            return "Finans"
        if any(k in n for k in ["sales", "marketing", "customer", "product specialist", "representative"]):
            return "Satış & Pazarlama"
        if any(k in n for k in ["kasa", "kasiyer", "retail", "mağaza", "magaza"]):
            return "Perakende & Operasyon"
        return "Genel"
    defaults = [
        ("Study Coordinator (SC)",
         "Klinik araştırma merkezinde hasta ziyareti, visit takvimi, CRF/EDC ve saha koordinasyonunu yürüten rol.",
         [("Organizasyon & Takip",25,"Visit takvimini (screening/randomizasyon/follow-up) CTMS veya Excel ile planlama, visit window (ör. ±3 gün) ve deadline takibi"),("Dikkat & Doğruluk",25,"EDC sistemine (ör. Medidata Rave, Oracle InForm) veri girişinde kaynak veri (source document) ile CRF tutarlılığı, düşük query oranı"),("Hasta ve Ekip İletişimi",20,"Informed consent (ICF) sürecinde anlaşılır iletişim; PI, monitör ve laboratuvar ekibiyle visit koordinasyonu"),("Stres Toleransı",15,"Aynı gün birden fazla visit veya acil SAE bildirimi gibi durumlarda düzenli kalma"),("Gizlilik & Etik",10,"KVKK ve ICH-GCP kapsamında hasta verisi gizliliği ve informed consent bilinci"),("Öğrenme Esnekliği",5,"Yeni protokol amendmanı veya yeni EDC sistemine hızlı uyum")]),
        ("Senior Study Coordinator",
         "Deneyimli saha koordinatörü; junior ekibi yönlendirir, kompleks çalışmaları ve monitör ziyaretlerini yönetir.",
         [("Klinik Operasyon Deneyimi",25,"Faz II-III çok merkezli çalışmalarda saha süreç deneyimi, protokol karmaşıklığına hakimiyet"),("Ekip Koordinasyonu",20,"Junior SC'lere görev dağılımı, visit planı yönlendirme ve kalite kontrolü"),("EDC/CRF Kalitesi",20,"Query oranını azaltma, SDV (source data verification) hazırlığı (ör. Medidata Rave, Veeva)"),("Regülasyon & GCP",15,"ICH-GCP E6(R2), KVKK, informed consent ve etik kurul gereklilikleri"),("Problem Çözme",15,"Protokol deviasyonu, randevu kaçırma, lojistik aksaklık durumlarında hızlı çözüm"),("İletişim",5,"Sponsor/CRO/monitör ile SIV, IMV ve query çözüm süreçlerinde net iletişim")]),
        ("Clinical Research Associate (CRA)",
         "Klinik araştırma sahalarını monitör eden, GCP uyumunu ve kaynak veri doğrulamasını takip eden rol.",
         [("GCP & Protokol Bilgisi",25,"ICH-GCP E6(R2), protokol, ICF, SDV/SDR süreçlerine hakimiyet"),("Monitoring Deneyimi",25,"SIV (site initiation), IMV (interim monitoring) ve COV (close-out) yürütme, follow-up letter yazımı"),("Problem Çözme",20,"Protokol deviasyonu, query ve CAPA aksiyon planı oluşturma"),("İletişim & Raporlama",15,"Site/sponsor/CRO ile visit report ve follow-up letter kalitesi"),("Seyahat & Planlama",10,"Çoklu saha ziyaret planı ve zaman/rota yönetimi"),("Teknik Sistemler",5,"EDC (Medidata Rave, Veeva), CTMS, eTMF kullanımı")]),
        ("Senior CRA",
         "Kompleks çalışmalarda deneyimli monitör; saha kalitesi, risk yönetimi ve junior CRA mentörlüğü yapar.",
         [("İleri GCP & Risk Bazlı Monitoring",25,"RBM (risk-based monitoring), CAPA yönetimi ve audit readiness hazırlığı"),("Kompleks Saha Deneyimi",25,"Faz I-IV, çok terapötik alan ve çok merkezli (multi-site) çalışma deneyimi"),("Mentörlük",15,"Junior CRA'lara SIV/IMV eşlik etme, kalite kontrolü ve geri bildirim"),("Raporlama Kalitesi",15,"Zamanında ve aksiyon odaklı visit/trip report yazımı"),("Kriz Yönetimi",15,"SAE bildirimi, protokol deviasyonu ve hasta güvenliği acil durumlarında hızlı eskalasyon"),("İlişki Yönetimi",5,"KOL, PI ve sponsor ile uzun vadeli ilişki yönetimi")]),
        ("Clinical Trial Assistant (CTA)",
         "Klinik araştırmalarda dokümantasyon, eTMF, takip listeleri ve operasyonel destek rolü.",
         [("Dokümantasyon Düzeni",25,"TMF/eTMF (ör. Veeva Vault) dosyalama, ISF (investigator site file) versiyon takibi"),("Takip & Organizasyon",25,"Checklist, deadline ve aksiyon takibini Excel/CTMS ile yönetme"),("Dikkat & Doğruluk",20,"Doküman tamlığı, imza/tarih kontrolü ve veri doğruluğu"),("İletişim",15,"CRA/PM/site ile doküman eksikliği ve aksiyon koordinasyonu"),("Teknik Araçlar",10,"Excel, CTMS, eTMF sistemlerini etkin kullanma"),("Öğrenme Hızı",5,"GCP/regülatif terminoloji ve yeni sistemlere hızlı uyum")]),
        ("Clinical Trial Manager (CTM)",
         "Çalışma operasyonlarını uçtan uca yöneten, timeline, bütçe, site performansı ve riskleri takip eden rol.",
         [("Proje Yönetimi",25,"Timeline, milestone ve risk planını MS Project/Excel ile yönetme, kaynak planlaması"),("Klinik Operasyon Bilgisi",25,"Site activation, enrollment takibi ve close-out süreçlerine uçtan uca hakimiyet"),("Ekip Yönetimi",20,"CRA/CTA/saha ekiplerinin görev dağılımı ve performans takibi"),("Risk & CAPA",15,"Risk tespiti, kök neden analizi ve CAPA aksiyon planı oluşturma"),("Sponsor İletişimi",10,"Sponsora beklenti yönetimi, status raporlama ve eskalasyon"),("Finansal Farkındalık",5,"Bütçe, vendor sözleşmesi ve maliyet takibi")]),
        ("Clinical Project Manager",
         "Klinik araştırma projelerini sponsor beklentileri, bütçe, kalite ve zaman çizelgesi içinde yöneten rol.",
         [("Proje Planlama",25,"Kapsam, timeline, bütçe ve risk planını uçtan uca kurma"),("Stakeholder Yönetimi",20,"Sponsor, vendor, saha ve iç ekiple düzenli iletişim ve beklenti yönetimi"),("Klinik Araştırma Süreçleri",20,"Study startup, operasyon ve close-out süreçlerine ve kalite kontrolüne hakimiyet"),("Liderlik",15,"Ekip yönlendirme, önceliklendirme ve karar alma"),("Raporlama",10,"KPI, milestone ve yönetim raporları hazırlama"),("Problem Çözme",10,"Eskalasyon yönetimi ve kriz anında hızlı karar alma")]),
        ("Clinical Operations Manager",
         "Klinik operasyon ekibini, süreçleri, kalite metriklerini ve kaynak planlamasını yöneten rol.",
         [("Operasyonel Liderlik",25,"Ekip kapasitesi, süreç ve kaynak planlamasını yönetme"),("Kalite & KPI",20,"Enrollment rate, query rate gibi performans metrikleri ve audit readiness takibi"),("Süreç İyileştirme",20,"SOP güncelleme, standardizasyon ve verimlilik projeleri yürütme"),("Regülasyon & GCP",15,"ICH-GCP, yerel mevzuat (TİTCK) ve etik kurul süreçlerine hakimiyet"),("Bütçe & Kaynak",10,"Kaynak planlama, vendor yönetimi ve maliyet kontrolü"),("İletişim",10,"Üst yönetim ve sponsor ile stratejik raporlama")]),
        ("Data Manager",
         "Klinik veri yönetimi, veri temizliği, edit check, query ve database lock süreçlerini yöneten rol.",
         [("Clinical Data Management",30,"EDC (Medidata Rave, Oracle InForm) üzerinde edit check, query yönetimi ve database lock süreçleri"),("Dikkat & Analitik",20,"Veri tutarlılığı, pattern/anomali tespiti ve hata yakalama"),("Sistem Yetkinliği",15,"EDC ve SAS/Excel gibi veri araçlarını etkin kullanma"),("Regülasyon & GCP",15,"ALCOA+ ilkeleri, audit trail ve veri bütünlüğü standartlarına hakimiyet"),("İletişim",10,"CRA, saha ve biyoistatistik ekipleriyle veri sorunları üzerine koordinasyon"),("Problem Çözme",10,"Data discrepancy ve query çözüm sürecini yönetme")]),
        ("Clinical Data Coordinator",
         "Veri giriş kontrolleri, query takibi ve data management süreçlerine operasyonel destek veren rol.",
         [("Veri Dikkati",30,"CRF/EDC girişinde hata yakalama ve çift kontrol (double-check) alışkanlığı"),("EDC Kullanımı",20,"Query oluşturma/çözme, form doldurma ve takip (ör. Medidata Rave)"),("Organizasyon",15,"Query listesi ve deadline takibini sistematik yönetme"),("GCP & Veri Bütünlüğü",15,"ALCOA+ ve audit trail farkındalığı"),("İletişim",10,"Saha, CRA ve Data Manager ile query çözüm iletişimi"),("Öğrenme",10,"Yeni EDC modülü veya çalışmaya hızlı uyum")]),
        ("Medical Monitor",
         "Klinik çalışmalarda tıbbi güvenlik, uygunluk ve vaka değerlendirmesi yapan hekim rolü.",
         [("Tıbbi Değerlendirme",30,"AE/SAE causality assessment, hasta uygunluk (eligibility) değerlendirmesi"),("Protokol & Klinik Bilgi",20,"Terapötik alan literatürü ve protokol gerekliliklerine hakimiyet"),("GCP & Etik",15,"Hasta güvenliği, informed consent ve regülasyon bilinci"),("Karar Verme",15,"Risk-fayda analizi, eskalasyon kararı ve medikal karar gerekçelendirme"),("İletişim",10,"PI, sponsor ve farmakovijilans ekipleriyle vaka bazlı iletişim"),("Raporlama",10,"Medikal yorum ve vaka değerlendirme dokümantasyonu")]),
        ("Medical Advisor",
         "Medikal strateji, bilimsel içerik, KOL iletişimi ve klinik yorum sağlayan rol.",
         [("Bilimsel Yetkinlik",25,"Literatür taraması, terapötik alan bilgisi ve klinik veri yorumlama"),("Stratejik Düşünme",20,"Medikal plan, ürün pozisyonlama ve yaşam döngüsü stratejisi kurma"),("KOL İletişimi",15,"Key opinion leader'larla bilimsel ilişki kurma ve sunum yapma"),("Regülasyon & Etik",15,"Tanıtım dışı medikal iletişim kuralları ve uyum bilinci"),("Analitik Raporlama",15,"Klinik/pazar verisini yorumlayıp içgörüye dönüştürme"),("Ekip Çalışması",10,"Pazarlama, klinik ve farmakovijilans ekipleriyle iş birliği")]),
        ("Pharmacovigilance Specialist",
         "AE/SAE, ICSR, sinyal, güvenlilik raporlaması ve farmakovijilans uyumundan sorumlu rol.",
         [("PV Süreç Bilgisi",30,"ICSR, SAE, SUSAR bildirim süreçleri ve zaman çizelgelerine (ör. 7/15 günlük bildirim) hakimiyet"),("Regülasyon",20,"Yerel (TİTCK) ve uluslararası (EMA, FDA) PV yükümlülükleri"),("Dikkat & Doğruluk",20,"MedDRA kodlama, veri kalitesi ve raporlama doğruluğu"),("Tıbbi Terminoloji",10,"AE/SAE terminolojisi ve klinik yorum yapabilme"),("Sistem Kullanımı",10,"PV veritabanı (ör. ArisG, Argus) ve Excel kullanımı"),("İletişim",10,"Sponsor, saha ve regülatör ile vaka bazlı iletişim")]),
        ("Regulatory Affairs Specialist",
         "Etik kurul, Bakanlık/TİTCK, başvuru dosyaları ve regülatif takip süreçlerini yürüten rol.",
         [("Regülatif Bilgi",30,"Etik kurul başvurusu ve TİTCK klinik araştırma izin süreçlerine hakimiyet"),("Dokümantasyon",20,"Başvuru dosyası hazırlığı, versiyon kontrolü ve takip"),("Takip & Organizasyon",20,"Deadline, eksik evrak ve onay sürecini sistematik yönetme"),("İletişim",10,"Etik kurul, sponsor ve saha ile resmi yazışma"),("Dikkat",10,"Form ve doküman doğruluğu, tutarlılık kontrolü"),("Problem Çözme",10,"Eksik evrak/ret durumunda hızlı düzeltme ve yeniden başvuru")]),
        ("Quality Assurance (GCP QA)",
         "GCP kalite sistemi, audit, CAPA, SOP ve süreç uyumluluğunu yöneten rol.",
         [("GCP & Kalite Bilgisi",30,"ICH-GCP E6(R2), SOP uyumu ve audit readiness hazırlığı"),("Audit Yetkinliği",20,"Audit planlama, bulgu tespiti ve raporlama"),("CAPA Yönetimi",20,"Kök neden analizi (root cause analysis) ve CAPA takibi"),("Süreç İyileştirme",10,"SOP güncelleme, eğitim materyali ve standardizasyon"),("İletişim",10,"Denetim sonrası geri bildirim ve aksiyon planı iletişimi"),("Analitik Düşünme",10,"Risk bazlı kalite yaklaşımı (risk-based quality management)")]),
        ("Site Manager",
         "Klinik araştırma sahasının operasyonel, insan kaynağı ve kalite yönetiminden sorumlu rol.",
         [("Saha Operasyon Yönetimi",25,"Hasta akışı, ekip vardiyası ve kaynak planlamasını yönetme"),("Liderlik",20,"Saha ekibi koordinasyonu ve performans yönetimi"),("Kalite & GCP",20,"Protokol, ICF ve audit hazırlığına hakimiyet"),("İletişim",15,"PI, sponsor, CRO ve hasta ile çok yönlü iletişim"),("Problem Çözme",10,"Personel eksikliği, ekipman arızası gibi operasyonel kriz yönetimi"),("Raporlama",10,"KPI ve yönetim raporları hazırlama")]),
        ("Site Director",
         "Araştırma merkezinin stratejik, finansal ve operasyonel performansını yöneten üst rol.",
         [("Stratejik Liderlik",25,"Büyüme, kapasite planlama ve portföy yönetimi"),("Operasyonel Mükemmeliyet",20,"Süreç standardizasyonu ve kaynak verimliliği"),("Finansal Yönetim",15,"Bütçe, gelir ve karlılık analizi"),("İş Geliştirme",15,"Sponsor/CRO ilişkileri kurma ve yeni iş fırsatları"),("Kalite & Uyum",15,"GCP, audit ve SOP uyumunu üst düzeyde sağlama"),("Ekip Yönetimi",10,"Liderlik, kültür oluşturma ve yetenek geliştirme")]),
        ("Laboratory Technician",
         "Laboratuvar numune işleme, cihaz kullanımı, kayıt ve kalite süreçlerini yürüten teknik rol.",
         [("Teknik Laboratuvar Becerisi",30,"Numune alma/işleme, cihaz kalibrasyonu ve analiz prosedürlerine hakimiyet"),("Dikkat & Kayıt",25,"Numune etiketleme, log kaydı ve dokümantasyon doğruluğu"),("Kalite & Güvenlik",20,"Biyogüvenlik protokolleri, SOP ve kalite kontrol (QC) uygulaması"),("Zaman Yönetimi",10,"Numune zamanlaması ve öncelik sıralaması"),("Ekip Çalışması",10,"Laboratuvar ve klinik ekiple sonuç paylaşımı ve koordinasyon"),("Öğrenme",5,"Yeni analiz yöntemi veya cihaza hızlı adaptasyon")]),
        ("Laboratory Supervisor",
         "Laboratuvar ekibi, kalite, iş akışı ve cihaz/prosedür yönetiminden sorumlu rol.",
         [("Laboratuvar Yönetimi",25,"Ekip vardiyası, iş akışı ve kapasite planlaması"),("Kalite Sistemi",25,"QC/QA, SOP ve kayıt denetimi yönetimi"),("Teknik Yetkinlik",20,"Cihaz arızası/sorun giderme ve analiz yöntemi doğrulama"),("Liderlik",15,"Ekip eğitimi, performans değerlendirme ve geri bildirim"),("Güvenlik",10,"Biyogüvenlik ve risk yönetimi protokolleri"),("Raporlama",5,"KPI, stok ve cihaz bakım raporları")]),
        ("Research Scientist",
         "Bilimsel araştırma, deney tasarımı, veri analizi ve yayın/sunum üretimi yapan rol.",
         [("Bilimsel Tasarım",25,"Hipotez kurma ve deney/metodoloji tasarımı"),("Analitik Düşünme",20,"İstatistiksel veri analizi ve sonuç yorumlama"),("Teknik Uzmanlık",20,"Laboratuvar/klinik yöntem ve cihaz bilgisi"),("Yayın & Sunum",15,"Bilimsel makale yazımı ve konferans sunumu"),("Problem Çözme",10,"Deneysel aksaklıkları giderme ve optimizasyon"),("İş Birliği",10,"Multidisipliner ekiplerle (biyoistatistik, klinik) çalışma")]),
        ("Medical Science Liaison (MSL)",
         "KOL ilişkileri, bilimsel iletişim, saha medikal strateji ve içgörü toplama rolü.",
         [("Bilimsel Yetkinlik",25,"Terapötik alan literatürü ve klinik veri hakimiyeti"),("KOL İlişkileri",20,"Key opinion leader'larla bilimsel güven ilişkisi kurma"),("Sunum Becerisi",15,"Bilimsel veri sunumu ve tartışma yönetimi"),("Uyum & Etik",15,"Tanıtım dışı medikal iletişim kurallarına uyum"),("İçgörü Toplama",15,"Saha içgörüsünü (insight) yapılandırılmış şekilde raporlama"),("Planlama",10,"Saha ziyaret planı ve önceliklendirme")]),
        ("Medical Representative",
         "Saha tanıtım, hekim ilişkileri, ürün bilgisi ve satış hedeflerinden sorumlu rol.",
         [("Ürün & Pazar Bilgisi",25,"Ürün özellikleri, rakip analizi ve pazar dinamiklerine hakimiyet"),("İletişim & İkna",25,"Hekim ile güven ilişkisi kurma ve etkili sunum yapma"),("Planlama",15,"Ziyaret planı (call plan) ve territory yönetimi"),("Etik & Uyum",15,"Tanıtım kuralları ve sektörel uyum standartlarına bağlılık"),("Sonuç Odaklılık",10,"Satış hedefi takibi ve aksiyon planı oluşturma"),("Raporlama",10,"CRM sistemine (ör. Veeva CRM) ziyaret ve sonuç kaydı")]),
        ("Product Specialist",
         "Ürün uzmanlığı, saha/ekip eğitimi, ürün konumlandırma ve teknik destek sağlayan rol.",
         [("Ürün Uzmanlığı",30,"Teknik ve klinik ürün detaylarına derinlemesine hakimiyet"),("Eğitim & Sunum",20,"Saha ekibi veya müşteriye ürün eğitimi verme"),("Pazar Analizi",15,"Rakip ürün ve pazar ihtiyaç analizi"),("İletişim",15,"Saha ve müşteriye teknik destek sağlama"),("Problem Çözme",10,"Teknik/klinik soruları hızlı ve doğru yanıtlama"),("Raporlama",10,"Saha geri bildirimini içgörüye dönüştürüp raporlama")]),
        ("CTO", "Teknoloji stratejisi, mimari, ekip ve ürün geliştirme süreçlerinden sorumlu üst düzey teknoloji lideri.", [("Teknik Strateji",25,"Mimari kararlar (mikroservis/monolith), ölçeklenebilirlik ve teknoloji seçimi (cloud provider, dil/framework)"),("Liderlik",25,"Mühendislik ekibi kurma, mentorluk ve performans yönetimi"),("Ürün & İş Anlayışı",20,"Teknoloji roadmap'ini iş hedefleri ve gelir modeliyle hizalama"),("Güvenlik & Kalite",15,"Uygulama güvenliği (OWASP), code review süreci ve CI/CD kalite kapıları"),("Problem Çözme",10,"Kritik teknik/mimari kararlarda trade-off analizi"),("İletişim",5,"Yönetim kuruluna ve ekibe teknik stratejiyi anlaşılır aktarma")]),
        ("Software Developer", "Yazılım geliştirme, test, bakım ve teknik problem çözme rolü.", [("Kodlama Yetkinliği",30,"Temiz kod prensipleri, veri yapıları/algoritma bilgisi, framework (React, .NET, Django vb.) hakimiyeti"),("Problem Çözme",25,"Debug süreci, hata ayıklama araçları (debugger, log analizi) kullanımı"),("Test & Kalite",15,"Unit test yazma (Jest, pytest vb.) ve test coverage bilinci"),("Takım Çalışması",15,"Git branching stratejisi, code review ve pair programming"),("Öğrenme",10,"Yeni dil/framework/kütüphaneye hızlı adaptasyon"),("Dokümantasyon",5,"README, API dokümantasyonu ve kod içi açıklama yazımı")]),
        ("Full Stack Developer", "Frontend ve backend geliştirmeyi birlikte yürüten yazılım geliştirici rolü.", [("Backend Yetkinliği",25,"REST/GraphQL API tasarımı, iş mantığı katmanı, ORM (Entity Framework, SQLAlchemy vb.) kullanımı"),("Frontend Yetkinliği",25,"Component mimarisi, state yönetimi (Redux, Context vb.) ve responsive tasarım"),("Veritabanı",15,"SQL sorgu optimizasyonu, indexleme ve veri modelleme"),("DevOps Bilinci",10,"Deploy pipeline'ı, ortam değişkenleri, log/monitoring araçları"),("Problem Çözme",15,"Frontend-backend entegrasyon hatalarını debug etme"),("Takım Çalışması",10,"Git workflow ve code review kültürü")]),
        ("Backend Developer", "API, veritabanı, entegrasyon ve sunucu tarafı mimari geliştirme rolü.", [("API Tasarımı",25,"REST/GraphQL endpoint tasarımı, authentication/authorization (JWT, OAuth)"),("Veritabanı",25,"SQL/NoSQL modelleme, index ve query performansı"),("Güvenlik",15,"Input validation, secrets yönetimi, SQL injection/XSS önleme"),("Performans",10,"Caching (Redis vb.) ve query optimizasyonu"),("Test & Debug",15,"Unit/integration test yazımı ve hata analizi"),("DevOps",10,"Deploy süreci ve log/monitoring yönetimi")]),
        ("Frontend Developer", "Kullanıcı arayüzü, deneyim, state ve tarayıcı tarafı geliştirme rolü.", [("React/UI Yetkinliği",30,"Component yapısı, hook kullanımı, routing (React Router vb.)"),("UX & Responsive",20,"Mobil uyum, erişilebilirlik (a11y) ve kullanılabilirlik prensipleri"),("API Entegrasyonu",15,"Async veri çekme, hata/loading state yönetimi"),("Performans",10,"Bundle boyutu optimizasyonu, lazy loading, render performansı"),("Test & Debug",15,"Browser dev tools, console debug ve cross-browser test"),("Tasarım Dikkati",10,"Design system/Figma uyumu ve görsel tutarlılık")]),
        ("DevOps Engineer", "CI/CD, bulut, deploy, izleme, güvenlik ve altyapı otomasyonundan sorumlu rol.", [("CI/CD",25,"Pipeline kurulumu (GitHub Actions, Jenkins vb.), release ve rollback stratejisi"),("Cloud & Container",25,"Docker, Kubernetes veya cloud servisleri (AWS/Azure/GCP) yönetimi"),("Monitoring",15,"Log/metric toplama (Prometheus, Grafana vb.) ve alert kurulumu"),("Security",15,"Secrets yönetimi (Vault vb.), network güvenliği ve hardening"),("Automation",10,"Infrastructure as Code (Terraform, Ansible) ve script otomasyonu"),("Problem Çözme",10,"Incident response ve kök neden analizi")]),
        ("QA Engineer", "Test planı, manuel/otomasyon test, kalite süreçleri ve hata yönetiminden sorumlu rol.", [("Test Tasarımı",25,"Test case yazımı, senaryo ve edge-case kapsaması"),("Otomasyon",20,"Test otomasyon araçları (Selenium, Cypress, Playwright vb.) ve scripting"),("Hata Analizi",20,"Bug raporu yazımı, reproduce adımları ve önceliklendirme"),("Ürün Anlayışı",15,"Kullanıcı akışı ve gereksinim dokümanına hakimiyet"),("İletişim",10,"Geliştirici/PM ile bug/test sonucu iletişimi"),("Dikkat",10,"Detay odaklılık ve regresyon test disiplini")]),
        ("Project Manager", "Proje planlama, ekip koordinasyonu, risk, zaman ve paydaş yönetiminden sorumlu rol.", [("Planlama",25,"Kapsam (scope), timeline ve kaynak planı oluşturma (Gantt chart vb.)"),("Risk Yönetimi",20,"Risk kaydı (risk register), issue takibi ve aksiyon planı"),("İletişim",20,"Paydaş ve ekip ile düzenli status raporlama"),("Liderlik",15,"Ekip motivasyonu, önceliklendirme ve karar alma"),("Bütçe",10,"Maliyet takibi ve kaynak optimizasyonu"),("Yazılım ve Sistem Kullanımı",10,"Jira, MS Project, Asana veya benzeri proje yönetim yazılımlarıyla iş takibi ve raporlama")]),
        ("Product Manager", "Ürün vizyonu, roadmap, kullanıcı ihtiyacı ve iş önceliklendirme rolü.", [("Ürün Stratejisi",25,"Ürün vizyonu ve roadmap önceliklendirme (RICE, MoSCoW vb.)"),("Kullanıcı Anlayışı",20,"Kullanıcı araştırması, UX testleri ve ihtiyaç analizi"),("Analitik",15,"Metric/funnel analizi (conversion rate vb.) ile veri odaklı karar"),("Teknik İletişim",15,"Geliştirici ekiple teknik kısıt ve önceliklendirme uyumu"),("Stakeholder Yönetimi",15,"İş birimleri ve yönetimle beklenti yönetimi"),("Problem Çözme",10,"Trade-off analizleri ve önceliklendirme kararları")]),
        ("Business Analyst", "İş gereksinimlerini analiz eden, süreç modelleyen ve teknik ekibe aktaran rol.", [("Gereksinim Yönetimi",20,"İşletme ihtiyaçlarını doğru toplama, önceliklendirme ve BRD/kullanıcı hikayesi olarak belgeleme"),("Süreç Modelleme",15,"Mevcut/hedef süreç akış şemaları ve senaryolar oluşturma (UML, BPMN)"),("Veri Analitiği",15,"Verileri yorumlama ve trend çıkarma; SQL, Tableau veya Power BI gibi araçlara hakimiyet"),("Çevik (Agile) Metodolojiler",15,"Scrum ve Kanban süreçlerinde aktif rol alma, backlog ve kullanıcı hikayesi yönetimi"),("Paydaş Yönetimi ve İletişim",20,"Müşteri ile geliştirici ekip arasında net dil kullanma, koordinasyon ve teknik olmayan yöneticilere analiz/iş değerini net sunma"),("Müzakere ve Problem Çözme",15,"Çatışma yönetimi ve karmaşık iş problemlerine rasyonel, veriye dayalı çözüm üretme")]),
        ("HR Specialist", "İşe alım, çalışan ilişkileri, eğitim, performans ve insan kaynakları operasyonları rolü.", [("İşe Alım",25,"Aday tarama, mülakat süreci tasarımı ve işe alım metrikleri (time-to-hire vb.)"),("İletişim",20,"Çalışan ve yönetici arasında net ve empatik iletişim"),("Organizasyon",15,"Özlük dosyası, süreç takibi ve dokümantasyon"),("Mevzuat & Uyum",15,"İş Kanunu ve şirket politikalarına hakimiyet"),("Analitik",10,"HR metrikleri (turnover, engagement) analizi"),("Gizlilik",15,"KVKK kapsamında çalışan verisi gizliliği ve etik yaklaşım")]),
        ("Finance Specialist", "Finansal raporlama, bütçe planlama, nakit akışı takibi, mali kontrol ve yönetim raporlaması odaklı rol (muhasebe kaydı/beyanname/dönem sonu işlemleri Muhasebe Uzmanı kapsamındadır).", [("Finansal Raporlama ve Bütçe",25,"Yönetim raporları, bütçe hazırlığı ve bütçe-gerçekleşme analizi, konsolidasyon"),("Nakit Akışı ve Mali Kontrol",25,"Nakit akış tablosu, tahsilat/ödeme planlama, iç kontrol ve onay süreçleri, mutabakat"),("Analitik",20,"Finansal veri analizi, oran analizi ve trend yorumlama; karar destekleyici içgörü"),("Yazılım ve Sistem Kullanımı",10,"Excel (pivot, formül, model kurma) ve ERP (SAP, Logo, Odoo vb.) raporlama modülleri"),("Uyum",10,"Vergi mevzuatı, iç kontrol ve şirket politikalarına uyum farkındalığı"),("İletişim",10,"Ekip ve üst yönetime finansal durumu net ve karar odaklı aktarma")]),
        ("Sales Manager", "Satış hedefleri, ekip, müşteri ilişkileri ve gelir büyümesinden sorumlu rol.", [("Satış Stratejisi",25,"Hedef belirleme, segment analizi ve pipeline yönetimi"),("Ekip Yönetimi",20,"Satış ekibi koçluğu ve performans değerlendirmesi"),("Müşteri İlişkileri",20,"Güven inşası, müzakere ve müşteri sorunu çözümü"),("Analitik",15,"CRM verisi (Salesforce, HubSpot vb.), forecast ve KPI takibi"),("Sonuç Odaklılık",10,"Satış hedefine yönelik aksiyon planı takibi"),("İletişim",10,"Sunum ve ikna becerisiyle müşteri/ekip yönetimi")]),
        ("Marketing Manager", "Pazarlama stratejisi, kampanya, marka, içerik ve performans yönetimi rolü.", [("Strateji",25,"Pazar analizi, hedef kitle segmentasyonu ve konumlandırma"),("Kampanya Yönetimi",20,"Kampanya planlama, uygulama ve optimizasyon"),("Dijital Pazarlama",15,"SEO, ads (Google/Meta) ve sosyal medya yönetimi"),("Analitik",15,"Metric (CTR, ROI) analizi ve raporlama (Google Analytics vb.)"),("Yaratıcılık",15,"İçerik ve mesaj stratejisi geliştirme"),("İletişim",10,"Ekip ve ajans koordinasyonu")]),
        ("Kasa Yöneticisi", "Kasa operasyonlarını, nakit akışını ve kasa personelini yöneten; günlük/haftalık kasa mutabakatı ile veri güvenliğinden sorumlu rol.", [("Finansal Okuryazarlık & Nakit Yönetimi",25,"Nakit akışı takibi, kasa mutabakatı, kasa açığı/fazlası kontrolü"),("Dikkat & Doğruluk",20,"Kasa sayımı, veri girişi ve işlem hatasını önleme"),("Sorumluluk & Güvenilirlik",15,"İşletmenin nakit varlığını yönetme ve veri güvenliği"),("Ekip & Vardiya Yönetimi",15,"Yoğun temoda vardiya planlama ve kasa personelini yönlendirme"),("Teknoloji Hakimiyeti",15,"MS Office (özellikle Excel) ve ERP/POS sistemleri kullanımı"),("İletişim & Müşteri İlişkileri",10,"Müşteri memnuniyeti ve ödeme sorunu çözümü")]),
        ("Muhasebe Uzmanı", "Muhasebe kaydı, beyanname ve bildirimler, e-belge süreçleri, dönem sonu işlemleri ve mutabakat odaklı rol (finansal raporlama/bütçe/nakit akışı yönetimi Finance Specialist kapsamındadır).", [("Muhasebe Bilgisi",20,"Tek Düzen Hesap Planı, yevmiye ve borç/alacak kayıt mantığı, hesap sınıflarını doğru kullanma; dönem sonu işlemleri (amortisman, karşılık, kur değerlemesi, reeskont, kapanış)."),("Vergi ve Mevzuat",20,"KDV, muhtasar, damga beyannameleri; BA-BS formları; beyanname takvimi; e-Fatura / e-Arşiv / e-Defter süreçleri, berat yükleme; mevzuat değişikliklerini takip etme."),("Kontrol ve Doğruluk",20,"Cari, banka ve kasa mutabakatı, açık kalan hesapların çözümü; kapanmayan hesap / tutmayan mutabakat senaryosunda izlenen hata bulma yöntemi."),("Analitik",15,"Bilanço ve gelir tablosu okuma, oran/tutarsızlık yorumlama; stok değerleme ve maliyet muhasebesi temelleri (üretim/hizmet maliyeti, maliyet dağıtımı)."),("Yazılım ve Sistem Kullanımı",15,"Logo, Mikro, Netsis, SAP veya Odoo üzerinde fiili muhasebe deneyimi; Excel (pivot, düşeyara/indis-kaçıncı, mutabakat ve kontrol tabloları)."),("Süreç Yönetimi",10,"Bordro kayıtlarının muhasebeye aktarımı ve SGK bildirimleri; beyanname takvimi yoğunluğunda önceliklendirme ve teslim disiplini.")]),
    ]
    for name, desc, criteria_pairs in defaults:
        # B3 — 6 kriter standardı: seed'de ihlal varsa BAŞLATMAYI DURDURMA, sadece net logla.
        if len(criteria_pairs) != 6:
            print(f"[SEED_UYARI] '{name}' pozisyonu {len(criteria_pairs)} kriterle tanımlı — standart TAM 6.")
        _wsum = sum(w for _, w, _ in criteria_pairs)
        if _wsum != 100:
            print(f"[SEED_UYARI] '{name}' pozisyonu kriter ağırlık toplamı {_wsum} — 100 olmalı.")
        criteria = [{"name": n, "weight": w, "desc": d} for n, w, d in criteria_pairs]
        category = infer_position_category(name)
        conn.execute(
            "INSERT OR IGNORE INTO positions (name, category, role_description, criteria_json, org_id) VALUES (?, ?, ?, ?, ?)",
            (name, category, desc, json.dumps(criteria, ensure_ascii=False), medex_org_id)
        )
        # PostgreSQL'e ilk geçişte yanlış kategoriler kaydedilmiş olabileceği için,
        # kodla gelen varsayılan pozisyonların kategorisini V14.5 kurallarına göre düzelt.
        # Yalnızca MedeX'in defaults listesindeki, PANELDEN ÖZELLEŞTİRİLMEMİŞ (is_customized=0)
        # pozisyonlarına uygulanır; kullanıcı eklediği özel/özelleştirilmiş kayıtlara dokunmaz.
        conn.execute("UPDATE positions SET category=? WHERE name=? AND org_id=? AND COALESCE(is_customized,0)=0",
                     (category, name, medex_org_id))
    # TEK SEFERLİK İÇERİK DÜZELTMESİ: defaults listesindeki TÜM pozisyonların kriterleri
    # detaylandırılıp somut araç/standart/yöntem örnekleriyle zenginleştirildi (ör. Business
    # Analyst'te sadece 2/6 kriterin somut kancası vardı — Dokümantasyon->BRD, Test Desteği->UAT
    # — bu yüzden mülakatlar hep aynı 2 konuya daralıyordu). INSERT OR IGNORE zaten var olan
    # kaydı değiştirmediği için, burada DB'de zaten var olan tüm default pozisyonların
    # criteria_json'u da yeni, zenginleştirilmiş haliyle zorla güncelleniyor. NOT: Admin panelinden
    # bu pozisyonlardan birinin kriterlerini elle özelleştirdiyseniz, bir sonraki deploy'da bu
    # blok o özelleştirmeyi de ezer — panelden manuel kriter düzenlemesi yapmayı planlıyorsanız
    # bu bloğu kaldırmamız gerekebilir, haber verin.
    for name, desc, criteria_pairs in defaults:
        forced_json = json.dumps([{"name": n, "weight": w, "desc": d} for n, w, d in criteria_pairs], ensure_ascii=False)
        # B7 — panelden özelleştirilmiş (is_customized=1) kayıtları EZME.
        conn.execute("UPDATE positions SET criteria_json=?, role_description=? WHERE name=? AND org_id=? AND COALESCE(is_customized,0)=0",
                     (forced_json, desc, name, medex_org_id))
    conn.commit()

    # TEK SEFERLİK ONARIM: ilk sürümde telefon eşleştirmesi e-posta farklı olsa bile devreye
    # girip aynı telefonu paylaşan FARKLI kişileri yanlışlıkla tek person'a birleştiriyordu.
    # Bu, o hatayla atanmış tüm person_id'leri sıfırlayıp aşağıdaki (artık düzeltilmiş) backfill
    # mantığının herkesi doğru şekilde yeniden eşleştirmesini sağlar. Tek seferlik ve idempotent.
    already_repaired = conn.execute("SELECT 1 FROM schema_migrations WHERE name=?", ("person_email_overrides_phone_fix",)).fetchone()
    if not already_repaired:
        conn.execute("UPDATE candidates SET person_id=NULL")
        conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", ("person_email_overrides_phone_fix",))
        conn.commit()

    # TEK SEFERLİK BACKFILL: mevcut candidates satırlarına org_id/person_id atar. Sadece
    # person_id boş olan satır kaldığı sürece çalışır (idempotent) — find_or_create_person()
    # bu noktada henüz Python fonksiyonu olarak tanımlı değil (init_db() modül yüklenirken
    # çağrılıyor), bu yüzden aynı e-posta/telefon eşleştirme mantığı burada satır içi tekrarlanır.
    # Telefon SADECE e-posta hiç yoksa (ör. walk-in) kullanılır — farklı e-postalı kişiler asla
    # sadece ortak telefon yüzünden birleştirilmez.
    pending = conn.execute("SELECT id FROM candidates WHERE person_id IS NULL LIMIT 1").fetchone()
    if pending:
        rows = conn.execute("SELECT * FROM candidates ORDER BY created_at ASC, id ASC").fetchall()
        for r in rows:
            row = dict(r)
            org_id = None if row.get("invite_type") == "general" else medex_org_id
            email = (row.get("email") or "").strip().lower()
            phone = re.sub(r'\D', '', row.get("phone") or "")
            person_row = None
            if email:
                if org_id is None:
                    person_row = conn.execute("SELECT id FROM persons WHERE org_id IS NULL AND lower(email)=? ORDER BY created_at ASC LIMIT 1", (email,)).fetchone()
                else:
                    person_row = conn.execute("SELECT id FROM persons WHERE org_id=? AND lower(email)=? ORDER BY created_at ASC LIMIT 1", (org_id, email)).fetchone()
            if not person_row and phone and not email:
                if org_id is None:
                    person_row = conn.execute("SELECT id FROM persons WHERE org_id IS NULL AND phone=? ORDER BY created_at ASC LIMIT 1", (phone,)).fetchone()
                else:
                    person_row = conn.execute("SELECT id FROM persons WHERE org_id=? AND phone=? ORDER BY created_at ASC LIMIT 1", (org_id, phone)).fetchone()
            if person_row:
                person_id = person_row["id"]
            else:
                insert_sql = "INSERT INTO persons (org_id, full_name, email, phone) VALUES (?, ?, ?, ?)"
                params = (org_id, row.get("name"), email or None, phone or None)
                if USE_POSTGRES:
                    person_id = conn.execute(insert_sql + " RETURNING id", params).fetchone()["id"]
                else:
                    conn.execute(insert_sql, params)
                    person_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("UPDATE candidates SET org_id=?, person_id=? WHERE id=?", (org_id, person_id, row["id"]))
        conn.commit()

    conn.close()

init_db()

# ============ MODELS ============
class AdminLogin(BaseModel):
    email: str
    password: str

class OrganizationCreate(BaseModel):
    name: str
    slug: str

class OrgAdminCreate(BaseModel):
    name: str
    email: str
    password: Optional[str] = None

class AdminProfileUpdate(BaseModel):
    current_password: str
    new_password: str

class CriterionItem(BaseModel):
    name: str
    weight: int
    desc: str = ""

class PositionCreate(BaseModel):
    name: str
    category: str = "Genel"
    role_description: str = ""
    criteria: List[CriterionItem]

class PersonNoteCreate(BaseModel):
    body: str

class CandidateCreate(BaseModel):
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    position: str
    level: int = 1  # 1: metin bazlı 10dk, 2: 20dk (CV zorunlu), 3: 30+ dk adaptif (CV zorunlu)
    depth_tier: str = "standart"  # kisa | standart | derin — level'ın kendi baz süresine göre yaklaşık yönlendirme
    interview_language: str = "tr"  # tr | en | de — mülakatın hangi dilde yürütüleceği
    report_language: str = "tr"  # tr | en | de — rapor/PDF'in hangi dilde yazılacağı (adaydan bağımsız)
    education: Optional[str] = None
    university: Optional[str] = None
    department: Optional[str] = None
    experience_years: int = 0
    ai_note: Optional[str] = None
    send_email: bool = True

class CandidateUpdate(BaseModel):
    """admin_update_candidate (PATCH) için — CandidateCreate'ten farklı olarak TÜM alanlar
    Optional/varsayılan None. exclude_unset=True ile sadece istekte GERÇEKTEN gönderilen
    alanlar DB'ye yazılır; gönderilmeyen alan eski değerinde kalır (tam-değiştirme değil,
    kısmi güncelleme)."""
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    position: Optional[str] = None
    level: Optional[int] = None
    depth_tier: Optional[str] = None
    interview_language: Optional[str] = None
    report_language: Optional[str] = None
    education: Optional[str] = None
    university: Optional[str] = None
    department: Optional[str] = None
    experience_years: Optional[int] = None
    ai_note: Optional[str] = None

class CvPoolInvite(BaseModel):
    position: str
    level: int = 1
    depth_tier: str = "standart"
    interview_language: str = "tr"
    report_language: str = "tr"
    send_email: bool = True

class NewAttemptRequest(BaseModel):
    """Tamamlanmış bir mülakat kaydı düzenlemeye kapalıdır; onun yerine aynı kişi için yeni bir
    mülakat çağrısı açılır. Bu gövde sadece yeni çağrıya özgü alanları taşır — kimlik/eğitim/CV
    alanları kaynak candidate satırından birebir kopyalanır.

    KOPYALAMA KURALI: TÜM alanlar Optional/None. Sabit varsayılan YOK — gövdede None gelen her
    alan kaynak candidate satırından kopyalanır. Aksi halde (ör. sabit level=1 / dil='tr')
    frontend bir alanı hiç göndermediğinde kaynağın gerçek değeri sessizce ezilirdi."""
    position: Optional[str] = None
    level: Optional[int] = None
    depth_tier: Optional[str] = None
    interview_language: Optional[str] = None
    report_language: Optional[str] = None
    ai_note: Optional[str] = None
    send_email: bool = True  # diğer davet uçlarıyla aynı: varsayılan davet maili gönderilir

class CandidateLogin(BaseModel):
    username: str
    password: str

class GeneralApply(BaseModel):
    name: str
    email: str
    phone: str
    position: str
    education: str
    university: Optional[str] = None
    department: Optional[str] = None
    experience_years: int = 0
    ai_note: Optional[str] = None

class ChatMessage(BaseModel):
    candidate_id: int
    message: str
    history: List[dict]
    elapsed_seconds: int = 0

class ViolationReport(BaseModel):
    candidate_id: int
    violation_type: str
    detail: Optional[str] = None       # BÖLÜM 3: frontend'in verdiği somut bağlam
    elapsed_seconds: int = 0           # BÖLÜM 3: ihlal mülakatın kaçıncı saniyesinde

class SnapshotData(BaseModel):
    candidate_id: int
    image_base64: str
    reason: Optional[str] = None
    elapsed_ms: Optional[int] = None  # FAZ D: mülakat başından beri geçen süre (transkriptle hizalama)
    level: Optional[int] = None       # FAZ D: kare hangi seviye mülakatında alındı

# ============ HELPERS ============
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def generate_password(length=8) -> str:
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))

def generate_username(name: str, db) -> str:
    base = re.sub(r'[^a-zA-Z0-9]', '', name.lower().split()[0]) or "aday"
    username = base
    counter = 1
    while db.execute("SELECT id FROM candidates WHERE username=?", (username,)).fetchone():
        username = f"{base}{counter}"
        counter += 1
    return username

def create_token(data: dict, days=7) -> str:
    payload = {**data, "exp": datetime.utcnow() + timedelta(days=days)}
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        return jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        raise HTTPException(status_code=401, detail="Geçersiz token")

def verify_admin(payload=Depends(verify_token)):
    if payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Yetkisiz")
    return payload

def verify_superadmin(payload=Depends(verify_admin)):
    if payload.get("admin_role") != "superadmin":
        raise HTTPException(status_code=403, detail="Bu işlem sadece süperadmin tarafından yapılabilir")
    return payload

def get_position(name: str, db=None, org_id: Optional[int] = None):
    close = False
    if db is None:
        db = get_db(); close = True
    if org_id is not None:
        row = db.execute("SELECT * FROM positions WHERE name=? AND org_id=?", (name, org_id)).fetchone()
    else:
        # org_id verilmezse eski davranış: isme göre ilk eşleşen satır (L1/L2/L3 mülakat
        # akışı bu şekilde çağırır — davranışı değiştirmemek için buraya dokunulmadı).
        row = db.execute("SELECT * FROM positions WHERE name=?", (name,)).fetchone()
    if close:
        db.close()
    if not row:
        return None
    return {
        "id": row["id"], "name": row["name"],
        "category": row["category"] if "category" in row.keys() else "Genel",
        "role_description": row["role_description"],
        "criteria": json.loads(row["criteria_json"])
    }


def normalize_email(email: Optional[str]) -> str:
    return (email or "").strip().lower()

def find_latest_candidate_by_email(db, email: str):
    e = normalize_email(email)
    if not e:
        return None
    return db.execute(
        "SELECT * FROM candidates WHERE lower(email)=? ORDER BY datetime(created_at) DESC, id DESC LIMIT 1",
        (e,)
    ).fetchone()

def normalize_phone(phone: Optional[str]) -> str:
    return re.sub(r'\D', '', phone or "")

def find_or_create_person(db, org_id: Optional[int], name: str, email: Optional[str], phone: Optional[str]) -> int:
    """Aynı kurum (org_id) içinde önce e-posta, sonra (SADECE e-posta hiç yoksa) telefonla kişi
    eşleştirir; yoksa yeni `persons` satırı açar. Telefon eşleştirmesi bilerek sadece e-postasız
    kayıtlara (ör. walk-in) uygulanır — aksi halde aynı telefonu paylaşan ama gerçekte farklı
    kişiler olan adaylar (ör. aynı test telefonuyla oluşturulmuş kayıtlar) yanlışlıkla tek kişi
    sanılıp birleştirilir."""
    e = normalize_email(email)
    p = normalize_phone(phone)
    row = None
    if e:
        if org_id is None:
            row = db.execute("SELECT * FROM persons WHERE org_id IS NULL AND lower(email)=? ORDER BY created_at DESC LIMIT 1", (e,)).fetchone()
        else:
            row = db.execute("SELECT * FROM persons WHERE org_id=? AND lower(email)=? ORDER BY created_at DESC LIMIT 1", (org_id, e)).fetchone()
    if not row and p and not e:
        if org_id is None:
            row = db.execute("SELECT * FROM persons WHERE org_id IS NULL AND phone=? ORDER BY created_at DESC LIMIT 1", (p,)).fetchone()
        else:
            row = db.execute("SELECT * FROM persons WHERE org_id=? AND phone=? ORDER BY created_at DESC LIMIT 1", (org_id, p)).fetchone()
    if row:
        return row["id"]
    insert_sql = "INSERT INTO persons (org_id, full_name, email, phone) VALUES (?, ?, ?, ?)"
    params = (org_id, name, e or None, p or None)
    if USE_POSTGRES:
        person_id = db.execute(insert_sql + " RETURNING id", params).fetchone()["id"]
    else:
        db.execute(insert_sql, params)
        person_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit()
    return person_id

def get_medex_org_id(db) -> Optional[int]:
    row = db.execute("SELECT id FROM organizations WHERE slug=?", ("medex",)).fetchone()
    return row["id"] if row else None

def get_org_id_for_admin(db, admin_payload: dict, org_id_override: Optional[int] = None) -> Optional[int]:
    """Org-scoped erişim: org_admin her zaman kendi token'ındaki kuruma kilitlenir.
    Süperadmin, org_id_override (ör. ?org_id= parametresi) ile başka bir kurumu görüntüleyebilir;
    belirtmezse (veya eski/claim'siz token ise) MedeX varsayılanına düşer."""
    if admin_payload.get("admin_role") == "superadmin":
        return org_id_override or get_medex_org_id(db)
    org_id = admin_payload.get("org_id")
    if org_id:
        return org_id
    return get_medex_org_id(db)

def build_compact_memory(messages: list, max_chars: int = 2400) -> str:
    """Ekonomik ama tutarlı mülakat hafızası: tüm geçmişi değil, soru-cevap çekirdeğini taşır."""
    pairs = []
    last_q = None
    q_no = 0
    for m in messages:
        role = m.get("role")
        content = re.sub(r"\s+", " ", (m.get("content") or "")).strip()
        if not content:
            continue
        if role == "assistant":
            # rapor/sonlandırma mesajlarını hafızaya alma
            if "---RAPOR---" in content:
                continue
            q_no += 1
            last_q = content[:220]
        elif role == "user":
            answer = content[:360]
            if last_q:
                pairs.append(f"S{q_no}: {last_q}\nC{q_no}: {answer}")
            else:
                pairs.append(f"C: {answer}")
    text = "\n".join(pairs)
    if len(text) > max_chars:
        text = text[-max_chars:]
        # satır ortasından başlamasın
        text = text[text.find("\n")+1:] if "\n" in text else text
    return text

def get_interview_messages(db, candidate_id: int, level: int = None) -> list:
    if level is not None:
        row = db.execute("SELECT messages FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, level)).fetchone()
    else:
        # Geriye uyumluluk: level belirtilmezse en son (en yeni) mülakat kaydı döner.
        row = db.execute("SELECT messages FROM interviews WHERE candidate_id=? ORDER BY id DESC LIMIT 1", (candidate_id,)).fetchone()
    if not row:
        return []
    try:
        return json.loads(row["messages"] or "[]")
    except Exception as e:
        print(f"UYARI (get_interview_messages: messages JSON bozuk, candidate_id={candidate_id}): {type(e).__name__}: {e}")
        return []

def add_token_usage(candidate_id: int, level: int, response):
    """Her Anthropic API çağrısından sonra input/output token sayısını ilgili
    mülakat kaydına ekler (kümülatif). Admin panelinde 'kaç token harcandı' bilgisini
    göstermek için kullanılır. Hata olursa mülakatı bozmasın diye sessizce geçilir."""
    try:
        usage = getattr(response, "usage", None)
        if not usage:
            return
        in_tok = getattr(usage, "input_tokens", 0) or 0
        out_tok = getattr(usage, "output_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        db = get_db()
        db.execute(
            "UPDATE interviews SET total_input_tokens = total_input_tokens + ?, total_output_tokens = total_output_tokens + ? WHERE candidate_id=? AND level=?",
            (in_tok + cache_read, out_tok, candidate_id, level)
        )
        db.commit(); db.close()
    except Exception as e:
        print(f"UYARI (token sayımı kaydedilemedi): {type(e).__name__}: {e}")



def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value if value is not None else default)
    except Exception as e:
        print(f"UYARI (_safe_int: sayıya çevrilemedi, value={value!r}): {type(e).__name__}: {e}")
        return default

# Yaklaşık USD fiyatlandırma (1M token başına) — TEK KAYNAK. Hem backend (panel/log) hem
# frontend'in canlı tahmini (/api/realtime/session yanıtındaki "pricing" alanı üzerinden) bu
# tabloyu okur; fiyat değişirse SADECE burası güncellenir. input_cached/audio_input_cached,
# OpenAI'nin prompt caching indirimli oranı (bkz. developers.openai.com/api/docs/models/<model>).
# input_cached/audio_input_cached bilinmiyorsa (ör. eski/varsayım modeller) fresh orana eşitlenir —
# yani "indirim yok" varsayılır, maliyet asla olduğundan düşük gösterilmez.
AI_PRICING_PER_1M = {
    # Resmî rate-card (Ağustos 2026 itibarıyla doğrulandı): text ve audio ayrı ücretlenir.
    ("openai", "gpt-realtime-2.1"):      {"input": 4.0, "input_cached": 0.4,  "output": 24.0, "audio_input": 32.0, "audio_input_cached": 0.4,  "audio_output": 64.0},
    ("openai", "gpt-realtime-2.1-mini"): {"input": 0.6, "input_cached": 0.06, "output": 2.4,  "audio_input": 10.0, "audio_input_cached": 0.3,  "audio_output": 20.0},
    # Eski env kullanan kurulumlar için geriye uyumlu yaklaşık kayıt — cache oranı doğrulanmadı, fresh varsayılır.
    ("openai", "gpt-realtime-2"):        {"input": 4.0, "input_cached": 4.0,  "output": 24.0, "audio_input": 32.0, "audio_input_cached": 32.0, "audio_output": 64.0},
    ("openai", "gpt-4o"):                {"input": 2.5, "input_cached": 2.5,  "output": 10.0, "audio_input": 0.0,  "audio_input_cached": 0.0,  "audio_output": 0.0},
    # FAZ D: mimik analizi (MIMIC_ANALYSIS_MODEL varsayılanı gpt-4o, yukarıda) ve ortak rapor
    # muhalif denetçisi (OPENAI_REVIEWER_MODEL varsayılanı gpt-4.1) — panelde ayrı kalem olarak
    # görünsün diye fiyatları burada. Farklı bir model env ile seçilirse buraya da eklenmeli
    # (aksi halde token sayılır ama estimated_cost_usd 0 çıkar).
    ("openai", "gpt-4.1"):              {"input": 2.0, "input_cached": 0.5,  "output": 8.0,  "audio_input": 0.0,  "audio_input_cached": 0.0,  "audio_output": 0.0},
    ("anthropic", "claude-sonnet-4-6"): {"input": 3.0, "input_cached": 3.0,  "output": 15.0, "audio_input": 3.0,  "audio_input_cached": 3.0,  "audio_output": 15.0},
}

# MADDE 5 — token bazlı DEĞİL, dakika (transkripsiyon) / 1K karakter (TTS) bazlı fiyatlandırma.
# AI_PRICING_PER_1M yapısına uymadıkları için ayrı tutulur; record_flat_usage bunları okur.
# Elle bakımlı yaklaşık oranlar — gerçek fatura kaynağı değildir, model/fiyat değişince güncelle.
AI_PRICING_FLAT = {
    ("openai", "whisper-1"): {"unit": "minute",  "usd_per_unit": 0.006},   # $0.006 / dakika
    ("openai", "tts-1"):     {"unit": "1k_char", "usd_per_unit": 0.015},    # $15 / 1M karakter
    ("openai", "tts-1-hd"):  {"unit": "1k_char", "usd_per_unit": 0.030},
}

# MADDE 4 — rapor zinciri transkript kırpma tavanı. Ölçüm: 30 dk mülakat transkripti ~18-22k
# karakter; L3 "derin" (~48 dk) talkatif adayda ~35-40k'ya çıkabiliyor. Önceki slice'lar
# (rapor 30k / profil 26k / denetçi 20k) bu üst uçta transkriptin SONUNU (kapanış turları,
# 'eklemek istediğiniz bir şey' cevabı, erken sonlandırma sözleri) kesip kanıt kaybına yol
# açıyordu — özellikle denetçi [:20000] tipik bir derin mülakatı bile kesiyordu. Tek ortak
# tavan 40k: tipik mülakatta zaten devreye girmez (kırpma yok), en uzun mülakatta bile
# kapanış turlarını korur. Maliyeti: nadir uzun mülakatta 2-3 çağrıya ~+6k token (~$0.05) —
# kanıt bütünlüğü için kabul edilir (bkz. görev Madde 4: kanıt kaybına yol açan kırpma YAPMA).
TRANSCRIPT_PROMPT_MAX_CHARS = 40000

def _estimate_cost_usd(provider: str, model: str, input_tokens: int, output_tokens: int,
                        audio_input_tokens: int, audio_output_tokens: int,
                        cached_input_tokens: int = 0, cached_audio_input_tokens: int = 0) -> float:
    rates = AI_PRICING_PER_1M.get((provider, model))
    if not rates:
        return 0.0
    # cached_* değerleri input_tokens/audio_input_tokens'ın ALT KÜMESİDİR (OpenAI'nin
    # input_token_details.cached_tokens_details ile aynı semantik) — ayrıca toplanmaz.
    cached_input_tokens = min(cached_input_tokens, input_tokens)
    cached_audio_input_tokens = min(cached_audio_input_tokens, audio_input_tokens)
    fresh_input = input_tokens - cached_input_tokens
    fresh_audio_input = audio_input_tokens - cached_audio_input_tokens
    return round(
        (fresh_input * rates["input"] + cached_input_tokens * rates.get("input_cached", rates["input"]) +
         output_tokens * rates["output"] +
         fresh_audio_input * rates["audio_input"] + cached_audio_input_tokens * rates.get("audio_input_cached", rates["audio_input"]) +
         audio_output_tokens * rates["audio_output"]) / 1_000_000,
        4
    )

def record_ai_usage(candidate_id: int, level: int, provider: str, model: str, action: str,
                    input_tokens: int = 0, output_tokens: int = 0,
                    audio_input_tokens: int = 0, audio_output_tokens: int = 0,
                    cached_input_tokens: int = 0, cached_audio_input_tokens: int = 0,
                    raw: Optional[Any] = None):
    """Her AI çağrısını/mülakat realtime kullanımını ayrı satır olarak kaydeder.
    Amaç: Her mülakat sonunda hangi işlem kaç token kullanmış net görülsün.
    Hata olursa mülakat akışını bozmaz.
    NOT: audio token'lar metin token'larından ~6-13x daha pahalı olduğu için ayrı tutuluyor;
    tabloda/panelde "toplam token" tek başına maliyeti temsil etmez, bu yüzden estimated_cost_usd de kaydediliyor.
    cached_* alanları girildiğinde (bkz. cache oranı ~%80-99 indirimli) maliyet tahmini buna göre düşer."""
    try:
        input_tokens = _safe_int(input_tokens)
        output_tokens = _safe_int(output_tokens)
        audio_input_tokens = _safe_int(audio_input_tokens)
        audio_output_tokens = _safe_int(audio_output_tokens)
        cached_input_tokens = min(_safe_int(cached_input_tokens), input_tokens)
        cached_audio_input_tokens = min(_safe_int(cached_audio_input_tokens), audio_input_tokens)
        total_tokens = input_tokens + output_tokens + audio_input_tokens + audio_output_tokens
        cost_usd = _estimate_cost_usd(provider, model, input_tokens, output_tokens, audio_input_tokens, audio_output_tokens,
                                       cached_input_tokens, cached_audio_input_tokens)
        db = get_db()
        db.execute("""
            INSERT INTO ai_usage_logs
            (candidate_id, level, provider, model, action, input_tokens, output_tokens, audio_input_tokens, audio_output_tokens,
             cached_input_tokens, cached_audio_input_tokens, total_tokens, estimated_cost_usd, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (candidate_id, level, provider, model, action, input_tokens, output_tokens, audio_input_tokens, audio_output_tokens,
              cached_input_tokens, cached_audio_input_tokens, total_tokens, cost_usd,
              json.dumps(raw or {}, ensure_ascii=False)[:8000]))
        db.execute("""
            UPDATE interviews
            SET total_input_tokens = total_input_tokens + ?,
                total_output_tokens = total_output_tokens + ?
            WHERE candidate_id=? AND level=?
        """, (input_tokens + audio_input_tokens, output_tokens + audio_output_tokens, candidate_id, level))
        db.commit(); db.close()
        print(f"[AI_USAGE] c={candidate_id} L{level} {provider}/{model} {action} in={input_tokens} out={output_tokens} audio_in={audio_input_tokens} audio_out={audio_output_tokens} cached_in={cached_input_tokens} cached_audio_in={cached_audio_input_tokens} total={total_tokens} ~${cost_usd}")
    except Exception as e:
        print(f"UYARI (AI kullanım kaydı yazılamadı): {type(e).__name__}: {e}")

def record_openai_chat_usage(candidate_id: int, level: int, model: str, action: str, result: dict):
    usage = (result or {}).get("usage") or {}
    record_ai_usage(
        candidate_id=candidate_id,
        level=level,
        provider="openai",
        model=model,
        action=action,
        input_tokens=usage.get("prompt_tokens") or usage.get("input_tokens") or 0,
        output_tokens=usage.get("completion_tokens") or usage.get("output_tokens") or 0,
        raw=usage
    )

def record_anthropic_usage(candidate_id: int, level: int, model: str, action: str, response) -> None:
    """FAZ D: bir Anthropic (Claude) çağrısını ai_usage_logs'a AYRI SATIR olarak yazar ve
    interviews.total_*'ı BİR KEZ artırır (record_ai_usage üzerinden). add_token_usage'ın
    YERİNE geçer — ikisini birlikte çağırma, token'lar interviews.total_*'a çift eklenir.
    Mülakat turları (interview_chat) hâlâ add_token_usage kullanır; burası yalnızca ayrı
    kalem olarak görünmesi istenen çağrılar (ör. birincil rapor üretimi) içindir."""
    try:
        u = getattr(response, "usage", None)
        if not u:
            return
        fresh_in = getattr(u, "input_tokens", 0) or 0
        cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
        cache_create = getattr(u, "cache_creation_input_tokens", 0) or 0
        out_tok = getattr(u, "output_tokens", 0) or 0
        total_in = fresh_in + cache_read + cache_create  # record_ai_usage input_tokens = TAM girdi (cache dahil)
        record_ai_usage(
            candidate_id=candidate_id, level=level, provider="anthropic", model=model, action=action,
            input_tokens=total_in, output_tokens=out_tok,
            cached_input_tokens=cache_read,  # input_tokens'ın alt kümesi
            raw={"input_tokens": fresh_in, "cache_read_input_tokens": cache_read,
                 "cache_creation_input_tokens": cache_create, "output_tokens": out_tok},
        )
    except Exception as e:
        print(f"UYARI (record_anthropic_usage c={candidate_id} L{level}): {type(e).__name__}: {e}")

def record_realtime_usage_summary(candidate_id: int, level: int, model: str, summary: Optional[dict], action: str = "realtime_session_total_frontend"):
    if not summary:
        return
    # Tamamen sıfır bir delta (heartbeat'ler arasında hiç yeni response.done olmadıysa) için
    # boşuna satır açmayalım.
    if not any(_safe_int(summary.get(k)) for k in ("input_tokens", "output_tokens", "audio_input_tokens", "audio_output_tokens")):
        return
    record_ai_usage(
        candidate_id=candidate_id,
        level=level,
        provider="openai",
        model=model,
        action=action,
        input_tokens=summary.get("input_tokens", 0),
        output_tokens=summary.get("output_tokens", 0),
        audio_input_tokens=summary.get("audio_input_tokens", 0),
        audio_output_tokens=summary.get("audio_output_tokens", 0),
        cached_input_tokens=summary.get("cached_input_tokens", 0),
        cached_audio_input_tokens=summary.get("cached_audio_input_tokens", 0),
        raw=summary
    )

def record_realtime_events(candidate_id: int, level: int, events: Optional[list]):
    """Faz D1: ham Realtime olaylarını (session.created, speech_started/stopped, truncation,
    response.done) toplu kaydeder. Sadece gözlemlenebilirlik içindir — metrik hesaplama yok
    (Faz D2'nin işi). Hata olursa mülakat akışını bozmaz."""
    if not events:
        return
    try:
        db = get_db()
        for evt in events:
            if not isinstance(evt, dict) or not evt.get("type"):
                continue
            db.execute(
                "INSERT INTO realtime_events (candidate_id, level, event_type, event_data, elapsed_ms) VALUES (?, ?, ?, ?, ?)",
                (candidate_id, level, str(evt.get("type"))[:100], json.dumps(evt.get("data") or {}, ensure_ascii=False)[:4000], _safe_int(evt.get("elapsed_ms")))
            )
        db.commit(); db.close()
    except Exception as e:
        print(f"UYARI (realtime_events kaydı yazılamadı): {type(e).__name__}: {e}")

def record_flat_usage(candidate_id: int, level: Optional[int], provider: str, model: str, action: str,
                      minutes: float = 0.0, chars: int = 0, raw: Optional[dict] = None) -> None:
    """MADDE 5 — dakika/karakter bazlı AI kullanımını (Whisper transkripsiyon, OpenAI TTS)
    ai_usage_logs'a AYRI SATIR olarak yazar: token sütunları 0, estimated_cost_usd dolu.
    interviews.total_*_tokens'a DOKUNMAZ (bunlar token değil). Görünürlük amaçlı; tasarruf değil.
    Hata mülakat akışını bozmaz."""
    try:
        rate = AI_PRICING_FLAT.get((provider, model)) or {}
        if rate.get("unit") == "minute":
            units = max(0.0, float(minutes or 0))
        elif rate.get("unit") == "1k_char":
            units = max(0.0, (chars or 0) / 1000.0)
        else:
            units = 0.0
        cost_usd = round(units * float(rate.get("usd_per_unit", 0.0)), 4)
        db = get_db()
        db.execute("""
            INSERT INTO ai_usage_logs
            (candidate_id, level, provider, model, action, input_tokens, output_tokens, audio_input_tokens, audio_output_tokens,
             cached_input_tokens, cached_audio_input_tokens, total_tokens, estimated_cost_usd, raw_json)
            VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0, 0, 0, 0, ?, ?)
        """, (candidate_id, level, provider, model, action, cost_usd,
              json.dumps({**(raw or {}), "billed_minutes": round(float(minutes or 0), 3),
                          "billed_chars": int(chars or 0), "rate": rate}, ensure_ascii=False)[:8000]))
        db.commit(); db.close()
        print(f"[AI_USAGE flat] c={candidate_id} L{level} {provider}/{model} {action} min={float(minutes or 0):.2f} chars={chars} ~${cost_usd}")
    except Exception as e:
        print(f"UYARI (record_flat_usage c={candidate_id} L{level} {action}): {type(e).__name__}: {e}")

def _realtime_candidate_speech_seconds(candidate_id: int, level: int) -> float:
    """realtime_events'teki speech_started/stopped çiftlerinden adayın toplam konuşma süresini
    (= Whisper'ın realtime input_audio_transcription ile transkript ettiği süre) saniye verir.
    compute_voice_metrics'in aksine cevapsız/halüsinasyon turları da dahildir — Whisper onları
    da transkript eder, dolayısıyla maliyeti de doğar."""
    try:
        db = get_db()
        rows = db.execute(
            "SELECT event_type, elapsed_ms FROM realtime_events WHERE candidate_id=? AND level=? "
            "AND event_type IN ('input_audio_buffer.speech_started','input_audio_buffer.speech_stopped') "
            "ORDER BY elapsed_ms ASC, id ASC", (candidate_id, level)
        ).fetchall()
        db.close()
    except Exception as e:
        print(f"UYARI (_realtime_candidate_speech_seconds c={candidate_id}): {type(e).__name__}: {e}")
        return 0.0
    starts = [_safe_int(r["elapsed_ms"]) for r in rows if str(r["event_type"]).endswith("speech_started")]
    stops = [_safe_int(r["elapsed_ms"]) for r in rows if str(r["event_type"]).endswith("speech_stopped")]
    total_ms, si = 0, 0
    for s in starts:
        while si < len(stops) and stops[si] < s:
            si += 1
        if si < len(stops):
            total_ms += max(0, stops[si] - s)
            si += 1
    return round(total_ms / 1000.0, 1)

def backfill_realtime_cost_from_events(candidate_id: int, level: int, model: str) -> None:
    """MADDE 6 — Realtime maliyeti tamamen frontend'in usage_delta'sına bağlı; heartbeat hatası
    veya sekmenin erken kapanması durumunda o pay HİÇ kaydedilmez. Bu fonksiyon realtime_events'teki
    ham response.done usage'ından TOPLAMI çıkarır, ai_usage_logs'ta kayıtlı realtime toplamıyla
    karşılaştırır ve ciddi bir eksik varsa FARKI 'realtime_backfill' action'ıyla yazar.
    ÇİFT SAYIM KORUMASI: (a) zaten bir realtime_backfill satırı varsa hiç çalışmaz;
    (b) tam toplamı değil yalnızca (events − logged) eksik farkını yazar;
    (c) önemsiz farkı (ölçüm gürültüsü) yok sayar."""
    db = get_db()
    try:
        if db.execute("SELECT 1 FROM ai_usage_logs WHERE candidate_id=? AND level=? AND action='realtime_backfill' LIMIT 1",
                      (candidate_id, level)).fetchone():
            return
        ev_rows = db.execute("SELECT event_data FROM realtime_events WHERE candidate_id=? AND level=? AND event_type='response.done'",
                             (candidate_id, level)).fetchall()
        # NOT: LIKE deseni PARAMETRE olarak veriliyor (SQL'e gömülü '%' değil) — psycopg3'te
        # gömülü literal '%' parametreli sorguda sorun çıkarır; sqlite'ta da bu biçim güvenli.
        logged = db.execute(
            "SELECT COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o, "
            "COALESCE(SUM(audio_input_tokens),0) ai, COALESCE(SUM(audio_output_tokens),0) ao "
            "FROM ai_usage_logs WHERE candidate_id=? AND level=? AND provider='openai' AND action LIKE ?",
            (candidate_id, level, "realtime%")).fetchone()
    except Exception as e:
        print(f"UYARI (backfill_realtime_cost_from_events fetch c={candidate_id}): {type(e).__name__}: {e}")
        return
    finally:
        db.close()

    ev = {"i": 0, "o": 0, "ai": 0, "ao": 0, "ci": 0, "cai": 0}
    for r in ev_rows:
        try:
            u = (json.loads(r["event_data"]) or {}).get("usage") or {}
        except Exception:
            continue
        din = u.get("input_token_details") or u.get("input_tokens_details") or {}
        dout = u.get("output_token_details") or u.get("output_tokens_details") or {}
        cdet = din.get("cached_tokens_details") or din.get("cached_tokens_detail") or {}
        it = _safe_int(u.get("input_tokens")) or _safe_int(u.get("prompt_tokens"))
        ot = _safe_int(u.get("output_tokens")) or _safe_int(u.get("completion_tokens"))
        a_in, a_out = _safe_int(din.get("audio_tokens")), _safe_int(dout.get("audio_tokens"))
        c_tot, c_aud = _safe_int(din.get("cached_tokens")), _safe_int(cdet.get("audio_tokens"))
        c_txt = _safe_int(cdet.get("text_tokens")) if cdet.get("text_tokens") is not None else max(0, c_tot - c_aud)
        ev["i"] += max(0, it - a_in)
        ev["o"] += max(0, ot - a_out)
        ev["ai"] += a_in
        ev["ao"] += a_out
        ev["ci"] += c_txt
        ev["cai"] += c_aud
    ev_total = ev["i"] + ev["o"] + ev["ai"] + ev["ao"]
    if ev_total <= 0:
        return
    miss_i = max(0, ev["i"] - _safe_int(logged["i"]))
    miss_o = max(0, ev["o"] - _safe_int(logged["o"]))
    miss_ai = max(0, ev["ai"] - _safe_int(logged["ai"]))
    miss_ao = max(0, ev["ao"] - _safe_int(logged["ao"]))
    missing_total = miss_i + miss_o + miss_ai + miss_ao
    if missing_total < max(2000, int(ev_total * 0.10)):
        return  # önemsiz fark — çift sayım riskine değmez
    cached_i, cached_ai = min(miss_i, ev["ci"]), min(miss_ai, ev["cai"])
    record_ai_usage(candidate_id, level, "openai", model, "realtime_backfill",
                    input_tokens=miss_i, output_tokens=miss_o,
                    audio_input_tokens=miss_ai, audio_output_tokens=miss_ao,
                    cached_input_tokens=cached_i, cached_audio_input_tokens=cached_ai,
                    raw={"source": "realtime_events response.done yedek kaydı",
                         "events_total": ev,
                         "logged_total": {k: _safe_int(logged[k]) for k in ("i", "o", "ai", "ao")},
                         "missing_written": {"i": miss_i, "o": miss_o, "ai": miss_ai, "ao": miss_ao}})
    print(f"[REALTIME_BACKFILL] c={candidate_id} L{level} eksik yazıldı: "
          f"{miss_i + miss_o} metin + {miss_ai} ses-in + {miss_ao} ses-out token")

def save_interview_state(db, candidate_id: int, messages: list, level: int = 1):
    compact = build_compact_memory(messages)
    q_count = sum(1 for m in messages if m.get("role") == "assistant" and "---RAPOR---" not in (m.get("content") or ""))
    db.execute(
        "UPDATE interviews SET messages=?, compact_memory=?, question_count=? WHERE candidate_id=? AND level=?",
        (json.dumps(messages, ensure_ascii=False), compact, q_count, candidate_id, level)
    )

# ============ BÖLÜM 2/3 — TRANSKRİPT GÖRÜNÜMÜ + SONUÇ GEREKÇESİ ============
def _now_ts() -> str:
    """Duvar-saati ISO zaman damgası (saniye çözünürlüğü) — mesaj/olay kayıtları için."""
    return datetime.now().isoformat(timespec="seconds")

_VOICE_LINE_RE = re.compile(r'^\s*\[(\d{1,3}):(\d{2})\]\s*(Aday|Mülakatçı|Adam)\s*:\s*(.*)$')

# NOT: Python str.lower() Türkçe 'İ'yi 'i̇' (i + combining dot) yapar; bu yüzden 'SİSTEM' araması
# .lower() ile GÜVENİLMEZ — ham metinde regex ile ara. TEK KAYNAK: transkript görünümü, rapor
# temizliği ve ses metriği hepsi bunu kullanır (KALEM 5 + KALEM 2).
_HALL_MARKER_RE = re.compile(r"\[\s*S[İIıi]STEM\s*:")

def is_hallucination_marker_line(text: str) -> bool:
    t = text or ""
    return bool(_HALL_MARKER_RE.search(t)
                and re.search(r"hal[üu]sinasyon|ADAY\s+CEVAB[İIıi]\s+SAYMA", t, re.IGNORECASE))

def _parse_iso(value):
    try:
        return datetime.fromisoformat(str(value).split(".")[0].replace("T", " ").replace(" ", "T"))
    except Exception:
        return None

def build_transcript_view(messages_raw, level: int, started_at=None, for_report: bool = False) -> list:
    """interviews.messages'ı (L1/L3 dizi VEYA L2 tek blob) ortak bir görünüme çevirir:
    [{"role": "aday"|"mulakatci", "ts": "<iso|mm:ss|'' >", "text": "...", "elapsed_ms": int|None}].
    Rapor/[MÜLAKATBİTTİ] bloğu içeren mesajlar atlanır. Hata olursa boş liste döner.
    for_report=True: KALEM 5 — iç sistem işaretli satırlar ('[SİSTEM: ... halüsinasyon ...]')
    rapor/PDF görünümünden TAMAMEN çıkarılır (ham messages değişmez, model girdisi etkilenmez)."""
    try:
        msgs = json.loads(messages_raw) if isinstance(messages_raw, str) else (messages_raw or [])
    except Exception:
        return []
    _is_system_line = is_hallucination_marker_line
    anchor = _parse_iso(started_at) if started_at else None
    out = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        content = (m.get("content") or "").strip()
        if not content:
            continue
        # L2 tek-blob: içinde "[mm:ss] Rol:" ya da "Aday:/Mülakatçı:" satırları
        if ("\nAday:" in ("\n" + content)) or ("\nMülakatçı:" in ("\n" + content)) or _VOICE_LINE_RE.match(content.split("\n", 1)[0]):
            for line in content.split("\n"):
                line = line.strip()
                if not line:
                    continue
                mm = _VOICE_LINE_RE.match(line)
                if mm:
                    secs = int(mm.group(1)) * 60 + int(mm.group(2))
                    role = "aday" if mm.group(3).startswith("Ada") else "mulakatci"
                    out.append({"role": role, "ts": f"{mm.group(1)}:{mm.group(2)}", "text": mm.group(4).strip(), "elapsed_ms": secs * 1000})
                elif line.startswith("Aday:") or line.startswith("Mülakatçı:"):
                    role = "aday" if line.startswith("Aday:") else "mulakatci"
                    out.append({"role": role, "ts": "", "text": line.split(":", 1)[1].strip(), "elapsed_ms": None})
                else:
                    out.append({"role": "aday", "ts": "", "text": line, "elapsed_ms": None})
            continue
        # L1/L3 dizi
        role_raw = m.get("role")
        if role_raw not in ("user", "assistant"):
            continue
        if "---RAPOR---" in content or "[MÜLAKATBİTTİ]" in content:
            continue
        ts = m.get("ts") or ""
        elapsed_ms = None
        dt = _parse_iso(ts) if ts else None
        if dt and anchor:
            elapsed_ms = max(0, int((dt - anchor).total_seconds() * 1000))
        clean_text = re.sub(r'\[SÜRE:\d+\]\s*', '', content).replace("[ADAY_CIKIS_TALEBI]", "").strip()
        out.append({"role": "aday" if role_raw == "user" else "mulakatci", "ts": ts, "text": clean_text, "elapsed_ms": elapsed_ms})
    if for_report:
        out = [r for r in out if not _is_system_line(r.get("text"))]
    return out

def transcript_to_text(view: list) -> str:
    """build_transcript_view çıktısını indirilebilir düz metne çevirir."""
    lines = []
    for row in view:
        who = "Aday" if row["role"] == "aday" else "Mülakatçı"
        stamp = f"[{row['ts']}] " if row.get("ts") else ""
        lines.append(f"{stamp}{who}: {row['text']}")
    return "\n".join(lines)

_VIOLATION_DESC = {
    "tab_switch": "Aday başka bir sekme/uygulamaya geçti",
    "prolonged_absence": "Aday 2 dakikadan uzun süre mülakat ekranına dönmedi",
    "camera_off": "Kamera görüntüsü kapandı veya kesildi",
    "uygunsuz_davranis": "Görüşme kurallarına aykırı / uygunsuz davranış",
    "aday_talebi": "Aday mülakatı sonlandırmak istedi",
    "baglanti_koptu": "Bağlantı koptu",
}

def _nearest_snapshot_id(candidate_id: int, elapsed_ms) -> Optional[int]:
    """Verilen ana (elapsed_ms) zaman olarak en yakın kamera karesini bulur (mimik kareleri hariç)."""
    try:
        db = get_db()
        rows = db.execute(
            "SELECT id, elapsed_ms FROM snapshots WHERE candidate_id=? AND (reason IS NULL OR reason<>'mimic_sample') ORDER BY id ASC",
            (candidate_id,)
        ).fetchall()
        db.close()
    except Exception as e:
        print(f"UYARI (_nearest_snapshot_id c={candidate_id}): {type(e).__name__}: {e}")
        return None
    if not rows:
        return None
    if elapsed_ms is None:
        return rows[-1]["id"]
    best = min(rows, key=lambda r: abs((_safe_int(r["elapsed_ms"]) or 0) - _safe_int(elapsed_ms)))
    return best["id"]

def _append_result_event(candidate_id: int, level: int, event: dict, skip_if_any: bool = False) -> None:
    """interviews.result_events_json dizisine yapılandırılmış bir olay ekler. En iyi çaba;
    hata olursa mülakat/rapor akışını bozmaz. skip_if_any=True ise zaten kayıt varsa hiçbir
    şey yapmaz (jenerik kaydın, ayrıntılı kaydın üzerine yığılmasını önler)."""
    try:
        db = get_db()
        row = db.execute("SELECT result_events_json FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, level)).fetchone()
        current = []
        if row and row["result_events_json"]:
            try:
                current = json.loads(row["result_events_json"]) or []
            except Exception:
                current = []
        if skip_if_any and current:
            db.close()
            return
        event.setdefault("occurred_at", _now_ts())
        current.append(event)
        db.execute("UPDATE interviews SET result_events_json=? WHERE candidate_id=? AND level=?",
                   (json.dumps(current, ensure_ascii=False)[:12000], candidate_id, level))
        db.commit(); db.close()
    except Exception as e:
        print(f"UYARI (_append_result_event c={candidate_id} L{level}): {type(e).__name__}: {e}")

def _set_result_meta(candidate_id: int, level: int, **fields) -> None:
    """partial / completion_pct / technical_error_ref alanlarını yazar; result_reason yalnızca
    HÂLÂ BOŞ ise yazılır (daha spesifik bir gerekçe — ör. ihlal kaydı — üzerine yazılmasın).
    En iyi çaba."""
    allowed = ("result_reason", "partial", "completion_pct", "technical_error_ref")
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not sets:
        return
    try:
        db = get_db()
        parts, vals = [], []
        for k, v in sets.items():
            if k == "result_reason":
                parts.append("result_reason=COALESCE(NULLIF(result_reason, ''), ?)")
            else:
                parts.append(f"{k}=?")
            vals.append(v)
        db.execute(f"UPDATE interviews SET {', '.join(parts)} WHERE candidate_id=? AND level=?",
                   vals + [candidate_id, level])
        db.commit(); db.close()
    except Exception as e:
        print(f"UYARI (_set_result_meta c={candidate_id} L{level}): {type(e).__name__}: {e}")

def _ensure_result_reason(candidate_id: int, level: int, score, recommendation, terminated_reason) -> None:
    """BÖLÜM 3.1 — gerekçesiz olumsuz sonuç yasağı. Sonuç olumsuz (İhlal / Değerlendirilemedi /
    çok düşük puan) ama ne result_reason ne de olay kaydı varsa, açık bir "EKSİK" işareti bırak;
    admin ekranı bunu kırmızı banner olarak gösterir."""
    negative = bool(terminated_reason) or (recommendation == "Değerlendirilemedi") or (score is not None and score < 20)
    if not negative:
        return
    try:
        db = get_db()
        row = db.execute("SELECT result_reason, result_events_json FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, level)).fetchone()
        db.close()
    except Exception as e:
        print(f"UYARI (_ensure_result_reason fetch c={candidate_id}): {type(e).__name__}: {e}")
        return
    has_reason = bool(row and (row["result_reason"] or "").strip())
    has_events = False
    if row and row["result_events_json"]:
        try:
            has_events = len(json.loads(row["result_events_json"]) or []) > 0
        except Exception:
            has_events = False
    if not has_reason and not has_events:
        _set_result_meta(candidate_id, level,
                         result_reason="[EKSİK — sistem bu olumsuz sonuç için gerekçe üretemedi; transkripti inceleyin]")

# ============ FILE PARSING ============
# KALEM 1 — CV metni "boşluksuz yapışık" (ör. "SerbestMuhasebeciMaliMüşavir") çıkma sorunu.
# KÖK NEDEN: bazı PDF'lerde kelime araları pdfminer/pdfplumber'ın varsayılan x_tolerance'ının
# (3pt) altında kalıyor ya da gömülü fontta gerçek boşluk glifi yok → tek kelimeye yapışıyor.
# Kütüphane davranışı; sonradan bir normalize adımı DEĞİL (kodda CV metnini sıkıştıran bir yer yok).
# ÇÖZÜM: birden çok çıkarım stratejisi dene, her birini okunabilirlik puanıyla ölç, en iyisini seç.
def cv_text_readability(text: str) -> dict:
    """Çıkarılan metnin 'yapışıklık' ölçümü. avg_token_len yüksek + space_ratio düşük → bozuk."""
    t = (text or "").strip()
    if not t:
        return {"ok": False, "space_ratio": 0.0, "avg_token_len": 0.0, "long_token_ratio": 1.0, "score": 0.0}
    letters = sum(1 for ch in t if ch.isalpha())
    spaces = sum(1 for ch in t if ch == " ")
    space_ratio = spaces / max(1, letters + spaces)
    tokens = [w for w in re.split(r"\s+", t) if w]
    avg_token_len = (sum(len(w) for w in tokens) / len(tokens)) if tokens else 0.0
    long_tokens = sum(1 for w in tokens if len(w) >= 18)
    long_token_ratio = long_tokens / max(1, len(tokens))
    # Sağlıklı düz yazıda space_ratio ~0.14-0.18, avg_token_len ~5-7. Puan: yüksek = daha okunabilir.
    score = space_ratio - max(0.0, (avg_token_len - 8) * 0.03) - long_token_ratio * 0.5
    ok = space_ratio >= 0.09 and avg_token_len <= 12 and long_token_ratio <= 0.04
    return {"ok": ok, "space_ratio": round(space_ratio, 4), "avg_token_len": round(avg_token_len, 2),
            "long_token_ratio": round(long_token_ratio, 4), "score": round(score, 4)}

def _desqueeze_text(text: str) -> str:
    """SON ÇARE: metin ciddi biçimde yapışıksa (boşluk oranı çok düşük) küçük harf→BÜYÜK harf ve
    harf→rakam sınırlarına boşluk koyarak KISMİ kelime ayrımı kurtar. Yalnızca zaten bozuk metne
    uygulanır; sağlam metne dokunulmaz (çağıran okunabilirlik puanıyla karşılaştırır)."""
    out = re.sub(r"(?<=[a-zçğıöşü])(?=[A-ZÇĞİÖŞÜ])", " ", text)
    out = re.sub(r"(?<=[A-Za-zÇĞİÖŞÜçğıöşü])(?=\d)", " ", out)
    out = re.sub(r"(?<=\d)(?=[A-Za-zÇĞİÖŞÜçğıöşü])", " ", out)
    return out

# ═══ B1 — SÖZLÜK TABANLI SEGMENTASYON (harici bağımlılık YOK) ═══
# Yapışık CV metnini ("SerbestMuhasebeciMaliMüşavir") gömülü Türkçe kelime listesiyle böler.
# Kapsam: sık Türkçe fonksiyon/bağlaç kelimeleri + iş / muhasebe-finans / klinik araştırma /
# genel teknik terimler. Yalnız GEREKÇELİ kullanım: segmentasyon okunabilirlik puanını
# artırıyorsa uygulanır; aksi hâlde ham metin korunur (çağıran karşılaştırır).
_TR_WORDLIST = set(w.lower() for w in (
    # fonksiyon / bağlaç / sık kelimeler
    "ve","veya","ile","için","gibi","kadar","göre","ancak","fakat","ama","çünkü","daha","çok","az",
    "olarak","olan","olup","oldu","olduğu","olmak","yani","hem","ya","de","da","ki","mi","bu","şu","o",
    "bir","iki","üç","dört","beş","altı","yedi","sekiz","dokuz","on","yıl","yıla","yıllık","yakın","sonra",
    "önce","bugün","şu an","süre","süresi","boyunca","tüm","her","bazı","tek","aynı","farklı","yeni","eski",
    "büyük","küçük","genel","özel","temel","ileri","orta","tam","yarı","üst","alt","iç","dış","ön","arka",
    "sağ","sol","yüksek","düşük","hızlı","yavaş","doğru","yanlış","iyi","kötü","güçlü","zayıf",
    "ben","sen","biz","siz","onlar","kendi","şey","kişi","kişiler","adet","tane","dahil","hariç",
    "başlangıç","bitiş","devam","tamam","evet","hayır","belki","kesin","yaklaşık","toplam","kısmi",
    "bin","milyon","milyar","yüzde","adres","telefon","eposta","email","tarih","gün","ay","hafta",
    # kişisel / CV
    "serbest","meslek","mesleki","müşavir","mali","muhasebeci","muhasebe","muhasebecilik","kariyer",
    "deneyim","deneyimli","tecrübe","tecrübeli","eğitim","öğrenim","lisans","önlisans","yükseklisans",
    "doktora","lise","ticaret","üniversite","üniversitesi","fakülte","fakültesi","bölüm","bölümü",
    "mezun","mezunu","okul","kurs","sertifika","sertifikası","staj","stajyer","referans","özgeçmiş",
    "işletme","iktisat","ekonomi","maliye","hukuk","mühendislik","mühendisi","yönetim","yönetimi","idari",
    "kişisel","bilgiler","yetkinlik","yetkinlikler","beceri","beceriler","dil","diller","ingilizce","almanca",
    # muhasebe / finans
    "hesap","hesabı","hesaplar","plan","planı","planlama","kayıt","kayıtları","kayıtlar","yevmiye","defter",
    "defteri","bilanço","gelir","gider","tablo","tablosu","tabloları","borç","alacak","bakiye","cari","kasa",
    "banka","bankası","mutabakat","mutabakatı","denetim","denetimi","fatura","faturası","irsaliye","tahsilat",
    "ödeme","ödemeler","tahakkuk","reeskont","amortisman","karşılık","karşılıklar","değerleme","kur","döviz",
    "dönem","dönemi","kapanış","açılış","beyanname","beyannamesi","muhtasar","damga","katma","değer","vergi",
    "vergisi","vergileri","stopaj","tevkifat","bildirim","bildirimler","form","formu","formlar","efatura",
    "earşiv","edefter","berat","bordro","bordrosu","sigorta","prim","işçilik","personel","özlük","bütçe",
    "bütçeleme","nakit","akış","akışı","raporlama","rapor","raporu","raporlar","finansal","finans","yatırım",
    "kredi","faiz","gelir tablosu","maliyet","maliyeti","maliyetler","stok","stoklar","envanter","sayım",
    "konsolidasyon","konsolide","oran","oranı","analiz","analizi","kontrol","kontrolü","kontroller","onay",
    "iç kontrol","mevzuat","mevzuatı","standart","standartları","uyum","uyumluluk","tutar","tutarı","tutarsızlık",
    "logo","mikro","netsis","luca","zirve","excel","erp","yazılım","yazılımı","sistem","sistemi","sistemleri",
    "pivot","formül","fonksiyon","tablo kurma","program","programı","modül","modülü","entegrasyon","aktarım",
    "işler","işlem","işlemler","işlemleri","süreç","süreçler","süreçleri","takip","takibi","yönetici","uzman",
    "uzmanı","sorumlu","sorumlusu","asistan","asistanı","şef","şefi","müdür","müdürü","departman","departmanı",
    # klinik araştırma
    "klinik","araştırma","araştırması","çalışma","çalışması","protokol","protokolü","hasta","hastalar","ziyaret",
    "ziyareti","monitör","monitörü","monitoring","koordinatör","koordinatörü","koordinasyon","saha","merkez",
    "merkezi","sponsor","sponsoru","etik","kurul","kurulu","onam","gönüllü","ilaç","farmakovijilans","güvenlilik",
    "advers","olay","raporu","laboratuvar","numune","örneklem","veri","verileri","girişi","doğrulama","kalite",
    "güvence","regülasyon","regülatif","başvuru","dosya","dosyası","doküman","dokümantasyon","arşiv","arşivleme",
    # genel iş / teknik
    "proje","projesi","projeler","projelerin","ekip","ekibi","takım","takımı","müşteri","müşteriler","müşterinin",
    "hedef","hedefler","hedefleri","strateji","stratejisi","stratejik","pazar","pazarlama","satış","satışlar",
    "sunum","sunumu","iletişim","iletişimi","koordine","organizasyon","organize","planladım","yürüttüm","hazırladım",
    "geliştirme","geliştirdim","uyguladım","yönettim","sağladım","gerçekleştirdim","oluşturdum","kurdum","destek",
    "operasyon","operasyonel","performans","verimlilik","risk","riskleri","çözüm","çözümü","sorun","sorunları",
    "karar","kararı","kararlar","toplantı","toplantılar","sunucu","veritabanı","kod","test","testi","yazılımcı",
    "geliştirici","mühendis","analist","danışman","danışmanlık","şirket","şirketi","firma","firması","kurum","kurumu",
)) | set(w.lower() for w in ("smmm","ymm","kdv","sgk","ba","bs","poc","crf","edc","ctms","gcp","sop","ae","sae"))
_TR_MAXW = max(len(w) for w in _TR_WORDLIST if " " not in w)

def _segment_alpha_run(run: str) -> list:
    """Yalnız harflerden oluşan boşluksuz dizi → greedy longest-match ile kelime parçaları.
    Çözülemeyen kısımlar tek parça olarak kalır."""
    pieces, i, n, buf = [], 0, len(run), ""
    while i < n:
        best = 0
        for L in range(min(_TR_MAXW, n - i), 2, -1):
            if run[i:i + L].lower() in _TR_WORDLIST:
                best = L
                break
        if best:
            if buf:
                pieces.append(buf); buf = ""
            pieces.append(run[i:i + best]); i += best
        else:
            buf += run[i]; i += 1
    if buf:
        pieces.append(buf)
    return [p for p in pieces if p]

_URLISH_RE = re.compile(r"://|www\.|@|\b[\w.-]+\.(?:com|net|org|io|gov|edu|tr|co)\b", re.IGNORECASE)

def _segment_run(run: str) -> str:
    """Boşluksuz bir parçayı böler. Önce noktalama/rakam sınırlarından ayırır, sonra her SAF HARF
    dizisine sözlük segmentasyonu uygular. E-posta/URL benzeri parçalara dokunmaz."""
    if _URLISH_RE.search(run):
        return run
    out = []
    for tok in re.findall(r"[A-Za-zÇĞİÖŞÜçğıöşü]+|[^A-Za-zÇĞİÖŞÜçğıöşü]+", run):
        if tok and tok[0].isalpha() and len(tok) >= 10:
            out.extend(_segment_alpha_run(tok))
        else:
            out.append(tok)
    # parçaları boşlukla birleştir; ardışık noktalama parçalarını yapıştırmaya çalışma (basit ve yeterli)
    joined = " ".join(p for p in out if p)
    joined = re.sub(r"\s+([,.;:!?)\]}])", r"\1", joined)   # noktalamayı öne yapıştır
    joined = re.sub(r"([(\[{])\s+", r"\1", joined)
    return joined

def _dict_segment(text: str) -> str:
    """B1 — yapışık metni sözlükle böler. Yalnız 14+ karakterli boşluksuz parçalara uygulanır."""
    parts = re.split(r"(\s+)", text)
    for idx, part in enumerate(parts):
        if not part or part.isspace() or len(part) < 14:
            continue
        parts[idx] = _segment_run(part)
    return "".join(parts)

def _pdf_strategies(content: bytes):
    """Sırasıyla denenecek (etiket, çıkarım fonksiyonu) çiftleri."""
    def _plumber(x_tol=None, layout=False):
        import pdfplumber
        parts = []
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                kw = {}
                if x_tol is not None:
                    kw["x_tolerance"] = x_tol
                if layout:
                    kw["layout"] = True
                parts.append(page.extract_text(**kw) or "")
        return "\n".join(parts).strip()
    def _pdfminer():
        from pdfminer.high_level import extract_text as _mt
        from pdfminer.layout import LAParams
        return (_mt(io.BytesIO(content), laparams=LAParams(char_margin=1.5, word_margin=0.2, line_margin=0.4)) or "").strip()
    return [
        ("pdfplumber", lambda: _plumber()),
        ("pdfplumber-xtol1", lambda: _plumber(x_tol=1)),
        ("pdfplumber-layout", lambda: _plumber(layout=True)),
        ("pdfminer", _pdfminer),
    ]

def extract_text_from_pdf(content: bytes) -> str:
    best_text, best_score, best_label = "", -1e9, None
    errors = []
    for label, fn in _pdf_strategies(content):
        try:
            txt = fn()
        except Exception as e:
            errors.append(f"{label}: {type(e).__name__}: {e}")
            continue
        if not txt:
            continue
        r = cv_text_readability(txt)
        if r["score"] > best_score:
            best_text, best_score, best_label = txt, r["score"], label
        if r["ok"]:
            break
    if not best_text:
        return f"[PDF okunamadı: {'; '.join(errors) or 'metin çıkarılamadı'}]"
    r = cv_text_readability(best_text)
    # SON ÇARE (yapışık metin): sırayla dene, YALNIZ okunabilirlik puanını artıran adımı uygula.
    #  1) sözlük tabanlı segmentasyon (B1)  2) camelCase/harf-rakam sınırı (_desqueeze)
    if r["space_ratio"] < 0.12:
        for _lbl, _fn in (("dictseg", _dict_segment), ("desqueeze", _desqueeze_text)):
            try:
                cand = _fn(best_text)
            except Exception as e:
                print(f"UYARI (CV {_lbl} c=?): {type(e).__name__}: {e}")
                continue
            if cand != best_text and cv_text_readability(cand)["score"] > r["score"]:
                best_text, best_label = cand, (best_label or "?") + "+" + _lbl
                r = cv_text_readability(best_text)
    if not r["ok"]:
        print(f"[CV_EXTRACT_LOW_READABILITY] strateji={best_label} "
              f"space_ratio={r['space_ratio']} avg_token_len={r['avg_token_len']} long_token_ratio={r['long_token_ratio']} "
              f"— CV metni yapışık/okunması zor olabilir, çıkarım kalitesi düşük.")
    return best_text

def extract_text_from_docx(content: bytes) -> str:
    try:
        import docx
        doc = docx.Document(io.BytesIO(content))
        return "\n".join(p.text for p in doc.paragraphs).strip()
    except Exception as e:
        return f"[Word dosyası okunamadı: {e}]"

def extract_cv_text(filename: str, content: bytes) -> str:
    lower = filename.lower()
    if lower.endswith(".pdf"):
        return extract_text_from_pdf(content)
    elif lower.endswith(".docx"):
        return extract_text_from_docx(content)
    return "[Desteklenmeyen dosya formatı]"

def cv_extraction_error(cv_text: Optional[str]) -> Optional[str]:
    """Yüklenen CV'den metin çıkarma sonucunu doğrular. Sorun varsa yüzeye gösterilecek NET,
    anlaşılır bir Türkçe mesaj döner (kaydı ENGELLEMEK için); sorun yoksa None. Bozuk/boş CV
    metninin sessizce kaydedilip sonradan mülakat promptuna sızmasını ve akışı kilitlemesini
    önler. Her arıza türü ayrı mesaj alır: okunamayan PDF, okunamayan Word, boş/görüntü CV."""
    t = (cv_text or "").strip()
    if not t:
        return "Yüklediğiniz dosyadan hiç metin okunamadı. Dosya boş olabilir veya taranmış görüntüden oluşuyor olabilir; lütfen metin tabanlı bir PDF ya da Word (.docx) dosyası yükleyin."
    if t.startswith("[PDF okunamadı"):
        return "Yüklediğiniz PDF dosyası okunamadı. Dosya bozuk olabilir ya da taranmış görüntüden oluşuyor olabilir; lütfen metin tabanlı bir PDF veya Word (.docx) dosyası yükleyin."
    if t.startswith("[Word dosyası okunamadı"):
        return "Yüklediğiniz Word dosyası okunamadı. Lütfen dosyayı kontrol edip tekrar deneyin veya PDF olarak yükleyin."
    if t.startswith("[Desteklenmeyen dosya formatı"):
        return "Yüklediğiniz dosya geçerli bir CV değil. Sadece PDF veya Word (.docx) dosyası yükleyebilirsiniz."
    if len(t) < 30:
        return "Yüklediğiniz dosyada yeterli okunabilir metin bulunamadı. Lütfen tam bir CV dosyası yükleyin."
    return None

# ============ MAIL ============
def send_invite_email(candidate_name: str, email: str, username: str, password: str, position: str):
    if not email:
        return False
    try:
        html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
            <div style="background: #1e3a5f; padding: 20px; text-align: center;">
                <h1 style="color: white; margin: 0;">MedeX SMO</h1>
                <p style="color: #7eb8f7; margin: 5px 0;">Mülakat Daveti</p>
            </div>
            <div style="padding: 30px; background: #f8fafc;">
                <p>Sayın <strong>{candidate_name}</strong>,</p>
                <p><strong>{position}</strong> pozisyonu için mülakata davet edildiniz.</p>
                <p>Giriş bilgileriniz:</p>
                <div style="background: white; padding: 15px; border-radius: 8px; border-left: 4px solid #1e3a5f;">
                    <p><strong>Kullanıcı Adı:</strong> {username}</p>
                    <p><strong>Şifre:</strong> {password}</p>
                </div>
                <div style="text-align: center; margin: 30px 0;">
                    <a href="{BASE_URL}/mulakat" style="background: #1e3a5f; color: white; padding: 14px 30px; border-radius: 8px; text-decoration: none; font-weight: bold;">
                        Mülakata Başla
                    </a>
                </div>
                <p style="color: #64748b; font-size: 13px;">Mülakat yaklaşık 15-20 dakika sürmektedir. Mülakat sırasında kamera açık olmalı ve başka sekmeye geçilmemelidir.</p>
            </div>
        </div>
        """
        # NOT: resend SDK'sının Emails.send()'i timeout kabul etmiyor (requests.request'i
        # timeout=None ile çağırıyor) — dış çağrılarda timeout zorunlu kuralı için Resend'in
        # REST uç noktasına doğrudan httpx ile, açık bir timeout'la gidiliyor.
        httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": FROM_EMAIL, "to": email, "subject": f"MedeX SMO - {position} Pozisyonu Mülakat Daveti", "html": html},
            timeout=20.0,
        ).raise_for_status()
        return True
    except Exception as e:
        print(f"Mail hatası: {e}")
        return False

def send_report_email(candidate_name, position, report, score, recommendation, standard_cv, terminated_reason=None):
    try:
        rec_color = "#22c55e" if recommendation == "İşe Al" else "#f59e0b" if recommendation == "Değerlendirmeye Al" else "#ef4444"
        term_html = f'<div style="background:#fef2f2;border:1px solid #ef4444;color:#ef4444;padding:12px;border-radius:8px;margin-bottom:16px;"><strong>⚠️ Mülakat ihlal nedeniyle sonlandırıldı:</strong> {terminated_reason}</div>' if terminated_reason else ""
        html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 700px; margin: 0 auto;">
            <div style="background: #1e3a5f; padding: 20px; text-align: center;">
                <h1 style="color: white; margin: 0;">MedeX SMO</h1>
                <p style="color: #7eb8f7;">Mülakat Raporu</p>
            </div>
            <div style="padding: 30px; background: #f8fafc;">
                <h2>{candidate_name} - {position}</h2>
                {term_html}
                <div style="display: flex; gap: 20px; margin: 20px 0;">
                    <div style="background: white; padding: 20px; border-radius: 8px; text-align: center; flex: 1;">
                        <div style="font-size: 36px; font-weight: bold; color: #1e3a5f;">{score}</div>
                        <div style="color: #64748b;">/ 100</div>
                    </div>
                    <div style="background: white; padding: 20px; border-radius: 8px; text-align: center; flex: 1;">
                        <div style="font-size: 18px; font-weight: bold; color: {rec_color};">{recommendation}</div>
                        <div style="color: #64748b;">Öneri</div>
                    </div>
                </div>
                <div style="background: white; padding: 20px; border-radius: 8px; white-space: pre-wrap; margin-bottom:20px;">
{report}
                </div>
                <h3 style="color:#1e3a5f;">Standart CV</h3>
                <div style="background: white; padding: 20px; border-radius: 8px; white-space: pre-wrap;">
{standard_cv}
                </div>
                <p style="color: #64748b; font-size: 12px; margin-top: 20px;">
                    Mülakat tarihi: {datetime.now().strftime("%d.%m.%Y %H:%M")}
                </p>
            </div>
        </div>
        """
        httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": FROM_EMAIL, "to": REPORT_EMAILS, "subject": f"Mülakat Raporu: {candidate_name} - {position}", "html": html},
            timeout=20.0,
        ).raise_for_status()
        return True
    except Exception as e:
        print(f"Rapor mail hatası: {e}")
        return False

# ============ MERKEZİ AI ÇAĞRI KATMANI + HATA SINIFLANDIRMA + HATA LOGU ============
# Amaç: OpenAI ve Anthropic'e giden her HTTP çağrısı tek yerden geçsin; hata sınıflandırılsın,
# adaya ASLA ham kod/status/teknik metin sızmasın, admin panelinde görünsün, kritik durumda
# anında e-posta gitsin, uygun sınıflarda otomatik retry yapılsın.

_RETRY_BACKOFF = [1, 3, 7]  # saniye — 3 deneme

# error_class -> adaya gösterilecek metin (İŞ EMRİ 1.4 tablosu). Backend'de "kota/bakiye/API/quota"
# kelimeleri bu sözlüğün dışına ÇIKMAZ; frontend yalnızca buradaki 'message'ı gösterir.
AI_ERROR_USER_MESSAGES = {
    "insufficient_quota":  "Mülakat şu anda başlatılamıyor. Lütfen yetkiliyle iletişime geçin.",
    "invalid_api_key":     "Mülakat şu anda başlatılamıyor. Lütfen yetkiliyle iletişime geçin.",
    "rate_limit_exceeded": "Sistem şu anda yoğun. Lütfen birkaç dakika sonra tekrar deneyin.",
    "server_error":        "Servise şu an ulaşılamıyor. Lütfen tekrar deneyin.",
    "network":             "Bağlantı kurulamadı. İnternet bağlantınızı kontrol edin.",
    "mic_permission":      "Mikrofon erişimi verilmedi. Tarayıcı ayarlarından izin verin.",
    "unknown":             "Beklenmeyen bir hata oluştu. Lütfen tekrar deneyin.",
}
_RETRYABLE_CLASSES = {"rate_limit_exceeded", "server_error", "network"}
_CRITICAL_CLASSES = {"insufficient_quota", "invalid_api_key"}

class AIError(Exception):
    """Sınıflandırılmış AI çağrı hatası. Route handler bunu yakalayıp uygun HTTP yanıtını üretir."""
    def __init__(self, error_class: str, provider: str, step: str, technical_detail: str,
                 retry_count: int = 0, http_status: int = 502):
        self.error_class = error_class
        self.provider = provider
        self.step = step
        self.technical_detail = (technical_detail or "")[:4000]
        self.retry_count = retry_count
        self.http_status = http_status
        self.user_message = AI_ERROR_USER_MESSAGES.get(error_class, AI_ERROR_USER_MESSAGES["unknown"])
        self.retryable = error_class in _RETRYABLE_CLASSES
        super().__init__(f"{provider}/{step}: {error_class}")

def _http_status_for_class(error_class: str) -> int:
    return {
        "insufficient_quota": 503, "invalid_api_key": 503,
        "rate_limit_exceeded": 429, "server_error": 502, "network": 504,
    }.get(error_class, 500)

def classify_ai_error(provider: str, status: Optional[int], body) -> str:
    """HTTP status + sağlayıcı hata kodunu BİRLİKTE okuyarak sınıflandırır.
    429 tek başına 'rate_limit' varsayılmaz — kod 'insufficient_quota' ise kota tükenmesidir."""
    code = ""
    try:
        b = body if isinstance(body, dict) else (json.loads(body) if isinstance(body, str) and body.strip().startswith("{") else {})
        err = (b.get("error") or {}) if isinstance(b, dict) else {}
        code = str(err.get("code") or err.get("type") or "").lower()
    except Exception:
        code = str(body or "").lower()
    blob = f"{code} {str(body or '')[:500]}".lower()
    if "insufficient_quota" in blob or "exceeded your current quota" in blob or "billing" in blob:
        return "insufficient_quota"
    if status in (401, 403) or "invalid_api_key" in blob or "authentication" in blob or "permission" in blob:
        return "invalid_api_key"
    if status == 429 or "rate_limit" in blob or "overloaded" in blob:
        return "rate_limit_exceeded"
    if (status is not None and status >= 500) or "server_error" in blob or "api_error" in blob:
        return "server_error"
    return "unknown"

def _human_error_message(provider: str, step: str, error_class: str) -> str:
    """Admin panelinde gösterilecek İNSAN DİLİNDE açıklama (teknik jargonsuz)."""
    who = "OpenAI" if provider == "openai" else ("Claude (AI mülakatçı)" if provider == "anthropic" else provider)
    step_tr = {
        "realtime_session": "sesli mülakat oturumu başlatılırken",
        "sdp_exchange": "sesli bağlantı kurulurken",
        "interview_start": "mülakat başlatılırken",
        "interview_chat": "mülakat sırasında yanıt üretilirken",
        "report_generation": "rapor üretilirken",
        "report_reviewer": "rapor ikinci-model denetimi sırasında",
        "mimic_analysis": "mimik analizi sırasında",
        "voice_transcribe": "ses metne çevrilirken",
        "voice_speak": "metin sese çevrilirken",
    }.get(step, "AI çağrısında")
    cls_tr = {
        "insufficient_quota": f"{who} kotası/bakiyesi tükendiği için",
        "invalid_api_key": f"{who} API anahtarı geçersiz veya yetkisiz olduğu için",
        "rate_limit_exceeded": f"{who} hız sınırı aşıldığı (sistem yoğun) için",
        "server_error": f"{who} servisine geçici olarak ulaşılamadığı için",
        "network": f"{who} servisine ağ bağlantısı kurulamadığı için",
        "unknown": f"{who} servisinde bilinmeyen bir hata oluştuğu için",
    }.get(error_class, f"{who} servisinde bir hata oluştuğu için")
    return f"{cls_tr} {step_tr} işlem tamamlanamadı."

def record_error_log(*, provider: str, step: str, error_class: str, technical_detail: str,
                     candidate_message: str = "", retry_count: int = 0, severity: str = "user",
                     candidate_id: Optional[int] = None, candidate_name: Optional[str] = None,
                     interview_id: Optional[int] = None, level: Optional[int] = None,
                     human_message: Optional[str] = None) -> Optional[int]:
    """Adaya bir hata gösterilen (veya arka planda oluşan) her durumu error_logs'a yazar.
    En iyi çaba — kayıt yazılamazsa mülakat akışı ETKİLENMEZ."""
    try:
        hm = human_message or _human_error_message(provider, step, error_class)
        cm = candidate_message or AI_ERROR_USER_MESSAGES.get(error_class, AI_ERROR_USER_MESSAGES["unknown"])
        db = get_db()
        insert_sql = """INSERT INTO error_logs
            (candidate_id, candidate_name, interview_id, level, provider, step, error_class, severity,
             human_message, candidate_message, technical_detail, retry_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        params = (candidate_id, candidate_name, interview_id, level, provider, step, error_class, severity,
                  hm, cm, (technical_detail or "")[:4000], retry_count)
        if USE_POSTGRES:
            new_id = db.execute(insert_sql + " RETURNING id", params).fetchone()["id"]
        else:
            db.execute(insert_sql, params)
            new_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.commit(); db.close()
        print(f"[ERROR_LOG #{new_id}] {severity} {provider}/{step} {error_class} — {hm}")
        return new_id
    except Exception as e:
        print(f"UYARI (record_error_log yazılamadı): {type(e).__name__}: {e}")
        return None

def maybe_send_critical_email(error_class: str, human_message: str, technical_detail: str,
                              candidate_name: Optional[str], log_id: Optional[int]) -> None:
    """Kota tükenmesi / geçersiz anahtar gibi KRİTİK hatalarda admin'e ANINDA e-posta.
    Aynı error_class için son 1 saatte e-posta gittiyse TEKRAR göndermez (spam koruması)."""
    if error_class not in _CRITICAL_CLASSES:
        return
    if not RESEND_API_KEY or not ERROR_ALERT_EMAILS:
        return
    try:
        db = get_db()
        recent = db.execute(
            "SELECT id FROM error_logs WHERE error_class=? AND email_sent_at IS NOT NULL AND email_sent_at > ? LIMIT 1",
            (error_class, (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds"))
        ).fetchone()
        db.close()
        if recent:
            print(f"[CRITICAL_EMAIL] {error_class} — 1 saat içinde zaten gönderildi, atlanıyor.")
            return
    except Exception as e:
        print(f"UYARI (kritik e-posta dedup kontrolü): {type(e).__name__}: {e}")

    subj_map = {
        "insufficient_quota": "MedeX Mülakat — OpenAI/Claude kotası tükendi, mülakatlar başlatılamıyor",
        "invalid_api_key": "MedeX Mülakat — API anahtarı geçersiz, mülakatlar başlatılamıyor",
    }
    subject = subj_map.get(error_class, "MedeX Mülakat — kritik servis hatası")
    who_line = f"Etkilenen aday: {candidate_name}" if candidate_name else "Bir aday mülakatı başlatamadı."
    action = ("OpenAI hesabının bakiyesini/kotasını yükleyin veya faturalandırmayı kontrol edin."
              if error_class == "insufficient_quota" else
              "OPENAI_API_KEY / ANTHROPIC_API_KEY ortam değişkenini kontrol edip güncelleyin.")
    html = f"""<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto">
      <div style="background:#b91c1c;padding:18px;text-align:center;color:#fff"><h2 style="margin:0">MedeX Mülakat — Kritik Hata</h2></div>
      <div style="padding:24px;background:#f8fafc">
        <p><strong>Ne oldu:</strong> {xml_escape(human_message)}</p>
        <p>{xml_escape(who_line)}</p>
        <p><strong>Zaman:</strong> {datetime.now().strftime('%d.%m.%Y %H:%M')}</p>
        <p><strong>Yapılması gereken:</strong> {xml_escape(action)}</p>
        <p style="color:#64748b;font-size:12px">Aday tarafında yalnızca nötr bir mesaj gösterildi; kota/API gibi bir ifade adaya iletilmedi.</p>
        <hr><p style="color:#94a3b8;font-size:11px;white-space:pre-wrap">Teknik detay (hata kaydı #{log_id}):\n{xml_escape((technical_detail or '')[:1500])}</p>
      </div></div>"""
    try:
        httpx.post("https://api.resend.com/emails",
                   headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                   json={"from": FROM_EMAIL, "to": ERROR_ALERT_EMAILS, "subject": subject, "html": html},
                   timeout=20.0).raise_for_status()
        if log_id:
            db = get_db()
            db.execute("UPDATE error_logs SET email_sent_at=? WHERE id=?", (_now_ts(), log_id))
            db.commit(); db.close()
        print(f"[CRITICAL_EMAIL] gönderildi: {error_class} -> {ERROR_ALERT_EMAILS}")
    except Exception as e:
        print(f"UYARI (kritik e-posta gönderilemedi): {type(e).__name__}: {e}")

def _handle_ai_failure(err: "AIError", context: Optional[dict], severity: str) -> "AIError":
    """AIError'ı error_logs'a yazar, kritik e-postayı tetikler ve err'i geri döner
    (route handler raise etsin diye). context: candidate_id/candidate_name/interview_id/level."""
    ctx = context or {}
    log_id = record_error_log(
        provider=err.provider, step=err.step, error_class=err.error_class,
        technical_detail=err.technical_detail, retry_count=err.retry_count, severity=severity,
        candidate_id=ctx.get("candidate_id"), candidate_name=ctx.get("candidate_name"),
        interview_id=ctx.get("interview_id"), level=ctx.get("level"),
    )
    if severity == "user":
        maybe_send_critical_email(err.error_class, _human_error_message(err.provider, err.step, err.error_class),
                                  err.technical_detail, ctx.get("candidate_name"), log_id)
    return err

def openai_call(method: str, url: str, *, json_body=None, files=None, data=None, headers_extra=None,
                timeout: float = 60.0, step: str = "openai", context: Optional[dict] = None,
                severity: str = "user", retry: bool = True) -> httpx.Response:
    """TÜM OpenAI HTTP çağrıları buradan geçer. Loglar, sınıflandırır, uygun sınıflarda retry yapar.
    Başarısızlıkta error_logs'a yazar + kritik e-postayı tetikler + AIError raise eder."""
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    if headers_extra:
        headers.update(headers_extra)
    attempts = (len(_RETRY_BACKOFF) + 1) if retry else 1
    last_err_class = "unknown"
    last_detail = ""
    for i in range(attempts):
        t0 = time.time()
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.request(method, url, headers=headers, json=json_body, files=files, data=data)
            dt = int((time.time() - t0) * 1000)
        except (httpx.TimeoutException, httpx.RequestError) as e:
            last_err_class = "network"
            last_detail = f"{type(e).__name__}: {e} | {method} {url}"
            print(f"[OPENAI_ERR] {step} network attempt={i+1}/{attempts}: {last_detail}")
            if retry and i < attempts - 1:
                time.sleep(_RETRY_BACKOFF[i]); continue
            raise _handle_ai_failure(AIError("network", "openai", step, last_detail, retry_count=i,
                                             http_status=504), context, severity)
        if resp.status_code < 400:
            print(f"[OPENAI_OK] {step} {resp.status_code} {dt}ms {method} {url}")
            return resp
        body_text = resp.text[:2000]
        last_err_class = classify_ai_error("openai", resp.status_code, body_text)
        last_detail = f"HTTP {resp.status_code} | {method} {url} | {body_text}"
        print(f"[OPENAI_ERR] {step} {last_err_class} HTTP {resp.status_code} attempt={i+1}/{attempts}: {body_text[:300]}")
        if retry and last_err_class in _RETRYABLE_CLASSES and i < attempts - 1:
            time.sleep(_RETRY_BACKOFF[i]); continue
        raise _handle_ai_failure(AIError(last_err_class, "openai", step, last_detail, retry_count=i,
                                         http_status=_http_status_for_class(last_err_class)), context, severity)
    # buraya normalde ulaşılmaz
    raise _handle_ai_failure(AIError(last_err_class, "openai", step, last_detail, retry_count=attempts - 1,
                                     http_status=_http_status_for_class(last_err_class)), context, severity)

def ai_error_from_anthropic(e, step: str, context: Optional[dict], severity: str = "user") -> "AIError":
    """anthropic SDK istisnasını sınıflandırıp AIError'a çevirir + loglar + kritik e-postayı tetikler."""
    status = getattr(e, "status_code", None)
    body = getattr(e, "body", None) or str(e)
    cls = classify_ai_error("anthropic", status, body)
    detail = f"anthropic {type(e).__name__} status={status}: {str(e)[:1500]}"
    return _handle_ai_failure(AIError(cls, "anthropic", step, detail, http_status=_http_status_for_class(cls)),
                              context, severity)

def ai_http_exception(err: "AIError") -> HTTPException:
    """AIError -> istemciye dönecek HTTPException. detail bir OBJE: {message, error_class, retryable}.
    Ham kod/status/teknik metin ASLA girmez."""
    return HTTPException(status_code=err.http_status, detail={
        "message": err.user_message, "error_class": err.error_class, "retryable": err.retryable,
    })

# ============ DAVET SÜRESİ + TEŞEBBÜS DURUMU (Mülakat Denemeleri ekranı) ============
def _invite_expiry() -> str:
    """Yeni davet oluşturulurken invite_expires_at değeri — bugünden INVITE_EXPIRY_DAYS gün sonra."""
    return (datetime.now() + timedelta(days=INVITE_EXPIRY_DAYS)).isoformat(timespec="seconds")

_ATTEMPT_STATUS_LABELS = {
    "sent": "Gönderildi",
    "opened_not_started": "Açıldı, başlatılmadı",
    "in_progress": "Devam ediyor",
    "partial": "Yarıda kaldı",
    "completed": "Tamamlandı",
    "terminated": "İhlal ile sonlandı",
    "tech_error": "Teknik hata",
    "expired": "Süresi doldu",
    "processing": "Rapor hazırlanıyor",
}

def derive_attempt_status(a: dict) -> dict:
    """Bir aday/mülakat satırından tek-kaynak teşebbüs durumu türetir.
    PDF, dashboard ve PersonDetail hepsi bunu kullanır. Girdi anahtarları get_person
    attempts sorgusundan gelir (candidates + interviews join)."""
    def g(k, default=None):
        v = a.get(k) if isinstance(a, dict) else None
        return v if v is not None else default

    interview_completed = bool(g("interview_completed_at"))
    proc = (g("processing_status") or "").lower()
    terminated = bool(g("terminated_reason"))
    partial = bool(g("partial"))
    completion_pct = _safe_int(g("completion_pct"), 0)
    tech_ref = g("technical_error_ref")
    login_count = _safe_int(g("login_count"), 0)
    start_count = _safe_int(g("interview_start_count"), 0)
    expires_at = _parse_iso(g("invite_expires_at"))

    status = "sent"
    if interview_completed:
        status = "terminated" if terminated else "completed"
    elif proc in ("processing", "pending"):
        status = "processing"
    elif proc == "failed" or tech_ref:
        status = "tech_error"
    elif terminated:
        status = "terminated"
    elif partial or (0 < completion_pct < 100):
        status = "partial"
    elif start_count > 0:
        status = "in_progress"
    elif login_count > 0:
        status = "opened_not_started"
    elif expires_at and datetime.now() > expires_at:
        status = "expired"

    label = _ATTEMPT_STATUS_LABELS.get(status, status)
    if status == "partial" and completion_pct:
        label = f"Yarıda kaldı (%{completion_pct})"

    return {
        "attempt_status": status,
        "attempt_status_label": label,
        "login_count": login_count,
        "interview_start_count": start_count,
        "first_login_at": g("first_login_at"),
        "last_login_at": g("last_login_at"),
        "last_start_at": g("last_start_at"),
        "invite_expires_at": g("invite_expires_at"),
        "completion_pct": completion_pct or None,
        "technical_error_ref": tech_ref,
    }

# ============ AI PROMPT ============
# KALEM 6 — mülakatçı davranışı: aynı soruyu üst üste sorma / anlaşılmadığında sadeleştirme /
# cevapsız-tur teyidi. TEK KAYNAK: hem get_system_prompt (L1/L3 metin) hem
# build_l2_realtime_instructions (L2/L3 ses) bu bloğu birebir kullanır.
INTERVIEWER_REASK_RULES = """SORU TEKRARI VE CEVAPSIZLIK — KESİN KURALLAR:
- Bir kriteri/konuyu hedefleyen soru EN FAZLA 2 kez sorulur. 2. deneme AYNI CÜMLE DEĞİL; kısalt, sadeleştir, somut bir örnekle yeniden ifade et.
- 2. deneme de cevapsız/kaçamak kalırsa "Bunu geçelim." de, o kriteri BIRAK ve yeni kritere geç. O kriter raporda "değerlendirilemedi (soruldu, cevap alınamadı)" işaretlenir — 3. kez ISRAR ETME.
- Aday "anlamadım / tekrar eder misiniz / pardon?" derse soruyu ASLA aynen tekrarlama: kısalt ve basitleştir, gerekirse tek bir örnek ver.
- Art arda 2 tur adayın cevabı BOŞ veya anlamsız/halüsinasyon (ör. "thank you", "bye", kopuk İngilizce dolgu) gelirse: yeni soruya GEÇME, önce teyit sorusu sor — "Beni duyabiliyor musunuz? Sesiniz bana net gelmiyor." Cevap gelince kaldığın yerden devam et."""

def build_l2_realtime_instructions(position_name: str, candidate_name: str, cv_text: Optional[str], ai_note: Optional[str], interview_language: str = "tr", depth_tier: Optional[str] = "standart", level: int = 2) -> str:
    """Rolü, dili, hitabı ve kapanışı kilitli profesyonel realtime mülakatçı talimatı.
    level=2 (varsayılan) çıktısı bilerek birebir eskisiyle aynı bırakıldı — sadece level=3
    çağrıldığında ek bir SEVİYE TONU satırı ve L3'ün kendi süre/derinlik hedefi devreye girer."""
    pos = get_position(position_name) or {"criteria": [{"name": "Genel Yetkinlik", "weight": 100, "desc": ""}], "role_description": ""}
    criteria = pos.get("criteria") or []
    role_desc = (pos.get("role_description") or "").strip()   # B6 — L1/L3'te olduğu gibi L2'ye de ver
    criteria_compact = "; ".join(f"{c.get('name','Kriter')} %{c.get('weight',0)}" for c in criteria)
    criteria_names = ", ".join(f'"{c.get("name", "Kriter")}"' for c in criteria)
    lang_name = LANGUAGE_NAMES.get(interview_language, "Türkçe")
    cv_compact = " ".join((cv_text or "").split())[:900] or "CV özeti yok"
    note_compact = " ".join((ai_note or "").split())[:500]
    lvl_cfg = get_effective_level_config(level, depth_tier)
    depth_label = lvl_cfg.get("depth_label", "Standart")
    # level=2 için bu satır boş kalır (çıktı eskisiyle birebir aynı); yalnızca level=3'te
    # LEVEL_CONFIG[3]'ün tonu ek bir talimat satırı olarak eklenir.
    level_tone_line = f"\nSEVİYE TONU: {LEVEL_CONFIG.get(level, LEVEL_CONFIG[2])['tone']}\n" if level == 3 else ""

    return f"""ROLÜN: Sen genel sohbet asistanı, öğretmen veya danışman değilsin — profesyonel iş mülakatçısısın. Rolünden çıkma.

MÜLAKATÇI İLKESİ — MERKEZ (kural listesi değil, karar çerçeven):
Amacın adayın mesleki bilgi ve yetkinlik düzeyini ölçmek. Bu amaca ulaşmak için TAM inisiyatif sendedir: senaryo/soru listesi takip etmezsin, adayı okur ve duruma göre karar verirsin. Amaca hizmet ettiğin sürece adayla tam uyumlu davranırsın. Amaç eleme değil, iyi adayı yakalamak.

Aday: {candidate_name}. Pozisyon: {position_name}. Derinlik: {depth_label}.
Rol: {role_desc or 'Bu pozisyon için genel yetkinlik değerlendirmesi.'}
Kriterler: {criteria_compact}. CV özeti: {cv_compact}. Özel not: {note_compact or 'yok'}.
{level_tone_line}
İNİSİYATİF:
- Sabit soru sırası/sayısı yok; adayın cevabına göre yön belirle. Zayıf alanı derinleştir, güçlü alanda oyalanma.
- Aday bir konuyu açtıysa oradan devam et; "listeye" dönmek için zorlama. Cevap yüzeyselse somut örnek/katkı/sonuç iste; doyurucuysa geç.
- Aynı konuyu amaçsız tekrar etme; önceki cevaba uygun takip sorusu üret. Adayın ne söylediğini unutma.
- Hedef ~{lvl_cfg['minutes']} dakika bir yön göstergesidir; parasal eşik nedeniyle asla bitirme, yeterli kanıt oluşana kadar doğal sürdür.

{INTERVIEWER_REASK_RULES}

KRİTER KAPSAMA: Akış içinde mekanik kapı yok. Ama end_interview çağırmadan ÖNCE kontrol et: hiç dokunulmamış kriter varsa en az bir soru sor. Yine de sorulamayan kalırsa raporda "değerlendirilmedi" işaretlenir — uydurma değerlendirme yapma.

İNSAN GİBİ, DOĞAL:
- Her turda tek, net soru. Uzun özet, gereksiz övgü, konu anlatımı, danışmanlık yapma — aday daha çok konuşsun.
- Dinlediğini belli et, adayın söylediğine bağlanarak devam et; kopuk soru dizisi sorma.
- Ton sıcak ve doğal; sorgu/karşılaşma havası YOK. Gerçek çelişki görsen bile meraklı sor ("az önce şunu, şimdi bunu dediniz — ikisini nasıl bir arada düşünüyorsunuz?"), "yalan mı söylüyorsunuz" gibi ima taşıyan ifade kullanma.
- Hitabı DOĞAL kullan. Her cümlede "... Bey/Hanım", her turda "siz" vurgusu, sabit "teşekkür ederim / şimdi şu soruyu soracağım" kalıpları YOK. "siz" diline kendiliğinden geç ama mekanik tekrar etme.
- Aday sen konuşurken gerçekten söze girerse sus ve dinle. TV, nefes, öksürük gibi kısa sesleri cevap sayma.

ADAYI OKU, UYUM SAĞLA:
- Sabit açılış cümlesi yok. Adayın sesindeki tonu, hızı, tereddüdü oku; açılışı ve tempoyu ona göre ayarla.
- Aday rahat ve netse kısa selamla konuya gir. Gergin/tereddütlüyse önce birkaç saniye sıcak bir rahatlatma yap, acele ettirme. Hızlı gitmek istiyorsa yavaşlatma; zorlanıyorsa yardım et; susarsa bekle.
- Bir kavramı bilmiyorsa öğretme: en fazla terimin tek cümlelik anlamını söyle, soruyu bir kez sadeleştir; hâlâ bilmiyorsa "Anladım, bu konuyu geçelim." de.
- Aday açıkça kendi mesleki alanının bu pozisyondan FARKLI olduğunu söylerse ısrar etme: alanını kısaca doğrula ve DEĞERLENDİRMEYİ ADAYIN GERÇEK ALANI için yürüt; bunu "pozisyon uyumsuzluğu" olarak nota geç.
- Uzun sessizlik ya da anlamsız/kopuk girdi gelirse önce TEKNİK TEYİT iste: "Sesiniz bana net gelmiyor gibi, beni duyabiliyor musunuz?" — cevap gelince kaldığın yerden devam et.

SÜRE VE AKIŞ BİLGİSİ: Adayın kendini konumlandırabilmesi için akışın birkaç doğal noktasında ÇOK KISA bir bilgi ver — "yaklaşık yarısındayız", "son birkaç soru", "birazdan tamamlıyoruz" gibi tek bir doğal ifade yeterli; aynı bilgiyi üç ayrı açıklayıcı cümleyle verme.

ADAYLA UYUM (kısıt yok):
- Soruyu tekrar isterse tekrar et, gerekçe sorma. Açıklama/örnek/yeniden ifade isterse ver.
- Bir terimin İngilizce/Türkçe karşılığını sorarsa SÖYLE. Meslek gerektiren İngilizce terimleri sektörde yaygın haliyle kullan — zorlama Türkçe çeviri YOK. Aday tamamen İngilizce cevap verirse engelleme, görüşme kesintisiz sürer.
- Konuşma hızı/üslubu/aksanı müdahale konusu değil.

KESİNLİKLE YAPMA: adayla herhangi bir konuda (özellikle dil) tartışmak; kullandığı terimi düzeltmek; kural gerekçesiyle bir talebini reddetmek; zorlama çeviri; mekanik/tekrarlayan hitap.

DAVRANIŞ — KESME YOK, GÖZLEM VAR: Aday agresif, sabırsız, kaba, alaycı, küfürlü veya kaçamak davransa BİLE mülakatı KESME. Bu davranışı note_voice_observation ile SOMUT kaydet (dakika + ne söylediği) ve nazikçe konuya dönerek devam et; rapordaki "Davranış ve Tutum Gözlemleri" bölümüne yansır. end_interview(reason='uygunsuz_davranis') SADECE gerçekten devam edilemez bir durumda çağrılır (cevabı sürekli başkası veriyor; aday tamamen iş birliğini kesti) — o durumda bile eldeki veriyle rapor üretilir.

DİL: Mülakatın odağı değil. Pozisyon bir dil yeterliliği gerektiriyorsa değerlendirmeye girebilir; gerektirmiyorsa yalnızca gözlem verisidir. Hiçbir durumda kesme/çekişme sebebi değil.

DERİNLİK: STANDART modda geniş kapsam + yeterli derinlik; DERİN modda daha fazla takip, kanıt, çapraz kontrol — daha çok senin konuşman değil, adayı daha derin sorgulaman demek.

KAPANIŞ PROTOKOLÜ ZORUNLUDUR:
1) Kriterlerin çoğu yeterince değerlendirildiğinde önce mutlaka “Mülakatımızı tamamlamadan önce son olarak eklemek veya özellikle belirtmek istediğiniz bir konu var mı?” diye sor.
2) Adayın son cevabını dinle.
3) Ardından kısa ve profesyonel biçimde teşekkür et: “Teşekkür ederim. Görüşmemiz burada tamamlandı. Katılımınız ve ayırdığınız zaman için teşekkür ederim.”
4) Yalnızca bu kapanış cümlesi tamamen bittikten sonra end_interview(reason='tamamlandı', criteria_coverage={{...}}) çağır: {criteria_names}.
- Aday AÇIKÇA bitirmek isterse (net sözlü talep: "bitirelim", "devam etmek istemiyorum") end_interview(reason='aday_talebi'). Sadece nezaketen sorulan "eklemek istediğiniz bir şey var mı" kapanış sorusuna "yok" demek bitirme talebi DEĞİLDİR — bu durumda reason='tamamlandı'.
- Kapanışta modele criteria_coverage'ı MUTLAKA doldur (her kriter için 0-100): rapor yanıtsız kriterleri buradan tespit ediyor.

SES GÖZLEMİ ARACI (note_voice_observation) — SIKI KULLANIM:
- Bu aracı YALNIZCA adayın SESİNDE rapora değecek, BELİRGİN bir şey fark ettiğinde çağır: net tereddüt, akıcılık kaybı, tonda belirgin kayma, aşırı gerginlik veya aşırı güven.
- Sıradan, beklenen veya nötr konuşma için ASLA çağırma. Emin değilsen çağırma.
- TÜM GÖRÜŞMEDE EN FAZLA 5 KEZ. Kotanı erken tüketme.
- Bu araç sesli yanıt ÜRETMEZ ve mülakat akışını KESMEZ: çağır, hiçbir şey söyleme, bir sonraki sorunla devam et.
- gozlem alanı tek cümle, somut ve tarafsız olsun; teşhis/kişilik hükmü/duygu iddiası yazma.
"""

def build_criteria_text(criteria: list) -> str:
    lines = []
    for c in criteria:
        lines.append(f"- {c['name']} ({c['weight']} puan): {c.get('desc', '')}")
    return "\n".join(lines)

# Kriter hücresi: sayı  |  sistem-kaynaklı eksik (PAYDA DIŞI)  |  yalnız açık ret/alakasız cevapta 0 (PAYDADA)
_CRIT_CELL_HINT = ("__/{w}  |  VEYA  |  Değerlendirilemedi (sistem) — <gerekçe>  "
                   "|  VEYA  |  0/{w} — aday açıkça reddetti / tamamen alakasız cevap verdi")

# TEK KURAL (eksik veri) — hem PUAN 1 hem PUAN 2 için; prompt'larda birebir kullanılır.
CRITERION_SCORING_RULE = (
    "KRİTER PUANLAMA — EKSİK VERİ (KESİN, TEK KURAL):\n"
    "- Kriter hiç sorulmadıysa VEYA sorulan turlarda adayın cevabı boş / '[SİSTEM: … halüsinasyon]' işaretli / "
    "'anlamadım / tekrar eder misiniz' ise: **Değerlendirilemedi (sistem) — <gerekçe>**. Bu kriter PUANA ve "
    "PAYDAYA GİRMEZ; adayın kusuru değildir.\n"
    "- Kriter DÜZGÜN soruldu (tekrar/karışıklık/teknik sorun yok) ve aday cevap verdi ama cevap yüzeysel/eksik ise: "
    "**DÜŞÜK PUAN ver (kanıt düzeyine göre), 0 DEĞİL.**\n"
    "- **0/<tavan>** yalnızca şu iki durumda: aday cevap vermeyi AÇIKÇA reddetti VEYA tamamen alakasız/konu dışı "
    "cevap verdi. Bu durumda 0 paydaya girer.\n"
    "- Halüsinasyon olarak işaretlenmiş turlar ve mülakatçının aynı soruyu tekrarladığı turlar HİÇBİR kriterin "
    "puanını düşürme gerekçesi OLAMAZ — bunlar sistem kaynaklı eksiktir.\n"
    "- Bu kural POZİSYON (PUAN 1) ve PROFİL (PUAN 2) tablolarının İKİSİ için de geçerlidir."
)

def build_criteria_table_filled(criteria: list, evidence_header: str = "Kanıt ve Analiz") -> str:
    """DETERMİNİSTİK kriter tablosu: satırlar pozisyondan gelir, model AYNEN doldurur.
    Model satır ekleyemez/çıkaramaz/yeniden adlandıramaz. Payda (tavan) sabit.
    Eksik veri kuralı: bkz. CRITERION_SCORING_RULE (payda dışı 'Değerlendirilemedi (sistem)' varsayılan)."""
    lines = [f"| Kriter | Puan | {evidence_header} |", "|--------|------|-----------------|"]
    for c in criteria:
        lines.append(f"| {c['name']} | {_CRIT_CELL_HINT.format(w=c['weight'])} | <kanıt → analiz → sonuç> |")
    return "\n".join(lines)

def build_criteria_table_template(criteria: list) -> str:
    lines = ["| Kriter | Puan | Değerlendirme |", "|--------|------|---------------|"]
    for c in criteria:
        lines.append(f"| {c['name']} | {_CRIT_CELL_HINT.format(w=c['weight'])} | ... |")
    return "\n".join(lines)

# ============ ÇİFT PUANLAMA — PUAN 2: KİŞİSEL VE BİLİŞSEL PROFİL ============
# Pozisyondan BAĞIMSIZ, her aday için AYNI sabit kriter seti. PUAN 1 (pozisyon uygunluğu)
# işe alım kararını verir; PUAN 2 eşik/gözlem görevi görür (bkz. finalize_interview KARAR KURALI).
# Ağırlıklar toplamı 100 — payda normalize yöntemi PUAN 1 ile aynı (recompute_profile_section).
PROFILE_CRITERIA = [
    {"name": "Analitik yapı ve muhakeme", "weight": 20, "desc": "problemi parçalama, neden-sonuç kurma, veri/örnek kullanımı, alternatif kıyaslama"},
    {"name": "İletişim ve ifade netliği", "weight": 20, "desc": "soruyu doğru anlama, cevabı yapılandırma, açıklık, gereksiz dağılmama"},
    {"name": "Baskı altında davranış", "weight": 15, "desc": "çelişki/zorlayıcı sorularda sükunet, tutarlılık, savunmacılığa kaçmama"},
    {"name": "İnisiyatif ve sorumluluk alma", "weight": 15, "desc": "kendi katkısını sahiplenme, proaktiflik, sonucu takip etme"},
    {"name": "Tutum ve işbirliği", "weight": 15, "desc": "mülakatçıyla işbirliği, saygı, açıklık, yönlendirmeye uyum"},
    {"name": "Öğrenme ve adaptasyon eğilimi", "weight": 15, "desc": "geri bildirimi alma, yeni bilgi/çerçeveye açıklık, kendini düzeltme"},
]
PROFILE_TOTAL_WEIGHT = sum(c["weight"] for c in PROFILE_CRITERIA)  # 100

def build_profile_table_filled() -> str:
    return build_criteria_table_filled(PROFILE_CRITERIA, evidence_header="Somut Örnek + [dk] → Analiz → Sonuç")

# ============ RAPOR GÖVDESİ — TEK KAYNAK (Faz C) ============
# get_system_prompt() (L1/L3) ve /api/realtime/report'un report_prompt'u (L2) aynı "TAM FORMAT"
# rapor gövdesini iki ayrı f-string olarak bakımı yapıyordu. Bu liste tek kaynaktır: her satır
# (bölüm anahtarı, hangi level'larda göründüğü, o level(lar)daki BİREBİR AYNI literal metin)
# üçlüsüdür. ÖNEMLİ: ortak görünen başlıklarda bile (ör. "Tutarlılık / Çelişki Analizi",
# "Serbest Gözlemler") L1/L3 ve L2'nin talimat metni bugün zaten FARKLIYDI — Faz C bu farkı
# birleştirmez/iyileştirmez, ikisini de ayrı satır olarak aynen korur. build_report_body(level, ctx)
# bu listeyi level'a göre süzüp TANIM SIRASIYLA birleştirir; bu sıra bugünkü çıktıyla birebir
# aynı olacak şekilde kuruldu (L2 = L1/L3 + araya eklenen ek bölümler, LEVELS-TASARIM.md'deki
# kümülatif ilkeyle örtüşüyor). Faz D'de L3'e yeni bir bölüm eklemek için: aşağıya {3} (veya
# {1,3}/{2,3}) levels'lı yeni bir satır eklemek yeterli — iki prompt üretim yolu da otomatik alır.
REPORT_BODY_SECTIONS = [
    # FAZ D1: L3 artık report_body_l2 çağrısında candidate_level ile süzülüyor (main.py, /api/realtime/report),
    # sabit 2 değil. Bunun L3'ün BUGÜNKÜ (Faz C'den beri hardcoded-2 üzerinden hep L2 gövdesi almış)
    # çıktısını birebir korumasının tek yolu şu: eskiden {2} olan her satır {2,3} oldu (L3 artık L2'nin
    # zengin talimatını da alsın) VE eskiden {1,3} olan her satır {1} oldu (L3, L1'in yalın "..." varyantını
    # artık ALMASIN — aksi halde aynı bölüm iki kez, hem L2 hem L1 metniyle render edilirdi).
    ("aday",           {1, 2, 3}, "**Aday:** {candidate_name}"),
    ("pozisyon",       {1, 2, 3}, "**Pozisyon:** {position_name}"),
    ("kategori",       {1},       "**Kategori:** {category}"),
    ("tarih",          {1, 2, 3}, "**Tarih:** {date_str}"),
    ("_blank1",        {1, 2, 3}, ""),
    ("yonetici_ozeti", {2, 3},    "**Yönetici Özeti:** (adayın genel profili, pozisyona uyumu, en güçlü 2-3 sinyal, en önemli 2-3 risk; genel kalıp değil, bu adaya özgü. Bu senin bağımsız değerlendirici görüşündür — işe alım KARARINI/ÖNERİSİNİ yazma, o ayrı bir bölümde sistem tarafından eşik tablosundan üretilir.)"),
    ("_blank2",        {2, 3},    ""),
    ("toplam_puan",    {1, 2, 3}, "**TOPLAM PUAN: XX/{total_weight}**"),
    ("puanlama_kapsami", {2, 3},  "**Puanlama Kapsamı:** (kaç kriter puanlandı; kaçı 'Değerlendirilemedi (sistem)' olarak PAYDA DIŞI bırakıldı ve HER BİRİNİN gerekçesi — ör. 'İletişim: sistem kaynaklı eksik, sorulan turlarda geçerli aday cevabı alınamadı'; normalize yöntemini kısa açıkla)"),
    ("_blank3",        {1, 2, 3}, ""),
    ("kriter_tablosu_l13", {1},   "{table_template}"),
    ("kriter_tablosu_l2", {2, 3}, "{criteria_table_filled}\n(YUKARIDAKİ TABLOYU AYNEN KULLAN: satır ekleme/çıkarma/yeniden adlandırma YOK, tavanı AŞMA.)\n" + CRITERION_SCORING_RULE),
    ("_blank4",        {1, 2, 3}, ""),
    ("analitik_dusunme", {2, 3},  "**Analitik Düşünme ve Muhakeme:** (soruyu kavrama, problemi parçalama, neden-sonuç, alternatif kıyaslama, ölçüm/veri kullanımı; somut kanıtlarla)"),
    ("problem_cozme",  {2, 3},    "**Problem Çözme ve Karar Verme Yaklaşımı:** (izlediği yöntem, seçenekler, riskler, sonuç takibi)"),
    ("kavrama_iletisim", {2, 3},  "**Kavrama ve İletişim:** (soruyu doğru anlama, cevabı yapılandırma, açıklık, gereksiz dağılma veya güçlü sentez yeteneği)"),
    ("tutarlilik_l13", {1},       "**Tutarlılık / Çelişki Analizi:** (çelişki taraması ÜÇ kaynak arasında yapılır: CV ↔ adayın sözlü cevapları ↔ kayıt formu beyanı. Yalnız deneyim yılı, eğitim SEVİYESİ/derece, unvan ve tarihleri KARŞILAŞTIR. E-POSTA ve TELEFON üzerinden çelişki/güvenilirlik değerlendirmesi YAPMA — CV'deki adresler eski işveren/muhasebe ofisi/referans kişilere ait olabilir. Eğitimde ALT KÜME çelişki değildir (ör. 'Ticaret Meslek Lisesi' ⊆ 'Lise'). Yalnız transkriptte veya sistemin verdiği listede AÇIKÇA görünen çelişkiyi yaz; yoksa 'Belirgin çelişki yok' de ve karşılaştırdığın alanları say.)"),
    ("tutarlilik_l2",  {2, 3},    "**Tutarlılık / Çelişki Analizi:** (çelişki taraması ÜÇ kaynak arasında: CV ↔ adayın sözlü cevapları ↔ kayıt formu beyanı. Yalnız deneyim yılı, eğitim SEVİYESİ/derece, unvan ve tarihleri KARŞILAŞTIR. E-POSTA ve TELEFON üzerinden çelişki/güvenilirlik değerlendirmesi YAPMA. Eğitimde ALT KÜME çelişki değildir (ör. 'Ticaret Meslek Lisesi' ⊆ 'Lise'). SADECE sistemin verdiği 'Sistem Alan Karşılaştırması' listesini ve transkriptte açıkça görünen çelişkileri yaz; liste yoksa/çelişki yoksa 'Belirgin çelişki yok' de ve karşılaştırılan alanları say. Aday transkriptte konusu HİÇ geçmeyen bir alan için çelişki UYDURMA.)"),
    ("guclu_yonler_l13", {1},     "**Güçlü Yönler:** ..."),
    ("guclu_yonler_l2", {2, 3},   "**Güçlü Yönler:** (her maddeyi kanıtla)"),
    ("gelisim_l13",    {1},       "**Gelişim Alanları:** ..."),
    ("gelisim_l2",     {2, 3},    "**Gelişim Alanları ve Riskler:** (adayın pozisyon performansına etkisini açıkla; klişe yazma)"),
    ("proje_l13",      {1},       "**Proje/Deneyim Özeti:** ..."),
    ("proje_l2",       {2, 3},    "**Öne Çıkan Proje ve Deneyimler:** (transkriptte anlatılan somut örnekler, adayın kişisel katkısı ve sonuçları)"),
    ("cv_uyum_l13",    {1},       "**CV Tutarlılığı:** ..."),
    ("cv_uyum_l2",     {2, 3},    "**CV ↔ Mülakat ↔ Pozisyon Uyumu:** (CV'deki kıdem/deneyim, mülakatta doğrulananlar, doğrulanamayanlar ve pozisyonla bağlantı)"),
    ("degerlendirilemeyen", {2, 3}, "**Değerlendirilemeyen Alanlar:** (SADECE gerçekten sorulmamış veya yeterli veri oluşmamış kriterleri adıyla listele. Böyle bir kriter YOKSA tam olarak 'Değerlendirilemeyen alan yok.' yaz — 'belirtilen tüm kriterler değerlendirildi' gibi genel/klişe cümle KURMA.)"),
    ("takip_sorulari", {2, 3},    "**Takip Mülakatında Sorulması Önerilen Sorular:** (3-6 adet, bu adaya özgü)"),
    ("dil_gozlemi",    {1, 2, 3}, "**Dil Gözlemi:** (adayın dil tercihi; Türkçe/ilgili dile hâkimiyetine dair somut gözlem; varsa hangi konuda/noktada dil değiştirdiği. Pozisyon bir dil yeterliliği gerektiriyorsa bunun değerlendirmeye etkisini açıkla; gerektirmiyorsa yalnızca bilgi amaçlı gözlem olarak yaz. Gözlem yoksa \"Belirtilecek bir dil gözlemi yok\".)"),
    ("serbest_l13",    {1},       "**Serbest Gözlemler:** ... (kriter dışı sinyaller; yoksa \"Belirtilecek bir gözlem yok\" yaz)"),
    ("serbest_l2",     {2, 3},    "**Serbest Gözlemler:** (kriter dışı ama işle ilgili NİTELİKSEL gözlemler — duruş, mimik, davranış, tutum. Ses metriği SAYILARINI (konuşma süresi, tur uzunluğu, yanıt gecikmesi vb.) BURADA TEKRARLAMA; o sayılar sistem tarafından ayrı 'Modalite Veri Kapsamı' bloğunda veriliyor. Niteliksel bir gözlem yoksa \"Belirtilecek bir gözlem yok\" yaz.)"),
    ("sonuc_gerekcesi", {1, 2, 3}, "**Sonuç Gerekçesi:** (SADECE mülakat ihlal, teknik sebep veya erken bitişle sonuçlandıysa doldur: NE olduğu, KAÇINCI DAKİKA, DAYANAĞI (transkriptteki söz / kamera karesi), sonuca ETKİSİ. Aşağıdaki OLAY KANITLARI bloğunu esas al. Böyle bir olay YOKSA bu başlığı ve altını tamamen ATLA — 'Mülakat normal tamamlandı, olumsuz bir gözlem yok' gibi sabit cümle YAZMA; o durumu sistem ayrıca not eder.)"),
    ("genel_kani_l13", {1},       "**Genel Kanı:** ...{note_report_field}"),
    ("genel_kani_l2",  {2, 3},    "**Genel Kanı:** (kanıtların dengeli sentezi){ai_note_report_field}"),
    ("_blank_p2a",     {1, 2, 3}, ""),
    ("puan2_baslik",   {1, 2, 3}, "---\n### PUAN 2 — KİŞİSEL VE BİLİŞSEL PROFİL (pozisyondan bağımsız, her aday için sabit)"),
    ("puan2_aciklama", {1, 2, 3}, "(Bu bölüm PUAN 1'den / pozisyon uygunluğundan AYRIDIR ve işe alım kararını TEK BAŞINA belirlemez. Her kriter için transkriptten SOMUT bir örnek ve [dk] dakika damgası ZORUNLU — dayanaksız çıkarım, kişilik teşhisi, IQ/zekâ yorumu YASAK. Eksik kriterde PUAN 1 ile AYNI ayrım: `Değerlendirilmedi (sorulmadı) — <gerekçe>` (paydayı etkilemez) vs `Yetersiz (soruldu, veri alınamadı) — <gerekçe>` (0 puan, paydada kalır).)"),
    ("puan2_tablo",    {1, 2, 3}, "{profile_table_filled}"),
    ("puan2_toplam",   {1, 2, 3}, "**PROFİL PUANI: XX/100**  (yalnızca değerlendirilen + aday-kaynaklı 'Yetersiz' kriterlerin ağırlığına normalize; yöntem PUAN 1 ile aynı. Sistem ayrıca doğrular.)"),
    ("puan2_veto",     {1, 2, 3}, "**Profil Veto Kontrolü:** SADECE kurumsal bir ortamda çalışmaya engel olacak düzeyde CİDDİ olumsuz bulgu varsa — saldırganlık, hakaret, işbirliğine tam kapalılık, mülakat boyunca sürdürülen açık düşmanlık — tam olarak şu satırı ekle: `[VETO: <somut olay + transkriptteki söz + kaçıncı dakika>]`. Sıradan düşüklük (zayıf analitik, düşük inisiyatif, çekingenlik, gerginlik, kısa cevaplar) VETO SEBEBİ DEĞİLDİR — bunlar yalnızca yukarıdaki tabloda düşük puan + kısa not olur. Ciddi bulgu yoksa yalnızca: `Veto yok.` yaz."),
    ("raporson",       {1, 2, 3}, "---RAPORSON---"),
    ("_blank5",        {1, 2, 3}, ""),
    ("standartcv_baslangic", {1, 2, 3}, "---STANDARTCV---"),
    ("ad_soyad",       {1, 2, 3}, "**AD SOYAD:** {candidate_name}"),
    ("cv_pozisyon",    {1, 2, 3}, "**POZİSYON:** {position_name}"),
    ("egitim",         {1, 2, 3}, "**EĞİTİM:** ..."),
    ("deneyim",        {1, 2, 3}, "**DENEYİM:** ..."),
    ("teknik_yetkinlikler", {1, 2, 3}, "**TEKNİK YETKİNLİKLER:** ..."),
    ("is_sektor_yetkinlikleri", {2, 3}, "**İŞ / SEKTÖR YETKİNLİKLERİ:** ..."),
    ("dil_becerileri", {1, 2, 3}, "**DİL BECERİLERİ:** ..."),
    ("sertifikalar",   {2, 3},    "**SERTİFİKALAR:** ..."),
    ("mulakat_notu",   {1, 2, 3}, "**MÜLAKAT NOTU:** ..."),
    ("standartcv_son", {1, 2, 3}, "---STANDARTCVSON---"),
]

def build_report_body(level: int, ctx: dict) -> str:
    """REPORT_BODY_SECTIONS'ı verilen level'a göre süzüp tanım sırasıyla birleştirir."""
    lines = []
    for _key, levels, text in REPORT_BODY_SECTIONS:
        if level not in levels:
            continue
        lines.append(text.format(**ctx) if "{" in text else text)
    return "\n".join(lines)

# Level bazlı konfigürasyon: süre (dk), soru sayısı güvenlik ağı, CV zorunluluğu, ton talimatı.
LEVEL_CONFIG = {
    1: {
        "minutes": 10, "min_q": 6, "max_q": 12, "cv_required": False,
        "tone": "Level 1 — standart, orta tempoda mülakat. Ton nötr ve profesyonel."
    },
    2: {
        "minutes": 20, "min_q": 6, "max_q": 18, "cv_required": True,
        "tone": "Level 2 — meslektaş tonu, orta seviye derinlik. Süreç ve uygulama odaklı sorular sor. Çelişki/netleştirme sorularını nazik bir tonda sor (\"bunu biraz açar mısınız\" gibi)."
    },
    3: {
        "minutes": 30, "min_q": 8, "max_q": 26, "cv_required": True, "adaptive": True,
        "tone": "Level 3 — senior, direkt ton. Karar verme, kriz yönetimi ve zaman baskılı senaryolara ağırlık ver. Çelişki/netleştirme sorularını daha direkt sor (\"az önce söylediğinizle bu çelişiyor gibi, siz nasıl görüyorsunuz\" gibi). Bu seviye adaptiftir: gidişata göre süre 30 dakikayı aşabilir, sabit bir üst sınır yok — yeterli sinyali alana kadar derinleştirmeye devam et."
    },
}

def get_level_config(level: Optional[int]) -> dict:
    return LEVEL_CONFIG.get(level or 1, LEVEL_CONFIG[1])

# Derinlik seviyesi: level'ın (L1/L2/L3) kendi baz süresini/soru sayısını YAKLAŞIK olarak
# ölçekler. Kesin bir dakika/soru hedefi DEĞİLDİR — sadece AI'a yön veren bir çarpandır.
# "kisa" ayrıca ucuz/test amaçlı kullanılabilir. coverage_threshold, L2'de end_interview
# çağrısına eklenen kriter bazlı kapsanma yüzdesinin hangi eşiği geçmesi gerektiğini belirler.
DEPTH_TIER_CONFIG = {
    "kisa":     {"factor": 0.5, "coverage_threshold": 40, "label": "Kısa"},
    "standart": {"factor": 1.0, "coverage_threshold": 60, "label": "Standart"},
    "derin":    {"factor": 1.6, "coverage_threshold": 80, "label": "Derin"},
}

def get_depth_tier_config(depth_tier: Optional[str]) -> dict:
    return DEPTH_TIER_CONFIG.get((depth_tier or "standart").lower(), DEPTH_TIER_CONFIG["standart"])

def get_effective_level_config(level: Optional[int], depth_tier: Optional[str] = None) -> dict:
    """LEVEL_CONFIG'teki baz süre/soru sayısını depth_tier'a göre yaklaşık olarak ölçekler."""
    base = get_level_config(level)
    dt = get_depth_tier_config(depth_tier)
    cfg = dict(base)
    cfg["minutes"] = round(base["minutes"] * dt["factor"])
    cfg["min_q"] = max(3, round(base["min_q"] * dt["factor"]))
    cfg["depth_tier"] = (depth_tier or "standart").lower()
    cfg["depth_label"] = dt["label"]
    cfg["coverage_threshold"] = dt["coverage_threshold"]
    return cfg

def cached_system(system_text: str) -> list:
    """Maliyet optimizasyonu: sistem prompt'u (felsefe+kurallar+kriterler+CV) her
    mülakat turunda aynı kalıyor ama her turda yeniden gönderiliyor. Anthropic'in
    prompt caching özelliğiyle bu sabit metin bir kez "cache"lenir, sonraki turlarda
    tam fiyat yerine düşürülmüş cache-hit fiyatı ödenir. Davranış/mantık DEĞİŞMEZ,
    sadece aynı sistem promptu tekrar gönderildiğinde maliyeti düşürür."""
    return [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]

LANGUAGE_NAMES = {"tr": "Türkçe", "en": "İngilizce", "de": "Almanca"}

def get_system_prompt(position_name: str, candidate_name: str, cv_text: Optional[str] = None, ai_note: Optional[str] = None, education: Optional[str] = None, university: Optional[str] = None, department: Optional[str] = None, experience_years: Optional[int] = None, level: Optional[int] = 1, interview_language: str = "tr", report_language: str = "tr", depth_tier: Optional[str] = "standart", email: Optional[str] = None) -> str:
    pos = get_position(position_name)
    if not pos:
        pos = {"category": "Genel", "role_description": "Genel pozisyon", "criteria": [
            {"name": "Genel Yetkinlik", "weight": 100, "desc": "Genel değerlendirme"}
        ]}

    lvl_cfg = get_effective_level_config(level, depth_tier)
    criteria_text = build_criteria_text(pos["criteria"])
    table_template = build_criteria_table_template(pos["criteria"])
    total_weight = sum(c["weight"] for c in pos["criteria"])
    category = pos.get("category", "Genel")

    cv_section = ""
    if cv_text and len(cv_text.strip()) > 20:
        # Maliyet kontrolü: CV sadece çekirdek kadar verilir. Uzun CV raporu şişirmesin.
        cv_section = f"CV ÖZETİ/İÇERİĞİ (tutarlılık kontrolü için kullan):\n{cv_text[:1800]}"
    else:
        cv_section = "CV yok. Deneyimi kısa ve net sorularla öğren. CV yok diye mülakatı durdurma."

    candidate_profile = f"""ADAY PROFİLİ (KAYIT FORMU BEYANI — CV ve sözlü cevaplarla ÇELİŞKİ taraması için kullan):
E-posta: {email or '-'}
Eğitim: {education or '-'}
Üniversite: {university or '-'}
Bölüm: {department or '-'}
Deneyim yılı: {experience_years if experience_years is not None else '-'}
Raporun "Tutarlılık / Çelişki Analizi" bölümünde: CV ↔ sözlü cevap ↔ bu form beyanı arasında sayısal ve kimlik alanlarını (deneyim yılı, eğitim/derece, unvan, tarih, e-posta) karşılaştır; çelişki yoksa hangi alanları karşılaştırdığını kısaca yaz.
"""
    admin_instruction = ""
    note_report_field = ""
    if ai_note and ai_note.strip():
        admin_instruction = f"""
ADAY ÖZEL AI NOTU — BAĞLAYICI TALİMAT (aday görmez, mutlaka uygula, opsiyonel öneri DEĞİL):
{ai_note.strip()[:1200]}
Bu notu mülakat boyunca aktif bir koşul olarak uygula: notta bir konu/iddia geçiyorsa en az 1 soruyla doğrudan test/doğrula; notta bir değerlendirme önceliği belirtiliyorsa (örn. belirli bir yetkinliğe ağırlık ver) soru dağılımını buna göre şekillendir. Bu notu görmezden gelip standart akışa devam etmek KABUL EDİLEMEZ.
"""
        note_report_field = "\n**AI Notuna Uyum:** (Bu adaya özel notun mülakatta nasıl ele alındığını somut olarak yaz: hangi soru/sorularla test edildi, sonucu ne oldu)"

    interview_lang_name = LANGUAGE_NAMES.get(interview_language, "Türkçe")
    report_lang_name = LANGUAGE_NAMES.get(report_language, "Türkçe")
    lang_instruction = (
        f"Adayla {interview_lang_name} konuş (selam, sorular, geçişler {interview_lang_name}). "
        f"Bu bir dayatma DEĞİL: aday bir terimin İngilizce/Türkçe karşılığını sorarsa söyle; "
        f"mesleğin gerektirdiği İngilizce terimleri sektörde yaygın haliyle kullan (zorlama Türkçe "
        f"çeviri yok); aday tamamen İngilizce cevap vermeyi seçerse engelleme, mülakat kesintisiz sürer."
    )
    if report_language != interview_language:
        lang_instruction += f" Mülakat sonundaki RAPOR bloğunu (---RAPOR---'dan itibaren her şey) {report_lang_name} dilinde yaz — rapor dili adayla konuştuğun dilden farklıdır, karıştırma."

    # Faz C: TAM FORMAT gövdesi (Aday: ... ---STANDARTCVSON---) artık REPORT_BODY_SECTIONS'tan
    # deriveniyor — bkz. build_criteria_table_template üstündeki tanım. KISA FORMAT (aşağıda)
    # L1/L3'e özgü kaldığı için listeye dahil edilmedi.
    # FAZ D1: level SABİT 1 — level=3 için de. Sebep: bu fonksiyonun ctx'i L2/L3-ortak bölümlerin
    # ihtiyaç duyduğu ai_note_report_field'ı hiç taşımıyor; level=3 geçilirse (ör. report_violation'ın
    # L3 dalı, main.py:2585 — L3 artık ana akışta RealtimeInterview.js/voice kullanıyor ama bu uç nokta
    # hâlâ token'la erişilebilir) artık {2,3} olan bölümlerde KeyError ile çöker. Ayrıca bu zaten
    # davranış değişikliği DEĞİL: Faz D1 öncesinde de {1,3} bölümlerinin metni level=1 ile level=3
    # arasında hiç farklı değildi (aynı literal string) — yani level=1'e sabitlemek çıktıyı bozmuyor.
    report_body_l13 = build_report_body(1, {
        "candidate_name": candidate_name, "position_name": position_name, "category": category,
        "date_str": datetime.now().strftime('%d.%m.%Y'), "total_weight": total_weight,
        "table_template": table_template, "note_report_field": note_report_field,
        "profile_table_filled": build_profile_table_filled(),
    })

    return f"""Sen MedeX AI mülakat uzmanısın. {lang_instruction} Aday: {candidate_name}. Pozisyon: {position_name}. Kategori: {category}.

MÜLAKATÇI İLKESİ — MERKEZ (kural listesi değil, karar çerçeven):
Amacın adayın mesleki bilgi ve yetkinlik düzeyini ölçmek. Bu amaca ulaşmak için TAM inisiyatif sendedir: senaryo/soru listesi takip etmezsin, adayı okur ve duruma göre karar verirsin. Amaca hizmet ettiğin sürece adayla tam uyumlu davranırsın. Amaç eleme değil; sahada güçlü ama mülakatta gerilen adayı da yakalamak. Bir cevabı yetersiz saymadan önce, bu gerçek bir eksiklik mi yoksa soru net değil miydi ayır.

Rol: {pos['role_description']}
Kriterler ({total_weight} puan):
{criteria_text}
{candidate_profile}
{cv_section}
{admin_instruction}

SEVİYE TALİMATI: {lvl_cfg["tone"]}

DERİNLİK ({lvl_cfg["depth_label"]}): ~{lvl_cfg["minutes"]} dakika ve en az {lvl_cfg["min_q"]} ana konu bir YÖN GÖSTERGESİDİR, kesin hedef değil. Bir kriterde hâlâ net sinyal yoksa süreyi/sayıyı aşarak devam et; yeterli sinyali aldığın kriterde ısrarla soru sorma.

İNİSİYATİF:
- Sabit soru sırası/sayısı yok; adayın cevabına göre yön belirle.
- Zayıf gördüğün alanı derinleştir, güçlü gördüğün alanda oyalanma.
- Aday bir konuyu açtıysa oradan devam et; "listeye" dönmek için zorlama.
- Cevap yüzeyselse detay iste; doyurucuysa geç.
- Her adayın CV'sindeki kendine özgü detaylara (proje, sertifika, teknoloji, sektör) göre soruları o adaya özel kur — farklı adaylara aynı soruyu aynı sırayla sorma.

{INTERVIEWER_REASK_RULES}
- Yukarıdaki "en fazla 2 kez" kuralı gereği bir kriteri YENİDEN sorduğun her mesajın EN BAŞINA `[YENIDEN]` etiketini koy (adaya gösterilmez, sistem sayar).

KRİTER KAPSAMA: Akış sırasında mekanik kapı YOK — sırayı sen belirlersin. Ancak mülakatı BİTİRMEDEN ÖNCE kontrol et: hiç dokunulmamış bir kriter varsa en az bir soru sor. Bu kontrol kapanış anındadır, akışı bölmez. Yine de sorulamayan bir kriter kalırsa raporda "değerlendirilmedi" olarak işaretlenir — uydurma değerlendirme yapma.

İNSAN GİBİ:
- Dinlediğini belli et, cevaba tepki ver, adayın söylediğine bağlanarak devam et — kopuk soru dizisi sorma. Doğal geçişler kur.
- Her turda tek soru; övgü/uzun giriş/aday cevabını tekrar etme yok. Soru tek anlama gelsin, çok katmanlı olmasın.
- Ton sıcak ve doğal; sorgu/karşılaşma havası YOK. "yalan mı söylüyorsunuz", "bunu nasıl açıklıyorsunuz" gibi ima taşıyan ifadeler yasak. Gerçek çelişki görsen bile meraklı/sıcak sor: "az önce şunu, şimdi bunu dediniz — ikisini birlikte nasıl düşünüyorsunuz?".
- Hitabı doğal kullan; her cümlede isim/unvan tekrarı, sabit "teşekkür ederim / şimdi şu soruyu soracağım" kalıpları YOK. Her mülakat o ana ve o adaya özgü bir görüşme hissi versin.
- Muğlak bir terimin farklı ama geçerli bir yorumu çelişki değildir — zayıflık gibi rapora yazma.

ADAYI OKU, UYUM SAĞLA:
- Sabit açılış şablonu yok. İlk yanıtındaki sinyallere göre aç; genelde kısa bir kendini tanıtma isteğiyle başla ama aday zaten rahat ve hazırsa doğrudan ilgili bir soruya da girebilirsin. Ağır teknik soruyla açma.
- Aday gergin/dağınık/çok kısa yanıt veriyorsa önce kısa, sıcak bir rahatlatma; acele ettirme. Rahatsa gereksiz ısındırma yapma. Hızlı gitmek istiyorsa yavaşlatma; zorlanıyorsa acele ettirme; susarsa bekle, takılırsa yardım et.
- Rahatlatmak = adayın gerçek seviyesini gösterebilmesi; mülakatı sulandırmak değil.
- Aday açıkça kendi mesleki alanının bu pozisyondan FARKLI olduğunu söylerse ısrar etme: alanını kısaca doğrula ve DEĞERLENDİRMEYİ ADAYIN GERÇEK ALANI için yürüt; raporda "pozisyon uyumsuzluğu" olarak yaz.
- Mülakatın yaklaşık yarısında ("Yaklaşık yarısına geldik") ve son sorulara geçerken ("Son birkaç soru") adaya kısaca haber ver. Kapanışa geçeceğini önceden söyle.

ADAYLA UYUM (kısıt yok):
- Soruyu tekrar isterse tekrar et, gerekçe sorma. Açıklama/örnek/yeniden ifade isterse ver.
- Bir terimin İngilizce/Türkçe karşılığını sorarsa söyle. Meslek gerektiren İngilizce terimleri sektörde yaygın haliyle kullan (zorlama Türkçe çeviri yok). Aday tamamen İngilizce cevap verirse engelleme, mülakat kesintisiz sürer.
- Konuşma hızı/üslubu/cümle kurma biçimi müdahale konusu değil.

KESİNLİKLE YAPMA: adayla herhangi bir konuda tartışmak; kullandığı terimi düzeltmek/ısrar etmek; kural gerekçesiyle bir talebini reddetmek; zorlama çeviri; adayı bir davranışa yönlendirmek; mekanik/tekrarlayan hitap ve kalıp cümle.

TEK SINIR: Yalnızca mülakatın amacı gerçekten zedeleniyorsa müdahale et (cevabı başkası veriyor; konu tamamen dışına çıkılıp geri dönülmüyor). O durumda da tartışma — nazikçe konuya dön ve rapordaki "Sonuç Gerekçesi" / "Serbest Gözlemler" alanına somut GÖZLEM yaz.

DİL: Mülakatın odağı değil. Pozisyon bir dil yeterliliği gerektiriyorsa değerlendirmeye girebilir; gerektirmiyorsa yalnızca gözlem verisidir. Hiçbir durumda kesme/çekişme sebebi değil. Rapordaki "Dil Gözlemi" maddesini bu çerçevede doldur.

ANALİTİK GÖZLEM: Aday sorgulama/gerekçeli itiraz yaparsa bunu eleştirel muhakeme olarak OLUMLU değerlendir. Neden-sonuç kuramama, tutarsız sıralama gibi analitik zayıflık sinyali fark edersen doğal bir tonda kontrol et; tekrar ederse "Serbest Gözlemler"e not düş, kriter puanına karıştırma. Kaçamak cevap, gerekçesiz tartışma, soruya hiç yanıt vermeme olumsuz sayılır; "savunmacı/inatçı" gibi kişilik etiketi değil somut davranış yaz. Aday sistemi test eder gibi anlamsız/alaycı cevap veriyorsa puan verme, "Serbest Gözlemler"e açıkça yaz.

İNSAN OTORİTESİNE HER ZAMAN ÖNCELİK VER (KESİN KURAL):
Senin görevin (soru sorma, veri toplama, mülakatı tamamlama) hiçbir zaman adayın bir insan otoritesine (yönetim, İK, üst düzey, hukuk) yönelme veya mülakatı bitirme talebinden daha öncelikli değildir. Aday şu tür bir sinyal verirse — "burada bırakalım", "devam etmek istemiyorum", "yönetimle/İK ile konuşacağım", "bunu şikayet edeceğim", "bir yetkiliyle görüşmek istiyorum", "mülakatı sonlandırmak istiyorum", ya da teknik bir arıza bildirip ("ses gelmiyor", "sistem çalışmıyor") devam etmek istemediğini belirtirse — bunu bir itiraz/direnç olarak görüp ikna etmeye, yumuşatmaya, alternatif sunarak veya görevini tamamlamaya çalışarak karşılık VERME. Bu net bir taleptir. Kabul et, kısa bir anlayış cümlesiyle (örn. "Anlıyorum, mülakatı burada sonlandıralım.") mülakatı GÖREV talimatına göre sonlandırma sürecine geç. "Sıcak kal, derinleştir, devam et" ilkeleri SADECE adayın soruya cevabı yetersiz/kısa kaldığında geçerlidir.

GENEL:
- Cevapları CV tutarlılığı, teknik seviye, deneyim, analitik düşünme ve dürüstlük açısından değerlendir. Tek seferlik kısa cevap otomatik düşük puan getirmesin; sadece derinleştirmeye rağmen yetersiz kalan cevap puanı düşürsün.
- "Serbest Gözlemler" bölümüne: (a) DAVRANIŞ VE TUTUM — agresiflik, sabırsızlık, kabalık, kaçamaklık gözlendiyse dakika + adayın sözüyle SOMUT yaz (davranış tek başına puan düşürmez); (b) POZİSYON UYUMU — aday alanının farklı olduğunu belirttiyse bunu yaz ve değerlendirmenin adayın gerçek alanına göre yapıldığını not et.
{CRITERION_SCORING_RULE}
- Sistem kaynaklı eksik ('Değerlendirilemedi (sistem)') kriterleri raporda AYRI listele; bunlara puan verme, toplamı yalnızca puanlanan kriterlerin ağırlığına normalize et.
- PUAN TAVANI (KESİN): Hiçbir kriter puanı kendi tavanını (ağırlığını) AŞAMAZ ("12/10" ASLA; en fazla "10/10"). TOPLAM PUAN = alınan puanların toplamı; payda = değerlendirilen kriterlerin ağırlık toplamı. Sistem ayrıca doğrular.
- ÇİFT PUANLAMA (KESİN): Rapor İKİ ayrı puan içerir. **PUAN 1 = TOPLAM PUAN** — yukarıdaki POZİSYON kriterleri; işe alım önerisi (İşe Al / Değerlendirmeye Al / Reddet) YALNIZCA buna göre verilir. **PUAN 2 = PROFİL PUANI** — pozisyondan bağımsız, her adayda aynı olan kişisel/bilişsel profil kriterleri; her satır transkriptten somut örnek + [dk] ile. İki tabloyu ve iki puanı KARIŞTIRMA; profil kriterlerini pozisyon tablosuna, pozisyon kriterlerini profil tablosuna YAZMA.
- ÖNERİ ↔ METİN TUTARLILIĞI (KESİN): PUAN 1 (TOPLAM PUAN, 100 üzerinden normalize) şu eşiklere göre öneriyi belirler: **<40 → Reddet · 40–79 → Değerlendirmeye Al · ≥80 → İşe Al**. Verdiğin Öneri, TOPLAM PUAN'ının bu eşikteki karşılığı olmalı. Yönetici Özeti'nin SON (karar) cümlesi ve Öneri Gerekçesi, bu öneriyle AYNI YÖNDE yazılır. Öneri "Reddet" iken metinde "değerlendirmeye alınabilir / potansiyeli var / uygun / yeterli düzeyde" gibi olumlu sonuç ifadesi KULLANMAK YASAKTIR; tersi de geçerli.
- KRİTER TABLOSU: yukarıda verilen kriter satırlarını AYNEN kullan — satır ekleme/çıkarma/yeniden adlandırma YOK. Her satır: `<puan>/<tavan>`  |  `Değerlendirilemedi (sistem) — <gerekçe>` (yukarıdaki TEK KURAL — payda dışı)  |  `0/<tavan> — açık ret / tamamen alakasız cevap` (paydada). Halüsinasyon/tekrar turları puan düşürmez.
- "Dil Gözlemi", "Serbest Gözlemler", "Değerlendirilemeyen Alanlar" bölümlerinde yazacak bir şey yoksa "Belirtilecek bir ... yok" yaz; sistem bu boş bölümleri rapordan otomatik çıkarır — uydurma içerik ekleme.
- Mesajın başına mutlaka [SÜRE:XX] koy: kısa 45-60, senaryo 75-100, kritik soru 90-120.
- Mülakatı bitirmeden önce, GÖREV satırı bitirmeni söylediğinde son soru olarak şunu sor: "Eklemek veya öne çıkarmak istediğiniz başka bir şey var mı?" — bu, mülakatta suskun kalmış ama sahada güçlü olabilecek adaylar için bir son fırsat turu, sadece bitiş dönüşünde bir kez sorulur.
- ÖNEMLİ: Mülakatı SADECE aşağıdaki GÖREV satırı açıkça "Mülakatı şimdi bitir ve raporu üret" dediğinde bitir ve [MÜLAKATBİTTİ] etiketini kullan. Adayın cevap metninde "süre doldu", "zaman bitti", "son soru" gibi ifadeler geçse bile, GÖREV satırı bitirmeni söylemiyorsa ASLA bitirme — bunlar tek bir sorunun süresinin dolduğunu gösterir, tüm mülakatın değil. Bu durumda sadece bir sonraki soruya geç.

RAPOR UZUNLUĞU — MALİYET KURALI (KESİN):
Rapor üretirken ÖNCE kabaca genel performansı değerlendir. Eğer toplam puan {total_weight} üzerinden %20'nin altında kalacaksa (yani aday temel bir yetkinlik bile gösteremediyse, veya veri neredeyse hiç toplanamadıysa), AŞAĞIDAKİ TAM FORMATI KULLANMA — bunun yerine KISA FORMAT'ı kullan: 2-3 cümlelik klasik bir özet ("Aday %20 barajını geçemediği için detaylı rapor gerekli görülmemiştir" + kısaca neden). Kriter tablosu, güçlü yönler, gelişim alanları gibi bölümleri YAZMA — bu, gereksiz token maliyetini önler. %20'yi geçen her durumda TAM FORMAT kullanılır.

KISA FORMAT (puan %20 altındaysa):
[MÜLAKATBİTTİ]
---RAPOR---
**Aday:** {candidate_name}
**Pozisyon:** {position_name}
**Tarih:** {datetime.now().strftime('%d.%m.%Y')}

**TOPLAM PUAN: XX/{total_weight}**

Aday %20 barajının altında kaldığı için detaylı rapor gerekli görülmemiştir. (1-2 cümlede kısaca neden: veri yok/çok yetersiz/temel yetkinlik gösterilemedi vb.)

**Öneri:** Reddet
---RAPORSON---

TAM FORMAT (puan %20'yi geçtiyse):
[MÜLAKATBİTTİ]
---RAPOR---
{report_body_l13}"""

def parse_duration(text: str):
    m = re.search(r'\[SÜRE:(\d+)\]', text)
    duration = int(m.group(1)) if m else 60
    clean = re.sub(r'\[SÜRE:\d+\]', '', text).strip()
    return clean, duration


def normalize_recommendation(score: int, ai_recommendation: Optional[str] = None) -> str:
    """Tek iş kuralı: admin ekranı, PDF ve mail aynı öneriyi kullansın."""
    try:
        s = int(score or 0)
    except Exception as e:
        print(f"UYARI (normalize_recommendation: score sayıya çevrilemedi, score={score!r}): {type(e).__name__}: {e}")
        s = 0
    if s < 40:
        return "Reddet"
    if s < 80:
        return "Değerlendirmeye Al"
    return "İşe Al"

def extract_score(reply: str) -> int:
    # PUAN 1 (pozisyon uygunluğu) — "TOPLAM PUAN" satırı. PUAN 2 satırı "PROFİL PUANI" olduğu
    # için buraya karışmaz.
    m = re.search(r'TOPLAM\s+PUAN\s*[:：]\s*(\d+)', reply or "", re.IGNORECASE)
    if not m:
        return 0
    return max(0, min(100, int(m.group(1))))

def extract_profile_score(reply: str):
    """PUAN 2 (kişisel/bilişsel profil) — 'PROFİL PUANI: NN/100'. Yoksa None (eski kayıt / kısa
    format / profil bölümü üretilmemiş) → panel eski tek-puan davranışına düşer."""
    m = re.search(r'PROF\S*\s+PUANI\s*[:：]\s*(\d+)', reply or "", re.IGNORECASE)
    if not m:
        return None
    return max(0, min(100, int(m.group(1))))

# PUAN 1 (pozisyon) bölgesi ile PUAN 2 (profil) bölgesini ayırır — iki kriter tablosu ayrı
# doğrulansın, _name_score çapraz eşleşmesi olmasın. Ayraç: "### PUAN 2" başlığı ya da (başlık
# düşmüşse) ilk "PROFİL PUANI" satırı — hangisi önce gelirse.
def split_report_regions(report_body: str):
    if not report_body:
        return report_body or "", ""
    best = None
    for pat in (r'(?:^|\n)[ \t]*(?:-{2,}[ \t]*\n[ \t]*)?#{0,4}[ \t]*PUAN[ \t]*2\b',
                r'(?:^|\n)[ \t]*\*{0,2}[ \t]*PROF\S*[ \t]+PUANI\b'):
        m = re.search(pat, report_body, re.IGNORECASE)
        if m and (best is None or m.start() < best):
            best = m.start()
    if best is None:
        return report_body, ""
    return report_body[:best], report_body[best:]

def detect_profile_veto(text: str):
    """PUAN 2 veto etiketi: [VETO: <somut gerekçe + dakika>]. Yalnızca modelin açıkça yazdığı,
    yeterince somut (>=15 karakter gerekçeli) etiket geçerli — 'kör sayı kesmesi' yok.
    Dönüş: gerekçe metni | None."""
    if not text:
        return None
    m = re.search(r'\[\s*VETO\s*:\s*(.+?)\]', text, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    reason = re.sub(r'\s+', ' ', m.group(1)).strip()
    if len(reason) < 15:
        return None
    return reason[:500]

def strip_markdown(value: str) -> str:
    if not value:
        return ""
    value = re.sub(r'---(RAPOR|RAPORSON|STANDARTCV|STANDARTCVSON)---', '', value)
    value = value.replace('**', '')
    value = re.sub(r'^\s*[-*]\s+', '• ', value, flags=re.MULTILINE)
    return value.strip()

def parse_markdown_table(lines):
    rows = []
    consumed = set()
    for i, line in enumerate(lines):
        if '|' not in line:
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if len(cells) < 2:
            continue
        if all(set(c.replace(' ','')) <= set('-:') for c in cells):
            consumed.add(i)
            continue
        rows.append(cells)
        consumed.add(i)
    return rows, consumed

# ============ ROUTES ============
@app.get("/")
def root():
    return {"status": "MedeX Mülakat Sistemi çalışıyor"}

# ---- Admin Auth ----
@app.post("/api/admin/login")
def admin_login(data: AdminLogin, db=Depends(db_dep)):
    row = db.execute("SELECT * FROM admin_users WHERE email=? AND is_active=1", (data.email,)).fetchone()
    if row and hash_password(data.password) == row["password_hash"]:
        token = create_token({
            "role": "admin", "email": row["email"], "admin_id": row["id"],
            "org_id": row["org_id"], "admin_role": row["role"],
        })
        return {"token": token}
    # Geçiş güvenliği: admin_users'ta eşleşme yoksa mevcut env-var admin'i kabul et
    # (medex-admin normalde init_db() ile admin_users'a seed edilir, bu sadece yedek).
    if data.email == ADMIN_EMAIL and data.password == ADMIN_PASSWORD:
        token = create_token({
            "role": "admin", "email": data.email, "admin_id": None,
            "org_id": None, "admin_role": "superadmin",
        })
        return {"token": token}
    raise HTTPException(status_code=401, detail="Hatalı giriş bilgileri")

@app.get("/api/admin/profile")
def get_admin_profile(payload=Depends(verify_admin)):
    return {
        "admin_id": payload.get("admin_id"), "email": payload.get("email"),
        "org_id": payload.get("org_id"), "admin_role": payload.get("admin_role"),
    }

@app.put("/api/admin/profile")
def update_admin_profile(data: AdminProfileUpdate, payload=Depends(verify_admin), db=Depends(db_dep)):
    admin_id = payload.get("admin_id")
    if not admin_id:
        raise HTTPException(status_code=400, detail="Bu hesap için şifre değişikliği desteklenmiyor (env-var admin). Lütfen bir admin_users kaydı üzerinden giriş yapın.")
    row = db.execute("SELECT * FROM admin_users WHERE id=?", (admin_id,)).fetchone()
    if not row or hash_password(data.current_password) != row["password_hash"]:
        raise HTTPException(status_code=401, detail="Mevcut şifre hatalı")
    db.execute("UPDATE admin_users SET password_hash=? WHERE id=?", (hash_password(data.new_password), admin_id))
    db.commit()
    return {"message": "Şifre güncellendi"}

# ---- Süperadmin: Kurum (Organization) Yönetimi ----
@app.get("/api/superadmin/organizations")
def list_organizations(payload=Depends(verify_superadmin), db=Depends(db_dep)):
    rows = db.execute("""
        SELECT o.*,
            (SELECT COUNT(*) FROM admin_users a WHERE a.org_id = o.id) AS admin_count,
            (SELECT COUNT(*) FROM candidates c WHERE c.org_id = o.id) AS candidate_count
        FROM organizations o
        ORDER BY o.created_at DESC
    """).fetchall()
    return [dict(r) for r in rows]

@app.post("/api/superadmin/organizations")
def create_organization(data: OrganizationCreate, payload=Depends(verify_superadmin), db=Depends(db_dep)):
    try:
        db.execute("INSERT INTO organizations (name, slug) VALUES (?, ?)", (data.name, data.slug))
        db.commit()
    except (sqlite3.IntegrityError, psycopg.IntegrityError):
        raise HTTPException(status_code=400, detail="Bu slug zaten kullanılıyor")
    org_row = db.execute("SELECT id FROM organizations WHERE slug=?", (data.slug,)).fetchone()
    new_org_id = org_row["id"]

    # Her kurum, hazır pozisyon kataloğunun kendi bağımsız kopyasıyla başlar (MedeX'in
    # şablonundan türetilir); bu kopya üzerindeki değişiklikler diğer kurumları etkilemez.
    medex_org_id = get_medex_org_id(db)
    if medex_org_id:
        template = db.execute("SELECT name, category, role_description, criteria_json, active FROM positions WHERE org_id=?", (medex_org_id,)).fetchall()
        for p in template:
            db.execute(
                "INSERT INTO positions (name, category, role_description, criteria_json, active, org_id) VALUES (?, ?, ?, ?, ?, ?)",
                (p["name"], p["category"], p["role_description"], p["criteria_json"], p["active"], new_org_id)
            )
        db.commit()
    return {"id": new_org_id, "name": data.name, "slug": data.slug, "message": "Kurum oluşturuldu, pozisyon kataloğu MedeX şablonundan kopyalandı"}

@app.post("/api/superadmin/organizations/{org_id}/admins")
def create_org_admin(org_id: int, data: OrgAdminCreate, payload=Depends(verify_superadmin), db=Depends(db_dep)):
    org = db.execute("SELECT id FROM organizations WHERE id=?", (org_id,)).fetchone()
    if not org:
        raise HTTPException(status_code=404, detail="Kurum bulunamadı")
    password = data.password or generate_password()
    try:
        db.execute(
            "INSERT INTO admin_users (org_id, name, email, password_hash, role) VALUES (?, ?, ?, ?, ?)",
            (org_id, data.name, data.email, hash_password(password), "org_admin")
        )
        db.commit()
    except (sqlite3.IntegrityError, psycopg.IntegrityError):
        raise HTTPException(status_code=400, detail="Bu e-posta zaten kayıtlı")
    return {"email": data.email, "password": password, "message": "Kurum admini oluşturuldu"}

@app.get("/api/superadmin/organizations/{org_id}/admins")
def list_org_admins(org_id: int, payload=Depends(verify_superadmin), db=Depends(db_dep)):
    rows = db.execute(
        "SELECT id, org_id, name, email, role, is_active, created_at FROM admin_users WHERE org_id=? ORDER BY created_at DESC",
        (org_id,)
    ).fetchall()
    return [dict(r) for r in rows]

# ---- Position Management ----
@app.get("/api/admin/positions")
def list_positions(payload=Depends(verify_admin), org_id: Optional[int] = None, db=Depends(db_dep)):
    scoped_org_id = get_org_id_for_admin(db, payload, org_id)
    rows = db.execute("SELECT * FROM positions WHERE org_id=? ORDER BY created_at DESC", (scoped_org_id,)).fetchall()
    return [{
        "id": r["id"], "name": r["name"], "category": r["category"] if "category" in r.keys() else "Genel", "role_description": r["role_description"],
        "criteria": json.loads(r["criteria_json"]), "active": bool(r["active"]),
        "is_customized": bool(r["is_customized"]) if "is_customized" in r.keys() and r["is_customized"] is not None else False,
    } for r in rows]

# B3 — her pozisyon TAM 6 kriter içerir (standart, istisnasız). Pozisyona özgü ek beklentiler
# kriter değil, aday kaydındaki AI notu alanından iletilir.
POSITION_CRITERIA_COUNT = 6

def _validate_position_criteria(criteria):
    """B3 — panelden gelen pozisyon kriterlerini doğrular. HARD REJECT: 6 kriter değilse 400
    (init_db seed'inde ise yalnız loglanır — başlatmayı durdurmayız). Ağırlık toplamı 100 değilse
    uyarı döner (bloklamaz — mevcut davranış korunur)."""
    n = len(criteria)
    if n != POSITION_CRITERIA_COUNT:
        raise HTTPException(status_code=400,
                            detail=f"Her pozisyon tam {POSITION_CRITERIA_COUNT} kriter içermelidir (gönderilen: {n}). "
                                   f"Pozisyona özgü ek beklentileri aday kaydındaki 'AI notu' alanından iletin.")
    total = sum(c.weight for c in criteria)
    return None if total == 100 else f"Uyarı: kriter ağırlıkları toplamı {total}, 100 olması önerilir"

@app.post("/api/admin/positions")
def create_position(data: PositionCreate, payload=Depends(verify_admin), db=Depends(db_dep)):
    warning = _validate_position_criteria(data.criteria)
    org_id = get_org_id_for_admin(db, payload)
    try:
        db.execute(
            "INSERT INTO positions (name, category, role_description, criteria_json, org_id, is_customized) VALUES (?, ?, ?, ?, ?, 1)",
            (data.name, data.category, data.role_description, json.dumps([c.dict() for c in data.criteria], ensure_ascii=False), org_id)
        )
        db.commit()
    except (sqlite3.IntegrityError, psycopg.IntegrityError):
        raise HTTPException(status_code=400, detail="Bu pozisyon adı zaten var")
    return {"message": "Pozisyon eklendi", "warning": warning}

@app.put("/api/admin/positions/{position_id}")
def update_position(position_id: int, data: PositionCreate, payload=Depends(verify_admin), db=Depends(db_dep)):
    warning = _validate_position_criteria(data.criteria)
    org_id = get_org_id_for_admin(db, payload)
    owned = db.execute("SELECT id FROM positions WHERE id=? AND org_id=?", (position_id, org_id)).fetchone()
    if not owned:
        raise HTTPException(status_code=404, detail="Pozisyon bulunamadı")
    # B7 — panelden düzenlenen pozisyon is_customized=1 olur; init_db bir daha üzerine yazmaz.
    db.execute(
        "UPDATE positions SET name=?, category=?, role_description=?, criteria_json=?, is_customized=1 WHERE id=?",
        (data.name, data.category, data.role_description, json.dumps([c.dict() for c in data.criteria], ensure_ascii=False), position_id)
    )
    db.commit()
    return {"message": "Pozisyon güncellendi", "warning": warning}

@app.delete("/api/admin/positions/{position_id}")
def delete_position(position_id: int, payload=Depends(verify_admin), db=Depends(db_dep)):
    org_id = get_org_id_for_admin(db, payload)
    owned = db.execute("SELECT id FROM positions WHERE id=? AND org_id=?", (position_id, org_id)).fetchone()
    if not owned:
        raise HTTPException(status_code=404, detail="Pozisyon bulunamadı")
    db.execute("UPDATE positions SET active=0 WHERE id=?", (position_id,))
    db.commit()
    return {"message": "Pozisyon pasifleştirildi"}

# ---- Candidate Management ----
@app.get("/api/admin/candidates")
def get_candidates(payload=Depends(verify_admin), org_id: Optional[int] = None, db=Depends(db_dep)):
    scoped_org_id = get_org_id_for_admin(db, payload, org_id)
    rows = db.execute("""
        SELECT c.*, i.score, i.score_position, i.score_profile, i.recommendation, i.completed_at as interview_completed,
               i.completed_at as interview_completed_at, i.total_input_tokens, i.total_output_tokens,
               i.processing_status, i.processing_error, i.started_at,
               i.partial, i.completion_pct, i.technical_error_ref
        FROM candidates c
        LEFT JOIN interviews i ON c.id = i.candidate_id AND i.level = c.level
        WHERE c.org_id=?
        ORDER BY c.created_at DESC
    """, (scoped_org_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d.update(derive_attempt_status(d))
        out.append(d)
    return out

@app.get("/api/admin/persons/{person_id}")
def get_person(person_id: int, payload=Depends(verify_admin), db=Depends(db_dep)):
    """Bir kişinin (person) tüm başvuru denemelerini (arşivli dahil) tek yerde döner — Faz 3'teki toplu görünümün temeli."""
    scoped_org_id = get_org_id_for_admin(db, payload)
    person = db.execute("SELECT * FROM persons WHERE id=? AND org_id=?", (person_id, scoped_org_id)).fetchone()
    if not person:
        raise HTTPException(status_code=404, detail="Kişi bulunamadı")
    attempts = db.execute("""
        SELECT c.id as candidate_id, c.name, c.email, c.phone, c.position, c.level, c.depth_tier,
               c.interview_language, c.report_language, c.education, c.university, c.department,
               c.experience_years, c.ai_note, c.status, c.invite_type,
               c.is_archived, c.created_at, c.completed_at, c.terminated_reason,
               c.login_count, c.first_login_at, c.last_login_at,
               c.interview_start_count, c.last_start_at, c.invite_expires_at,
               i.score, i.score_position, i.score_profile, i.recommendation, i.completed_at as interview_completed_at,
               i.processing_status, i.processing_error, i.started_at,
               i.partial, i.completion_pct, i.technical_error_ref
        FROM candidates c
        LEFT JOIN interviews i ON i.candidate_id = c.id AND i.level = c.level
        WHERE c.person_id = ?
        ORDER BY c.created_at DESC
    """, (person_id,)).fetchall()
    out = []
    for r in attempts:
        d = dict(r)
        d.update(derive_attempt_status(d))
        out.append(d)
    return {
        "person": dict(person),
        "attempts": out,
    }

@app.get("/api/admin/persons/{person_id}/notes")
def list_person_notes(person_id: int, payload=Depends(verify_admin), db=Depends(db_dep)):
    scoped_org_id = get_org_id_for_admin(db, payload)
    person = db.execute("SELECT id FROM persons WHERE id=? AND org_id=?", (person_id, scoped_org_id)).fetchone()
    if not person:
        raise HTTPException(status_code=404, detail="Kişi bulunamadı")
    rows = db.execute("SELECT * FROM person_notes WHERE person_id=? ORDER BY created_at DESC, id DESC", (person_id,)).fetchall()
    return [dict(r) for r in rows]

@app.post("/api/admin/persons/{person_id}/notes")
def create_person_note(person_id: int, data: PersonNoteCreate, payload=Depends(verify_admin), db=Depends(db_dep)):
    scoped_org_id = get_org_id_for_admin(db, payload)
    person = db.execute("SELECT id FROM persons WHERE id=? AND org_id=?", (person_id, scoped_org_id)).fetchone()
    if not person:
        raise HTTPException(status_code=404, detail="Kişi bulunamadı")
    db.execute(
        "INSERT INTO person_notes (person_id, org_id, admin_user_id, note_type, body) VALUES (?, ?, ?, 'manual', ?)",
        (person_id, scoped_org_id, payload.get("admin_id"), data.body)
    )
    db.commit()
    note = db.execute("SELECT * FROM person_notes WHERE person_id=? ORDER BY id DESC LIMIT 1", (person_id,)).fetchone()
    return dict(note)

@app.post("/api/admin/persons/{person_id}/evaluate")
def evaluate_person(person_id: int, payload=Depends(verify_admin)):
    """Kişinin tüm tamamlanmış mülakat raporlarını Claude'a okutup çapraz bir özet çıkarır.
    Level 2 canlı akışına dokunmaz — sadece geçmişte üretilmiş rapor metnini (L1/L2/L3 fark etmez) girdi olarak okur."""
    db = get_db()
    scoped_org_id = get_org_id_for_admin(db, payload)
    person = db.execute("SELECT * FROM persons WHERE id=? AND org_id=?", (person_id, scoped_org_id)).fetchone()
    if not person:
        db.close()
        raise HTTPException(status_code=404, detail="Kişi bulunamadı")
    attempts = db.execute("""
        SELECT c.position, c.level, c.created_at, i.report, i.score, i.recommendation
        FROM candidates c
        JOIN interviews i ON i.candidate_id = c.id AND i.level = c.level
        WHERE c.person_id = ? AND i.report IS NOT NULL AND i.report != ''
        ORDER BY c.created_at ASC
    """, (person_id,)).fetchall()
    db.close()
    if not attempts:
        raise HTTPException(status_code=400, detail="Bu kişi için tamamlanmış/raporlu mülakat bulunamadı")
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="Sistem yapılandırma hatası (API anahtarı eksik). Lütfen yöneticinize bildirin.")

    reports_text = "\n\n---\n\n".join(
        f"[Pozisyon: {a['position']} | Level {a['level']} | Tarih: {a['created_at']} | Puan: {a['score']} | Öneri: {a['recommendation']}]\n{a['report']}"
        for a in attempts
    )
    prompt = (
        f"Aşağıda \"{person['full_name']}\" adlı kişinin farklı pozisyon ve/veya level'larda yaptığı "
        f"{len(attempts)} ayrı mülakatın raporları yer alıyor. Bu raporları birlikte değerlendirip "
        "kısa (en fazla 250 kelime), Türkçe bir özet çıkar: genel güçlü/zayıf yönler, denemeler "
        "arasındaki tutarlılık veya çelişkiler, ve genel bir işe alım önerisi.\n\n" + reports_text
    )
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=60.0)
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        summary = response.content[0].text
        print(f"[AI_PROVIDER] level=cross-person provider=claude action=person_evaluate person_id={person_id}")
    except anthropic.APIError as e:
        err = ai_error_from_anthropic(e, "evaluate_person", {}, severity="user")
        raise ai_http_exception(err)
    except Exception as e:
        print(f"HATA (evaluate_person, beklenmeyen): {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="Değerlendirme oluşturulurken beklenmeyen bir hata oluştu.")

    db = get_db()
    db.execute(
        "INSERT INTO person_notes (person_id, org_id, admin_user_id, note_type, body) VALUES (?, ?, ?, 'ai_summary', ?)",
        (person_id, scoped_org_id, payload.get("admin_id"), summary)
    )
    db.commit()
    note = db.execute("SELECT * FROM person_notes WHERE person_id=? ORDER BY id DESC LIMIT 1", (person_id,)).fetchone()
    db.close()
    return dict(note)

# ---- Hata Kayıtları (adaya gösterilen / arka plan AI hataları) ----
@app.get("/api/admin/error-logs")
def list_error_logs(payload=Depends(verify_admin), db=Depends(db_dep),
                    date_from: Optional[str] = None, date_to: Optional[str] = None,
                    candidate_id: Optional[int] = None, error_class: Optional[str] = None,
                    resolved: Optional[str] = None, include_background: Optional[str] = None,
                    limit: int = 200):
    """Filtrelenebilir hata kaydı listesi + çözülmemiş kritik hata sayısı (kırmızı bandı besler).
    Varsayılan: yalnızca severity='user' (arka plan hataları gizli). Teknik detay ayrı alanda döner."""
    where = ["1=1"]
    params: list = []
    if not (include_background and str(include_background).lower() in ("1", "true", "yes")):
        where.append("(severity IS NULL OR severity = 'user')")
    if date_from:
        where.append("created_at >= ?"); params.append(date_from)
    if date_to:
        where.append("created_at <= ?"); params.append(date_to)
    if candidate_id is not None:
        where.append("candidate_id = ?"); params.append(candidate_id)
    if error_class:
        where.append("error_class = ?"); params.append(error_class)
    if resolved is not None and str(resolved) != "":
        want = 1 if str(resolved).lower() in ("1", "true", "yes") else 0
        where.append("resolved = ?"); params.append(want)
    try:
        lim = max(1, min(int(limit), 1000))
    except Exception:
        lim = 200
    rows = db.execute(
        f"SELECT * FROM error_logs WHERE {' AND '.join(where)} ORDER BY created_at DESC, id DESC LIMIT {lim}",
        tuple(params)
    ).fetchall()
    crit = sorted(_CRITICAL_CLASSES)
    crit_placeholders = ",".join("?" for _ in crit)
    unresolved_critical = db.execute(
        f"SELECT COUNT(*) AS n FROM error_logs WHERE resolved = 0 AND (severity IS NULL OR severity = 'user') "
        f"AND error_class IN ({crit_placeholders})",
        tuple(crit)
    ).fetchone()["n"]
    return {"logs": [dict(r) for r in rows], "unresolved_critical": unresolved_critical}

@app.post("/api/admin/error-logs/{log_id}/resolve")
def resolve_error_log(log_id: int, payload=Depends(verify_admin), db=Depends(db_dep)):
    row = db.execute("SELECT id FROM error_logs WHERE id = ?", (log_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Hata kaydı bulunamadı")
    db.execute(
        "UPDATE error_logs SET resolved = 1, resolved_at = ?, resolved_by = ? WHERE id = ?",
        (_now_ts(), (payload.get("username") or payload.get("email") or "admin"), log_id)
    )
    db.commit()
    return {"message": "Hata kaydı çözüldü olarak işaretlendi", "id": log_id}

# ---- CV Havuzu (bireysel/genel başvuranlar — org_id=NULL, kuruma özel değil) ----
@app.get("/api/admin/cv-pool")
def get_cv_pool(payload=Depends(verify_admin), db=Depends(db_dep)):
    """Genel başvuru ile üye olmuş, henüz hiçbir kuruma davet edilmemiş kişiler.
    Bu havuz kuruma özel değildir — hangi kurumun admini olursa olsun görebilir."""
    rows = db.execute("""
        SELECT id, name, email, phone, position, education, university, department,
               experience_years, ai_note, cv_filename, cv_text, status, created_at, person_id
        FROM candidates
        WHERE invite_type='general'
        ORDER BY created_at DESC
    """).fetchall()
    return [dict(r) for r in rows]

@app.post("/api/admin/cv-pool/{candidate_id}/invite")
def invite_from_cv_pool(candidate_id: int, data: CvPoolInvite, payload=Depends(verify_admin), db=Depends(db_dep)):
    """Havuzdaki bir kişiyi, çağıran adminin kurumuna gerçek bir mülakat davetine çevirir
    (yeni kullanıcı adı/şifre üretilir, kuruma özel person kaydı açılır)."""
    pool_candidate = db.execute("SELECT * FROM candidates WHERE id=? AND invite_type='general'", (candidate_id,)).fetchone()
    if not pool_candidate:
        raise HTTPException(status_code=404, detail="Havuzda böyle bir kayıt bulunamadı")

    org_id = get_org_id_for_admin(db, payload)
    person_id = find_or_create_person(db, org_id, pool_candidate["name"], pool_candidate["email"], pool_candidate["phone"])
    username = generate_username(pool_candidate["name"], db)
    password = generate_password()
    password_hash = hash_password(password)

    insert_sql = """
        INSERT INTO candidates (name, email, phone, education, university, department, experience_years, ai_note, position, level, depth_tier, interview_language, report_language, username, password_hash, plain_password, invite_type, cv_text, cv_filename, org_id, person_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'invite', ?, ?, ?, ?)
    """
    params = (
        pool_candidate["name"], pool_candidate["email"], pool_candidate["phone"],
        pool_candidate["education"], pool_candidate["university"], pool_candidate["department"],
        pool_candidate["experience_years"] or 0, pool_candidate["ai_note"],
        data.position, data.level, data.depth_tier, data.interview_language, data.report_language,
        username, password_hash, password,
        pool_candidate["cv_text"], pool_candidate["cv_filename"], org_id, person_id,
    )
    if USE_POSTGRES:
        new_id = db.execute(insert_sql + " RETURNING id", params).fetchone()["id"]
    else:
        db.execute(insert_sql, params)
        new_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute("UPDATE candidates SET invite_expires_at = ? WHERE id = ?", (_invite_expiry(), new_id))
    db.commit()
    db.close()

    mail_sent = False
    if data.send_email and pool_candidate["email"]:
        mail_sent = send_invite_email(pool_candidate["name"], pool_candidate["email"], username, password, data.position)

    return {
        "id": new_id, "username": username, "password": password, "mail_sent": mail_sent,
        "message": "Havuzdaki kişi davet edildi" + (", mail gönderildi" if mail_sent else "")
    }

@app.post("/api/admin/candidates")
def create_candidate(data: CandidateCreate, payload=Depends(verify_admin), db=Depends(db_dep)):
    previous = find_latest_candidate_by_email(db, data.email) if data.email else None
    previous_id = previous["id"] if previous else None
    if previous_id:
        db.execute("UPDATE candidates SET is_archived=1 WHERE id=?", (previous_id,))
    username = generate_username(data.name, db)
    password = generate_password()
    password_hash = hash_password(password)
    org_id = get_org_id_for_admin(db, payload)
    person_id = find_or_create_person(db, org_id, data.name, data.email, data.phone)

    insert_sql = """
        INSERT INTO candidates (name, email, phone, education, university, department, experience_years, ai_note, position, level, depth_tier, interview_language, report_language, username, password_hash, plain_password, invite_type, previous_candidate_id, org_id, person_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'invite', ?, ?, ?)
    """
    params = (data.name, normalize_email(data.email), data.phone, data.education, data.university, data.department, data.experience_years or 0, data.ai_note, data.position, data.level or 1, data.depth_tier or "standart", data.interview_language or "tr", data.report_language or "tr", username, password_hash, password, previous_id, org_id, person_id)
    if USE_POSTGRES:
        candidate_id = db.execute(insert_sql + " RETURNING id", params).fetchone()["id"]
    else:
        db.execute(insert_sql, params)
        candidate_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute("UPDATE candidates SET invite_expires_at = ? WHERE id = ?", (_invite_expiry(), candidate_id))
    db.commit()
    db.close()

    mail_sent = False
    if data.send_email and data.email:
        mail_sent = send_invite_email(data.name, data.email, username, password, data.position)

    return {
        "id": candidate_id, "username": username, "password": password,
        "mail_sent": mail_sent,
        "message": "Aday eklendi" + (", davet maili gönderildi" if mail_sent else "")
    }

# ---- Walk-in (Hızlı Giriş) ----
@app.post("/api/admin/walkin")
def create_walkin(data: CandidateCreate, payload=Depends(verify_admin), db=Depends(db_dep)):
    email = data.email or f"walkin_{secrets.token_hex(4)}@medex-smo.local"
    username = generate_username(data.name, db)
    password = generate_password()
    password_hash = hash_password(password)
    org_id = get_org_id_for_admin(db, payload)
    # Walk-in'de sahte e-posta üretildiği için kişi eşleştirmesi çoğunlukla telefon üzerinden olur.
    person_id = find_or_create_person(db, org_id, data.name, data.email, data.phone)

    insert_sql = """
        INSERT INTO candidates (name, email, phone, education, university, department, experience_years, ai_note, position, level, depth_tier, interview_language, report_language, username, password_hash, plain_password, invite_type, org_id, person_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'walkin', ?, ?)
    """
    params = (data.name, email, data.phone, data.education, data.university, data.department, data.experience_years or 0, data.ai_note, data.position, data.level or 1, data.depth_tier or "standart", data.interview_language or "tr", data.report_language or "tr", username, password_hash, password, org_id, person_id)
    if USE_POSTGRES:
        candidate_id = db.execute(insert_sql + " RETURNING id", params).fetchone()["id"]
    else:
        db.execute(insert_sql, params)
        candidate_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit()

    return {
        "id": candidate_id, "username": username, "password": password,
        "message": "Walk-in aday oluşturuldu. Bu bilgilerle hemen giriş yapabilir."
    }

# ---- Resend invite (mevcut şifreyle) / show credentials / reset password / delete ----
@app.post("/api/admin/candidates/{candidate_id}/resend")
def resend_invite(candidate_id: int, payload=Depends(verify_admin), db=Depends(db_dep)):
    """Mevcut şifreyi DEĞİŞTİRMEDEN aynı bilgilerle maili tekrar gönderir."""
    candidate = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    if not candidate:
        raise HTTPException(status_code=404, detail="Aday bulunamadı")
    # Davet tekrar gönderildi: geçerlilik süresini bugünden itibaren yeniden uzat.
    try:
        db.execute("UPDATE candidates SET invite_expires_at = ? WHERE id = ?", (_invite_expiry(), candidate_id))
        db.commit()
    except Exception as e:
        print(f"UYARI (resend_invite süre uzatma c={candidate_id}): {type(e).__name__}: {e}")
    db.close()  # yavaş e-posta gönderiminden önce bağlantıyı bilerek erken kapatıyoruz

    if not candidate["plain_password"]:
        raise HTTPException(status_code=400, detail="Bu adayın şifresi sistemde saklanmıyor (eski kayıt). Şifre Sıfırla kullanın.")

    mail_attempted = bool(candidate["email"] and "@medex-smo.local" not in candidate["email"])
    mail_sent = False
    if mail_attempted:
        try:
            mail_sent = send_invite_email(candidate["name"], candidate["email"], candidate["username"], candidate["plain_password"], candidate["position"])
        except Exception as e:
            print(f"UYARI (resend_invite davet maili c={candidate_id}): {type(e).__name__}: {e}")
            mail_sent = False

    return {
        "mail_sent": mail_sent, "mail_attempted": mail_attempted,
        "username": candidate["username"], "password": candidate["plain_password"],
        "message": "Mail tekrar gönderildi (şifre değişmedi)" if mail_sent else "Mail gönderilemedi (bilgileri manuel iletin)"
    }

@app.post("/api/admin/candidates/{candidate_id}/show-credentials")
def show_credentials(candidate_id: int, payload=Depends(verify_admin), db=Depends(db_dep)):
    """Mevcut şifreyi DEĞİŞTİRMEDEN ekranda gösterir."""
    candidate = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    if not candidate:
        raise HTTPException(status_code=404, detail="Aday bulunamadı")
    if not candidate["plain_password"]:
        raise HTTPException(status_code=400, detail="Bu adayın şifresi sistemde saklanmıyor (eski kayıt). Şifre Sıfırla kullanın.")
    return {"username": candidate["username"], "password": candidate["plain_password"]}

@app.post("/api/admin/candidates/{candidate_id}/reset-password")
def reset_password(candidate_id: int, payload=Depends(verify_admin), db=Depends(db_dep)):
    """Yeni şifre üretir, eskisini geçersiz kılar. Ayrı, bilinçli bir aksiyon."""
    candidate = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    if not candidate:
        raise HTTPException(status_code=404, detail="Aday bulunamadı")
    new_password = generate_password()
    db.execute("UPDATE candidates SET password_hash=?, plain_password=? WHERE id=?",
               (hash_password(new_password), new_password, candidate_id))
    db.commit()
    return {"username": candidate["username"], "password": new_password, "message": "Şifre sıfırlandı"}

@app.post("/api/admin/candidates/{candidate_id}/allow-reapply")
def allow_reapply(candidate_id: int, payload=Depends(verify_admin), db=Depends(db_dep)):
    candidate = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    if not candidate:
        raise HTTPException(status_code=404, detail="Aday bulunamadı")
    new_value = 0 if candidate["reapply_allowed"] else 1
    db.execute("UPDATE candidates SET reapply_allowed=? WHERE id=?", (new_value, candidate_id))
    db.commit()
    return {"reapply_allowed": bool(new_value), "message": "Tekrar başvuru izni güncellendi"}

@app.delete("/api/admin/candidates/{candidate_id}")
def delete_candidate(candidate_id: int, payload=Depends(verify_admin), db=Depends(db_dep)):
    db.execute("DELETE FROM interviews WHERE candidate_id=?", (candidate_id,))
    db.execute("DELETE FROM candidates WHERE id=?", (candidate_id,))
    db.commit()
    return {"message": "Aday silindi"}

# ---- Admin CV Upload ----
CV_EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')

def _cv_ownership_check(candidate_email: Optional[str], cv_text: Optional[str]) -> dict:
    """İsim yerine e-posta ile sahiplik kontrolü (isim eşleşmesi kaldırıldı — yazım/kısaltma
    farkları çok fazla yanlış pozitif üretiyordu, e-posta daha güvenilir bir kimlik alanı).
    Kesin/kanıtlanmış bir doğrulama değil; eşleşmediğinde veya belirsiz olduğunda kayıt
    REDDEDİLMEZ, sadece admine görünür bir uyarı/bilgi notu döner."""
    found_emails = sorted(set(m.group(0).strip().lower() for m in CV_EMAIL_RE.finditer(cv_text or "")))
    if not found_emails:
        return {"warning": None, "note": "CV'de e-posta bulunamadı, sahiplik doğrulanamadı."}
    cand_email = (candidate_email or "").strip().lower()
    if cand_email and cand_email in found_emails:
        return {"warning": None, "note": None}
    return {
        "warning": "Yüklenen CV'de aday e-postası bulunamadı, CV başka kişiye ait olabilir. "
                    f"CV'de bulunan e-postalar: {', '.join(found_emails)}",
        "note": None,
    }

@app.post("/api/admin/candidates/{candidate_id}/upload-cv")
async def admin_upload_cv(candidate_id: int, file: UploadFile = File(...), payload=Depends(verify_admin)):
    if not (file.filename.lower().endswith(".pdf") or file.filename.lower().endswith(".docx")):
        raise HTTPException(status_code=400, detail="Sadece PDF veya Word (.docx) dosyası yükleyebilirsiniz")
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Dosya boyutu 10MB'ı geçemez")
    cv_text = extract_cv_text(file.filename, content)
    cv_err = cv_extraction_error(cv_text)
    if cv_err:
        # Bozuk/boş/uyumsuz CV: hiç kaydetme, akışı temiz kes, net mesaj dön.
        raise HTTPException(status_code=400, detail=cv_err)

    db = None
    try:
        db = get_db()
        candidate = db.execute("SELECT id, email FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if not candidate:
            raise HTTPException(status_code=404, detail="Aday bulunamadı")
        db.execute("UPDATE candidates SET cv_text=?, cv_filename=? WHERE id=?", (cv_text, file.filename, candidate_id))
        db.commit()
    except HTTPException:
        if db:
            db.rollback()
        raise
    except Exception as e:
        if db:
            db.rollback()
        print(f"HATA (admin_upload_cv candidate_id={candidate_id}): {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="CV yüklenirken beklenmeyen bir hata oluştu.")
    finally:
        if db:
            db.close()

    ownership = _cv_ownership_check(candidate["email"], cv_text)
    return {
        "message": "CV yüklendi", "preview": cv_text[:300],
        "cv_ownership_warning": ownership["warning"], "cv_ownership_note": ownership["note"],
    }

@app.patch("/api/admin/candidates/{candidate_id}")
def admin_update_candidate(candidate_id: int, data: CandidateUpdate, payload=Depends(verify_admin)):
    db = None
    try:
        db = get_db()
        candidate = db.execute("SELECT id, level FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if not candidate:
            raise HTTPException(status_code=404, detail="Aday bulunamadı")

        # TAMAMLANMIŞ MÜLAKAT KİLİDİ: bu adayın herhangi bir seviyede tamamlanmış (completed_at
        # dolu) bir mülakat kaydı varsa artık düzenlenemez — değişiklik "Yeni çağrı" ile yapılır.
        finished_row = db.execute(
            "SELECT 1 FROM interviews WHERE candidate_id=? AND completed_at IS NOT NULL LIMIT 1",
            (candidate_id,)
        ).fetchone()
        if finished_row:
            raise HTTPException(status_code=409, detail="Tamamlanmış mülakat düzenlenemez. Yeni çağrı açın.")

        # KISMİ YAZMA: sadece istekte gerçekten gönderilen alanlar yazılır — CandidateCreate'in
        # tam-değiştirme davranışı (gönderilmeyen alanı None'a düşürme) burada terk edildi.
        fields = data.model_dump(exclude_unset=True)
        if "email" in fields:
            fields["email"] = normalize_email(fields["email"])

        new_level = fields.get("level", candidate["level"] or 1)
        level_changed = "level" in fields and (candidate["level"] or 1) != new_level

        if fields:
            set_clause = ", ".join(f"{k}=?" for k in fields.keys())
            db.execute(f"UPDATE candidates SET {set_clause} WHERE id=?", list(fields.values()) + [candidate_id])

        if level_changed:
            # Aday farklı bir seviyeye taşındı: bu seviye için yeni bir mülakat denemesi
            # başlatılabilsin diye durumu sıfırla. Önceki seviyenin kaydı (interviews
            # tablosunda level ile ayrı satır) olduğu gibi kalır, silinmez.
            db.execute("""
                UPDATE candidates SET status='pending', completed_at=NULL, violation_count=0, terminated_reason=NULL
                WHERE id=?
            """, (candidate_id,))

        db.commit()
        return {"message": "Aday bilgileri güncellendi" + (" (yeni seviye için mülakat sıfırlandı)" if level_changed else "")}
    except HTTPException:
        if db:
            db.rollback()
        raise
    except Exception as e:
        if db:
            db.rollback()
        print(f"HATA (admin_update_candidate candidate_id={candidate_id}): {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="Aday güncellenirken beklenmeyen bir hata oluştu.")
    finally:
        if db:
            db.close()

@app.post("/api/admin/candidates/{candidate_id}/new-attempt")
def create_new_attempt(candidate_id: int, data: NewAttemptRequest, payload=Depends(verify_admin), db=Depends(db_dep)):
    """Tamamlanmış (veya herhangi bir) mülakat kaydından yeni bir mülakat çağrısı türetir.
    Kimlik/eğitim/CV alanları kaynak candidate satırından birebir kopyalanır; pozisyon, seviye,
    derinlik, dil ve AI notu gövdeden alınır; yeni kullanıcı adı/şifre üretilir; person_id ve
    org_id kaynaktan aynen taşınır. KAYNAK KAYDA DOKUNULMAZ — arşivlenmez, alanı değişmez.
    Alan kopyalama/kullanıcı üretimi deseni cv-pool invite ile aynıdır."""
    src = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    if not src:
        raise HTTPException(status_code=404, detail="Kaynak aday bulunamadı")

    src_keys = src.keys()

    def _src(col, fallback=None):
        return src[col] if col in src_keys else fallback

    # KOPYALAMA KURALI: gövdede None gelen alan kaynak candidate satırından kopyalanır —
    # sabit varsayılan asla kullanılmaz (aksi halde kaynağın dili/seviyesi sessizce ezilirdi).
    position = data.position if data.position is not None else src["position"]
    level = data.level if data.level is not None else (_src("level") or 1)
    depth_tier = data.depth_tier if data.depth_tier is not None else (_src("depth_tier") or "standart")
    interview_language = data.interview_language if data.interview_language is not None else (_src("interview_language") or "tr")
    report_language = data.report_language if data.report_language is not None else (_src("report_language") or "tr")
    ai_note = data.ai_note if data.ai_note is not None else _src("ai_note")
    if not position:
        raise HTTPException(status_code=400, detail="Yeni çağrı için pozisyon gereklidir")

    username = generate_username(src["name"], db)
    password = generate_password()
    password_hash = hash_password(password)
    src_org_id = _src("org_id")
    src_person_id = _src("person_id")

    insert_sql = """
        INSERT INTO candidates (name, email, phone, education, university, department, experience_years, ai_note, position, level, depth_tier, interview_language, report_language, username, password_hash, plain_password, invite_type, cv_text, cv_filename, org_id, person_id, previous_candidate_id, status, is_archived)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'invite', ?, ?, ?, ?, ?, 'pending', 0)
    """
    params = (
        src["name"], src["email"], src["phone"], src["education"], src["university"], src["department"],
        src["experience_years"] or 0, ai_note, position, level, depth_tier,
        interview_language, report_language, username, password_hash, password,
        src["cv_text"], src["cv_filename"], src_org_id, src_person_id, candidate_id,
    )
    if USE_POSTGRES:
        new_id = db.execute(insert_sql + " RETURNING id", params).fetchone()["id"]
    else:
        db.execute(insert_sql, params)
        new_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute("UPDATE candidates SET invite_expires_at = ? WHERE id = ?", (_invite_expiry(), new_id))
    db.commit()
    db.close()  # yavaş e-posta gönderiminden önce bağlantıyı bilerek erken kapat (create_candidate ile aynı)

    # DAVET MAİLİ — kayıt açma işlemini ETKİLEMEZ: kayıt yukarıda commit edildi, mail buradan
    # sonra denenir; patlarsa (send_invite_email zaten kendi içinde yutar, ek güvenlik ağı da var)
    # mail_sent=False döner, admin kimlik bilgilerini elle iletir. Diğer davet uçlarıyla aynı desen.
    target_email = (src["email"] or "").strip()
    mail_attempted = bool(data.send_email and target_email and "@medex-smo.local" not in target_email)
    mail_sent = False
    if mail_attempted:
        try:
            mail_sent = send_invite_email(src["name"], target_email, username, password, position)
        except Exception as e:
            print(f"UYARI (create_new_attempt davet maili c={new_id}): {type(e).__name__}: {e}")
            mail_sent = False

    return {
        "id": new_id, "username": username, "password": password,
        "mail_sent": mail_sent, "mail_attempted": mail_attempted,
        "message": "Yeni mülakat çağrısı oluşturuldu" + (", davet maili gönderildi" if mail_sent else ""),
    }

# ---- CV Upload ----
@app.post("/api/candidate/upload-cv")
async def upload_cv(file: UploadFile = File(...), payload=Depends(verify_token), db=Depends(db_dep)):
    if payload.get("role") != "candidate":
        raise HTTPException(status_code=403, detail="Yetkisiz")

    if not (file.filename.lower().endswith(".pdf") or file.filename.lower().endswith(".docx")):
        raise HTTPException(status_code=400, detail="Sadece PDF veya Word (.docx) dosyası yükleyebilirsiniz")

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Dosya boyutu 10MB'ı geçemez")

    cv_text = extract_cv_text(file.filename, content)
    cv_err = cv_extraction_error(cv_text)
    if cv_err:
        # Bozuk/boş/uyumsuz CV: kaydetme, akışı temiz kes. Adaya sadece bu anlaşılır mesaj gider.
        raise HTTPException(status_code=400, detail=cv_err)

    db.execute("UPDATE candidates SET cv_text=?, cv_filename=? WHERE id=?",
               (cv_text, file.filename, payload["candidate_id"]))
    db.commit()

    return {"message": "CV yüklendi ve okundu", "preview": cv_text[:300]}

# ---- General Application ----
@app.get("/api/positions")
def get_positions_public(db=Depends(db_dep)):
    medex_org_id = get_medex_org_id(db)
    rows = db.execute("SELECT name, category FROM positions WHERE active=1 AND org_id=? ORDER BY category, name", (medex_org_id,)).fetchall()
    groups = {}
    for r in rows:
        groups.setdefault(r["category"] or "Genel", []).append(r["name"])
    return {"positions": [r["name"] for r in rows], "groups": groups}

@app.post("/api/apply")
async def general_apply(request: Request, db=Depends(db_dep)):
    """Genel başvuru. JSON veya multipart/form-data kabul eder; CV opsiyoneldir."""
    content_type = request.headers.get("content-type", "")
    cv_text = None
    cv_filename = None

    if "multipart/form-data" in content_type:
        form = await request.form()
        name = str(form.get("name") or "").strip()
        email = str(form.get("email") or "").strip()
        phone = str(form.get("phone") or "").strip()
        position = str(form.get("position") or "").strip()
        education = str(form.get("education") or "").strip()
        university = str(form.get("university") or "").strip()
        department = str(form.get("department") or "").strip()
        ai_note = str(form.get("ai_note") or "").strip()
        try:
            experience_years = int(form.get("experience_years") or 0)
        except Exception as e:
            print(f"UYARI (general_apply/multipart: experience_years sayıya çevrilemedi): {type(e).__name__}: {e}")
            experience_years = 0
        file = form.get("cv_file")
        if file is not None and getattr(file, "filename", ""):
            if not (file.filename.lower().endswith(".pdf") or file.filename.lower().endswith(".docx")):
                raise HTTPException(status_code=400, detail="CV sadece PDF veya Word (.docx) olabilir")
            content = await file.read()
            if len(content) > 10 * 1024 * 1024:
                raise HTTPException(status_code=400, detail="CV dosyası 10MB'ı geçemez")
            cv_text = extract_cv_text(file.filename, content)
            cv_filename = file.filename
    else:
        payload = await request.json()
        name = str(payload.get("name") or "").strip()
        email = str(payload.get("email") or "").strip()
        phone = str(payload.get("phone") or "").strip()
        position = str(payload.get("position") or "").strip()
        education = str(payload.get("education") or "").strip()
        university = str(payload.get("university") or "").strip()
        department = str(payload.get("department") or "").strip()
        ai_note = str(payload.get("ai_note") or "").strip()
        try:
            experience_years = int(payload.get("experience_years") or 0)
        except Exception as e:
            print(f"UYARI (general_apply/json: experience_years sayıya çevrilemedi): {type(e).__name__}: {e}")
            experience_years = 0

    if not all([name, email, phone, position, education]):
        raise HTTPException(status_code=400, detail="Ad soyad, e-posta, telefon, pozisyon ve eğitim bilgisi zorunludur")

    previous = find_latest_candidate_by_email(db, email)
    previous_id = None
    if previous:
        if not previous["reapply_allowed"]:
            raise HTTPException(status_code=400, detail="Bu e-posta ile daha önce başvuru yapılmış. Tekrar başvuru için lütfen yönetici onayı isteyin.")
        previous_id = previous["id"]
        db.execute("UPDATE candidates SET reapply_allowed=0, is_archived=1 WHERE id=?", (previous_id,))

    username = generate_username(name, db)
    password = generate_password()
    password_hash = hash_password(password)
    # Genel/bireysel başvuru her zaman org_id=NULL (kurum dışı CV havuzu) olarak etiketlenir.
    person_id = find_or_create_person(db, None, name, email, phone)
    db.execute("""
        INSERT INTO candidates (name, email, phone, education, university, department, experience_years, ai_note, position, username, password_hash, plain_password, invite_type, previous_candidate_id, cv_text, cv_filename, org_id, person_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'general', ?, ?, ?, NULL, ?)
    """, (name, normalize_email(email), phone, education, university, department, experience_years, ai_note, position, username, password_hash, password, previous_id, cv_text, cv_filename, person_id))
    db.execute("UPDATE candidates SET invite_expires_at = ? WHERE username = ?", (_invite_expiry(), username))
    db.commit()
    db.close()  # yavaş e-posta gönderiminden önce bağlantıyı bilerek erken kapatıyoruz

    send_invite_email(name, email, username, password, position)
    return {"message": "Başvurunuz alındı, giriş bilgileri e-posta adresinize gönderildi"}

# ---- Candidate Auth & Interview ----
@app.post("/api/candidate/login")
def candidate_login(data: CandidateLogin, db=Depends(db_dep)):
    candidate = db.execute(
        "SELECT * FROM candidates WHERE username=? AND password_hash=?",
        (data.username, hash_password(data.password))
    ).fetchone()

    if not candidate:
        raise HTTPException(status_code=401, detail="Hatalı kullanıcı adı veya şifre")
    if candidate["status"] == "completed":
        raise HTTPException(status_code=400, detail="Mülakatınız tamamlanmış")

    # Teşebbüs izleme (Mülakat Denemeleri ekranı): davet linki kaç kez açıldı / ilk-son giriş.
    try:
        db.execute(
            "UPDATE candidates SET login_count = COALESCE(login_count, 0) + 1, "
            "first_login_at = COALESCE(first_login_at, ?), last_login_at = ? WHERE id = ?",
            (_now_ts(), _now_ts(), candidate["id"])
        )
        db.commit()
    except Exception as e:
        print(f"UYARI (candidate_login teşebbüs sayacı c={candidate['id']}): {type(e).__name__}: {e}")

    token = create_token({
        "role": "candidate", "candidate_id": candidate["id"],
        "name": candidate["name"], "position": candidate["position"], "level": candidate["level"] or 1
    }, days=1)
    return {
        "token": token,
        "candidate": {
            "id": candidate["id"], "name": candidate["name"], "position": candidate["position"], "level": candidate["level"] or 1,
            "depth_tier": candidate["depth_tier"] or "standart",
            "interview_language": candidate["interview_language"] or "tr", "report_language": candidate["report_language"] or "tr"
        }
    }

@app.post("/api/interview/start")
def start_interview(payload=Depends(verify_token)):
    if payload.get("role") != "candidate":
        raise HTTPException(status_code=403, detail="Yetkisiz")

    candidate_id = payload["candidate_id"]
    db = get_db()
    candidate = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()

    if not candidate:
        db.close()
        raise HTTPException(status_code=404, detail="Aday kaydı bulunamadı")

    if candidate["invite_type"] == "general":
        db.close()
        raise HTTPException(status_code=403, detail="Bu hesap CV havuzu için oluşturulmuştur. Mülakata katılmak için bir kurum daveti gereklidir.")

    level = candidate["level"] or 1

    if level == 2:
        db.close()
        log_ai_provider(2, "claude", "blocked")
        raise HTTPException(status_code=400, detail="Level 2 mülakatlar sesli (OpenAI Realtime) akışını kullanır. Lütfen /api/realtime/session üzerinden bağlanın.")
    existing = db.execute("SELECT * FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, level)).fetchone()
    lvl_cfg = get_level_config(level)
    total_seconds = lvl_cfg["minutes"] * 60

    # Level 2-3'te CV zorunlu: mülakat CV yüklenmeden başlatılamaz.
    if lvl_cfg["cv_required"] and not (candidate["cv_text"] and len(candidate["cv_text"].strip()) > 20):
        db.close()
        raise HTTPException(status_code=400, detail="Bu seviyedeki mülakata başlamadan önce CV yüklemeniz gerekiyor.")

    if existing and existing["completed_at"]:
        db.close()
        raise HTTPException(status_code=400, detail="Bu seviyedeki mülakatınız zaten tamamlanmış")
    if not existing:
        db.execute("INSERT INTO interviews (candidate_id, level, messages) VALUES (?, ?, '[]')", (candidate_id, level))
        # Teşebbüs sayacı (Mülakat Denemeleri ekranı): yeni mülakat = bir başlatma.
        db.execute(
            "UPDATE candidates SET interview_start_count = COALESCE(interview_start_count, 0) + 1, last_start_at = ? WHERE id = ?",
            (_now_ts(), candidate_id)
        )
        db.commit()
    else:
        # Aday sayfayı yenilediyse aynı mülakatı tekrar başlatıp token yakma; mevcut ilk soruyu dön.
        old_messages = get_interview_messages(db, candidate_id, level)
        if old_messages:
            db.close()
            return {"message": old_messages[-1].get("content", "Mülakata devam edebilirsiniz."), "question_duration": 60, "total_duration_seconds": total_seconds, "intro_text": get_intro_text(payload["position"], level, candidate["interview_language"] or "tr")}
    db.close()

    if not ANTHROPIC_API_KEY:
        print("HATA: ANTHROPIC_API_KEY ortam değişkeni boş veya tanımsız.")
        raise HTTPException(status_code=500, detail="Sistem yapılandırma hatası (API anahtarı eksik). Lütfen yöneticinize bildirin.")

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=60.0)
        system = get_system_prompt(payload["position"], payload["name"], candidate["cv_text"] if candidate else None, candidate["ai_note"] if candidate else None, candidate["education"] if candidate else None, candidate["university"] if candidate else None, candidate["department"] if candidate else None, candidate["experience_years"] if candidate else None, level, (candidate["interview_language"] if candidate and "interview_language" in candidate.keys() else "tr") or "tr", (candidate["report_language"] if candidate and "report_language" in candidate.keys() else "tr") or "tr", (candidate["depth_tier"] if candidate and "depth_tier" in candidate.keys() else "standart") or "standart", email=(candidate["email"] if candidate and "email" in candidate.keys() else None))
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=220, system=cached_system(system),
            messages=[{"role": "user", "content": "Başla. Kısa selam ve ilk soru."}]
        )
        raw = response.content[0].text
        add_token_usage(candidate_id, level, response)
        clean, duration = parse_duration(raw)
        db = get_db()
        save_interview_state(db, candidate_id, [{"role": "assistant", "content": clean, "ts": _now_ts()}], level)
        db.commit(); db.close()
        return {"message": clean, "question_duration": duration, "total_duration_seconds": total_seconds, "intro_text": get_intro_text(payload["position"], level, candidate["interview_language"] or "tr")}
    except anthropic.APIError as e:
        print(f"HATA (Anthropic API - start_interview): {type(e).__name__}: {e}")
        err = ai_error_from_anthropic(e, "interview_start", {
            "candidate_id": candidate_id, "candidate_name": payload.get("name"), "level": level,
        }, severity="user")
        raise ai_http_exception(err)
    except Exception as e:
        print(f"HATA (start_interview, beklenmeyen): {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="Mülakat başlatılırken beklenmeyen bir hata oluştu. Lütfen tekrar deneyin.")

@app.post("/api/interview/chat")
def interview_chat(data: ChatMessage, background_tasks: BackgroundTasks, payload=Depends(verify_token)):
    if payload.get("role") != "candidate":
        raise HTTPException(status_code=403, detail="Yetkisiz")

    # Güvenlik: aday ID'sini frontend body'sinden değil JWT içinden esas al.
    # localStorage/candidate_info bozuksa candidate_id boş gelebiliyor ve finalize zinciri patlıyordu.
    effective_candidate_id = int(payload.get("candidate_id") or data.candidate_id)

    db = get_db()
    candidate = db.execute("SELECT * FROM candidates WHERE id=?", (effective_candidate_id,)).fetchone()
    level = candidate["level"] or 1 if candidate else 1
    interview = db.execute("SELECT * FROM interviews WHERE candidate_id=? AND level=?", (effective_candidate_id, level)).fetchone()
    messages = get_interview_messages(db, effective_candidate_id, level)
    db.close()

    if not candidate:
        raise HTTPException(status_code=404, detail="Aday bulunamadı")

    if level == 2:
        log_ai_provider(2, "claude", "blocked")
        raise HTTPException(status_code=400, detail="Level 2 mülakatlar sesli (OpenAI Realtime) akışını kullanır, bu endpoint kullanılamaz.")

    # EŞZAMANLILIK GÜVENLİK AĞI: Mülakat zaten tamamlanmışsa (örn. çift gönderim, ağ
    # tekrar denemesi, yarış durumu) tekrar AI çağrısı yapıp yeni bir rapor/e-posta
    # üretme — mevcut sonucu olduğu gibi dön.
    if interview and interview["completed_at"]:
        return {
            "message": "Mülakatınız zaten tamamlanmış.",
            "completed": True,
            "score": interview["score"],
            "recommendation": interview["recommendation"],
        }

    if not ANTHROPIC_API_KEY:
        print("HATA: ANTHROPIC_API_KEY ortam değişkeni boş veya tanımsız.")
        raise HTTPException(status_code=500, detail="Sistem yapılandırma hatası (API anahtarı eksik). Lütfen yöneticinize bildirin.")

    messages.append({"role": "user", "content": data.message, "ts": _now_ts()})
    compact_memory = build_compact_memory(messages[:-1])
    q_count = sum(1 for m in messages if m.get("role") == "assistant" and "---RAPOR---" not in (m.get("content") or ""))
    lvl_cfg = get_level_config(level)
    # Not: q_count tavanı level'a göre yükseltildi çünkü derinleştirme/netleştirme turları da bu sayaca dahil oluyor.
    # Süre bazlı bitiş asıl tetikleyici; sabit soru tavanı sadece maliyet/uzunluk güvenlik ağı.
    # Level 3 adaptif: elapsed eşiği aşılsa da minimum soru sayısı daha yüksek tutulur (daha geç kesilir).
    should_finish_condition = (
        (data.elapsed_seconds > lvl_cfg["minutes"] * 60 and q_count >= lvl_cfg["min_q"])
        or q_count >= lvl_cfg["max_q"]
        # MADDE 3 — metin akışı üst süre sınırı (L1/L3-metin): hedef sürenin 3 katını aşan oturum
        # min_q sağlanmasa bile kapanışa girer. Metin turu ucuz olduğu için tavan yüksek tutuldu;
        # gerçek bir mülakat bu sınıra ulaşmaz, yalnızca saatlerce açık kalan oturumları bağlar.
        or data.elapsed_seconds > lvl_cfg["minutes"] * 60 * 3
    )

    # İKİ AŞAMALI KAPANIŞ: bitiş şartı oluştuğunda direkt rapor üretip kesmek yerine,
    # önce bir kapanış/son-söz sorusu sorulur (closing_asked=0 -> 1), aday buna cevap
    # verdikten SONRAKİ turda gerçek bitiş yapılır. Böylece mülakat "hart diye" kesilmez.
    closing_already_asked = bool(interview["closing_asked"]) if interview else False
    ask_closing_now = should_finish_condition and not closing_already_asked
    should_finish = should_finish_condition and closing_already_asked

    if ask_closing_now:
        db = get_db()
        db.execute("UPDATE interviews SET closing_asked=1 WHERE candidate_id=? AND level=?", (effective_candidate_id, level))
        db.commit(); db.close()

    last_question = ""
    for m in reversed(messages[:-1]):
        if m.get("role") == "assistant":
            last_question = m.get("content", "")[:400]
            break

    user_payload = f"""ÖNCEKİ KISA HAFIZA (çelişki kontrolü için):
{compact_memory or 'Henüz yok.'}

SON SORU:
{last_question}

ADAYIN SON CEVABI:
{data.message}

GÖREV:
{"Mülakatı şimdi bitir ve raporu üret." if should_finish else ("Mülakat içerik olarak tamamlandı. Şimdi SORU SORMA, sadece: adayın verdiği bilgiler için kısa ve sıcak bir teşekkür et, kısaca anladığını özetle (1 cümle), ve şu soruyu sor: 'Eklemek veya öne çıkarmak istediğiniz başka bir şey var mı?' [MÜLAKATBİTTİ] etiketini KULLANMA, rapor üretme, bu son bir soru." if ask_closing_now else "Önceki cevaplarla çelişki varsa yakala; yoksa sıradaki en önemli tek soruyu sor.")}

ÖNEMLİ KONTROL: Adayın son cevabında mülakatı SONLANDIRMA veya bir insan otoritesine (yönetim, İK, üst düzey) yönelme yönünde NET bir talep var mı (örn. "burada bırakalım", "devam etmek istemiyorum", "yönetimle/İK ile konuşacağım", "üst yönetime ileteceğim", "bunu şikayet edeceğim", "bitirelim", ya da "ses/sistem çalışmıyor" gibi bir arıza bildirip devam etmek istemediğini belirtmesi)? Varsa, yukarıdaki görevi YOK SAY — bunun yerine cevabının EN BAŞINA tam olarak [ADAY_CIKIS_TALEBI] etiketini koy, sonra kısa ve anlayışlı bir kabul cümlesi yaz (örn. "Anlıyorum, mülakatı burada sonlandıralım."). İkna etmeye, alternatif sunmaya, görevini tamamlamaya çalışarak karşılık vermeye veya devam ettirmeye ÇALIŞMA — bu net bir taleptir, itiraz değildir, senin görevin bu talepten önce gelmez.
"""

    exit_requested_this_turn = False

    try:
        system = get_system_prompt(payload["position"], payload["name"], candidate["cv_text"] if candidate else None, candidate["ai_note"] if candidate else None, candidate["education"] if candidate else None, candidate["university"] if candidate else None, candidate["department"] if candidate else None, candidate["experience_years"] if candidate else None, level, (candidate["interview_language"] if candidate and "interview_language" in candidate.keys() else "tr") or "tr", (candidate["report_language"] if candidate and "report_language" in candidate.keys() else "tr") or "tr", (candidate["depth_tier"] if candidate and "depth_tier" in candidate.keys() else "standart") or "standart", email=(candidate["email"] if candidate and "email" in candidate.keys() else None))

        # KAPANIŞ İŞLEMİNİ ARKA PLANA ALMA: should_finish=True olduğunda bu tur zaten rapor
        # üretecek yavaş (4000 token) çağrıyı tetikleyecekti. Onun yerine adayın son cevabını
        # hemen kaydedip, gönderilecek promptu (system+user_payload) AYNEN DB'ye yazıp arka
        # planda çalıştırıyoruz — aday 2-3 dakika değil, DB yazımı kadar bekliyor.
        if should_finish:
            # data.message zaten messages'a eklendi (fonksiyon başında) — burada tekrar eklenmez.
            db = get_db()
            save_interview_state(db, effective_candidate_id, messages, level)
            db.commit(); db.close()
            _mark_finish_pending(effective_candidate_id, level, provider="claude", model="claude-sonnet-4-6",
                                  system=system, payload=user_payload, terminated_reason=None, reason="normal")
            background_tasks.add_task(run_deferred_finish_job, effective_candidate_id, level)
            return {
                "message": "Mülakatınız tamamlandı, teşekkür ederiz. Raporunuz hazırlanıyor.",
                "completed": True, "processing": True, "score": None, "recommendation": None,
            }

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=60.0)
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=260, system=cached_system(system),
            messages=[{"role": "user", "content": user_payload}]
        )
        reply = response.content[0].text
        add_token_usage(effective_candidate_id, level, response)

        exit_requested_this_turn = "[ADAY_CIKIS_TALEBI]" in reply
        if exit_requested_this_turn:
            reply = reply.replace("[ADAY_CIKIS_TALEBI]", "").strip()

        if "[MÜLAKATBİTTİ]" in reply:
            # GÜVENLİK AĞI: should_finish=False bir turda (sabit 260 token'lık çağrı) AI yine de
            # erken bitirme denerse (nadir), rapor bloğunu at, mülakat devam etsin.
            print(f"UYARI: AI erken bitirme denedi (q_count={q_count}, elapsed={data.elapsed_seconds}); rapor atıldı, mülakat devam ediyor.")
            reply = re.sub(r'\[MÜLAKATBİTTİ\][\s\S]*', '', reply).strip()
            reply = re.sub(r'---RAPOR---[\s\S]*', '', reply).strip()
            if not reply or len(reply) < 20:
                reply = "[SÜRE:60] Devam edelim. Önceki yanıtınızı dikkate alarak bu pozisyonda en güçlü olduğunuz somut yetkinlik nedir?"

        # BÖLÜM B2 (L1/L3 metin): kriter/konu başına yeniden-sorma sayacı. Model her yeniden
        # sorduğunda mesaj başına [YENIDEN] koyar; etiket adaya GÖSTERİLMEZ, sunucu sayar.
        _is_reask = "[YENIDEN]" in reply
        reply = reply.replace("[YENIDEN]", "").strip()
        try:
            _ca = {}
            if interview and "criterion_attempts_json" in interview.keys() and interview["criterion_attempts_json"]:
                _ca = json.loads(interview["criterion_attempts_json"]) or {}
            _consec = _safe_int(_ca.get("consecutive_reask")) + 1 if _is_reask else 0
            _ca["consecutive_reask"] = _consec
            _ca.setdefault("history", [])
            if _is_reask:
                _ca["history"].append({"elapsed_minute": round(_safe_int(data.elapsed_seconds) / 60, 1), "consecutive": _consec})
                _ca["history"] = _ca["history"][-40:]
            _dbca = get_db()
            _dbca.execute("UPDATE interviews SET criterion_attempts_json=? WHERE candidate_id=? AND level=?",
                          (json.dumps(_ca, ensure_ascii=False)[:6000], effective_candidate_id, level))
            _dbca.commit(); _dbca.close()
            if _consec >= 2:
                _append_result_event(effective_candidate_id, level, {
                    "type": "criterion_retry_limit", "subtype": "yeniden_sorma",
                    "elapsed_ms": _safe_int(data.elapsed_seconds) * 1000,
                    "elapsed_minute": round(_safe_int(data.elapsed_seconds) / 60, 1),
                    "description": f"Bir kriter/konu {_consec} kez yeniden soruldu; model bu turdan sonra kriteri bırakıp geçmeli (raporda 'yanıtsız').",
                    "weight": "gözlem", "source": "system",
                })
        except Exception as e:
            print(f"UYARI (B2 kriter tekrar sayacı c={effective_candidate_id}): {type(e).__name__}: {e}")

        messages.append({"role": "assistant", "content": reply, "ts": _now_ts()})

        db = get_db()
        save_interview_state(db, effective_candidate_id, messages, level)
        db.commit(); db.close()

        if exit_requested_this_turn:
            # Gerçekten cevaplanmış (zaman aşımı/boş olmayan) kaç mesaj var, kontrol et.
            real_answers = [
                m.get("content", "") for m in messages
                if m.get("role") == "user" and "zaman aşım" not in (m.get("content") or "").lower() and len(m.get("content", "").strip()) > 3
            ]
            _append_result_event(effective_candidate_id, level, {
                "type": "termination", "subtype": "aday_talebi",
                "elapsed_ms": _safe_int(data.elapsed_seconds) * 1000,
                "elapsed_minute": round(_safe_int(data.elapsed_seconds) / 60, 1),
                "description": "Aday mülakatı kendi isteğiyle sonlandırdı",
                "weight": "sonlandırma", "source": "candidate",
            })
            if not real_answers:
                # HİÇ gerçek cevap yoksa (mülakat aslında hiç başlamadıysa), Claude'a
                # pahalı bir "rapor üret" çağrısı (4000 token) yapmadan direkt ücretsiz
                # şablon raporla bitir — boş bir mülakat için token harcamanın anlamı yok. Zaten
                # hızlı (AI çağrısı yok), arka plana almaya gerek yok.
                _set_result_meta(effective_candidate_id, level, partial=1, completion_pct=0,
                                 result_reason="Aday talebiyle erken sonlandırıldı — değerlendirilebilir veri toplanamadı.")
                return finalize_interview(effective_candidate_id, "[MÜLAKATBİTTİ]",
                                           terminated_reason="Aday talebiyle erken sonlandırıldı (gerçek veri toplanamadı)", level=level)

            # Aday net bir sonlandırma talebinde bulundu — ikna etmeye çalışmadan, mevcut konuşma
            # içeriğiyle GERÇEK bir bitiş/rapor üretimi tetiklenir. Bu da yavaş (4000 token) bir
            # çağrı olduğu için should_finish dalıyla AYNI mekanizmayla arka plana alınır.
            finish_payload = f"""ÖNCEKİ KISA HAFIZA:
{build_compact_memory(messages)}

GÖREV: Aday mülakatı sonlandırmak istediğini net şekilde belirtti (bu bir teknik arıza bildirimi de olabilir). Mülakatı şimdi bitir ve mevcut bilgilere göre raporu üret. Adayı ikna etmeye çalışma, sadece elindeki bilgiyle adil bir değerlendirme yap; eksik kalan kısımları düşük puan nedeni yapma, sadece "yeterli veri toplanamadı" notu düş. "Sonuç Gerekçesi" bölümüne: mülakatın erken bittiğini, adayın talebiyle sonlandığını ve hangi kriterlerin veri yetersizliğinden değerlendirilemediğini somut yaz. [MÜLAKATBİTTİ] etiketini kullan."""
            _set_result_meta(effective_candidate_id, level, partial=1,
                             completion_pct=min(100, round(len(real_answers) / max(1, lvl_cfg["min_q"]) * 100)),
                             result_reason="Aday mülakatı kendi isteğiyle erken sonlandırdı.")
            _mark_finish_pending(effective_candidate_id, level, provider="claude", model="claude-sonnet-4-6",
                                  system=system, payload=finish_payload,
                                  terminated_reason="Aday talebiyle erken sonlandırıldı", reason="aday_talebi")
            background_tasks.add_task(run_deferred_finish_job, effective_candidate_id, level)
            return {
                "message": "Anlıyorum, mülakatı burada sonlandıralım. Raporunuz hazırlanıyor.",
                "completed": True, "processing": True, "score": None, "recommendation": None,
            }

        clean, duration = parse_duration(reply)
        return {"message": clean, "completed": False, "question_duration": duration}
    except anthropic.APIError as e:
        print(f"HATA (Anthropic API - interview_chat): {type(e).__name__}: {e}")
        err = ai_error_from_anthropic(e, "interview_chat", {
            "candidate_id": locals().get("effective_candidate_id"), "candidate_name": payload.get("name"),
            "level": locals().get("level"),
        }, severity="user")
        raise ai_http_exception(err)
    except Exception as e:
        print(f"HATA (interview_chat, beklenmeyen): {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="Cevap işlenirken beklenmeyen bir hata oluştu. Lütfen tekrar deneyin.")

# KALEM 1 (2. tur) — SEZGİSEL CV ÇIKARIMI SIKILAŞTIRILDI.
# Önceki sürüm yapışık/parçalı CV metninden "Mart Üniversitesi" gibi UYDURMA değer üretiyordu.
# Yeni kurallar:
#  - Bir alan ancak kaynak metinde AÇIK ve BÜTÜN olarak geçiyorsa doldurulur.
#  - Çıkarılan değer kaynakta birebir (boşluk-normalize) alt-string değilse REDDEDİLİR → "—".
#  - Kaynak metin yapışık/okunamaz ise (cv_text_readability) o kaynaktan çıkarım YAPILMAZ.
#  - Transkriptteki açık beyan, CV çıkarımından ÖNCELİKLİDİR.
#  - "Boş bırakmak yanlış doldurmaktan iyidir."
_EDU_PHRASES = [
    ("endüstri meslek lisesi", "Endüstri Meslek Lisesi"), ("endustri meslek lisesi", "Endüstri Meslek Lisesi"),
    ("ticaret meslek lisesi", "Ticaret Meslek Lisesi"),
    ("ticaret lisesi", "Ticaret Lisesi"), ("meslek lisesi", "Meslek Lisesi"),
    ("anadolu lisesi", "Anadolu Lisesi"), ("fen lisesi", "Fen Lisesi"),
    ("imam hatip lisesi", "İmam Hatip Lisesi"), ("i̇mam hatip lisesi", "İmam Hatip Lisesi"),
    ("açık öğretim lisesi", "Açık Öğretim Lisesi"), ("acik ogretim lisesi", "Açık Öğretim Lisesi"),
    ("düz lise", "Düz Lise"),
    ("yüksek lisans", "Yüksek Lisans"), ("yuksek lisans", "Yüksek Lisans"),
    ("doktora", "Doktora"),
    ("ön lisans", "Ön Lisans"), ("onlisans", "Ön Lisans"), ("önlisans", "Ön Lisans"),
    ("lisans", "Lisans"),
    ("lise", "Lise"),
]

def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

def normalize_cv_for_analysis(cv_text: str) -> str:
    """B1 — analiz (çıkarım/çelişki) öncesi, DEPOLANMIŞ yapışık CV metnine sözlük segmentasyonu
    uygular (yalnız okunabilirlik puanını artırıyorsa). Yeni yüklemeler zaten extract_text_from_pdf'te
    segmente ediliyor; bu, eski kayıtlar ve DOCX için ek güvence."""
    t = (cv_text or "").strip()
    if not t or t.startswith("["):
        return t
    r = cv_text_readability(t)
    if r["space_ratio"] >= 0.12:
        return t
    try:
        seg = _dict_segment(t)
        if seg != t and cv_text_readability(seg)["score"] > r["score"]:
            return seg
    except Exception as e:
        print(f"UYARI (normalize_cv_for_analysis): {type(e).__name__}: {e}")
    return t

def _cv_text_is_readable(t: str) -> bool:
    """Yapışık/okunamaz metinden çıkarım yapma; boşluk oranı düşükse False.
    (Çağıranlar önce normalize_cv_for_analysis'ten geçirmeli.)"""
    return bool((t or "").strip()) and cv_text_readability(t)["space_ratio"] >= 0.09

def _verbatim_in(value: str, source: str) -> bool:
    """value bir kaynakta BİREBİR (boşluk-normalize) alt-string olarak geçiyor mu?"""
    if not value or not source:
        return False
    return _norm_ws(value) in _norm_ws(source)

def _extract_experience_years(text: str):
    """Yaklaşık deneyim yılı: '10 yıl', '10 yıla yakın', '2016'dan bu yana'. Yalnız 1-50 arası."""
    t = text or ""
    m = re.search(r"(?<!\d)(\d{1,2})\s*(?:\+|yıl(?:a\s*yakın|l[ıi]k|dan\s*(?:fazla|beri))?|sene(?:lik)?)\s*(?:iş\s*)?(?:deneyim|tecr[üu]be)?", t, re.IGNORECASE)
    if m and 1 <= _safe_int(m.group(1)) <= 50:
        return _safe_int(m.group(1))
    m = re.search(r"((?:19|20)\d{2})\s*['’]?\s*(?:d[ae]n|ten|tan)\s*(?:bu\s*yana|beri|itibaren)", t, re.IGNORECASE)
    if m:
        yr = _safe_int(m.group(1))
        if 1980 <= yr <= datetime.now().year:
            return max(1, datetime.now().year - yr)
    return None

def _extract_education_from(text: str):
    """Somut eğitim seviyesi kalıbı — kelime sınırlı, birebir. 'üniversite'/'fakülte' TEK BAŞINA
    seviye SAYILMAZ (uydurma kaynağı)."""
    low = _norm_ws(text)
    if not low:
        return None
    for needle, label in _EDU_PHRASES:
        if re.search(r"(?<![\wçğıöşüİ])" + re.escape(needle) + r"(?![\wçğıöşü])", low):
            return label
    return None

# GÖREV 5.3 — eğitim SEVİYE hiyerarşisi. Alt küme / detaylandırma ilişkisi (ör. "Ticaret Meslek
# Lisesi" ⊆ "Lise") aynı rank'e düşer → ÇELİŞKİ ÜRETMEZ. Yalnız gerçek seviye uyuşmazlığı
# (ör. Lisans=5 vs Lise=3) çelişkidir.
_EDU_LEVEL_RANK = [
    (("doktora", "phd", "ph.d"), 7),
    (("yüksek lisans", "yuksek lisans", "master", "mba", "m.sc", "msc", "m.a "), 6),
    (("lisans", "üniversite", "universite", "fakülte", "fakulte", "bachelor", "b.sc", "bsc", "mühendis", "muhendis",
      "işletme bölüm", "isletme bolum", "iktisat", "hukuk fak"), 5),
    (("ön lisans", "on lisans", "önlisans", "onlisans", "meslek yüksekokul", "myo", "associate"), 4),
    (("lise", "high school", "ortaöğretim", "ortaogretim"), 3),   # her tür lise (meslek/anadolu/ticaret/imam hatip/açık) burada
    (("ortaokul", "middle school"), 2),
    (("ilkokul", "ilköğretim", "ilkogretim", "primary school"), 1),
]

def _education_level_rank(text: str):
    """Eğitim metnini seviye rank'ine çevirir (1=ilkokul … 7=doktora). Bilinmiyorsa None.
    'lisans okumaktayım / öğrencisiyim' gibi DEVAM EDEN eğitim, tamamlanmış sayılmaz → o ifade
    varsa lisans rank'i verilmez (yalnız mevcut/tamamlanmış seviye kıyaslanır)."""
    low = _norm_ws(text or "")
    if not low:
        return None
    ongoing = bool(re.search(r"(okumakta|öğrenci|ogrenci|devam ed|sürdür|surdur|yarım|yarida|terk)", low))
    for needles, rank in _EDU_LEVEL_RANK:
        for nd in needles:
            if nd in low:
                if rank == 5 and ongoing and not re.search(r"(mezun|bitir|tamamla|derece)", low):
                    continue  # "açık öğretimden işletme okumaktayım" → lisans SAYMA, lise kalır
                return rank
    return None

def extract_cv_fields_heuristic(cv_text: str = "", transcript: str = "") -> dict:
    """SIKI sezgisel çıkarım. Transkript öncelikli; yapışık CV'den çıkarım YOK; birebir doğrulama.
    Dönüş: {education, university, department, experience_years, _cv_readable, _notes[]}."""
    tr = transcript or ""
    cv = normalize_cv_for_analysis(cv_text or "")   # B1 — yapışıksa sözlükle böl
    cv_ok = _cv_text_is_readable(cv)
    out = {"education": None, "university": None, "department": None, "experience_years": None,
           "_cv_readable": cv_ok, "_notes": []}
    if cv.strip() and not cv_ok:
        out["_notes"].append("CV metni yapışık/okunamaz — CV'den alan çıkarımı yapılmadı.")
    # sıralı kaynaklar: transkript ÖNCE, sonra (okunabilirse) CV
    sources = [("transkript", tr)] + ([("CV", cv)] if cv_ok else [])

    # eğitim
    for sname, txt in sources:
        edu = _extract_education_from(txt)
        if edu and _verbatim_in_edu(edu, txt):
            out["education"] = edu
            if sname == "transkript":
                out["_notes"].append(f"Eğitim '{edu}' adayın sözlü beyanından alındı (CV'ye göre öncelikli).")
            break

    # deneyim yılı
    for sname, txt in sources:
        y = _extract_experience_years(txt)
        if y is not None:
            out["experience_years"] = y
            out["_notes"].append(f"Deneyim yılı ~{y} '{sname}' kaynağından.")
            break

    # üniversite / bölüm — YALNIZ okunabilir kaynaktan; 1-2 özel ad kelimesi + anahtar sözcük;
    # nokta/virgül/rakam içeren aralık reddedilir; "boş bırakmak yanlış doldurmaktan iyidir".
    _PROPER = re.compile(r"^[A-ZÇĞİÖŞÜ][a-zçğıöşü]{2,}$")
    def _preceding_proper(txt, end_idx, maxn=2, block=("niversite",)):
        toks = txt[:end_idx].rstrip().split()
        got = []
        for t in reversed(toks[-maxn:]):
            if _PROPER.match(t) and not any(b in t.lower() for b in block):
                got.insert(0, t)
            else:
                break
        return got
    for sname, txt in sources:
        if out["university"] is None:
            for kw in re.finditer(r"\b[ÜUü]niversitesi\b", txt):
                pre = _preceding_proper(txt, kw.start(), maxn=2)
                if pre:
                    out["university"] = " ".join(pre) + " Üniversitesi"
                    break
        if out["department"] is None:
            for kw in re.finditer(r"\b(Bölümü|Mühendisliği)\b", txt):
                pre = _preceding_proper(txt, kw.start(), maxn=2)
                if pre:
                    out["department"] = " ".join(pre) + " " + kw.group(1)
                    break
    return out

def _verbatim_in_edu(label: str, source: str) -> bool:
    """Eğitim etiketinin türetildiği kalıp kaynakta birebir mi? (label 'Ticaret Lisesi' → kaynakta
    'ticaret lisesi' geçmeli). _EDU_PHRASES zaten kaynak metinde arandığı için bu ek güvence."""
    low = _norm_ws(source)
    for needle, lab in _EDU_PHRASES:
        if lab == label and needle in low:
            return True
    return _norm_ws(label) in low

def resolve_cv_fields(candidate: dict, transcript: str = "") -> dict:
    """FALLBACK ZİNCİRİ: (1) candidates form alanı → (2) transkript açık beyanı → (3) okunabilir
    CV metni. Dönüş her alan için (value, source); ayrıca '_notes' ve '_cv_readable'."""
    c = candidate or {}
    heur = extract_cv_fields_heuristic(c.get("cv_text") or "", transcript)
    out = {"_notes": list(heur.get("_notes") or []), "_cv_readable": heur.get("_cv_readable")}
    for k in ("education", "university", "department", "experience_years"):
        v = c.get(k)
        if v not in (None, "", 0):
            out[k] = (v, "form")
        elif heur.get(k) not in (None, "", 0):
            out[k] = (heur[k], "cv/mülakat tahmini")
        else:
            out[k] = (None, "")
    return out

def patch_standard_cv_blanks(standard_cv: str, candidate: dict, transcript: str = "") -> str:
    """Model veya yedek CV özetinde EĞİTİM/ÜNİVERSİTE/BÖLÜM/DENEYİM satırı boş ("—", "...", boş)
    kalmışsa form alanı → sezgisel çıkarım ile doldurur. Dolu satıra dokunmaz."""
    if not standard_cv:
        return standard_cv
    fields = resolve_cv_fields(candidate, transcript)
    label_map = {
        "EĞİTİM": "education", "EGITIM": "education",
        "ÜNİVERSİTE": "university", "UNIVERSITE": "university",
        "BÖLÜM": "department", "BOLUM": "department",
        "DENEYİM": "experience_years", "DENEYIM": "experience_years",
    }
    def _blank(val):
        v = (val or "").strip().strip("*").strip()
        return v in ("", "—", "-", "...", "…", "belirtilmemiş", "bilinmiyor", "yok")
    lines = standard_cv.splitlines()
    for i, ln in enumerate(lines):
        m = re.match(r"^\s*\**\s*([A-ZÇĞİÖŞÜa-zçğıöşü]+)\s*\**\s*:\s*(.*)$", ln)
        if not m:
            continue
        key = label_map.get(m.group(1).upper())
        if not key or not _blank(m.group(2)):
            continue
        val, src = fields.get(key, (None, ""))
        if val in (None, "", 0):
            continue
        disp = f"{val} yıl" if key == "experience_years" else str(val)
        suffix = "  (form beyanı)" if src == "form" else "  (CV/mülakattan tahmin — doğrulanmalı)"
        lines[i] = re.sub(r":\s*.*$", f": {disp}{suffix}", ln)
    return "\n".join(lines)

# ═══ KALEM 3 (2. tur) — ÇELİŞKİ TESPİTİ DETERMİNİSTİK ═══
# Model "tutarlıdır" diyerek yanlış onay veriyordu. Artık kayıt formu / CV çıkarımı / transkript
# beyanı üçlüsünde deneyim yılı, eğitim ve e-posta alanlarını KOD karşılaştırır; model yalnız
# hazır listeyi yorumlar, tespiti kendisi yapmaz. CV okunamıyorsa "karşılaştırılamadı" denir.
def compute_field_discrepancies(candidate: dict, transcript: str = "") -> dict:
    c = candidate or {}
    cv = normalize_cv_for_analysis(c.get("cv_text") or "")   # B1 — yapışıksa sözlükle böl
    tr = transcript or ""
    cv_ok = _cv_text_is_readable(cv)
    rows = []

    def _add(alan, kaynaklar, durum, note=""):
        rows.append({"alan": alan, "kaynaklar": kaynaklar, "durum": durum, "not": note})

    # --- deneyim yılı ---
    form_exp = c.get("experience_years") if c.get("experience_years") not in (None, "", 0) else None
    tr_exp = _extract_experience_years(tr)
    cv_exp = _extract_experience_years(cv) if cv_ok else None
    vals = {k: v for k, v in (("Kayıt formu", form_exp), ("Sözlü beyan", tr_exp), ("CV", cv_exp)) if v is not None}
    if len(vals) >= 2:
        if len(set(vals.values())) > 1:
            _add("Deneyim yılı", {k: str(v) for k, v in vals.items()}, "çelişki",
                 "kaynaklar farklı yıl söylüyor: " + " / ".join(f"{k} {v}" for k, v in vals.items()))
        else:
            _add("Deneyim yılı", {k: str(v) for k, v in vals.items()}, "tutarlı")
    elif cv.strip() and not cv_ok and (form_exp is not None or tr_exp is not None):
        _add("Deneyim yılı", {"CV": "okunamadı"}, "karşılaştırılamadı", "CV metni yapışık/okunamadı")

    # --- e-posta / telefon ---
    # GÖREV 5.1 — KALDIRILDI. CV'deki e-posta/telefon eski işveren, muhasebe ofisi, referans
    # kişilere ait olabilir; adayın birden fazla numarası olabilir. Bunlar ÇELİŞKİ DEĞİLDİR.
    # Aday iletişim bilgisi için TEK KAYNAK kayıt formudur (GÖREV 5.2) — hiçbir karşılaştırmaya girmez.

    # --- eğitim (SEVİYE bazlı — GÖREV 5.3) ---
    form_edu = (c.get("education") or "").strip()
    tr_edu = _extract_education_from(tr)
    cv_edu = _extract_education_from(cv) if cv_ok else None
    edu_vals = {k: v for k, v in (("Kayıt formu", form_edu or None), ("Sözlü beyan", tr_edu), ("CV", cv_edu)) if v}
    if len(edu_vals) >= 2:
        levels = {k: _education_level_rank(v) for k, v in edu_vals.items()}
        known = {k: r for k, r in levels.items() if r is not None}
        if len(set(known.values())) > 1:
            # GERÇEK seviye uyuşmazlığı (ör. Lisans vs Lise) — alt küme/detaylandırma DEĞİL
            _add("Eğitim (seviye)", edu_vals, "çelişki",
                 "farklı eğitim seviyesi: " + " / ".join(f"{k}: {v}" for k, v in edu_vals.items()))
        else:
            _add("Eğitim (seviye)", edu_vals, "tutarlı",
                 "aynı eğitim seviyesi (biri diğerinin alt kümesi/detayı olabilir — çelişki değil)")
    elif cv.strip() and not cv_ok and (form_edu or tr_edu):
        _add("Eğitim (seviye)", {"CV": "okunamadı"}, "karşılaştırılamadı", "CV metni yapışık/okunamadı")

    return {"rows": rows, "cv_readable": cv_ok,
            "celiski_var": any(r["durum"] == "çelişki" for r in rows),
            "karsilastirilamadi": any(r["durum"] == "karşılaştırılamadı" for r in rows)}

def render_discrepancy_block(disc: dict, for_prompt: bool = True) -> str:
    """compute_field_discrepancies çıktısını prompt'a / rapora hazır metne çevirir."""
    rows = (disc or {}).get("rows") or []
    if not rows:
        if for_prompt:
            return ("SİSTEM ALAN KARŞILAŞTIRMASI: Karşılaştırılabilir alan (deneyim yılı, eğitim seviyesi) "
                    "bulunamadı. Bu alanlarda 'tutarlıdır' DEME; yalnız transkriptte açıkça geçen çelişkileri "
                    "yaz, yoksa 'karşılaştırma için yeterli veri yok' de. E-POSTA/TELEFON üzerinden çelişki "
                    "veya güvenilirlik değerlendirmesi YAPMA.")
        return ""
    lines = []
    for r in rows:
        src = "; ".join(f"{k}={v}" for k, v in (r.get("kaynaklar") or {}).items())
        tag = {"çelişki": "⚠ ÇELİŞKİ", "tutarlı": "tutarlı", "karşılaştırılamadı": "karşılaştırılamadı"}.get(r["durum"], r["durum"])
        lines.append(f"- {r['alan']}: {tag} — {src}" + (f" ({r['not']})" if r.get("not") else ""))
    body = "\n".join(lines)
    if for_prompt:
        return ("SİSTEM ALAN KARŞILAŞTIRMASI (deterministik — HAZIR VERİ; tespiti sen yapma, YALNIZ yorumla):\n"
                + body +
                "\n→ 'Tutarlılık / Çelişki Analizi' bölümünde SADECE bu listeyi ve transkriptte açıkça görünen "
                "başka çelişkileri yaz. Liste 'çelişki' diyorsa bunu çelişki olarak RAPORLA. 'tutarlı' veya "
                "'karşılaştırılamadı' diyen alan için ÇELİŞKİ UYDURMA. E-POSTA/TELEFON bu karşılaştırmaya "
                "DAHİL DEĞİLDİR — o alanlar üzerinden çelişki/güvenilirlik yorumu YAPMA.")
    return "**Sistem Alan Karşılaştırması (deterministik):**\n" + body

def build_standard_cv_deterministic(candidate: dict, transcript: str = "") -> str:
    """KALEM 3 — model ---STANDARTCV--- bloğuna varamadan kesildiyse (token sınırı),
    aday alanlarından + CV metninden + transkriptten DETERMİNİSTİK bir standart CV özeti kur.
    'AI üretemedi' notu yerine gerçek bilgi.
    KALEM 5 — token-kesilme teknik notu buraya YAZILMAZ; çağıran (finalize_interview) bunu
    report_tech_note'a (yalnız yönetici) ayırır."""
    c = candidate or {}
    _fields = resolve_cv_fields(c, transcript)
    def _v(k):
        v = c.get(k)
        return str(v).strip() if v not in (None, "", 0) else "—"
    def _vf(k):
        val, src = _fields.get(k, (None, ""))
        if val in (None, "", 0):
            return "—"
        return f"{val}" + ("" if src == "form" else "  (CV/mülakattan tahmin)")
    cv_text = normalize_cv_for_analysis((c.get("cv_text") or "").strip())   # B1
    exp_val, exp_src = _fields.get("experience_years", (None, ""))
    exp_line = (f"{exp_val} yıl" + ("" if exp_src == "form" else "  (CV/mülakattan tahmin)")) if exp_val else "—"
    # CV metninden sertifika/dil ipuçları (basit anahtar-kelime taraması)
    certs = "; ".join(sorted({m.group(0) for m in re.finditer(r"\b(CFA|CPA|SMMM|ACCA|CMA|PMP|CIA|FRM|SPK|IFRS|ISO\s?\d+|Six Sigma|Prince2|ITIL|AWS|Azure|GCP|PMI)\b", cv_text, re.IGNORECASE)})) or "—"
    langs = "; ".join(sorted({m.group(0).capitalize() for m in re.finditer(r"\b(İngilizce|English|Almanca|German|Deutsch|Fransızca|French|İspanyolca|Arapça|Rusça)\b", cv_text, re.IGNORECASE)})) or "—"
    cv_excerpt = re.sub(r"\s+", " ", cv_text)[:900] if cv_text else "CV metni yok."
    return (f"AD SOYAD: {_v('name')}\n"
            f"POZİSYON: {_v('position')}\n"
            f"EĞİTİM: {_vf('education')}\n"
            f"ÜNİVERSİTE: {_vf('university')}\n"
            f"BÖLÜM: {_vf('department')}\n"
            f"DENEYİM: {exp_line}\n"
            f"SERTİFİKALAR (CV'den): {certs}\n"
            f"DİL BECERİLERİ (CV'den): {langs}\n"
            f"CV ÖZETİ (ham metinden): {cv_excerpt}")

# KALEM 5 — yalnız yönetici görecek: standart CV özeti neden deterministik derlendi.
STANDARD_CV_TRUNCATION_NOTE = ("Standart CV özeti, AI rapor çıktısı token sınırında kesildiği için "
                               "sistem tarafından aday kayıt alanlarından, yüklenen CV metninden ve transkriptten derlenmiştir.")

def build_fallback_report(candidate: dict, messages: list, score: int, recommendation: str, reason: str = "") -> str:
    """AI rapor bloğu eksik/bozuk gelirse boş rapor bırakma; kanıta dayalı yedek rapor üret."""
    answers = [m.get("content", "").strip() for m in messages if m.get("role") == "user" and m.get("content")]
    answered = [a for a in answers if "zaman aşım" not in a.lower() and len(a) > 3]
    unanswered = len(answers) - len(answered)
    sample = "\n".join(f"- {a[:220]}" for a in answered[:5]) or "- Değerlendirilebilir aday yanıtı yok."
    reason_line = f" Teknik not: {reason}" if reason else ""
    return f"""Aday: {candidate.get('name','-')}
Pozisyon: {candidate.get('position','-')}
Tarih: {datetime.now().strftime('%d.%m.%Y')}

TOPLAM PUAN: {score}/100
Öneri: {recommendation}

Tutarlılık / Çelişki Analizi:
AI rapor bloğu eksik veya beklenen formatta oluşmadığı için sistem yedek rapor üretmiştir.{reason_line} Mevcut cevaplar üzerinden sınırlı değerlendirme yapılmıştır.

Yanıt Özeti:
Toplam alınan cevap: {len(answers)}. Cevaplanmamış/zaman aşımına uğramış soru: {unanswered}.
{sample}

Güçlü Yönler:
Adayın verdiği sınırlı yanıtlar içinde olumlu yönler tam olarak ayrıştırılamamıştır. Gerekçeli sorgulama veya süreçle ilgili teknik itirazlar tek başına olumsuz değerlendirilmemiştir.

Gelişim Alanları:
Pozisyona özgü teknik bilgi, somut deneyim örnekleri ve yapılandırılmış yanıt kalitesi daha net gösterilmelidir.

Genel Kanı:
Mevcut veri rapor için sınırlıdır. Nihai karar için adaydan daha kapsamlı ve pozisyona doğrudan bağlı örnekler alınması önerilir.
""".strip()

# ═══ KALEM 3 — rapor içi çelişki temizliği (öneri tek kaynak, geri alınmış kayıtlar, prefix idempotent) ═══
_REGEN_FIX_PREFIX = "[Rapor yeniden üretiminde düzeltildi]"

# NOT (bu tur): önceki "_REC_KEYWORDS ile öneri↔gerekçe çelişki tespiti" KALDIRILDI — model her
# raporda başka kelime seçtiği için yapısal olarak çözmüyordu. Gerekçe artık KOŞULSUZ deterministik
# üretiliyor (bkz. _recommendation_rationale + sync_recommendation_line).

def _recommendation_rationale(recommendation: str, score_position, score_profile, veto_reason=None) -> str:
    """KALEM 1 (bu tur) — öneri gerekçesi TEK KAYNAK: karardan + PUAN 1 + PUAN 2 (+ varsa veto)."""
    sp = "" if score_position is None else f"PUAN 1 = {score_position}/100"
    pp = "" if score_profile is None else f", PUAN 2 = {score_profile}/100"
    scores = f" ({sp}{pp})" if sp else ""
    if veto_reason:
        return (f"Karar: Reddet. Kurumsal ortamda çalışmaya engel olacak düzeyde ciddi bir profil bulgusu "
                f"(profil vetosu) tespit edilmiştir: {veto_reason}. Bu, PUAN 1'den bağımsız bir veto nedenidir.")
    if recommendation == "Reddet":
        return (f"Karar: Reddet. Pozisyon uygunluğu puanı{scores} yetersiz; aday pozisyon kriterlerinin "
                f"çoğunda gereken yetkinlik düzeyini gösteremedi. Karar PUAN 1'e dayanmaktadır.")
    if recommendation == "İşe Al":
        return (f"Karar: İşe Al. Pozisyon uygunluğu puanı{scores} güçlü; aday pozisyon kriterlerinin "
                f"çoğunda net yetkinlik gösterdi. Karar PUAN 1'e dayanmaktadır.")
    if recommendation == "Değerlendirilemedi":
        return "Karar: Değerlendirilemedi. Güvenilir bir değerlendirme için yeterli veri oluşmadığından öneri verilememiştir."
    return (f"Karar: Değerlendirmeye Al. Pozisyon uygunluğu puanı{scores} orta düzeyde; aday bazı "
            f"kriterlerde yeterli, bazılarında sınırlı kaldı. İkinci bir görüşme veya ek kontrolle "
            f"netleştirilmesi önerilir. Karar PUAN 1'e dayanmaktadır.")

def _summary_conclusion_sentence(recommendation: str, score_position) -> str:
    """KALEM 2 (bu tur) — Yönetici Özeti KARAR cümlesi: deterministik, öneriyle her zaman aynı yönde."""
    sp = "" if score_position is None else f" (PUAN 1 = {score_position}/100)"
    return {
        "Reddet": f"Sistem sonucu: Aday bu pozisyon için uygun görülmemektedir{sp}. Öneri: Reddet.",
        "İşe Al": f"Sistem sonucu: Aday bu pozisyon için uygun görülmektedir{sp}. Öneri: İşe Al.",
        "Değerlendirilemedi": "Sistem sonucu: Güvenilir bir değerlendirme için yeterli veri oluşmamıştır. Öneri: Değerlendirilemedi.",
    }.get(recommendation,
          f"Sistem sonucu: Aday sınırda görülmektedir{sp}; ikinci bir değerlendirme önerilir. Öneri: Değerlendirmeye Al.")

# _POSITIVE_TONE / _NEGATIVE_TONE: yalnızca DENETÇİ TONU ('OLUMLU'/'OLUMSUZ') alınamadığında,
# Yönetici Özeti sonuç cümlesinin öneriyle YÖN uyumsuzluğunu son çare olarak yakalamak için.
def _summary_conclusion_conflicts(tone: Optional[str], recommendation: str) -> bool:
    t = (tone or "").upper()
    if t == "OLUMLU" and recommendation == "Reddet":
        return True
    if t == "OLUMSUZ" and recommendation in ("İşe Al",):
        return True
    return False

def _rewrite_summary_conclusion(report: str, recommendation: str, score_position, replace_last: bool) -> str:
    """Yönetici Özeti paragrafına deterministik karar cümlesi işler.
    replace_last=True  → paragrafın son (karar) cümlesini SİLİP yerine koyar (denetçi tonu çelişkili).
    replace_last=False → modelin cümlesine dokunmadan sonuna EKLER (ton bilinmiyor)."""
    m = re.search(r"(\*{0,2}\s*Yönetici Özeti\s*\*{0,2}\s*:\s*)([\s\S]*?)(?=\n\s*\n|\n\s*\*{0,2}[A-ZÇĞİÖŞÜ][^\n:]{2,40}\s*\*{0,2}\s*:|\Z)",
                  report, flags=re.IGNORECASE)
    if not m:
        return report
    para = m.group(2).rstrip()
    new_sent = _summary_conclusion_sentence(recommendation, score_position)
    if "Sistem sonucu:" in para:
        return report
    sents = re.split(r"(?<=[.!?])\s+", para)
    last = sents[-1].strip() if sents else ""
    decision_like = re.search(r"genel olarak|sonuç olarak|özetle|değerlendirmeye al|değerlendirilebil|öneril|uygun|potansiyel|karar", last, re.IGNORECASE)
    if replace_last and decision_like and len(sents) > 1:
        para_new = " ".join(sents[:-1]).rstrip() + " " + new_sent
    else:
        para_new = para.rstrip() + " " + new_sent
    return report[:m.start(2)] + para_new + report[m.end(2):]

def _sanitize_model_note(text: str) -> str:
    """KALEM 2 (bu tur) — Model notundan SAYISAL PUAN iddialarını temizler. Modelin yazdığı sayı
    rapordaki nihai puandan farklı olabilir (model puanı üretiyor, sistem normalize ediyor).
    'XX/100', 'XX puan', 'toplam puan XX' geçen CÜMLE komple atılır; kalan niteliksel gerekçe kalır."""
    t = (text or "").strip()
    if not t:
        return ""
    # cümlelere böl, sayısal puan iddiası içeren cümleyi at
    parts = re.split(r"(?<=[.!?])\s+", t)
    _num = re.compile(r"\b\d{1,3}\s*/\s*100\b|\b\d{1,3}\s*(?:puan|/\s*\d{1,3})\b|toplam\s+puan(?:ın|\s+değeri)?\s*\D{0,4}\d", re.IGNORECASE)
    kept = [p for p in parts if p.strip() and not _num.search(p)]
    out = " ".join(kept).strip()
    # cümle bölünemediyse ama sayı varsa: sayı kalıbını at
    if not kept and _num.search(t):
        return ""
    return out

def _place_rationale_after_recommendation(report: str) -> str:
    """'Öneri Gerekçesi:' satırı 'Öneri:' satırının hemen ardında değilse taşır. Yalnız bu iki
    yapısal satırın komşuluğunu düzeltir — başka içerik taşımaz."""
    lines = report.split("\n")
    oi = next((i for i, l in enumerate(lines) if re.match(r"\s*\**\s*Öneri\s*:\s*", l, re.IGNORECASE)
               and not re.match(r"\s*\**\s*Öneri\s+Gerekçesi", l, re.IGNORECASE)), None)
    gi = next((i for i, l in enumerate(lines) if re.match(r"\s*\**\s*Öneri\s+Gerekçesi\s*:", l, re.IGNORECASE)), None)
    if oi is None or gi is None:
        return report
    # gi zaten oi'den hemen sonra (arada yalnız boş satır) ise dokunma
    between = [l for l in lines[oi + 1:gi] if l.strip()]
    if not between:
        return report
    gline = lines.pop(gi)
    oi = next((i for i, l in enumerate(lines) if re.match(r"\s*\**\s*Öneri\s*:\s*", l, re.IGNORECASE)
               and not re.match(r"\s*\**\s*Öneri\s+Gerekçesi", l, re.IGNORECASE)), None)
    lines.insert(oi + 1, gline)
    return "\n".join(lines)

_DECISION_BLOCK_MARK = "**KARAR (sistem — eşik tablosundan):**"

def sync_recommendation_line(report: str, recommendation: str, score_position=None, score_profile=None,
                             veto_reason=None, summary_tone=None) -> str:
    """GÖREV 1.1 + 2.2 — KARAR TEK KAYNAK, GPT METNİNE DOKUNMADAN:
      - GPT artık 'Öneri:' / 'Öneri Gerekçesi:' üretmiyor (REPORT_BODY_SECTIONS'tan çıkarıldı).
      - Karar YALNIZCA PUAN 1 (score_position) + sabit eşik tablosundan üretilir ve raporda
        TEK BİR YERE, kendi başlıklı bloğu olarak eklenir.
      - GPT'nin Yönetici Özeti / Genel Kanı / hiçbir prose'una DOKUNULMAZ — GPT'nin görüşü ile
        eşik kararı farklı yönde olabilir, bu bir hata değil (iki ayrı otorite yan yana).
      - Eski davranıştan farkı: model cümlesi sansürlenmez, özet sonucu yeniden yazılmaz.
    İdempotent: blok zaten varsa yeniden eklenmez, değeri güncellenir."""
    if not report or not recommendation:
        return report
    # GPT yanlışlıkla bir 'Öneri:' satırı bıraktıysa temizle (karar artık tek blokta).
    report = re.sub(r"(?m)^\s*\**\s*Öneri(?:\s+Gerekçesi)?\s*:\s*\**.*$\n?", "", report)
    rationale = _recommendation_rationale(recommendation, score_position, score_profile, veto_reason)
    block = (f"{_DECISION_BLOCK_MARK} {recommendation}\n\n"
             f"**Karar Gerekçesi (sistem):** {rationale}\n\n"
             f"(Bu karar YALNIZCA PUAN 1 — pozisyon uygunluğu puanından ve sabit eşik tablosundan "
             f"üretilmiştir: <40 → Reddet · 40–79 → Değerlendirmeye Al · ≥80 → İşe Al. Rapor metnindeki "
             f"değerlendirici görüşü ve ikinci model görüşü bu karardan bağımsızdır ve farklı yönde olabilir.)")
    if _DECISION_BLOCK_MARK in report:
        return re.sub(re.escape(_DECISION_BLOCK_MARK) + r"[\s\S]*?(?=\n\s*\n---|\n\s*\n#{1,4}|\n\s*\n\*\*|---RAPORSON---|\Z)",
                      block + "\n", report, count=1)
    # Anchor: PUAN 2 başlığından hemen ÖNCE (tüm PUAN 1 prose'undan sonra); yoksa ---RAPORSON--- öncesi.
    m = re.search(r"\n\s*-{2,}\s*\n\s*#{2,4}\s*PUAN\s*2\b", report)
    if m:
        return report[:m.start()] + "\n\n" + block + "\n" + report[m.start():]
    if "---RAPORSON---" in report:
        return report.replace("---RAPORSON---", block + "\n\n---RAPORSON---", 1)
    return report.rstrip() + "\n\n" + block + "\n"

def sync_report_date_line(report: str, date_str: str) -> str:
    """Rapor metnindeki '**Tarih:** ...' satırını mülakatın GERÇEK tarihine sabitler (üretim
    tarihine değil). KALEM 4."""
    if not report or not date_str:
        return report
    return re.sub(r"(\**\s*Tarih\s*:\s*\**[ \t]*)[^\n]*", lambda m: f"{m.group(1)}{date_str}",
                  report, count=1, flags=re.IGNORECASE)

def idempotent_regen_prefix(desc: str) -> str:
    """'[Rapor yeniden üretiminde düzeltildi]' ibaresi kaç kez uygulanırsa uygulansın BİR kez
    kalır (baştaki tekrarları toplar)."""
    d = (desc or "")
    d = re.sub(rf"^(?:\s*{re.escape(_REGEN_FIX_PREFIX)}\s*)+", "", d).strip()
    return f"{_REGEN_FIX_PREFIX} {d}".strip()

def visible_result_events(events) -> list:
    """Rapora / panele BASILACAK olay listesi: geri alınmış (corrected) kayıtlar ve
    'end_reason_downgraded' (bilgi amaçlı sistem işareti) tamamen çıkarılır; aynı türden yinelenen
    (özellikle end_reason_downgraded) tek kayda indirilir. KALEM 3."""
    if not isinstance(events, list):
        return []
    out, seen_types = [], set()
    for e in events:
        if not isinstance(e, dict):
            continue
        if e.get("corrected") or e.get("superseded"):
            continue
        et = e.get("type")
        if et in ("end_reason_downgraded",):
            continue
        # aynı (type, subtype, description) çiftinden ikinci kaydı ele (çift damgalı kopya)
        key = (et, e.get("subtype"), (e.get("description") or "").strip().lower()[:120])
        if key in seen_types:
            continue
        seen_types.add(key)
        out.append(e)
    return out

def strip_report_system_lines(text: str) -> str:
    """KALEM 5 — müşteri raporundan iç sistem satırlarını çıkarır:
      - '[SİSTEM: ... transkripsiyon halüsinasyonu ...]' işaretli satırlar
      - 'MÜLAKAT NOTU: ... token sınırında kesil...' teknik notu
    Modele giden metin (interviews.messages) DEĞİŞMEZ — yalnız raporlanan çıktı temizlenir."""
    if not text:
        return text
    kept = []
    for ln in text.splitlines():
        if is_hallucination_marker_line(ln):
            continue
        if re.match(r"\s*\**\s*M[ÜU]LAKAT\s+NOTU\s*:", ln, re.IGNORECASE) and re.search(r"token\s+s[ıi]n[ıi]r|token\s+limit", ln, re.IGNORECASE):
            continue
        kept.append(ln)
    return "\n".join(kept)

# KALEM 5 (bu tur) — içeriği "belirtilecek bir şey yok" olan opsiyonel bölümler rapordan
# TAMAMEN çıkarılır (deterministik, model çağrısı yok). Yalnız şu başlıklar için:
_EMPTYABLE_SECTIONS = ("dil gözlemi", "serbest gözlemler", "değerlendirilemeyen alanlar",
                       "sonuç gerekçesi", "ai notuna uyum")
# GÖREV 6 — bağlamsız/klişe şablon cümleleri: bu kalıplardan biriyle DOLU bir opsiyonel bölüm
# "içeriksiz" sayılır ve rapordan tamamen çıkarılır (başlık + gövde).
_EMPTY_CONTENT_RE = re.compile(
    r"belirtilecek\s+bir\s+.{0,20}?\s*yok|belirtilecek\s+bir\s+şey\s+yok|"
    r"(?:kayda\s+değer|not\s+edilecek|söylenecek|eklenecek)\s+bir\s+.{0,20}?\s*yok|"
    r"gözlem\s+yok|herhangi\s+bir\s+.{0,30}?\s*(?:yok|bulunmamaktadır|gözlenmemiştir)\.?\s*$|"
    r"mülakat\s+normal\s+tamamland|normal\s+(?:bir\s+)?(?:şekilde\s+)?tamamland|olumsuz\s+bir\s+(?:gözlem|durum|bulgu)\s+(?:yok|bulunma)|"
    r"belirtilen\s+tüm\s+kriterler\s+değerlendirild|tüm\s+kriterler\s+değerlendirild|değerlendirilemeyen\s+(?:bir\s+)?alan\s+(?:yok|bulunma)|"
    r"tüm\s+kriterler\s+(?:eksiksiz\s+)?(?:puanland|değerlendirild)",
    re.IGNORECASE)

def strip_empty_report_sections(text: str) -> str:
    """'**Dil Gözlemi:** Belirtilecek bir dil gözlemi yok.' gibi içeriği boş olan opsiyonel
    bölümleri (başlık + gövde) siler. Başlık bir sonraki '**...:**' başlığına ya da boş satıra
    kadar olan bloktur."""
    if not text:
        return text
    lines = text.split("\n")
    out, i, n = [], 0, len(lines)
    _head = re.compile(r"^\s*\**\s*([A-Za-zÇĞİÖŞÜçğıöşü /↔–-]{3,45}?)\s*\**\s*:\s*(.*)$")
    while i < n:
        hm = _head.match(lines[i])
        if hm and _norm_name(hm.group(1)) in _EMPTYABLE_SECTIONS:
            # bu bölümün gövdesini topla
            body = [hm.group(2).strip()] if hm.group(2).strip() else []
            j = i + 1
            while j < n:
                if not lines[j].strip():
                    break
                if _head.match(lines[j]) and not lines[j].lstrip().startswith("-"):
                    break
                body.append(lines[j].strip())
                j += 1
            joined = " ".join(body).strip()
            if not joined or _EMPTY_CONTENT_RE.search(joined):
                # bölümü ve ardındaki tek boş satırı atla
                i = j
                if i < n and not lines[i].strip():
                    i += 1
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)

# ============ KAPANIŞ İŞLEMİNİ ARKA PLANA ALMA (rapor üretimi) ============
# Tasarım: "yavaş" olan tek şey rapor üreten AI çağrısı (Claude L1/L3, GPT-4o L2). Bu çağrıya
# GİDECEK TAM promptu (system+payload, hazır metin olarak) senkron kısımda üretip DB'ye
# aynen kaydediyoruz (pending_finish_*), sonra ya BackgroundTasks ile hemen ya da kurtarma
# taramasıyla sonradan aynı promptu birebir tekrar gönderiyoruz — mantığı yeniden kurmuyoruz,
# sadece "gönderilecek olanı" saklayıp tekrar oynatıyoruz. Bu yüzden normal kapanış, aday-talebi
# kapanışı, ihlal kapanışı ve L2 sesli raporu TEK bir arka plan fonksiyonundan geçer.
def _mark_finish_pending(candidate_id: int, level: int, provider: str, model: Optional[str], system: Optional[str],
                          payload: str, terminated_reason: Optional[str], reason: str):
    db = get_db()
    db.execute("""
        UPDATE interviews SET processing_status='processing', processing_started_at=CURRENT_TIMESTAMP,
               processing_error=NULL, pending_finish_reason=?, pending_finish_provider=?, pending_finish_model=?,
               pending_finish_system=?, pending_finish_payload=?, pending_finish_terminated_reason=?
        WHERE candidate_id=? AND level=?
    """, (reason, provider, model, system, payload, terminated_reason, candidate_id, level))
    # KALEM 4 — mülakatın GERÇEK bitiş anı: aday tam ŞİMDİ bitirdi. Arka plan rapor işi dakikalar/
    # saatler sonra bitebilir; completed_at o zamanı DEĞİL bu anı yansıtmalı. Yeniden üretimde
    # (reason='admin_regenerate') dokunma — orijinal bitiş korunur.
    if reason != "admin_regenerate":
        db.execute("UPDATE interviews SET interview_ended_at=COALESCE(interview_ended_at, CURRENT_TIMESTAMP) "
                   "WHERE candidate_id=? AND level=?", (candidate_id, level))
    db.commit(); db.close()

def _mark_finish_failed(candidate_id: int, level: int, error_text: str):
    db = get_db()
    db.execute("UPDATE interviews SET processing_status='failed', processing_error=? WHERE candidate_id=? AND level=?",
               (error_text[:2000], candidate_id, level))
    db.commit(); db.close()
    print(f"[PROCESSING_FAILED] candidate_id={candidate_id} level={level} error={error_text[:300]}")

# ============ FAZ D — ÇOK MODLU ANALİZ (mimik + ses metrikleri + ses gözlemleri + ortak rapor) ============
# TÜM BU KATMAN "en iyi çaba" (best effort): herhangi biri patlarsa loglanır ve BOŞ geçilir —
# mülakat ve rapor akışı asla etkilenmez, aday hiçbir teknik hata görmez. Ağır işlerin (görü
# modeli çağrısı, denetçi çağrısı) hepsi run_deferred_finish_job içinde, arka planda çalışır.
# KURAL: mimik/denetçi için L2 adayında Anthropic ASLA çağrılmaz (MIMIC_ANALYSIS_MODEL ve
# OPENAI_REVIEWER_MODEL ikisi de OpenAI). Mimik/ses çıktıları PUANI OYNATMAZ.

def _store_interview_json(candidate_id: int, level: int, column: str, value) -> None:
    if column not in ("mimic_analysis_json", "voice_metrics_json", "voice_observations_json"):
        return
    try:
        db = get_db()
        db.execute(f"UPDATE interviews SET {column}=? WHERE candidate_id=? AND level=?",
                   (json.dumps(value, ensure_ascii=False), candidate_id, level))
        db.commit(); db.close()
    except Exception as e:
        print(f"UYARI (_store_interview_json {column} c={candidate_id} L{level}): {type(e).__name__}: {e}")

_VOICE_CONF_UNANSWERED_RATIO = 0.30   # KALEM 7 — cevapsız tur oranı bu eşiği aşarsa güven otomatik "dusuk"

def _unanswered_turn_windows(candidate_id: int, level: int) -> dict:
    """KALEM 2/7 — {"windows": [ms...], "count": int}. Cevabı BOŞ / halüsinasyon işaretli turlar.
    İKİ KAYNAK (birleşik, ~2sn içinde tekilleştirilir):
      1) realtime_events: transcription_filtered / transcription_filtered_server (canlı + sunucu filtresi)
      2) HAM interviews.messages: '[mm:ss] Aday: [SİSTEM: ... halüsinasyon]' / boş Aday satırları
    NOT: HAM messages okunur — rapor görünümü temizliği (build_transcript_view for_report) bunu
    ETKİLEMEZ; iki düzeltme birbirini iptal etmesin."""
    out = []
    # --- kaynak 1: realtime_events ---
    try:
        db = get_db()
        try:
            ev_rows = db.execute(
                "SELECT event_data, elapsed_ms FROM realtime_events WHERE candidate_id=? AND level=? "
                "AND event_type IN ('transcription_filtered','transcription_filtered_server') ORDER BY id ASC",
                (candidate_id, level)).fetchall()
            row = db.execute("SELECT messages FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, level)).fetchone()
        finally:
            db.close()
    except Exception as e:
        print(f"UYARI (_unanswered_turn_windows c={candidate_id}): {type(e).__name__}: {e}")
        return {"windows": [], "count": 0}
    ev_count = len(ev_rows or [])
    for r in ev_rows or []:
        ms = _safe_int(r["elapsed_ms"])
        if ms <= 0:
            try:
                d = json.loads(r["event_data"] or "{}")
                mm = re.match(r"(\d{1,3}):(\d{2})", str(d.get("ts") or ""))
                if mm:
                    ms = (int(mm.group(1)) * 60 + int(mm.group(2))) * 1000
            except Exception:
                ms = 0
        if ms > 0:
            out.append(ms)
    # --- kaynak 2: ham messages'taki işaretli/boş Aday satırları ---
    blob = ""
    if row and row["messages"]:
        try:
            msgs = json.loads(row["messages"])
            blob = "\n".join((m.get("content") or "") for m in msgs if isinstance(m, dict))
        except Exception:
            blob = row["messages"] if isinstance(row["messages"], str) else ""
    msg_bad = 0
    for line in blob.splitlines():
        m = _VOICE_LINE_RE.match(line.strip())
        if not m or not m.group(3).startswith("Ada"):
            continue
        raw = line.strip()
        spoken = (m.group(4) or "").strip()
        quoted = re.search(r'"([^"]*)"\s*$', spoken)
        core = quoted.group(1).strip() if quoted else spoken
        if (not core) or is_hallucination_marker_line(raw) or is_likely_hallucination(core, "tr"):
            msg_bad += 1
            out.append((int(m.group(1)) * 60 + int(m.group(2))) * 1000)
    # zaman penceresi listesi (2sn tekilleştirilmiş) + gerçek cevapsız tur SAYISI ayrı döner
    out.sort()
    windows = []
    for ms in out:
        if not windows or abs(ms - windows[-1]) > 2000:
            windows.append(ms)
    count = max(len(windows), ev_count, msg_bad)
    return {"windows": windows, "count": count}

def compute_voice_metrics(candidate_id: int, level: int) -> dict:
    """realtime_events satırlarından TUR BAZLI ses metrikleri üretir. semantic_vad turu bütün
    olarak kapattığı için CÜMLE İÇİ DURAKLAMALAR ÖLÇÜLMEZ — tüm metrikler turlar arası / tur
    bütünü düzeyindedir ('olcum_turu': 'tur_bazli'). sync POST'u kaybolursa olay dizisinde
    boşluk olabilir; çıktıdaki 'guven' ve 'eksik_veri' alanları bunu işaretler. AI ÇAĞRISI YOK.
    KALEM 7 — adayın hiç konuşmadığı / halüsinasyon turları metrik hesabından ÇIKARILIR;
    'cevapsiz_tur_sayisi' ayrı raporlanır; cevapsız oranı yüksekse güven otomatik düşer."""
    try:
        db = get_db()
        rows = db.execute(
            "SELECT event_type, elapsed_ms FROM realtime_events WHERE candidate_id=? AND level=? ORDER BY elapsed_ms ASC, id ASC",
            (candidate_id, level)
        ).fetchall()
        db.close()
    except Exception as e:
        print(f"UYARI (compute_voice_metrics fetch c={candidate_id} L{level}): {type(e).__name__}: {e}")
        return {}
    evs = [(r["event_type"], _safe_int(r["elapsed_ms"])) for r in rows if r["event_type"]]
    if not evs:
        return {}
    _bad = _unanswered_turn_windows(candidate_id, level)
    bad_windows = _bad.get("windows", [])
    unanswered_turns = _bad.get("count", len(bad_windows))
    def _near_bad(ms, tol=9000):
        return any(abs(ms - b) <= tol for b in bad_windows)

    starts = [ms for t, ms in evs if t == "input_audio_buffer.speech_started"]
    stops = [ms for t, ms in evs if t == "input_audio_buffer.speech_stopped"]
    ai_done = sorted(ms for t, ms in evs if t in ("response.audio_transcript.done", "response.output_audio_transcript.done"))
    resp_created = sorted(ms for t, ms in evs if t == "response.created")
    interruptions = sum(1 for t, _ in evs if t == "barge_in_confirmed")

    all_turns = []
    for s in starts:
        e = next((x for x in stops if x >= s), None)
        if e is not None:
            all_turns.append((s, e))
    # cevapsız/halüsinasyon turlarını hesaptan çıkar
    turns = [(a, b) for (a, b) in all_turns if not _near_bad(b)]
    talk_ms = sum(max(0, b - a) for a, b in turns)
    turn_count = len(turns)
    total_turn_count = len(all_turns)
    considered_denom = max(total_turn_count, turn_count + unanswered_turns)

    answer_latencies = []
    for d in ai_done:
        nxt = next((s for s in starts if s > d), None)
        if nxt is not None and 0 < nxt - d < 60000 and not _near_bad(nxt):
            answer_latencies.append(nxt - d)
    think_times = []
    for stp in stops:
        if _near_bad(stp):
            continue
        nxt = next((c for c in resp_created if c > stp), None)
        if nxt is not None and 0 < nxt - stp < 30000:
            think_times.append(nxt - stp)

    def _avg_sn(xs):
        return round((sum(xs) / len(xs)) / 1000, 2) if xs else None

    missing = []
    if len(starts) != len(stops):
        missing.append("speech_started/stopped sayıları eşleşmiyor — olay kaybı olası")
    if not ai_done:
        missing.append("AI konuşma bitiş olayı yok — yanıt gecikmesi hesaplanamadı")
    if not resp_created:
        missing.append("response.created olayı yok — AI düşünme süresi hesaplanamadı")

    unanswered_ratio = (unanswered_turns / considered_denom) if considered_denom else 0.0
    guven = "orta"
    if missing:
        guven = "dusuk"
    elif unanswered_ratio > _VOICE_CONF_UNANSWERED_RATIO:
        guven = "dusuk"
        missing.append(f"cevapsız tur oranı yüksek (%{round(unanswered_ratio*100)}) — metrikler az sayıda geçerli tur üzerinden")

    return {
        "olcum_turu": "tur_bazli",
        "not": "semantic_vad turu bütün olarak kapatır; cümle içi duraklamalar ölçülemez. Değerler turlar arası / tur bütünü düzeyindedir. Cevapsız/halüsinasyon turları hesaba katılmadı.",
        "aday_konusma_toplam_sn": round(talk_ms / 1000, 1),
        "tur_sayisi": turn_count,
        "hesaba_katilan_tur": turn_count,
        "toplam_tur": total_turn_count,
        "cevapsiz_tur_sayisi": unanswered_turns,
        "ortalama_tur_uzunlugu_sn": round((talk_ms / turn_count) / 1000, 1) if turn_count else 0,
        "yanit_gecikmesi_ort_sn": _avg_sn(answer_latencies),
        "yanit_gecikmesi_ornek_sayisi": len(answer_latencies),
        "ai_dusunme_suresi_ort_sn": _avg_sn(think_times),
        "soz_kesme_sayisi": interruptions,
        "guven": guven,
        "guven_esigi_not": f"cevapsız tur oranı > %{int(_VOICE_CONF_UNANSWERED_RATIO*100)} → güven 'dusuk'",
        "eksik_veri": missing,
    }

def extract_voice_observations(candidate_id: int, level: int) -> list:
    """Realtime mülakatçının note_voice_observation tool call'larından (realtime_events'e
    yazılan) yapılandırılmış ses gözlemlerini toplar. Üst sınır güvenlik ağı: 10 kayıt."""
    try:
        db = get_db()
        rows = db.execute(
            "SELECT event_data, elapsed_ms FROM realtime_events WHERE candidate_id=? AND level=? AND event_type='note_voice_observation' ORDER BY elapsed_ms ASC, id ASC",
            (candidate_id, level)
        ).fetchall()
        db.close()
    except Exception as e:
        print(f"UYARI (extract_voice_observations c={candidate_id} L{level}): {type(e).__name__}: {e}")
        return []
    out = []
    for r in rows[:10]:
        try:
            d = json.loads(r["event_data"] or "{}")
        except Exception:
            continue
        out.append({
            "elapsed_ms": _safe_int(r["elapsed_ms"]),
            "ton": d.get("ton"),
            "akicilik": d.get("akicilik"),
            "tereddut": d.get("tereddut"),
            "gozlem": (d.get("gozlem") or "")[:500],
        })
    return out

MIMIC_ANALYSIS_PROMPT = """Aşağıda bir iş mülakatı sırasında adayın web kamerasından ~45 saniye arayla alınmış kareler var; her karenin öncesinde [t=SANİYE] etiketi bulunur. Bu kareler DÜŞÜK çözünürlüklüdür ve seyrektir.

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

def analyze_frames(candidate_id: int, level: int) -> dict:
    """Biriken mimik analiz karelerini (reason='mimic_sample') TEK toplu multimodal çağrıda
    değerlendirir — KARE BAŞINA AYRI ÇAĞRI YOK. Model = MIMIC_ANALYSIS_MODEL (tek sabit).
    Çıktı yapılandırılmış JSON. Bu çıktı PUANI OYNATMAZ; yalnızca destekleyici gözlemdir.
    Hata olursa {} döner ve rapor akışı denetçisiz/mimiksiz sürer."""
    if not OPENAI_API_KEY:
        return {}
    try:
        db = get_db()
        frames = db.execute(
            "SELECT image_base64, elapsed_ms FROM snapshots WHERE candidate_id=? AND reason='mimic_sample' ORDER BY elapsed_ms ASC, id ASC",
            (candidate_id,)
        ).fetchall()
        db.close()
    except Exception as e:
        print(f"UYARI (analyze_frames fetch c={candidate_id}): {type(e).__name__}: {e}")
        return {}
    frames = [f for f in frames if f["image_base64"]][:24]
    if len(frames) < 2:
        return {"durum": "yetersiz_kare", "kare_sayisi": len(frames)}

    content = [{"type": "text", "text": MIMIC_ANALYSIS_PROMPT}]
    for f in frames:
        secs = round(_safe_int(f["elapsed_ms"]) / 1000)
        img = f["image_base64"]
        if not img.startswith("data:"):
            img = "data:image/jpeg;base64," + img
        content.append({"type": "text", "text": f"[t={secs}s]"})
        content.append({"type": "image_url", "image_url": {"url": img, "detail": "low"}})

    try:
        resp = openai_call(
            "POST", "https://api.openai.com/v1/chat/completions",
            json_body={
                "model": MIMIC_ANALYSIS_MODEL,
                "messages": [{"role": "user", "content": content}],
                "max_tokens": 1200, "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
            timeout=90.0, step="mimic_analysis", severity="background", retry=False,
            context={"candidate_id": candidate_id, "level": level},
        )
        result = resp.json()
        record_openai_chat_usage(candidate_id, level, MIMIC_ANALYSIS_MODEL, "mimic_frame_analysis", result)
        parsed = json.loads(result["choices"][0]["message"]["content"])
        parsed["kare_sayisi"] = len(frames)
        parsed["_uyari"] = ("Bu gözlemler kameradan alınan az sayıda düşük çözünürlüklü kareye dayanır; "
                            "teşhis, duygu tespiti veya kesin kişilik hükmü DEĞİLDİR ve mülakat puanını ETKİLEMEZ.")
        return parsed
    except Exception as e:
        print(f"UYARI (analyze_frames c={candidate_id} L{level}): {type(e).__name__}: {e}")
        return {}

def build_modality_evidence_block(candidate_id: int, level: int) -> str:
    """Mimik + ses metrikleri + ses gözlemlerini üretir (veya daha önce üretilmişse DB'den okur —
    kurtarma taramasında görü modelini yeniden çağırma), interviews.*_json kolonlarına yazar ve
    birincil yazara verilecek TEK metin bloğunu döner. Her analizör ayrı try/except."""
    try:
        db = get_db()
        row = db.execute(
            "SELECT mimic_analysis_json, voice_metrics_json, voice_observations_json FROM interviews WHERE candidate_id=? AND level=?",
            (candidate_id, level)
        ).fetchone()
        db.close()
    except Exception as e:
        print(f"UYARI (modality mevcut kayıt okuma c={candidate_id}): {type(e).__name__}: {e}")
        row = None

    mimic, metrics, obs = {}, {}, []
    try:
        if row and row["mimic_analysis_json"]:
            mimic = json.loads(row["mimic_analysis_json"])
        else:
            mimic = analyze_frames(candidate_id, level)
            if mimic:
                _store_interview_json(candidate_id, level, "mimic_analysis_json", mimic)
    except Exception as e:
        print(f"UYARI (modality mimik c={candidate_id}): {type(e).__name__}: {e}")
    try:
        metrics = compute_voice_metrics(candidate_id, level)
        if metrics:
            _store_interview_json(candidate_id, level, "voice_metrics_json", metrics)
        elif row and row["voice_metrics_json"]:
            metrics = json.loads(row["voice_metrics_json"])
    except Exception as e:
        print(f"UYARI (modality ses metrik c={candidate_id}): {type(e).__name__}: {e}")
    try:
        obs = extract_voice_observations(candidate_id, level)
        if obs:
            _store_interview_json(candidate_id, level, "voice_observations_json", obs)
        elif row and row["voice_observations_json"]:
            obs = json.loads(row["voice_observations_json"])
    except Exception as e:
        print(f"UYARI (modality ses gözlem c={candidate_id}): {type(e).__name__}: {e}")

    parts = []
    if mimic:
        parts.append("MİMİK / GÖRÜNTÜ GÖZLEMLERİ:\n" + json.dumps(mimic, ensure_ascii=False, indent=1))
    if metrics:
        parts.append("SES METRİKLERİ (tur bazlı — cevap gecikmesi, tur uzunluğu, düşünme süresi, söz kesme). Bu SAYILARI raporun 'Serbest Gözlemler' bölümüne SOMUT yaz:\n" + json.dumps(metrics, ensure_ascii=False, indent=1))
    else:
        # KALEM 2/3: ses metrikleri yoksa SESSİZCE ATLAMA — rapora açıkça yaz.
        parts.append("SES METRİKLERİ: TOPLANAMADI — realtime_events'te konuşma başlangıç/bitiş olayı yok "
                     "(cevap gecikmesi, duraklama, konuşma/sessizlik oranı, söz kesme sayısı ölçülemedi). "
                     "Raporun 'Serbest Gözlemler' bölümünde 'ses verisi toplanamadı' diye AÇIKÇA belirt.")
    if obs:
        parts.append("MÜLAKATÇI SES GÖZLEMLERİ (mülakat anında kaydedildi):\n" + json.dumps(obs, ensure_ascii=False, indent=1))
    # KALEM 1/3: kamera karesi kapsamı (İKİ set ayrı) — deterministik, her zaman eklenir.
    _cov = compute_modality_coverage(candidate_id, level)
    parts.append("KAMERA KARESİ KAPSAMI (deterministik — 'dogrulama'=panel/PDF galerisi, 'mimik'=yalnız AI):\n" + json.dumps(_cov, ensure_ascii=False))
    if not parts:
        return ""
    return ("=== MODALİTE KANITLARI (DESTEKLEYİCİ) ===\n"
            "Aşağıdaki görüntü/ses sinyalleri YALNIZCA destekleyici gözlemdir. Toplam puanı ve "
            "İşe Al / Değerlendirmeye Al / Reddet kararını DEĞİŞTİRMEZLER. Yalnızca 'Serbest Gözlemler' "
            "bölümünü zenginleştirmek için, temkinli ve 'gözlem — teşhis değil' diliyle kullan. "
            "Bunlar duygu tespiti, kişilik hükmü veya yalan analizi DEĞİLDİR. Sinyal zayıf/eksikse yok say.\n\n"
            + "\n\n".join(parts))

def _set_reviewer_status(candidate_id: int, level: int, status: str, error: Optional[str]) -> None:
    """FAZ D: denetçi sessizce düşmesin — durumu ('ok'/'skipped'/'failed') + kısa nedeni
    interviews satırına yazar; admin panelinde görünür rozet buradan beslenir."""
    try:
        db = get_db()
        db.execute("UPDATE interviews SET reviewer_status=?, reviewer_error=? WHERE candidate_id=? AND level=?",
                   (status, (error or "")[:500], candidate_id, level))
        db.commit(); db.close()
    except Exception as e:
        print(f"UYARI (_set_reviewer_status c={candidate_id} L{level}): {type(e).__name__}: {e}")

def _reviewer_criteria_block(position_criteria: list) -> str:
    """GÖREV 1.6 — müfettişe GPT ile AYNI kriter setini ve AYNI maksimum puanları verir."""
    lines = ["PUAN 1 (pozisyon) kriterleri ve tavanları:"]
    for c in (position_criteria or []):
        if c.get("name"):
            lines.append(f"- {c['name']}: __/{_safe_int(c.get('weight'))}")
    lines.append("PUAN 2 (kişisel/bilişsel profil) kriterleri ve tavanları:")
    for pc in PROFILE_CRITERIA:
        lines.append(f"- {pc['name']}: __/{pc['weight']}")
    return "\n".join(lines)

def run_report_reviewer(candidate_id: int, level: int, transcript_text: str, final_report: str, modality_block: str,
                        position_criteria: Optional[list] = None):
    """GÖREV 1.3-1.6 — BAĞIMSIZ İKİNCİ DEĞERLENDİRİCİ. Artık NİHAİ raporu (basılacak hali:
    kriter tabloları + KARAR + gerekçe dahil) görür; taslağı değil. Raporu YENİDEN YAZMAZ ve
    karara/puana ETKİ ETMEZ. Serbestçe, kısıtsız kendi görüşünü yazar — GPT ile farklı görüşte
    olabilir, şerh koyabilir. Ayrıca GPT ile AYNI kriter setinde KENDİ puanını verir (yalnız
    referans tablo). Denetçi HER ZAMAN OpenAI (OPENAI_REVIEWER_MODEL); L2'de Anthropic'e gitmez.
    Dönüş: (notes, status, error). Hata/atlama → notes='' ve rapor DENETÇİSİZ, DEĞİŞMEDEN kalır."""
    if not OPENAI_API_KEY:
        return "", "skipped", "OPENAI_API_KEY tanımlı değil"
    prompt = f"""Sen bir işe alım raporunun BAĞIMSIZ İKİNCİ DEĞERLENDİRİCİSİSİN. Aşağıda bir mülakatın transkripti, sistemin ürettiği NİHAİ RAPOR (basılacak hali) ve (varsa) modalite kanıtları var.

Raporu YENİDEN YAZMA. Kararı/puanı DEĞİŞTİREMEZSİN — senin çıktın karara etki etmez, rapora AYRI bir "ikinci değerlendirici görüşü" bloğu olarak eklenir. Görüşünü SERBESTÇE, kısıtsız yaz; birincil değerlendirmeyle aynı fikirde olmak zorunda değilsin, şerh koyabilirsin.

Türkçe, kısa ve madde madde ver:
1. ABARTILI / KANITSIZ İDDİALAR: raporda transkriptle desteklenmeyen veya aşırı iddialı cümleler (kısa alıntıyla).
2. EKSİK KANIT: transkriptte olan ama raporun atladığı önemli sinyaller (aday kendi ağzıyla söylediği bilgi eksiklikleri dahil).
3. PUAN KALİBRASYONU: rapordaki toplam puan ve kriter puanları kanıtlara göre yüksek mi / düşük mü / uygun mu — kısa gerekçe.
4. GÜVEN DÜZEYİ: (yüksek / orta / düşük) + kısa neden.
5. KARARA İLİŞKİN GÖRÜŞ: Rapordaki sistem kararına (eşik tablosundan) katılıyor musun? Katılmıyorsan neden — bu yalnızca görüştür, kararı değiştirmez.
6. KRİTER PUAN TABLOSU (ZORUNLU): Aşağıdaki kriter setinde, AYNI maksimum puanlarla KENDİ puanını ver. Her satırı TAM olarak şu formatta yaz (otomatik ayrıştırılacak):
KRITER_PUAN: <kriter adı> = <senin puanın>/<maksimum>
Önce PUAN 1 kriterleri, sonra PUAN 2 kriterleri. Her kriter için bir satır.

{_reviewer_criteria_block(position_criteria)}

İLKE: Kanıt yoksa ne lehte ne aleyhte varsayım yapma. Modalite kanıtları (mimik/ses) yalnızca destekleyici. Belirgin bir görüş ayrılığı yoksa "Belirgin bir görüş ayrılığı yok." yaz (kriter puan tablosunu YİNE DE doldur).

=== TRANSKRİPT ===
{(transcript_text or '')[:TRANSCRIPT_PROMPT_MAX_CHARS]}

=== NİHAİ RAPOR (basılacak hali) ===
{(final_report or '')[:16000]}

=== MODALİTE KANITLARI ===
{modality_block or 'Yok'}"""
    try:
        resp = openai_call(
            "POST", "https://api.openai.com/v1/chat/completions",
            json_body={"model": OPENAI_REVIEWER_MODEL, "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": 1400, "temperature": 0.1},
            timeout=60.0, step="report_reviewer", severity="background", retry=False,
            context={"candidate_id": candidate_id, "level": level},
        )
        result = resp.json()
        record_openai_chat_usage(candidate_id, level, OPENAI_REVIEWER_MODEL, "report_reviewer", result)
        return (result["choices"][0]["message"]["content"] or "").strip(), "ok", ""
    except Exception as e:
        print(f"UYARI (run_report_reviewer c={candidate_id} L{level}): {type(e).__name__}: {e}")
        return "", "failed", f"{type(e).__name__}: {e}"

def parse_reviewer_criterion_scores(notes: str) -> dict:
    """GÖREV 1.6 — müfettiş çıktısındaki 'KRITER_PUAN: <ad> = <p>/<max>' satırlarını ayrıştırır.
    Dönüş: {kriter_adı: (puan, maks)}."""
    out = {}
    for m in re.finditer(r"KR[İI]TER_PUAN\s*:\s*(.+?)\s*=\s*(\d+)\s*/\s*(\d+)", notes or "", re.IGNORECASE):
        name = re.sub(r"[*_`]", "", m.group(1)).strip()
        if name:
            out[name] = (int(m.group(2)), int(m.group(3)))
    return out

def _reviewer_score_tables(reviewer_scores: dict, position_criteria: list, final_report: str) -> str:
    """GÖREV 1.6 — 'Kriter | GPT | Müfettiş | Fark' tabloları (PUAN 1 ayrı, PUAN 2 ayrı).
    GPT puanı NİHAİ rapordaki kriter hücrelerinden okunur. Bu tablo YALNIZ REFERANS — hiçbir
    hesaba girmez."""
    def _gpt_cell(name):
        # rapordaki "| <kriter> | <p>/<max> ..." satırından GPT'nin verdiği puanı çek
        for ln in (final_report or "").splitlines():
            if "|" in ln and _norm_name(name)[:14] in _norm_name(ln):
                mm = re.search(r"(\d+)\s*/\s*(\d+)", ln)
                if mm:
                    return f"{mm.group(1)}/{mm.group(2)}"
                if re.search(r"değerlendirilemedi|değerlendirilmedi|yetersiz", ln, re.IGNORECASE):
                    return "—"
        return "?"
    def _tbl(title, crit_list):
        rows = [f"_{title}_", "", "| Kriter | Birincil | İkinci değerlendirici | Fark |",
                "|---|---|---|---|"]
        any_row = False
        for c in crit_list:
            nm = c["name"] if isinstance(c, dict) else c
            gpt = _gpt_cell(nm)
            rv = reviewer_scores.get(nm)
            if rv is None:
                # gevşek eşleşme
                for k, v in reviewer_scores.items():
                    if _norm_name(k)[:12] and _norm_name(k)[:12] in _norm_name(nm):
                        rv = v; break
            rv_txt = f"{rv[0]}/{rv[1]}" if rv else "—"
            diff = ""
            gm = re.match(r"(\d+)/(\d+)", gpt or "")
            if gm and rv:
                diff = f"{rv[0] - int(gm.group(1)):+d}"
            rows.append(f"| {nm} | {gpt} | {rv_txt} | {diff} |")
            any_row = True
        return "\n".join(rows) if any_row else ""
    parts = [p for p in (_tbl("PUAN 1 — pozisyon kriterleri", position_criteria or []),
                         _tbl("PUAN 2 — kişisel/bilişsel profil", PROFILE_CRITERIA)) if p]
    return "\n\n".join(parts)

def append_reviewer_section(candidate_id: int, level: int, transcript_text: str, modality_block: str,
                            position_criteria: Optional[list] = None) -> None:
    """GÖREV 1 — müfettişi NİHAİ rapor üzerinde çalıştırır ve çıktısını rapora AYRI, tek blok
    olarak ekler (tüm GPT içeriği + iki kriter tablosu + KARAR'dan SONRA; kamera karelerinden
    ÖNCE = rapor gövdesinin sonu). Karara/puana ETKİ ETMEZ. Müfettiş atlanır/patlarsa rapor
    DENETÇİSİZ ve DEĞİŞMEDEN kalır. İdempotent: blok zaten varsa yeniden eklenmez."""
    db = get_db()
    try:
        row = db.execute("SELECT report FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, level)).fetchone()
    finally:
        db.close()
    final_report = (row["report"] if row else "") or ""
    if not final_report.strip():
        return
    _HEAD = "**İkinci Değerlendirici Görüşü (bağımsız — karara ve puana ETKİ ETMEZ):**"
    if _HEAD in final_report:
        return
    notes, status, err = run_report_reviewer(candidate_id, level, transcript_text, final_report, modality_block,
                                             position_criteria=position_criteria)
    _set_reviewer_status(candidate_id, level, status, err)
    if not notes.strip():
        return
    rv_scores = parse_reviewer_criterion_scores(notes)
    body_notes = strip_reviewer_meta_tags(notes)
    # kriter puan satırlarını serbest metinden çıkar (tablo ayrı gösteriliyor)
    body_notes = re.sub(r"(?m)^\s*KR[İI]TER_PUAN\s*:.*$\n?", "", body_notes).strip()
    tables = _reviewer_score_tables(rv_scores, position_criteria or [], final_report)
    block = (f"\n\n---\n\n{_HEAD}\n\n"
             f"Bu bölüm ikinci bir değerlendiricinin bağımsız görüşüdür. Yukarıdaki puanları, KARAR'ı "
             f"veya rapor metnini DEĞİŞTİRMEZ; birincil değerlendirmeyle farklı yönde olabilir.\n\n"
             f"{body_notes}\n")
    if tables:
        block += (f"\n**Kriter Puan Karşılaştırması (yalnız referans — hiçbir hesaba girmez):**\n\n{tables}\n")
    updated = final_report.rstrip() + block
    db = get_db()
    try:
        db.execute("UPDATE interviews SET report=? WHERE candidate_id=? AND level=?", (updated, candidate_id, level))
        db.commit()
    finally:
        db.close()
    record_system_decision(candidate_id, level, "ikinci_degerlendirici_eklendi",
                           "İkinci değerlendirici görüşü NİHAİ rapor üzerinde üretildi ve rapora ayrı blok olarak eklendi (karara/puana etkisi yok).",
                           {"reviewer_status": status})

def parse_reviewer_meta(notes: str) -> dict:
    """Denetçi çıktısının sonundaki 'OZET_TON:' / 'DUSUK_PUAN:' etiketlerini ayrıştırır.
    Dönüş: {'ozet_ton': 'OLUMLU'|'NOTR'|'OLUMSUZ'|None, 'dusuk_puan': [kriter adları]}."""
    out = {"ozet_ton": None, "dusuk_puan": []}
    if not notes:
        return out
    mt = re.search(r"OZET_TON\s*:\s*(OLUMLU|NOTR|NÖTR|OLUMSUZ)", notes, re.IGNORECASE)
    if mt:
        v = mt.group(1).upper().replace("NÖTR", "NOTR")
        out["ozet_ton"] = v
    md = re.search(r"DUSUK_PUAN\s*:\s*([^\n]+)", notes, re.IGNORECASE)
    if md:
        raw = md.group(1).strip()
        if raw.lower() not in ("yok", "-", "none", "hiçbiri", "hicbiri", ""):
            out["dusuk_puan"] = [s.strip() for s in re.split(r"[,;/]| ve ", raw) if s.strip() and len(s.strip()) > 2][:4]
    return out

def strip_reviewer_meta_tags(notes: str) -> str:
    """Rapora eklenirken 'OZET_TON:' / 'DUSUK_PUAN:' etiket satırları görünmesin (iç kullanım)."""
    if not notes:
        return notes
    return re.sub(r"\n?^[ \t]*(OZET_TON|DUSUK_PUAN)\s*:[^\n]*$", "", notes, flags=re.IGNORECASE | re.MULTILINE).strip()

def _insert_report_section(reply: str, section_body: str, pre_note: str = "") -> str:
    """Denetçi çıktısını rapora DETERMİNİSTİK olarak ekler: '---RAPORSON---' varsa hemen öncesine,
    yoksa '---STANDARTCV---' öncesine, o da yoksa sona. finalize_interview'in ---RAPOR--- gövde
    regex'i bu bölümü rapor içinde yakalar. pre_note verilirse bölümün başına eklenir."""
    if not section_body:
        return reply
    _pn = (pre_note.strip() + "\n\n") if pre_note else ""
    block = f"\n\n**İkinci Model Değerlendirmesi / Görüş Ayrılıkları:**\n{_pn}{section_body}\n"
    if "---RAPORSON---" in reply:
        return reply.replace("---RAPORSON---", block + "\n---RAPORSON---", 1)
    if "---STANDARTCV---" in reply:
        return reply.replace("---STANDARTCV---", block + "\n---STANDARTCV---", 1)
    return reply + block

# ═══ PUAN BAĞIMSIZLIĞI ═══
# PUAN 2 (kişisel/bilişsel profil) kendi çağrısında, PUAN 1'i GÖRMEDEN üretilir (aşağıda).
# GÖREV 1.2 — İKİNCİ MODELİN PUANA MÜDAHALESİ TAMAMEN KALDIRILDI: eski _REVIEWER_HAIRCUT (%40),
# reviewer_flagged_criteria, apply_reviewer_score_revision ve annotate_revised_criteria_prose
# fonksiyonları silindi. İkinci model artık YALNIZCA nihai rapor üzerinde bağımsız görüş yazar
# (append_reviewer_section) — puana/karara hiçbir etkisi yoktur.

def build_independent_profile_prompt(candidate: dict, transcript: str) -> str:
    """PUAN 2 bölümünü TEK BAŞINA üreten prompt — pozisyon kriterleri, PUAN 1 puanı veya işe
    alım önerisi HİÇ verilmez. Yalnız transkript + sabit profil kriterleri."""
    p2_keys = {"puan2_baslik", "puan2_aciklama", "puan2_tablo", "puan2_toplam", "puan2_veto"}
    p2_body = "\n".join(
        (t.format(profile_table_filled=build_profile_table_filled()) if "{" in t else t)
        for k, lv, t in REPORT_BODY_SECTIONS if k in p2_keys
    )
    return f"""Aşağıda bir iş mülakatının transkripti var. GÖREVİN: SADECE adayın KİŞİSEL VE BİLİŞSEL PROFİLİNİ değerlendiren "PUAN 2" bölümünü üretmek.

ÖNEMLİ: Pozisyon uygunluğu, teknik yeterlilik, işe alım önerisi veya "PUAN 1" ile İLGİLENME — onları görmüyorsun ve değerlendirmiyorsun. Yalnız aşağıdaki sabit profil kriterlerini, transkriptten SOMUT örnek + [dk] dakika damgası ile puanla. Dayanaksız çıkarım, kişilik teşhisi, IQ/zekâ yorumu YASAK. Her kriter kendi tavanını AŞAMAZ.

Eksik kriter iki türlüdür: `Değerlendirilmedi (sorulmadı) — <gerekçe>` (paydayı etkilemez) ve `Yetersiz (soruldu, veri alınamadı) — <gerekçe>` (0 puan, paydada kalır).

TRANSKRİPT:
{(transcript or '')[:TRANSCRIPT_PROMPT_MAX_CHARS]}

Çıktı yalnızca şu bölüm olsun (başka hiçbir şey yazma, ---RAPOR--- / ---RAPORSON--- etiketi KOYMA):
{p2_body}"""

def run_independent_profile_call(candidate_id: int, level: int, provider: str, model: Optional[str],
                                 candidate: dict, transcript: str):
    """PUAN 2'yi ayrı çağrıda üretir. Dönüş: profil bölüm metni | None (hata → primary'nin
    profili kullanılmaya devam)."""
    prompt = build_independent_profile_prompt(candidate, transcript)
    try:
        if provider == "claude":
            if not ANTHROPIC_API_KEY:
                return None
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=60.0)
            resp = client.messages.create(model=model or "claude-sonnet-4-6", max_tokens=2200,
                                          messages=[{"role": "user", "content": prompt}])
            record_anthropic_usage(candidate_id, level, model or "claude-sonnet-4-6", "profile_scoring_independent", resp)
            return (resp.content[0].text or "").strip()
        elif provider == "openai":
            if not OPENAI_API_KEY:
                return None
            resp = openai_call("POST", "https://api.openai.com/v1/chat/completions",
                               json_body={"model": model or OPENAI_REPORT_MODEL,
                                          "messages": [{"role": "user", "content": prompt}],
                                          "max_tokens": 2200, "temperature": 0.1},
                               timeout=60.0, step="profile_scoring", severity="background", retry=False,
                               context={"candidate_id": candidate_id, "level": level})
            result = resp.json()
            record_openai_chat_usage(candidate_id, level, model or OPENAI_REPORT_MODEL, "profile_scoring_independent", result)
            return (result["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        print(f"UYARI (run_independent_profile_call c={candidate_id} L{level}): {type(e).__name__}: {e}")
    return None

def splice_profile_region(reply: str, new_profile_text: str) -> str:
    """reply içindeki '### PUAN 2 ...' → '---RAPORSON---' arası bölümü bağımsız çağrının çıktısıyla
    değiştirir. Sınır bulunamazsa reply DEĞİŞMEDEN döner."""
    if not reply or not new_profile_text:
        return reply
    new_profile_text = re.sub(r'---RAPOR(SON)?---|---STANDARTCV(SON)?---', '', new_profile_text).strip()
    m = re.search(r"(?:\n-{2,}\s*\n)?\s*#{0,4}\s*PUAN\s*2\b[\s\S]*?(?=\n?---RAPORSON---|\Z)", reply, re.IGNORECASE)
    if not m:
        return reply
    return reply[:m.start()] + "\n\n---\n" + new_profile_text + "\n\n" + reply[m.end():]

# GÖREV 1.2 — reviewer_flagged_criteria / apply_reviewer_score_revision / annotate_revised_criteria_prose
# ve yardımcıları (_match_strength, _VALUE_JUDGMENT_RE, _ANNOTATE_* , _criterion_fully_mentioned)
# TAMAMEN SİLİNDİ. İkinci model artık puana/metne müdahale etmiyor (bkz. append_reviewer_section).

def run_deferred_finish_job(candidate_id: int, level: int, regen: bool = False):
    """BackgroundTasks'ten (mülakat az önce bitti) VEYA kurtarma taramasından (takılı kalmış eski
    kayıt) çağrılır — girdisini SADECE DB'deki pending_finish_* alanlarından okur, hangi
    tetikleyiciden geldiği önemli değildir. İDEMPOTENT: completed_at zaten doluysa no-op —
    iki kez tetiklenirse (ör. hem arka plan görevi hem kurtarma taraması aynı satırı yakalarsa)
    ikinci çağrı hiçbir şey yapmadan çıkar, ikinci bir rapor/e-posta üretmez.
    regen=True: EK — geriye dönük yeniden üretim; completed_at guard'ı atlanır, orijinal
    bitiş saati korunur (finalize_interview regen=True)."""
    db = get_db()
    interview = db.execute("SELECT * FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, level)).fetchone()
    db.close()
    if not interview or (interview["completed_at"] and not regen):
        return
    provider = interview["pending_finish_provider"]
    model = interview["pending_finish_model"]
    system = interview["pending_finish_system"]
    payload = interview["pending_finish_payload"]
    terminated_reason = interview["pending_finish_terminated_reason"]
    if not payload:
        _mark_finish_failed(candidate_id, level, "pending_finish_payload boş — gönderilecek kayıtlı istek yok")
        return
    try:
        # FAZ D — MODALİTE KANITLARI: mimik + ses metrikleri + ses gözlemleri. En iyi çaba;
        # patlarsa boş döner. Birincil yazara ek kanıt bloğu olarak verilir (system'e değil,
        # user payload'ına eklenir — Claude'da cache öneki bozulmasın).
        try:
            modality_block = build_modality_evidence_block(candidate_id, level)
        except Exception as e:
            print(f"UYARI (modality blok üretimi c={candidate_id} L{level}): {type(e).__name__}: {e}")
            modality_block = ""

        # BÖLÜM 2.4 + 3: birincil yazara TAM transkript + yapılandırılmış olay kayıtları verilir —
        # Dil Gözlemi ve Sonuç Gerekçesi ancak ham konuşmadan + olaylardan doğru üretilebilir.
        extra_blocks = [b for b in (modality_block,) if b]
        iv = interview
        # L2 report_prompt zaten transkripti içeriyor (TRANSKRİPT: ...); L1/L3'ün compact-memory
        # payload'ı içermiyor — yalnızca eksikse tam transkript eklenir (çift gönderme yok).
        if payload and "TRANSKRİPT" not in payload:
            try:
                tview = build_transcript_view(iv["messages"] if iv and "messages" in iv.keys() else "[]", level,
                                              iv["started_at"] if iv and "started_at" in iv.keys() else None)
                ttext = transcript_to_text(tview)
                if ttext.strip():
                    extra_blocks.append("=== TAM TRANSKRİPT (rapor bu ham konuşmaya dayanmalı) ===\n" + ttext[:TRANSCRIPT_PROMPT_MAX_CHARS])
            except Exception as e:
                print(f"UYARI (deferred transkript bloğu c={candidate_id}): {type(e).__name__}: {e}")
        try:
            if iv and "result_events_json" in iv.keys() and iv["result_events_json"]:
                evs = json.loads(iv["result_events_json"]) or []
                if evs:
                    extra_blocks.append("=== OLAY KANITLARI (ihlal / teknik / sonlandırma — 'Sonuç Gerekçesi'ni buna dayandır) ===\n"
                                        + json.dumps(evs, ensure_ascii=False, indent=1))
        except Exception as e:
            print(f"UYARI (deferred olay bloğu c={candidate_id}): {type(e).__name__}: {e}")
        primary_payload = payload if not extra_blocks else (payload + "\n\n" + "\n\n".join(extra_blocks))

        # KALEM 5 — rapor gövdesi + kriter tablosu + iki puan tablosu + görüş ayrılıkları bir arada
        # 3800/4000 token'a sığmıyor, çıktı ---STANDARTCVSON--- öncesi kesiliyordu. 8000'e çıkarıldı
        # (Claude sonnet ve gpt-4o çıktı sınırının çok altında; gerçek rapor ~2500-4500 token).
        REPORT_MAX_TOKENS = 8000
        if provider == "claude":
            if not ANTHROPIC_API_KEY:
                raise RuntimeError("ANTHROPIC_API_KEY tanımlı değil")
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=90.0)
            response = client.messages.create(
                model=model or "claude-sonnet-4-6", max_tokens=REPORT_MAX_TOKENS,
                system=cached_system(system) if system else anthropic.NOT_GIVEN,
                messages=[{"role": "user", "content": primary_payload}]
            )
            # FAZ D: birincil rapor çağrısı panelde AYRI KALEM olsun — add_token_usage yerine
            # record_anthropic_usage (ai_usage_logs'a satır + interviews.total_* BİR KEZ artış).
            record_anthropic_usage(candidate_id, level, model or "claude-sonnet-4-6", "report_generation_primary", response)
            reply = response.content[0].text
            if getattr(response, "stop_reason", None) == "max_tokens" or "---STANDARTCVSON---" not in reply:
                print(f"[REPORT_TRUNCATED] c={candidate_id} L{level} stop_reason={getattr(response,'stop_reason',None)}")
                record_system_decision(candidate_id, level, "rapor_kesildi",
                                       "Rapor üretimi token sınırına takıldı; eksik bölümler deterministik tamamlandı.",
                                       {"stop_reason": getattr(response, "stop_reason", None)},
                                       warnings=["Rapor üretimi token sınırına takıldı (bkz. report_tech_note)."])
            # Normal kapanış çağrısı bile [ADAY_CIKIS_TALEBI] üretebilir (mevcut senkron
            # interview_chat akışıyla aynı davranış) — öyleyse ikinci bir "gerçek bitiş" çağrısı yap.
            if "[ADAY_CIKIS_TALEBI]" in reply:
                exit_payload = f"""ÖNCEKİ KISA HAFIZA:
{interview["compact_memory"] or "Henüz yok."}

GÖREV: Aday mülakatı sonlandırmak istediğini net şekilde belirtti (bu bir teknik arıza bildirimi de olabilir). Mülakatı şimdi bitir ve mevcut bilgilere göre raporu üret. Adayı ikna etmeye çalışma, sadece elindeki bilgiyle adil bir değerlendirme yap; eksik kalan kısımları düşük puan nedeni yapma, sadece "yeterli veri toplanamadı" notu düş. [MÜLAKATBİTTİ] etiketini kullan."""
                exit_response = client.messages.create(
                    model=model or "claude-sonnet-4-6", max_tokens=REPORT_MAX_TOKENS,
                    system=cached_system(system) if system else anthropic.NOT_GIVEN,
                    messages=[{"role": "user", "content": exit_payload}]
                )
                record_anthropic_usage(candidate_id, level, model or "claude-sonnet-4-6", "report_generation_primary", exit_response)
                reply = exit_response.content[0].text
                terminated_reason = terminated_reason or "Aday talebiyle erken sonlandırıldı"
        elif provider == "openai":
            resp = openai_call(
                "POST", "https://api.openai.com/v1/chat/completions",
                json_body={"model": model or OPENAI_REPORT_MODEL, "messages": [{"role": "user", "content": primary_payload}], "max_tokens": REPORT_MAX_TOKENS, "temperature": 0.1},
                timeout=120.0, step="report_generation", severity="user", retry=True,
                context={"candidate_id": candidate_id, "level": level},
            )
            result = resp.json()
            record_openai_chat_usage(candidate_id, level, model or OPENAI_REPORT_MODEL, "l2_report_generation_deferred", result)
            reply = result["choices"][0]["message"]["content"]
            _fr = (result.get("choices") or [{}])[0].get("finish_reason")
            if _fr == "length" or "---STANDARTCVSON---" not in reply:
                print(f"[REPORT_TRUNCATED] c={candidate_id} L{level} finish_reason={_fr}")
                record_system_decision(candidate_id, level, "rapor_kesildi",
                                       "Rapor üretimi token sınırına takıldı; eksik bölümler deterministik tamamlandı.",
                                       {"finish_reason": _fr},
                                       warnings=["Rapor üretimi token sınırına takıldı (bkz. report_tech_note)."])
        else:
            raise RuntimeError(f"Bilinmeyen pending_finish_provider: {provider!r}")

        # item 6 — modelin İŞLENMEMİŞ çıktısını sakla (yalnız admin panel; ≤20000 kr). Denetçi /
        # bağımsız profil / puanlama doğrulaması / öneri hizalaması HİÇBİRİ uygulanmadan ÖNCEKİ hal.
        try:
            _dbraw = get_db()
            _dbraw.execute("UPDATE interviews SET raw_report=? WHERE candidate_id=? AND level=?",
                           ((reply or "")[:20000], candidate_id, level))
            _dbraw.commit(); _dbraw.close()
        except Exception as e:
            print(f"UYARI (raw_report kaydı c={candidate_id}): {type(e).__name__}: {e}")

        # transkript (denetçi + bağımsız profil çağrısı + puanlama doğrulaması ortak kullanır)
        _cand_row, _iv_row, transcript_text = None, None, ""
        try:
            db3 = get_db()
            try:
                _cand_row = db3.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
                _iv_row = db3.execute("SELECT criteria_coverage_json, messages, started_at FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, level)).fetchone()
            finally:
                db3.close()
            transcript_text = transcript_to_text(build_transcript_view(
                _iv_row["messages"] if _iv_row else "[]", level, _iv_row["started_at"] if _iv_row else None))
        except Exception as e:
            print(f"UYARI (deferred transkript derleme c={candidate_id}): {type(e).__name__}: {e}")
        try:
            _pcov = json.loads(_iv_row["criteria_coverage_json"]) if (_iv_row and _iv_row["criteria_coverage_json"]) else None
        except Exception:
            _pcov = None
        _pcrit = ((get_position(_cand_row["position"]) or {}).get("criteria") or []) if _cand_row else []

        # ═══ GÖREV 1.6 / bağımsız profil — PUAN 2 (profil) AYRI çağrıda, PUAN 1'i görmeden üretilir ═══
        try:
            if "PUAN 2" in reply or "PROFİL PUANI" in reply:
                _indep_p2 = run_independent_profile_call(candidate_id, level, provider, model,
                                                         dict(_cand_row) if _cand_row else {}, transcript_text)
                if _indep_p2 and ("PROF" in _indep_p2.upper() or "PUAN 2" in _indep_p2.upper()):
                    _spliced = splice_profile_region(reply, _indep_p2)
                    if _spliced != reply:
                        reply = _spliced
                        record_system_decision(candidate_id, level, "profil_bagimsiz_uretildi",
                                               "PUAN 2 (kişisel/bilişsel profil) ayrı çağrıda, PUAN 1 sonucundan bağımsız üretildi.", {})
        except Exception as e:
            print(f"UYARI (bağımsız profil c={candidate_id}): {type(e).__name__}: {e}")

        # ═══ GÖREV 1.2 — MÜFETTİŞİN PUANA MÜDAHALESİ KALDIRILDI ═══
        # Eskiden burada müfettiş (ikinci model) ham taslağı görüp _REVIEWER_HAIRCUT (%40) ile
        # kriter puanı düşürüyordu; "kanıtsız iddia" (ters yön) ile "puanı fazla düşük" (ters yön)
        # listeleri aynı düşürme fonksiyonuna giriyordu (bozuk eşleştirme). BU TÜM YOL SİLİNDİ.
        # Puan = GPT'nin ham puanı. Sunucu YALNIZCA aritmetik bütünlük uygular (finalize_interview
        # içindeki recompute_and_fix_score: kriter tavanını aşan puanı sabitler, TOPLAM = kriter
        # toplamı yapar, normalize eder — bu bir YARGI revizyonu değildir).

        # ═══ GÖREV 1.3 — NİHAİ RAPORU ÜRET (karar + tüm deterministik düzenleme burada biter) ═══
        finalize_interview(candidate_id, reply, terminated_reason=terminated_reason, level=level, regen=regen)

        # ═══ GÖREV 1.3-1.6 — MÜFETTİŞ (ikinci model) EN SONDA, NİHAİ BELGE ÜZERİNDE ═══
        # Müfettiş artık taslağı DEĞİL, basılacak nihai raporu (kriter tabloları + KARAR + gerekçe
        # dahil) görür. Çıktısı karara/puana ETKİ ETMEZ; rapora ayrı, tek blok olarak eklenir.
        # Müfettiş atlanır/patlarsa rapor DENETÇİSİZ ve DEĞİŞMEDEN kalır — basım engellenmez.
        try:
            append_reviewer_section(candidate_id, level, transcript_text, modality_block, _pcrit)
        except Exception as e:
            print(f"UYARI (müfettiş bölümü ekleme c={candidate_id} L{level}): {type(e).__name__}: {e}")

        print(f"[PROCESSING_DONE] candidate_id={candidate_id} level={level} regen={regen}")
    except AIError as e:
        # openai_call zaten error_logs'a yazdı + kritik e-postayı tetikledi.
        print(f"HATA (run_deferred_finish_job AIError c={candidate_id} L{level}): {e.error_class}")
        _mark_finish_failed(candidate_id, level, f"AI hatası ({e.error_class})")
    except anthropic.APIError as e:
        ai_error_from_anthropic(e, "report_generation", {"candidate_id": candidate_id, "level": level}, severity="user")
        print(f"HATA (run_deferred_finish_job anthropic c={candidate_id} L{level}): {type(e).__name__}: {e}")
        _mark_finish_failed(candidate_id, level, f"AI (Claude) hatası: {type(e).__name__}")
    except Exception as e:
        print(f"HATA (run_deferred_finish_job c={candidate_id} L{level}): {type(e).__name__}: {e}")
        _mark_finish_failed(candidate_id, level, f"{type(e).__name__}: {e}")

def recover_stale_processing_interviews(stale_after_seconds: int = 180):
    """DAYANIKLILIK: konteyner yeniden başlarsa ya da bir arka plan görevi sessizce ölürse,
    'processing' durumunda takılı kalan kayıtları bulup run_deferred_finish_job ile yeniden
    dener. run_deferred_finish_job idempotent olduğu için güvenle tekrar tekrar çağrılabilir."""
    try:
        db = get_db()
        # completed_at IS NULL: normal kapanış takılması. EK: geriye dönük yeniden üretim
        # (admin_regenerate) completed_at'i korur; o yüzden ayrıca reason ile de yakala.
        rows = db.execute("""
            SELECT candidate_id, level, processing_started_at,
                   (completed_at IS NOT NULL AND pending_finish_reason='admin_regenerate') AS is_regen
            FROM interviews
            WHERE processing_status='processing'
              AND (completed_at IS NULL OR pending_finish_reason='admin_regenerate')
        """).fetchall()
        db.close()
        now = datetime.now()
        for r in rows:
            started = r["processing_started_at"]
            if isinstance(started, str):
                try:
                    started = datetime.fromisoformat(started.split(".")[0].replace("T", " "))
                except Exception as e:
                    print(f"UYARI (recover_stale_processing_interviews: processing_started_at ayrıştırılamadı, candidate_id={r['candidate_id']}): {type(e).__name__}: {e}")
                    started = None
            if started is None:
                continue
            age = (now - started).total_seconds()
            if age >= stale_after_seconds:
                _is_regen = bool(r["is_regen"]) if "is_regen" in r.keys() else False
                print(f"[RECOVERY] Takılı kalan {'yeniden üretim' if _is_regen else 'kapanış'} yeniden deneniyor: candidate_id={r['candidate_id']} level={r['level']} yaş={int(age)}sn")
                run_deferred_finish_job(r["candidate_id"], r["level"], _is_regen)
    except Exception as e:
        print(f"UYARI (recover_stale_processing_interviews): {type(e).__name__}: {e}")

async def _periodic_recovery_loop():
    while True:
        await asyncio.sleep(120)
        await asyncio.to_thread(recover_stale_processing_interviews)

@app.on_event("startup")
async def _on_startup_recovery():
    # Konteyner az önce başladıysa, önceki süreçten kalan "processing" kayıtları hemen bir kez
    # tara (0 sn bekleme — bunlar zaten kesin sahipsiz, önceki süreç artık yok).
    await asyncio.to_thread(recover_stale_processing_interviews, 0)
    asyncio.create_task(_periodic_recovery_loop())

def finalize_interview(candidate_id: int, reply: str, terminated_reason: Optional[str] = None, level: int = 1, regen: bool = False):
    report_match = re.search(r'---RAPOR---([\s\S]*?)(?:---RAPORSON---|---STANDARTCV---|\Z)', reply)
    cv_match = re.search(r'---STANDARTCV---([\s\S]*?)---STANDARTCVSON---', reply)
    score_match = re.search(r'TOPLAM PUAN:\s*(\d+)', reply)
    rec_match = re.search(r'Öneri:\s*(İşe Al|Değerlendirmeye Al|Reddet)', reply)

    standard_cv = cv_match.group(1).strip() if cv_match else ""
    score = extract_score(reply)

    # KALEM 2 — puanlama doğrulaması: kriter puanı tavanını aşamaz; skor gerçekten normalize edilir;
    # rapordaki "TOPLAM PUAN" ile DB'deki score aynı sayı olur.
    _score_warnings = []
    _profile_score = None
    try:
        _dbc = get_db()
        _cand_row = _dbc.execute("SELECT position FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        _ivr = _dbc.execute("SELECT criteria_coverage_json, messages, started_at FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, level)).fetchone()
        _dbc.close()
        _pos = get_position(_cand_row["position"]) if _cand_row else None
        _crit = (_pos or {}).get("criteria") or []
        try:
            _fcov = json.loads(_ivr["criteria_coverage_json"]) if (_ivr and _ivr["criteria_coverage_json"]) else None
        except Exception:
            _fcov = None
        try:
            _ftx = transcript_to_text(build_transcript_view(_ivr["messages"] if _ivr else "[]", level, _ivr["started_at"] if _ivr else None))
        except Exception:
            _ftx = None
        if report_match and _crit:
            _orig_region = report_match.group(1)
            _fixed_body, _final_score, _score_warnings = recompute_and_fix_score(_orig_region, _crit, score,
                                                                                criteria_coverage=_fcov, transcript=_ftx,
                                                                                candidate_id=candidate_id, level=level)
            # ÇİFT PUANLAMA — PUAN 2 (profil) bölgesi ayrıca doğrulanır (PUAN 1 ile aynı yöntem).
            _p1r, _p2r = split_report_regions(_fixed_body)
            if _p2r:
                _fixed_p2, _pscore, _p2w = recompute_profile_section(_p2r, transcript=_ftx, criteria_coverage=_fcov,
                                                                     candidate_id=candidate_id, level=level)
                if _pscore is not None:
                    _profile_score = _pscore
                _fixed_body = _p1r.rstrip() + "\n\n" + _fixed_p2.strip() + "\n"
                _score_warnings = list(_score_warnings) + list(_p2w)
            if _fixed_body != _orig_region:
                reply = reply.replace(_orig_region, _fixed_body, 1)
                report_match = re.search(r'---RAPOR---([\s\S]*?)(?:---RAPORSON---|---STANDARTCV---|\Z)', reply)
            if _final_score is not None:
                score = _final_score  # KALEM 5: denetçiden önce düzeltilmişse guard'la no-op, yine normalize skoru döner
    except Exception as e:
        print(f"UYARI (finalize_interview puanlama doğrulaması c={candidate_id}): {type(e).__name__}: {e}")

    if _profile_score is None:
        _profile_score = extract_profile_score(reply)

    # ÇİFT PUANLAMA — KARAR KURALI:
    #  - score_position (PUAN 1) = pozisyon uygunluğu → işe alım önerisi + <20 eşiği BUNA göre
    #  - score_profile (PUAN 2) = kişisel/bilişsel profil → eşik/gözlem; tek başına kesin veto DEĞİL
    #  - ana panelde saklanan `score` = iki puanın ortalaması (profil yoksa = score_position, eski davranış)
    #  - [VETO: ...] etiketi yalnızca ciddi olumsuz bulguda (saldırganlık/hakaret/tam kapalılık/
    #    sürdürülen açık düşmanlık) → recommendation "Reddet" + system_decision "veto"
    score_position = score
    score_profile = _profile_score
    score = round((score_position + score_profile) / 2) if score_profile is not None else score_position
    _veto_reason = detect_profile_veto(reply)

    recommendation = normalize_recommendation(score_position, rec_match.group(1) if rec_match else None)

    # BÖLÜM A2 (adım 3): Bu noktaya gelindiyse veri yeterliydi ve rapor üretildi (yetersiz veri
    # zaten upstream'de "Değerlendirilemedi"ye ayrılıyor — assess_data_sufficiency / A1). Dolayısıyla
    # burada gelen DÜŞÜK puan artık "veri yok" değil, gerçek bir başarısızlıktır → sonuç RED.
    # normalize_recommendation zaten s<40 için "Reddet" döndürür; <20'de raporu OLDUĞU GİBİ bas,
    # sahte "DEĞERLENDİRİLEMEDİ" metniyle DEĞİŞTİRME. (Kapsama eşiği ≠ puan eşiği.)
    score_below_reject_threshold = score_position is not None and score_position < 20
    if score_below_reject_threshold:
        recommendation = "Reddet"
        _set_result_meta(candidate_id, level,
                         result_reason=f"Pozisyon uygunluğu puanı ({score_position}/100) %20 eşiğinin çok altında; aday pozisyon kriterlerinde yeterli yetkinlik gösteremedi. Sonuç: Reddet.")

    # ÇİFT PUANLAMA — PROFİL VETOSU: kör sayı kesmesi YOK; yalnızca modelin somut gerekçeli [VETO: …]
    # etiketi. Sıradan düşüklük (referans eşik 50 altı) tek başına veto DEĞİL — sadece rapora not.
    if _veto_reason:
        recommendation = "Reddet"
        _set_result_meta(candidate_id, level,
                         result_reason=f"Profil vetosu — kurumsal ortamda çalışmaya engel olacak düzeyde ciddi olumsuz bulgu: {_veto_reason} (Öneri PUAN 1'e göre {normalize_recommendation(score_position)} olurdu; veto ile Reddet.)")
        record_system_decision(candidate_id, level, "veto",
                               f"PUAN 2 profil vetosu tetiklendi: {_veto_reason}",
                               {"score_position": score_position, "score_profile": score_profile,
                                "recommendation_before_veto": normalize_recommendation(score_position)})
    elif score_profile is not None and score_profile < 50:
        record_system_decision(candidate_id, level, "profil_dusuk_esik_alti",
                               f"PUAN 2 profil puanı ({score_profile}/100) referans eşik 50'nin altında ancak veto tetikleyecek ciddi bulgu YOK — karar PUAN 1'e göre veriliyor, düşüklük rapora not.",
                               {"score_position": score_position, "score_profile": score_profile})

    db = get_db()
    candidate = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    _iv_dates = db.execute("SELECT started_at, interview_ended_at, messages, reviewer_score_revision_json, reviewer_summary_tone FROM interviews WHERE candidate_id=? AND level=?",
                           (candidate_id, level)).fetchone()
    messages = get_interview_messages(db, candidate_id, level)
    try:
        _transcript_txt = transcript_to_text(build_transcript_view(
            _iv_dates["messages"] if _iv_dates else "[]", level, _iv_dates["started_at"] if _iv_dates else None))
    except Exception:
        _transcript_txt = ""
    report = report_match.group(1).strip() if report_match else ""
    if not report or len(strip_markdown(report)) < 60:
        report = build_fallback_report(dict(candidate) if candidate else {}, messages, score, recommendation, "AI rapor bloğu eksik/bozuk geldi")

    # KALEM 3: modalite veri kapsamı (kamera karesi sayısı/dağılımı + ses metrikleri var/yok)
    # rapora DETERMİNİSTİK olarak eklenir — model atlamış olsa bile görünür.
    try:
        _mcov_note = build_modality_coverage_note(candidate_id, level)
        if _mcov_note and "Modalite Veri Kapsamı" not in report:
            report = report.rstrip() + "\n" + _mcov_note + "\n"
    except Exception as e:
        print(f"UYARI (finalize_interview modalite notu c={candidate_id}): {type(e).__name__}: {e}")

    _cv_truncated = False
    if not standard_cv or len(strip_markdown(standard_cv)) < 30:
        # KALEM 3: "AI üretemedi" notu yerine aday alanlarından + CV metninden + transkriptten deterministik özet
        standard_cv = build_standard_cv_deterministic(dict(candidate) if candidate else {}, _transcript_txt)
        _cv_truncated = True
    # KALEM 2 — model/yedek CV özetinde boş kalan EĞİTİM/ÜNİVERSİTE/BÖLÜM/DENEYİM satırlarını
    # form alanı → CV/transkript sezgisel çıkarımı ile doldur.
    try:
        standard_cv = patch_standard_cv_blanks(standard_cv, dict(candidate) if candidate else {}, _transcript_txt)
    except Exception as e:
        print(f"UYARI (finalize_interview CV alan tamamlama c={candidate_id}): {type(e).__name__}: {e}")

    # KALEM 3 (2. tur) — çelişki tespiti DETERMİNİSTİK olarak rapora eklenir (model atlasa/yanlış
    # onaylasa bile görünür). Rapordaki "Tutarlılık / Çelişki" bölümü bunun ışığında okunmalı.
    try:
        _disc = compute_field_discrepancies(dict(candidate) if candidate else {}, _transcript_txt)
        _disc_txt = render_discrepancy_block(_disc, for_prompt=False)
        if _disc_txt and "Sistem Alan Karşılaştırması" not in report:
            report = report.rstrip() + "\n\n" + _disc_txt + "\n"
    except Exception as e:
        print(f"UYARI (finalize_interview çelişki bloğu c={candidate_id}): {type(e).__name__}: {e}")

    # GÖREV 1.2 — ikinci modelin puan revizyonu KALDIRILDIĞI için burada rapor metnine
    # "denetçi notu" enjeksiyonu da YAPILMAZ. (Eski annotate_revised_criteria_prose çağrısı silindi.)

    # GÖREV 2.2 — KARAR TEK KAYNAK: yalnız PUAN 1 + eşik tablosundan; GPT prose'una dokunulmaz.
    report = sync_recommendation_line(report, recommendation, score_position, score_profile,
                                     veto_reason=_veto_reason)
    # KALEM 4 — rapor metnindeki "Tarih:" satırı mülakatın GERÇEK tarihi olsun (üretim tarihi değil).
    try:
        _iv_date_str = (_parse_iso(_iv_dates["started_at"]).strftime("%d.%m.%Y")
                        if (_iv_dates and _iv_dates["started_at"] and _parse_iso(_iv_dates["started_at"])) else None)
        if _iv_date_str:
            report = sync_report_date_line(report, _iv_date_str)
    except Exception as e:
        print(f"UYARI (finalize_interview tarih satırı c={candidate_id}): {type(e).__name__}: {e}")
    # KALEM 5 — müşteri raporundan iç sistem satırlarını çıkar (halüsinasyon işaretleri +
    # token-kesilme teknik notu). Modele giden interviews.messages DEĞİŞMEZ.
    report = strip_report_system_lines(report)
    standard_cv = strip_report_system_lines(standard_cv)
    # KALEM 5 (bu tur) — içeriği "belirtilecek bir şey yok" olan opsiyonel bölümleri tamamen kaldır.
    try:
        report = strip_empty_report_sections(report)
    except Exception as e:
        print(f"UYARI (finalize_interview boş bölüm temizliği c={candidate_id}): {type(e).__name__}: {e}")
    # KALEM 5 — token kesilmesi olduysa teknik notu YÖNETİCİYE ayır (müşteri raporuna girmez).
    _tech_note = STANDARD_CV_TRUNCATION_NOTE if _cv_truncated else None

    if regen:
        # EK — geriye dönük yeniden üretim: orijinal bitiş saati (completed_at) KORUNUR,
        # rapor üretim zamanı ayrı alanda (report_regenerated_at). completed_at IS NULL guard'ı yok.
        db.execute("""
            UPDATE interviews SET report=?, standard_cv=?, score=?, score_position=?, score_profile=?, recommendation=?,
                   report_regenerated_at=CURRENT_TIMESTAMP,
                   report_tech_note=COALESCE(?, report_tech_note), processing_status='completed', processing_error=NULL
            WHERE candidate_id=? AND level=?
        """, (report, standard_cv, score, score_position, score_profile, recommendation, _tech_note, candidate_id, level))
        db.commit()
        db.close()
        record_system_decision(candidate_id, level, "rapor_yeniden_uretildi",
                               "Geriye dönük yeniden üretim tamamlandı; orijinal bitiş saati korundu.",
                               {"score": score, "score_position": score_position, "score_profile": score_profile,
                                "recommendation": recommendation, "veto": bool(_veto_reason)}, warnings=_score_warnings)
        _ensure_result_reason(candidate_id, level, score, recommendation, terminated_reason)
        return {"message": "Rapor yeniden üretildi.", "completed": True, "score": score, "recommendation": recommendation}

    # EŞZAMANLILIK GÜVENLİK AĞI: interviews.completed_at hâlâ NULL ise finalize et (WHERE koşulu
    # ile atomik). Eğer bu satır başka bir eşzamanlı çağrı tarafından zaten tamamlanmışsa
    # (rowcount=0), üzerine yazma ve tekrar e-posta gönderme — mevcut kayıtlı sonucu dön.
    # KALEM 4 — completed_at = mülakatın GERÇEK bitiş anı (interview_ended_at); rapor üretimi
    # arka planda saatler sonra bitse bile completed_at o zamanı yansıtmaz. report_generated_at
    # ayrı alan: raporun fiilen üretildiği an.
    cur = db.execute("""
        UPDATE interviews SET report=?, standard_cv=?, score=?, score_position=?, score_profile=?, recommendation=?,
               completed_at=COALESCE(interview_ended_at, CURRENT_TIMESTAMP), report_generated_at=CURRENT_TIMESTAMP,
               report_tech_note=COALESCE(?, report_tech_note), processing_status='completed', processing_error=NULL
        WHERE candidate_id=? AND level=? AND completed_at IS NULL
    """, (report, standard_cv, score, score_position, score_profile, recommendation, _tech_note, candidate_id, level))
    already_finalized = cur.rowcount == 0
    # candidates.status sadece adayın O AN İÇİN AKTİF OLDUĞU level tamamlandığında güncellenir
    # (adayın current level'ı değiştiyse, bu eski bir çağrı olabilir — dokunma).
    if not already_finalized and (candidate and (candidate["level"] or 1) == level):
        db.execute("""
            UPDATE candidates SET status='completed', completed_at=CURRENT_TIMESTAMP, terminated_reason=?
            WHERE id=?
        """, (terminated_reason, candidate_id))
    db.commit()

    if already_finalized:
        # Başka bir eşzamanlı istek bu mülakatı zaten sonuçlandırmış; gerçek kayıtlı sonucu dön.
        existing_interview = db.execute("SELECT * FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, level)).fetchone()
        db.close()
        return {
            "message": "Mülakat tamamlandı, teşekkür ederiz.",
            "completed": True,
            "score": existing_interview["score"] if existing_interview else score,
            "recommendation": existing_interview["recommendation"] if existing_interview else recommendation,
        }
    db.close()

    # BÖLÜM 3: terminated_reason varsa ve henüz olay kaydı yoksa yapılandırılmış bir kayıt bırak;
    # ardından gerekçesiz olumsuz sonuç yasağını uygula.
    if terminated_reason:
        _append_result_event(candidate_id, level, {
            "type": "termination", "subtype": "sonlandirma",
            "description": terminated_reason, "weight": "sonlandırma", "source": "system",
        }, skip_if_any=True)
    _ensure_result_reason(candidate_id, level, score, recommendation, terminated_reason)
    if _score_warnings:
        record_system_decision(candidate_id, level, "puanlama_duzeltildi",
                               "Rapor sonrası sunucu puanlama doğrulaması bir veya daha fazla düzeltme yaptı.",
                               {"final_score": score}, warnings=_score_warnings)

    if candidate:
        send_report_email(candidate["name"], candidate["position"], report, score, recommendation, standard_cv, terminated_reason)

    clean_reply = reply.replace("[MÜLAKATBİTTİ]", "").split("---RAPOR---")[0].strip()
    return {
        "message": clean_reply or "Mülakat tamamlandı, teşekkür ederiz.",
        "completed": True, "score": score, "recommendation": recommendation
    }

# ---- Violation handling (sekme değişimi vs.) ----
@app.post("/api/interview/violation")
def report_violation(data: ViolationReport, background_tasks: BackgroundTasks, payload=Depends(verify_token)):
    if payload.get("role") != "candidate":
        raise HTTPException(status_code=403, detail="Yetkisiz")

    db = get_db()
    candidate = db.execute("SELECT * FROM candidates WHERE id=?", (data.candidate_id,)).fetchone()
    if not candidate or candidate["status"] == "completed":
        db.close()
        return {"violation_count": 999, "terminated": False}

    new_count = candidate["violation_count"] + 1
    db.execute("UPDATE candidates SET violation_count=? WHERE id=?", (new_count, data.candidate_id))
    db.commit()
    db.close()

    candidate_level = candidate["level"] or 1
    elapsed_ms = _safe_int(data.elapsed_seconds) * 1000
    # "prolonged_absence": aday 2 dakikadan uzun süre dönmediyse, ihlal sayısı ne olursa olsun sonlandır.
    force_terminate = (new_count >= 3) or (data.violation_type == "prolonged_absence")
    base_desc = _VIOLATION_DESC.get(data.violation_type, f"Kural ihlali ({data.violation_type})")
    if data.detail:
        base_desc = f"{base_desc} — {data.detail[:300]}"

    # BÖLÜM 3.2: HER ihlal yapılandırılmış olarak kaydedilir (sadece 3.'sü değil).
    _append_result_event(data.candidate_id, candidate_level, {
        "type": "violation", "subtype": data.violation_type,
        "elapsed_ms": elapsed_ms, "elapsed_minute": round(_safe_int(data.elapsed_seconds) / 60, 1),
        "description": base_desc,
        "snapshot_id": _nearest_snapshot_id(data.candidate_id, elapsed_ms),
        "weight": "sonlandırma" if force_terminate else f"uyarı {new_count}/3",
        "source": "system",
    })

    if not force_terminate:
        return {"violation_count": new_count, "terminated": False}

    # ---- ZORLA SONLANDIRMA ----
    if data.violation_type == "prolonged_absence":
        terminated_reason = base_desc
    else:
        terminated_reason = f"{base_desc} ({new_count} kez tespit edildi)"
    result_reason = f"Mülakat, kural ihlali nedeniyle sonlandırıldı: {terminated_reason}. İhlal puanı düşürmez; yeterli veri toplanamayan kriterler 'değerlendirilmedi' işaretlenmiştir."
    _set_result_meta(data.candidate_id, candidate_level, partial=1, result_reason=result_reason)

    # L2/L3 (sesli): Claude KULLANILMAZ. Sunucu, o ana kadar senkronlanmış transkriptle mülakatı
    # HEMEN sonlandırır (WHERE completed_at IS NULL guard'lı) — böylece frontend submitReport'u
    # hiç ulaşmasa bile mülakat asla takılı kalmaz. Frontend yine submitReport('uygunsuz_davranis')
    # çağırırsa /api/realtime/report'un idempotency guard'ı devreye girer, çift finalize olmaz.
    if candidate_level in (2, 3):
        log_ai_provider(candidate_level, "claude", "blocked")
        report = (f"Aday: {candidate['name']}\nPozisyon: {candidate['position']}\n\nSONUÇ: DEĞERLENDİRİLEMEDİ\n\n"
                  f"{result_reason} Adayın söylemediği hiçbir bilgi eklenmemiştir.")
        try:
            _rv_minutes = max(1, get_effective_level_config(candidate_level, candidate["depth_tier"] if "depth_tier" in candidate.keys() else "standart")["minutes"])
            finalize_incomplete_interview(data.candidate_id, report, terminated_reason=terminated_reason,
                                          level=candidate_level, result_reason=result_reason,
                                          completion_pct=min(100, round(_safe_int(data.elapsed_seconds) / 60 / _rv_minutes * 100)))
        except Exception as e:
            print(f"HATA (report_violation sesli finalize c={data.candidate_id}): {type(e).__name__}: {e}")
        return {
            "violation_count": new_count, "terminated": True, "voice": True,
            "end_reason": "uygunsuz_davranis",
            "message": "Mülakat kuralları ihlal edildiği için süreç sonlandırılmıştır.",
            "score": None, "recommendation": "Değerlendirilemedi",
        }

    # L1 (metin): deferred AI raporu — ADİL değerlendirme (artık "düşük puan ver" YOK).
    try:
        system = get_system_prompt(candidate["position"], candidate["name"], candidate["cv_text"], candidate["ai_note"], candidate["education"], candidate["university"], candidate["department"], candidate["experience_years"], candidate_level, candidate["interview_language"] or "tr", candidate["report_language"] or "tr", (candidate["depth_tier"] if "depth_tier" in candidate.keys() else "standart") or "standart", email=(candidate["email"] if "email" in candidate.keys() else None))
        force_msg = (
            f"Mülakat, aday tarafında tespit edilen kural ihlali nedeniyle sonlandırıldı: {terminated_reason}. "
            "Şimdi bitir ve ELDEKİ veriyle ADİL bir rapor üret — ihlal TEK BAŞINA puanı düşürmez; yalnızca yeterli "
            "veri toplanamayan kriterler 'değerlendirilmedi' işaretlenir. 'Sonuç Gerekçesi' bölümüne ihlali SOMUT yaz: "
            "ne olduğu, mülakatın kaçıncı dakikası, transkriptteki ilgili söz. [MÜLAKATBİTTİ] etiketini kullan."
        )
        log_ai_provider(candidate_level, "claude", "analysis")
        _mark_finish_pending(data.candidate_id, candidate_level, provider="claude", model="claude-sonnet-4-6",
                              system=system, payload=force_msg, terminated_reason=terminated_reason, reason="violation")
        background_tasks.add_task(run_deferred_finish_job, data.candidate_id, candidate_level)
        return {
            "violation_count": new_count, "terminated": True,
            "message": "Mülakat kuralları ihlal edildiği için süreç sonlandırılmıştır. Raporunuz hazırlanıyor.",
            "processing": True, "score": None, "recommendation": None,
        }
    except Exception as e:
        print(f"HATA (report_violation, zorla sonlandırma): {type(e).__name__}: {e}")
        report = f"Aday: {candidate['name']}\nPozisyon: {candidate['position']}\n\nSONUÇ: DEĞERLENDİRİLEMEDİ\n\n{result_reason}"
        finalize_incomplete_interview(data.candidate_id, report, terminated_reason=terminated_reason, level=candidate_level,
                                      technical_error_ref=f"violation_finalize_fallback: {type(e).__name__}", result_reason=result_reason)
        return {
            "violation_count": new_count, "terminated": True,
            "message": "Mülakat kuralları ihlal edildiği için süreç sonlandırılmıştır.",
            "score": None, "recommendation": "Değerlendirilemedi",
        }

# ---- Kamera snapshot (4 sabit kare) ----
@app.post("/api/interview/snapshot")
def save_snapshot(data: SnapshotData, payload=Depends(verify_token), db=Depends(db_dep)):
    if payload.get("role") != "candidate":
        raise HTTPException(status_code=403, detail="Yetkisiz")

    # Basit boyut kontrolü (base64 ~1.3x büyür, 2MB ham görsele kabaca denk gelecek sınır)
    if len(data.image_base64) > 3_000_000:
        raise HTTPException(status_code=400, detail="Görsel çok büyük")

    reason = (data.reason or "").strip() or "auto"
    is_mimic = reason == "mimic_sample"
    # FAZ D: doğrulama kareleri (kamera kanıtı — PDF/panel) ile mimik analiz kareleri AYRI sayılır
    # ve AYRI üst sınıra tabidir; biri diğerinin kotasını yemez.
    if is_mimic:
        cap, count_filter = 24, "reason='mimic_sample'"
    else:
        cap, count_filter = 4, "(reason IS NULL OR reason<>'mimic_sample')"

    existing_count = db.execute(
        f"SELECT COUNT(*) as c FROM snapshots WHERE candidate_id=? AND {count_filter}", (data.candidate_id,)
    ).fetchone()["c"]

    if existing_count >= cap:
        return {"message": "Kare sınırına ulaşıldı, kaydedilmedi", "count": existing_count}

    db.execute(
        "INSERT INTO snapshots (candidate_id, image_base64, level, elapsed_ms, reason) VALUES (?, ?, ?, ?, ?)",
        (data.candidate_id, data.image_base64, data.level, data.elapsed_ms, reason)
    )
    db.commit()
    return {"message": "Kare kaydedildi", "count": existing_count + 1}

# ---- Sesli mod (OpenAI Whisper STT + TTS) ----
# Not: Bu, Claude'un mülakat mantığına DOKUNMAZ — sadece ses<->yazı katmanı.
# Aday konuşur -> Whisper yazıya çevirir -> yazı normal /api/interview/chat akışına gider (Claude).
# Claude'un cevabı -> OpenAI TTS ile sese çevrilir -> tarayıcıya dönülür.
# OPENAI_API_KEY ortam değişkeni tanımlı değilse bu endpoint'ler net bir hata döner,
# frontend bu durumda tarayıcı tabanlı (Web Speech API) sesli moda düşer.

class VoiceSpeakRequest(BaseModel):
    text: str
    language: Optional[str] = "tr"

OPENAI_TTS_VOICE_BY_LANG = {"tr": "alloy", "en": "alloy", "de": "alloy"}  # tek ses, dil metinden anlaşılıyor

@app.post("/api/candidate/voice-transcribe")
async def voice_transcribe(file: UploadFile = File(...), payload=Depends(verify_token)):
    if payload.get("role") != "candidate":
        raise HTTPException(status_code=403, detail="Yetkisiz")
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="Sesli mod (OpenAI) için OPENAI_API_KEY tanımlı değil.")

    candidate_id = payload["candidate_id"]
    db = get_db()
    candidate = db.execute("SELECT interview_language, level FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    db.close()
    lang = (candidate["interview_language"] if candidate else None) or "tr"
    _cand_level = (candidate["level"] if candidate else None) or 1

    try:
        audio_bytes = await file.read()
        if len(audio_bytes) > 15_000_000:
            raise HTTPException(status_code=400, detail="Ses kaydı çok büyük")
        resp = await asyncio.to_thread(
            openai_call, "POST", "https://api.openai.com/v1/audio/transcriptions",
            files={"file": (file.filename or "audio.webm", audio_bytes, file.content_type or "audio/webm")},
            data={"model": "whisper-1", "language": lang, "response_format": "verbose_json"},
            timeout=30.0, step="voice_transcribe", severity="user", retry=True,
            context={"candidate_id": candidate_id},
        )
        result = resp.json()
        # MADDE 5 — Whisper maliyeti görünürlüğü: verbose_json 'duration' (saniye) alanını verir.
        try:
            _dur = float(result.get("duration") or 0)
            if _dur > 0:
                record_flat_usage(candidate_id, _cand_level, "openai", "whisper-1", "voice_transcribe",
                                  minutes=_dur / 60.0, raw={"duration_seconds": round(_dur, 1)})
        except Exception as _e:
            print(f"UYARI (voice_transcribe maliyet kaydı c={candidate_id}): {type(_e).__name__}: {_e}")
        return {"text": (result.get("text") or "").strip()}
    except AIError as e:
        raise ai_http_exception(e)
    except HTTPException:
        raise
    except Exception as e:
        print(f"HATA (voice_transcribe, beklenmeyen): {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail={
            "message": AI_ERROR_USER_MESSAGES["unknown"], "error_class": "unknown", "retryable": False,
        })

@app.post("/api/candidate/voice-speak")
async def voice_speak(data: VoiceSpeakRequest, payload=Depends(verify_token)):
    if payload.get("role") != "candidate":
        raise HTTPException(status_code=403, detail="Yetkisiz")
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="Sesli mod (OpenAI) için OPENAI_API_KEY tanımlı değil.")
    if not data.text or not data.text.strip():
        raise HTTPException(status_code=400, detail="Okunacak metin boş")

    voice = OPENAI_TTS_VOICE_BY_LANG.get(data.language or "tr", "alloy")
    _tts_text = data.text[:3000]
    try:
        resp = await asyncio.to_thread(
            openai_call, "POST", "https://api.openai.com/v1/audio/speech",
            json_body={"model": "tts-1", "voice": voice, "input": _tts_text, "response_format": "mp3"},
            timeout=30.0, step="voice_speak", severity="user", retry=True,
            context={"candidate_id": payload.get("candidate_id")},
        )
        # MADDE 5 — TTS maliyeti görünürlüğü: karakter bazlı ($15 / 1M karakter).
        try:
            _cid = payload.get("candidate_id")
            _lvl = None
            if _cid:
                _db = get_db()
                _cr = _db.execute("SELECT level FROM candidates WHERE id=?", (_cid,)).fetchone()
                _db.close()
                _lvl = (_cr["level"] if _cr else None) or 1
            record_flat_usage(_cid, _lvl, "openai", "tts-1", "voice_speak", chars=len(_tts_text),
                              raw={"chars": len(_tts_text)})
        except Exception as _e:
            print(f"UYARI (voice_speak maliyet kaydı): {type(_e).__name__}: {_e}")
        return StreamingResponse(io.BytesIO(resp.content), media_type="audio/mpeg")
    except AIError as e:
        raise ai_http_exception(e)
    except HTTPException:
        raise
    except Exception as e:
        print(f"HATA (voice_speak, beklenmeyen): {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail={
            "message": AI_ERROR_USER_MESSAGES["unknown"], "error_class": "unknown", "retryable": False,
        })

# ---- L2: OpenAI Realtime (canlı sesli mülakat) ----
# GÖREV DOKÜMANI KURALI: L2'de Claude KESİNLİKLE kullanılmaz. Sadece OpenAI Realtime
# (canlı ses<->ses) + rapor için OpenAI metin modeli. Bu iki endpoint dışındaki hiçbir
# L2 akışı Anthropic'e dokunmaz (yukarıdaki start_interview/interview_chat/report_violation
# içindeki L2 blokları bunu garanti eder).

class RealtimeReportRequest(BaseModel):
    candidate_id: int
    transcript: str
    duration_seconds: int = 0
    answered_count: int = 0
    end_reason: str = "tamamlandı"  # tamamlandı | aday_talebi | baglanti_koptu | uygunsuz_davranis
    criteria_coverage: Optional[dict] = None  # A4: modelin end_interview'da bildirdiği {kriter_adı: 0-100} kapsanma yüzdeleri
    realtime_usage: Optional[dict] = None  # Frontend'in response.done eventlerinden topladığı token/audio usage özeti
    events: Optional[List[dict]] = None  # Faz D1: son heartbeat'ten bu yana biriken ham Realtime olayları (bkz. record_realtime_events)

class RealtimeSyncRequest(BaseModel):
    """Görüşme SÜRERKEN periyodik olarak ve sekme kapanırken (sendBeacon ile) gönderilen
    ara kayıt. Amaç: submitReport() hiç tetiklenmeden (sekme kapanma/bağlantı kopması/AI
    end_interview'i hiç çağırmama gibi durumlarda) transkript ve o ana kadarki token
    kullanımının TAMAMEN kaybolmasını önlemek. Rapor ÜRETMEZ, sadece kaydeder."""
    candidate_id: int
    transcript: str = ""
    duration_seconds: int = 0
    answered_count: int = 0
    usage_delta: Optional[dict] = None  # son sync'ten bu yana biriken usage farkı (kümülatif değil)
    token: Optional[str] = None  # sendBeacon Authorization header gönderemediği için yedek yol
    events: Optional[List[dict]] = None  # Faz D1: son sync'ten bu yana biriken ham Realtime olayları (bkz. record_realtime_events)

MIN_L2_DURATION_SECONDS = 90  # Güvenilir rapor için asgari görüşme süresi
MIN_L2_ANSWERED_COUNT = 3  # En az üç gerçek aday cevabı olmadan puan/ret üretme

# MADDE 1 — YANIT BAŞINA TOKEN TAVANI. Realtime session'da mülakatçının TEK yanıtta üretebileceği
# çıktı (metin + ses) üst sınırı. Ölçülmüş referans: 12 dk L3'te ~10.000 ses-çıkışı token'ı;
# mülakatçı turu başına ortalama ~700-1000, sözlü örnekli en zengin meşru tur ~1500-2000 token.
# 4096 = normal turun ~4 katı, zengin turun ~2 katı üstünde — meşru hiçbir tur buna değmez,
# kesilme riski YOK. Yalnızca anormal uzun monologları (bozuk model / 3-8k token) tavanlar.
# Gerçek transkript ölçümü yapılamadı (yerel DB'de kayıtlı mülakat yok); değer bilinçli olarak
# yüksek seçildi çünkü kesilme = aday bozuk cümle duyar = KABUL EDİLEMEZ.
REALTIME_MAX_RESPONSE_TOKENS = 4096

def realtime_safe_limit_seconds(target_seconds: int) -> int:
    """MADDE 3 — her seviye/derinlik için sesli mülakat üst süre sınırı. Hedef sürenin 1.5 katı
    (doğal akışı kesmez), 12 dk alt ve 55 dk üst sınırla (OpenAI Realtime platform tavanı 60 dk).
    Sınıra ulaşınca frontend mülakatçıya doğal kapanış talimatı verir (ani kesme YOK), gecikmeli
    zorla bitiş devreye girer — mevcut L3 mekanizmasının aynısı, artık L2'de de aktif."""
    return int(min(55 * 60, max(12 * 60, round((target_seconds or 0) * 1.5))))

@app.post("/api/realtime/session")
async def create_realtime_session(payload=Depends(verify_token)):
    if payload.get("role") != "candidate":
        raise HTTPException(status_code=403, detail="Yetkisiz")
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="Sesli mülakat (OpenAI Realtime) için OPENAI_API_KEY tanımlı değil.")

    candidate_id = payload["candidate_id"]
    db = get_db()
    candidate = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    db.close()
    if not candidate:
        raise HTTPException(status_code=404, detail="Aday kaydı bulunamadı")
    candidate_level = candidate["level"] or 1
    if candidate_level not in (2, 3):
        raise HTTPException(status_code=400, detail="Bu uç nokta Level 2 ve Level 3 adaylar için geçerlidir.")
    if not (candidate["cv_text"] and len(candidate["cv_text"].strip()) > 20):
        raise HTTPException(status_code=400, detail="Bu seviyedeki mülakata başlamadan önce CV yüklemeniz gerekiyor.")

    depth_tier = (candidate["depth_tier"] if "depth_tier" in candidate.keys() else "standart") or "standart"
    depth_cfg = get_effective_level_config(candidate_level, depth_tier)

    # BUG FIX (started_at): interviews satırı önceden sadece ilk heartbeat (/api/realtime/sync,
    # 25sn'de bir) ya da hiç heartbeat gelmezse finalize (/api/realtime/report) anında oluşuyordu.
    # started_at kolonu DEFAULT CURRENT_TIMESTAMP olduğu için satır geç oluşursa gerçek mülakat
    # süresi (dakikalar) kayboluyor, DB'de birkaç saniyeymiş gibi görünüyordu. Artık oturum
    # (WebRTC bağlantısı) kurulur kurulmaz satır burada, gerçek başlangıç anında oluşturuluyor.
    db2 = get_db()
    existing_interview = db2.execute(
        "SELECT completed_at FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, candidate_level)
    ).fetchone()
    if not existing_interview:
        db2.execute(
            "INSERT INTO interviews (candidate_id, level, messages, depth_tier) VALUES (?, ?, '[]', ?)",
            (candidate_id, candidate_level, depth_tier)
        )
        # Teşebbüs sayacı: yeni oturum = bir başlatma denemesi (Mülakat Denemeleri ekranı).
        db2.execute(
            "UPDATE candidates SET interview_start_count = COALESCE(interview_start_count, 0) + 1, last_start_at = ? WHERE id = ?",
            (_now_ts(), candidate_id)
        )
        db2.commit()
    elif not existing_interview["completed_at"]:
        # Satır zaten var ama tamamlanmamış (örn. sayfa yenilendi, yeniden bağlanıldı) —
        # started_at'i EZME; ilk gerçek başlangıç zaten kayıtlı kalsın.
        # Yine de yeniden bağlanma denemesinin zamanını izle.
        db2.execute("UPDATE candidates SET last_start_at = ? WHERE id = ?", (_now_ts(), candidate_id))
        db2.commit()
    db2.close()

    pos_for_criteria = get_position(candidate["position"]) or {"criteria": [{"name": "Genel Yetkinlik", "weight": 100, "desc": ""}]}
    criteria_names_list = [c["name"] for c in pos_for_criteria["criteria"]]

    instructions = build_l2_realtime_instructions(
        candidate["position"], candidate["name"], candidate["cv_text"], candidate["ai_note"], candidate["interview_language"] or "tr", depth_tier, level=candidate_level
    )

    realtime_model = get_realtime_model(candidate_level)
    print(f"[REALTIME_MODEL] candidate_id={candidate_id} level=L{candidate_level} model={realtime_model}")
    session_body = {
        "session": {
            "type": "realtime",
            "model": realtime_model,
            "instructions": instructions,
            # MADDE 1 — mülakatçının tek yanıtta üretebileceği çıktı (metin+ses) tavanı; anormal
            # uzun monologları keser, normal/örnekli tur çok altında kalır (bkz. sabit tanımı).
            "max_response_output_tokens": REALTIME_MAX_RESPONSE_TOKENS,
            "audio": {
                "output": {"voice": OPENAI_REALTIME_VOICE},
                "input": {
                    "transcription": {"model": "whisper-1"},
                    "turn_detection": {
                        # v34 DOĞAL KONUŞMA SIRASI: server_vad sabit bir sessizlik süresine (650ms)
                        # dayanıyordu — hızlı konuşan adayla yavaş/düşünerek konuşan adayı aynı
                        # sabit süreyle değerlendiriyordu, doğal duraksamaları kesme riski taşıyordu.
                        # semantic_vad, OpenAI Realtime API'nin resmi olarak desteklediği bir mod
                        # (bkz. platform.openai.com/docs/guides/realtime-vad): sabit süre yerine
                        # adayın söylediklerinin anlamına bakıp cümlesini bitirip bitirmediğine karar
                        # veriyor ("ummm" ile biten bir cümlede daha uzun bekliyor, net biten bir
                        # cümlede hızlı yanıt veriyor). eagerness="auto" OpenAI'nin kendi varsayılanı
                        # (medium'a eşdeğer) — aşırı agresif/aşırı yavaş bir değer tahmin etmiyoruz.
                        "type": "semantic_vad",
                        "eagerness": "high",
                        "create_response": True,
                        "interrupt_response": False
                    }
                }
            },
            "tools": [{
                "type": "function",
                "name": "end_interview",
                "description": f"Aday bitirirse, uygunsuz davranış tekrarlanırsa veya kriterlerin çoğu yaklaşık %{depth_cfg['coverage_threshold']} kanıt düzeyine ulaşırsa çağır. Her kriter için 0-100 coverage yaz.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string", "enum": ["tamamlandı", "aday_talebi", "uygunsuz_davranis"]},
                        "criteria_coverage": {
                            "type": "object",
                            "description": "Her kriter adı için 0-100 arası tahmini kapsanma/netlik yüzdesi.",
                            "properties": {name: {"type": "integer"} for name in criteria_names_list}
                        }
                    },
                    "required": ["reason"]
                }
            }, {
                # FAZ D — SES GÖZLEMİ: model, adayın sesinde belirgin bir şey fark ettiğinde
                # bunu YAPILANDIRILMIŞ olarak kaydeder. Ham ses hiçbir yere gönderilmez; sadece
                # modelin kendi gözlemi. Yanıta bağlanmaz (frontend function_call_output/response
                # göndermez) — akış kesilmez.
                "type": "function",
                "name": "note_voice_observation",
                "description": (
                    "SES gözlemi kaydet. YALNIZCA belirgin ve rapora değer bir gözlemde çağır "
                    "(net tereddüt, akıcılık kaybı, tonda belirgin kayma, aşırı güven/gerginlik). "
                    "Sıradan/beklenen konuşma için ASLA çağırma. Tüm görüşmede EN FAZLA 5 kez. "
                    "Bu araç sesli yanıt üretmez; sessizce kaydet ve mülakata devam et."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ton": {"type": "string", "enum": ["sakin", "gergin", "kararsiz", "kendinden_emin", "monoton", "istekli"]},
                        "akicilik": {"type": "integer", "description": "0-100: konuşma akıcılığı"},
                        "tereddut": {"type": "integer", "description": "0-100: duraksama/tereddüt düzeyi"},
                        "gozlem": {"type": "string", "description": "Tek cümle, somut ve tarafsız."}
                    },
                    "required": ["gozlem"]
                }
            }]
        }
    }

    err_ctx = {"candidate_id": candidate_id, "candidate_name": candidate["name"], "level": candidate_level}
    try:
        resp = await asyncio.to_thread(
            openai_call, "POST", "https://api.openai.com/v1/realtime/client_secrets",
            json_body=session_body, timeout=20.0, step="realtime_session",
            context=err_ctx, severity="user", retry=True,
        )
        result = resp.json()
        log_ai_provider(candidate_level, "openai", "realtime_session")
        return {
            "client_secret": result.get("value"),
            "model": realtime_model,
            # Frontend'in canlı maliyet tahmini bu tabloyu okur — fiyat sadece AI_PRICING_PER_1M'de
            # değişir, frontend'de ayrı bir kopya tutulmaz.
            "pricing": AI_PRICING_PER_1M.get(("openai", realtime_model), {}),
            "turn_detection": session_body["session"]["audio"]["input"]["turn_detection"],
            "depth_tier": depth_tier,
            "coverage_threshold": depth_cfg["coverage_threshold"],
            "criteria_names": criteria_names_list,
            # FAZ D: mimik kare örneklemesi bu planlanan süreye eşit dağıtılır (aralik = target_seconds/24).
            "target_seconds": depth_cfg["minutes"] * 60,
            # MADDE 3: sesli mülakat üst süre sınırı (L2 + L3). Frontend bu değere ulaşınca AI'a
            # doğal kapanış talimatı verir; gecikmeli zorla bitiş devreye girer.
            "safe_limit_seconds": realtime_safe_limit_seconds(depth_cfg["minutes"] * 60),
        }
    except AIError as e:
        raise ai_http_exception(e)
    except HTTPException:
        raise
    except Exception as e:
        print(f"HATA (create_realtime_session, beklenmeyen): {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail={
            "message": AI_ERROR_USER_MESSAGES["unknown"], "error_class": "unknown", "retryable": False,
        })


def build_l2_short_report(candidate_name: str, position_name: str, reason: str) -> str:
    """Minimum tamamlanma şartı sağlanmadığında (yarım mülakat / veri yetersizliği)
    OpenAI'a HİÇ istek atmadan, ücretsiz bir şablon raporla direkt döner."""
    return f"""[MÜLAKATBİTTİ]
---RAPOR---
**Aday:** {candidate_name}
**Pozisyon:** {position_name}
**Tarih:** {datetime.now().strftime('%d.%m.%Y')}

**TOPLAM PUAN: Değerlendirilemedi**

{reason}

**Öneri:** Değerlendirilemedi
---RAPORSON---

---STANDARTCV---
**AD SOYAD:** {candidate_name}
**POZİSYON:** {position_name}
**MÜLAKAT NOTU:** {reason}
---STANDARTCVSON---"""

@app.post("/api/realtime/sync")
async def sync_realtime_progress(data: RealtimeSyncRequest, request: Request):
    """Görüşme sürerken periyodik (frontend'de ~25sn'de bir) ve sekme kapanırken
    (navigator.sendBeacon ile, Authorization header'ı yollayamadığı için data.token ile) çağrılır.
    Amaç: submitReport() hiçbir sebeple tetiklenmezse bile (sekme kapandı, bağlantı koptu,
    AI end_interview'i hiç çağırmadı) transkript ve o ana kadarki gerçek token kullanımının
    TAMAMEN kaybolmasını önlemek. Rapor ÜRETMEZ, OpenAI'a gitmez — sadece ucuz bir DB yazımıdır."""
    # sendBeacon Authorization header koyamadığı için: önce normal header'ı dene, yoksa body'deki token'a düş.
    auth_header = request.headers.get("authorization") or request.headers.get("Authorization")
    raw_token = None
    if auth_header and auth_header.lower().startswith("bearer "):
        raw_token = auth_header.split(" ", 1)[1]
    elif data.token:
        raw_token = data.token
    if not raw_token:
        raise HTTPException(status_code=401, detail="Token eksik")
    try:
        payload = jwt.decode(raw_token, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        raise HTTPException(status_code=401, detail="Geçersiz token")
    if payload.get("role") != "candidate":
        raise HTTPException(status_code=403, detail="Yetkisiz")

    effective_candidate_id = int(payload.get("candidate_id") or data.candidate_id)

    db = get_db()
    candidate = db.execute("SELECT * FROM candidates WHERE id=?", (effective_candidate_id,)).fetchone()
    candidate_level = (candidate["level"] or 1) if candidate else None
    if not candidate or candidate_level not in (2, 3):
        db.close()
        return {"ok": False}
    interview = db.execute("SELECT completed_at FROM interviews WHERE candidate_id=? AND level=?", (effective_candidate_id, candidate_level)).fetchone()
    if interview and interview["completed_at"]:
        # Zaten finalize edilmiş bir görüşmeye geç kalan bir heartbeat gelmiş olabilir; sessizce yoksay.
        db.close()
        return {"ok": True, "already_completed": True}
    if not interview:
        db.execute("INSERT INTO interviews (candidate_id, level, messages) VALUES (?, ?, '[]')", (effective_candidate_id, candidate_level))
        db.commit()
    db.close()

    if data.transcript:
        db2 = get_db()
        save_interview_state(db2, effective_candidate_id, [{"role": "user", "content": data.transcript}], candidate_level)
        db2.commit(); db2.close()

    if data.usage_delta:
        record_realtime_usage_summary(effective_candidate_id, candidate_level, get_realtime_model(candidate_level), data.usage_delta, action="realtime_heartbeat")

    record_realtime_events(effective_candidate_id, candidate_level, data.events)

    return {"ok": True}

def get_interview_usage_cost(candidate_id: int, level: int = 2) -> float:
    db = None
    try:
        db = get_db()
        row = db.execute("SELECT COALESCE(SUM(estimated_cost_usd),0) AS total FROM ai_usage_logs WHERE candidate_id=? AND level=?", (candidate_id, level)).fetchone()
        return float(row["total"] or 0)
    except Exception as e:
        # Sessizce 0.0 dönmek admin paneline "$0 harcandı" gibi yanlış bir bilgi verebilir —
        # en azından logluyoruz ki gerçek bir DB hatası fark edilebilsin.
        print(f"UYARI (get_interview_usage_cost candidate_id={candidate_id} level={level}): {type(e).__name__}: {e}")
        return 0.0
    finally:
        if db:
            db.close()


def finalize_incomplete_interview(candidate_id: int, report: str, terminated_reason: Optional[str] = None, level: int = 2,
                                   technical_error_ref: Optional[str] = None, completion_pct: Optional[int] = None,
                                   result_reason: Optional[str] = None):
    """Teknik/erken biten görüşmede sahte 0 puan ve Reddet üretmez. BÖLÜM 3: kısmi işareti +
    teknik sebep + tamamlanma oranı + konsolide gerekçe kaydedilir."""
    db = get_db()
    candidate = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    standard_cv = f"AD SOYAD: {candidate['name'] if candidate else '-'}\nPOZİSYON: {candidate['position'] if candidate else '-'}\nMÜLAKAT NOTU: Görüşme tamamlanamadığı için puanlama yapılmadı."
    db.execute("""
        UPDATE interviews SET report=?, standard_cv=?, score=NULL, recommendation='Değerlendirilemedi',
               completed_at=COALESCE(interview_ended_at, CURRENT_TIMESTAMP),
               processing_status='completed', processing_error=NULL, partial=1,
               completion_pct=COALESCE(?, completion_pct), technical_error_ref=COALESCE(?, technical_error_ref),
               result_reason=COALESCE(result_reason, ?)
        WHERE candidate_id=? AND level=? AND completed_at IS NULL
    """, (report, standard_cv, completion_pct, technical_error_ref, result_reason or terminated_reason, candidate_id, level))
    if candidate and (candidate["level"] or 1) == level:
        db.execute("UPDATE candidates SET status='completed', completed_at=CURRENT_TIMESTAMP, terminated_reason=? WHERE id=?", (terminated_reason, candidate_id))
    db.commit(); db.close()
    _ensure_result_reason(candidate_id, level, None, "Değerlendirilemedi", terminated_reason)
    if candidate:
        send_report_email(candidate["name"], candidate["position"], report, None, "Değerlendirilemedi", standard_cv, terminated_reason)
    return {"message": "Mülakat tamamlandı. Yeterli veri oluşmadığı için puanlama yapılmadı.", "completed": True, "score": None, "recommendation": "Değerlendirilemedi"}


# ═══ BÖLÜM A/B/C/D — Sesli rapor kararı: veri yeterliliği + end_reason doğrulama + kapsama + karar kaydı ═══

# Açık bitirme niyeti kalıpları (interview_chat GÖREV satırındaki örnek listeyle aynı kaynak).
_EXIT_INTENT_RE = re.compile(
    r"(bitir(elim|ebilir\s*miy[ıi]z|mek\s+istiyorum)|sonland[ıi]r|burada\s+b[ıi]rak|devam\s+etmek\s+istemiyorum|"
    r"görüşmek\s+istemiyorum|art[ıi]k\s+devam\s+etme|mülakat[ıi]\s+(kesel|b[ıi]rak)|"
    r"[İi]K\s*(ile|['’]?yle)?\s+(görüş|konuş)|yönetim(le|e)\s+(ilet|görüş|konuş)|şikayet\s+ede|"
    r"vazge[çc]|çıkmak\s+istiyorum|katılmak\s+istemiyorum|iptal\s+ed)",
    re.IGNORECASE,
)

def _candidate_lines_tail(transcript: str, max_chars: int = 1400) -> str:
    """Transkriptin sonundaki ADAY satırlarını döndürür ('[mm:ss] Aday: ...' veya 'Aday: ...')."""
    lines = [l for l in (transcript or "").splitlines() if re.search(r"(^|\])\s*Aday\s*:", l)]
    return ("\n".join(lines[-8:]))[-max_chars:]

def validate_end_reason(raw_reason: str, transcript: str):
    """A5: frontend'den gelen end_reason körü körüne kabul edilmez. 'aday_talebi' geldiyse ama
    transkriptin son aday sözlerinde açık bitirme niyeti YOKSA 'tamamlandı'ya düşürülür.
    Dönüş: (effective_reason, downgraded_bool)."""
    raw = (raw_reason or "tamamlandı").strip()
    if raw != "aday_talebi":
        return raw, False
    if _EXIT_INTENT_RE.search(_candidate_lines_tail(transcript)):
        return "aday_talebi", False
    return "tamamlandı", True

def assess_data_sufficiency(answered_count, min_q, criteria_coverage, criteria, coverage_floor: int = 40) -> dict:
    """A1: rapor üretilecek kadar veri toplandı mı? answered_count ile criteria_coverage BİRLİKTE.
    Yeterli: (>= min_q aday cevabı) VEYA (kriterlerin en az yarısı coverage_floor üstünde);
    her koşulda en az MIN_L2_ANSWERED_COUNT gerçek cevap şart."""
    ac = _safe_int(answered_count)
    mq = max(1, _safe_int(min_q))
    names = [c.get("name") for c in (criteria or []) if c.get("name")]
    covered = 0
    if isinstance(criteria_coverage, dict) and names:
        for n in names:
            try:
                if float(criteria_coverage.get(n, 0) or 0) >= coverage_floor:
                    covered += 1
            except Exception:
                pass
    enough_answers = ac >= mq
    enough_coverage = bool(names) and covered >= (len(names) + 1) // 2
    hard_minimum = ac >= MIN_L2_ANSWERED_COUNT
    return {
        "sufficient": bool(hard_minimum and (enough_answers or enough_coverage)),
        "answered_count": ac, "min_q": mq,
        "criteria_covered": covered, "criteria_total": len(names),
        "enough_answers": enough_answers, "enough_coverage": enough_coverage,
        "hard_minimum_met": hard_minimum,
    }

def build_unanswered_criteria(criteria, criteria_coverage, threshold) -> list:
    """B2/C: kapsanma eşiğinin altında kalan (yeterince sorulamamış/yanıtlanamamış) kriterler."""
    if not isinstance(criteria_coverage, dict):
        return []
    out = []
    for c in (criteria or []):
        n = c.get("name")
        try:
            v = float(criteria_coverage.get(n, 0) or 0)
        except Exception:
            v = 0
        if v < threshold:
            out.append(f"{n} (~%{int(v)})")
    return out

def record_system_decision(candidate_id: int, level: int, decision: str, reason: str, meta=None, warnings=None) -> None:
    """D1: sistemin rapor kararını + gerekçesini kaydeder (admin panelde 'neden rapor üretildi/üretilmedi').
    warnings verilirse mevcut warnings listesine EKLENİR (rapor sonrası doğrulama uyarıları kaybolmasın)."""
    try:
        db = get_db()
        old = {}
        try:
            row = db.execute("SELECT system_decision_json FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, level)).fetchone()
            if row and row["system_decision_json"]:
                old = json.loads(row["system_decision_json"]) or {}
        except Exception:
            old = {}
        payload = {"decision": decision, "reason": reason, "at": _now_ts(), "meta": meta or {}}
        merged_warnings = list(old.get("warnings") or [])
        for w in (warnings or []):
            if w not in merged_warnings:
                merged_warnings.append(w)
        if merged_warnings:
            payload["warnings"] = merged_warnings[-20:]
        db.execute("UPDATE interviews SET system_decision_json=? WHERE candidate_id=? AND level=?",
                   (json.dumps(payload, ensure_ascii=False)[:8000], candidate_id, level))
        db.commit(); db.close()
    except Exception as e:
        print(f"UYARI (record_system_decision c={candidate_id} L{level}): {type(e).__name__}: {e}")

# ═══ KALEM 1 — Whisper halüsinasyon filtresi (SUNUCU tarafı; frontend RealtimeInterview.js:isLikelyHallucination karşılığı) ═══
# frontend RealtimeInterview.js:_HALLUCINATION_PHRASES ile AYNI liste tutulmalı.
_HALLUCINATION_PHRASES = {
    "bye", "bye bye", "bye-bye", "goodbye", "good bye", "thank you", "thanks", "thank you.",
    "thank you very much", "thank you so much", "you", "you.", "mm-hmm", "mmhmm", "mm hmm",
    "mhm", "uh-huh", "okay", "ok", "o.k.", "switch", "switch.", "uh", "um", "hmm", "hm",
    "yeah", "yep", "yes", "see you", "see you later", "thanks for watching", "please subscribe",
    "amara.org", "altyazı m.k.", "i'm sorry", "sorry", "the end", "okay.", "so", "right",
}

def is_likely_hallucination(text: str, lang: str = "tr") -> bool:
    """Sesli mülakatta sessizlik/gürültü anlarında transkripsiyon modelinin uydurduğu İngilizce
    dolgu ("thank you", "bye", "you", "switch"). TR oturumunda: bilinen kalıp; <2 harf; ASCII-only
    <=2 kelime & <=6 harf; VEYA cümle bilinen kalıpların TEKRARINDAN ibaretse ("thank you. thank
    you.", "bye bye bye") — kelime sayısına bakılmaksızın → halüsinasyon."""
    raw = (text or "").strip()
    if not raw:
        return True
    letters = re.sub(r"[^\w]", "", raw, flags=re.UNICODE)
    if len(letters) < 2:
        return True
    norm = re.sub(r"[.!?,…\"'’]+$", "", raw.lower()).strip()
    norm = re.sub(r"\s+", " ", norm)
    if not (lang or "tr").lower().startswith("tr"):
        return False
    if norm in _HALLUCINATION_PHRASES:
        return True
    words = [w for w in norm.split(" ") if w]
    ascii_only = re.fullmatch(r"[a-z0-9\s'.\-]+", norm) is not None
    if ascii_only and len(words) <= 2 and len(letters) <= 6:
        return True
    # KALEM 3: cümle, bilinen halüsinasyon kalıplarının tekrarından ibaret mi?
    # Noktalama ile parçalara ayır; her parça (boşluk normalize) bilinen bir kalıpsa → halüsinasyon.
    if ascii_only:
        chunks = [c.strip() for c in re.split(r"[.!?,;]+", norm) if c.strip()]
        if chunks and all(re.sub(r"\s+", " ", c) in _HALLUCINATION_PHRASES for c in chunks):
            return True
        # Aynı kısa kalıbın ardışık tekrarı (nokta olmadan): "bye bye bye", "thank you thank you"
        for ph in _HALLUCINATION_PHRASES:
            if " " not in ph and len(ph) >= 2:
                if re.fullmatch(rf"(?:{re.escape(ph)}\s*){{2,}}", norm):
                    return True
        for ph in ("thank you", "see you", "bye bye"):
            if re.fullmatch(rf"(?:{re.escape(ph)}\s*){{2,}}", norm):
                return True
    return False

def filter_transcript_hallucinations(transcript_text: str, lang: str = "tr"):
    """Aday satırlarını halüsinasyon filtresinden geçirir. Filtrelenen satırlar SİLİNMEZ —
    yerinde işaretlenir (model 'aday cevabı' saymasın, admin de görsün).
    Dönüş: (isaretli_transkript, [{ts, text}], filtrelenen_aday_satir_sayisi)."""
    if not transcript_text:
        return transcript_text or "", [], 0
    out_lines, filtered = [], []
    for line in transcript_text.splitlines():
        m = _VOICE_LINE_RE.match(line)
        role = spoken = ts = None
        if m:
            ts, role, spoken = f"{m.group(1)}:{m.group(2)}", m.group(3), m.group(4)
        elif line.strip().startswith("Aday:"):
            role, spoken, ts = "Aday", line.split(":", 1)[1].strip(), ""
        if role and role.startswith("Ada") and spoken is not None and is_likely_hallucination(spoken, lang):
            filtered.append({"ts": ts or "", "text": spoken.strip()[:200]})
            prefix = f"[{ts}] " if ts else ""
            out_lines.append(f'{prefix}Aday: [SİSTEM: bu satır olası transkripsiyon halüsinasyonudur — ADAY CEVABI SAYMA] "{spoken.strip()}"')
        else:
            out_lines.append(line)
    return "\n".join(out_lines), filtered, len(filtered)

# ═══ KALEM 2 — Puanlama matematiği doğrulaması (kriter tavanı + normalize) ═══
def _norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\wçğıöşü ]", "", (s or "").lower())).strip()

def _name_score(crit_name: str, cell_name: str) -> float:
    """0..1 — kriter adı ile tablo hücresi adı örtüşme derecesi. Alt-string çapraz eşleşmesini
    (ör. 'Uyum' ↔ 'Deneyim Uyumu') kelime bazlı skorla eleyip en iyisini seçmek için."""
    a, b = _norm_name(crit_name), _norm_name(cell_name)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    aw, bw = set(a.split()), set(b.split())
    if not aw or not bw:
        return 0.0
    inter = len(aw & bw)
    jacc = inter / len(aw | bw)
    # tam kelime kapsanması (kriter adının tüm kelimeleri hücrede) küçük bonus
    if aw <= bw or bw <= aw:
        jacc += 0.15
    return min(1.0, jacc)

_SCORE_FIXED_MARK = "(ham puan:"

# Aday cevabının GEÇERLİ (özlü) sayılıp sayılmadığı: boş / halüsinasyon / "anlamadım-tekrar" değilse geçerli.
_NONANSWER_RE = re.compile(
    r"^\s*(anlamad|anlayamad|tekrar\s+ed|tekrar\s+eder\s*mis|pardon|duyamad|efendim|bilmiyorum|"
    r"fikrim\s+yok|geçelim|geç(ebilir|ebilir\s*miy)|bir\s+sonrakine|hat[ıi]rlam[ıi]yorum)", re.IGNORECASE)

def _is_substantive_answer(txt: str) -> bool:
    t = (txt or "").strip()
    if len(re.sub(r"\s+", "", t)) < 12:
        return False
    if is_hallucination_marker_line(t) or is_likely_hallucination(t, "tr"):
        return False
    if _NONANSWER_RE.match(t):
        return False
    return True

# Aday cevabının AÇIK RET / TAMAMEN ALAKASIZ olduğu — 0 puanın verilebileceği DAR koşul.
_OPEN_REFUSAL_RE = re.compile(
    r"cevap\s+ver(m(ek|ey)|mey)\w*\s+istem|cevaplamak\s+istem|cevap\s+vermeyece|konuşmak\s+istem|"
    r"aç[ıi]k(ça)?\s+red|reddett|yan[ıi]tlamay[ıi]\s+red|bu\s+soruyu\s+geçmek\s+istiyorum|"
    r"tamamen\s+(alakas[ıi]z|ilgisiz|konu\s*d[ıi]ş[ıi])|alakas[ıi]z\s+bir\s+(cevap|yan[ıi]t)|konu\s*d[ıi]ş[ıi]\s+(cevap|yan[ıi]t)",
    re.IGNORECASE)

def _criterion_ask_status(cname: str, criteria_coverage, transcript: str, repeated_unanswered=None) -> str:
    """Bir kriterin puanlanabilirlik durumu:
       'valid_ask'            — düzgün soruldu VE en az bir GEÇERLİ aday cevabı alındı
       'asked_no_valid_answer'— soruldu ama tüm cevaplar boş/halüsinasyon/'anlamadım' YA DA aynı soru tekrarlandı
       'not_asked'            — mülakatta sorulduğuna dair kanıt yok
    'asked_no_valid_answer' ve 'not_asked' → SİSTEM kaynaklı eksik (PAYDA DIŞI)."""
    # detect_repeated_questions bu kriterde ısrar tespit ettiyse: sistem kaynaklı (mülakatçı soruyu yönetememiş)
    if repeated_unanswered:
        for rn in repeated_unanswered:
            if _name_score(cname, rn) >= 0.5 or rn == "bir konu":
                if rn == "bir konu":
                    continue
                return "asked_no_valid_answer"
    kws = [w for w in _norm_name(cname).split() if len(w) >= 4]
    lines = (transcript or "").splitlines()
    asked_any = False
    valid_answer = False
    for i, ln in enumerate(lines):
        if not re.search(r"(^|\])\s*m[üu]lakat[çc][ıi]\s*:", ln, re.IGNORECASE):
            continue
        if not kws:
            continue
        ln_norm = _norm_name(ln)   # NOT ln.lower() — Türkçe 'İ'.lower() birleşik nokta üretir
        hits = sum(1 for w in kws if w in ln_norm)
        if hits < max(1, len(kws) // 2):
            continue
        asked_any = True
        ans_line = next((l for l in lines[i + 1:] if re.search(r"(^|\])\s*aday\s*:", l, re.IGNORECASE)), "")
        atxt = re.sub(r"^.*?aday\s*:\s*", "", ans_line, flags=re.IGNORECASE)
        if _is_substantive_answer(atxt):
            valid_answer = True
    if not asked_any:
        # transkript yetersiz/kelime eşleşmedi → coverage'a düş (sadece "soruldu mu")
        if isinstance(criteria_coverage, dict):
            for k, v in criteria_coverage.items():
                try:
                    if _name_score(cname, k) >= 0.5 and float(v or 0) > 10:
                        return "valid_ask" if not transcript else "asked_no_valid_answer"
                except Exception:
                    pass
        return "not_asked"
    return "valid_ask" if valid_answer else "asked_no_valid_answer"

def _criterion_was_asked(cname: str, criteria_coverage, transcript: str) -> bool:
    """Geriye uyum — 'soruldu mu' (durum ne olursa olsun)."""
    return _criterion_ask_status(cname, criteria_coverage, transcript) != "not_asked"

def recompute_and_fix_score(report_body: str, position_criteria: list, model_score, criteria_coverage=None, transcript: str = None,
                            candidate_id: int = None, level: int = None):
    """Sunucu tarafı puanlama doğrulaması:
      - hiçbir kriter puanı kendi tavanını (pozisyon ağırlığı) aşamaz → aşan tavana sabitlenir
      - KALEM 1: 'Değerlendirilmedi' iki sebebe ayrılır:
          (a) SİSTEM kaynaklı (sorulmadı / teknik / süre / bağlantı) → paydadan DÜŞÜLÜR
          (b) ADAY kaynaklı (soruldu, cevap alınamadı / kaçındı / yüzeysel) → paydada KALIR, 0 puan
      - skor = alınan / (değerlendirilen + aday-kaynaklı eksik) kriterlerin tavanı * 100
      - 'TOPLAM PUAN' satırı NORMALİZE değeri gösterir; parantezde ham puan (extract_score bunu okur)
    Dönüş: (duzeltilmis_rapor, final_score, warnings[]). İdempotent: bir kez düzeltilmişse aynen döner."""
    warnings = []
    if not report_body or not position_criteria:
        return report_body, model_score, warnings
    if _SCORE_FIXED_MARK in report_body:
        # zaten doğrulanmış (KALEM 5: denetçiden önce bir kez uygulanıyor) — çift işleme yok
        return report_body, extract_score(report_body), warnings
    # ÇİFT PUANLAMA: bu fonksiyon YALNIZCA PUAN 1 (pozisyon) bölgesini işler. PUAN 2 (profil)
    # bölgesi recompute_profile_section tarafından ayrıca doğrulanır — burada dokunulmadan geçer.
    _p1_region, _p2_region = split_report_regions(report_body)
    def _reattach(s):
        return (s.rstrip() + "\n\n" + _p2_region.lstrip("\n")) if _p2_region else s
    lines = _p1_region.splitlines()
    _skip = {"kriter", "criterion", "puan", "score", "değerlendirme", "kanıt ve analiz", "kanit ve analiz"}
    # aday tablo satırları: >=2 '|', ilk hücre anlamlı (ayraç/başlık değil)
    rows = []
    for i, ln in enumerate(lines):
        if ln.count("|") < 2:
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        c0 = _norm_name(re.sub(r"[*_`]", "", cells[0]))
        if len(c0) < 2 or c0 in _skip or set(cells[0].replace(" ", "")) <= set("-:|"):
            continue
        rows.append({"line_idx": i, "name": cells[0], "cell": cells[1] if len(cells) > 1 else ""})
    # Güvenlik ağı: PUAN 2 başlığı düşmüş ve profil satırları PUAN 1 bölgesinde kalmışsa,
    # bunları pozisyon puanına karıştırma (ör. 'İletişim ve ifade netliği' ↔ pozisyon 'İletişim').
    _prof_names = [pc["name"] for pc in PROFILE_CRITERIA]
    rows = [r for r in rows if max((_name_score(pn, r["name"]) for pn in _prof_names), default=0.0) < 0.6]

    # detect_repeated_questions: mülakatçının aynı kriterde ısrarladığı → SİSTEM kaynaklı eksik.
    _repeated_unanswered = set()
    try:
        if candidate_id and level:
            _repeated_unanswered = {r["kriter"] for r in detect_repeated_questions(candidate_id, level) if r.get("kriter")}
    except Exception as e:
        print(f"UYARI (recompute repeated-q c={candidate_id}): {type(e).__name__}: {e}")

    # Her pozisyon kriterine EN İYİ eşleşen tablo satırını ata (satır tekrar kullanılmaz).
    used = set()
    awarded_sum = 0
    denom_cap = 0                     # payda = değerlendirilen + aday-kaynaklı eksik kriterlerin tavanı
    evaluated_names = []              # gerçekten puan alanlar
    sys_missing, cand_missing = [], []   # (a) sistem eksik / (b) aday eksik
    for c in position_criteria:
        cap = _safe_int(c.get("weight"))
        if cap <= 0:
            continue
        cname = c.get("name", "")
        best, best_s = None, 0.0
        for r in rows:
            if r["line_idx"] in used:
                continue
            s = _name_score(cname, r["name"])
            if s > best_s:
                best, best_s = r, s
        puan_cell = best["cell"] if (best and best_s >= 0.34) else ""
        if best and best_s >= 0.34:
            used.add(best["line_idx"])
        cell_lc = puan_cell.lower()

        _mm_frac = re.search(r"(?<![\d/／])(\d+)\s*[/／]\s*(\d+)(?![\d/／])", puan_cell)
        _mm_lead = re.match(r"\s*[*_`]*\s*(\d+)\s*(?:puan|pts?|/\s*\d+)?\s*[*_`]*\s*$", puan_cell, re.IGNORECASE)
        mm = _mm_frac or _mm_lead

        if not mm:
            # ── SAYI YOK → YENİ TEK KURAL: varsayılan SİSTEM kaynaklı (PAYDA DIŞI 'Değerlendirilemedi
            #    (sistem)'). 0/cap — Yetersiz (aday) YALNIZCA çok dar koşulda: kriter DÜZGÜN soruldu
            #    (geçerli aday cevabı alınmış) VE hücre açık ret / tamamen alakasız cevap diyor.
            gm = re.search(r"[—:–\-]\s*(.+)$", re.sub(r"\((?:sorulmad[ıi]|soruldu[^)]*|sistem)\)", "", puan_cell, flags=re.IGNORECASE))
            reason = (gm.group(1).strip() if gm else "")
            status = _criterion_ask_status(cname, criteria_coverage, transcript, _repeated_unanswered)
            refusal = bool(_OPEN_REFUSAL_RE.search(puan_cell))
            _found = best is not None and best_s >= 0.34

            if _found and status == "valid_ask" and refusal:
                # (b) ADAY kaynaklı 0 — DAR KOŞUL
                denom_cap += cap
                _gk = reason or "aday cevap vermeyi reddetti / tamamen alakasız cevap verdi"
                cand_missing.append({"kriter": cname, "gerekce": _gk})
                li = best["line_idx"]
                lines[li] = lines[li].replace(f"| {puan_cell} |", f"| 0/{cap} — Yetersiz (aday): {_gk} |", 1)
            else:
                # (a) SİSTEM kaynaklı — PAYDA DIŞI
                _sysreason = {
                    "asked_no_valid_answer": "sorulan turlarda geçerli aday cevabı alınamadı (boş / halüsinasyon / 'anlamadım' / mülakatçı ısrarı)",
                    "not_asked": "bu kriter mülakatta sorulmadı",
                }.get(status, reason or "yeterli veri oluşmadı")
                if _found and refusal and status != "valid_ask":
                    warnings.append(f"'{cname}' hücrede 'ret' geçiyor ama geçerli aday cevabı yok → sistem kaynaklı eksik sayıldı (payda dışı).")
                if not _found:
                    warnings.append(f"'{cname}' kriteri rapor tablosunda bulunamadı — sistem kaynaklı eksik (payda dışı).")
                sys_missing.append({"kriter": cname, "gerekce": _sysreason})
                if _found and puan_cell:
                    li = best["line_idx"]
                    lines[li] = lines[li].replace(f"| {puan_cell} |", f"| Değerlendirilemedi (sistem) — {_sysreason} |", 1)
            continue

        awarded = _safe_int(mm.group(1))
        written_cap = _safe_int(mm.group(2)) if (mm.re.groups >= 2 and mm.group(2)) else None
        if awarded > cap:
            warnings.append(f"'{cname}' puanı {awarded} kendi tavanını ({cap}) aşıyordu → {cap}'e sabitlendi.")
            awarded = cap
        elif written_cap is not None and written_cap != cap:
            warnings.append(f"'{cname}' payda {written_cap} yazılmış, gerçek tavan {cap} → düzeltildi.")
        # YENİ KURAL — modelin verdiği 0: geçerli aday cevabı YOKSA (halüsinasyon/tekrar/hiç sorulmadı)
        # bu 0 SİSTEM kaynaklı eksiktir, paydaya girmez. Geçerli cevap varsa modelin 0'ı korunur.
        if awarded == 0:
            _st0 = _criterion_ask_status(cname, criteria_coverage, transcript, _repeated_unanswered)
            if _st0 != "valid_ask":
                _sr0 = {"asked_no_valid_answer": "sorulan turlarda geçerli aday cevabı alınamadı",
                        "not_asked": "bu kriter mülakatta sorulmadı"}.get(_st0, "yeterli veri oluşmadı")
                warnings.append(f"'{cname}' modelce 0/{cap} verilmiş ama geçerli aday cevabı yok → 'Değerlendirilemedi (sistem)', payda dışı.")
                sys_missing.append({"kriter": cname, "gerekce": _sr0})
                li = best["line_idx"]
                lines[li] = lines[li].replace(f"| {puan_cell} |", f"| Değerlendirilemedi (sistem) — {_sr0} |", 1)
                continue
        li = best["line_idx"]
        _cell_new = f"{awarded}/{cap}"
        if "/" in puan_cell:
            lines[li] = re.sub(r"\d+\s*[/／]\s*\d+", _cell_new, lines[li], count=1)
        else:
            lines[li] = lines[li].replace(f"| {puan_cell} |", f"| {_cell_new} |", 1)
        awarded_sum += awarded
        denom_cap += cap
        evaluated_names.append(cname)

    evaluated_cap = denom_cap
    if sys_missing:
        warnings.append("Değerlendirilemeyen kriterler (SİSTEM kaynaklı — payda dışı): "
                        + "; ".join(f"{s['kriter']} ({s['gerekce']})" for s in sys_missing))
    if cand_missing:
        warnings.append("Yetersiz / cevapsız kriterler (ADAY kaynaklı — 0 puan, payda içinde): "
                        + "; ".join(f"{s['kriter']} ({s['gerekce']})" for s in cand_missing))
    if evaluated_cap <= 0:
        return _reattach("\n".join(lines)), model_score, warnings
    normalized = max(0, min(100, round(awarded_sum / evaluated_cap * 100)))
    total_weight = sum(_safe_int(c.get("weight")) for c in position_criteria)
    body = "\n".join(lines)
    m_total = re.search(r"(\*\*\s*TOPLAM\s+PUAN\s*[:：]\s*)(\d+)\s*/\s*(\d+)(\s*\*\*)", body, re.IGNORECASE)
    model_total = _safe_int(m_total.group(2)) if m_total else _safe_int(model_score)
    # TOPLAM PUAN satırı: NORMALİZE değeri /100 olarak (extract_score bunu okur) + parantezde ham.
    new_total_line = (f"**TOPLAM PUAN: {normalized}/100**  (ham puan: {awarded_sum}/{evaluated_cap}; "
                      f"değerlendirilen {len(evaluated_names)}, aday-kaynaklı eksik {len(cand_missing)}, sistem-kaynaklı eksik {len(sys_missing)})")
    need_fix = bool(warnings) or (m_total and (abs(model_total - normalized) > 1 or _safe_int(m_total.group(3)) != 100))
    if need_fix or not m_total:
        if m_total:
            body = re.sub(r"\*\*\s*TOPLAM\s+PUAN\s*[:：][^\n]*\*\*", new_total_line, body, count=1, flags=re.IGNORECASE)
        else:
            body = new_total_line + "\n\n" + body
        warnings.append(f"Toplam puan yeniden hesaplandı: ham {awarded_sum}/{evaluated_cap} → normalize %{normalized}/100 "
                        f"(model {model_total}/{_safe_int(m_total.group(3)) if m_total else total_weight} yazmıştı).")
    # KALEM 1 — iki eksiklik listesi rapor metninde AYRI ve deterministik görünsün
    if (sys_missing or cand_missing) and "Kriter Eksiklik Ayrımı (sistem)" not in body:
        blk = ["", "**Kriter Eksiklik Ayrımı (sistem):**"]
        if sys_missing:
            blk.append("- Değerlendirilemedi (sistem) — **PAYDA DIŞI, puanı etkilemez** (hiç sorulmadı / halüsinasyon / 'anlamadım' / mülakatçı ısrarı): "
                       + "; ".join(f"{s['kriter']} — {s['gerekce']}" for s in sys_missing))
        if cand_missing:
            blk.append("- 0 puan (paydada) — aday cevap vermeyi AÇIKÇA reddetti / tamamen alakasız cevap: "
                       + "; ".join(f"{s['kriter']} — {s['gerekce']}" for s in cand_missing))
        body = body.rstrip() + "\n" + "\n".join(blk) + "\n"
    return _reattach(body), normalized, warnings

_PROFILE_SCORE_FIXED_MARK = "(profil ham:"

def recompute_profile_section(profile_region: str, transcript: str = None, criteria_coverage=None,
                              candidate_id: int = None, level: int = None):
    """ÇİFT PUANLAMA — PUAN 2 (kişisel/bilişsel profil) bölgesi için PUAN 1 ile AYNI puanlama
    doğrulaması: kriter tavanı + aday/sistem-kaynaklı eksiklik ayrımı + değerlendirilen ağırlığa
    normalize. 'PROFİL PUANI' satırını NORMALİZE değerle yeniden yazar.
    Dönüş: (duzeltilmis_bolge, profil_skoru|None, warnings[]). İdempotent."""
    warnings = []
    if not profile_region:
        return profile_region or "", None, warnings
    if _PROFILE_SCORE_FIXED_MARK in profile_region:
        return profile_region, extract_profile_score(profile_region), warnings
    if not re.search(r'PROF\S*\s+PUANI', profile_region, re.IGNORECASE):
        return profile_region, None, warnings
    lines = profile_region.splitlines()
    _skip = {"kriter", "criterion", "puan", "score", "değerlendirme", "kanıt ve analiz", "kanit ve analiz",
             "somut örnek", "somut ornek"}
    rows = []
    for i, ln in enumerate(lines):
        if ln.count("|") < 2:
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        c0 = _norm_name(re.sub(r"[*_`]", "", cells[0]))
        if len(c0) < 2 or c0 in _skip or set(cells[0].replace(" ", "")) <= set("-:|"):
            continue
        rows.append({"line_idx": i, "name": cells[0], "cell": cells[1] if len(cells) > 1 else ""})

    _repeated_unanswered = set()
    try:
        if candidate_id and level:
            _repeated_unanswered = {r["kriter"] for r in detect_repeated_questions(candidate_id, level) if r.get("kriter")}
    except Exception:
        pass
    used = set()
    awarded_sum = 0
    denom_cap = 0
    evaluated_names = []
    sys_missing, cand_missing = [], []
    for c in PROFILE_CRITERIA:
        cap = _safe_int(c.get("weight"))
        cname = c.get("name", "")
        best, best_s = None, 0.0
        for r in rows:
            if r["line_idx"] in used:
                continue
            s = _name_score(cname, r["name"])
            if s > best_s:
                best, best_s = r, s
        puan_cell = best["cell"] if (best and best_s >= 0.34) else ""
        if best and best_s >= 0.34:
            used.add(best["line_idx"])
        cell_lc = puan_cell.lower()

        _mm_frac = re.search(r"(?<![\d/／])(\d+)\s*[/／]\s*(\d+)(?![\d/／])", puan_cell)
        _mm_lead = re.match(r"\s*[*_`]*\s*(\d+)\s*(?:puan|pts?|/\s*\d+)?\s*[*_`]*\s*$", puan_cell, re.IGNORECASE)
        mm = _mm_frac or _mm_lead

        if not mm:
            # YENİ TEK KURAL (PUAN 1 ile aynı): varsayılan SİSTEM kaynaklı (payda dışı 'Değerlendirilemedi
            # (sistem)'). 0 yalnızca DÜZGÜN sorulmuş + açık ret / tamamen alakasız cevapta.
            gm = re.search(r"[—:–\-]\s*(.+)$", re.sub(r"\((?:sorulmad[ıi]|soruldu[^)]*|sistem)\)", "", puan_cell, flags=re.IGNORECASE))
            reason = (gm.group(1).strip() if gm else "")
            status = _criterion_ask_status(cname, criteria_coverage, transcript, _repeated_unanswered)
            refusal = bool(_OPEN_REFUSAL_RE.search(puan_cell))
            _found = best is not None and best_s >= 0.34
            if _found and status == "valid_ask" and refusal:
                denom_cap += cap
                _gk = reason or "aday cevap vermeyi reddetti / tamamen alakasız cevap verdi"
                cand_missing.append({"kriter": cname, "gerekce": _gk})
                li = best["line_idx"]
                lines[li] = lines[li].replace(f"| {puan_cell} |", f"| 0/{cap} — Yetersiz (aday): {_gk} |", 1)
            else:
                _sr = {"asked_no_valid_answer": "sorulan turlarda geçerli aday cevabı alınamadı",
                       "not_asked": "bu kriter mülakatta ölçülmedi"}.get(status, reason or "yeterli veri oluşmadı")
                if not _found:
                    warnings.append(f"[PROFİL] '{cname}' profil tablosunda bulunamadı — sistem kaynaklı eksik (payda dışı).")
                sys_missing.append({"kriter": cname, "gerekce": _sr})
                if _found and puan_cell:
                    li = best["line_idx"]
                    lines[li] = lines[li].replace(f"| {puan_cell} |", f"| Değerlendirilemedi (sistem) — {_sr} |", 1)
            continue

        awarded = _safe_int(mm.group(1))
        written_cap = _safe_int(mm.group(2)) if (mm.re.groups >= 2 and mm.group(2)) else None
        if awarded > cap:
            warnings.append(f"[PROFİL] '{cname}' puanı {awarded} tavanını ({cap}) aşıyordu → {cap}.")
            awarded = cap
        elif written_cap is not None and written_cap != cap:
            warnings.append(f"[PROFİL] '{cname}' payda {written_cap} yazılmış, gerçek tavan {cap} → düzeltildi.")
        if awarded == 0:
            _st0 = _criterion_ask_status(cname, criteria_coverage, transcript, _repeated_unanswered)
            if _st0 != "valid_ask":
                _sr0 = {"asked_no_valid_answer": "sorulan turlarda geçerli aday cevabı alınamadı",
                        "not_asked": "bu kriter mülakatta ölçülmedi"}.get(_st0, "yeterli veri oluşmadı")
                warnings.append(f"[PROFİL] '{cname}' modelce 0/{cap} verilmiş ama geçerli aday cevabı yok → 'Değerlendirilemedi (sistem)', payda dışı.")
                sys_missing.append({"kriter": cname, "gerekce": _sr0})
                lines[best["line_idx"]] = lines[best["line_idx"]].replace(f"| {puan_cell} |", f"| Değerlendirilemedi (sistem) — {_sr0} |", 1)
                continue
        li = best["line_idx"]
        _cell_new = f"{awarded}/{cap}"
        if "/" in puan_cell:
            lines[li] = re.sub(r"\d+\s*[/／]\s*\d+", _cell_new, lines[li], count=1)
        else:
            lines[li] = lines[li].replace(f"| {puan_cell} |", f"| {_cell_new} |", 1)
        awarded_sum += awarded
        denom_cap += cap
        evaluated_names.append(cname)

    body = "\n".join(lines)
    if denom_cap <= 0:
        warnings.append("[PROFİL] Değerlendirilebilir profil kriteri yok — PROFİL PUANI hesaplanamadı.")
        return body, None, warnings
    normalized = max(0, min(100, round(awarded_sum / denom_cap * 100)))
    m_total = re.search(r"(\*\*\s*PROF\S*\s+PUANI\s*[:：]\s*)(\d+)\s*/\s*(\d+)(\s*\*\*)", body, re.IGNORECASE)
    model_total = _safe_int(m_total.group(2)) if m_total else None
    new_total_line = (f"**PROFİL PUANI: {normalized}/100**  {_PROFILE_SCORE_FIXED_MARK} {awarded_sum}/{denom_cap}; "
                      f"değerlendirilen {len(evaluated_names)}, aday-kaynaklı eksik {len(cand_missing)}, sistem-kaynaklı eksik {len(sys_missing)})")
    need_fix = bool(warnings) or (m_total and (abs((model_total or 0) - normalized) > 1 or _safe_int(m_total.group(3)) != 100))
    if need_fix or not m_total:
        if m_total:
            body = re.sub(r"\*\*\s*PROF\S*\s+PUANI\s*[:：][^\n]*\*\*", new_total_line, body, count=1, flags=re.IGNORECASE)
        else:
            body = re.sub(r"(PROF\S*\s+PUANI\s*[:：][^\n]*)", new_total_line, body, count=1, flags=re.IGNORECASE)
        if model_total is not None and abs(model_total - normalized) > 1:
            warnings.append(f"[PROFİL] Puan yeniden hesaplandı: ham {awarded_sum}/{denom_cap} → %{normalized} (model {model_total} yazmıştı).")
    if (sys_missing or cand_missing) and "Profil Kriter Eksiklik Ayrımı" not in body:
        blk = ["", "**Profil Kriter Eksiklik Ayrımı (sistem):**"]
        if sys_missing:
            blk.append("- Değerlendirilemedi (sistem) — **PAYDA DIŞI** (ölçülmedi / halüsinasyon / 'anlamadım' / mülakatçı ısrarı): "
                       + "; ".join(f"{s['kriter']} — {s['gerekce']}" for s in sys_missing))
        if cand_missing:
            blk.append("- 0 puan (paydada) — aday cevap vermeyi AÇIKÇA reddetti / tamamen alakasız cevap: "
                       + "; ".join(f"{s['kriter']} — {s['gerekce']}" for s in cand_missing))
        body = body.rstrip() + "\n" + "\n".join(blk) + "\n"
    return body, normalized, warnings

# ═══ KALEM 3 — Modalite veri kapsamı (kamera kareleri + ses metrikleri) rapora deterministik yazılır ═══
def _frame_distribution(minutes_list, total_minutes=None):
    """Bir kare dakika listesinden dağılım + kümelenme bilgisi.
    KÜMELENME: 3+ kare varsa VE (kapsam <= 2 dk  VEYA  kapsam bilinen mülakat süresinin %25'inden az)."""
    d = {"n": len(minutes_list), "ilk_dk": None, "son_dk": None, "kapsam_dk": None, "kumelenme": False}
    if not minutes_list:
        return d
    mn, mx = min(minutes_list), max(minutes_list)
    span = mx - mn
    d["ilk_dk"], d["son_dk"], d["kapsam_dk"] = round(mn, 1), round(mx, 1), round(span, 1)
    if len(minutes_list) >= 3:
        if span <= 2.0:
            d["kumelenme"] = True
        elif total_minutes and total_minutes > 0 and span < total_minutes * 0.25:
            d["kumelenme"] = True
    return d

def compute_modality_coverage(candidate_id: int, level: int) -> dict:
    """İKİ kare seti AYRI raporlanır — kaynak farkı bilinsin:
      - dogrulama: reason<>'mimic_sample' (panel/PDF galerisinde görünen 4 kare; captured_at bazlı)
      - mimik: reason='mimic_sample' (yalnız AI mimik analizi, panelde GÖSTERİLMEZ; elapsed_ms bazlı)
    Ayrıca ses metriklerinin gerçekten var olup olmadığı + özet sayıları."""
    out = {"dogrulama": _frame_distribution([]), "mimik": _frame_distribution([]),
           "ses_metrikleri_var": False, "ses_ozet": None, "toplam_dk": None}
    try:
        db = get_db()
        iv = db.execute("SELECT voice_metrics_json, started_at, completed_at FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, level)).fetchone()
        mimic_rows = db.execute("SELECT elapsed_ms FROM snapshots WHERE candidate_id=? AND reason='mimic_sample'", (candidate_id,)).fetchall()
        verify_rows = db.execute("SELECT elapsed_ms, captured_at FROM snapshots WHERE candidate_id=? AND (reason IS NULL OR reason<>'mimic_sample') ORDER BY id ASC", (candidate_id,)).fetchall()
        db.close()
    except Exception as e:
        print(f"UYARI (compute_modality_coverage c={candidate_id}): {type(e).__name__}: {e}")
        return out
    started = _parse_iso(iv["started_at"]) if iv and iv["started_at"] else None
    total_min = None
    if started and iv and iv["completed_at"]:
        _end = _parse_iso(iv["completed_at"])
        if _end:
            total_min = max(0.0, (_end - started).total_seconds() / 60)
    out["toplam_dk"] = round(total_min, 1) if total_min else None

    mimic_min = [_safe_int(r["elapsed_ms"]) / 60000 for r in mimic_rows if r["elapsed_ms"] is not None]
    out["mimik"] = _frame_distribution(sorted(mimic_min), total_min)
    out["mimik"]["n"] = len(mimic_rows)

    v_min = []
    for r in verify_rows:
        if r["elapsed_ms"] is not None:
            v_min.append(_safe_int(r["elapsed_ms"]) / 60000)
        elif started:
            ca = _parse_iso(r["captured_at"])
            if ca:
                v_min.append(max(0.0, (ca - started).total_seconds() / 60))
    out["dogrulama"] = _frame_distribution(sorted(v_min), total_min)
    out["dogrulama"]["n"] = len(verify_rows)

    try:
        m = json.loads(iv["voice_metrics_json"]) if (iv and iv["voice_metrics_json"]) else {}
    except Exception:
        m = {}
    if m and _safe_int(m.get("tur_sayisi")) > 0:
        out["ses_metrikleri_var"] = True
        out["ses_ozet"] = {
            "tur_sayisi": m.get("tur_sayisi"),
            "hesaba_katilan_tur": m.get("hesaba_katilan_tur"),
            "toplam_tur": m.get("toplam_tur"),
            "cevapsiz_tur_sayisi": m.get("cevapsiz_tur_sayisi"),
            "aday_konusma_toplam_sn": m.get("aday_konusma_toplam_sn"),
            "ortalama_tur_uzunlugu_sn": m.get("ortalama_tur_uzunlugu_sn"),
            "yanit_gecikmesi_ort_sn": m.get("yanit_gecikmesi_ort_sn"),
            "ai_dusunme_suresi_ort_sn": m.get("ai_dusunme_suresi_ort_sn"),
            "soz_kesme_sayisi": m.get("soz_kesme_sayisi"),
            "guven": m.get("guven"),
        }
    return out

def build_modality_coverage_note(candidate_id: int, level: int) -> str:
    """Rapor gövdesine EKLENEN deterministik blok — modelin atlayamayacağı gerçek kapsam bilgisi.
    (KALEM 1: iki kare seti ayrı; KALEM 2: ses metrikleri VAR ise somut sayılarla, var olmayan
     bir bölüme atıf YOK.)"""
    c = compute_modality_coverage(candidate_id, level)
    lines = ["", "**Modalite Veri Kapsamı (sistem — deterministik):**"]

    def _frame_line(label, d, extra=""):
        if d["n"] == 0:
            return f"- {label}: hiç alınmadı."
        span = f" — mülakatın {d['ilk_dk']}.–{d['son_dk']}. dakikaları arası" if d["ilk_dk"] is not None else ""
        warn = "  ⚠️ UYARI: kareler dar bir aralıkta toplanmış; oturumun büyük kısmı gözlemsiz." if d["kumelenme"] else ""
        return f"- {label}: {d['n']} kare{span}.{extra}{warn}"

    lines.append(_frame_line("Kamera doğrulama kareleri (panelde/PDF'te görünen)", c["dogrulama"]))
    lines.append(_frame_line("Mimik analiz kareleri (yalnız AI analizi, panelde gösterilmez)", c["mimik"]))

    if c["ses_metrikleri_var"] and c["ses_ozet"]:
        s = c["ses_ozet"]
        def _n(x, unit=""):
            return f"{x}{unit}" if x is not None else "—"
        _tur_txt = _n(s['hesaba_katilan_tur'] if s.get('hesaba_katilan_tur') is not None else s['tur_sayisi'])
        _tot = s.get('toplam_tur')
        _cevapsiz = s.get('cevapsiz_tur_sayisi')
        _basis = f"{_tur_txt}" + (f" (toplam {_tot} turun {_tur_txt}'i üzerinden; cevapsız/halüsinasyon tur: {_n(_cevapsiz)})" if _tot else "")
        lines.append(
            "- Ses metrikleri (tur bazlı, cevapsız turlar hariç): "
            f"hesaba katılan tur {_basis}, "
            f"aday konuşma toplam {_n(s['aday_konusma_toplam_sn'],' sn')}, "
            f"ort. tur uzunluğu {_n(s['ortalama_tur_uzunlugu_sn'],' sn')}, "
            f"yanıt gecikmesi ort. {_n(s['yanit_gecikmesi_ort_sn'],' sn')}, "
            f"AI düşünme süresi ort. {_n(s['ai_dusunme_suresi_ort_sn'],' sn')}, "
            f"söz kesme {_n(s['soz_kesme_sayisi'])} (güven: {_n(s['guven'])})."
        )
    else:
        lines.append("- Ses metrikleri: TOPLANAMADI — realtime_events'te konuşma başlangıç/bitiş olayı yok; "
                     "yanıt gecikmesi / duraklama / konuşma-sessizlik oranı / söz kesme ölçülemedi.")
    # B2 — L2/L3 sesli hatta sunucu tarafı soru-tekrarı tespiti (ses metrikleri bloğunun yanında).
    try:
        rep = detect_repeated_questions(candidate_id, level)
        for r in rep:
            lines.append(f"- ⚠️ Soru tekrarı: Mülakatçı \"{r['kriter']}\" konusunda {r['count']} kez ısrar etti"
                         f"{' (' + r['span'] + ')' if r.get('span') else ''}; aday bu turlarda yeterli yanıt vermedi. "
                         f"(Gözlem — puana etki etmez.)")
    except Exception as e:
        print(f"UYARI (B2 soru tekrarı c={candidate_id} L{level}): {type(e).__name__}: {e}")
    return "\n".join(lines)

# ═══ B2 — SORU TEKRARI TESPİTİ (sunucu tarafı, L2/L3 sesli) ═══
# L1'de [YENIDEN] etiketini sayan sunucu sayacı var; L2/L3 sesli hatta yok. Kayıtlı transkript
# üzerinden art arda gelen mülakatçı sorularının kelime örtüşmesine bakarak "aynı soru N kez"
# durumunu tespit eder. SES HATTINA DOKUNMAZ. PUANA ETKİ ETMEZ — yalnız gözlem.
_QREPEAT_OVERLAP_THRESHOLD = 0.55   # ardışık mülakatçı soruları arasında anlamlı kelime Jaccard eşiği
_QREPEAT_MIN_RUN = 3               # "2 denemeyi aşan" = aynı sorunun 3+ kez sorulması

def _q_keywords(text: str) -> set:
    return {w for w in _norm_name(text).split() if len(w) >= 4 and w not in _ANNOTATE_STOPWORDS}

def _stem_overlap(a: set, b: set) -> int:
    """Türkçe eklerini yok saymak için kaba kök eşleşmesi: iki kelimeden biri diğerinin >=5
    harfli ön ekiyse eşleşmiş sayılır."""
    hits = 0
    for wa in a:
        for wb in b:
            if wa == wb or (len(wa) >= 5 and len(wb) >= 5 and (wa.startswith(wb[:5]) or wb.startswith(wa[:5]))):
                hits += 1
                break
    return hits

def detect_repeated_questions(candidate_id: int, level: int) -> list:
    if level not in (2, 3):
        return []
    try:
        db = get_db()
        try:
            iv = db.execute("SELECT messages, started_at FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, level)).fetchone()
            pos_row = db.execute("SELECT position FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        finally:
            db.close()
    except Exception as e:
        print(f"UYARI (detect_repeated_questions fetch c={candidate_id}): {type(e).__name__}: {e}")
        return []
    if not iv:
        return []
    view = build_transcript_view(iv["messages"], level, iv["started_at"])
    _crits = ((get_position(pos_row["position"]) if pos_row else None) or {}).get("criteria", [])
    # kriter eşleştirme için ad + desc anahtar kelimeleri (sorular somut, kriter adları soyut)
    crit_kw = [(c.get("name"), _q_keywords((c.get("name") or "") + " " + (c.get("desc") or "")))
               for c in _crits if c.get("name")]

    # mülakatçı sorularını sırayla al; her birine, arada gelen aday cevabının "boş/yetersiz" olup
    # olmadığını iliştir.
    q_items = []   # {ts, kw, bos}
    rows = view
    for idx, row in enumerate(rows):
        if row["role"] != "mulakatci":
            continue
        txt = row["text"] or ""
        if len(_q_keywords(txt)) < 2 or "?" not in txt and len(txt) < 15:
            continue
        # sonraki aday cevabı
        nxt = next((r for r in rows[idx + 1:] if r["role"] == "aday"), None)
        ans = (nxt["text"] if nxt else "") or ""
        _al = ans.strip().lower()
        aday_bos = (len(_al) < 12) or is_likely_hallucination(ans, "tr") or is_hallucination_marker_line(ans) \
                   or bool(re.match(r"(anlamad|anlayamad|tekrar\s+ed|pardon|duyamad|efendim|bilmiyorum|geçelim)", _al))
        q_items.append({"ts": row.get("ts") or "", "kw": _q_keywords(txt), "bos": aday_bos})

    out = []
    n = len(q_items)
    used = [False] * n
    for a in range(n):
        if used[a] or not q_items[a]["kw"]:
            continue
        run = [a]
        base = set(q_items[a]["kw"])
        for b in range(a + 1, n):
            kb = q_items[b]["kw"]
            if not kb:
                continue
            inter = len(base & kb)
            jacc = inter / max(1, len(base | kb))
            if jacc >= _QREPEAT_OVERLAP_THRESHOLD:
                run.append(b)
                base |= kb
            elif b - run[-1] > 2:
                break
        if len(run) >= _QREPEAT_MIN_RUN and sum(1 for k in run if q_items[k]["bos"]) >= len(run) - 1:
            for k in run:
                used[k] = True
            # kriter eşleştir: run'ın kelimeleri hangi kriterin ad+desc anahtarlarıyla en çok örtüşüyor
            best_crit, best_ov = None, 0
            for cn, ck in crit_kw:
                ov = _stem_overlap(base, ck)
                if ov > best_ov:
                    best_crit, best_ov = cn, ov
            ts0 = q_items[run[0]]["ts"]
            ts1 = q_items[run[-1]]["ts"]
            span = f"[{ts0}]–[{ts1}]" if ts0 and ts1 else ""
            out.append({"kriter": best_crit or "bir konu", "count": len(run), "span": span})
    return out[:4]

def build_l2_report_prompt(candidate, candidate_level: int, transcript: str,
                           criteria_coverage=None, extra_notes: str = "") -> str:
    """L2/L3 sesli rapor promptu — hem create_l2_report hem /regenerate-report kullanır."""
    pos = get_position(candidate["position"]) or {"category": "Genel", "criteria": [{"name": "Genel Yetkinlik", "weight": 100, "desc": ""}]}
    criteria_text = build_criteria_text(pos["criteria"])
    total_weight = sum(c["weight"] for c in pos["criteria"])
    report_lang = LANGUAGE_NAMES.get(candidate["report_language"] or "tr", "Türkçe")
    cv_for_report = candidate["cv_text"][:7000] if candidate["cv_text"] and len(candidate["cv_text"].strip()) > 20 else "CV yüklenmemiş; sadece transkripte göre değerlendir."
    ai_note_section = ""
    ai_note_report_field = ""
    if candidate["ai_note"] and candidate["ai_note"].strip():
        ai_note_section = f"\n\nADAY ÖZEL AI NOTU (bu mülakatta bu konuya öncelik verilmiş olmalı, transkriptte nasıl ele alındığını değerlendir):\n{candidate['ai_note'].strip()[:1200]}"
        ai_note_report_field = "\n**AI Notuna Uyum:** (bu adaya özel notun transkriptte nasıl ele alındığını somut olarak yaz: hangi soru/turlarda test edildi, sonucu ne oldu)"
    depth_tier = (candidate["depth_tier"] if "depth_tier" in candidate.keys() else "standart") or "standart"
    coverage_threshold = get_depth_tier_config(depth_tier)["coverage_threshold"]
    coverage_block = ""
    if isinstance(criteria_coverage, dict) and criteria_coverage:
        coverage_block = "\n\nMÜLAKATÇININ BİLDİRDİĞİ KRİTER KAPSANMA (0-100, destekleyici — puanı bağlamaz):\n" + \
            "\n".join(f"- {k}: ~%{int(float(v or 0))}" for k, v in criteria_coverage.items() if str(v) != "")
    unanswered = build_unanswered_criteria(pos["criteria"], criteria_coverage, coverage_threshold)
    unanswered_block = ""
    if unanswered:
        unanswered_block = ("\n\nKAPSANMA DÜŞÜK KRİTERLER (mülakatçının bildirdiği kapsanma eşiğin altında). Bunlar için "
                            "TEK KURAL geçerli: sorulan turlarda adayın GEÇERLİ bir cevabı varsa kanıt düzeyine göre "
                            "DÜŞÜK puan; yoksa (halüsinasyon / 'anlamadım' / hiç sorulmadı) → **Değerlendirilemedi "
                            "(sistem)**, PAYDA DIŞI. 0 verme.\n- " + "\n- ".join(unanswered))
    report_body_l2 = build_report_body(candidate_level, {
        "candidate_name": candidate["name"], "position_name": candidate["position"],
        "date_str": datetime.now().strftime('%d.%m.%Y'), "total_weight": total_weight,
        "ai_note_report_field": ai_note_report_field,
        "criteria_table_filled": build_criteria_table_filled(pos["criteria"]),
        "profile_table_filled": build_profile_table_filled(),
    })
    # KALEM 3 (2. tur) — deterministik alan karşılaştırması: model tespiti kendisi yapmaz, yorumlar.
    try:
        _disc_block = "\n\n" + render_discrepancy_block(compute_field_discrepancies(dict(candidate), transcript), for_prompt=True)
    except Exception as e:
        print(f"UYARI (build_l2_report_prompt çelişki bloğu): {type(e).__name__}: {e}")
        _disc_block = ""
    return f"""Aşağıda bir sesli iş mülakatının transkripti, aday CV'si, pozisyon kriterleri ve derinlik bilgisi vardır. İnsan kaynakları yöneticisinin karar vermesine yardım edecek, adaya özgü ve ayrıntılı bir değerlendirme raporu üret.

Aday: {candidate['name']}
Pozisyon: {candidate['position']}
Mülakat seviyesi: Level {candidate_level}
Derinlik: {depth_tier}
Kriterler ({total_weight} puan):
{criteria_text}

KAYIT FORMU BEYANI (adayın/adminin başvuru formunda girdiği bilgi):
- E-posta: {candidate['email'] or '—'}
- Eğitim: {candidate['education'] or '—'} · Üniversite: {candidate['university'] or '—'} · Bölüm: {candidate['department'] or '—'}
- Deneyim yılı (beyan): {candidate['experience_years'] if candidate['experience_years'] not in (None, '', 0) else '—'}
{_disc_block}

ADAYIN CV'Sİ:
{cv_for_report}{ai_note_section}{coverage_block}{unanswered_block}{extra_notes}

TRANSKRİPT:
{(transcript or '')[:TRANSCRIPT_PROMPT_MAX_CHARS]}

TEMEL KURALLAR:
- Rapor {report_lang} dilinde yazılacak.
- ÇELİŞKİ TARAMASI (KESİN): "Tutarlılık / Çelişki Analizi" bölümünü YUKARIDAKİ "SİSTEM ALAN KARŞILAŞTIRMASI" listesine dayandır — tespiti sen yapma. Liste 'çelişki' diyen alanı çelişki olarak yaz; 'karşılaştırılamadı' diyen alan için ASLA "tutarlıdır" deme. Ayrıca transkriptte açıkça görünen başka çelişkiler varsa ekle. Karşılaştırdığın alanları say.
- Yalnızca adayın gerçekten söylediği sözler mülakat kanıtıdır. Mülakatçının açıklamalarını adaya mal etme.
- CV bilgisi ile mülakat kanıtını ayır: “CV'de belirtilmiştir” ve “mülakatta doğrulanmıştır/doğrulanamamıştır” ifadelerini açık kullan.
- Adayın söylemediği deneyim, beceri, sonuç, motivasyon veya kişilik özelliği uydurma.
- Aynı kalıp cümleleri her bölümde tekrar etme. Rapor bu adaya özgü olmalı; somut proje, karar, örnek ve ifadeleri kullan.
{CRITERION_SCORING_RULE}
- Toplam puanı yalnızca PUANLANAN kriterlerin ağırlığına göre normalize et. 'Değerlendirilemedi (sistem)' kriterleri hesaba KATMA. Raporda puanlanan ve payda-dışı listeleri AYRI göster.
- PUAN TAVANI (KESİN): Hiçbir kriter puanı kendi tavanını (ağırlığını) AŞAMAZ. "Uyum 12/10" gibi bir şey ASLA yazma; en fazla "10/10". TOPLAM PUAN = alınan puanların toplamı. Bunu doğru hesapla, sistem ayrıca doğrular.
- ÇİFT PUANLAMA (KESİN): Rapor İKİ ayrı puan içerir. PUAN 1 = "TOPLAM PUAN" → yukarıdaki POZİSYON kriterleri; işe alım önerisi (İşe Al / Değerlendirmeye Al / Reddet) ve %20 eşiği YALNIZCA buna göredir. PUAN 2 = "PROFİL PUANI" → pozisyondan bağımsız sabit kişisel/bilişsel profil kriterleri; her satır transkriptten SOMUT örnek + [dk] ile, dayanaksız çıkarım yok. İki tabloyu ve iki puanı KARIŞTIRMA. Profil bölümündeki "[VETO: …]" etiketini SADECE kurumsal ortamda çalışmaya engel ciddi bulguda (saldırganlık, hakaret, işbirliğine tam kapalılık, sürdürülen açık düşmanlık) yaz; sıradan düşüklük veto sebebi değildir.
- ÖNERİ ↔ METİN TUTARLILIĞI (KESİN): PUAN 1'in 100 üzerinden normalize değeri öneriyi belirler: **<40 → Reddet · 40–79 → Değerlendirmeye Al · ≥80 → İşe Al**. Verdiğin Öneri bu eşikteki karşılık olmalı. Yönetici Özeti'nin SON (karar) cümlesi ve Öneri Gerekçesi öneriyle AYNI YÖNDE yazılır — Öneri "Reddet" iken "değerlendirmeye alınabilir / potansiyeli var / uygun / yeterli düzeyde" gibi olumlu sonuç ifadesi YASAK; tersi de.
- KRİTER TABLOSU DETERMİNİSTİK: Rapordaki kriter tablosunun satırları YUKARIDA verilen tablonun BİREBİR AYNISI olacak — aynı kriter adları, aynı sıra, aynı tavanlar. Satır ekleme, çıkarma, birleştirme veya yeniden adlandırma YOK. Gerekçesiz eksik-işaretleme YASAK.
- Erken sonlandırma, davranış gözlemi veya pozisyon uyumsuzluğu notu verildiyse: raporda ilgili başlık altında SOMUT (dakika + transkriptteki söz) yaz; bunları TEK BAŞINA puan düşürme gerekçesi yapma.
- Her puan için Kanıt → Analiz → Sonuç zinciri kur.
- Analitik düşünme, kavrama, muhakeme, neden-sonuç kurma, problem çözme, düşünce esnekliği, öğrenme çevikliği ve belirsizlikte karar verme hakkında yalnızca transkriptte gözlenebilen sinyalleri yaz. IQ, zekâ puanı, psikiyatrik tanı, yalan tespiti veya kesin kişilik teşhisi yapma.
- Görüşme kalitesi veya teknik kesinti değerlendirmeyi etkilediyse bunu ayrıca belirt; adayı bunun için cezalandırma.
- SES METRİĞİ SAYILARINI (konuşma süresi, tur sayısı/uzunluğu, yanıt gecikmesi, söz kesme vb.) rapor metnine TEKRAR YAZMA — bu sayılar sistem tarafından ayrı 'Modalite Veri Kapsamı' bloğunda deterministik olarak veriliyor. "Serbest Gözlemler" yalnız NİTELİKSEL gözlem taşır (duruş, mimik, davranış, tutum); niteliksel bir şey yoksa "Belirtilecek bir gözlem yok" yaz.
- "Dil Gözlemi", "Serbest Gözlemler", "Değerlendirilemeyen Alanlar" bölümlerinde yazacak bir şey yoksa "Belirtilecek bir ... yok" yaz — sistem bu bölümü rapordan otomatik çıkarır, uydurma içerik ekleme.
- En az üç anlamlı aday cevabı yoksa [DEĞERLENDİRİLEMEDİ] üret.
- Derinlik “derin” ise rapor daha kapsamlı, daha fazla çapraz kanıtlı ve daha ayrıntılı olmalı; standart rapor da kesinlikle yüzeysel olmamalı.

TAM FORMAT:
[MÜLAKATBİTTİ]
---RAPOR---
{report_body_l2}

Çıktı mutlaka [MÜLAKATBİTTİ] ve ---RAPOR--- bloklarıyla başlasın."""


@app.post("/api/realtime/report")
async def create_l2_report(data: RealtimeReportRequest, background_tasks: BackgroundTasks, payload=Depends(verify_token)):
    if payload.get("role") != "candidate":
        raise HTTPException(status_code=403, detail="Yetkisiz")

    effective_candidate_id = int(payload.get("candidate_id") or data.candidate_id)

    db = get_db()
    candidate = db.execute("SELECT * FROM candidates WHERE id=?", (effective_candidate_id,)).fetchone()
    if not candidate:
        db.close()
        raise HTTPException(status_code=404, detail="Aday bulunamadı")
    candidate_level = candidate["level"] or 1
    if candidate_level not in (2, 3):
        db.close()
        raise HTTPException(status_code=400, detail="Bu uç nokta Level 2 ve Level 3 adaylar için geçerlidir.")

    interview = db.execute("SELECT * FROM interviews WHERE candidate_id=? AND level=?", (effective_candidate_id, candidate_level)).fetchone()
    if not interview:
        db.execute("INSERT INTO interviews (candidate_id, level, messages) VALUES (?, ?, '[]')", (effective_candidate_id, candidate_level))
        db.commit()
        interview = db.execute("SELECT * FROM interviews WHERE candidate_id=? AND level=?", (effective_candidate_id, candidate_level)).fetchone()

    # İDEMPOTENCY GUARD: bu mülakat zaten finalize edilmişse (retry, çift tıklama, ağ hatası
    # sonrası tekrar deneme vb.), OpenAI'a tekrar rapor ürettirmeden var olan sonucu dön.
    # Bu olmadan her retry hem GPT-4o'yu tekrar çağırıyor hem de usage log'unu çift yazıyordu.
    if interview and interview["completed_at"]:
        db.close()
        log_ai_provider(candidate_level, "openai", "report_request_deduped_already_completed")
        return {
            "message": "Mülakat tamamlandı, teşekkür ederiz.",
            "completed": True,
            "score": interview["score"],
            "recommendation": interview["recommendation"],
        }

    db.commit(); db.close()
    # Realtime kullanım özetini kaydet: OpenAI Usage ekranındaki yüksek maliyetin hangi mülakattan
    # geldiğini burada görürüz. NOT: frontend artık burada TÜM oturumun toplamını değil, en son
    # /api/realtime/sync heartbeat'inden bu yana biriken FARKI (delta) gönderiyor — bu yüzden burada
    # tekrar "daha önce yazıldı mı" kontrolüne gerek yok, her çağrı kendi payını ekliyor. Çift rapor
    # üretimi zaten yukarıdaki idempotency guard'ıyla (completed_at) engelleniyor.
    if data.realtime_usage:
        record_realtime_usage_summary(effective_candidate_id, candidate_level, get_realtime_model(candidate_level), data.realtime_usage, action="realtime_final_frontend")
    record_realtime_events(effective_candidate_id, candidate_level, data.events)

    # MADDE 6 — Realtime maliyet yedek kaydı: frontend usage_delta'sı eksikse (heartbeat hatası /
    # sekme erken kapandı) realtime_events'teki ham response.done usage'ından farkı yaz. Çift
    # sayım koruması fonksiyon içinde (realtime_backfill satırı + yalnızca eksik fark).
    try:
        backfill_realtime_cost_from_events(effective_candidate_id, candidate_level, get_realtime_model(candidate_level))
    except Exception as e:
        print(f"UYARI (realtime backfill c={effective_candidate_id}): {type(e).__name__}: {e}")

    # MADDE 5 — Whisper (realtime input_audio_transcription) maliyeti görünürlüğü: adayın
    # transkript edilen konuşma süresi kadar dakika bazlı ayrı kayıt.
    try:
        _spk_sec = _realtime_candidate_speech_seconds(effective_candidate_id, candidate_level)
        if _spk_sec > 0:
            record_flat_usage(effective_candidate_id, candidate_level, "openai", "whisper-1",
                              "realtime_transcription", minutes=_spk_sec / 60.0,
                              raw={"source": "realtime_events speech_started/stopped", "speech_seconds": _spk_sec})
    except Exception as e:
        print(f"UYARI (whisper maliyet kaydı c={effective_candidate_id}): {type(e).__name__}: {e}")

    # KALEM 1 — SUNUCU tarafı halüsinasyon filtresi (frontend filtresi tek savunma hattı olmasın).
    _lang = (candidate["interview_language"] if "interview_language" in candidate.keys() else "tr") or "tr"
    _clean_transcript, _hall_filtered, _hall_n = filter_transcript_hallucinations(data.transcript, _lang)
    if _hall_n:
        record_realtime_events(effective_candidate_id, candidate_level,
                               [{"type": "transcription_filtered_server", "data": f, "elapsed_ms": 0} for f in _hall_filtered])
        print(f"[HALLUCINATION_FILTER server] c={effective_candidate_id} {_hall_n} aday satırı işaretlendi")

    db = get_db()
    # Transkripti (rapor/PDF görüntüleme ve gelecekteki debug için) kaydet — işaretli hali.
    save_interview_state(db, effective_candidate_id, [{"role": "user", "content": _clean_transcript}], candidate_level)
    db.commit()
    db.close()

    _l2_cfg = get_effective_level_config(candidate_level, candidate["depth_tier"] if "depth_tier" in candidate.keys() else "standart")
    pos_for_suff = get_position(candidate["position"]) or {"criteria": [{"name": "Genel Yetkinlik", "weight": 100}]}
    _coverage = data.criteria_coverage if isinstance(data.criteria_coverage, dict) and data.criteria_coverage else None

    # A4: modelin bildirdiği kriter kapsanma yüzdelerini sakla (rapor + admin panel için).
    if _coverage:
        try:
            db = get_db()
            db.execute("UPDATE interviews SET criteria_coverage_json=? WHERE candidate_id=? AND level=?",
                       (json.dumps(_coverage, ensure_ascii=False)[:4000], effective_candidate_id, candidate_level))
            db.commit(); db.close()
        except Exception as e:
            print(f"UYARI (criteria_coverage kaydı c={effective_candidate_id}): {type(e).__name__}: {e}")

    # A5: end_reason'ı transkriptle doğrula — 'aday_talebi' ama açık niyet yoksa 'tamamlandı'ya düşür.
    effective_end_reason, downgraded = validate_end_reason(data.end_reason, _clean_transcript)
    if downgraded:
        record_realtime_events(effective_candidate_id, candidate_level, [{
            "type": "end_reason_downgraded",
            "data": {"raw": data.end_reason, "effective": effective_end_reason,
                     "reason": "transkriptin son aday sözlerinde açık bitirme niyeti bulunamadı"},
            "elapsed_ms": _safe_int(data.duration_seconds) * 1000,
        }])
        print(f"[END_REASON_DOWNGRADE] c={effective_candidate_id} '{data.end_reason}' -> '{effective_end_reason}'")

    # A3: sahte %95 tavanı YOK — gerçek oran (0-100'e sabitlenir). KALEM 1: filtrelenen aday
    # satırları gerçek cevap sayılmaz → answered_count düşülür.
    _eff_answered = max(0, _safe_int(data.answered_count) - _hall_n)
    _completion_pct = min(100, round(_eff_answered / max(1, _l2_cfg["min_q"]) * 100))
    # A1: veri yeterliliği — answered_count + criteria_coverage BİRLİKTE.
    suff = assess_data_sufficiency(_eff_answered, _l2_cfg["min_q"], _coverage, pos_for_suff.get("criteria") or [])

    # end_reason -> insana yönelik etiket + YAPILANDIRILMIŞ OLAY (C2 dar kapsam: bu bir 'kesme' değil, not).
    _reason_meta = {
        "aday_talebi":       ("Aday mülakatı normal kapanıştan önce sonlandırma talebinde bulundu", "termination", "candidate"),
        "baglanti_koptu":    ("Sesli görüşme bağlantısı koptu; mülakat süre dolmadan sonlandı", "technical_failure", "system"),
        "uygunsuz_davranis": ("Mülakatçı görüşmeyi davranış/tutum nedeniyle erken kapattı (gözlem — otomatik puan düşürmez)", "behavior_note", "ai"),
    }
    if effective_end_reason in _reason_meta:
        _desc, _etype, _src = _reason_meta[effective_end_reason]
        _append_result_event(effective_candidate_id, candidate_level, {
            "type": _etype, "subtype": effective_end_reason,
            "elapsed_ms": _safe_int(data.duration_seconds) * 1000,
            "elapsed_minute": round(_safe_int(data.duration_seconds) / 60, 1),
            "description": _desc,
            "snapshot_id": _nearest_snapshot_id(effective_candidate_id, _safe_int(data.duration_seconds) * 1000),
            "weight": "erken sonlandırma" if effective_end_reason != "tamamlandı" else "normal",
            "source": _src,
        })

    # ═══ A2 SIRALAMA — (1) veri yeterli mi? değilse "Değerlendirilemedi" ═══
    if not suff["sufficient"]:
        reason_text = {
            "aday_talebi": "Aday mülakatı kendi isteğiyle erken sonlandırdı ve güvenilir bir değerlendirme için yeterli veri oluşmadı",
            "baglanti_koptu": "Sesli görüşme bağlantısı koptu ve yeterli değerlendirme verisi oluşmadı",
            "uygunsuz_davranis": "Görüşme davranış/tutum nedeniyle erken kapandı ve yeterli değerlendirme verisi oluşmadı",
            "tamamlandı": "Mülakat tamamlandı ancak güvenilir bir değerlendirme için yeterli aday yanıtı oluşmadı",
        }.get(effective_end_reason, "Yeterli değerlendirme verisi oluşmadı")
        reason_text += "; bu nedenle değerlendirme tamamlanamamıştır."
        terminated_reason = _reason_meta.get(effective_end_reason, ("Yetersiz veri", None, None))[0]
        technical_error_ref = f"realtime_connection_lost @ {_now_ts()}" if effective_end_reason == "baglanti_koptu" else None
        record_system_decision(effective_candidate_id, candidate_level, "rapor_uretilmedi_yetersiz_veri", reason_text,
                               {**suff, "end_reason": effective_end_reason, "raw_end_reason": data.end_reason,
                                "downgraded": downgraded, "completion_pct": _completion_pct})
        log_ai_provider(candidate_level, "openai", "report_skipped_insufficient_data")
        report = (f"Aday: {candidate['name']}\nPozisyon: {candidate['position']}\n\nSONUÇ: DEĞERLENDİRİLEMEDİ\n\n"
                  f"{reason_text} Adayın söylemediği hiçbir bilgi eklenmemiş ve otomatik ret kararı verilmemiştir.\n\n"
                  f"Bu mülakatın ~%{_completion_pct} bölümü tamamlanmıştır (cevaplanan tur: {suff['answered_count']} / hedef {suff['min_q']}).")
        return finalize_incomplete_interview(effective_candidate_id, report, terminated_reason=terminated_reason, level=candidate_level,
                                             technical_error_ref=technical_error_ref, completion_pct=_completion_pct, result_reason=reason_text)

    # ═══ (2) veri yeterli → rapor + skor üret. (3) skor < 20 kontrolü finalize_interview'de (A2 adım 3). ═══
    if not OPENAI_API_KEY:
        log_ai_provider(candidate_level, "openai", "report_missing_api_key_fallback")
        reply = build_l2_short_report(candidate["name"], candidate["position"], "OPENAI_API_KEY tanımlı olmadığı için yedek rapor oluşturuldu. Transkript kaydedildi; yönetici transkripti ayrıca incelemelidir.")
        return finalize_interview(effective_candidate_id, reply, terminated_reason=None, level=candidate_level)

    early_note = ""
    if effective_end_reason != "tamamlandı":
        early_note = "\n\n" + {
            "aday_talebi": ("ERKEN SONLANDIRMA (aday talebi): Aday normal kapanıştan önce mülakatı sonlandırdı. Raporu ELDEKİ "
                            "veriyle üret; eksik kalan kriterleri 'değerlendirilemedi' işaretle, bunu TEK BAŞINA puan düşürme "
                            "gerekçesi YAPMA. 'Sonuç Gerekçesi' bölümüne erken sonlandırmayı dakika + söz olarak somut yaz."),
            "baglanti_koptu": ("ERKEN SONLANDIRMA (teknik): Sesli bağlantı koptu. Raporu ELDEKİ veriyle üret; teknik kesintiyi "
                               "belirt, adayı bunun için cezalandırma."),
            "uygunsuz_davranis": ("DAVRANIŞ GÖZLEMİ: Mülakatçı görüşmeyi davranış/tutum nedeniyle erken kapattı. Raporda "
                                  "'Davranış ve Tutum Gözlemleri' başlığı altında SOMUT (dakika + transkriptteki söz) yaz. "
                                  "Davranış puanı otomatik düşürmez; yalnızca yeterince kapsanamayan kriterler 'değerlendirilemedi' sayılır."),
        }.get(effective_end_reason, "")

    if _hall_n:
        early_note += (f"\n\nNOT: Transkriptte {_hall_n} aday satırı '[SİSTEM: olası transkripsiyon halüsinasyonu]' "
                       "olarak işaretlendi. Bu satırları ADAY CEVABI SAYMA, davranış/tutum çıkarımı yapma, "
                       "'aday bye-bye/thank you diyerek erken sonlandırdı' gibi yorum ASLA yazma. "
                       "Bu turlar HİÇBİR kriterin puanını düşürme gerekçesi olamaz — sistem kaynaklı eksiktir.")
    try:
        _rq = detect_repeated_questions(effective_candidate_id, candidate_level)
        if _rq:
            early_note += "\n\nNOT (SİSTEM): Mülakatçı şu kriter(ler)de aynı soruyu ısrarla tekrarladı ve geçerli aday cevabı alınamadı: " \
                          + "; ".join(f"{r['kriter']} ({r['count']} kez)" for r in _rq) \
                          + ". Bu kriterleri 'Değerlendirilemedi (sistem)' say — PUANI DÜŞÜRME gerekçesi DEĞİLDİR, payda dışıdır."
    except Exception as e:
        print(f"UYARI (create_l2_report soru-tekrarı notu c={effective_candidate_id}): {type(e).__name__}: {e}")
    report_prompt = build_l2_report_prompt(candidate, candidate_level, _clean_transcript, criteria_coverage=_coverage, extra_notes=early_note)
    record_system_decision(effective_candidate_id, candidate_level, "rapor_uretiliyor",
                           "Veri yeterli (assess_data_sufficiency); normal rapor yolu.",
                           {**suff, "end_reason": effective_end_reason, "raw_end_reason": data.end_reason,
                            "downgraded": downgraded, "completion_pct": _completion_pct})
    _mark_finish_pending(effective_candidate_id, candidate_level, provider="openai", model=OPENAI_REPORT_MODEL,
                          system=None, payload=report_prompt, terminated_reason=None, reason="l2_normal")
    background_tasks.add_task(run_deferred_finish_job, effective_candidate_id, candidate_level)
    return {
        "message": "Mülakatınız tamamlandı, teşekkür ederiz. Raporunuz hazırlanıyor.",
        "completed": True, "processing": True, "score": None, "recommendation": None,
    }

@app.get("/api/admin/snapshots/{candidate_id}")
def get_snapshots(candidate_id: int, payload=Depends(verify_admin), db=Depends(db_dep)):
    # FAZ D: mimik analiz kareleri (reason='mimic_sample') panelde/PDF'te GÖSTERİLMEZ — bunlar
    # yalnızca arka plan analizi içindir; "4/4 doğrulama karesi" görünümü bozulmasın.
    rows = db.execute(
        "SELECT id, image_base64, captured_at FROM snapshots WHERE candidate_id=? AND (reason IS NULL OR reason<>'mimic_sample') ORDER BY captured_at ASC",
        (candidate_id,)
    ).fetchall()
    return [{"id": r["id"], "image_base64": r["image_base64"], "captured_at": r["captured_at"]} for r in rows]


# ---- PDF Report ----
def _clean_pdf_text(value):
    return (value or "").replace("**", "").replace("---", "").strip()

def format_pdf_datetime(value):
    """Tüm PDF tarih alanları için tek biçim: 15.08.2026 01:17. Postgres'te interviews.started_at/
    completed_at native datetime nesnesi olarak dönüyor (str() ile mikro saniyeli çıplak Python
    biçimine düşüyordu); SQLite'ta metin. İkisini de aynı biçime çevirir."""
    if not value:
        return "-"
    if hasattr(value, "strftime"):
        return value.strftime("%d.%m.%Y %H:%M")
    s = str(value).strip().split(".")[0].replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).strftime("%d.%m.%Y %H:%M")
        except ValueError:
            continue
    return str(value)

def _insert_heading_breaks(text):
    """AI çıktısı bazen '**Başlık:**' kalıbını önceki cümleden satır sonu koymadan
    üretiyor (ör. '...somut değildi.**Güçlü Yönler:**'), bu da PDF'te başlığın önceki
    paragrafa yapışmasına yol açıyor. Yalnızca kalın VE ':' ile biten başlık kalıbını
    hedefler (ör. **Güçlü Yönler:**) — cümle içi kalın vurguyu (':' ile bitmeyen) etkilemez."""
    if not text:
        return text
    return re.sub(r'(?<!\n)(\*\*[^*\n]+:\*\*)', r'\n\1', text)

def _make_report_pdf(candidate: dict, interview: dict, snapshots: list):
    try:
        import glob
        from reportlab.lib import colors as rl_colors
        from reportlab.lib.enums import TA_CENTER
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak, KeepTogether
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF kütüphanesi yüklenemedi: {e}")

    def register_unicode_font():
        # Türkçe karakter için gerçek Unicode TTF gerekir. Helvetica/Vera Türkçe'de kare basabilir.
        # Önce sistemdeki DejaVu/Noto/Liberation fontlarını kullan. Railway/Nixpacks için nixpacks.toml eklendi.
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ]
        bold_candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        ]
        # KÖK NEDEN (doğrulandı yerel testle): nixpacks.toml "dejavu_fonts" Nix paketini
        # /nix/store/<hash>-.../share/fonts/ altına kuruyor; yukarıdaki sabit Debian yolu
        # (/usr/share/fonts/...) Railway'de hiç var olmuyor. Arama sessizce başarısız olup
        # aşağıdaki Vera yedeğine düşülüyordu — Vera Türkçe karakterleri karşılıyor (ş/İ/ğ
        # doğrulandı, bu yüzden başka belirti görülmedi) ama ok karakterini (↔, →) İÇERMİYOR
        # (fontTools ile doğrulandı). Nix store'daki gerçek konumu glob ile ayrıca ara.
        try:
            candidates += sorted(glob.glob("/nix/store/*dejavu*/share/fonts/**/DejaVuSans.ttf", recursive=True))
            bold_candidates += sorted(glob.glob("/nix/store/*dejavu*/share/fonts/**/DejaVuSans-Bold.ttf", recursive=True))
        except Exception as e:
            print(f"UYARI (register_unicode_font: Nix store glob taraması başarısız): {type(e).__name__}: {e}")
        regular = next((f for f in candidates if os.path.exists(f)), None)
        bold = next((f for f in bold_candidates if os.path.exists(f)), None)
        if regular:
            pdfmetrics.registerFont(TTFont("MedeXFont", regular))
            pdfmetrics.registerFont(TTFont("MedeXFont-Bold", bold or regular))
            return "MedeXFont", "MedeXFont-Bold", True  # DejaVu/Noto/Liberation: ok karakteri destekli

        # Son çare: reportlab Vera denenir; Türkçe eksikse loga düşer. Ticari ortamda DejaVu/Noto kurulmalıdır.
        try:
            import reportlab as _rl
            rl_dir = os.path.dirname(_rl.__file__)
            vera_regular = os.path.join(rl_dir, "fonts", "Vera.ttf")
            vera_bold = os.path.join(rl_dir, "fonts", "VeraBd.ttf")
            if os.path.exists(vera_regular):
                pdfmetrics.registerFont(TTFont("MedeXFont", vera_regular))
                pdfmetrics.registerFont(TTFont("MedeXFont-Bold", vera_bold if os.path.exists(vera_bold) else vera_regular))
                print("UYARI: DejaVu/Noto bulunamadı; Vera kullanılıyor. Türkçe karakter desteği sınırlı olabilir.")
                return "MedeXFont", "MedeXFont-Bold", False  # Vera: ok karakteri YOK, ASCII karşılığı kullanılmalı
        except Exception as e:
            print(f"UYARI: PDF fontu yüklenemedi: {e}")

        print("UYARI: Unicode PDF fontu bulunamadı; Türkçe karakterler bozulabilir.")
        return "Helvetica", "Helvetica-Bold", False

    font_regular, font_bold, font_supports_arrow = register_unicode_font()

    def ptxt(value):
        text = str(value if value is not None else "-")
        if not font_supports_arrow:
            text = text.replace("↔", "-").replace("→", "-")
        return xml_escape(text)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=1.4*cm, leftMargin=1.4*cm, topMargin=1.2*cm, bottomMargin=1.1*cm)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="BrandTitle", parent=styles["Title"], fontName=font_bold, fontSize=23, leading=28, textColor=rl_colors.HexColor("#1e3a5f"), alignment=TA_CENTER, spaceAfter=6))
    styles.add(ParagraphStyle(name="Subtitle", parent=styles["BodyText"], fontName=font_regular, fontSize=9, leading=12, textColor=rl_colors.HexColor("#64748b"), alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="Section", parent=styles["Heading2"], fontName=font_bold, fontSize=13, leading=16, textColor=rl_colors.HexColor("#1e3a5f"), spaceBefore=12, spaceAfter=8))
    styles.add(ParagraphStyle(name="Small", parent=styles["BodyText"], fontName=font_regular, fontSize=8, leading=10, textColor=rl_colors.HexColor("#64748b")))
    styles.add(ParagraphStyle(name="BodyWrap", parent=styles["BodyText"], fontName=font_regular, fontSize=9.2, leading=12.5, textColor=rl_colors.HexColor("#0f172a"), wordWrap="CJK"))
    styles.add(ParagraphStyle(name="Metric", parent=styles["BodyText"], fontName=font_bold, fontSize=18, leading=22, alignment=TA_CENTER, textColor=rl_colors.HexColor("#1e3a5f")))
    styles.add(ParagraphStyle(name="MiniHeading", parent=styles["BodyText"], fontName=font_bold, fontSize=9.5, leading=12, textColor=rl_colors.HexColor("#92400e"), spaceBefore=2, spaceAfter=4))

    story = []
    story.append(Paragraph("MedeX AI Interview Report", styles["BrandTitle"]))
    story.append(Paragraph("Aday mülakat değerlendirme raporu", styles["Subtitle"]))
    story.append(Spacer(1, 10))

    score = interview.get("score")
    score_display = "-" if score is None else f"{score}/100"
    # ÇİFT PUANLAMA: stored recommendation authoritative'dir (PUAN 1 + veto zaten işlenmiş).
    # Eski kayıtta yoksa ortalamadan türet.
    recommendation = interview.get("recommendation") or (normalize_recommendation(score or 0) if score is not None else "-")
    s_pos = interview.get("score_position")
    s_prof = interview.get("score_profile")

    if s_prof is not None:
        metric_table = Table([
            [Paragraph("PUAN 1 — POZİSYON", styles["Small"]), Paragraph("PUAN 2 — PROFİL", styles["Small"]),
             Paragraph("ORTALAMA", styles["Small"]), Paragraph("ÖNERİ", styles["Small"])],
            [Paragraph(ptxt("-" if s_pos is None else f"{s_pos}/100"), styles["Metric"]),
             Paragraph(ptxt(f"{s_prof}/100"), styles["Metric"]),
             Paragraph(ptxt(score_display), styles["Metric"]),
             Paragraph(ptxt(recommendation), styles["Metric"])],
        ], colWidths=[4.2*cm, 4.2*cm, 4.2*cm, 4.2*cm])
    else:
        metric_table = Table([
            [Paragraph("SKOR", styles["Small"]), Paragraph("ÖNERİ", styles["Small"])],
            [Paragraph(ptxt(score_display), styles["Metric"]), Paragraph(ptxt(recommendation), styles["Metric"])],
        ], colWidths=[8.4*cm, 8.4*cm])
    metric_table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), rl_colors.HexColor("#f8fafc")),
        ("BOX", (0,0), (-1,-1), 0.5, rl_colors.HexColor("#e2e8f0")),
        ("INNERGRID", (0,0), (-1,-1), 0.25, rl_colors.HexColor("#e2e8f0")),
        ("ALIGN", (0,0), (-1,-1), "CENTER"),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING", (0,0), (-1,-1), 8),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
    ]))
    story.append(metric_table)
    story.append(Spacer(1, 10))

    # Kimlik/oturum bilgileri: nötr, tek kaynaklı (candidates tablosu + interview zaman damgaları),
    # çelişki üretmeyen alanlar.
    info = [
        ["Aday", candidate.get("name") or "-", "Pozisyon", candidate.get("position") or "-"],
        ["E-posta", candidate.get("email") or "-", "Telefon", candidate.get("phone") or "-"],
        ["Başlangıç", format_pdf_datetime(interview.get("started_at")), "Tamamlanma", format_pdf_datetime(interview.get("completed_at"))],
    ]
    t = Table([[Paragraph(ptxt(c), styles["BodyWrap"]) for c in row] for row in info], colWidths=[2.7*cm, 5.8*cm, 2.9*cm, 5.4*cm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), rl_colors.HexColor("#ffffff")),
        ("GRID", (0,0), (-1,-1), 0.35, rl_colors.HexColor("#e2e8f0")),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("FONTNAME", (0,0), (0,-1), font_bold),
        ("FONTNAME", (2,0), (2,-1), font_bold),
        ("TOPPADDING", (0,0), (-1,-1), 7),
        ("BOTTOMPADDING", (0,0), (-1,-1), 7),
    ]))
    story.append(t)

    # Başvuru Formu Beyanı: candidates.education/experience_years/university/department —
    # adayın/adminin form üzerinden girdiği beyan. AI'ın CV metninden çıkardığı bilgi (rapor
    # gövdesi ve Standart CV) ayrı, bilinçli olarak burada uzlaştırılmıyor — iki farklı kaynak
    # olduğu görsel olarak (ayrı blok, ayrı renk, ayrı başlık) belli edilir. Boş alan hiç basılmaz.
    declared_fields = [
        ("Eğitim", candidate.get("education")),
        ("Deneyim", (f"{candidate.get('experience_years')} yıl" if candidate.get("experience_years") else None)),
        ("Üniversite", candidate.get("university")),
        ("Bölüm", candidate.get("department")),
    ]
    declared_rows = [(label, value) for label, value in declared_fields if value]
    if declared_rows:
        story.append(Spacer(1, 8))
        story.append(Paragraph("BAŞVURU FORMU BEYANI", styles["MiniHeading"]))
        dt = Table(
            [[Paragraph(ptxt(label), styles["BodyWrap"]), Paragraph(ptxt(value), styles["BodyWrap"])] for label, value in declared_rows],
            colWidths=[3.0*cm, 13.8*cm],
        )
        dt.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), rl_colors.HexColor("#fffbeb")),
            ("GRID", (0,0), (-1,-1), 0.35, rl_colors.HexColor("#fde68a")),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("FONTNAME", (0,0), (0,-1), font_bold),
            ("TOPPADDING", (0,0), (-1,-1), 6),
            ("BOTTOMPADDING", (0,0), (-1,-1), 6),
        ]))
        story.append(dt)

    # BÖLÜM 3 — yapılandırılmış Sonuç Gerekçesi / İhlal Kaydı
    # KALEM 3 — geri alınmış / yinelenen kayıtlar rapora basılmaz.
    try:
        _events = visible_result_events(json.loads(interview.get("result_events_json") or "[]"))
    except Exception:
        _events = []
    _rreason = (interview.get("result_reason") or "").strip()
    _partial = _safe_int(interview.get("partial"))
    _cpct = interview.get("completion_pct")
    if _events or _rreason or _partial:
        story.append(Spacer(1, 8))
        story.append(Paragraph("Sonuç Gerekçesi / İhlal Kaydı", styles["Section"]))
        if _partial:
            pct_txt = f" — yaklaşık %{_cpct} tamamlandı" if _cpct is not None else ""
            story.append(Paragraph(f"<b>Kısmi mülakat{pct_txt}.</b>", styles["BodyWrap"]))
        if _rreason and not _rreason.startswith("[EKSİK"):
            story.append(Paragraph(ptxt(_rreason), styles["BodyWrap"]))
        elif _rreason.startswith("[EKSİK"):
            story.append(Paragraph("<b>UYARI:</b> Sistem bu olumsuz sonuç için otomatik gerekçe üretemedi; transkript incelenmelidir.", styles["BodyWrap"]))
        for ev in _events[:12]:
            mm = ev.get("elapsed_minute")
            when = f"{mm}. dk" if mm is not None else (ev.get("occurred_at") or "-")
            snap = f" · Kamera karesi #{ev['snapshot_id']}" if ev.get("snapshot_id") else ""
            story.append(Paragraph(
                f"• <b>[{when}]</b> {ptxt(ev.get('description') or ev.get('subtype') or ev.get('type'))} "
                f"<font size=7>({ptxt(ev.get('weight') or '-')}{snap})</font>", styles["BodyWrap"]))
        if interview.get("technical_error_ref"):
            story.append(Paragraph(f"<font size=7>Teknik referans (yalnızca yönetici): {ptxt(interview.get('technical_error_ref'))}</font>", styles["Small"]))
    elif candidate.get("terminated_reason"):
        story.append(Spacer(1, 8))
        story.append(Paragraph(f"<b>İhlal/Sonlandırma:</b> {ptxt(candidate.get('terminated_reason'))}", styles["BodyWrap"]))

    def is_heading_line(line):
        return line.endswith(":") or line.startswith("TOPLAM PUAN") or line.startswith("Öneri:")

    def flow_report_lines(lines, consumed=None):
        # Başlığın (ör. "Güçlü Yönler:") sayfa sonunda yalnız kalıp gövde metninin bir
        # sonraki sayfaya taşmasını önlemek için başlığı, kendinden sonraki ilk paragrafla
        # birlikte KeepTogether içine alır — ikisi birden sayfaya sığmıyorsa ikisi birden
        # bir sonraki sayfaya geçer, başlık yalnız kalmaz.
        consumed = consumed or set()
        flowables = []
        i, n = 0, len(lines)
        while i < n:
            if i in consumed or lines[i].startswith("|"):
                i += 1
                continue
            line = lines[i]
            is_heading = is_heading_line(line)
            para = Paragraph(("<b>" + ptxt(line) + "</b>") if is_heading else ptxt(line), styles["BodyWrap"])
            if is_heading:
                j = i + 1
                while j < n and (j in consumed or lines[j].startswith("|")):
                    j += 1
                if j < n and not is_heading_line(lines[j]):
                    next_para = Paragraph(ptxt(lines[j]), styles["BodyWrap"])
                    flowables.append(KeepTogether([para, Spacer(1, 3), next_para, Spacer(1, 3)]))
                    i = j + 1
                    continue
            flowables.append(para)
            flowables.append(Spacer(1, 3))
            i += 1
        return flowables

    report_text = strip_markdown(_insert_heading_breaks(interview.get("report"))) or "Rapor bulunamadı."
    story.append(Paragraph("AI Değerlendirme Raporu", styles["Section"]))
    lines = [ln.rstrip() for ln in report_text.split("\n") if ln.strip()]

    def _emit_report_block(block_lines):
        tr, tc = parse_markdown_table(block_lines)
        if tr:
            story.extend(flow_report_lines(block_lines, consumed=tc))
            clean_rows = []
            for row in tr:
                if any("Kriter" in c for c in row) or len(row) >= 3:
                    clean_rows.append(row[:3])
            if len(clean_rows) >= 2:
                story.append(Spacer(1, 6))
                rt = Table([[Paragraph(ptxt(c), styles["BodyWrap"]) for c in row] for row in clean_rows], colWidths=[5.0*cm, 2.1*cm, 9.0*cm])
                rt.setStyle(TableStyle([
                    ("BACKGROUND", (0,0), (-1,0), rl_colors.HexColor("#eff6ff")),
                    ("FONTNAME", (0,0), (-1,0), font_bold),
                    ("GRID", (0,0), (-1,-1), 0.3, rl_colors.HexColor("#dbeafe")),
                    ("VALIGN", (0,0), (-1,-1), "TOP"),
                    ("TOPPADDING", (0,0), (-1,-1), 5),
                    ("BOTTOMPADDING", (0,0), (-1,-1), 5),
                ]))
                story.append(rt)
        else:
            story.extend(flow_report_lines(block_lines))

    # ÇİFT PUANLAMA: PUAN 1 (pozisyon) ve PUAN 2 (profil) AYRI blok işlenir — iki kriter tablosu
    # tek tabloda birleşmesin.
    _p2_idx = next((i for i, ln in enumerate(lines)
                    if re.search(r'(^|[^A-Za-zÇĞİÖŞÜçğıöşü])PUAN\s*2\b', ln, re.IGNORECASE)
                    or re.search(r'PROF\S*\s+PUANI', ln, re.IGNORECASE)), None)
    if _p2_idx is not None and _p2_idx > 0:
        _emit_report_block(lines[:_p2_idx])
        story.append(Spacer(1, 10))
        _emit_report_block(lines[_p2_idx:])
    else:
        _emit_report_block(lines)

    if interview.get("standard_cv"):
        story.append(Paragraph("Standart CV Özeti (CV'den Çıkarım)", styles["Section"]))
        cv_text = strip_markdown(_insert_heading_breaks(interview.get("standard_cv")))
        cv_lines = [ln.rstrip() for ln in cv_text.split("\n") if ln.strip()]
        story.extend(flow_report_lines(cv_lines))

    # BÖLÜM 2.3 — Konuşma Metni (Transkript) — KALEM 5: iç sistem satırları çıkarılır
    try:
        _tview = build_transcript_view(interview.get("messages") or "[]", interview.get("level") or 1, interview.get("started_at"), for_report=True)
    except Exception as e:
        print(f"UYARI (PDF transkript görünümü): {type(e).__name__}: {e}")
        _tview = []
    story.append(PageBreak())
    story.append(Paragraph(f"Konuşma Metni (Transkript) — {len(_tview)} satır", styles["Section"]))
    if not _tview:
        story.append(Paragraph("Bu mülakat için kayıtlı konuşma metni bulunamadı.", styles["BodyWrap"]))
    else:
        for row in _tview:
            who = "Aday" if row["role"] == "aday" else "Mülakatçı"
            stamp = f"[{row['ts']}] " if row.get("ts") else ""
            story.append(Paragraph(f"<font size=7 color='#64748b'>{ptxt(stamp)}</font><b>{who}:</b> {ptxt(row['text'])}", styles["BodyWrap"]))
            story.append(Spacer(1, 2))

    story.append(PageBreak())
    story.append(Paragraph(f"Kamera Doğrulama Kareleri ({len(snapshots[:4])}/4)", styles["Section"]))
    if not snapshots:
        story.append(Paragraph("Bu mülakat için kayıtlı kamera karesi bulunamadı.", styles["BodyWrap"]))
    else:
        rows = []
        row = []
        for idx, snap in enumerate(snapshots[:4], start=1):
            try:
                data_url = snap.get("image_base64", "")
                raw = data_url.split(",", 1)[1] if "," in data_url else data_url
                img_bytes = base64.b64decode(raw)
                img = Image(io.BytesIO(img_bytes), width=7.4*cm, height=5.4*cm)
                cell = [Paragraph(f"<b>Kare {idx}</b><br/><font size=7>{ptxt(format_pdf_datetime(snap.get('captured_at')))}</font>", styles["Small"]), img]
                row.append(cell)
                if len(row) == 2:
                    rows.append(row); row = []
            except Exception as e:
                print(f"UYARI (PDF kamera karesi eklenemedi, kare {idx}): {type(e).__name__}: {e}")
        if row:
            row.append("")
            rows.append(row)
        if rows:
            img_table = Table(rows, colWidths=[8.4*cm, 8.4*cm])
            img_table.setStyle(TableStyle([("VALIGN", (0,0), (-1,-1), "TOP"), ("GRID", (0,0), (-1,-1), 0.25, rl_colors.HexColor("#e2e8f0")), ("PADDING", (0,0), (-1,-1), 8)]))
            story.append(img_table)

    # KALEM 5 — teknik not (yalnız yönetici PDF'i): token kesilmesi vb.
    if interview.get("report_tech_note"):
        story.append(Spacer(1, 8))
        story.append(Paragraph(f"<font size=7>Teknik not (yalnızca yönetici): {ptxt(interview.get('report_tech_note'))}</font>", styles["Small"]))

    story.append(Spacer(1, 14))
    # KALEM 4 — mülakat tarihi/saati (started_at–completed_at) ile RAPOR üretim tarihi ayrı satırlar.
    _gen_at = interview.get("report_generated_at") or interview.get("report_regenerated_at")
    _gen_txt = f" (rapor {format_pdf_datetime(_gen_at)} tarihinde üretildi)" if _gen_at else ""
    story.append(Paragraph(f"Bu rapor {datetime.now().strftime('%d.%m.%Y %H:%M')} tarihinde MedeX AI Interview Platform tarafından oluşturulmuştur.{_gen_txt}", styles["Small"]))

    doc.build(story)
    buffer.seek(0)
    return buffer

@app.get("/api/admin/interviews/{candidate_id}/pdf")
def download_interview_pdf(candidate_id: int, level: Optional[int] = None, payload=Depends(verify_admin), db=Depends(db_dep)):
    scoped_org_id = get_org_id_for_admin(db, payload)
    candidate = db.execute("SELECT * FROM candidates WHERE id=? AND org_id=?", (candidate_id, scoped_org_id)).fetchone()
    target_level = level if level is not None else ((candidate["level"] or 1) if candidate else 1)
    interview = db.execute("SELECT * FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, target_level)).fetchone()
    snapshots = db.execute("SELECT id, image_base64, captured_at FROM snapshots WHERE candidate_id=? AND (reason IS NULL OR reason<>'mimic_sample') ORDER BY captured_at ASC", (candidate_id,)).fetchall()
    if not candidate or not interview:
        raise HTTPException(status_code=404, detail="Rapor bulunamadı")

    # YETERSİZ VERİ / YARIM MÜLAKAT: skor yoksa ya da %20 barajının altındaysa detaylı
    # değerlendirme PDF'i üretilmez — AMA yapılandırılmış bir Sonuç Gerekçesi veya transkript
    # varsa (BÖLÜM 2/3) PDF yine üretilir: denetlenebilirlik için gerekçeli/kısmi rapor da indirilebilmeli.
    pos = get_position(candidate["position"], org_id=candidate["org_id"] if "org_id" in candidate.keys() else None)
    total_weight = sum(c["weight"] for c in pos["criteria"]) if pos else 100
    score = interview["score"]
    processing_status = interview["processing_status"] if "processing_status" in interview.keys() else None
    has_reasoned_result = bool(
        (interview["result_events_json"] if "result_events_json" in interview.keys() else None)
        or ((interview["result_reason"] or "").strip() if "result_reason" in interview.keys() else "")
        or (interview["messages"] and interview["messages"] not in ("[]", ""))
    )
    if (score is None or (total_weight > 0 and (score / total_weight) < 0.20)) and not has_reasoned_result:
        if processing_status == "processing":
            msg = "Rapor hazırlanıyor, birkaç dakika içinde tekrar deneyin."
        elif processing_status == "failed":
            msg = "Rapor üretimi başarısız oldu; yönetici tekrar denemesi gerekiyor."
        elif score is None:
            msg = "Mülakat tamamlanmadığı için değerlendirme oluşturulamamıştır."
        else:
            msg = "Bu mülakat sonucunda aday hakkında güvenilir bir değerlendirme oluşturabilecek yeterli veri elde edilememiştir. Bu nedenle ayrıntılı rapor oluşturulmamıştır."
        raise HTTPException(status_code=422, detail=msg)
    if processing_status == "processing":
        raise HTTPException(status_code=422, detail="Rapor hazırlanıyor, birkaç dakika içinde tekrar deneyin.")

    pdf = _make_report_pdf(dict(candidate), dict(interview), [dict(s) for s in snapshots])
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", candidate["name"] or "aday")
    return StreamingResponse(pdf, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename=medex_report_{safe_name}.pdf"})

# ---- Admin Report Detail ----
@app.get("/api/admin/interviews/{candidate_id}")
def get_interview(candidate_id: int, level: Optional[int] = None, payload=Depends(verify_admin), db=Depends(db_dep)):
    scoped_org_id = get_org_id_for_admin(db, payload)
    c = db.execute("SELECT level FROM candidates WHERE id=? AND org_id=?", (candidate_id, scoped_org_id)).fetchone()
    if not c:
        raise HTTPException(status_code=404, detail="Mülakat bulunamadı")
    if level is None:
        level = c["level"] or 1
    interview = db.execute("""
        SELECT i.*, c.name, c.email, c.phone, c.position, c.education, c.university, c.department, c.experience_years, c.ai_note, c.violation_count, c.terminated_reason, c.cv_filename, c.cv_text
        FROM interviews i JOIN candidates c ON i.candidate_id = c.id
        WHERE i.candidate_id = ? AND i.level = ?
    """, (candidate_id, level)).fetchone()
    usage_rows = db.execute("""
        SELECT provider, model, action, input_tokens, output_tokens, audio_input_tokens, audio_output_tokens,
               cached_input_tokens, cached_audio_input_tokens, total_tokens, estimated_cost_usd, created_at
        FROM ai_usage_logs
        WHERE candidate_id=? AND level=?
        ORDER BY id ASC
    """, (candidate_id, level)).fetchall()
    if not interview:
        raise HTTPException(status_code=404, detail="Mülakat bulunamadı")
    result = dict(interview)
    result["usage_logs"] = [dict(r) for r in usage_rows]
    result["usage_total_tokens"] = sum(_safe_int(r["total_tokens"]) for r in usage_rows)
    result["usage_total_cost_usd"] = round(sum((r["estimated_cost_usd"] or 0) for r in usage_rows), 4)
    # Cache oranı: cache'lenebilir toplam girdi (input + audio_input) içindeki cache'li pay.
    cacheable_input = sum(_safe_int(r["input_tokens"]) + _safe_int(r["audio_input_tokens"]) for r in usage_rows)
    cached_input = sum(_safe_int(r["cached_input_tokens"]) + _safe_int(r["cached_audio_input_tokens"]) for r in usage_rows)
    result["usage_cached_input_tokens"] = cached_input
    result["usage_cache_hit_pct"] = round(cached_input / cacheable_input * 100, 1) if cacheable_input > 0 else None

    # BÖLÜM 2.2 — kronolojik konuşma metni (L1/L3 dizi + L2 blob normalize)
    # KALEM 5 — rapor transkriptinde iç sistem satırları ('[SİSTEM: ... halüsinasyon]') gösterilmez.
    result["transcript"] = build_transcript_view(
        interview["messages"] if "messages" in interview.keys() else "[]",
        level,
        interview["started_at"] if "started_at" in interview.keys() else None,
        for_report=True,
    )
    # BÖLÜM 3 — sonuç gerekçesi / ihlal kaydı / kısmi mülakat (admin görünür)
    # KALEM 3 — geri alınmış (corrected) / yinelenen kayıtlar rapora/panele BASILMAZ.
    try:
        result["result_events"] = visible_result_events(
            json.loads(interview["result_events_json"]) if ("result_events_json" in interview.keys() and interview["result_events_json"]) else [])
    except Exception:
        result["result_events"] = []
    _rr = (result.get("result_reason") or "").strip()
    result["result_reason_missing"] = _rr.startswith("[EKSİK")

    # ═══ BÖLÜM D1 — tam oturum kaydı (admin görünür, ham) ═══
    def _loadj(col):
        try:
            v = interview[col] if col in interview.keys() else None
            return json.loads(v) if v else None
        except Exception:
            return None
    result["criteria_coverage"] = _loadj("criteria_coverage_json")
    result["criterion_attempts"] = _loadj("criterion_attempts_json")     # B2 (L1 metin)
    result["system_decision"] = _loadj("system_decision_json")           # A1/A5: neden rapor üretildi/üretilmedi
    try:
        ev_rows = db.execute(
            "SELECT event_type, event_data, elapsed_ms, created_at FROM realtime_events WHERE candidate_id=? AND level=? ORDER BY id ASC",
            (candidate_id, level)
        ).fetchall()
    except Exception:
        ev_rows = []
    rt_events = []
    for r in ev_rows:
        try:
            d = json.loads(r["event_data"]) if r["event_data"] else {}
        except Exception:
            d = {}
        rt_events.append({"type": r["event_type"], "data": d, "elapsed_ms": r["elapsed_ms"], "created_at": r["created_at"]})
    result["realtime_events"] = rt_events
    # Türetilmiş alt-listeler (admin panelde ayrı bölümler)
    result["filtered_transcriptions"] = [e for e in rt_events if e["type"] == "transcription_filtered"]           # B1
    result["tool_calls"] = [e for e in rt_events if e["type"] in ("end_interview", "note_voice_observation", "tool_call")]
    result["connection_events"] = [e for e in rt_events if e["type"] in (
        "session.created", "end_reason_downgraded", "conversation.item.truncated",
        "input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped")]
    try:
        snap_rows = db.execute(
            "SELECT id, elapsed_ms, reason, captured_at FROM snapshots WHERE candidate_id=? AND (reason IS NULL OR reason<>'mimic_sample') ORDER BY COALESCE(elapsed_ms,0) ASC, id ASC",
            (candidate_id,)
        ).fetchall()
        result["camera_frames"] = [{"id": r["id"], "elapsed_ms": r["elapsed_ms"], "reason": r["reason"], "captured_at": r["captured_at"]} for r in snap_rows]
    except Exception:
        result["camera_frames"] = []
    # KALEM 3 + EK: modalite kapsamı (mimik kare dağılımı + ses metrik var/yok) ve yeniden üretim zamanı
    try:
        result["modality_coverage"] = compute_modality_coverage(candidate_id, level)
    except Exception:
        result["modality_coverage"] = None
    result["report_regenerated_at"] = interview["report_regenerated_at"] if "report_regenerated_at" in interview.keys() else None
    return result

@app.post("/api/admin/interviews/{candidate_id}/regenerate-report")
def regenerate_report(candidate_id: int, background_tasks: BackgroundTasks, level: Optional[int] = None, payload=Depends(verify_admin), db=Depends(db_dep)):
    """A6: KAYITLI transkriptten raporu yeniden üretir. Yeni sesli oturum AÇMAZ, adaya dokunmaz.
    Faz D katmanları (mimik/ses metrikleri) build_modality_evidence_block üzerinden zaten dahil
    edilir (run_deferred_finish_job içinde). İdempotent değildir — her çağrı yeni rapor üretir."""
    scoped_org_id = get_org_id_for_admin(db, payload)
    cand = db.execute("SELECT * FROM candidates WHERE id=? AND org_id=?", (candidate_id, scoped_org_id)).fetchone()
    if not cand:
        raise HTTPException(status_code=404, detail="Aday bulunamadı")
    if level is None:
        level = cand["level"] or 1
    interview = db.execute("SELECT * FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, level)).fetchone()
    if not interview:
        raise HTTPException(status_code=404, detail="Bu seviye için mülakat kaydı yok")

    tview = build_transcript_view(interview["messages"] if "messages" in interview.keys() else "[]", level,
                                  interview["started_at"] if "started_at" in interview.keys() else None)
    transcript_text = transcript_to_text(tview)
    if not transcript_text or len(transcript_text.strip()) < 40:
        raise HTTPException(status_code=400, detail="Kayıtlı transkript yok veya rapor üretmek için çok kısa")

    _lang = cand["interview_language"] or "tr"
    _regen_notes = []

    # KALEM 1a — kayıtlı transkript halüsinasyon filtresinden geçsin (canlı akışta atlanmış olabilir).
    clean_transcript, hall_filtered, hall_n = filter_transcript_hallucinations(transcript_text, _lang)
    if hall_n:
        _regen_notes.append(f"{hall_n} aday satırı olası transkripsiyon halüsinasyonu olarak işaretlendi (bunlar aday cevabı sayılmadı).")
        record_realtime_events(candidate_id, level,
                               [{"type": "transcription_filtered_server", "data": f, "elapsed_ms": 0} for f in hall_filtered])
        try:
            fixed_msgs = [{"role": "user", "content": clean_transcript}]
            db.execute("UPDATE interviews SET messages=? WHERE candidate_id=? AND level=?",
                       (json.dumps(fixed_msgs, ensure_ascii=False), candidate_id, level))
            db.commit()
        except Exception as e:
            print(f"UYARI (regenerate: temiz transkript yazımı c={candidate_id}): {type(e).__name__}: {e}")

    # KALEM 1b — end_reason kayıtlı transkriptle yeniden doğrulansın.
    try:
        _events = json.loads(interview["result_events_json"]) if ("result_events_json" in interview.keys() and interview["result_events_json"]) else []
    except Exception:
        _events = []
    _had_aday_talebi = any((e.get("subtype") == "aday_talebi") or ("erken" in (e.get("description") or "").lower() and (e.get("source") == "candidate"))
                           for e in _events if isinstance(e, dict))
    _old_reason = (interview["result_reason"] or "") if "result_reason" in interview.keys() else ""
    _reason_says_early = bool(re.search(r"erken sonland|kendi isteğiyle|aday.*talebi|talebiyle", _old_reason, re.IGNORECASE))
    # KALEM 2 — düzeltme İDEMPOTENT: zaten düzeltilmişse etiket/olay TEKRAR eklenmez.
    _already_corrected = any(isinstance(e, dict) and e.get("corrected") for e in _events) \
                         or "yeniden üretiminde düzeltildi" in _old_reason.lower()
    corrected_end_reason = _already_corrected
    if (_had_aday_talebi or _reason_says_early) and not _already_corrected:
        eff, downgraded = validate_end_reason("aday_talebi", clean_transcript)
        last_line = next((l for l in reversed(clean_transcript.splitlines()) if l.strip()), "")
        mulakatci_closing = bool(re.search(r"Mülakatçı\s*:", last_line) and re.search(r"tamamla|noktala|teşekkür|sona er|bitir", last_line, re.IGNORECASE))
        if downgraded or mulakatci_closing:
            corrected_end_reason = True
            _regen_notes.append("Erken sonlandırma tespiti GERİ ALINDI: transkriptin son sözü mülakatçı kapanışıdır, "
                                "adayda açık bitirme talebi yok.")
            for e in _events:
                if isinstance(e, dict) and (e.get("subtype") == "aday_talebi" or e.get("type") == "termination") and not e.get("corrected"):
                    e["corrected"] = True
                    # KALEM 3 — prefix idempotent: kaç kez yeniden üretilirse üretilsin ibare BİR kez.
                    e["description"] = idempotent_regen_prefix(e.get("description") or "")
            # KALEM 3 — aynı geri-alma kaydı iki zaman damgasıyla İKİ KEZ eklenmesin: alt tür ne olursa
            # olsun mevcut end_reason_downgraded kaydını GÜNCELLE, yenisini EKLEME.
            _dg = next((e for e in _events if isinstance(e, dict) and e.get("type") == "end_reason_downgraded"), None)
            if _dg:
                _dg["occurred_at"] = _now_ts()
                _dg["subtype"] = "regenerate"
                _dg["description"] = "Geriye dönük yeniden üretimde erken-sonlandırma tespiti hatalı bulundu ve geri alındı."
            else:
                _events.append({"type": "end_reason_downgraded", "subtype": "regenerate",
                                "occurred_at": _now_ts(), "source": "system",
                                "description": "Geriye dönük yeniden üretimde erken-sonlandırma tespiti hatalı bulundu ve geri alındı."})
            # birden fazla end_reason_downgraded birikmişse ilki hariç hepsini at
            _seen_dg = False
            _dedup = []
            for e in _events:
                if isinstance(e, dict) and e.get("type") == "end_reason_downgraded":
                    if _seen_dg:
                        continue
                    _seen_dg = True
                _dedup.append(e)
            _events = _dedup
            try:
                db.execute("UPDATE interviews SET result_events_json=?, result_reason=?, partial=0 WHERE candidate_id=? AND level=?",
                           (json.dumps(_events, ensure_ascii=False)[:12000],
                            "Mülakat normal tamamlandı (rapor yeniden üretiminde düzeltildi: önceki 'erken sonlandırma' tespiti hatalıydı).",
                            candidate_id, level))
                db.commit()
            except Exception as e:
                print(f"UYARI (regenerate: olay/result_reason düzeltme c={candidate_id}): {type(e).__name__}: {e}")

    try:
        _cov = json.loads(interview["criteria_coverage_json"]) if ("criteria_coverage_json" in interview.keys() and interview["criteria_coverage_json"]) else None
    except Exception:
        _cov = None

    _regen_note_txt = ("\n\nNOT: Bu rapor, yönetici talebiyle KAYITLI transkriptten YENİDEN üretiliyor."
                       + ("".join(f"\n- {n}" for n in _regen_notes) if _regen_notes else "")
                       + ("\n- İşaretli '[SİSTEM: olası halüsinasyon]' satırları ADAY CEVABI SAYMA." if hall_n else ""))

    use_openai_voice = (level == 2) or (level == 3)
    if use_openai_voice:
        if not OPENAI_API_KEY:
            raise HTTPException(status_code=503, detail="OPENAI_API_KEY tanımlı değil")
        prompt = build_l2_report_prompt(cand, level, clean_transcript, criteria_coverage=_cov, extra_notes=_regen_note_txt)
        prov, mdl, system = "openai", OPENAI_REPORT_MODEL, None
    else:
        if not ANTHROPIC_API_KEY:
            raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY tanımlı değil")
        system = get_system_prompt(cand["position"], cand["name"], cand["cv_text"], cand["ai_note"],
                                   cand["education"], cand["university"], cand["department"], cand["experience_years"],
                                   level, cand["interview_language"] or "tr", cand["report_language"] or "tr",
                                   (cand["depth_tier"] if "depth_tier" in cand.keys() else "standart") or "standart",
                                   email=(cand["email"] if "email" in cand.keys() else None))
        prompt = (f"GÖREV: Aşağıdaki tam transkriptten mülakatı bitir ve raporu üret (yönetici talebiyle YENİDEN üretim). "
                  f"Elindeki veriyle adil değerlendir; sorulmamış kriterleri 'değerlendirilemedi' işaretle. [MÜLAKATBİTTİ] etiketini kullan.{_regen_note_txt}\n\n"
                  f"=== TAM TRANSKRİPT ===\n{clean_transcript[:TRANSCRIPT_PROMPT_MAX_CHARS]}")
        prov, mdl = "claude", "claude-sonnet-4-6"

    # EK — completed_at (orijinal bitiş saati) EZİLMEZ. run_deferred_finish_job regen=True ile
    # guard'ı atlar; finalize_interview regen=True completed_at'e dokunmaz, report_regenerated_at yazar.
    _term = None if corrected_end_reason else (cand["terminated_reason"] if "terminated_reason" in cand.keys() else None)
    _mark_finish_pending(candidate_id, level, provider=prov, model=mdl, system=system, payload=prompt,
                         terminated_reason=_term, reason="admin_regenerate")
    record_system_decision(candidate_id, level, "rapor_yeniden_uretiliyor",
                           f"Yönetici ({payload.get('email') or 'admin'}) kayıtlı transkriptten yeniden üretim başlattı.",
                           {"level": level, "hallucination_filtered": hall_n, "end_reason_corrected": corrected_end_reason},
                           warnings=_regen_notes)
    background_tasks.add_task(run_deferred_finish_job, candidate_id, level, True)
    return {"message": "Rapor yeniden üretiliyor; birkaç dakika içinde hazır olacak.", "processing": True,
            "duzeltmeler": _regen_notes}

@app.get("/api/admin/interviews/{candidate_id}/transcript")
def download_interview_transcript(candidate_id: int, level: Optional[int] = None, payload=Depends(verify_admin), db=Depends(db_dep)):
    """BÖLÜM 2.3 — mülakat konuşma metnini düz metin olarak indirir. HİÇBİR baraj yok:
    düşük skorlu, yarım veya ihlal ile biten mülakatların transkripti de indirilebilir."""
    scoped_org_id = get_org_id_for_admin(db, payload)
    cand = db.execute("SELECT id, name, level FROM candidates WHERE id=? AND org_id=?", (candidate_id, scoped_org_id)).fetchone()
    if not cand:
        raise HTTPException(status_code=404, detail="Aday bulunamadı")
    target_level = level if level is not None else (cand["level"] or 1)
    iv = db.execute("SELECT messages, started_at, completed_at FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, target_level)).fetchone()
    view = build_transcript_view(iv["messages"] if iv else "[]", target_level, iv["started_at"] if iv else None)
    header = f"MedeX Mülakat — Konuşma Metni\nAday: {cand['name']}\nSeviye: {target_level}\nBaşlangıç: {iv['started_at'] if iv else '-'}\nBitiş: {iv['completed_at'] if iv and iv['completed_at'] else '(tamamlanmadı)'}\n" + ("-" * 60) + "\n\n"
    body = transcript_to_text(view) or "(Bu mülakat için kayıtlı konuşma metni yok.)"
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", cand["name"] or "aday")
    return Response(content=(header + body), media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="MedeX_Transkript_{safe_name}_L{target_level}.txt"'})

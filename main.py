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
import random
import jwt
import anthropic
import httpx
import resend
import asyncio
from datetime import datetime, timedelta
import json
import re
import difflib
import io
import time
import base64
import traceback
from xml.sax.saxutils import escape as xml_escape
from decimal import Decimal, ROUND_HALF_UP

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
# İŞ EMRİ — L1 OPENAI-ONLY MİMARİSİ: L1 metin mülakatının CANLI sohbet turları (start_interview +
# interview_chat) için ayrı model. OPENAI_REPORT_MODEL ile aynı varsayılanı paylaşır (gpt-4o) ama
# ayrı env ile bağımsız ayarlanabilsin diye kendi anahtarı var — rapor modeliyle karıştırılmaz.
OPENAI_L1_INTERVIEW_MODEL = os.getenv("OPENAI_L1_INTERVIEW_MODEL", "gpt-4o")
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
        ("interviews", "technical_annex", "TEXT"),  # TUR 3 / GÖREV 5 — ham metrikler/JSON; rapor gövdesinden AYRI, PDF'te en sonda "Teknik Ek (yalnızca yönetici)"
        ("interviews", "transcript_raw", "TEXT"),  # TUR 4 / GÖREV 2 — BİR KEZ yazılır (mülakat bitişinde/ilk backfill'de), bir daha ÜZERİNE YAZILMAZ; her temizleme HER SEFERİNDE bu ham veriden başlar
        ("interviews", "reviewer_score_position", "INTEGER"),  # 2026-09 rapor yeniden tasarımı — 2. değerlendiricinin türetilmiş pozisyon puanı (Değerlendirme Puanları tablosu + Genel Puan ortalaması için)
        ("interviews", "reviewer_score_profile", "INTEGER"),  # 2026-09 rapor yeniden tasarımı — 2. değerlendiricinin türetilmiş profil puanı
        ("candidates", "person_id", "BIGINT" if USE_POSTGRES else "INTEGER"),
        ("candidates", "org_id", "BIGINT" if USE_POSTGRES else "INTEGER"),
        ("positions", "org_id", "BIGINT" if USE_POSTGRES else "INTEGER"),
        # B7 — panelden düzenlenmiş pozisyonlar deploy'da (init_db forced-update) EZİLMESİN.
        ("positions", "is_customized", "INTEGER DEFAULT 0"),
        # İŞ EMRİ — FINAL EVALUATION ARCHITECTURE / CANONICAL FINAL SCORE — FATAL AUDIT'in kesin
        # bulgusu: blended Position/Profile RENDER anında hesaplanıyor, hiçbir DB sütununda
        # persist EDİLMİYORDU (aynı raporda "Pozisyon Puanı" adıyla 3 farklı semantik değer
        # görünmesinin kök nedeni). Artık final_score_position/final_score_profile TEK canonical
        # kaynak: L1/L2'de = primary (second evaluator yok); L3'te = primary + VALIDATED Claude
        # reviewer'dan deterministik türetilir (bkz. _persist_final_scores). Render sırasında
        # gizli/yeniden hesaplama YOK — her yüzey (rapor metni/admin/PDF) BU sütunları okur.
        ("interviews", "final_score_position", "INTEGER"),
        ("interviews", "final_score_profile", "INTEGER"),
        # İŞ EMRİ — madde 10: Quality Gate üç durumlu status (PASS | PATCHED_AND_VALIDATED |
        # BLOCKED_INTEGRITY) — "BLOCKING bulundu ama sessizce geçirildi" açığını KAPATIR. Yalnız
        # L3'te dolar; L1/L2'de her zaman NULL (Quality Gate hiç çalışmaz).
        ("interviews", "quality_gate_status", "TEXT"),
        # İŞ EMRİ — madde 11: AI Quality Gate'ten SONRA, DB finalization'dan ÖNCE çalışan
        # deterministik bütünlük kapısının sonucu (PASS | FAIL) — yönetici görünür, ayrı log.
        ("interviews", "final_integrity_status", "TEXT"),
        # İŞ EMRİ — ÇOKLU TALENT MİMARİSİ / madde I: aynı candidate+level için aynı anda YALNIZ
        # TEK finish/regenerate job'ı çalışabilsin diye atomik claim alanları (bkz.
        # _mark_finish_pending). processing_status zaten vardı (status); bunlar job_id/operation/
        # attempt'i EKLER — process-local değil, DB seviyesinde (çoklu worker/process güvenli).
        ("interviews", "processing_job_id", "TEXT"),
        ("interviews", "processing_operation", "TEXT"),
        ("interviews", "processing_attempt", "INTEGER DEFAULT 0"),
        # İŞ EMRİ — ÇOKLU TALENT MİMARİSİ / madde B: aynı candidate+level için aynı anda YALNIZ
        # TEK aktif Realtime (canlı ses) oturumu kabul edilsin diye ownership alanları (bkz.
        # create_realtime_session). Transcript/skor alanlarına dokunmaz, yalnız "şu an bu satırın
        # canlı oturum sahibi kim/ne zamandan beri" bilgisini tutar.
        ("interviews", "realtime_owner_token", "TEXT"),
        ("interviews", "realtime_owner_at", "TIMESTAMP" if USE_POSTGRES else "TEXT"),
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

# İş emri — PRIMARY DEĞERLENDİRME VE KANIT SEÇİMİ GÜVENİLİRLİĞİ / TIMESTAMP GROUNDING (FAZ 3):
# kaynak transkriptte GERÇEK [mm:ss] konuşmacı-damgası var mı — deterministik kontrol. Prompt
# builder'lar (build_l2_report_prompt/get_system_prompt) bu bayrağı, modelden kanıt havuzu/kanıt
# hücresi formatında [mm:ss] BEKLEYİP BEKLEMEYECEĞİNE karar vermek için kullanır — kaynakta hiç
# timestamp yoksa model damga UYDURMAYA zorlanmaz. _VOICE_LINE_RE'nin AYNISI (yeni bir desen YOK).
def _transcript_has_real_timestamps(transcript_text: str) -> bool:
    if not transcript_text:
        return False
    return bool(_VOICE_LINE_RE.search(transcript_text))

_ANY_TS_RE = re.compile(r"\[(\d{1,3}:[0-5]\d)\]")

def _strip_unsourced_timestamps_for_display(report_text: str, transcript_text: str) -> str:
    """İş emri — TIMESTAMP GROUNDING / madde 22: YALNIZ kullanıcıya gösterilen final report
    metninden, kaynak transkriptte GERÇEKTEN bulunmayan [mm:ss] damgalarını kaldırır. raw_report
    ve final_integrity_status/grounding_fail kayıtları bu fonksiyondan ETKİLENMEZ — çağrı sırası
    (run_deferred_finish_job) bu fonksiyonun run_final_deterministic_integrity_check'ten SONRA
    çalışmasını garanti eder (bkz. çağrı sitesi notu). Yalnız RENDER/GÖRÜNÜM temizliği — kanıt
    METNİ (tırnak içindeki alıntı) DEĞİŞMEZ, yalnız önündeki kaynaksız [mm:ss] etiketi (+ hemen
    ardındaki tek boşluk) silinir; kaynaklı damgalar AYNEN korunur."""
    if not report_text:
        return report_text or ""
    real_ts = set(_ANY_TS_RE.findall(transcript_text or ""))
    def _repl(m):
        return m.group(0) if m.group(1) in real_ts else ""
    return re.sub(r"\[(\d{1,3}:[0-5]\d)\][ \t]?", _repl, report_text)

# TUR 4 / GÖREV 1.3 — konumu (zaman damgası) bilinmeyen transkript satırlarının toplandığı
# başlık. build_transcript_view / transcript_to_text / PDF transkript render'ı bu TAM METNİ
# tanır ve bir konuşmacı satırı DEĞİL, bağımsız bir bölüm başlığı olarak işler (role="baslik").
UNPLACED_LINES_HEADING = "Konumu belirlenemeyen satırlar (sistem):"

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
                # TUR 4 / GÖREV 1.3 — konumu belirlenemeyen satırlar başlığı: konuşmacı DEĞİL,
                # bölüm başlığı. "Aday:"ya asla yazılmaz.
                if line == UNPLACED_LINES_HEADING:
                    out.append({"role": "baslik", "ts": "", "text": line, "elapsed_ms": None})
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
        # İş emri — KRİTER GEREKÇESİ (GÖREV 3): önceden burada HAM ISO zaman damgası (ts) tutulup
        # transcript_to_text/PDF'e AYNEN basılıyordu (ör. "[2026-09-12T08:15:32]") — L1/L3 (metin)
        # adaylar için modelin gördüğü transkriptte GERÇEK [mm:ss] damgası HİÇ yoktu; model rapora
        # yazdığı her [dk] damgasını fiilen UYDURUYORDU (kök neden — GÖREV 3'ün "SORU_DAMGASI/KANIT
        # transkriptte gerçekten var mı" doğrulaması bu yüzden L1/L3'te anlamlıydı ama daha önce hiç
        # test edilebilir değildi). Artık ts, ses akışıyla (L2/L3-sesli) AYNI biçimde elapsed_ms'ten
        # türetilen M:SS — anchor/started_at yoksa boş (uydurma bir damga göstermek yerine damgasız).
        disp_ts = f"{elapsed_ms // 60000}:{(elapsed_ms // 1000) % 60:02d}" if elapsed_ms is not None else ""
        out.append({"role": "aday" if role_raw == "user" else "mulakatci", "ts": disp_ts, "text": clean_text, "elapsed_ms": elapsed_ms})
    if for_report:
        out = [r for r in out if not _is_system_line(r.get("text"))]
    return out

def transcript_to_text(view: list) -> str:
    """build_transcript_view çıktısını indirilebilir düz metne çevirir."""
    lines = []
    for row in view:
        if row["role"] == "baslik":
            lines.append(row["text"])
            continue
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

# GÖREV 3 — kamera DOĞRULAMA karesi seçimi.
#   L1 → 0 (yazışmalı mülakat, kamera yok)   L2 → 4   L3 → 6
# Kaynak havuz = MİMİK analizi kareleri (reason='mimic_sample'); ayrı bir doğrulama seti YOK.
# Kareler mülakat süresine EŞİT dilimlere bölünüp her dilimden bir kare (dilim ortasına en yakın)
# alınır — tek dakikada toplanma sorunu ortadan kalkar.
VERIFICATION_FRAME_COUNT = {1: 0, 2: 4, 3: 6}

# TUR 4 / GÖREV 3+6.3 — seviyenin kamera/ses modalitesi olup olmadığı TEK yerden okunur
# (Kader'e ÖZEL değil; her aday, her seviye için geçerli GENEL kural). Level 1 metin
# tabanlıdır ve hiçbir zaman kamera/ses verisi üretmez — raporun ilgili bölümleri bu adaylar
# için hiç basılmamalı (eksik veri notu bile değil, çünkü hiç beklenmiyor).
def _level_has_camera(level: Optional[int]) -> bool:
    return VERIFICATION_FRAME_COUNT.get(level or 1, 0) > 0

def _level_has_voice(level: Optional[int]) -> bool:
    return (level or 1) in (2, 3)

def _is_near_duplicate(text: str, existing: list, threshold: float = 0.6) -> bool:
    """Basit benzerlik kontrolü — aynı gözlemin birden çok kaynaktan (mimik + ses metriği +
    mülakatçı anlık gözlemi) neredeyse aynı cümleyle 2-3 kez tekrarlanmasını önler."""
    t = (text or "").lower().strip()
    if not t:
        return False
    for e in existing:
        if difflib.SequenceMatcher(None, t, (e or "").lower()).ratio() >= threshold:
            return True
    return False

def select_verification_frames(candidate_id: int, level: Optional[int]) -> list:
    """Mimik havuzundan süreye yayılmış N doğrulama karesi seçer. Dönüş: [{id, image_base64,
    captured_at, elapsed_ms}] — elapsed_ms artan sırada. Mimik karesi yoksa eski doğrulama
    setine (reason<>'mimic_sample') düşer."""
    n = VERIFICATION_FRAME_COUNT.get(level or 1, 4)
    if n <= 0:
        return []
    db = get_db()
    try:
        pool = db.execute(
            "SELECT id, image_base64, captured_at, elapsed_ms FROM snapshots "
            "WHERE candidate_id=? AND reason='mimic_sample' ORDER BY COALESCE(elapsed_ms,0) ASC, id ASC",
            (candidate_id,)
        ).fetchall()
        if not pool:
            pool = db.execute(
                "SELECT id, image_base64, captured_at, elapsed_ms FROM snapshots "
                "WHERE candidate_id=? AND (reason IS NULL OR reason<>'mimic_sample') "
                "ORDER BY COALESCE(elapsed_ms,0) ASC, captured_at ASC, id ASC",
                (candidate_id,)
            ).fetchall()
    finally:
        db.close()
    rows = [dict(r) for r in pool]
    if not rows:
        return []
    if len(rows) <= n:
        return rows
    lo = _safe_int(rows[0].get("elapsed_ms")) or 0
    hi = _safe_int(rows[-1].get("elapsed_ms")) or (lo + 1)
    span = max(1, hi - lo)
    picked, used = [], set()
    for k in range(n):
        target = lo + span * (k + 0.5) / n           # k. dilimin ORTASI
        cand = min((r for i, r in enumerate(rows) if i not in used),
                   key=lambda r: abs((_safe_int(r.get("elapsed_ms")) or 0) - target), default=None)
        if cand is None:
            break
        used.add(rows.index(cand))
        picked.append(cand)
    picked.sort(key=lambda r: _safe_int(r.get("elapsed_ms")) or 0)
    return picked

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
    def _plumber_columns():
        # GÖREV 7.2 — KOLON-DUYARLI okuma. Sayfadaki kelimeleri x konumuna göre kümeleyip
        # net bir dikey boşluk (gutter) varsa SOL kolonu tam okur, sonra SAĞ kolonu — satır
        # satır iç içe geçirmez. Tek kolonlu CV'de tek küme çıkar, davranış değişmez.
        import pdfplumber
        out_pages = []
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                words = page.extract_words(x_tolerance=1.5, y_tolerance=3, keep_blank_chars=False)
                if not words:
                    continue
                pw = float(page.width or 0) or max((w["x1"] for w in words), default=1)
                # aday kolon sınırı: sayfa ortası çevresinde, hiç kelimenin KESMEDİĞİ bir x var mı?
                mid_lo, mid_hi = pw * 0.40, pw * 0.60
                crossing = [w for w in words if w["x0"] < mid_hi and w["x1"] > mid_lo]
                two_col = len(crossing) <= max(2, len(words) * 0.04)
                def _emit(ws):
                    ws = sorted(ws, key=lambda w: (round(w["top"] / 6), w["x0"]))
                    lines, cur, cur_top = [], [], None
                    for w in ws:
                        if cur_top is None or abs(w["top"] - cur_top) <= 6:
                            cur.append(w["text"]); cur_top = w["top"] if cur_top is None else cur_top
                        else:
                            lines.append(" ".join(cur)); cur = [w["text"]]; cur_top = w["top"]
                    if cur:
                        lines.append(" ".join(cur))
                    return "\n".join(lines)
                if two_col:
                    split_x = pw * 0.5
                    left = [w for w in words if (w["x0"] + w["x1"]) / 2 < split_x]
                    right = [w for w in words if (w["x0"] + w["x1"]) / 2 >= split_x]
                    out_pages.append(_emit(left) + "\n\n" + _emit(right))
                else:
                    out_pages.append(_emit(words))
        return "\n\n".join(out_pages).strip()
    return [
        ("pdfplumber-columns", _plumber_columns),   # GÖREV 7.2 — önce kolon-duyarlı dene
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

_RETRY_BACKOFF = [1, 3, 7]  # saniye — 3 deneme (GERİYE UYUM: artık yalnız _technical_retry_delay'in
# Retry-After YOKSA düştüğü bounded exponential formülünün başlangıç tabanı olarak kullanılıyor;
# davranışı hâlâ tanımlayan TEK yer değil, bkz. aşağıdaki İŞ EMRİ — TEKNİK RETRY bloğu.

# İŞ EMRİ — ÇOKLU TALENT MİMARİSİ / madde G — TEKNİK RETRY ile CONTENT RETRY kesin ayrıldı.
# Bu blok yalnız TEKNİK (429/timeout/network/geçici 5xx) retry'ı yönetir — "AI cevabını beğenmedik,
# yeniden üret" (content retry) burada YOK, o normal akıştan tamamen çıkarıldı (bkz.
# apply_structured_rationale_gate / finalize_interview yönetici özeti bloğu). Sonsuz retry YOK —
# _RETRY_MAX_ATTEMPTS ile ve her denemenin beklemesi _RETRY_MAX_DELAY_SECONDS ile tavanlanır.
_RETRY_MAX_ATTEMPTS = 4          # ilk deneme + en fazla 3 teknik retry (mevcut üst sınırla AYNI)
_RETRY_MAX_DELAY_SECONDS = 20.0  # Retry-After bile bunu aşarsa yine bu tavanda bekler (sonsuz bekleme yok)


def _parse_retry_after(headers) -> Optional[float]:
    """HTTP Retry-After header'ını saniyeye çevirir (tam sayı/saniye biçimi VEYA HTTP-date).
    Yok/parse edilemezse None — çağıran bounded exponential backoff'a düşer."""
    if not headers:
        return None
    val = headers.get("retry-after") or headers.get("Retry-After")
    if not val:
        return None
    try:
        return max(0.0, float(val))
    except (TypeError, ValueError):
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(val)
        if dt is not None:
            now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
            return max(0.0, (dt - now).total_seconds())
    except Exception:
        pass
    return None


def _log_rate_limit_headers(step: str, headers) -> None:
    """GÖREV J/M — x-ratelimit-* header'ları (varsa) yalnız GÖZLEMLENEBİLİRLİK için loglanır;
    kapasite kararı BUNLARA dayanmaz (OpenAI bu header'ları her istekte garanti etmez, provider'a
    göre değişir) — yalnız admin/debug tanısı için Railway stdout'a basılır. Secret/token YOK."""
    if not headers:
        return
    keys = ("x-ratelimit-remaining-requests", "x-ratelimit-remaining-tokens",
            "x-ratelimit-limit-requests", "x-ratelimit-limit-tokens",
            "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens")
    found = {k: headers.get(k) for k in keys if headers.get(k) is not None}
    if found:
        print(f"[RATE_LIMIT_HEADERS] step={step} {found}")


def _technical_retry_delay(attempt_idx: int, headers=None) -> float:
    """GÖREV G — teknik retry gecikmesi: ÖNCE sunucunun Retry-After'ı (varsa) kullanılır; yoksa
    bounded exponential backoff + jitter (sabit [1,3,7] yerine — art arda çok sayıda çağrı aynı
    TPM penceresine YIĞILMASIN diye jitter var). Her durumda _RETRY_MAX_DELAY_SECONDS ile
    tavanlanır — sonsuz/aşırı uzun bekleme yok."""
    ra = _parse_retry_after(headers)
    if ra is not None:
        return min(ra, _RETRY_MAX_DELAY_SECONDS)
    base = min(1.0 * (2 ** attempt_idx), _RETRY_MAX_DELAY_SECONDS)
    jitter = random.uniform(0, base * 0.5)
    return min(base + jitter, _RETRY_MAX_DELAY_SECONDS)

# error_class -> adaya gösterilecek metin (İŞ EMRİ 1.4 tablosu). Backend'de "kota/bakiye/API/quota"
# kelimeleri bu sözlüğün dışına ÇIKMAZ; frontend yalnızca buradaki 'message'ı gösterir.
AI_ERROR_USER_MESSAGES = {
    "insufficient_quota":  "Mülakat şu anda başlatılamıyor. Lütfen yetkiliyle iletişime geçin.",
    "invalid_api_key":     "Mülakat şu anda başlatılamıyor. Lütfen yetkiliyle iletişime geçin.",
    # ACİL — /api/realtime/session teşhisi (2026-09): classify_ai_error önceden 404/"model not
    # found" tarzı sağlayıcı hatalarını HİÇ tanımıyordu — bunlar sessizce "unknown" kovasına
    # düşüyordu (generic 500 + "Beklenmeyen bir hata oluştu" — ADMIN İÇİN TEŞHİS EDİLEMEZ bir
    # mesaj, ayrıca kritik e-posta/uyarı TETİKLEMİYORDU çünkü _CRITICAL_CLASSES'ta değildi).
    # Model adı deploy/config kaynaklı, admin-aksiyonlu bir sorundur (kota/API-anahtarıyla AYNI
    # sınıf) — insufficient_quota/invalid_api_key ile AYNI muameleyi (503 + kritik e-posta) görür.
    "model_unavailable":   "Mülakat şu anda başlatılamıyor. Lütfen yetkiliyle iletişime geçin.",
    "rate_limit_exceeded": "Sistem şu anda yoğun. Lütfen birkaç dakika sonra tekrar deneyin.",
    "server_error":        "Servise şu an ulaşılamıyor. Lütfen tekrar deneyin.",
    "network":             "Bağlantı kurulamadı. İnternet bağlantınızı kontrol edin.",
    "mic_permission":      "Mikrofon erişimi verilmedi. Tarayıcı ayarlarından izin verin.",
    "unknown":             "Beklenmeyen bir hata oluştu. Lütfen tekrar deneyin.",
}
_RETRYABLE_CLASSES = {"rate_limit_exceeded", "server_error", "network"}
_CRITICAL_CLASSES = {"insufficient_quota", "invalid_api_key", "model_unavailable"}

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
        "insufficient_quota": 503, "invalid_api_key": 503, "model_unavailable": 503,
        "rate_limit_exceeded": 429, "server_error": 502, "network": 504,
    }.get(error_class, 500)

def classify_ai_error(provider: str, status: Optional[int], body) -> str:
    """HTTP status + sağlayıcı hata kodunu BİRLİKTE okuyarak sınıflandırır.
    429 tek başına 'rate_limit' varsayılmaz — kod 'insufficient_quota' ise kota tükenmesidir.
    ACİL — /api/realtime/session teşhisi (2026-09) — KÖK NEDEN ADAYI: model adı deploy/config
    kaynaklı olarak geçersiz/kaldırılmış olabilir (OpenAI 404 "model_not_found"/"invalid_request_
    error" döner) — bu durum ÖNCEDEN hiç tanınmıyordu, "unknown" kovasına (generic 500, kritik
    e-posta YOK, admin için teşhis edilemez mesaj) sessizce düşüyordu. Artık AYRI sınıflandırılır:
    admin-aksiyonlu (kota/API-anahtarıyla AYNI aile), 503 + kritik e-posta tetikler."""
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
    if (status == 404 or "model_not_found" in blob or "does not exist" in blob or "unknown model" in blob
       or ("model" in blob and ("invalid_request_error" in blob or "not found" in blob))):
        return "model_unavailable"
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
        "model_unavailable": f"{who} tarafında kullanılan model adı geçersiz/kaldırılmış olduğu için (deploy config kontrol edilmeli)",
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
    attempts = _RETRY_MAX_ATTEMPTS if retry else 1
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
                time.sleep(_technical_retry_delay(i)); continue
            raise _handle_ai_failure(AIError("network", "openai", step, last_detail, retry_count=i,
                                             http_status=504), context, severity)
        _log_rate_limit_headers(step, resp.headers)
        if resp.status_code < 400:
            print(f"[OPENAI_OK] {step} {resp.status_code} {dt}ms {method} {url}")
            return resp
        body_text = resp.text[:2000]
        last_err_class = classify_ai_error("openai", resp.status_code, body_text)
        last_detail = f"HTTP {resp.status_code} | {method} {url} | {body_text}"
        print(f"[OPENAI_ERR] {step} {last_err_class} HTTP {resp.status_code} attempt={i+1}/{attempts}: {body_text[:300]}")
        if retry and last_err_class in _RETRYABLE_CLASSES and i < attempts - 1:
            # GÖREV G/J — Retry-After (varsa) veya bounded exponential+jitter; sabit [1,3,7] artık YOK.
            time.sleep(_technical_retry_delay(i, resp.headers)); continue
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
- 2. deneme de cevapsız/kaçamak kalırsa "Bunu geçelim." de, o kriteri BIRAK ve yeni kritere geç — 3. kez ISRAR ETME. Bu kriter "sorulmuş ama yeterli cevap alınamamış" sayılır; bu "değerlendirilemedi" İLE AYNI ŞEY DEĞİLDİR (kriter hiç sorulmadıysa "değerlendirilemedi" olur) — raporlama aşamasında TEK KURAL'a göre otomatik taban puanla (tavanın %25'i) puanlanır, sen puan/etiket hesaplama.
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

KRİTER KAPSAMA (ÖNEMLİ — İŞ EMRİ): Amaç, tanımlı kriterlerin TAMAMININ mülakat sırasında gerçekten ölçülme fırsatı bulmasıdır — bu opsiyonel bir "varsa kontrol et" değil, mülakatın asıl işlevidir. Akış içinde mekanik kapı yok, ama end_interview çağırmadan ÖNCE her kriteri tek tek gözden geçir: hiç dokunulmamış olan varsa en az bir soru sor. Adayın verdiği TEK bir cevap birden fazla kriter için geçerli kanıt oluşturabilir — böyle bir kriteri tekrar sormaya ZORUNLU değilsin, gereksiz tekrar soru üretme. Yine de mülakat sonunda gerçekten hiç sorulamayan bir kriter kalırsa raporda "değerlendirilmedi" işaretlenir — uydurma değerlendirme yapma.

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

# İŞ EMRİ — PRIMARY PUAN FORMATI DÜZELTMESİ: Kriter hücresi (PUAN sütunu) kriter PAYDA
# İÇİNDEYSE (sorulmuş — taban puan/düşük puan/açık ret dahil) HER ZAMAN sayısal X/Y'dir;
# "Düşük"/"Yetersiz Cevap (taban puan)" gibi serbest metin ARTIK PUAN sütununa YAZILMAZ —
# gerekçe/açıklama KANIT VE ANALİZ (3. hücre) içine yazılır. Yalnız kriter PAYDA DIŞIYSA
# (hiç sorulmadı) "Değerlendirilemedi (sistem)" metni PUAN sütununda kalır (sayı YOKTUR,
# çünkü payda dışı kriterin puanı da yoktur). Bu, YALNIZCA promptun GPT'ye ne İSTEDİĞİNİ
# değiştirir — recompute_and_fix_score/recompute_profile_section'ın GPT sayı yazMAdığında
# uyguladığı mevcut taban puan/ayrıştırma mantığı DEĞİŞMEDİ (bkz. o fonksiyonlardaki notlar).
_CRIT_CELL_HINT = ("__/{w}  (kriter sorulmuş ve payda İÇİNDEYSE HER ZAMAN bu sayısal biçim — "
                   "taban puan/düşük puan/açık ret durumlarında DA sayı yaz, açıklamayı PUAN "
                   "sütununa DEĞİL KANIT VE ANALİZ sütununa yaz)  |  VEYA (yalnızca kriter HİÇ "
                   "SORULMADIYSA, payda DIŞI) — Değerlendirilemedi (sistem) — <gerekçe>")

# İŞ EMRİ — KRİTER KAPSAMA + YETERSİZ CEVAP PUANLAMA: TEK KURAL (eksik veri) — hem PUAN 1 hem PUAN 2
# için; prompt'larda birebir kullanılır. ÜÇ AYRI DURUM birbirine KARIŞTIRILMAZ: (1) kriter HİÇ
# sorulmadı → Değerlendirilemedi (payda dışı); (2) kriter sorulmuş/yeterli fırsat verilmiş ama
# değerlendirilebilir cevap yok → TABAN PUAN (payda içinde, sistem otomatik %25 uygular —
# "bilmiyorum"/"deneyimim yok"/boş/anlamadım/kısa-anlamsız cevap DA BU KATEGORİDİR, Değerlendirilemedi
# DEĞİLDİR); (3) değerlendirilebilir (zayıf da olsa) bir cevap varsa normal puanlama devam eder.
CRITERION_SCORING_RULE = (
    "KRİTER PUANLAMA — EKSİK VERİ (KESİN, TEK KURAL, ÜÇ AYRI DURUM):\n"
    "1) Kriter mülakatta HİÇ SORULMADIYSA (transkriptin başka bir yerinde de o kriteri değerlendirmeye "
    "yetecek anlamlı bir veri YOKSA): **Değerlendirilemedi (sistem) — <gerekçe>**. Bu kriter PUANA ve "
    "PAYDAYA GİRMEZ.\n"
    "2) Kriter DÜZGÜN SORULDUYSA (gerekirse en fazla 2. denemeyle kısaltılmış/basitleştirilmiş biçimde "
    "yeniden de soruldu) ama aday cevap VERMEDİ, 'bilmiyorum' / 'bu konuda deneyimim/tecrübem yok' dedi, "
    "cevabı boş / '[SİSTEM: … halüsinasyon]' işaretli kaldı, veya 'anlamadım / tekrar eder misiniz' gibi "
    "bir anlamlı cevap oluşmadı: BU 'DEĞERLENDİRİLEMEDİ' DEĞİLDİR — kriter SORULMUŞ ve DEĞERLENDİRİLMİŞ "
    "sayılır; PUANA ve PAYDAYA GİRER. PUAN sütununa YİNE SAYISAL bir X/Y yaz (tavanın yaklaşık %25'i — "
    "kesin rakamı sistem ayrıca deterministik doğrular/düzeltir) — PUAN sütununa 'Yetersiz Cevap (taban "
    "puan)' gibi SERBEST METİN YAZMA, açıklamayı ('sorgulandı, yeterli cevap alınamadı: <gerekçe>') "
    "KANIT VE ANALİZ sütununa yaz.\n"
    "3) Kriter DÜZGÜN soruldu ve aday DEĞERLENDİRİLEBİLİR bir cevap verdi ama cevap yüzeysel/kısa/eksik "
    "kaldıysa (2. maddedeki 'anlamlı cevap YOK' durumuyla KARIŞTIRMA — burada gerçek bir cevap İÇERİĞİ "
    "VAR): **DÜŞÜK PUAN ver (kanıt düzeyine göre, SCORING_RUBRIC'e göre)** — otomatik %25'e veya 0'a "
    "ZORLAMA, cevabın kendi kalitesine göre PUAN sütununa SAYISAL bir X/Y yaz (metin YAZMA); adayın "
    "cevabının TAMAMINI (yalnız ilk/kısa cümleyi değil) dikkate alarak puanla.\n"
    "- **0/<tavan>** yalnızca şu iki durumda: aday cevap vermeyi AÇIKÇA reddetti VEYA tamamen alakasız/konu "
    "dışı cevap verdi. Bu durumda 0 paydaya girer (2. maddedeki taban puan İLE KARIŞTIRMA — bu daha "
    "adversarial/dar bir durumdur).\n"
    "- Halüsinasyon olarak işaretlenmiş turlar ve mülakatçının aynı soruyu tekrarladığı turlar HİÇBİR kriterin "
    "puanını (2. maddedeki taban puandan AŞAĞI) düşürme gerekçesi OLAMAZ.\n"
    "- Bu kural POZİSYON (PUAN 1) ve PROFİL (PUAN 2) tablolarının İKİSİ için de geçerlidir."
)

# TUR 2 / GÖREV B — AÇIK PUANLAMA RUBRİĞİ. Amaç puanı yükseltmek/düşürmek değil, KANITA BAĞLAMAK.
# İki çalıştırma arasında aynı transkriptte puanların gerekçesiz oynamasını (69→80, Süreç Yönetimi
# 5→10/10) önler. Bantlar kriterden BAĞIMSIZ geneldir; tam puan için kanıt zorunluluğu KESİN.
SCORING_RUBRIC = (
    "PUANLAMA RUBRİĞİ (KESİN — her kriter için uygula, AYNI transkript her seferinde AYNI puanı "
    "üretmeli — 'yeterli' / 'sınırlı' gibi öznel kelimelerle DEĞİL, aşağıdaki GÖZLENEBİLİR "
    "ölçütlerle karar ver):\n"
    "- TAM PUANA YAKIN (tavanın %85-100'ü) — GÖZLENEBİLİR ÖLÇÜT (ÜÇÜ BİRDEN şart): (1) transkriptten "
    "EN AZ İKİ ayrı somut örnek/detay, (2) anlattığı yaklaşımda EN AZ 2 ayrı adım/aşama sayılabiliyor "
    "(ör. '1) ekstreye bakarım 2) karşı tarafla mutabakat yaparım'), (3) aday kendiliğinden bir istisna/zor "
    "durumu ('ama şöyle olursa...') ele alıp doğru yönetti. Üçü birlikte yoksa bu bandı VERME.\n"
    "- ORTA (tavanın %45-70'i) — GÖZLENEBİLİR ÖLÇÜT: EN AZ 1 somut örnek VE en az 1 "
    "adım/aşama sayılabiliyor, ama istisna/zor durum yönetimi YOK veya örnek TEK.\n"
    "- DÜŞÜK (tavanın %15-40'ı) — GÖZLENEBİLİR ÖLÇÜT VE ZORUNLU KANIT (TAM PUAN bandıyla AYNI titizlikte): "
    "cevapta somut adım/örnek YOK (yalnızca genel-geçer bir cümle) VEYA aday soruyu kısmen/hiç yanıtlayamadı. "
    "Bu bandı verirken 'Kanıt ve Analiz' hücresinde AYNEN şunlar ZORUNLUDUR: (a) HANGİ SORU soruldu, (b) "
    "adayın cevabında SOMUT olarak NEYİN eksik/yanlış/yetersiz kaldığı. Tek kelimelik gerekçe "
    "('yüzeysel', 'yetersiz', 'sınırlı' vb. TEK BAŞINA) YETERSİZDİR ve KABUL EDİLMEZ.\n"
    "- 0 / Değerlendirilemedi: yukarıdaki TEK KURAL'a göre.\n"
    "\n"
    "GEREKÇE (Kanıt ve Analiz hücresi) YAZIM KURALLARI (KESİN):\n"
    "1) YASAK KALIPLAR — bunları veya eş anlamlılarını YAZMA (her adayda aynı çıkıyor, hiçbir şey "
    "söylemiyor): \"daha fazla detay gerekmektedir\", \"daha fazla derinlik gerekmektedir\", \"daha fazla "
    "netlik gerekmektedir\", \"daha fazla açıklık gerekmektedir\", \"daha fazla detaylandırma gerekmektedir\", "
    "\"daha fazla bilgi verilmemiştir\", \"daha derin anlatmamıştır\", \"somut delil verememiştir\", "
    "\"yeterince açıklamamıştır\", \"daha fazla esneklik gerekmektedir\".\n"
    "2) \"ANCAK\" KALIBI YASAK — her gerekçe 'X yapmıştır. Ancak Y eksiktir.' yapısında OLMAYACAK; cümle "
    "yapısını kriterden kritere DEĞİŞTİR. Altı-sekiz kriterin hepsi aynı cümle iskeletinde çıkarsa HATADIR.\n"
    "3) ODAK OLANA: gerekçenin ağırlığı adayın NE YAPABİLDİĞİ / NE BİLDİĞİ / NASIL YAKLAŞTIĞI üzerinde "
    "olsun; tek kelimelik bir eksik-kapanışı YETMEZ.\n"
    "4) Gerçekten eksik varsa SOMUT yaz: hangi soru soruldu, aday ne cevap verdi/veremedi, bu NEDEN eksik "
    "sayıldı. Genel 'yetersiz kaldı' YASAK.\n"
    "5) TAM veya tama yakın puan alan kriterde eksik/'ancak' cümlesi KURMA — yalnız güçlü yönü anlat.\n"
    "6) Aynı transkript anını/kanıtını birden fazla kriterde OTOMATİK tekrar KULLANMA. Gerçekten iki kriter "
    "için de bağımsız kanıt oluşturuyorsa kullanılabilir, ama HER kriterde NEYİ gösterdiğini AYRI anlat — "
    "cümleyi kopyalama.\n"
    "\n"
    "DAKİKA DAMGASI (KESİN, GEVŞETİLDİ): Damga (ör. [8:21]) YALNIZCA şu üç durumda kullanılır: bir puanı "
    "DOĞRUDAN gerekçelendiren somut alıntı, adayın kendi ağzıyla belirttiği önemli bir beyan, bir risk/"
    "çelişki tespiti. Anlatı akarken her cümlenin başına/sonuna damga EKLEME. Bir gerekçede EN FAZLA 1-2 "
    "damga yeterlidir — 3+ damganın art arda yığılması (\"[1:21] ... [3:15] ... [8:21]\") YASAK. Damganın "
    "rapora BASILMAMASI kanıtın kullanılmadığı anlamına GELMEZ — puanı yine transkriptteki gerçek bir ana "
    "dayandır, yalnızca o anı METİNDE her seferinde işaretleme."
)

# İş emri EK — GÖREV 5: PUAN, CEVABIN KRİTERİ KARŞILAMASINA GÖRE VERİLİR (2026-09, önceki iş
# emrine ek). Kök neden: SCORING_RUBRIC cevabın BİÇİMİNE (somut örnek var mı, adım sayılabiliyor
# mu) bakıyordu, KRİTERİ KARŞILAYIP KARŞILAMADIĞINA bakmıyordu — "benim alanım değil" akıcı/net/
# tutarlı bir cevaptır ama kriterin sorduğunu KARŞILAMAZ; sistem bunu orta/yüksek banda koyuyordu
# (gerçek örnek: Finance Specialist adayı 4 ayrı turda alan dışı olduğunu beyan etti, yine de
# Nakit Akışı 18/25 aldı). ÖNCEL KURAL diğer TÜM bantlardan ÖNCE uygulanır.
_SCOPE_PRIORITY_RULE = (
    "ÖNCEL KURAL (diğer tüm bantlardan ÖNCE uygulanır, GÖREV 5): Puan, cevabın KRİTERİN SORDUĞU "
    "ŞEYİ KARŞILAMA DERECESİNE göre verilir — cevabın akıcılığı/netliği/tutarlılığı/dürüstlüğü TEK "
    "BAŞINA puan getirmez. Aşağıdakilerden biri varsa cevap ne kadar net/tutarlı olursa olsun bu "
    "kriter tavanın EN FAZLA %25'ini alabilir: (a) ALAN DIŞI BEYANI — aday bu işin kendi "
    "sorumluluğunda olmadığını söylüyor ('benim alanım değil', 'ben o kısımda yokum', 'bize "
    "gelmiyor', 'görebileceğim bir alan yok'); (b) DEVRETME — cevap yerine süreci başkasına "
    "bırakıyor ('yöneticime sorarım', 'yöneticim karar verir'); (c) SORUYU KARŞILAMAYAN CEVAP — "
    "konu dışı bir şey anlatıyor; (d) TEKRARLI SORUYA CEVAPSIZLIK — mülakatçı aynı noktayı 2+ kez "
    "sorduğu halde aday somut cevap vermiyor. Aday konuya temas edip pozisyonun gerektirdiği "
    "SEVİYEDE değilse (ör. finans sorusuna muhasebe seviyesinde cevap) tavanın EN FAZLA %50'sini "
    "alabilir (kısmi karşılama — cevap yanlış değildir ama kriterin ölçtüğü yetkinliği göstermez). "
    "AYNI CEVAP FARKLI POZİSYONLARDA FARKLI PUAN alır — kriter adı ve pozisyon tanımı neyi "
    "ölçüyorsa ona göre değerlendir, genel 'iyi cevap' ölçütü KULLANMA. (a) veya (b) tespit "
    "edilirse: KANIT alanına [dk] damgasıyla YAZ (sistem bunu doğrular ve payda dışına almazsa "
    "GÜÇLENDİRİLMİŞ RAPORLAMA için Gelişim Alanları'na ayrıca RİSK olarak ekler); bu bulgu "
    "SESSİZCE GEÇİLEMEZ — işe alım kararını belirleyen en önemli sinyaldir."
)

# İŞ EMRİ — OPENAI PRIMARY KRİTER DEĞERLENDİRME DÜZELTMESİ: (1) adayın kriterle ilgili cevabının
# TAMAMININ değerlendirilmesi (ilk/kısa cümle seçilip devamındaki daha güçlü içeriğin gözden
# kaçırılmaması), (2) kanıtın atandığı kriterin TANIMINI gerçekten desteklemesi (teknik bilgi/araç
# kullanımının TEK BAŞINA alakasız bir davranışsal profil kriterine kanıt yapılmaması). Bu kural
# önceden yalnızca L2/L3 sesli rapor promptunda (build_l2_report_prompt TEMEL KURALLAR) vardı, L1
# metin promptunda (get_system_prompt) HİÇ yoktu, POZİSYON/PROFİL tablo üretiminde de yoktu — TEK
# yerden (build_report_content_prompt, L1/L2/L3 ORTAK) eklenir. Mevcut anti-misattribution
# kuralları (build_l2_report_prompt TEMEL KURALLAR, run_report_reviewer promptu) SİLİNMEDİ/
# değiştirilmedi, bu yalnız aynı ilkeyi POZİSYON+PROFİL tablo üretiminin kendisine de taşır.
_EVIDENCE_COMPLETENESS_AND_MATCH_RULE = (
    "CEVABIN TAMAMINI DEĞERLENDİR VE KANITI DOĞRU KRİTERLE EŞLEŞTİR (KESİN — hem POZİSYON hem "
    "PROFİL tablosu için geçerli):\n"
    "- Bir kriteri puanlarken adayın o kriterle ilgili verdiği cevabın TAMAMINI dikkate al. Cevabın "
    "İLK CÜMLESİNİ veya kısa bir bölümünü seçip, aynı cevabın DEVAMINDAKİ daha ayrıntılı/daha güçlü "
    "içeriği GÖZ ARDI ETME — puanı ve kanıtı cevabın BÜTÜNÜNE göre belirle.\n"
    "- Kanıt olarak kullandığın ifade, puanladığın kriterin TANIMINI (yukarıdaki KRİTER TANIMLARI) "
    "GERÇEKTEN desteklemelidir. Teknik bilgi veya bir yazılım/aracın kullanılması TEK BAŞINA "
    "davranışsal bir profil kriterinin (ör. tutum/işbirliği, öğrenme/adaptasyon, inisiyatif, baskı "
    "altında davranış) kanıtı DEĞİLDİR — bir teknik cevap yalnızca GERÇEKTEN desteklediği kriterde "
    "kanıt olarak kullanılmalıdır.\n"
    "- İlgili kritere gerçek bir davranışsal kanıt bulunmuyorsa başka bir kriterin cevabından veya "
    "mülakatçının yönlendirme/geçiş cümlesinden alakasız bir ifade taşıyarak yapay kanıt OLUŞTURMA."
)

# GÖREV 5.5 — alan dışı / devretme beyanının LEKSİK imzaları (transkriptte GERÇEKTEN söylenen
# cümleler; KANIT alanındaki alıntı/özet bu kalıplardan birini içeriyorsa kriter "ALAN DIŞI/
# DEVRETME" sayılır — bkz. validate_criterion_fields: out_of_scope_high_score).
_OUT_OF_SCOPE_RE = re.compile(
    r"benim alan[ıi]m de[ğg]il|ben o k[ıi]s[ıi]mda de[ğg]ilim|ben o k[ıi]sma?da? de[ğg]ilim|"
    r"(?:bana|bize) gelmiyor|g[öo]rebilece[ğg]im bir alan yok|benim yapt[ıi][ğg][ıi]m bir k[ıi]s[ıi]m de[ğg]il|"
    r"ben (?:bunlar[ıi]|bunu) kontrol etmiyorum|bu benim sorumlulu[ğg]umda de[ğg]il|"
    r"finans k[ıi]sm[ıi]nda yokum|bu k[ıi]s[ıi]mda de[ğg]ilim|benim g[öo]revim de[ğg]il",
    re.IGNORECASE)
_DELEGATION_RE = re.compile(
    r"y[öo]neticim(?:le| ile)? konu[şs]urum|y[öo]neticim karar verir|y[öo]neticime sorar[ıi]m|"
    r"o bana y[öo]nlendirir|(?:ekibim|arkada[şs][ıi]m|meslekta[şs][ıi]m)(?:a|e) (?:sorar[ıi]m|b[ıi]rak[ıi]r[ıi]m)",
    re.IGNORECASE)

# İş emri — RAPOR İÇERİK STANDARDI / B3 — KAVRAM AYRIŞTIRMASI: "devretme" (ör. "yöneticime
# sorarım" — kararı BAŞKASINA bırakma) ile "alan dışı" (ör. "benim alanım değil" — konunun kendi
# mesleki alanı OLMADIĞI iddiası) KAVRAMSAL OLARAK farklıdır; önceki turda ikisi de tek bir
# "alan dışı/devretme beyanı" etiketiyle sunuluyordu — hangisinin gerçekleştiği belirsizleşiyordu.
def _scope_declaration_label(text: str) -> str:
    is_oos = bool(_OUT_OF_SCOPE_RE.search(text or ""))
    is_del = bool(_DELEGATION_RE.search(text or ""))
    if is_oos and is_del:
        return "alan dışı ve devretme"
    if is_del:
        return "devretme"
    if is_oos:
        return "alan dışı"
    return "alan dışı/devretme"  # eşleşme metinde (kanıt kırpıldığı için) yeniden bulunamadıysa güvenli varsayılan

# İş emri — KAPI EŞİĞİ VE SON TUTARLILIK / MADDE 5 — KÖK NEDEN: CV↔Uyum enjeksiyonu _scope_flagged
# listesindeki HER kriter için AYRI bir cümle üretiyordu; birden fazla kriter AYNI beyana (aynı
# alıntı, aynı [mm:ss]) dayandığında (gerçek örnek: 6-8 kriter, hepsi AYNI alıntıyla) paragraf
# okunamaz hale geliyordu. Fix: AYNI (beyan türü, kanıt) çiftini paylaşan kriterler TEK cümlede,
# birlikte sayılarak yazılır.
def _render_scope_flagged_sentences(flagged: list) -> str:
    groups, order = {}, []
    for f in flagged:
        key = (_scope_declaration_label(f["kanit"]), f["kanit"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(f["kriter"])
    sentences = []
    for label, kanit in order:
        kriterler = groups[(label, kanit)]
        if len(kriterler) == 1:
            sentences.append(f"\"{kriterler[0]}\" kriterinde aday {label} beyanında bulundu ({kanit}).")
        else:
            sentences.append(f"Aday, birden fazla kriterde ({', '.join(kriterler)}) {label} beyanında bulundu ({kanit}).")
    return " ".join(sentences)

# GÖREV 5 EK — yasak kalıp listesine "beklenmiştir" ailesi (önceki tur 8 kriterde bunu üretti,
# mevcut regex'lerin HİÇBİRİNE takılmadı): "daha <X> ... beklenmiştir" / "... sunması/yapması/
# göstermesi/alması beklenmiştir" — araya giren kelimelerden BAĞIMSIZ.
# İş emri — VALIDATOR KALİBRASYONU / GÖREV 3 (2026-09, sonraki tur) — bu regex yalnız "beklenmiştir"
# (geçmiş zaman) çekimini yakalıyordu; Yönetici Özeti'nde GERÇEKTEN kullanılan "beklenmektedir"
# (şimdiki zaman-geniş, çok daha yaygın çekim) ve "gerektiği" biten kalıp HİÇBİRİNE takılmadı —
# canlı örnekte 4/5 kaçtı. "daha <X> ... beklen*/gerektiği" kalıbı artık _BANNED_PHRASE_FAMILY_RE'nin
# genelleştirilmiş "daha <HERHANGİ SIFAT>" dalına TAŞINDI (KAYIP ANLATI BÖLÜMLERİ turu — "daha
# fazla" sabit ön eki "daha somut ... gerekmektedir" gibi varyantları üçüncü kez kaçırdı); burada
# yalnız "daha" ÖN EKİ OLMADAN geçen "sunması/yapması/... beklenmektedir" kalıbı kalıyor.
_EXPECTATION_PHRASE_RE = re.compile(
    r"(?:sunmas[ıi]|yapmas[ıi]|g[öo]stermesi|almas[ıi]|destekle(?:mesi|nmesi))(?:\s+[\wçğıöşü]+){0,4}\s+beklen(?:mi[şs]tir|mektedir|iyor)",
    re.IGNORECASE)

# İş emri — KAYIP ANLATI BÖLÜMLERİ / GÖREV 2.2 (2026-09, sonraki tur) — SABİT KALIP LİSTESİ üç
# kez art arda kaçırıldı ("daha fazla" → "daha somut", "beklenmiştir" → "beklenmektedir", vb.);
# artık YAPISAL bir kısıt da var: Yönetici Özeti'nde ZORUNLULUK KİPİ (-meli/-malı — Türkçede bu
# çekim HER ZAMAN "yapılması gereken bir şey" anlamına gelir, geçmişte YAPILANI anlatan bir
# özette neredeyse hiç meşru kullanımı yoktur) tek başına yeterli bir sinyal. Yalnız Yönetici
# Özeti'ne uygulanır (kriter hücreleri/Güçlü Yönler gibi diğer serbest metinlerde -meli/-malı
# başka bağlamlarda geçebilir; scope dar tutuldu).
_MODAL_OBLIGATION_RE = re.compile(r"\b[a-zçğıöşü]+(?:meli|mal[ıi])(?:dir|d[ıi]r)?\b", re.IGNORECASE)

def detect_future_expectation(text: str) -> list:
    """GÖREV 2.2 — Yönetici Özeti'nde GELECEĞE DÖNÜK BEKLENTİ cümlesi tespiti: banned_phrase_hits
    (genişletilmiş kalıp ailesi) + zorunluluk kipi (-meli/-malı) BİRLİKTE. Yalnız tespit eder,
    çağıran (finalize_interview) retry tetikler."""
    if not text:
        return []
    hits = banned_phrase_hits(text)
    hits += [m.group(0) for m in _MODAL_OBLIGATION_RE.finditer(text)]
    return hits

# İş emri GÖREV 1.1 — kriter gerekçelerinde klişe kalıp DETEKSİYONU (siler/düzeltmez — bir kalıbı
# cümle ORTASINDAN çıkarmak grameri bozar; bu, GÖREV 5'in takip-sorusu SATIRLARINI çıkarmasından
# FARKLI bir durum — orada tüm satır bağımsız bir madde, burada kalıp cümlenin İÇİNDE). Yalnız
# teşhis için record_system_decision'a loglanır.
_FORBIDDEN_EVIDENCE_CLICHE_RE = re.compile(
    r"daha fazla (?:detay|derinlik|netlik|aç[ıi]kl[ıi]k|detayland[ıi]rma|esneklik) gerekmektedir"
    r"|daha fazla bilgi verilmemi[şs]tir"
    r"|daha derin anlatmam[ıi][şs]t[ıi]r"
    r"|somut delil verememi[şs]tir"
    r"|yeterince aç[ıi]klamam[ıi][şs]t[ıi]r",
    re.IGNORECASE)

def detect_evidence_cliches(text: str) -> list:
    """Pozisyon/Profil kriter tablosu METNİNDE (Kanıt ve Analiz hücreleri) yukarıdaki yasaklı
    klişe kalıplarından biri geçen SATIRLARI döner (boş liste = temiz). Kaldırmaz — çağıran loglar.
    NOT (İş emri — KRİTER GEREKÇESİ: YAPISAL ÜRETİM + DETERMİNİSTİK DOĞRULAMA, 2026-09): bu fonksiyon
    artık TEK BAŞINA yeterli değil (prompt talimatı + bu log-only tarama, paraphrase ile atlatıldı —
    kanıtlı geri bildirim). Aşağıdaki _BANNED_PHRASE_FAMILY_RE + validate_criterion_fields (bkz.
    apply_structured_rationale_gate, run_deferred_finish_job yakınında) artık bir KAPI: geçemeyen
    içerik rapora HİÇ girmez. Bu fonksiyon geriye uyum + ek bir alt-kontrol olarak kalır."""
    if not text:
        return []
    return [ln.strip() for ln in text.splitlines() if ln.strip() and _FORBIDDEN_EVIDENCE_CLICHE_RE.search(ln)]

# İş emri GÖREV 2.1 — YASAKLI KALIP AİLESİ (yapısal desen, TEK TEK kelime listesi DEĞİL): önceki
# turun sabit ifade listesi ("daha fazla X gerekmektedir" vb. birebir) paraphrase ile atlatıldı
# ("daha fazla bilgi sunmamıştır", "yeterli çözüm önerisi sunamamıştır" — listede YOKTU). Bu desen
# aradaki kelimelerden BAĞIMSIZ çalışır: "daha fazla <ne olursa olsun> gerekmektedir/sunmamıştır/
# vermemiştir/sunamamıştır", "yeterli <ne olursa olsun> sunamamıştır", "yeterince <...>mamıştır",
# "somut <ne olursa olsun> verememiştir". GÖSTERDİĞİ ve EKSİK alanlarının İKİSİNE de uygulanır
# (validate_criterion_fields) — model klişeyi hangi alana yazarsa yazsın yakalanır.
# İş emri — KAYIP ANLATI BÖLÜMLERİ / GÖREV 2 (2026-09, sonraki tur) — "daha fazla" SABİT ÖN EKİ
# ÜÇÜNCÜ kez kaçırdı: gerçek örnekte "daha SOMUT örnekler vermesi GEREKMEKTEDİR" kullanıldı —
# "daha fazla" değil "daha somut", "gerekmektedir" değil (bu turda _EXPECTATION_PHRASE_RE'de
# olmayan bir bitiş). "daha fazla" artık "daha <HERHANGİ bir sıfat>" olarak genelleştirildi VE
# bitiş fiil ailesi (gerekmektedir/gerekiyor/gerektiği/beklenmektedir/beklenmiştir/bekleniyor)
# TEK bir yerde toplandı — iki ayrı regex'te (biri "daha fazla", öbürü yalnız "gerektiği/beklen*")
# YARIM YARIM tutulmasının kaçırdığı boşluk kapatıldı.
_FUTURE_VERB_ENDINGS = (r"(?:gerekmektedir|gerekiyor|gerekti[ğg]i|beklenmektedir|beklenmi[şs]tir|bekleniyor|"
                       r"sunmam[ıi][şs]t[ıi]r|vermemi[şs]tir|sunamam[ıi][şs]t[ıi]r|aktaramam[ıi][şs]t[ıi]r|"
                       r"anlatmam[ıi][şs]t[ıi]r)")
_BANNED_PHRASE_FAMILY_RE = re.compile(
    r"daha [\wçğıöşü]+(?:\s+[\wçğıöşü]+){0,4}\s+" + _FUTURE_VERB_ENDINGS +
    r"|yeterli [\wçğıöşü]+(?:\s+[\wçğıöşü]+){0,3}\s+(?:sunamam[ıi][şs]t[ıi]r|sa[ğg]lamam[ıi][şs]t[ıi]r|verememi[şs]tir)"
    r"|yeterince [\wçğıöşü]+(?:mam[ıi][şs]t[ıi]r|memi[şs]tir)"
    r"|somut [\wçğıöşü]+(?:\s+[\wçğıöşü]+){0,3}\s+(?:verememi[şs]tir|sunamam[ıi][şs]t[ıi]r|olu[şs]turamam[ıi][şs]t[ıi]r)",
    re.IGNORECASE)

def banned_phrase_hits(text: str) -> list:
    """GÖREV 2.1 + GÖREV 5 EK — _BANNED_PHRASE_FAMILY_RE + _EXPECTATION_PHRASE_RE ('beklenmiştir'
    ailesi) + eski _FORBIDDEN_EVIDENCE_CLICHE_RE (sabit liste, geriye uyum) birlikte taranır.
    Dönüş: eşleşen ifadeler (boş liste = temiz)."""
    if not text:
        return []
    hits = [m.group(0) for m in _BANNED_PHRASE_FAMILY_RE.finditer(text)]
    hits += [m.group(0) for m in _EXPECTATION_PHRASE_RE.finditer(text)]
    hits += [m.group(0) for m in _FORBIDDEN_EVIDENCE_CLICHE_RE.finditer(text)]
    return hits

# İş emri GÖREV 1 — YAPISAL GEREKÇE ALANLARI. Model artık serbest cümle değil, '~~' ile ayrılmış
# 4 ETİKETLİ alan üretir: G(österdiği, zorunlu) / K(anıt, zorunlu, [dk] damgalı) / E(ksik, isteğe
# bağlı) / S(oru damgası, E doluysa zorunlu). Rapor derleyicisi (kod) bunları NİHAİ cümleye döker
# (bkz. render_criterion_rationale, run_deferred_finish_job yakınında) — sabit 'X. Ancak Y.'
# şablonu YOK, EN AZ 3 farklı şablon rotasyonla kullanılır.
_CRIT_EVIDENCE_HINT = ("G: <adayın ne yapabildiği/bildiği/nasıl yaklaştığı> ~~ "
                       "K: [mm:ss] <transkriptte GERÇEKTEN var olan an + kısa alıntı/özet> ~~ "
                       "E: <GERÇEK bir eksik varsa TEK cümle, yoksa BOŞ bırak> ~~ "
                       "S: <E doluysa mülakatçının bu eksikliği ortaya çıkaran sorusunun [mm:ss] damgası, E boşsa BOŞ bırak>")

_STRUCTURED_EVIDENCE_FORMAT_INSTRUCTIONS = """YAPISAL GEREKÇE FORMATI (KESİN — 'Kanıt ve Analiz' hücresinin TAMAMI budur, BAŞKA HİÇBİR ŞEY YAZMA):
Her hücreye TAM OLARAK şu 4 alanı, aralarına " ~~ " koyarak yaz (etiketler ZORUNLU, sıra sabit):
G: <adayın bu kriterde NE YAPABİLDİĞİ/NE BİLDİĞİ/NASIL YAKLAŞTIĞI — TEK cümle. 'ancak/fakat/ne var ki' YAZMA (geçiş cümlesini SİSTEM ekler, sen ASLA ekleme).>
K: <[mm:ss] transkriptte GERÇEKTEN var olan bir an + kısa somut alıntı/özet. UYDURMA damga YASAK — sistem doğrular, geçemeyen kriter YENİDEN ÜRETTİRİLİR.>
E: <GERÇEKTEN bir eksik/zayıflık varsa TEK cümle; YOKSA bu alanı TAMAMEN BOŞ bırak ('E: ~~' yazıp geç) — eksik UYDURMA, sorulmamış bir konu için EKSİK YAZMA.>
S: <E doluysa, mülakatçının BU eksikliği ortaya çıkaran GERÇEK sorusunun [mm:ss] damgası (uydurma YASAK, sistem doğrular); E boşsa bu alanı BOŞ bırak.>
KANIT SEÇMEDEN ÖNCE SEMANTİK ÖZ-DENETİM (KESİN — her kriter için ayrı ayrı uygula): K'yı yazmadan önce kendine sor: "Bu aday ifadesi GERÇEKTEN BU kriterin tanımını mı destekliyor, yoksa başka bir yetkinliği mi gösteriyor?" Yukarıda kriter için verilen TANIMA (parantez içindeki açıklama) bak — yalnızca kriterin ADINA değil. Aday ifadesi başka bir yetkinliğe (örn. genel ses tonu/üslup, başka bir konudaki deneyim, ilgisiz bir anekdot) aitse, o ifadeyi BU kriter için KULLANMA — sırf zamanca yakın olması veya kriterin adıyla kelime benzerliği taşıması YETERLİ DEĞİLDİR. Bu kritere GERÇEKTEN uygun bir kanıt bulamıyorsan, K'yı uydurmak yerine bu kriteri CRITERION_SCORING_RULE'daki eksik-veri kuralına göre değerlendir.
YÖN KONTROLÜ (KESİN — G/E'yi yazmadan önce ayrıca uygula): Kanıtı G'ye veya E'ye dökmeden önce kendine sor: "Adayın bu ifadesi GERÇEKTEN olumlu bir yetkinlik kanıtı mı, yoksa bir eksiklik/sınırlılık/belirsizlik/olası olumsuz sinyal mi?" Adayın söylediği olumsuz veya zayıf bir ifadeyi SIRF G alanını doldurmak için olumlu bir yetkinlik cümlesine DÖNÜŞTÜRME; kanıtın doğal/açık anlamından DAHA GÜÇLÜ bir sonuç ÇIKARMA. G ve E, kanıtın GERÇEK yönünü (olumlu/olumsuz/belirsiz) korumalı — adayın kendi ifadesi bir sınırlılığa/kaçınmaya işaret ediyorsa bunu E'ye (veya puanı düşük tutarak G'nin ölçülü bir cümlesine) yansıt, iddiayı OLDUĞUNDAN OLUMLU gösterme. Bu yalnız transkriptteki ifade ile rapor iddiası arasındaki YÖN tutarlılığıdır — adayın söyleminin mesleki/regülasyonel açıdan DOĞRU olup olmadığına dair dış bilgiyle hüküm VERME, bu senin işin değil.
ÖRNEK (eksik VAR): G: KDV beyannamesi hazırlama sürecini uçtan uca anlattı ~~ K: [08:12] "önce mizanı kontrol ederim, sonra beyannameyi keserim" ~~ E: Gecikme faizi hesaplamasını sorduğumuzda somut bir yöntem tarifleyemedi ~~ S: [09:40]
ÖRNEK (eksik YOK): G: Enflasyon muhasebesi düzeltmelerini iki farklı senaryo üzerinden karşılaştırdı ~~ K: [14:03] "sabit kıymetlerde endeksleme farkını ayrı hesaplarım" ~~ E: ~~ S:
Bu format DIŞINDA hiçbir cümle/açıklama YAZMA — sistem bu 4 alanı ayrıştırıp NİHAİ cümleyi kendisi kurar; format bozuksa veya damga uydurmaysa bu kriter YENİDEN ÜRETTİRİLİR."""

# İş emri — PRIMARY DEĞERLENDİRME VE KANIT SEÇİMİ GÜVENİLİRLİĞİ (FAZ 1): "önce kanıt, sonra puan"
# sırası mevcut KRİTER|PUAN|KANIT tablosunun kendi içinde sağlanamaz (model metni soldan sağa
# üretir — puan hücresi kanıt hücresinden ÖNCE yazılır). Bunun yerine AYNI primary çağrıda, tablo
# üretilmeden ÖNCE ayrı, deterministik marker'lı bir KANIT HAVUZU bloğu istenir — model önce
# kriter başına TÜM ilgili aday sözlerini toplar, tabloyu (puanı) bundan SONRA üretir. Bu yeni bir
# evaluator/API çağrısı DEĞİLDİR — aynı çağrının ÇIKTI SIRASINI değiştiren bir prompt sözleşmesidir.
_KANIT_HAVUZU_POZISYON_START = "<<<KANIT_HAVUZU_POZISYON>>>"
_KANIT_HAVUZU_POZISYON_SON = "<<<KANIT_HAVUZU_POZISYON_SON>>>"
_KANIT_HAVUZU_PROFIL_START = "<<<KANIT_HAVUZU_PROFIL>>>"
_KANIT_HAVUZU_PROFIL_SON = "<<<KANIT_HAVUZU_PROFIL_SON>>>"

# İş emri madde 15 — davranışsal/teknik ayrımı önceki turda yalnız kriter TANIMLARINDA örtük
# olarak vardı, AÇIK bir GENEL kural olarak request'te doğrulanamamıştı. Şimdi açık kural.
_BEHAVIORAL_EVIDENCE_RULE = (
    "DAVRANIŞSAL KANIT KURALI (KESİN, AÇIK): Salt teknik araç/framework/API/test tekniği/güvenlik/"
    "veritabanı/kodlama BİLGİSİ TEK BAŞINA davranışsal/profil kanıtı DEĞİLDİR. Davranışsal bir "
    "kriterin kanıtı adayın DAVRANIŞINI, YAKLAŞIMINI, KARAR BİÇİMİNİ, İLETİŞİM BİÇİMİNİ, problem "
    "karşısındaki TUTUMUNU, öğrenme/adaptasyon, inisiyatif veya işbirliği DAVRANIŞINI GÖSTEREN "
    "içerikten gelmelidir — kriterin TANIMIYLA doğrudan ilişkili olmalıdır. Teknik bir örnek "
    "GERÇEKTEN davranışsal bir sinyal taşıyorsa kullanılabilir (teknik içerik OTOMATİK yasak "
    "DEĞİLDİR) ama salt teknik bilgi TEK BAŞINA yeterli değildir."
)

def _evidence_pool_instructions(start_marker: str, son_marker: str, label: str, has_real_timestamps: bool) -> str:
    """İş emri madde 1-5, 12-14, 16-18: kanıt havuzu talimatı. Yalnız candidate/aday etiketli
    sözlerden, transkriptin TAMAMI taranarak (tek komşu soru-cevap çifti değil), kriter TANIMIYLA
    gerçekten eşleşen kanıt toplanır. Timestamp-awareness: kaynakta gerçek [mm:ss] yoksa model
    damga üretmeye ZORLANMAZ (madde 17) — yalnız alıntı/metin istenir."""
    ts_note = (
        "Kaynak transkriptte GERÇEK [mm:ss] zaman damgaları VAR — GERÇEKTEN o anda geçen bir sözü "
        "alıntılıyorsan kaynaktaki damgayı da yazabilirsin; damga UYDURMA/tahmin ETME, emin "
        "değilsen damgasız yaz."
        if has_real_timestamps else
        "Kaynak transkriptte GERÇEK [mm:ss] zaman damgası YOK — alıntılarında [mm:ss] formatı "
        "KULLANMA (uydurma damga KESİNLİKLE YASAK), yalnız metin/alıntı yaz."
    )
    return f"""{start_marker}
Aşağıdaki tabloyu üretmeden ÖNCE, {label} kriterlerinin HER BİRİ için, adayın bu kriterle
GERÇEKTEN ilgili TÜM sözlerini transkriptin TAMAMINI tarayarak (yalnız en yakın tek soru-cevap
çiftini değil — aday ilk soruda kısa, sonraki bir turda ayrıntılı cevap vermiş olabilir, ikisini
de topla) burada listele:

KRİTER: <kriter adı — aşağıdaki tablodaki adla BİREBİR AYNI yaz, kısaltma/parafraz YAPMA>
E1: "<adayın gerçek sözü/alıntısı>"
E2: "<varsa ikinci ilgili söz>"
...

Yalnız ADAY/CANDIDATE etiketli sözleri kullan — mülakatçı/sistem sözünü ASLA E olarak yazma.
{_BEHAVIORAL_EVIDENCE_RULE if label == "PROFİL" else ""}
Evidence ilgili kriterin TANIMIYLA gerçekten eşleşmeli — salt ortak kelime YETERLİ DEĞİLDİR; başka
bir kriter için anlamlı olan bir sözü yalnız kelime benzerliği yüzünden BURAYA taşıma.
İlgili gerçek aday sözü YOKSA: E: YOK yaz — ilgili söz VARKEN kolaylık olsun diye YOK yazma.
{ts_note}
{son_marker}"""

def build_criteria_table_filled(criteria: list, evidence_header: str = "Kanıt ve Analiz") -> str:
    """DETERMİNİSTİK kriter tablosu: satırlar pozisyondan gelir, model AYNEN doldurur.
    Model satır ekleyemez/çıkaramaz/yeniden adlandıramaz. Payda (tavan) sabit.
    Eksik veri kuralı: bkz. CRITERION_SCORING_RULE (payda dışı 'Değerlendirilemedi (sistem)' varsayılan).
    Kanıt ve Analiz hücresi artık YAPISAL (bkz. _CRIT_EVIDENCE_HINT + _STRUCTURED_EVIDENCE_FORMAT_INSTRUCTIONS).
    İŞ EMRİ — OPENAI PRIMARY KRİTER DEĞERLENDİRME DÜZELTMESİ: tablodan ÖNCE, varsa her kriterin
    'desc' tanımı ayrı bir blok olarak eklenir — bu, Claude reviewer'ın zaten aldığı bilgiyle
    (bkz. _reviewer_criteria_block) OpenAI primary arasındaki asimetriyi kapatır. Tanım tablo
    HÜCRESİNE değil, tablodan önceki düz metne yazılır — kriter adı hücresi (cells[0]) saf kalır,
    aşağı akıştaki isim eşleştirme (_name_score, recompute_and_fix_score/recompute_profile_section)
    ETKİLENMEZ."""
    lines = []
    _defs = [(c.get("name"), (c.get("desc") or "").strip()) for c in criteria if (c.get("desc") or "").strip()]
    if _defs:
        lines.append("KRİTER TANIMLARI (kanıt seçerken ve kanıtın kriteri GERÇEKTEN desteklediğini kontrol ederken bu tanımları kullan):")
        for _name, _desc in _defs:
            lines.append(f"- {_name}: {_desc}")
        lines.append("")
    lines.append(f"| Kriter | Puan | {evidence_header} |")
    lines.append("|--------|------|-----------------|")
    for c in criteria:
        lines.append(f"| {c['name']} | {_CRIT_CELL_HINT.format(w=c['weight'])} | {_CRIT_EVIDENCE_HINT} |")
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

# ============ RAPOR GÖVDESİ — YENİDEN TASARIM (2026-09 iş emri) — TEK KAYNAK ============
# Eski REPORT_BODY_SECTIONS / PUAN 1-PUAN 2 terminolojisi ve ---STANDARTCV--- ayrı bloğu
# KALDIRILDI. Model artık YALNIZCA 7 bölümü, ===BAŞLIK=== ayraçlarıyla üretir (aşağıda
# build_report_content_prompt). Değerlendirme Puanları tablosu, Görüntü ve Ses Gözlemi, Beyan
# Tutarlılığı, İkinci Değerlendirici Görüşü, Metodoloji Notu ve Ekler TAMAMEN DETERMİNİSTİKTİR —
# assemble_final_report() içinde kod tarafından üretilir, modelden İSTENMEZ. Karar (ÖNERİ) da
# modelden istenmez; yalnız Genel Puan'dan TEK bir yerde (decide_recommendation) hesaplanır —
# rapor metni karar üretmez (iş emri madde 21). parse_llm_report_sections bu ayraçları okur.
def _tr_upper(s: str) -> str:
    """Türkçe-doğru büyük harfe çevirir. Python'un yerleşik str.upper()/lower() Türkçe İ/ı
    harflerini YANLIŞ çevirir ('İ'.lower() == 'i̇' — noktalı bileşik karakter, düz 'i' DEĞİL);
    bu, aşağı yukarı EVERY başlık eşleşmesini (İ içermeyen 'GÜÇLÜ YÖNLER' hariç) sessizce
    bozuyordu — kök neden, canlı testte yakalandı. Önce Türkçe i/ı'yı doğru çevirip SONRA
    standart .upper() uygular; ASCII olmayan diğer harfler (Ç,Ğ,Ö,Ş,Ü) zaten .upper()'da doğru."""
    return (s or "").replace("i", "İ").replace("ı", "I").upper()

_REPORT_SECTION_ALIASES = {
    _tr_upper("Yönetici Özeti"): "yonetici_ozeti",
    # İş emri — KAYIP ANLATI BÖLÜMLERİ (2026-09, sonraki tur) / GÖREV 1.1 — eski (2026-09-08
    # öncesi) rapor formatında Yönetici Özeti ile kriter tabloları ARASINDA bir anlatı katmanı
    # vardı (Analitik Düşünme, Problem Çözme, Kavrama ve İletişim, Öne Çıkan Proje, CV↔Mülakat↔
    # Pozisyon Uyumu, Dil Gözlemi); 2026-09 yeniden tasarımında bu katman "iş emrinin 14 bölümlük
    # listesine dahil değil" gerekçesiyle TAMAMEN atlandı — ama bu katman kararı GEREKÇELENDİREN
    # katmandı, kaybı fark edilmeden geçti. Aşağıdaki 6 anahtar bu katmanı GERİ getiriyor (YENİ
    # kalite kurallarıyla — bkz. build_report_content_prompt). "Tutarlılık / Çelişki Analizi" ve
    # "Değerlendirilemeyen Alanlar" BİLİNÇLİ OLARAK yeniden eklenmedi — işlevleri sırasıyla KORUNAN
    # Beyan Tutarlılığı (deterministik) ve YENİ Puanlama Kapsamı (deterministik) bölümleriyle ZATEN
    # karşılanıyor; aynı bilgiyi iki ayrı bölümde tekrar etmek bu iş emri serisinin baştan beri
    # savaştığı "tekrar" sorununu yeniden üretirdi.
    _tr_upper("Analitik Düşünme ve Muhakeme"): "analitik_dusunme",
    _tr_upper("Problem Çözme ve Karar Verme Yaklaşımı"): "problem_cozme",
    _tr_upper("Kavrama ve İletişim"): "kavrama_iletisim",
    _tr_upper("Öne Çıkan Proje ve Deneyimler"): "one_cikan_proje",
    _tr_upper("CV ↔ Mülakat ↔ Pozisyon Uyumu"): "cv_mulakat_pozisyon_uyumu",
    _tr_upper("Dil Gözlemi"): "dil_gozlemi",
    _tr_upper("Pozisyon Yetkinlikleri"): "pozisyon_yetkinlikleri",
    _tr_upper("Kişisel ve Bilişsel Profil"): "profil",
    _tr_upper("Güçlü Yönler"): "guclu_yonler",
    _tr_upper("Gelişim Alanları"): "gelisim_alanlari",
    _tr_upper("CV Özeti"): "cv_ozeti",
    _tr_upper("Genel Kanı"): "genel_kani",
    _tr_upper("Takip Mülakatı Soruları"): "takip_sorulari",
}

def parse_llm_report_sections(text: str) -> dict:
    """===BAŞLIK=== ayraçlı model çıktısını {section_key: content} sözlüğüne çevirir. Bilinmeyen/
    eksik ayraç → o bölüm sözlükte hiç yer almaz (uydurma yok, assemble_final_report atlar).
    İçerik tam olarak 'YOK' ise boş sayılır (bölüm oluşturulmaz). Eşleşme _tr_upper ile yapılır
    (bkz. üstteki not) — model başlığı Yönetici Özeti/YÖNETİCİ ÖZETİ/yönetici özeti gibi hangi
    harf büyüklüğüyle yazarsa yazsın doğru eşleşir."""
    out = {}
    if not text:
        return out
    parts = re.split(r'(?m)^[ \t]*={3,}[ \t]*([^=\n]+?)[ \t]*={3,}[ \t]*$', text)
    for i in range(1, len(parts) - 1, 2):
        key = _REPORT_SECTION_ALIASES.get(_tr_upper(parts[i].strip()))
        if not key:
            continue
        content = parts[i + 1].strip()
        if _tr_upper(content) in ("YOK", "YOK.", ""):
            content = ""
        out[key] = content
    return out

def build_report_content_prompt(criteria_table_filled: str, profile_table_filled: str, has_real_timestamps: bool = False) -> str:
    """Modelden istenen TEK gövde: 13 bölüm, ===BAŞLIK=== ayraçlı. L1/L2/L3 ORTAK — seviyeler
    arası içerik farkı yoktur; CV yoksa/kamera-ses yoksa ilgili içerik zaten deterministik
    katmanda atlanır, modele ayrı bir 'seviye talimatı' verilmesine gerek yok.
    İş emri — KAYIP ANLATI BÖLÜMLERİ (2026-09, sonraki tur) — 6 bölüm (Analitik Düşünme,
    Problem Çözme, Kavrama ve İletişim, Öne Çıkan Proje, CV↔Mülakat↔Pozisyon Uyumu, Dil Gözlemi)
    Yönetici Özeti'nden HEMEN SONRA, ve Genel Kanı raporun SONUNDA (Takip Soruları'ndan önce)
    eklendi — eski (2026-09-08 öncesi) formatta vardı, yeniden tasarımda kayboldu; kararı
    GEREKÇELENDİREN katmandı. Puanlama Kapsamı/Öneri Gerekçesi/Profil Veto Kontrolü modelden
    İSTENMEZ — TAMAMEN deterministik (bkz. render_puanlama_kapsami/render_oneri_gerekcesi/
    render_profile_veto_control, finalize_interview/append_reviewer_section içinde eklenir).
    İş emri — PRIMARY DEĞERLENDİRME VE KANIT SEÇİMİ GÜVENİLİRLİĞİ (FAZ 1): POZİSYON/PROFİL
    tablolarından HEMEN ÖNCE ayrı, marker'lı bir KANIT HAVUZU bloğu istenir (_evidence_pool_
    instructions) — fiziksel çıktı sırası KANIT HAVUZU → TABLO'dur, model puanı kanıtı yazdıktan
    SONRA üretir. has_real_timestamps, kaynak transkriptte gerçek [mm:ss] olup olmadığına göre
    havuz talimatının timestamp beklentisini ayarlar (bkz. _transcript_has_real_timestamps)."""
    return f"""Aşağıdaki bölümleri, TAM OLARAK bu sırayla ve TAM OLARAK bu ayraçlarla üret. Ayraç satırlarını (===...===) AYNEN kopyala; başka hiçbir başlık/ayraç EKLEME. Bir bölümde yazacak GERÇEKTEN somut bir şey yoksa o bölümün içeriğine SADECE "YOK" yaz (sistem o bölümü rapordan çıkarır) — asla "belirtilecek bir şey yok" gibi dolgu cümle kurma, asla "-", "—" veya "bulunmamaktadır" yazma. Aşağıdaki HİÇBİR bölümde yasak kalıp (banned_phrase_hits — "daha fazla/somut/derin ... gerekmektedir/beklenmektedir/gerektiği" ailesi, "beklenmiştir" ailesi) KULLANMA; sistem bunu tespit edip o CÜMLEYİ siler. Hiçbir bölümde bir kriterin KANIT alanındaki veya Pozisyon/Profil tablolarındaki cümleyi AYNEN tekrar ETME. Sorulmamış bir konuda eksiklik/olumsuz yargı YAZMA.

===YÖNETİCİ ÖZETİ===
2-3 kısa paragraf, TOPLAM yaklaşık 150-250 kelime (bu sınırı AŞMA). İçerik: aday kim, hangi deneyime sahip; mülakatta NEYİ SOMUT OLARAK gösterdi; hangi konularda güçlü, hangi konularda değil; pozisyona uygunluk açısından sonuç. Dakika damgası KULLANMA (detay aşağıdaki bölümlerde). Bir karar/öneri kelimesi (Reddet/İşe Al/Değerlendir vb.) YAZMA — karar ayrı, sistem tarafından üretilir. Aşağıdaki bölümlerdeki cümleleri AYNEN kopyalama.
KESİN YASAK — GELECEĞE DÖNÜK BEKLENTİ CÜMLESİ KURMA ("aday X yapmalı/sunmalı/göstermeli", "...beklenmektedir", "...gerekmektedir/gerektiği" gibi "adayın NE YAPMASI GEREKTİĞİ" cümleleri) — bu özet adayın MÜLAKATTA NE YAPTIĞINI/GÖSTERDİĞİNİ anlatır, ne yapması gerektiğini DEĞİL (o Gelişim Alanları'nın işi). Bu ihlal edilirse sistem cümleyi SİLER.

===ANALİTİK DÜŞÜNME VE MUHAKEME===
1-2 cümle, EN AZ BİR [dk] damgasıyla: adayın problemi nasıl parçaladığı, neden-sonuç kurma biçimi, veri/örnek kullanımı — somut. Pozisyon/Profil tablolarındaki gerekçenin TEKRARI OLMAYACAK (orada puan gerekçesi var, burada niteliksel bir gözlem). Somut bir şey yoksa YOK yaz.

===PROBLEM ÇÖZME VE KARAR VERME YAKLAŞIMI===
1-2 cümle, EN AZ BİR [dk] damgasıyla: izlediği yöntem, düşündüğü seçenekler, riskler, sonucu nasıl takip ettiği — somut. Tekrarı olmayacak. Somut bir şey yoksa YOK yaz.

===KAVRAMA VE İLETİŞİM===
1-2 cümle, EN AZ BİR [dk] damgasıyla: soruyu doğru anlama, cevabı yapılandırma, açıklık — ya da tersi (dağılma, yanlış anlama). Tekrarı olmayacak. Somut bir şey yoksa YOK yaz.

===ÖNE ÇIKAN PROJE VE DENEYİMLER===
Transkriptte anlatılan GERÇEKTEN somut bir proje/deneyim varsa (adayın kişisel katkısı + sonucu), [dk] damgasıyla özetle. Güçlü Yönler'in TEKRARI OLMAYACAK (orası yetkinlik değerlendirmesi/yorumu, burası SOMUT olay/proje anlatımı — salt aktarım, yorum yok). Böyle bir proje/deneyim anlatılmadıysa YOK yaz.

===CV ↔ MÜLAKAT ↔ POZİSYON UYUMU===
Üç soruyu cevapla: (a) CV'de/sözlü beyanda iddia edilen yetkinlikler mülakatta doğrulandı mı, (b) pozisyonun ÇEKİRDEK alanıyla adayın fiili çalışma alanı örtüşüyor mu, (c) alan dışı/devretme beyanı varsa (ör. "benim alanım değil", "yöneticime sorarım") burada da SOMUT belirt. Beyan Tutarlılığı bölümünden FARKLIDIR (o sayısal/kimlik alanı karşılaştırması — deneyim yılı, eğitim, tarih; bunu TEKRARLAMA), bu bölüm YETKİNLİK-POZİSYON uyumu. Somut bir şey yoksa YOK yaz.

===DİL GÖZLEMİ===
Adayın dil tercihine/hakimiyetine dair GERÇEKTEN somut bir gözlem varsa (hangi konuda dil değiştirdiği, pozisyonun dil gereksinimiyle ilişkisi) yaz. Gözlem YOKSA "YOK" yaz (sistem bölümü hiç basmaz) — "Belirtilecek bir dil gözlemi yok" gibi kendini çürüten dolgu cümle YASAK.

===POZİSYON YETKİNLİKLERİ===
{_evidence_pool_instructions(_KANIT_HAVUZU_POZISYON_START, _KANIT_HAVUZU_POZISYON_SON, "POZİSYON", has_real_timestamps)}

{criteria_table_filled}
(YUKARIDAKİ TABLOYU AYNEN KULLAN: satır ekleme/çıkarma/yeniden adlandırma YOK, tavanı AŞMA. Puanı, yukarıda az önce kendi ürettiğin kanıt havuzundaki İLGİLİ kriterin TÜM evidence'larına göre ver — yalnız ilk/kısa bir cevaba bakıp havuzdaki devamındaki güçlü kanıtları yok sayma.)
{_SCOPE_PRIORITY_RULE}
{CRITERION_SCORING_RULE}
{SCORING_RUBRIC}
{_EVIDENCE_COMPLETENESS_AND_MATCH_RULE}
{_STRUCTURED_EVIDENCE_FORMAT_INSTRUCTIONS}

===KİŞİSEL VE BİLİŞSEL PROFİL===
(Pozisyon yetkinliklerinden AYRI, pozisyondan bağımsız, her aday için SABİT kriter seti — işe alım kararını TEK BAŞINA belirlemez, yalnızca destekleyici bir puandır.)
{_evidence_pool_instructions(_KANIT_HAVUZU_PROFIL_START, _KANIT_HAVUZU_PROFIL_SON, "PROFİL", has_real_timestamps)}

{profile_table_filled}
(YUKARIDAKİ TABLOYU AYNEN KULLAN.) Aynı kanıt standardı, ÖNCEL KURAL, GEREKÇE YAZIM KURALLARI ve CEVABIN TAMAMINI DEĞERLENDİR VE KANITI DOĞRU KRİTERLE EŞLEŞTİR kuralı (yukarıda) burada da geçerlidir: yüksek puanda ≥2 bağımsız kanıt, düşük puanda somut gerekçe, tahmin YOK, klişe kalıp YOK, damga yalnız kritik kanıtta. Dayanaksız çıkarım, kişilik teşhisi, IQ/zekâ yorumu YASAK. Puanı, yukarıda az önce ürettiğin profil kanıt havuzundaki İLGİLİ kriterin TÜM evidence'larına göre ver.
{_STRUCTURED_EVIDENCE_FORMAT_INSTRUCTIONS}

===GÜÇLÜ YÖNLER===
Her madde PARAGRAF halinde (tek satır/etiket DEĞİL) — bir madde: (a) aday NE YAPABİLİYOR, (b) bunu mülakatın neresinden anlıyoruz (somut örnek/alıntı, EN AZ BİR [dk] damgasıyla — damgasız "X'i mülakatta belirttiği örneklerle desteklemiştir" gibi GENEL/DAYANAKSIZ övgü cümlesi YASAK, sistem bunu tespit edip SİLER), (c) pozisyon açısından ne anlama geliyor. Yalnız GERÇEKTEN güçlü, somut bulgular — genel ifade YASAK ("Luca kullanıyor" değil, "Luca'da e-fatura iptal/iade süreçlerini bağımsız yürütebilir" gibi somut ve pozisyona bağlanmış). Madde sayısı gerçekten olan kadar — üçe tamamlama YOK. Kriter gerekçelerinin (yukarıdaki tablolardaki) TEKRARI OLMAYACAK — orada puan gerekçesi var, burada yöneticinin göreceği "bu aday işe başlayınca ne yapabilir" var.

===GELİŞİM ALANLARI===
Her madde PARAGRAF halinde: (a) aday NE YAPAMIYOR/NEREDE ZORLANDI, (b) hangi somut senaryoda/soruda kendini gösterdi ([dk] damgasıyla), (c) bu eksiğin işte YARATABİLECEĞİ SOMUT RİSK. Risk niteliğinde bir bulgu varsa (tutarsız beyan, mevzuata aykırı yaklaşım, iç kontrol zaafı, ALAN DIŞI/DEVRETME beyanı — bkz. ÖNCEL KURAL, kurumsal ortamda çalışmayı zorlaştıracak somut bir tutum/davranış vb.) paragrafın başına "RİSK:" yaz — bir kriterde ALAN DIŞI/DEVRETME beyanı tespit ettiysen bunu BURADA da RİSK olarak belirtmen ZORUNLUDUR (sistem, hiç belirtilmediyse kendisi bir RİSK paragrafı ekler). Kanıtsız gelişim alanı üretme; madde sayısı gerçekten olan kadar. Kriter gerekçelerinin TEKRARI OLMAYACAK (yukarıdaki not — Güçlü Yönler için de geçerli). Genel ifade YASAK ("analitik düşünmede derinlik eksikliği" değil, hangi senaryoda nasıl zorlandığı). Bir RİSK paragrafının [dk] damgası transkriptte GERÇEKTEN var olan bir ana karşılık gelmiyorsa sistem o paragrafı rapordan ÇIKARIR — damgasız/uydurma RİSK YAZMA.

===CV ÖZETİ===
CV metninden ve/veya adayın mülakatta SÖZLÜ beyan ettiğinden yalnızca GERÇEKTEN bilgi olan alanları, her biri ayrı satırda, şu etiketlerle yaz: Eğitim, Deneyim, Teknik Yetkinlikler, Sektör Yetkinlikleri, Diller, Sertifikalar. HER SATIRI TAM OLARAK "Etiket: içerik" biçiminde yaz — etiket ile içerik arasına İKİ NOKTA (:) koy, ASLA slash (/) veya başka bir ayraç kullanma (ör. "Eğitim: Ticaret Meslek Lisesi mezunu" — "Eğitim / Ticaret Meslek Lisesi mezunu" DEĞİL). Adayın SÖZLÜ beyanındaki SPESİFİK ifadeyi AYNEN kullan, GENELLEŞTİRME/kısaltma yapma (ör. aday "ticaret meslek lisesi mezunuyum" dediyse "Ticaret Meslek Lisesi mezunu" yaz — asla daha genel bir kategoriye, ör. sadece "Lise mezunu", İNDİRGEME). Bilgi CV'de yoksa ama adayın SÖZLÜ beyanından geliyorsa satırın sonuna "(kaynak: sözlü beyan)" ekle. Bir alanda hiç bilgi YOKSA o satırı hiç YAZMA (atla). Bu bölümde DEĞERLENDİRME/yorum yapma, yalnız özetle.

===GENEL KANI===
2-4 cümlelik bir SENTEZ: kriter tabloları, Güçlü Yönler, Gelişim Alanları ve CV↔Mülakat↔Pozisyon Uyumu'ndaki bulguları BİR ARAYA getiren bütüncül bir kanı. Bir karar/öneri kelimesi (Reddet/İşe Al/Değerlendir) YAZMA — o ayrı, sistem tarafından üretilir. Yukarıdaki bölümlerin cümlelerini AYNEN tekrar ETME, sentezle. Somut bir sentez kurulamıyorsa YOK yaz.

===TAKİP MÜLAKATI SORULARI===
En fazla 3-5 soru (üst sınır — hedef DEĞİL: somut belirsizlik azsa 3'ten az da yazabilirsin, hatta hiç olmayabilir). YALNIZ bu mülakatta ortaya çıkan SOMUT belirsizliklere yönelik olsun. Her soru şu kaynaklardan birine dayanmalı: (a) adayın cevap veremediği/atladığı bir soru, (b) adayın kendi ağzıyla belirttiği bir bilgi eksikliği, (c) örnek istenip alınamayan/yüzeysel kalmış bir cevap, (d) CV'de yazılı olup mülakatta doğrulanamayan bir yetkinlik, (e) ikinci değerlendiricinin işaret edebileceği türden bir belirsizlik. Her sorunun transkriptte somut bir dayanağı olmalı.
ZORUNLU BİÇİM (GÖREV 3 — dayanaksız/uydurma soruların sisteme yakalanması için): her soru satırının BAŞINA, bu belirsizliği ortaya çıkaran mülakatçı sorusunun/anının GERÇEK zaman damgasını şu biçimde ekle: "[dayanak: mm:ss] <soru metni>". Bu damga transkriptte GERÇEKTEN var olan bir mülakatçı sorusuna karşılık gelmelidir — uydurma damga YASAK; sistem doğrular, geçemeyen soru RAPORDAN SİLİNİR (damga rapora BASILMAZ, yalnız doğrulama içindir).
KURAL: Sorular adayın GEÇMİŞİNE ve BİLDİKLERİNE yönelik olsun, GELECEK PLANINA değil.
  Yanlış (GELECEK PLANI sorusu): "[dayanak: 12:03] Bu konuda kendinizi nasıl geliştirmeyi planlıyorsunuz?"
  Doğru (GEÇMİŞE/BİLGİYE dayalı): "[dayanak: 12:03] Şu yöntemi bildiğinizi belirttiniz; hangi koşullarda ve hangi kayıtla uyguladığınızı somut bir örnek üzerinden açıklar mısınız?"
KESİNLİKLE YASAK — aşağıdaki kalıplarla veya eş anlamlılarıyla SORU YAZMA (bunlar genel kariyer-koçluğu sorularıdır, HER adaya sorulabilir, BU adayı değerlendirmeye yaramaz; ayrıca "daha fazla bilgi verebilir misiniz" gibi tek başına hiçbir şey sormayan genel-geçer sorular da YASAK — sistem bu kalıplardan birini tespit ederse o SORUYU RAPORDAN SİLER):
  - "hangi adımları atmayı planlıyorsunuz"
  - "nasıl bir gelişim planı oluşturabilirsiniz"
  - "hangi kaynakları kullanabilirsiniz"
  - "hangi eğitim veya kaynaklardan yararlanmayı düşünüyorsunuz"
  - "kendinizi nasıl geliştirmeyi planlıyorsunuz"
  - "hangi stratejileri uygulayabilirsiniz"
  - "daha fazla bilgi verebilir misiniz" / "daha fazla bilgi verir misiniz"
Somut bir belirsizlik YOKSA "YOK" yaz — sayıyı tamamlamak için soru uydurma.
===BÖLÜM SONU==="""

# Takip mülakatı sorularındaki YASAK (genel gelişim koçluğu) kalıplarının deterministik tespiti.
# İş emri GÖREV 5.2 (bu tur) — artık aktif müdahale eder (bkz. finalize_interview: satırı SİLER,
# madde 25'teki "sessizce yeni kural uydurma" yasağı bu SATIR-BAZLI silmeye uygulanmaz çünkü bu,
# şartnamenin AÇIKÇA istediği davranış). KAPSAM GENİŞLETİLDİ: önceki tur yalnız 6 SABİT kalıbı
# birebir arıyordu; model bunları paraphrase edince (ör. "hangi kaynaklardan yararlanmayı
# düşünüyorsunuz" / "nasıl aşmayı planlıyorsunuz") kaçıyordu — canlı-benzeri sentetik testte
# yakalandı. Artık GÖREV 5.4'ün ayırt edici ilkesine (GELECEK PLANI vs GEÇMİŞ/BİLGİ) göre genel
# desenler de yakalanıyor: "...planlıyorsunuz" (planlamak HER ZAMAN gelecek-yönelimlidir) ve
# "yararlanmayı/kullanmayı/geliştirmeyi/uygulamayı düşünüyorsunuz" (gelecek niyeti sorgusu).
# İş emri — KAYIP ANLATI BÖLÜMLERİ / GÖREV 3.1 (2026-09, sonraki tur) — "...düşünüyorsunuz" YALNIZ
# belirli fiillerin (yararlanmayı/kullanmayı/...) ARDINDAN aranıyordu; "nasıl geliştirebileceğinizi
# düşünüyorsunuz" bambaşka bir çekim (-ebileceğinizi, yeterlilik+gelecek nominalizasyonu) kullandığı
# için KAÇTI. Artık "düşünüyorsunuz" TEK BAŞINA (hangi fiilin ardından gelirse gelsin) yakalanıyor —
# bu bağlamda (takip sorusu, adaya yöneltilen) HER ZAMAN gelecek niyeti/planı sorgusudur, GEÇMİŞE
# dayalı bir soru "düşünüyorsunuz" ile bitmez. Ayrıca kesin gelecek kipi ("-eceksiniz/-acaksınız").
_FORBIDDEN_FOLLOWUP_RE = re.compile(
    r"planl[ıi]yorsunuz"
    r"|d[üu][şs][üu]n[üu]yorsunuz"
    r"|nas[ıi]l bir geli[şs]im plan[ıi]"
    r"|hangi kaynaklar[ıi]?(?:dan)? kullanabilirsiniz"
    r"|hangi strateji(?:leri)? uygulayabilirsiniz"
    r"|daha fazla bilgi ver(?:ebilir|ir) misiniz"
    r"|[ae]ceksiniz\b",
    re.IGNORECASE)

def detect_forbidden_followup_patterns(text: str) -> list:
    """Takip Mülakatı Soruları içeriğinde yasaklı genel-gelişim kalıbı geçen SATIRLARI döner
    (boş liste = temiz). GÖREV 2.2/3.5 — artık _FORBIDDEN_FOLLOWUP_RE'ye ek olarak GÖREV 2.2'nin
    genişletilmiş yasaklı-kalıp ailesi (banned_phrase_hits) de aynı satırlarda taranır (bir soru
    hem 'genel gelişim koçluğu' kalıbında OLMASA bile klişe bir ifadeyle sorulmuş olabilir).
    Kaldırmaz — çağıran (finalize_interview) aktif olarak siler."""
    if not text:
        return []
    return [ln.strip() for ln in text.splitlines()
            if ln.strip() and (_FORBIDDEN_FOLLOWUP_RE.search(ln) or banned_phrase_hits(ln))]

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
    total_weight = sum(c["weight"] for c in pos["criteria"])
    category = pos.get("category", "Genel")

    cv_section = ""
    if cv_text and len(cv_text.strip()) > 20:
        # Maliyet kontrolü: CV sadece çekirdek kadar verilir. Uzun CV raporu şişirmesin.
        cv_section = f"CV ÖZETİ/İÇERİĞİ (tutarlılık kontrolü için kullan):\n{cv_text[:1800]}"
    else:
        cv_section = "CV yok. Deneyimi kısa ve net sorularla öğren. CV yok diye mülakatı durdurma."

    candidate_profile = f"""ADAY PROFİLİ (KAYIT FORMU BEYANI):
E-posta: {email or '-'}
Eğitim: {education or '-'}
Üniversite: {university or '-'}
Bölüm: {department or '-'}
Deneyim yılı: {experience_years if experience_years is not None else '-'}
(Bu beyanın CV/sözlü cevapla çelişip çelişmediğini sistem ayrıca deterministik olarak karşılaştırır — sen bunu raporlama.)
"""
    admin_instruction = ""
    if ai_note and ai_note.strip():
        admin_instruction = f"""
ADAY ÖZEL AI NOTU — BAĞLAYICI TALİMAT (aday görmez, mutlaka uygula, opsiyonel öneri DEĞİL):
{ai_note.strip()[:1200]}
Bu notu mülakat boyunca aktif bir koşul olarak uygula: notta bir konu/iddia geçiyorsa en az 1 soruyla doğrudan test/doğrula; notta bir değerlendirme önceliği belirtiliyorsa (örn. belirli bir yetkinliğe ağırlık ver) soru dağılımını buna göre şekillendir. Bu notu görmezden gelip standart akışa devam etmek KABUL EDİLEMEZ.
Raporda bu notun nasıl ele alındığını (hangi soru/sorularla test edildi, sonucu ne oldu) Yönetici Özeti'nde veya ilgili olduğu Güçlü Yönler/Gelişim Alanları maddesinde somut olarak yansıt — ayrı bir başlık AÇMA.
"""

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

    # 2026-09 rapor yeniden tasarımı — TEK gövde şablonu, level'dan bağımsız (bkz.
    # build_report_content_prompt). Kimlik/tarih alanları artık modelden İSTENMEZ: Başlık
    # Şeridi'ni sistem candidate/interview kayıtlarından DETERMİNİSTİK üretir.
    report_body_l13 = build_report_content_prompt(
        build_criteria_table_filled(pos["criteria"]), build_profile_table_filled())

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

KRİTER KAPSAMA (ÖNEMLİ — İŞ EMRİ): Amaç, tanımlı kriterlerin TAMAMININ mülakat sırasında gerçekten ölçülme fırsatı bulmasıdır — bu opsiyonel bir "varsa kontrol et" değil, mülakatın asıl işlevidir. Akış sırasında mekanik kapı YOK — sırayı sen belirlersin. Ancak mülakatı BİTİRMEDEN ÖNCE her kriteri tek tek gözden geçir: hiç dokunulmamış bir kriter varsa en az bir soru sor. Bu kontrol kapanış anındadır, akışı bölmez. Adayın verdiği TEK bir cevap birden fazla kriter için geçerli kanıt oluşturabilir — böyle bir kriteri tekrar sormaya ZORUNLU değilsin, gereksiz tekrar soru üretme. Yine de sorulamayan bir kriter kalırsa raporda "değerlendirilmedi" olarak işaretlenir — uydurma değerlendirme yapma.

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
- Davranış/tutum gözlemi (agresiflik, sabırsızlık, kabalık, kaçamaklık) veya pozisyon uyumsuzluğu (aday alanının farklı olduğunu belirtti) fark edersen, raporda ilgili olduğu Gelişim Alanları/Yönetici Özeti maddesinde dakika + adayın sözüyle SOMUT yaz (davranış tek başına puan düşürmez).
{CRITERION_SCORING_RULE}
{SCORING_RUBRIC}
- Sistem kaynaklı eksik ('Değerlendirilemedi (sistem)') kriterleri raporda AYRI listele; bunlara puan verme, toplamı yalnızca puanlanan kriterlerin ağırlığına normalize et.
- PUAN TAVANI (KESİN): Hiçbir kriter puanı kendi tavanını (ağırlığını) AŞAMAZ ("12/10" ASLA; en fazla "10/10"). TOPLAM PUAN = alınan puanların toplamı; payda = değerlendirilen kriterlerin ağırlık toplamı. Sistem ayrıca doğrular.
- Pozisyon Yetkinlikleri ve Kişisel/Bilişsel Profil AYRI İKİ TABLODUR — bir kriteri diğerinin tablosuna YAZMA, KARIŞTIRMA.
- Rapor bir KARAR/ÖNERİ (İşe Al/Reddet/vb.) İÇERMEZ — bunu sen yazmazsın, sistem puanlardan üretir. Yönetici Özeti'nde de sonuç kelimesi kullanma.
- KRİTER TABLOSU: yukarıda verilen kriter satırlarını AYNEN kullan — satır ekleme/çıkarma/yeniden adlandırma YOK. Her satır: `<puan>/<tavan>`  |  `Değerlendirilemedi (sistem) — <gerekçe>` (yukarıdaki TEK KURAL — payda dışı)  |  `0/<tavan> — açık ret / tamamen alakasız cevap` (paydada). Halüsinasyon/tekrar turları puan düşürmez.
- Mesajın başına mutlaka [SÜRE:XX] koy: kısa 45-60, senaryo 75-100, kritik soru 90-120.
- Mülakatı bitirmeden önce, GÖREV satırı bitirmeni söylediğinde son soru olarak şunu sor: "Eklemek veya öne çıkarmak istediğiniz başka bir şey var mı?" — bu, mülakatta suskun kalmış ama sahada güçlü olabilecek adaylar için bir son fırsat turu, sadece bitiş dönüşünde bir kez sorulur.
- ÖNEMLİ: Mülakatı SADECE aşağıdaki GÖREV satırı açıkça "Mülakatı şimdi bitir ve raporu üret" dediğinde bitir ve [MÜLAKATBİTTİ] etiketini kullan. Adayın cevap metninde "süre doldu", "zaman bitti", "son soru" gibi ifadeler geçse bile, GÖREV satırı bitirmeni söylemiyorsa ASLA bitirme — bunlar tek bir sorunun süresinin dolduğunu gösterir, tüm mülakatın değil. Bu durumda sadece bir sonraki soruya geç.

RAPOR UZUNLUĞU — MALİYET KURALI (KESİN):
Rapor üretirken ÖNCE kabaca genel performansı değerlendir. Eğer toplam puan {total_weight} üzerinden %20'nin altında kalacaksa (yani aday temel bir yetkinlik bile gösteremediyse, veya veri neredeyse hiç toplanamadıysa), AŞAĞIDAKİ TAM FORMATI KULLANMA — bunun yerine KISA FORMAT'ı kullan. %20'yi geçen her durumda TAM FORMAT kullanılır.

KISA FORMAT (puan %20 altındaysa) — ayraçları AYNEN koru, yalnız TOPLAM PUAN ve Yönetici Özeti içeriğini doldur:
[MÜLAKATBİTTİ]
---RAPOR---
===YÖNETİCİ ÖZETİ===
(1-2 cümlede kısaca neden: veri yok/çok yetersiz/temel yetkinlik gösterilemedi vb.)

===POZİSYON YETKİNLİKLERİ===
**TOPLAM PUAN: XX/{total_weight}**

===KİŞİSEL VE BİLİŞSEL PROFİL===
YOK

===GÜÇLÜ YÖNLER===
YOK

===GELİŞİM ALANLARI===
YOK

===CV ÖZETİ===
YOK

===TAKİP MÜLAKATI SORULARI===
YOK
===BÖLÜM SONU===
---RAPORSON---

TAM FORMAT (puan %20'yi geçtiyse):
[MÜLAKATBİTTİ]
---RAPOR---
{report_body_l13}
---RAPORSON---"""

def parse_duration(text: str):
    m = re.search(r'\[SÜRE:(\d+)\]', text)
    duration = int(m.group(1)) if m else 60
    clean = re.sub(r'\[SÜRE:\d+\]', '', text).strip()
    return clean, duration


def normalize_recommendation(score: int, ai_recommendation: Optional[str] = None) -> str:
    """Tek iş kuralı: admin ekranı, PDF ve mail aynı öneriyi kullansın. 2026-09 rapor yeniden
    tasarımı: kanonik orta-bant etiketi 'Değerlendirmeye Al' → 'Değerlendir' (başlık hücresinde
    kelime ortasından bölünmesin, iş emri madde 4). `ai_recommendation` artık HİÇ kullanılmaz —
    modelin kendi öneri metni güvenilir kaynak değil (iş emri madde 21: karar tek yerde, yalnız
    sayıdan); parametre yalnızca geriye dönük çağrı uyumluluğu için tutuldu."""
    try:
        s = int(score or 0)
    except Exception as e:
        print(f"UYARI (normalize_recommendation: score sayıya çevrilemedi, score={score!r}): {type(e).__name__}: {e}")
        s = 0
    if s < 40:
        return "Reddet"
    if s < 80:
        return "Değerlendir"
    return "İşe Al"

# ============ 2026-09 RAPOR YENİDEN TASARIMI — TEK KARAR KAYNAĞI (iş emri madde 6+21) ============
# GENEL PUAN = mevcut (None olmayan) puanların eşit ağırlıklı ortalaması — 1. değerlendirici
# pozisyon/profil + (varsa) 2. değerlendirici pozisyon/profil, en fazla 4 değer. Karar YALNIZCA
# bu sayıdan üretilir; rapor metni (LLM prose'u) KARAR ÜRETMEZ, model bir öneri/karar kelimesi
# yazmaz. Eski "veto" mekanizması KALDIRILDI — ciddi bulgular artık Gelişim Alanları'nda "RİSK:"
# etiketiyle metinsel olarak yer alır, puanı/kararı OTOMATİK değiştirmez (iş emri madde 11).
# İŞ EMRİ — FINAL EVALUATION ARCHITECTURE / madde 5 — GENERAL SCORE VE ROUNDING. FATAL AUDIT
# kesin bulgusu: Python'un yerleşik round() 'round-half-to-even' (banker's rounding) kullanıyordu
# — 68.5→68, 79.5→80, 70.5→70 gibi aynı ".5" durumunun PARITY'YE göre farklı yöne yuvarlandığı,
# kullanıcı için ÖNGÖRÜLEMEZ bir davranış (ör. gerçek bir production raporunda Genel:68 böyle
# çıkmıştı). TEK canonical, deterministik yuvarlama kuralı: ROUND_HALF_UP (Decimal ile, float
# tesadüflerine güvenmeden). 68.5→69, 70.5→71, 79.5→80. Skorlama ile ilgili HER yuvarlama
# (compute_genel_puan, _final_component_score) BU TEK fonksiyondan geçer — aynı hesap birden
# fazla yerde farklı şekilde yeniden implement EDİLMEZ.
def _round_half_up(value) -> int:
    """Skorlama için TEK canonical yuvarlama — ROUND_HALF_UP (ör. 68.5 -> 69), Decimal ile
    deterministik. float'ın kendi ikili temsilinden kaynaklanan tesadüfi sapmalara karşı `str()`
    üzerinden Decimal'e çevrilir (ör. Decimal(68.5) DEĞİL, Decimal('68.5'))."""
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

def compute_genel_puan(score_pos_1, score_profile_1=None, score_pos_2=None, score_profile_2=None):
    """Mevcut puanların eşit ağırlıklı ortalaması, ROUND_HALF_UP ile en yakın tam sayıya
    yuvarlanır (bkz. _round_half_up — madde 5). Hiçbiri yoksa None (uydurma puan YOK — çağıran
    veri yetersizliğini ayrıca ele almalı)."""
    vals = [v for v in (score_pos_1, score_profile_1, score_pos_2, score_profile_2) if v is not None]
    if not vals:
        return None
    return _round_half_up(sum(vals) / len(vals))

# İŞ 6V-FIX — FINAL POSITION/PROFILE SCORE CONSISTENCY (İŞ EMRİ — FINAL EVALUATION ARCHITECTURE
# ile GENİŞLETİLDİ): bu helper artık yalnız GÖSTERİM için değil — CANONICAL final_score_position/
# final_score_profile DB alanlarını hesaplamak için de kullanılır (bkz. _persist_final_scores).
# AYNI ('mevcutları filtrele + _round_half_up(ortalama)') standardı TEK bileşen (yalnız pozisyon
# YA DA yalnız profil) için tekrar kullanır — General Score formülü DEĞİŞMEDİ, yalnız yuvarlama
# kuralı artık ROUND_HALF_UP (madde 5).
def _final_component_score(primary_value, reviewer_value):
    """Reviewer değeri VARSA primary+reviewer ortalaması (compute_genel_puan ile AYNI
    _round_half_up standardı); reviewer değeri YOKSA/None ise yalnız primary. İkisi de None ise
    None."""
    vals = [v for v in (primary_value, reviewer_value) if v is not None]
    if not vals:
        return None
    return _round_half_up(sum(vals) / len(vals))

def decide_recommendation(genel_puan) -> Optional[str]:
    """TEK karar kaynağı: <40 Reddet · 40-79 Değerlendir · ≥80 İşe Al. genel_puan None ise
    karar da None (veri yetersiz — 'Değerlendirilemedi' durumu, ayrı ele alınır)."""
    if genel_puan is None:
        return None
    return normalize_recommendation(genel_puan)

# ============ 2026-09 RAPOR YENİDEN TASARIMI — YASAK İFADELER (iş emri madde 20) ============
# Rapor gövdesinde ASLA görünmemesi gereken kalıplar — eski (TUR1-4) sistem-notu üslubundan
# kalma parantez açıklamaları + eski terminoloji (PUAN 1/2, AI-1/2, birinci/ikinci model, veto
# vb.). Kaynak metinlerdeki bu ifadeler KAYNAKTA da ayrıca temizlendi (aşağıdaki liste SON bir
# güvenlik ağıdır — modelin veya eski kod yollarının kaçırdığı bir şey varsa burada yakalanır).
# NOT (KAYIP ANLATI BÖLÜMLERİ turu, 2026-09 sonraki tur) — "veto yok" listeden ÇIKARILDI: o zaman
# bu, modelin eski mimaride kendiliğinden yazabildiği bir DOLGU ifadeydi (yasaklanması doğruydu).
# GÖREV 1.7 ile Profil Veto Kontrolü artık DETERMİNİSTİK olarak "Veto yok." metnini KENDİSİ
# üretiyor — bu artık meşru bir sistem çıktısı, dolgu değil; listede kalsaydı scrub_forbidden_
# phrases KENDİ ürettiğimiz metni sessizce siliyordu (sentetik testte yakalandı — "Profil Veto
# Kontrolü:." biçiminde, metni eksik basıyordu).
FORBIDDEN_PHRASES = [
    "(sistem — deterministik)", "(sistem - deterministik)",
    "(eşik tablosundan)", "(esik tablosundan)",
    "(yalnız referans)", "(yalniz referans)",
    "(niteliksel gözlem)", "(niteliksel gozlem)",
    "puana etki etmez", "puanı etkilemez", "puani etkilemez",
    "hesaba girmez", "hesaba katılmaz",
    "kararı değiştirmez", "karari degistirmez",
    "normalize yöntemiyle", "normalize yontemiyle",
    "ham puan", "payda dışı", "payda disi",
    "belirgin çelişki yok", "belirgin celiski yok",
    "0 kriter eksik",
    "puan 1", "puan 2",
    "birinci model", "ikinci model", "ai-1", "ai-2",
]
_FORBIDDEN_PHRASES_RE = re.compile("|".join(re.escape(p) for p in FORBIDDEN_PHRASES), re.IGNORECASE)

def scrub_forbidden_phrases(text: str) -> str:
    """İş emri madde 20 — yukarıdaki kalıplardan biri geçen SATIRDAKİ o ifadeyi (parantezli
    kalıpları tüm parantezle birlikte) siler; satırı tamamen atmaz, çevresindeki gerçek içerik
    korunur. Boşluk/nokta artıklarını temizler."""
    if not text or not _FORBIDDEN_PHRASES_RE.search(text):
        return text or ""
    out_lines = []
    for ln in text.splitlines():
        if _FORBIDDEN_PHRASES_RE.search(ln):
            # Parantez içinde geçiyorsa TÜM parantezi at; değilse yalnız kalıbı at.
            ln = re.sub(r"\([^)]*(?:" + "|".join(re.escape(p) for p in FORBIDDEN_PHRASES) + r")[^)]*\)",
                        "", ln, flags=re.IGNORECASE)
            ln = _FORBIDDEN_PHRASES_RE.sub("", ln)
            ln = re.sub(r"[ \t]{2,}", " ", ln)
            ln = re.sub(r"\s+([.,;:])", r"\1", ln)
            ln = re.sub(r"\*\*\s*:\s*\*\*", "", ln)
        out_lines.append(ln.rstrip())
    return "\n".join(out_lines)

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
# TUR 2 / GÖREV A — PUAN 2 (profil) bölümünün GERÇEK başlangıcını bulur. Eskiden `#{0,4}` (yani
# HİÇ diyez de olabilir) kullanıldığı için "PUAN 2" ifadesi rapor METNİNDE geçtiği her yerde
# (ör. bir başlık içinde "(... → PUAN 2)" ya da bir açıklama cümlesinde "... PUAN 2 profilini
# besler") yanlışlıkla eşleşiyor, splice_profile_region bu noktadan itibaren TÜM prose bölümlerini
# siliyordu. Artık YALNIZCA gerçek bir başlık kabul edilir:
#   - `### PUAN 2 ...` (2-4 diyezli markdown başlığı, satır başında)
#   - `**PUAN 2 — ...**` (kalın başlık, satır başında, hemen ardından tire/çizgi)
#   - `**PROFİL PUANI: ...**` / `PROFİL PUANI:` satırı (fallback — model başlığı atladıysa)
_PROFILE_REGION_RE = re.compile(
    r"(?m)^[ \t]*(?:-{2,}[ \t]*\n[ \t]*)?"
    r"(?:#{2,4}[ \t]*PUAN[ \t]*2\b"
    r"|\*{1,3}[ \t]*PUAN[ \t]*2[ \t]*[—–-]"
    r"|\*{0,3}[ \t]*PROF\wL?[ \t]+PUANI\b)",
    re.IGNORECASE)

def _profile_region_start(text: str):
    """PUAN 2 bölümünün başladığı indeks | None. Bir 'PUAN 2' metin-içi geçişi asla eşleşmez."""
    if not text:
        return None
    m = _PROFILE_REGION_RE.search(text)
    return m.start() if m else None

def split_report_regions(report_body: str):
    if not report_body:
        return report_body or "", ""
    idx = _profile_region_start(report_body)
    if idx is None:
        return report_body, ""
    return report_body[:idx], report_body[idx:]

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

_TR_L = "a-zçğıöşü"
_TR_U = "A-ZÇĞİÖŞÜ"

def repair_report_spacing(text: str, glue_terms=None) -> str:
    """GÖREV 9.2 — string birleştirme kaynaklı boşluk kayıplarını deterministik onarır:
      - noktalama sonrası boşluk: 'nedenle,adayın' → 'nedenle, adayın'
        (ondalık sayı '3.5', kısaltma 'vb.', URL, saat '08:47' KORUNUR)
      - küçük→BÜYÜK harf geçişinde birleşmiş cümle/başlık: 'sorgulatmaktadır.Öne' zaten
        yukarıda ayrılır; noktasız 'düzeydeGüçlü' gibi geçişlere de boşluk ekler
      - bilinen terimlerin (kriter adları) boşluksuz hâli metinde geçiyorsa yeniden boşluklar
    Sadece rapor METNİ / CV özeti için — transkripte/koda dokunmaz."""
    if not text:
        return text
    # noktalama + hemen ardından harf/rakam → araya boşluk (ondalık sayı ve saat ':' hariç)
    text = re.sub(rf"(?<=[{_TR_L}{_TR_U}])([,;!?])(?=[{_TR_L}{_TR_U}0-9])", r"\1 ", text)
    text = re.sub(rf"(?<=[{_TR_L}])\.(?=[{_TR_U}])", ". ", text)                  # cümle sonu + Büyük harf: 'değildi.Öne'
    text = re.sub(rf"(?<=[{_TR_L}]{{2}})\.(?=[{_TR_L}]{{3,}})", ". ", text)       # 'değildi.somut' (kısaltma vb. korunur: 'vb.x' 2 harf öncesi yetmez)
    for term in (glue_terms or []):
        if not term or " " not in term:
            continue
        glued = term.replace(" ", "")
        if len(glued) >= 8 and glued.lower() in text.lower():
            text = re.sub(re.escape(glued), term, text, flags=re.IGNORECASE)
    # çift boşlukları tekle (satır başı girintisi korunur)
    text = re.sub(r"(?<=\S)  +(?=\S)", " ", text)
    return text

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
               i.partial, i.completion_pct, i.technical_error_ref,
               i.reviewer_score_position, i.reviewer_score_profile
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
    # İŞ EMRİ — MÜLAKAT LEVEL VE DERİNLİK BİLGİSİNİ GÖSTER: i.level/i.depth_tier AYRICA (ALIAS'lı)
    # seçilir — bunlar interviews satırının KENDİ kayıtlı değeridir; c.level/c.depth_tier adminin
    # BU adayın SONRAKİ mülakatı için sonradan değiştirebileceği (editForm) alanlardır, mülakat
    # BAŞLADIKTAN SONRA artık o geçmiş mülakatı doğru temsil etmeyebilir. Frontend interview_level/
    # interview_depth_tier VARSA onu, YOKSA (mülakat henüz hiç başlamadıysa i satırı yok) c.level/
    # c.depth_tier'ı gösterir.
    attempts = db.execute("""
        SELECT c.id as candidate_id, c.name, c.email, c.phone, c.position, c.level, c.depth_tier,
               c.interview_language, c.report_language, c.education, c.university, c.department,
               c.experience_years, c.ai_note, c.status, c.invite_type,
               c.is_archived, c.created_at, c.completed_at, c.terminated_reason,
               c.login_count, c.first_login_at, c.last_login_at,
               c.interview_start_count, c.last_start_at, c.invite_expires_at,
               i.score, i.score_position, i.score_profile, i.recommendation, i.completed_at as interview_completed_at,
               i.processing_status, i.processing_error, i.started_at,
               i.partial, i.completion_pct, i.technical_error_ref,
               i.reviewer_score_position, i.reviewer_score_profile,
               i.level as interview_level, i.depth_tier as interview_depth_tier
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
        candidate = db.execute("SELECT id, level, person_id FROM candidates WHERE id=?", (candidate_id,)).fetchone()
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

        # İŞ EMRİ — KİŞİ GEÇMİŞİ / DÜZENLE SENKRONİZASYONU: candidate'ın Ad Soyad/E-posta/Telefonu
        # değiştiğinde, bağlı persons ana kaydı (varsa) AYNI candidate_id/person_id İLİŞKİSİ
        # üzerinden senkronize edilir — isim/e-posta metniyle eşleştirme YAPILMAZ, yeni person
        # OLUŞTURULMAZ. Kök neden: bu uç nokta yalnızca candidates satırını güncelliyordu;
        # PersonDetail.js'in üst başlığı/kişi kartı ise persons.full_name/email/phone'dan (ayrı bir
        # kopya) okunuyor — ikisi arasında hiçbir senkronizasyon yoktu.
        person_id = candidate["person_id"] if "person_id" in candidate.keys() else None
        if person_id:
            person_fields = {}
            if "name" in fields:
                person_fields["full_name"] = fields["name"]
            if "email" in fields:
                person_fields["email"] = fields["email"]
            if "phone" in fields:
                person_fields["phone"] = fields["phone"]
            if person_fields:
                person_set_clause = ", ".join(f"{k}=?" for k in person_fields.keys())
                db.execute(f"UPDATE persons SET {person_set_clause} WHERE id=?",
                          list(person_fields.values()) + [person_id])

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

    if level in (2, 3):
        db.close()
        log_ai_provider(level, "claude", "blocked")
        raise HTTPException(status_code=400, detail="Level 2/3 mülakatlar sesli (OpenAI Realtime) akışını kullanır. Lütfen /api/realtime/session üzerinden bağlanın.")
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

    # İŞ EMRİ — L1 OPENAI-ONLY MİMARİSİ: L1 metin mülakatının CANLI sohbeti de OpenAI'ye taşındı
    # (önceki fazda yalnız rapor üretimi taşınmıştı). L1 normal akışta artık Anthropic çağrısı YOK.
    if not OPENAI_API_KEY:
        print("HATA: OPENAI_API_KEY ortam değişkeni boş veya tanımsız.")
        raise HTTPException(status_code=500, detail="Sistem yapılandırma hatası (API anahtarı eksik). Lütfen yöneticinize bildirin.")

    try:
        system = get_system_prompt(payload["position"], payload["name"], candidate["cv_text"] if candidate else None, candidate["ai_note"] if candidate else None, candidate["education"] if candidate else None, candidate["university"] if candidate else None, candidate["department"] if candidate else None, candidate["experience_years"] if candidate else None, level, (candidate["interview_language"] if candidate and "interview_language" in candidate.keys() else "tr") or "tr", (candidate["report_language"] if candidate and "report_language" in candidate.keys() else "tr") or "tr", (candidate["depth_tier"] if candidate and "depth_tier" in candidate.keys() else "standart") or "standart", email=(candidate["email"] if candidate and "email" in candidate.keys() else None))
        resp = openai_call("POST", "https://api.openai.com/v1/chat/completions",
                           json_body={"model": OPENAI_L1_INTERVIEW_MODEL,
                                      "messages": [{"role": "system", "content": system},
                                                   {"role": "user", "content": "Başla. Kısa selam ve ilk soru."}],
                                      "max_tokens": 220},
                           timeout=60.0, step="interview_start", severity="user", retry=True,
                           context={"candidate_id": candidate_id, "candidate_name": payload.get("name"), "level": level})
        result = resp.json()
        record_openai_chat_usage(candidate_id, level, OPENAI_L1_INTERVIEW_MODEL, "interview_start", result)
        raw = result["choices"][0]["message"]["content"]
        clean, duration = parse_duration(raw)
        db = get_db()
        save_interview_state(db, candidate_id, [{"role": "assistant", "content": clean, "ts": _now_ts()}], level)
        db.commit(); db.close()
        return {"message": clean, "question_duration": duration, "total_duration_seconds": total_seconds, "intro_text": get_intro_text(payload["position"], level, candidate["interview_language"] or "tr")}
    except AIError as e:
        raise ai_http_exception(e)
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

    if level in (2, 3):
        log_ai_provider(level, "claude", "blocked")
        raise HTTPException(status_code=400, detail="Level 2/3 mülakatlar sesli (OpenAI Realtime) akışını kullanır, bu endpoint kullanılamaz.")

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

    # İŞ EMRİ — L1 OPENAI-ONLY MİMARİSİ: L1 metin mülakatının CANLI sohbeti de OpenAI'ye taşındı.
    if not OPENAI_API_KEY:
        print("HATA: OPENAI_API_KEY ortam değişkeni boş veya tanımsız.")
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
            # İŞ EMRİ — L1 OPENAI-ONLY MİMARİSİ: L1 birincil DEĞERLENDİRME/RAPOR ve canlı sohbetin
            # tamamı artık OpenAI (RAPOR üretimi önceki fazda taşınmıştı, CANLI sohbet bu fazda taşındı).
            _job_id = _mark_finish_pending(effective_candidate_id, level, provider="openai", model=OPENAI_REPORT_MODEL,
                                           system=system, payload=user_payload, terminated_reason=None, reason="normal")
            # madde I — claim başarısızsa (nadiren: çift-submit vb.) zaten başka bir job aktif;
            # ikinci run_deferred_finish_job TETİKLENMEZ, ama yanıt yine de doğru (bir iş SÜRÜYOR).
            if _job_id:
                background_tasks.add_task(run_deferred_finish_job, effective_candidate_id, level)
            return {
                "message": "Mülakatınız tamamlandı, teşekkür ederiz. Raporunuz hazırlanıyor.",
                "completed": True, "processing": True, "score": None, "recommendation": None,
            }

        resp = openai_call("POST", "https://api.openai.com/v1/chat/completions",
                           json_body={"model": OPENAI_L1_INTERVIEW_MODEL,
                                      "messages": [{"role": "system", "content": system},
                                                   {"role": "user", "content": user_payload}],
                                      "max_tokens": 260},
                           timeout=60.0, step="interview_chat", severity="user", retry=True,
                           context={"candidate_id": effective_candidate_id, "candidate_name": payload.get("name"), "level": level})
        result = resp.json()
        record_openai_chat_usage(effective_candidate_id, level, OPENAI_L1_INTERVIEW_MODEL, "interview_chat", result)
        reply = result["choices"][0]["message"]["content"]

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
            # İŞ EMRİ — L1 OPENAI-ONLY MİMARİSİ: L1 birincil DEĞERLENDİRME/RAPOR OpenAI.
            _job_id = _mark_finish_pending(effective_candidate_id, level, provider="openai", model=OPENAI_REPORT_MODEL,
                                           system=system, payload=finish_payload,
                                           terminated_reason="Aday talebiyle erken sonlandırıldı", reason="aday_talebi")
            if _job_id:
                background_tasks.add_task(run_deferred_finish_job, effective_candidate_id, level)
            return {
                "message": "Anlıyorum, mülakatı burada sonlandıralım. Raporunuz hazırlanıyor.",
                "completed": True, "processing": True, "score": None, "recommendation": None,
            }

        clean, duration = parse_duration(reply)
        return {"message": clean, "completed": False, "question_duration": duration}
    except AIError as e:
        raise ai_http_exception(e)
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
        # GÖREV 7.4 — sözlü beyanda yaygın alan adı + "bölüm/program okuyorum/mezunuyum" kalıbı
        # (küçük harfli alan adları: 'açık öğretimden işletme bölüm okumaktayım')
        if out["department"] is None:
            dm = re.search(r"\b(işletme|iktisat|muhasebe|maliye|ekonomi|hukuk|psikoloji|sosyoloji|"
                           r"mühendislik|bilgisayar|endüstri|kimya|makine|elektrik|inşaat|istatistik|"
                           r"uluslararası ilişkiler|kamu yönetimi|iş idaresi|bankacılık|finans)\s+"
                           r"(?:bölüm|program|okul)", txt, re.IGNORECASE)
            if dm:
                _d = dm.group(1).strip()
                # Türkçe-duyarlı ilk harf büyütme (i→İ)
                out["department"] = (_d[0].replace("i", "İ").upper() + _d[1:]) if _d else _d
                out["_notes"].append(f"Bölüm '{out['department']}' adayın sözlü beyanından alındı.")
        if out["university"] is None and re.search(r"açık ?öğretim|açık ?üniversite|aö[fl]\b", txt, re.IGNORECASE):
            out["university"] = "Açık Öğretim"
            out["_notes"].append("Üniversite 'Açık Öğretim' adayın sözlü beyanından alındı.")
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
    # Anchor: GERÇEK PUAN 2 başlığından hemen ÖNCE (tüm PUAN 1 prose'undan sonra). Metin-içi
    # "PUAN 2" geçişine değil — _profile_region_start strict regex kullanır.
    idx = _profile_region_start(report)
    if idx is not None:
        # başlıktan hemen önceki '---' ayıracını da bloğun ÜSTÜNE al ki çift '---' olmasın
        _pre = report[:idx].rstrip()
        if _pre.endswith("---"):
            _pre = _pre[:-3].rstrip()
        return _pre + "\n\n" + block + "\n\n---\n" + report[idx:].lstrip("-").lstrip()
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

# İş emri — RAPOR İÇERİK STANDARDI / A4 — KÖK NEDEN: bu iş emrinden önceki turda regenerate_report'un
# result_reason'a yazdığı iç-süreç dilindeki cümle ("...rapor yeniden üretiminde düzeltildi: önceki
# 'erken sonlandırma' tespiti hatalıydı...") nötrleştirilmişti — AMA yalnız KAYNAK (yeni yazımlar)
# düzeltilmişti. DB'de bu eski metinle KAYITLI SATIRLAR (ör. Murat) hiç geriye dönük düzeltilmedi
# (bu ortamdan prod DB'ye erişim yok) — 12.09 21:46 raporunda eski metin AYNEN basılı çıktı. Fix:
# müşteriye giden metin, KAYITTAKİ değer ne olursa olsun, BASIM ANINDA (aşağıda _make_report_pdf
# içinde) bu desenlerden biriyle eşleşiyorsa nötr cümleyle değiştirilir — kayıt DEĞİŞMEZ (geriye
# dönük veri migrasyonu bu iş emrinin kapsamında değil, yalnız BASIM normalize edilir).
_INTERNAL_PROCESS_LANGUAGE_RE = re.compile(
    r"rapor yeniden [üu]retim\w*|[öo]nceki[^.]{0,40}tespit\w*\s+hatal[ıi]\w*|tespiti hatal[ıi]\w*|"
    r"d[üu]zeltildi\s*[:\(]",
    re.IGNORECASE)

def sanitize_result_reason_for_customer(text: str) -> str:
    """A4 — result_reason'da iç-süreç/debug dili tespit edilirse (ör. 'rapor yeniden üretiminde
    düzeltildi', '...tespiti hatalıydı') TÜM metin nötr, sonuç-odaklı bir cümleyle değiştirilir.
    Kayıttaki değeri DEĞİŞTİRMEZ — yalnız müşteriye giden PDF'e basılırken uygulanır."""
    if not text:
        return text
    if _INTERNAL_PROCESS_LANGUAGE_RE.search(text):
        return "Mülakat normal şekilde tamamlanmıştır."
    return text

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
                       "sonuç gerekçesi", "ai notuna uyum",
                       # TUR 4 / GÖREV 6.1 — CV yüklenmemiş adayda bu bölümler "CV yok" tekrarından
                       # ibaret kalıyor; GENEL KURAL, yalnız Kader'e özel değil.
                       "cv tutarlılığı", "cv ↔ mülakat ↔ pozisyon uyumu")
# GÖREV 6 — bağlamsız/klişe şablon cümleleri: bu kalıplardan biriyle DOLU bir opsiyonel bölüm
# "içeriksiz" sayılır ve rapordan tamamen çıkarılır (başlık + gövde).
_EMPTY_CONTENT_RE = re.compile(
    r"belirtilecek\s+bir\s+.{0,20}?\s*yok|belirtilecek\s+bir\s+şey\s+yok|"
    r"(?:kayda\s+değer|not\s+edilecek|söylenecek|eklenecek)\s+bir\s+.{0,20}?\s*yok|"
    r"gözlem\s+yok|herhangi\s+bir\s+.{0,30}?\s*(?:yok|bulunmamaktadır|gözlenmemiştir)\.?\s*$|"
    r"mülakat\s+normal\s+tamamland|normal\s+(?:bir\s+)?(?:şekilde\s+)?tamamland|olumsuz\s+bir\s+(?:gözlem|durum|bulgu)\s+(?:yok|bulunma)|"
    r"belirtilen\s+tüm\s+kriterler\s+değerlendirild|tüm\s+kriterler\s+değerlendirild|değerlendirilemeyen\s+(?:bir\s+)?alan\s+(?:yok|bulunma)|"
    r"tüm\s+kriterler\s+(?:eksiksiz\s+)?(?:puanland|değerlendirild)|"
    # TUR 4 / GÖREV 6.1 — CV yüklenmemiş aday: "CV yok/yüklenmedi" boilerplate'i GENEL kural
    # olarak içeriksiz sayılır (gerçek bir CV karşılaştırması VARSA bu kalıba düşmez).
    r"\bcv\s+(?:yok|yüklenmedi|paylaş[ıi]lmad[ıi]|bulunmuyor|mevcut\s+değil)\b|"
    # TUR 3 / GÖREV 6.2 — "AI Notuna Uyum" klişesi HER İKİ KUTUPTA: 'detaylı irdelendi + (yeterli/yetersiz)'
    # gibi mekanizmasız genel cümle. (Dakika damgaları önce ayıklanır, bkz. strip_empty_report_sections.)
    r"\b(?:detayl[ıi]\s+(?:bir\s+)?(?:şekilde\s+)?)?(?:irdelen(?:miş|di)|ele\s+al[ıi]n(?:m[ıi][şs]|d[ıi])|değinilmiş|incelen(?:miş|di))\b"
    r".{0,90}\b(?:yeterli\s+(?:bilgiye\s+sahip|olduğunu)|yetersiz\s+kal|eksik\s+kal|başar[ıi]l[ıi]\s+(?:olduğunu|bir)|"
    r"gösterm(?:iştir|iş)|sahip\s+olduğunu\s+göster)",
    re.IGNORECASE)

# TUR 3 / GÖREV 6 — "AI Notuna Uyum" bölümü için EK kontrol: gerçek bir mekanizma (soru/cevap/
# alıntı) referansı YOKSA → içeriksiz. Sadece dakika damgası koymak bölümü kurtarmaz.
_AI_NOTE_MECHANISM_RE = re.compile(
    r"\bsor(?:uldu|du|ulmuş|ması|ya)\b|\bsoru(?:yla|nun|su|ları)\b|\bcevab|\byan[ıi]t(?:lad|ıyla)?\b|"
    r"\bdedi\b|\bbelirtti\b|\bifade\s+etti\b|\bsöyled|\baçıkça\s+(?:söyled|belirtti)|[\"“].{4,}[\"”]",
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
            _sec_norm = _norm_name(hm.group(1))
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
            # TUR 3 / GÖREV 6.2 — dakika damgalarını + kriter puanlarını ayıkla, KLİŞE kontrolü ondan sonra
            _bare = re.sub(r"\[\s*\d{1,3}\s*:\s*\d{2}\s*\]|\b\d{1,3}\s*/\s*\d{1,3}\b", " ", joined)
            _bare = re.sub(r"\s+", " ", _bare).strip()
            _is_empty = (not joined) or _EMPTY_CONTENT_RE.search(_bare)
            # "AI Notuna Uyum": gerçek bir mekanizma (soru/cevap/alıntı) referansı yoksa da içeriksiz
            if not _is_empty and _sec_norm == "ai notuna uyum" and not _AI_NOTE_MECHANISM_RE.search(joined):
                _is_empty = True
            if _is_empty:
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
_FINISH_JOB_STALE_SECONDS = 180  # recover_stale_processing_interviews ile AYNI eşik (bkz. aşağıda)


def _staleness_clause(column: str, seconds: int) -> str:
    """İŞ EMRİ — ÇOKLU TALENT MİMARİSİ / madde I+B: 'stale mi' karşılaştırması HER ZAMAN DB
    motorunun KENDİ saatine göre yapılır — Python'un datetime.now() (yerel saat dilimi) ile
    SQLite/Postgres'in CURRENT_TIMESTAMP'i (UTC) arasındaki fark, claim'in YANLIŞ zamanda
    stale sayılmasına yol açabiliyordu (regresyon testinde yakalandı — kök neden: bu fark)."""
    if USE_POSTGRES:
        return f"{column} < CURRENT_TIMESTAMP - INTERVAL '{int(seconds)} seconds'"
    return f"{column} < datetime('now', '-{int(seconds)} seconds')"


def _mark_finish_pending(candidate_id: int, level: int, provider: str, model: Optional[str], system: Optional[str],
                          payload: str, terminated_reason: Optional[str], reason: str) -> Optional[str]:
    """İŞ EMRİ — ÇOKLU TALENT MİMARİSİ / madde I: ATOMİK CLAIM. Aynı candidate+level için HÂLİHAZIRDA
    aktif (stale OLMAYAN) bir finish/regenerate job'ı varsa bu çağrı BAŞARISIZ olur (None döner) —
    çağıran background_tasks.add_task'i TETİKLEMEMELİDİR (aksi halde iki run_deferred_finish_job
    aynı satırı eşzamanlı işler, klasik race). Tek atomik UPDATE...WHERE — DB seviyesinde, process-
    local kilide DAYANMAZ (çoklu worker/replica güvenli). Başarılıysa job_id (str) döner."""
    job_id = secrets.token_hex(12)
    db = get_db()
    try:
        stale_sql = _staleness_clause("processing_started_at", _FINISH_JOB_STALE_SECONDS)
        cur = db.execute(f"""
            UPDATE interviews SET processing_status='processing', processing_started_at=CURRENT_TIMESTAMP,
                   processing_error=NULL, pending_finish_reason=?, pending_finish_provider=?, pending_finish_model=?,
                   pending_finish_system=?, pending_finish_payload=?, pending_finish_terminated_reason=?,
                   processing_job_id=?, processing_operation=?, processing_attempt=COALESCE(processing_attempt,0)+1
            WHERE candidate_id=? AND level=?
              AND (processing_status IS NULL OR processing_status IN ('completed','failed')
                   OR processing_started_at IS NULL OR {stale_sql})
        """, (reason, provider, model, system, payload, terminated_reason, job_id, reason,
              candidate_id, level))
        claimed = (cur.rowcount or 0) > 0
        if not claimed:
            db.rollback()
            print(f"[FINISH_JOB_CLAIM_REJECTED] candidate_id={candidate_id} level={level} reason={reason} "
                  "— zaten aktif bir işlem var, ikinci tetikleme atlandı (madde I).")
            return None
        # KALEM 4 — mülakatın GERÇEK bitiş anı: aday tam ŞİMDİ bitirdi. Arka plan rapor işi dakikalar/
        # saatler sonra bitebilir; completed_at o zamanı DEĞİL bu anı yansıtmalı. Yeniden üretimde
        # (reason='admin_regenerate') dokunma — orijinal bitiş korunur.
        if reason != "admin_regenerate":
            db.execute("UPDATE interviews SET interview_ended_at=COALESCE(interview_ended_at, CURRENT_TIMESTAMP) "
                       "WHERE candidate_id=? AND level=?", (candidate_id, level))
        db.commit()
        return job_id
    finally:
        db.close()

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

def _dedup_filter_events(candidate_id: int, level: int, hall_filtered: list) -> None:
    """TUR 2 / GÖREV E — 'transcription_filtered_server' olaylarını YALNIZCA yeni ts'ler için ekler.
    Regen her çalıştığında aynı 2 halüsinasyon satırı için tekrar olay yazılıyordu; bu ev_count'u
    şişirip cevapsız tur sayısını yapay yükseltiyordu."""
    if not hall_filtered:
        return
    try:
        db = get_db()
        try:
            existing = db.execute(
                "SELECT event_data FROM realtime_events WHERE candidate_id=? AND level=? "
                "AND event_type IN ('transcription_filtered','transcription_filtered_server')",
                (candidate_id, level)).fetchall()
        finally:
            db.close()
    except Exception as e:
        print(f"UYARI (_dedup_filter_events fetch c={candidate_id}): {type(e).__name__}: {e}")
        existing = []
    seen_ts = set()
    for r in existing:
        try:
            d = json.loads(r["event_data"] or "{}")
            if d.get("ts"):
                seen_ts.add(str(d["ts"]))
        except Exception:
            pass
    fresh = [f for f in hall_filtered if str(f.get("ts") or "") not in seen_ts or not f.get("ts")]
    # ts'siz kayıt tek seferlik: aynı metin zaten varsa atla
    seen_txt = set()
    for r in existing:
        try:
            d = json.loads(r["event_data"] or "{}")
            if d.get("text"):
                seen_txt.add(str(d["text"])[:80])
        except Exception:
            pass
    fresh = [f for f in fresh if str(f.get("text") or "")[:80] not in seen_txt]
    if fresh:
        record_realtime_events(candidate_id, level,
                               [{"type": "transcription_filtered_server", "data": f, "elapsed_ms": 0} for f in fresh])

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
    # TUR 2 / GÖREV E — TOLERANS 9000 → 2500 ms. Eski geniş pencere, bir halüsinasyon/dolgu
    # anının ±9 sn'sinde biten GERÇEK aday cevaplarını da "cevapsız" sayıyordu (payda küçülüyor,
    # cevapsız sayısı yapay yükseliyordu). Bir halüsinasyon turunun speech_stop'u, bozduğu turun
    # bitişine ~2.5 sn içindedir.
    def _near_bad(ms, tol=2500):
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
    # TUR 2 / GÖREV E — her BAD WINDOW en fazla BİR turu düşürür (en yakın olanı). Blanket
    # exclude YOK; düşürülen tur sayısı len(bad_windows) ile sınırlı.
    _drop_idx = set()
    for bw in bad_windows:
        _cands = [(abs(b - bw), i) for i, (a, b) in enumerate(all_turns) if i not in _drop_idx and abs(b - bw) <= 2500]
        if _cands:
            _drop_idx.add(min(_cands)[1])
    turns = [t for i, t in enumerate(all_turns) if i not in _drop_idx]
    _dropped_turns = [all_turns[i] for i in sorted(_drop_idx)]
    talk_ms = sum(max(0, b - a) for a, b in turns)
    turn_count = len(turns)
    total_turn_count = len(all_turns)
    # GÖREV 9.1 — DENKLİK: toplam_tur = hesaba_katilan_tur + cevapsiz_tur_sayisi (AYNI kaynaktan).
    dropped_turn_count = len(_drop_idx)
    considered_denom = max(total_turn_count, turn_count + dropped_turn_count)
    # TUR 2 / GÖREV E.3 — hangi turlar düşürüldü, logla.
    if _dropped_turns:
        print(f"[VOICE_METRICS_DROPPED] c={candidate_id} L{level} {dropped_turn_count} tur düşürüldü "
              f"(bad_windows={len(bad_windows)}): "
              + "; ".join(f"{a//1000}-{b//1000}sn" for a, b in _dropped_turns))

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
        # GÖREV 9.1 — bu sayı VAD turlarından türetilir: toplam_tur = hesaba_katilan_tur + cevapsiz_tur_sayisi
        "cevapsiz_tur_sayisi": dropped_turn_count,
        "cevapsiz_tur_ek_isaret": unanswered_turns,  # transkript kaynaklı ayrı sinyal (denkleme girmez)
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

# ══════════════════════════════════════════════════════════════════════════════
# TUR 3 / GÖREV 4+5 — MODALİTE BULGULARI: İNSAN DİLİYLE RAPOR + AYRI TEKNİK EK
# ══════════════════════════════════════════════════════════════════════════════
def _read_modality_json(candidate_id: int, level: int):
    """interviews.*_json kolonlarından mimik/ses/gözlem verisini + kapsamı okur."""
    mimic, metrics, obs = {}, {}, []
    try:
        db = get_db()
        row = db.execute("SELECT mimic_analysis_json, voice_metrics_json, voice_observations_json "
                         "FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, level)).fetchone()
        db.close()
        if row:
            if row["mimic_analysis_json"]:
                mimic = json.loads(row["mimic_analysis_json"]) or {}
            if row["voice_metrics_json"]:
                metrics = json.loads(row["voice_metrics_json"]) or {}
            if row["voice_observations_json"]:
                obs = json.loads(row["voice_observations_json"]) or []
    except Exception as e:
        print(f"UYARI (_read_modality_json c={candidate_id}): {type(e).__name__}: {e}")
    return mimic, metrics, obs

def _tr_lower_first(s: str) -> str:
    """Bir değeri cümle İÇİNE gömerken ilk harfini küçültür — Türkçe-doğru (İ→i, I→ı; bkz.
    _tr_upper'daki aynı kök neden notu, str[0].lower() burada da YANLIŞ sonuç verir)."""
    if not s:
        return s
    c = s[0]
    if c == "İ":
        c2 = "i"
    elif c == "I":
        c2 = "ı"
    else:
        c2 = c.lower()
    return c2 + s[1:]

def _relative_time_bin(t_sn, total_min) -> Optional[str]:
    """İş emri GÖREV 7 — ham [mm:ss] damgası yerine GÖRECELİ konum ('başında'/'ortasında'/
    'sonlarında') döner. total_min bilinmiyorsa None (çağıran o zaman zaman ifadesi eklemez,
    UYDURMAZ)."""
    if not isinstance(t_sn, (int, float)) or not total_min:
        return None
    frac = (t_sn / 60.0) / total_min
    if frac < 0.33:
        return "başında"
    if frac < 0.66:
        return "ortasında"
    return "sonlarında"

def build_modality_prose(candidate_id: int, level: int) -> str:
    """Mimik + ses + mülakatçı gözlemlerinden İNSAN DİLİYLE, JSON ALAN ADI/ETİKETİ SIZDIRMAYAN,
    GÖRÜNTÜ ve SES ayrı paragraflarda bir 'Görüntü ve Ses Gözlemi' bölümü üretir. Ham SAYILAR bu
    bölüme GİRMEZ (yalnız build_technical_annex()'te, EK 3) — iş emri madde 3: aynı bilgi iki
    yerde sayı olarak tekrarlanmasın, burada niteliksel ifadeye çevrilir. Puanı ETKİLEMEZ.
    GENEL KURAL: seviye kamera/ses YAKALAMIYORSA (Level 1) ilgili paragraf hiç üretilmez; hiç
    veri yoksa TÜM bölüm boş döner (üst katman boş başlık basmaz)."""
    mimic, metrics, obs = _read_modality_json(candidate_id, level)
    cov = compute_modality_coverage(candidate_id, level) if _level_has_camera(level) else {}
    total_min = cov.get("toplam_dk")

    # ── Görüntü paragrafı (yalnız kamera yakalayan seviyelerde) ──
    goruntu = []
    if _level_has_camera(level):
        dg = cov.get("dogrulama") or {}
        if dg.get("n"):
            if dg.get("kumelenme"):
                goruntu.append("Aday kamera görüntüsü oturumun sınırlı bir bölümünden doğrulandı; "
                               "kareler dar bir aralıkta toplandığı için oturumun geri kalanı görüntüyle gözlemlenemedi.")
            else:
                goruntu.append("Aday kamera görüntüsü oturum boyunca düzenli aralıklarla doğrulandı.")
        if isinstance(mimic, dict) and mimic and mimic.get("durum") != "yetersiz_kare":
            # genel_durus + goz_temasi_egilimi TEK cümlede birleşir (alan adları hiç yazılmaz,
            # değerler cümle içine gömülürken ilk harfleri küçültülür — iş emri madde 1.2).
            _durus = str(mimic.get("genel_durus") or "").strip().rstrip(".")
            _goz = str(mimic.get("goz_temasi_egilimi") or "").strip().rstrip(".")
            _parcalar = []
            if _durus:
                _parcalar.append(_tr_lower_first(_durus))
            if _goz and "kestiril" not in _goz.lower() and not _is_near_duplicate(_goz, _parcalar):
                _parcalar.append(_tr_lower_first(_goz))
            if _parcalar:
                goruntu.append("Görüntüde " + ", ".join(_parcalar) + " gözlendi.")

            # belirgin_anlar — İş emri GÖREV 7 (+ önceki tur madde 1.3+1.4): "Öne çıkan anlar:"
            # gibi bir ETİKET/liste başlığı YOK, [mm:ss] damgası da YOK (GÖREV 2/7.2 — bu bölümde
            # damga gerekli değil); aynı gözlem birden çok anda geçiyorsa TEK cümlede toplanır,
            # ne zaman geçtiği HAM DAMGA değil GÖRECELİ konum (oturumun başında/ortasında/
            # sonlarında) ile, tam cümle içine gömülü olarak anlatılır.
            _anlar = [a for a in (mimic.get("belirgin_anlar") or []) if isinstance(a, dict) and a.get("gozlem")]
            _groups = []  # [{"text","secs":[...],"yorum"}]
            for a in _anlar[:8]:
                gtxt = str(a["gozlem"]).strip().rstrip(".")
                if not gtxt:
                    continue
                _t = a.get("t_sn") if isinstance(a.get("t_sn"), (int, float)) else None
                yr = str(a.get("yorum") or "").strip().rstrip(".")
                if yr.lower() in ("nötr", "notr"):
                    yr = ""
                grp = next((g for g in _groups if _is_near_duplicate(gtxt, [g["text"]], threshold=0.55)), None)
                if grp:
                    if _t is not None:
                        grp["secs"].append(_t)
                else:
                    _groups.append({"text": gtxt, "secs": [_t] if _t is not None else [], "yorum": yr})
            if _groups:
                clauses = []
                for g in _groups[:3]:
                    _bins = []
                    for _s in g["secs"]:
                        _b = _relative_time_bin(_s, total_min)
                        if _b and _b not in _bins:
                            _bins.append(_b)
                    when_txt = f" (oturumun {' ve '.join(_bins)})" if _bins else ""
                    yr_txt = f" ({g['yorum']})" if g["yorum"] else ""
                    clauses.append(f"{_tr_lower_first(g['text'])}{yr_txt}{when_txt}")
                goruntu.append("Ayrıca " + "; ".join(clauses) + ".")

            _izlenim = str(mimic.get("genel_izlenim") or "").strip().rstrip(".")
            if _izlenim and not _is_near_duplicate(_izlenim, goruntu):
                goruntu.append(_izlenim + ".")
        # İş emri (2026-09, madde 12) — kamera verisi YOKSA paragraf hiç üretilmez, açıklayıcı
        # cümle bile yazılmaz (goruntu boş kalır → aşağıda hiç eklenmez).

    # ── Ses paragrafı (yalnız sesli akışı kullanan seviyelerde) ──
    ses = []
    if _level_has_voice(level):
        if metrics and _safe_int(metrics.get("tur_sayisi")) > 0:
            talk = metrics.get("aday_konusma_toplam_sn")
            _pct = round(100 * (talk / 60.0) / total_min) if (talk is not None and total_min) else None
            if _pct is not None:
                # İş emri madde 3 — yüzde/sayı yerine niteliksel bant (ham sayı EK 3'te kalır).
                _lab = ("sürenin yarısından fazlasında" if _pct >= 50
                       else "sürenin önemli bir bölümünde" if _pct >= 25
                       else "sürenin küçük bir bölümünde")
                cumle = f"Aday mülakat {_lab} konuştu"
            else:
                cumle = "Aday konuşma süresi ölçülebildi"
            avg_turn = metrics.get("ortalama_tur_uzunlugu_sn")
            if avg_turn is not None:
                _tl = "kısa ve öz" if avg_turn < 20 else ("orta uzunlukta" if avg_turn < 45 else "uzun ve ayrıntılı")
                cumle += f"; cevapları ortalama {_tl} oldu."
            else:
                cumle += "."
            ses.append(cumle)
            lat = metrics.get("yanit_gecikmesi_ort_sn")
            if lat is not None:
                if lat < 3:
                    ses.append("Sorulara hızlı yanıt verdi, belirgin bir düşünme duraklaması gözlenmedi.")
                elif lat <= 9:
                    ses.append("Sorulardan sonra cevap vermeden önce belirgin bir düşünme süresi aldı.")
                else:
                    ses.append("Bazı sorulardan sonra uzun duraklamalar oldu; bu teknik gecikmeden de kaynaklanmış olabilir.")
            sk = _safe_int(metrics.get("soz_kesme_sayisi"))
            if sk >= 4:
                ses.append("Mülakatçıyla üst üste konuşma birçok noktada oldu.")
            elif sk >= 1:
                ses.append("Mülakatçıyla birkaç noktada üst üste konuşma oldu.")
            _cev = _safe_int(metrics.get("cevapsiz_tur_sayisi"))
            if _cev >= 2:
                ses.append("Birkaç soruda geçerli bir aday cevabı alınamadı (sessizlik veya transkripsiyon gürültüsü).")
            if (metrics.get("guven") or "").lower() == "dusuk":
                ses.append("Ses metriklerinin güveni düşük olduğundan bu gözlemler temkinli okunmalı.")
        # İş emri (2026-09, madde 12) — ses verisi YOKSA paragraf hiç üretilmez.

        # Mülakatçının anlık ses gözlemleri — metrik cümleleriyle örtüşenler tekrar edilmez.
        for o in (obs or [])[:4]:
            gtxt = (o.get("gozlem") or "").strip()
            if not gtxt or _is_near_duplicate(gtxt, ses):
                continue
            _ems = _safe_int(o.get("elapsed_ms"))
            _w = f"[{_ems//60000}:{(_ems//1000)%60:02d}] " if _ems else ""
            ses.append(f"{_w}{gtxt.rstrip('.')}.")

    if not goruntu and not ses:
        return ""
    bolumler = ["**Görüntü ve Ses Gözlemi:**"]
    if goruntu:
        bolumler.append("Görüntü: " + " ".join(goruntu))
    if ses:
        bolumler.append("Ses: " + " ".join(ses))
    return "\n\n".join(bolumler)

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
        parts.append("SES METRİKLERİ (tur bazlı):\n" + json.dumps(metrics, ensure_ascii=False, indent=1))
    else:
        parts.append("SES METRİKLERİ: TOPLANAMADI — realtime_events'te konuşma başlangıç/bitiş olayı yok.")
    if obs:
        parts.append("MÜLAKATÇI SES GÖZLEMLERİ (mülakat anında kaydedildi):\n" + json.dumps(obs, ensure_ascii=False, indent=1))
    _cov = compute_modality_coverage(candidate_id, level)
    parts.append("KAMERA KARESİ KAPSAMI:\n" + json.dumps(_cov, ensure_ascii=False))
    if not parts:
        return ""
    # TUR 3 / GÖREV 5 — bu blok yalnızca BAĞLAM'dır. Model bu SAYILARI rapora YAZMAYACAK; sistem
    # zaten insan diliyle 'Görüntü ve Ses Gözlemi' bölümünü + ayrı 'Teknik Ek'i deterministik ekler.
    return ("=== MODALİTE BAĞLAMI (yalnızca senin bilgin için — RAPORA SAYI/METRİK YAZMA) ===\n"
            "Aşağıdaki görüntü/ses sinyalleri YALNIZCA destekleyici bağlamdır. Toplam puanı ve kararı "
            "DEĞİŞTİRMEZLER. Bu bloktaki SAYILARI, metrik adlarını veya JSON'u rapora KOPYALAMA — "
            "sistem modalite gözlemini insan diliyle ayrıca ekliyor. Yalnızca transkriptteki bir "
            "gözlemi doğruluyor/çürütüyorsa dikkate al. Duygu/kişilik/yalan analizi DEĞİLDİR.\n\n"
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
    """Müfettişe (Claude, İŞ EMRİ — FINAL EVALUATION ARCHITECTURE'dan beri) primary ile AYNI kriter
    setini ve AYNI maksimum puanları, KİMLİK (ID) ile verir.
    İş emri GÖREV 6.1 — kriter eşleştirmesi artık GÖRÜNEN ADA göre YAPILMAZ (benzer isimli
    pozisyon/profil kriterleri — ör. 'Analitik Yaklaşım' ↔ 'Analitik yapı ve muhakeme' —
    birbirine karışıyordu). Müfettiş KRITER_PUAN/KRITER_GEREKCE satırlarında kriter ADINI değil
    bu P#/K# kimliğini yazar; kimlik→ad→tavan eşleşmesi sistemde SABİT ve tekildir.
    İŞ 6T — kriter TANIMI (varsa) da eklendi: müfettiş artık yalnız ADI değil, primary/retry'ın
    zaten gördüğü TANIMI da görüyor (semantik ilgi kontrolü için önkoşul, bkz. İş 6Q teşhisi).
    Tanım yoksa/boşsa satır ESKİ haliyle (yalnız ad+tavan) kalır — CRASH/format bozulması YOK."""
    lines = ["POZİSYON kriterleri ve tavanları — KRITER_PUAN/KRITER_GEREKCE satırlarında kriter ADI DEĞİL, buradaki KİMLİĞİ (P1, P2, ...) yaz:"]
    for i, c in enumerate((position_criteria or []), start=1):
        if c.get("name"):
            _desc = (c.get("desc") or "").strip()
            _desc_suffix = f" — tanım: {_desc}" if _desc else ""
            lines.append(f"- P{i}: {c['name']} — __/{_safe_int(c.get('weight'))}{_desc_suffix}")
    lines.append("KİŞİSEL VE BİLİŞSEL PROFİL kriterleri ve tavanları — aynı şekilde KİMLİĞİ (K1, K2, ...) yaz:")
    for i, pc in enumerate(PROFILE_CRITERIA, start=1):
        _pdesc = (pc.get("desc") or "").strip()
        _pdesc_suffix = f" — tanım: {_pdesc}" if _pdesc else ""
        lines.append(f"- K{i}: {pc['name']} — __/{pc['weight']}{_pdesc_suffix}")
    return "\n".join(lines)

# İŞ EMRİ — L3 İKİNCİ DEĞERLENDİRME TUTARLILIĞI + SOURCE VISIBILITY / madde 4 — KÖK NEDEN: birincil
# rapor üretici (build_l2_report_prompt, "KAYIT FORMU BEYANI" bloğu) eğitim/üniversite/bölüm/deneyim
# yılı gibi adayın/adminin BAŞVURU FORMUNA girdiği alanları görüyordu; Claude second evaluator ve
# Final Report Quality Gate bu alanları HİÇ görmüyordu — yalnız (zaten üretilmiş) rapor METNİNDE bu
# bilgiyi buluyor, CV/transkriptte karşılığını arayıp bulamayınca haklı biçimde "kaynağı belirsiz"
# sanıyorlardı. TEK yerden üretilen bu blok, İKİSİNE de AYNI kaynak anlamıyla (CV DEĞİL, transkript
# DEĞİL, kayıt/başvuru formu) veriliyor — reviewer/QG bu veriyi puanlamaz, yalnız kaynak ayrımı için
# kullanır.
def _basvuru_formu_beyani_block(candidate) -> str:
    edu = candidate["education"] if candidate and "education" in candidate.keys() else None
    uni = candidate["university"] if candidate and "university" in candidate.keys() else None
    dept = candidate["department"] if candidate and "department" in candidate.keys() else None
    exp = candidate["experience_years"] if candidate and "experience_years" in candidate.keys() else None
    email = candidate["email"] if candidate and "email" in candidate.keys() else None
    if not any([edu, uni, dept, (exp not in (None, "", 0))]):
        return "(Başvuru formunda ek beyan yok.)"
    return (
        "Bu bilgiler CV METNİ DEĞİLDİR, TRANSKRİPT DEĞİLDİR — adayın/adminin başvuru/kayıt formuna "
        "girdiği, kendi başına GEÇERLİ, AYRI bir kaynaktır. Bu bilginin CV'de veya transkriptte AYRICA "
        "geçmemesi TEK BAŞINA 'kaynaksız/uydurma/halüsinasyon' sayılmaz — kaynağı zaten bu formdur; "
        "yalnız CV'de VEYA transkriptte GERÇEKTEN ÇELİŞEN bir ifade varsa bunu işaretle.\n"
        f"- E-posta: {email or '—'}\n"
        f"- Eğitim: {edu or '—'} · Üniversite: {uni or '—'} · Bölüm: {dept or '—'}\n"
        f"- Deneyim yılı (beyan): {exp if exp not in (None, '', 0) else '—'}"
    )

def run_report_reviewer(candidate_id: int, level: int, transcript_text: str, final_report: str, modality_block: str,
                        position_criteria: Optional[list] = None):
    """İŞ EMRİ — FINAL EVALUATION ARCHITECTURE: BAĞIMSIZ İKİNCİ DEĞERLENDİRİCİ. ARTIK YALNIZ L3'te
    çağrılır (bkz. append_reviewer_section'ın level != 3 erken-dönüşü) ve ARTIK Claude/Anthropic
    kullanır (ÖNCEDEN her zaman OpenAI'ydi — birincil L2/L3 zaten OpenAI olduğu için, ikinci
    değerlendiricinin GERÇEKTEN bağımsız/farklı bir sağlayıcı olması için Claude'a taşındı).
    NİHAİ raporu (basılacak hali: kriter tabloları + KARAR + gerekçe dahil) görür; taslağı değil.
    Raporu YENİDEN YAZMAZ ve karara/puana DOĞRUDAN ETKİ ETMEZ — yalnız KRITER_PUAN/KRITER_GEREKCE
    (grounding'i doğrulanırsa `apply_reviewer_criterion_correction` üzerinden KONTROLLÜ olarak final
    duruma girebilir, bkz. o fonksiyon) ve SEMANTIC_ISSUE (yalnız görüntüleme) üretir.
    Dönüş: (notes, status, error). Hata/atlama → notes='' ve rapor DENETÇİSİZ, DEĞİŞMEDEN kalır."""
    if not ANTHROPIC_API_KEY:
        return "", "skipped", "ANTHROPIC_API_KEY tanımlı değil"
    db_cv = get_db()
    try:
        _cand_cv = db_cv.execute(
            "SELECT cv_text, email, education, university, department, experience_years "
            "FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    finally:
        db_cv.close()
    cv_excerpt = ((_cand_cv["cv_text"] or "").strip()[:1800]) if _cand_cv and _cand_cv["cv_text"] else ""
    basvuru_formu_block = _basvuru_formu_beyani_block(_cand_cv)
    # TUR 3 / GÖREV 2+3 — SERBEST METİN. Sabit 6-başlık şablonu KALDIRILDI (model, boş şablonu
    # doldurmak için "Belirgin bir görüş ayrılığı yok." klişesini 5 kez tekrarlıyordu). Artık:
    # yalnızca gerçekten SÖYLEYECEK bir şeyi varsa yazar; yoksa "GÖRÜŞ YOK" der ve bölüm hiç basılmaz.
    prompt = f"""Sen bir işe alım raporunun BAĞIMSIZ İKİNCİ DEĞERLENDİRİCİSİSİN. Aşağıda bir mülakatın transkripti, sistemin ürettiği NİHAİ RAPOR, (varsa) modalite kanıtları ve (varsa) CV/kaynak metni var.

Görevin: birincil değerlendirmeyi transkript (ve varsa CV) karşısında BAĞIMSIZ OLARAK DENETLEMEK. Kararı veya puanı DOĞRUDAN DEĞİŞTİREMEZSİN — ama açık, kanıtlanabilir bir birincil hata bulursan bunu yalnız yorum olarak bırakmak YETERLİ DEĞİL: KRITER_PUAN/KRITER_GEREKCE ile somut, gerekçeli bir düzeltme öner (sistem bunu grounding'ini doğruladıktan SONRA kontrollü uygular).

Şu hata sınıflarını ÖZELLİKLE kontrol et:
1. Kanıt gerçekten ADAYA mı ait (mülakatçının sözü aday kanıtı gibi kullanılmış olabilir mi)?
2. Kanıttaki [mm:ss] zaman damgası doğru mu?
3. Alıntı/parafraz transkriptle GERÇEKTEN örtüşüyor mu (uydurma/başka ana ait olabilir mi)?
4. CV'deki bir bilgi, mülakatta SÖYLENMİŞ gibi (interview evidence) sunulmuş mu? (NOT: aşağıdaki BAŞVURU FORMU BEYANI ayrı ve KENDİ BAŞINA geçerli bir kaynaktır — bir bilginin yalnız CV'de/transkriptte değil BAŞVURU FORMUNDA geçmesi TEK BAŞINA kaynaksızlık/uydurma SAYILMAZ.)
5. Kanıt, atandığı kriteri GERÇEKTEN destekliyor mu (başka bir yetkinliğe mi ait)?
6. Kanıtın olumlu/olumsuz yönü doğru mu (bir sınırlılık/eksiklik olumluya çevrilmiş olabilir mi)?
7. Puan, kanıt/gerekçeyle TUTARLI mı?
8. "Değerlendirilemedi" kararı doğru mu (aslında yeterli veri VARDI mı, ya da tersine yetersiz veri olduğu halde puanlanmış mı)?
9. Anlatı (narrative) bölümleri, birincil/kendi bulgularınla ÇELİŞİYOR mu?
10. Ciddi bir kaynak/mantık hatası (rapor içi çelişki, kaynaksız önemli iddia) var mı?

SERBEST METİN yaz — sabit başlık, numaralı madde, şablon YOK. Yalnızca GERÇEKTEN kayda değer, somut gözlemlerini yaz:
- Raporda transkriptle desteklenmeyen / aşırı iddialı bir cümle görüyorsan: kısa alıntıyla belirt.
- Transkriptte olan ama raporun atladığı önemli bir sinyal varsa (aday kendi ağzıyla söylediği eksiklik dahil): yaz.
- Bir kriter puanı kanıta göre belirgin şekilde yüksek/düşükse: hangi kriter, neden.
- Sistem kararına (Genel Puan → Reddet/Değerlendir/İşe Al) katılmıyorsan: neden — bu yalnızca görüştür.

UZUNLUK: söyleyeceğin kadar. Bir cümle de olur, üç paragraf da. SAYFA DOLDURMA. Klişe cümle ("genel olarak yeterli", "belirgin bir sorun yok", "değerlendirme uygun") KURMA.

SÖYLEYECEK SOMUT BİR ŞEYİN YOKSA — birincil değerlendirmede ciddi bir sorun görmüyorsan — BU KISIM için SADECE şu iki kelime yaz: GÖRÜŞ YOK

Bunun DIŞINDA, yukarıdaki görüşün olup olmamasından TAMAMEN BAĞIMSIZ olarak, yanıtına AYRICA aşağıdaki İKİ bloğu MUTLAKA ekle:

=== ADAY ÖZGÜVENİ İZLENİMİ ===
Mülakatın TAMAMINA (akış baskısı olmadan, dışarıdan) bakarak adayın özgüvenine dair gözlemini yaz: kendinden emin mi/tereddütlü mü, kararlarını savunabiliyor mu/geri adım atıyor mu, belirsizlik veya zorlayıcı bir soru karşısındaki tutumu, bilmediğini açıkça kabul edebiliyor mu, görüşünü gerekçelendirerek mi savunuyor yoksa sadece tekrarlıyor mu, mülakatçı zorladığında pozisyonunu koruyor mu. Bu bir KONTROL LİSTESİ DEĞİLDİR — yalnız transkriptte GERÇEKTEN karşılığı olan yönleri yaz, karşılığı olmayan madde için cümle KURMA. KESİN KİŞİLİK HÜKMÜ YASAK (ör. "özgüveni düşük bir kişi" YAZMA) — somut davranış + [dk] damgası yaz, ör: "zorlayıcı sorularda pozisyonunu değiştirmeden savundu [12:36], ancak gerekçesini yeni bir örnekle desteklemek yerine aynı ifadeyi tekrarladı [13:47]". EN AZ BİR [dk] damgası ZORUNLU — damgasız genel yorum YAZMA. 1-2 paragraf, doldurma yok. SADECE transkript bu izlenimi kurmaya gerçekten yetmiyorsa (aday neredeyse hiç konuşmadı / mülakat çok kısa kesildi) bu bloğa SADECE "YETERSİZ VERİ" yaz — zorla üretme.

=== KRİTER PUANLARI ===
Bağımsız değerlendirmeni dikkatli ve kanıta dayalı yap. Her kriteri yalnızca o kriterle doğrudan ilişkili mülakat kanıtlarıyla değerlendir. İlgisiz kanıtları kriterler arasında taşıma ve kanıtın desteklemediği çıkarımlar yapma. Teknik bilgi veya mevcut yazılım kullanımını tek başına analitik düşünme, öğrenme/adaptasyon, inisiyatif veya başka bir davranışsal yetkinliğin kanıtı sayma. Aynı kanıtı birden fazla kriterde ancak her kriteri bağımsız ve doğrudan destekliyorsa kullan. Olumlu ve olumsuz kanıtları birlikte değerlendir. Puan, gerekçe ve kanıt birbiriyle tutarlı olsun.
AŞAĞIDAKİ (P1.. ve K1..) KRİTER LİSTESİNDEKİ HER KRİTER İÇİN, birincil değerlendirmeden BAĞIMSIZ olarak KENDİ puanını üret — birincilinkiyle AYNI sonuca varsan BİLE bu satırları atlama, yine de yaz:
KRITER_PUAN: <KİMLİK, ör. P1 veya K3 — AŞAĞIDAKİ LİSTEDEN, kriter ADINI YAZMA> = <senin puanın>/<maksimum>
KRITER_GEREKCE: <AYNI KİMLİK> = <2-3 cümle gerekçe, en az bir [dk] damgalı somut kanıt>
(Listedeki HER kimlik için bir KRITER_PUAN/KRITER_GEREKCE çifti OLMALI — rapordaki puanları KOPYALAMA, transkripte göre KENDİ bağımsız değerlendirmeni yap. KRITER_PUAN yazıp KRITER_GEREKCE YAZMAMAK KABUL EDİLMEZ.)
Bir kriterin puanına AÇIKÇA itiraz ediyorsan — "bu puan fazla yüksek/düşük", "bu kanıt bu kritere ait değil ve değerlendirmeyi etkiliyor" gibi puanı GERÇEKTEN etkileyen bir tespit yapıyorsan — bu itirazın o kriterin KRITER_PUAN/KRITER_GEREKCE'sine YANSIMASI ZORUNLU (yukarıdaki "her kriter için yaz" kuralı zaten bunu garanti eder). Kanıt yanlış kritere atanmış diyorsan: o kriter için GERÇEKTEN GEÇERLİ bir kanıt transkriptte var mı diye ayrıca bak — VARSA o kanıtla kendi puanını üret; net biçimde YOKSA aşağıdaki TEK KURAL'a göre karar ver (YENİ bir değerlendirilebilirlik kategorisi UYDURMA, bu dört durumun DIŞINA ÇIKMA):
{CRITERION_SCORING_RULE}
GUVEN_DUZEYI: <yüksek|orta|düşük> — <kendi değerlendirmene duyduğun güven düşükse KISA neden; yüksekse yalnızca 'yüksek' yaz> (bu satır ADAYIN değil SENİN kendi değerlendirmene duyduğun güvendir — rapora BASILMAZ, yalnız yönetici kaydı için)

=== SEMANTİK TUTARLILIK ===
HER kriter için (aşağıdaki listeden) İÇSEL olarak (yazmadan) şu üçünü kontrol et:
1) Kanıt GERÇEKTEN bu kriterin TANIMIYLA ilgili mi (aşağıdaki listede kriterin yanındaki tanıma bak), yoksa başka bir yetkinliği mi gösteriyor?
2) Rapordaki iddia (G) GERÇEKTEN verilen kanıttan (K) çıkıyor mu, yoksa kanıtın doğal anlamından DAHA GÜÇLÜ/FARKLI bir sonuç mu çıkarılmış?
3) Kanıtın olumlu/olumsuz yönü raporda KORUNMUŞ mu — adayın söylediği bir sınırlılık/eksiklik/belirsizlik ifadesi, rapor tarafından olduğundan OLUMLU/NÖTR gösterilmiş mi?
Bu senin işin DEĞİL: adayın söyleminin mesleki/regülasyonel açıdan doğru olup olmadığına dış bilgiyle hükmetmek. Yalnız raporun adayın GERÇEKTEN söylediğinden desteklenmeyen bir sonuç üretip üretmediğine bak.
ÇIKTI EKONOMİSİ (KESİN): PASS olan (belirgin bir sorun görmediğin) kriterleri TEK TEK YAZMA — hiçbir satır üretme. YALNIZ belirgin bir semantik sorun gördüğün kriterler için, aşağıdaki TEK SATIR formatında yaz:
SEMANTIC_ISSUE: <KİMLİK, ör. P1 veya K3> = <çok kısa (1 cümle) neden>
Hiçbir kriterde sorun görmüyorsan bu bölüme HİÇBİR SATIR yazma (boş bırak) — "GÖRÜŞ YOK" gibi bir cümle de YAZMA, sadece atla.
ÖNEMLİ: Burada bir SEMANTIC_ISSUE yazman, yukarıdaki "=== KRİTER PUANLARI ===" bölümündeki YAPISAL TUTARLILIK ZORUNLULUĞUNU karşılamış SAYILMAZ — tespit ettiğin sorun puanı GERÇEKTEN etkiliyorsa (yalnız üslup/vurgu değilse) o kriter için AYRICA KRITER_PUAN/KRITER_GEREKCE de yazmalısın.

{_reviewer_criteria_block(position_criteria)}

=== TRANSKRİPT ===
{(transcript_text or '')[:TRANSCRIPT_PROMPT_MAX_CHARS]}

=== NİHAİ RAPOR (basılacak hali) ===
{(final_report or '')[:16000]}

=== MODALİTE KANITLARI ===
{modality_block or 'Yok'}

=== CV/KAYNAK METNİ (varsa — 4. madde: CV bilgisi mülakat kanıtı gibi kullanılmış mı kontrolü için) ===
{cv_excerpt or "(CV metni yok veya çok kısa.)"}

=== BAŞVURU FORMU BEYANI ===
{basvuru_formu_block}"""
    try:
        # TEK DÜZELTME — TIMEOUT: 60.0 -> 120.0. Prompt artık TÜM kriterler için KRITER_PUAN/
        # KRITER_GEREKCE istediğinden üretim süresi uzadı (bkz. Kader EMECEN teşhisi — Claude'un
        # bu çağrısı 60s x SDK varsayılan retry ile ~186s'de APITimeoutError verdi, hiç yanıt
        # dönmedi). max_tokens/prompt/retry ayarları DEĞİŞMEDİ — yalnız istemci zaman aşımı.
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=120.0)
        # TEK DÜZELTME — İKİNCİ DEĞERLENDİRİCİ TÜM KRİTER PUANLARI: prompt artık HER kriter için
        # (yalnız farklı olanlar değil) bir KRITER_PUAN/KRITER_GEREKCE çifti istiyor — eski 1400
        # bütçesi (yalnız 1-2 farklı kriter için yeterliydi) 12+ kriterlik tam listeyi kesebilirdi.
        # Aynı TEK çağrı, yalnızca çıktı bütçesi büyütüldü — yeni bir AI çağrısı/pass EKLENMEDİ.
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=5000, temperature=0.3,
            messages=[{"role": "user", "content": prompt}],
        )
        record_anthropic_usage(candidate_id, level, "claude-sonnet-4-6", "report_reviewer", response)
        raw_out = (response.content[0].text or "").strip()
        # TUR 3 / GÖREV 2.1 — HAM çıktıyı (parse öncesi) logla + system_decision'a kalıcı iz.
        print(f"[REVIEWER_RAW] c={candidate_id} L{level} len={len(raw_out)}\n{raw_out[:1500]}")
        record_system_decision(candidate_id, level, "mufettis_ham_cikti",
                               "İkinci değerlendiricinin parse ÖNCESİ ham çıktısı (teşhis için).",
                               {"raw": raw_out[:4000]})
        return raw_out, "ok", ""
    except Exception as e:
        print(f"UYARI (run_report_reviewer c={candidate_id} L{level}): {type(e).__name__}: {e}")
        return "", "failed", f"{type(e).__name__}: {e}"

def parse_reviewer_criterion_scores(notes: str) -> dict:
    """Müfettiş çıktısındaki 'KRITER_PUAN: <KİMLİK> = <p>/<max>' satırlarını ayrıştırır — iş emri
    GÖREV 6.1: kimlik (P1, K3, ...) ile, ARTIK ada göre DEĞİL (benzer isimli pozisyon/profil
    kriterleri karışıyordu). Dönüş: {kimlik: (puan, model_maks)} — model_maks yalnız teşhis
    içindir, gerçek tavan HER ZAMAN kriter listesinden (build_reviewer_diff_block/
    compute_reviewer_overall) okunur, modelin yazdığı sayı GÜVENİLMEZ."""
    out = {}
    for m in re.finditer(r"KR[İI]TER_PUAN\s*:\s*\**\s*([PK]\d+)\s*\**\s*=\s*(\d+)\s*/\s*(\d+)", notes or "", re.IGNORECASE):
        cid = m.group(1).upper()
        out[cid] = (int(m.group(2)), int(m.group(3)))
    return out

# İş emri — KANIT BÜTÜNLÜĞÜ VE İKİNCİ DEĞERLENDİRİCİ ÇIKTISI / KALEM 4a — KÖK NEDEN: model bazen
# kendi genel güven damgasını ("GUVEN_DUZEYI: ...", DOKUNULMAYACAK — yalnız yönetici kaydı içindir)
# KRITER_GEREKCE satırının SONUNA köşeli parantezle ekliyor; bu satır tek satır REGEX'iyle ('.+$')
# yakalandığı için ham etiket doğrudan müşteri tablosuna (devralma) veya diff bloğuna sızıyordu.
_INLINE_GUVEN_DUZEYI_TAG_RE = re.compile(r"\[\s*GUVEN_DUZEYI\s*:[^\]]*\]\s*", re.IGNORECASE)

# İş emri — KAPI EŞİĞİ VE SON TUTARLILIK / MADDE 3 — KÖK NEDEN: yukarıdaki (eski) regex TEK
# SATIRLA sınırlıydı ('.+$', DOTALL yok) — prompt KRITER_GEREKCE için "2-3 cümle" istiyor (bkz.
# reviewer prompt'u), model bu metni bazen kendi çıktısında satır kaydırıyor; regex İLK satırdan
# sonrasını SESSİZCE atıyordu — gerekçe cümle/kelime ortasında (görünürde "karakter sınırına
# takılmış gibi") kesiliyordu. Fix: değer, bir SONRAKİ KRITER_PUAN/KRITER_GEREKCE/GUVEN_DUZEYI
# satırına ya da metnin sonuna kadar (satır atlamalarını da İÇEREREK) yakalanır.
_KRITER_GEREKCE_RE = re.compile(
    r"(?ms)^\s*KR[İI]TER_GEREKCE\s*:\s*\**\s*([PK]\d+)\s*\**\s*=\s*(.+?)"
    r"(?=\n\s*KR[İI]TER_(?:PUAN|GEREKCE)\s*:|\n\s*GUVEN_DUZEYI\s*:|\Z)",
    re.IGNORECASE)
# Rejenerasyon güvenlik ağı — kök neden (yukarıda) çözülse de model PATOLOJİK derecede uzun tek
# bir gerekçe üretirse müşteri tablosu şişmesin; kesme HER ZAMAN cümle sonunda yapılır, yarım
# cümle YAZILMAZ (sınır aşılan cümle tamamen atılır).
_GEREKCE_MAX_LEN = 600

def _truncate_at_sentence_boundary(text: str, max_len: int) -> str:
    t = (text or "").strip()
    if len(t) <= max_len:
        return t
    sents = re.split(r'(?<=[.!?])\s+', t)
    out, total = [], 0
    for s in sents:
        if total + len(s) + 1 > max_len:
            break
        out.append(s)
        total += len(s) + 1
    return " ".join(out).strip() if out else t[:max_len].rsplit(" ", 1)[0].rstrip(",;:") + "…"

def parse_reviewer_criterion_gerekce(notes: str) -> dict:
    """Müfettişin 'KRITER_GEREKCE: <KİMLİK> = <metin>' satırlarını kimliğe göre ayrıştırır
    (yalnız birincilden FARKLI puan verdiği kriterler için beklenir). Dönüş: {kimlik: metin}."""
    out = {}
    for m in _KRITER_GEREKCE_RE.finditer(notes or ""):
        cid = m.group(1).upper()
        text = _INLINE_GUVEN_DUZEYI_TAG_RE.sub("", m.group(2).strip()).strip()
        text = _truncate_at_sentence_boundary(text, _GEREKCE_MAX_LEN)
        if text:
            out[cid] = text
    return out

# İŞ 6T — SEMANTİK TUTARLILIK. Müfettişin 'SEMANTIC_ISSUE: <KİMLİK> = <kısa neden>' satırlarını
# ayrıştırır (bkz. run_report_reviewer prompt'undaki '=== SEMANTİK TUTARLILIK ===' bloğu).
# GÖRÜNTÜLEME AMAÇLIDIR — bu fonksiyonun/dönüşünün G/K/E/S'i, evaluability'yi, primary/Genel puanı
# veya recommendation'ı DEĞİŞTİRMESİ YASAK (bkz. build_semantic_issue_block + append_reviewer_section
# — yalnız Ek Görüş'e eklenir, hiçbir tabloya/skora dokunmaz).
_SEMANTIC_ISSUE_RE = re.compile(
    r"(?ms)^\s*SEMANTIC_ISSUE\s*:\s*\**\s*([PK]\d+)\s*\**\s*=\s*(.+?)"
    r"(?=\n\s*SEMANTIC_ISSUE\s*:|\n\s*KR[İI]TER_(?:PUAN|GEREKCE)\s*:|\n\s*GUVEN_DUZEYI\s*:|\Z)",
    re.IGNORECASE)
_SEMANTIC_ISSUE_MAX_LEN = 220

def parse_reviewer_semantic_issues(notes: str) -> dict:
    """İŞ 6T — Dönüş: {kimlik: kısa_neden}. Kimlik listede yoksa (bkz. build_semantic_issue_block)
    render aşamasında sessizce atlanır — sistemi bozacak bir kimlik uydurma riski YOK."""
    out = {}
    for m in _SEMANTIC_ISSUE_RE.finditer(notes or ""):
        cid = m.group(1).upper()
        reason = _INLINE_GUVEN_DUZEYI_TAG_RE.sub("", m.group(2).strip()).strip()
        reason = _truncate_at_sentence_boundary(reason, _SEMANTIC_ISSUE_MAX_LEN)
        if reason:
            out[cid] = reason
    return out

# İŞ 6W-FIX1 — KÖK NEDEN (İş 6W teşhisinde synthetic doğrulandı): çağıran taraf
# `parse_reviewer_semantic_issues(scores_raw or notes_wo_confidence)` gibi bir Python `or` ile
# yalnız TEK bir kaynağı tarıyordu — `scores_raw` (GUVEN_DUZEYI satırı ZORUNLU olduğu için) neredeyse
# HER ZAMAN truthy olduğundan, model bir SEMANTIC_ISSUE satırını serbest-metin (KRİTER PUANLARI
# başlığından ÖNCEKİ) kısma yazarsa bu satır SESSİZCE kayboluyordu. Reviewer prompt'u modele
# SEMANTIC_ISSUE'yu NEREYE yazacağını KESİN olarak dikte etmiyor (bkz. İş 6W) — bu yüzden parser
# ÇIKTININ HANGİ GEÇERLİ BÖLÜMÜNDE olursa olsun aynı satırı yakalamalı. Fix: TEK kaynak seçmek
# yerine BİRDEN FAZLA kaynak ayrı ayrı taranır, sonuçlar deterministik biçimde BİRLEŞTİRİLİR (aynı
# kimlik+aynı metin -> tek kayıt; aynı kimlik+FARKLI metin -> veri KAYBETMEDEN ikisi de saklanır).
def _merge_semantic_issues(*sources: dict) -> dict:
    """İŞ 6W-FIX1 — birden fazla `parse_reviewer_semantic_issues(...)` sonucunu kayıpsız birleştirir.
    Aynı kimlik (P#/K#) birden fazla kaynakta AYNI metinle geçiyorsa tek kayıt (duplicate render
    olmaz); FARKLI metinle geçiyorsa ikisi de ' | ' ile birleştirilerek korunur (veri KAYBI yok).
    Kaynak sırası ÖNEMLİ DEĞİL — sonuç deterministik (kimlik+metin kümesi girdi sırasından bağımsız)."""
    merged: dict = {}
    for src in sources:
        for cid, reason in (src or {}).items():
            if cid not in merged:
                merged[cid] = [reason]
            elif reason not in merged[cid]:
                merged[cid].append(reason)
    return {cid: " | ".join(reasons) for cid, reasons in merged.items()}

def build_semantic_issue_block(semantic_issues: dict, position_criteria: list, profile_criteria: list) -> str:
    """İŞ 6T — 'SEMANTIC_ISSUE' kimliklerini kriter ADINA çevirip kısa bir madde listesi üretir.
    SADECE GÖRÜNTÜLEME — pos/prof tablolarına, awarded'a, evaluability'ye, Genel Puan'a veya
    recommendation'a DOKUNMAZ; çağıran (append_reviewer_section) bu bloğu yalnız Ek Görüş metnine
    ekler. Kimlik listede karşılığı yoksa (model uydurduysa) o satır sessizce atlanır."""
    if not semantic_issues:
        return ""
    lines = []
    for criteria_list, prefix in ((position_criteria or [], "P"), (profile_criteria or [], "K")):
        for i, c in enumerate(criteria_list, start=1):
            cid = f"{prefix}{i}"
            reason = semantic_issues.get(cid)
            if not reason:
                continue
            name = c.get("name") if isinstance(c, dict) else c
            if not name:
                continue
            lines.append(f"- **{name}**: {reason}")
    return "\n".join(lines)

# TUR 3 / GÖREV 3 — müfettiş "susmuş" (yalnızca klişe / boş) mu? Bu kalıplar ve <40 kr → sus.
_REVIEWER_EMPTY_RE = re.compile(
    r"g[öo]r[üu][şs]\s*yok|belirgin\s+bir\s+(?:g[öo]r[üu][şs]\s+ayr[ıi]l[ıi][ğg][ıi]|sorun|farkl[ıi]l[ıi]k|eksik|hata)\s*(?:yok|bulunma|g[öo]r[üu]lme)|"
    r"genel\s+olarak\s+(?:yeterli|uygun|tutarl[ıi]|olumlu|ba[şs]ar[ıi]l[ıi])|"
    r"de[ğg]erlendirme\s+(?:uygun|yeterli|tutarl[ıi])|kal[ıi]brasyon\s+uygun|katmayacak\s+bir\s+[şs]ey",
    re.IGNORECASE)

def _split_reviewer_output(raw: str):
    """Ham müfettiş çıktısını (serbest metin, kriter puanları bloğu) ayırır.
    Dönüş: (serbest_metin, kriter_puan_bloğu_metni)."""
    if not raw:
        return "", ""
    raw = strip_reviewer_meta_tags(raw)
    m = re.search(r"(?im)^[ \t=*#-]*KR[İI]TER\s+PUANLARI[ \t=*#-]*$", raw)
    if m:
        return raw[:m.start()].strip(), raw[m.end():].strip()
    # başlık yoksa: ilk KRITER_PUAN satırından böl
    m2 = re.search(r"(?im)^\s*KR[İI]TER_PUAN\s*:", raw)
    if m2:
        return raw[:m2.start()].strip(), raw[m2.start():].strip()
    return raw.strip(), ""

# İş emri GÖREV 4 — ikinci değerlendiricinin ADAY ÖZGÜVENİ izlenimi: "GÖRÜŞ YOK" mantığından
# BAĞIMSIZ, ayrı bir blok (kriter farkı olmasa bile MUTLAKA üretilir — madde 4.1). Bu ikisi
# _extract_confidence_impression/_strip_confidence_block ile serbest metinden AYRIŞTIRILIR ki
# KRİTER PUANLARI bloğuna veya _format_reviewer_notes'a karışmasın.
_CONFIDENCE_BLOCK_RE = re.compile(r"(?is)===\s*ADAY\s+[ÖO]ZG[ÜU]VEN[İI]\s+[İI]ZLEN[İI]M[İI]\s*===\s*(.*?)(?=\n\s*===|\Z)")

def _extract_confidence_impression(raw: str) -> str:
    """'=== ADAY ÖZGÜVENİ İZLENİMİ ===' bloğunu döner. Blok yoksa VEYA içeriği 'YETERSİZ VERİ'
    ise '' döner (iş emri madde 4.7 — transkript yetmiyorsa zorla üretilmez)."""
    if not raw:
        return ""
    m = _CONFIDENCE_BLOCK_RE.search(raw)
    if not m:
        return ""
    text = m.group(1).strip()
    if not text or _tr_upper(text).rstrip(".") == "YETERSİZ VERİ":
        return ""
    return text

def _strip_confidence_block(raw: str) -> str:
    """Aday özgüveni bloğunu ham çıktıdan çıkarır (kalan metin _split_reviewer_output'a gider)."""
    return _CONFIDENCE_BLOCK_RE.sub("", raw or "").strip()

def parse_reviewer_confidence_level(notes: str) -> Optional[str]:
    """İş emri — 'DOKUNULMAYACAKLAR': müfettişin KENDİ değerlendirmesine duyduğu güven düzeyi ana
    rapora GİRMEZ, yalnız yönetici kaydına (system_decision) düşer. 'GUVEN_DUZEYI: <seviye> —
    <neden>' satırını ayrıştırır; yoksa None."""
    m = re.search(r"(?im)^\s*GUVEN_DUZEYI\s*:\s*(.+)$", notes or "")
    return m.group(1).strip() if m else None

def reviewer_has_substance(free_text: str) -> bool:
    """TUR 3 / GÖREV 3.3 — müfettişin serbest metni RAPORA basılmaya değer mi?
    'GÖRÜŞ YOK' / yalnızca klişe / çok kısa → HAYIR (bölüm hiç basılmaz)."""
    t = (free_text or "").strip()
    if not t:
        return False
    # sadece klişe cümlelerden mi ibaret?
    _stripped = re.sub(r"[\s\.\,\;\:\!\?\-–—\*_]", "", t.lower())
    if len(_stripped) < 25:
        return False
    # cümlelere böl; klişe olmayan en az bir cümle var mı?
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", t) if s.strip() and len(s.strip()) > 8]
    real = [s for s in sents if not _REVIEWER_EMPTY_RE.search(s)]
    return len(real) >= 1 and sum(len(s) for s in real) >= 40

def _format_reviewer_notes(free_text: str) -> str:
    """TUR 3 / GÖREV 3 — SERBEST METİN temizliği (sabit şablon YOK). Meta etiketler + KRITER_PUAN
    satırları zaten _split_reviewer_output ile ayrıldı; burada yalnızca biçim onarımı."""
    t = (free_text or "").strip()
    if not t:
        return ""
    t = re.sub(r"(?m)^\s*KR[İI]TER_PUAN\s*:.*$\n?", "", t)
    # eski turlardan kalma numaralı başlık kalıntısı gelirse sadeleştir
    t = re.sub(r"(?m)^\s*\**\s*[1-6][\.\)]\s*\**\s*", "", t)
    t = re.sub(r"(:\*{0,2})[ \t]*(?=[A-ZÇĞİÖŞÜ][a-zçğıöşü])", r"\1 ", t)
    t = repair_report_spacing(t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t

# 2026-09 rapor yeniden tasarımı — eski _reviewer_score_tables ('Kriter | GPT | Müfettiş | Fark'
# KARŞILAŞTIRMA TABLOSU) KALDIRILDI: iş emri madde 9 "Karşılaştırma tablosu oluşturma" diyor.
# TEK DÜZELTME — İKİNCİ DEĞERLENDİRİCİ KRİTER PUANLARI: build_reviewer_diff_block artık İKİNCİ
# değerlendiricinin PUANLADIĞI TÜM kriterleri listeler (yalnız birincilden farklı olanları DEĞİL —
# bu kısıtlama kaldırıldı, admin artık her kriterde ikinci değerlendiricinin GERÇEK puanını görmek
# istiyor). Format: "Kriter adı — X / maksimum puan" + "Açıklama: <mevcut gerekçe>". Birincilin
# puanı buraya KOPYALANMAZ/karşılaştırılmaz — yalnız rv_scores'taki (ikinci değerlendiricinin KENDİ
# ürettiği) puan kullanılır; reviewer bir kriteri hiç puanlamadıysa (rv_scores'ta yok) o kriter
# HİÇ listelenmez (uydurma yok).
def build_reviewer_diff_block(rv_scores: dict, rv_gerekce: dict, position_criteria: list, profile_criteria: list,
                              pos_table_text: str, prof_table_text: str) -> str:
    """İkinci değerlendiricinin PUANLADIĞI her kriter için satır üretir: 'Kriter adı — X/maksimum'
    + (varsa) 'Açıklama: <ikinci değerlendiricinin kendi gerekçesi>'. GÖREV 6.1+6.2 — eşleştirme
    KİMLİK (P#/K#) üzerinden, ADA göre DEĞİL; tavan HER ZAMAN kriter listesinden (modelin kendi
    yazdığı 'maksimum' asla güvenilmez — GÖREV 6.2). Birincilin puanına hiç bakmaz/kıyaslamaz."""
    lines = []
    for criteria_list, table_text, prefix in ((position_criteria or [], pos_table_text, "P"),
                                              (profile_criteria or [], prof_table_text, "K")):
        for i, c in enumerate(criteria_list, start=1):
            cid = f"{prefix}{i}"
            rv = rv_scores.get(cid)
            if rv is None:
                continue  # ikinci değerlendirici bu kriteri hiç puanlamadı — uydurma yok, atla
            name = c["name"] if isinstance(c, dict) else c
            real_cap = _safe_int(c.get("weight")) if isinstance(c, dict) else None
            eff_cap = real_cap or rv[1]
            if not eff_cap:
                continue
            rv_awarded = max(0, min(rv[0], eff_cap))
            lines.append(f"**{name}** — {rv_awarded}/{eff_cap}")
            gerekce = rv_gerekce.get(cid)
            if gerekce:
                lines.append(f"Açıklama: {str(gerekce).strip()}")
    return "\n\n".join(lines)

# İş emri madde 3 — İkinci Değerlendirici Görüşü, Kişisel ve Bilişsel Profil'den HEMEN sonra
# yer alır. Bu bölüm 2. değerlendiricinin (ayrı bir LLM çağrısı) tamamlanmasını BEKLEDİĞİ için
# finalize_interview assemble ederken bu YER TUTUCUYU bırakır; append_reviewer_section (SONRA,
# arka planda) onu gerçek içerikle DEĞİŞTİRİR ya da (görüş yoksa) SİLER — asla literal olarak
# rapora sızmaz (PDF renderer'da da savunma amaçlı ikinci bir temizlik var).
_REVIEWER_SLOT_MARK = "<<İKİNCİ_DEĞERLENDİRİCİ_GÖRÜŞÜ_YERİ>>"
_REVIEWER_HEAD = "**İkinci Değerlendirici Görüşü:**"

# İş emri — KANIT BÜTÜNLÜĞÜ VE İKİNCİ DEĞERLENDİRİCİ ÇIKTISI / KALEM 4b — KÖK NEDEN: bölüm başlığı
# zaten _REVIEWER_HEAD ile ayrıca basılıyor; model serbest metninin İLK SATIRINA da kendi başlığını
# ("İKİNCİ DEĞERLENDİRİCİ GÖRÜŞÜ") yazınca başlık İKİ KEZ çıkıyordu.
_REVIEWER_HEAD_TEXT_NORM = _tr_upper(re.sub(r"[*:]", "", _REVIEWER_HEAD)).strip()

def _strip_duplicate_reviewer_heading(text: str) -> str:
    if not text:
        return text
    parts = text.split("\n", 1)
    first_clean = re.sub(r"[*:#\-\s]+$", "", re.sub(r"^[*:#\-\s]+", "", parts[0]))
    if _tr_upper(first_clean).strip() == _REVIEWER_HEAD_TEXT_NORM:
        return parts[1].strip() if len(parts) > 1 else ""
    return text

# İş emri — KANIT BÜTÜNLÜĞÜ VE İKİNCİ DEĞERLENDİRİCİ ÇIKTISI / KALEM 3 — KÖK NEDEN: müfettiş
# bazen ADAY hakkında değil RAPORUN/PUANIN KENDİSİ hakkında konuşuyor (ör. "raporda X kriteri
# 'değerlendirilemeyen' arasında yok, ancak...", "bu puan görece yüksek görünüyor", "...
# işaretlenmesi daha uygun olurdu") — bu iç kalite-denetimi dili müşteri Ek Görüş'üne AYNEN
# giriyordu, rapor kendi güvenilirliğine gölge düşürüyordu. Müşteriye giden Ek Görüş yalnız ADAY
# hakkında gözlem içermeli.
# NOT (kendi bulunan regresyon, testte yakalandı): bare "raporda" ÇOK GENİŞTİ — reviewer_
# contradiction_unresolved mekanizmasının dayandığı MEŞRU bir cümle kalıbını da yakalıyordu
# ('Raporda "..." iddiası transkriptte desteklenmiyor' — bu adayla İLGİLİ bir kanıt-denetimi,
# raporun KENDİ yapısal/etiketleme kararını eleştiren KALEM 3 örneklerinden FARKLI). Desen artık
# yalnız "raporda ... yer almıyor" (raporun KENDİ listeleme/etiketleme eksikliğini eleştiren) gibi
# KALEM 3'ün somut örnekleriyle eşleşen, DAHA DAR kalıpları arar.
# İş emri — KAPI EŞİĞİ VE SON TUTARLILIK / MADDE 6 — desen genişletilmedi (bir önceki turun geniş
# desenin meşru cümle sildiği dersi hâlâ geçerli), YALNIZ gerçek örnekle eşleşen iki DAR kalıp
# eklendi: "raporda ... değerlendirilemedi olarak bırakılmış" ve "genel puanının ... olduğundan
# daha zayıf görünmesine".
_REPORT_META_COMMENTARY_RE = re.compile(
    r"raporda[^.\n]{0,80}yer\s+almıyor|\braporun\s+kendisi|"
    r"i[şs]aretlenmesi\s+daha\s+uygun|olarak\s+i[şs]aretlenmesi\s+daha\s+uygun|"
    r"bu\s+puan\s+(?:g[öo]rece\s+)?(?:y[üu]ksek|d[üu][şs][üu]k)\s+g[öo]r[üu]n[üu]yor|"
    r"gerek[çc]elendirilmi[şs]\s*;?\s*ancak|"
    r"raporda[^.\n]{0,120}de[ğg]erlendirilemedi['’]?\s+olarak\s+b[ıi]rak[ıi]lm[ıi][şs]|"
    r"genel\s+puan[ıi]n[ıi]n[^.\n]{0,60}oldu[ğg]undan\s+daha\s+zay[ıi]f\s+g[öo]r[üu]nmesine",
    re.IGNORECASE)

def strip_report_meta_commentary(text: str) -> tuple:
    """KALEM 3 — RAPORUN/PUANIN KENDİSİ hakkında konuşan cümleleri çıkarır (adayı DEĞİL). Dönüş:
    (temiz_metin, çıkarılan_cümleler[])."""
    if not text:
        return text, []
    sents = re.split(r'(?<=[.!?])\s+', text)
    kept, dropped = [], []
    for s in sents:
        if _REPORT_META_COMMENTARY_RE.search(s):
            dropped.append(s.strip())
        else:
            kept.append(s)
    return " ".join(kept).strip(), dropped

def recompute_overall_decision(candidate_id: int, level: int, reviewer_score_position=None, reviewer_score_profile=None):
    """TEK KARAR KAYNAĞI'nın 2. çağrısı (iş emri madde 6+21) — append_reviewer_section reviewer'ın
    kendi pozisyon/profil puanlarını türettikten SONRA burayı çağırır; Genel Puan artık mevcut
    OLAN 4 puana kadar (1. pozisyon/profil + 2. pozisyon/profil) genişler ve karar YENİDEN üretilir.
    finalize_interview'daki İLK hesaplama (yalnız 1. değerlendirici) YANLIŞ değildi — bu sadece
    onu daha fazla veriyle GÜNCELLER; aynı TEK fonksiyondan (compute_genel_puan) geçer.
    Dönüş: (genel_puan, recommendation, score_position, score_profile) — YOK ise None. İş emri —
    RAPOR İÇERİK STANDARDI / A2: çağıran (append_reviewer_section) bu DEĞERLERİ artık Öneri
    Gerekçesi'ni yeniden üretmek için KULLANIR — önceden bu fonksiyon None dönüyordu, DB'yi
    güncelliyordu ama rapor METNİNDEKİ (zaten basılmış) Öneri Gerekçesi asla haberdar olmuyordu.
    İŞ EMRİ — FINAL EVALUATION ARCHITECTURE / madde 3 — CANONICAL FINAL SCORE: bu fonksiyon artık
    final_score_position/final_score_profile'ı da AYNI transaction'da, TEK YERDE yazar
    (_final_component_score ile — İş 6V-FIX'in render-anı hesaplamasının YERİNE geçer, artık
    PERSIST edilir). Bundan sonra HİÇBİR yüzey (rapor metni/admin/PDF) bu değeri yeniden
    hesaplamaz, yalnız DB'den OKUR."""
    db = get_db()
    try:
        row = db.execute("SELECT score_position, score_profile FROM interviews WHERE candidate_id=? AND level=?",
                         (candidate_id, level)).fetchone()
        if not row:
            return None
        genel_puan = compute_genel_puan(row["score_position"], row["score_profile"], reviewer_score_position, reviewer_score_profile)
        recommendation = decide_recommendation(genel_puan) or "Değerlendirilemedi"
        final_score_position = _final_component_score(row["score_position"], reviewer_score_position)
        final_score_profile = _final_component_score(row["score_profile"], reviewer_score_profile)
        db.execute("UPDATE interviews SET score=?, recommendation=?, reviewer_score_position=?, reviewer_score_profile=?, "
                  "final_score_position=?, final_score_profile=? "
                  "WHERE candidate_id=? AND level=?",
                  (genel_puan, recommendation, reviewer_score_position, reviewer_score_profile,
                   final_score_position, final_score_profile, candidate_id, level))
        db.commit()
        return genel_puan, recommendation, row["score_position"], row["score_profile"]
    finally:
        db.close()

# İŞ 1 — TAKEOVER SONRASI RAPOR TUTARLILIĞI (Problem A / "Kader" vakası): devralma sonrası "Puanlama
# Kapsamı" ve "Değerlendirilemeyen Alanlar" TEK ortak kaynaktan (final, devralma-sonrası kriter
# tablosu) üretilsin; ikisini yazan patch işlemlerinden biri sessizce no-op olursa artık fark
# edilmeden geçmesin. Skor/karar/validator/takeover/scope-clamp mantığına DOKUNULMADI — yalnız bu
# iki metin bölümünün üretim/yazım mekanizması.
def _extract_disqualified_criteria_names(table_text: str) -> list:
    """TEK GERÇEK KAYNAK: final kriter tablosunun (devralma dahil tüm mutasyonlardan SONRAKİ hali)
    satırlarını tarar, _DISQUALIFIED_CELL_RE ile eşleşen (GERÇEKTEN 'Değerlendirilemedi (sistem)'
    kalan) satırların kriter adını döner. Puanlama Kapsamı ve Değerlendirilemeyen Alanlar BU
    fonksiyonun döndürdüğü AYNI listeden üretilirse, ikisi ayrı ayrı hesaplanamaz — yapısal olarak
    birbirinden farklı sayı/liste veremezler."""
    names = []
    for _ln in (table_text or "").splitlines():
        if _DISQUALIFIED_CELL_RE.search(_ln):
            _c0 = _ln.strip().strip("|").split("|")[0].strip()
            if _c0:
                names.append(_c0)
    return names

def _patch_report_section(report_text: str, head: str, pattern, new_body: str, section_label: str,
                          candidate_id: int, level: int) -> str:
    """Puanlama Kapsamı/Değerlendirilemeyen Alanlar yeniden-yazım yardımcı. Önceden bu iki bölüm
    birbirinden BAĞIMSIZ, sessizce no-op olabilen iki ayrı 'if HEAD in report_text' bloğuydu — biri
    başarısız olursa diğeri fark etmeden devam ediyordu (Kader vakası: Puanlama Kapsamı güncellendi,
    Değerlendirilemeyen Alanlar eski/7 kriterlik listede kaldı). Artık başarısızlık SESSİZ DEĞİL:
    head bulunamazsa record_system_decision'a görünür bir uyarı yazılır — puanlama/skor/karar
    mantığına dokunmaz, yalnız görünürlük sağlar."""
    if head in report_text:
        return pattern.sub(head + "\n" + new_body, report_text, count=1)
    try:
        record_system_decision(candidate_id, level, "rapor_bolum_patch_basarisiz",
                               f"İŞ 1 — devralma sonrası '{section_label}' bölümü rapor metninde bulunamadı, "
                               "güncel liste rapora YAZILAMADI (bölüm eski/stale kalmış olabilir).",
                               {"bolum": section_label, "beklenen_baslik": head})
    except Exception as e:
        print(f"UYARI (_patch_report_section log c={candidate_id} L{level} bolum={section_label}): {type(e).__name__}: {e}")
    return report_text

def _verify_scope_consistency(pos_table_text: str, prof_table_text: str, dropped_pos: list, dropped_prof: list) -> list:
    """Yapısal doğrulama (defense-in-depth, test edilebilir): dropped listesindeki HİÇBİR kriter
    adı, final tabloda GERÇEKTEN puanlı (Değerlendirilemedi (sistem) değil) bir satırda görünmesin;
    tersine, tabloda hâlâ 'Değerlendirilemedi (sistem)' olan bir kriter dropped listesinden eksik
    olmasın. Sorun bulunursa açıklayıcı string listesi döner (boş liste = tutarlı). Rapor
    içeriğini/kararı DEĞİŞTİRMEZ, yalnızca tespit eder."""
    problems = []
    for table_text, dropped in ((pos_table_text, dropped_pos), (prof_table_text, dropped_prof)):
        for _ln in (table_text or "").splitlines():
            if _ln.count("|") < 2:
                continue
            cells = [x.strip() for x in _ln.strip().strip("|").split("|")]
            if len(cells) < 2:
                continue
            cname = cells[0].strip()
            if not cname:
                continue
            is_disqualified = bool(_DISQUALIFIED_CELL_RE.search(_ln))
            if cname in dropped and not is_disqualified:
                problems.append(f"'{cname}' dropped listesinde ama tabloda PUANLI görünüyor")
            if is_disqualified and cname not in dropped:
                problems.append(f"'{cname}' tabloda Değerlendirilemedi (sistem) ama dropped listesinde YOK")
    return problems

def append_reviewer_section(candidate_id: int, level: int, transcript_text: str, modality_block: str,
                            position_criteria: Optional[list] = None) -> dict:
    """İkinci (bağımsız) değerlendiriciyi NİHAİ rapor üzerinde çalıştırır. Yalnız birincilden
    GERÇEKTEN farklı puanladığı kriterleri diff olarak gösterir (iş emri madde 9 — karşılaştırma
    tablosu YOK); ayrıca kendi pozisyon/profil GENEL puanlarını türetip recompute_overall_decision
    ile Genel Puan'a (iş emri madde 6) katar. Müfettiş atlanır/patlarsa rapor DEĞİŞMEDEN kalır,
    Genel Puan yalnızca 1. değerlendiriciden hesaplanmış haliyle kalır. İdempotent: yer tutucu
    zaten değiştirilmişse (blok zaten varsa) tekrar eklenmez.
    İŞ 6X-1 — Dönüş: {'rv_scores', 'rv_gerekce', 'rv_semantic'} sözlükleri — Final Report Quality
    Gate'in (run_deferred_finish_job'da bu fonksiyondan SONRA çağrılır) reviewer bulgularını YENİDEN
    AI/DB round-trip'i OLMADAN kullanabilmesi için. Erken çıkış yollarında boş sözlük döner (mevcut
    davranış aynen korunur, yalnız dönüş tipi None'dan dict'e değişti).
    İŞ EMRİ — FINAL EVALUATION ARCHITECTURE / DEĞİŞMEZ LEVEL MİMARİSİ: second evaluator ARTIK
    YALNIZ L3'te çalışır (L1/L2'de reviewer YOK). Bu, çağıranın (run_deferred_finish_job) level
    kontrolüyle SAĞLANIR — AMA yalnız çağırana güvenmek "Doğrudan endpoint çağrısıyla yanlış
    pipeline'a girilmesi" riskini AÇIK bırakır; bu yüzden fonksiyon KENDİSİ de level != 3 ise
    HİÇBİR AI çağrısı yapmadan erken döner (savunma amaçlı, ikinci bir kapı)."""
    if level != 3:
        return {}
    db = get_db()
    try:
        row = db.execute("SELECT report FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, level)).fetchone()
    finally:
        db.close()
    final_report = (row["report"] if row else "") or ""
    if not final_report.strip():
        return {}
    if _REVIEWER_HEAD in final_report or _REVIEWER_SLOT_MARK not in final_report:
        return {}  # zaten işlendi (yeniden üretim/kurtarma taraması çift eklemez)

    # İş emri — KANIT BÜTÜNLÜĞÜ VE İKİNCİ DEĞERLENDİRİCİ ÇIKTISI / KALEM 2 — devralmanın (aşağıda)
    # ikinci değerlendiricinin gerekçesindeki [mm:ss] referansını doğrulayabilmesi için transkript
    # GÖRÜNÜMÜ (rol+elapsed_ms) gerekir — önceden bu fonksiyona hiç verilmiyordu.
    try:
        db_tv = get_db()
        try:
            _iv_tv = db_tv.execute("SELECT messages, started_at FROM interviews WHERE candidate_id=? AND level=?",
                                   (candidate_id, level)).fetchone()
        finally:
            db_tv.close()
        transcript_view = build_transcript_view(_iv_tv["messages"] if _iv_tv else "[]", level,
                                                 _iv_tv["started_at"] if _iv_tv else None, for_report=True)
    except Exception as e:
        print(f"UYARI (append_reviewer_section transcript_view c={candidate_id} L{level}): {type(e).__name__}: {e}")
        transcript_view = []

    def _save(updated_report: str):
        db2 = get_db()
        try:
            db2.execute("UPDATE interviews SET report=? WHERE candidate_id=? AND level=?", (updated_report, candidate_id, level))
            db2.commit()
        finally:
            db2.close()

    notes, status, err = run_report_reviewer(candidate_id, level, transcript_text, final_report, modality_block,
                                             position_criteria=position_criteria)
    _set_reviewer_status(candidate_id, level, status, err)
    if not notes.strip():
        _save(final_report.replace(_REVIEWER_SLOT_MARK, "").strip())
        return {}

    # İş emri GÖREV 4 — aday özgüveni izlenimi, "GÖRÜŞ YOK" mantığından TAMAMEN BAĞIMSIZ ayrı
    # bir blok (madde 4.1: kriter farkı/eleştiri olmasa bile MUTLAKA üretilir — yalnız veri
    # yetersizse, madde 4.7, boş kalır). Serbest metinden/KRİTER PUANLARI'ndan önce ayıklanır.
    confidence_text = _extract_confidence_impression(notes)
    notes_wo_confidence = _strip_confidence_block(notes)

    free_raw, scores_raw = _split_reviewer_output(notes_wo_confidence)
    # KALEM 4b — model kendi serbest metninin başına bölüm başlığını da yazmışsa (başlık zaten
    # _REVIEWER_HEAD ile ayrıca basılıyor) o satır çıkarılır.
    free_raw = _strip_duplicate_reviewer_heading(free_raw)
    # KALEM 3 — RAPORUN/PUANIN KENDİSİ hakkında konuşan (adayı değil) cümleler müşteri bölümünden
    # çıkarılır; iç kayıt olarak (yönetici görebilir) system_decision'a taşınır.
    free_raw, _dropped_meta_sents = strip_report_meta_commentary(free_raw)
    if _dropped_meta_sents:
        record_system_decision(candidate_id, level, "reviewer_meta_yorum_cikarildi",
                               "KALEM 3 — ikinci değerlendiricinin RAPORUN/PUANIN KENDİSİ hakkında konuşan cümleleri müşteri Ek Görüş bölümünden çıkarıldı (yalnız yönetici kaydı).",
                               {"cikarilan_cumleler": _dropped_meta_sents})
    rv_scores = parse_reviewer_criterion_scores(scores_raw or notes_wo_confidence)
    rv_gerekce = parse_reviewer_criterion_gerekce(scores_raw or notes_wo_confidence)
    # İŞ 6T — semantik tutarlılık notları: yalnız AYIKLAMA + GÖRÜNTÜLEME (render_semantic_block
    # aşağıda, yalnız Ek Görüş'e eklenir). G/K/E/S, evaluability, awarded, Genel Puan, recommendation
    # BURADA hiç DOKUNULMAZ — pos_table_text/prof_table_text/rv_scores'a hiç KARIŞMAZ.
    # İŞ 6W-FIX1 — `or` ile TEK kaynak seçmek yerine hem serbest-metin (free_raw) hem KRİTER PUANLARI
    # bloğu (scores_raw) AYRI AYRI taranır ve kayıpsız birleştirilir (bkz. _merge_semantic_issues).
    rv_semantic = _merge_semantic_issues(
        parse_reviewer_semantic_issues(free_raw),
        parse_reviewer_semantic_issues(scores_raw),
    )
    has_view = reviewer_has_substance(free_raw)

    _pos_m = re.search(r'\*\*Pozisyon Yetkinlikleri:\*\*\s*\n([\s\S]*?)(?=\n\*\*[^\n]{2,60}:\*\*|\Z)', final_report)
    _prof_m = re.search(r'\*\*Kişisel ve Bilişsel Profil:\*\*\s*\n([\s\S]*?)(?=\n\*\*[^\n]{2,60}:\*\*|\Z)', final_report)
    pos_table_text = _pos_m.group(1) if _pos_m else ""
    prof_table_text = _prof_m.group(1) if _prof_m else ""

    # İŞ EMRİ — CLAUDE PRIMARY KRİTER TABLOSUNU DEĞİŞTİRMESİN: OpenAI (1. değerlendirici) ve Claude
    # (2. değerlendirici) sonuçları AYRI kalmalı — Claude'un puanı/gerekçesi artık primary'nin
    # kriter tablosunun (score_position/score_profile dahil) ÜZERİNE YAZILMIYOR. Bilinçli, geri
    # alınabilir devre dışı bırakma (geri almak için "if False:" satırını silip içeriği bir seviye
    # sola kaydırmak yeterli) — fonksiyonların (apply_criterion_takeover/apply_reviewer_criterion_
    # correction) kendisi SİLİNMEDİ/değiştirilmedi, yalnız bu çağrı noktaları artık tetiklenmiyor.
    # Claude'un KENDİ 12 kriter puanı/gerekçesi ("İkinci Değerlendirici Görüşü") ve Genel Puan'a
    # katkısı (compute_reviewer_overall/recompute_overall_decision, rv_scores/rv_gerekce üzerinden
    # çalışır, primary tablo metnine YAZMAZ) bu değişiklikten ETKİLENMEDİ.
    if False:
        # İş emri GÖREV 1.3 (VALIDATOR KALİBRASYONU) — DEVRALMA: birincilde 3 denemede düşen ama
        # ikinci değerlendiricide GEÇERLİ (puan+gerekçe) bir değerlendirmesi olan kriterler artık
        # "Değerlendirilemedi" KALMIYOR (bkz. apply_criterion_takeover). Sessizce olmaz — loglanır.
        try:
            _new_pos_tbl, _new_score_pos_tk, _log_pos_tk = apply_criterion_takeover(
                pos_table_text, position_criteria or [], rv_scores, rv_gerekce, "P", transcript_view)
            _new_prof_tbl, _new_score_prof_tk, _log_prof_tk = apply_criterion_takeover(
                prof_table_text, PROFILE_CRITERIA, rv_scores, rv_gerekce, "K", transcript_view)
            _takeover_log = _log_pos_tk + _log_prof_tk
            if _takeover_log:
                if _new_pos_tbl != pos_table_text:
                    final_report = final_report.replace(pos_table_text, _new_pos_tbl, 1)
                    pos_table_text = _new_pos_tbl
                if _new_prof_tbl != prof_table_text:
                    final_report = final_report.replace(prof_table_text, _new_prof_tbl, 1)
                    prof_table_text = _new_prof_tbl
                _db_tk = get_db()
                try:
                    if _new_score_pos_tk is not None:
                        _db_tk.execute("UPDATE interviews SET score_position=? WHERE candidate_id=? AND level=?",
                                      (_new_score_pos_tk, candidate_id, level))
                    if _new_score_prof_tk is not None:
                        _db_tk.execute("UPDATE interviews SET score_profile=? WHERE candidate_id=? AND level=?",
                                      (_new_score_prof_tk, candidate_id, level))
                    _db_tk.commit()
                finally:
                    _db_tk.close()
                record_system_decision(candidate_id, level, "kriter_devralindi",
                                       "GÖREV 1.3 — birincil doğrulayıcıda düşen bazı kriterler için ikinci değerlendiricinin GEÇERLİ puan+gerekçesi kullanıldı; kriter 'Değerlendirilemedi' olarak KALMADI.",
                                       {"devralinan": _takeover_log})
        except Exception as e:
            print(f"UYARI (append_reviewer_section devralma c={candidate_id} L{level}): {type(e).__name__}: {e}")

        # İş emri — FINAL EVALUATION ARCHITECTURE / madde 2 — devralmanın (yukarıda, YALNIZ diskalifiye
        # satırlar) HEMEN SONRASI: L3 Claude reviewer'ın ZATEN PUANLI bir kriterde bulduğu, GROUNDED
        # (kanıtlanabilir) düzeltmesi de final duruma GİREBİLİR — apply_criterion_takeover'ın
        # sorumluluk alanına (diskalifiye) HİÇ dokunmaz, yalnız onun DIŞINDA kalan satırlarda çalışır.
        try:
            _new_pos_tbl2, _new_score_pos_corr, _log_pos_corr = apply_reviewer_criterion_correction(
                pos_table_text, position_criteria or [], rv_scores, rv_gerekce, "P", transcript_view)
            _new_prof_tbl2, _new_score_prof_corr, _log_prof_corr = apply_reviewer_criterion_correction(
                prof_table_text, PROFILE_CRITERIA, rv_scores, rv_gerekce, "K", transcript_view)
            _correction_log = _log_pos_corr + _log_prof_corr
            if _correction_log:
                if _new_pos_tbl2 != pos_table_text:
                    final_report = final_report.replace(pos_table_text, _new_pos_tbl2, 1)
                    pos_table_text = _new_pos_tbl2
                if _new_prof_tbl2 != prof_table_text:
                    final_report = final_report.replace(prof_table_text, _new_prof_tbl2, 1)
                    prof_table_text = _new_prof_tbl2
                _db_corr = get_db()
                try:
                    if _new_score_pos_corr is not None:
                        _db_corr.execute("UPDATE interviews SET score_position=? WHERE candidate_id=? AND level=?",
                                         (_new_score_pos_corr, candidate_id, level))
                    if _new_score_prof_corr is not None:
                        _db_corr.execute("UPDATE interviews SET score_profile=? WHERE candidate_id=? AND level=?",
                                         (_new_score_prof_corr, candidate_id, level))
                    _db_corr.commit()
                finally:
                    _db_corr.close()
                record_system_decision(candidate_id, level, "reviewer_kriter_duzeltmesi",
                                       "İŞ EMRİ — L3 Claude second evaluator, ZATEN PUANLI bir kriterde kanıtlanabilir/grounded bir düzeltme buldu; deterministik doğrulamadan geçtiği için final duruma UYGULANDI (grounding geçemeyenler reddedildi, log'da ayrıca görünür).",
                                       {"duzeltmeler": _correction_log})
        except Exception as e:
            print(f"UYARI (append_reviewer_section reviewer düzeltmesi c={candidate_id} L{level}): {type(e).__name__}: {e}")

    # İş emri — KAYIP ANLATI BÖLÜMLERİ / GÖREV 1.4 (devam, sonraki tur) — devralma SONRASI
    # "Puanlama Kapsamı" bölümü (HER ZAMAN vardır — bkz. render_puanlama_kapsami) YENİDEN
    # HESAPLANIR ve GÜNCELLENİR: devralınan kriterler artık düşmüş sayılmaz.
    try:
        # İŞ 1 — TEK GERÇEK KAYNAK: dropped listeleri, final (devralma-sonrası) tablolardan TEK bir
        # yerden çıkarılır; Puanlama Kapsamı ve Değerlendirilemeyen Alanlar AYNI listeyi kullanır.
        _dropped_pos2 = _extract_disqualified_criteria_names(pos_table_text)
        _dropped_prof2 = _extract_disqualified_criteria_names(prof_table_text)
        _new_kapsami_text = render_puanlama_kapsami(position_criteria or [], PROFILE_CRITERIA, _dropped_pos2, _dropped_prof2)
        final_report = _patch_report_section(final_report, _PUANLAMA_KAPSAMI_HEAD, _PUANLAMA_KAPSAMI_RE,
                                             _new_kapsami_text, "Puanlama Kapsamı", candidate_id, level)
        # MADDE 2 — Değerlendirilemeyen Alanlar, Puanlama Kapsamı ile AYNI (devralma-sonrası)
        # listeden, AYNI anda yeniden yazılır — iki bölüm ASLA farklı listede kalamaz. Patch
        # başarısız olursa (head bulunamazsa) artık sessiz değil — _patch_report_section loglar.
        _new_degerlendirilemeyen_text = render_degerlendirilemeyen_alanlar(_dropped_pos2, _dropped_prof2)
        final_report = _patch_report_section(final_report, _DEGERLENDIRILEMEYEN_ALANLAR_HEAD, _DEGERLENDIRILEMEYEN_ALANLAR_RE,
                                             _new_degerlendirilemeyen_text, "Değerlendirilemeyen Alanlar", candidate_id, level)
        # İŞ 1 — defense-in-depth doğrulama: puanlı bir kriter dropped listesinde (veya tersi)
        # görünüyorsa görünür şekilde logla (rapor içeriğine/karara dokunmaz, yalnız tespit eder).
        _consistency_problems = _verify_scope_consistency(pos_table_text, prof_table_text, _dropped_pos2, _dropped_prof2)
        if _consistency_problems:
            record_system_decision(candidate_id, level, "rapor_tutarlilik_ihlali",
                                   "İŞ 1 — devralma sonrası final tablo ile dropped listesi arasında tutarsızlık tespit edildi.",
                                   {"sorunlar": _consistency_problems})
        _still_dropped = len(_dropped_pos2) + len(_dropped_prof2)
        _total_crit_n2 = len(position_criteria or []) + len(PROFILE_CRITERIA)
        if _total_crit_n2 and (_still_dropped / _total_crit_n2) > 0.25:
            record_system_decision(candidate_id, level, "yuksek_dusme_orani_guncellendi",
                                   "GÖREV 1.4 — devralma sonrası düşme oranı hâlâ %25'in üzerinde; Puanlama Kapsamı güncellendi.",
                                   {"dusen_kriterler": _dropped_pos2 + _dropped_prof2, "toplam_kriter": _total_crit_n2, "dusen_sayisi": _still_dropped})
    except Exception as e:
        print(f"UYARI (append_reviewer_section puanlama kapsamı güncelleme c={candidate_id} L{level}): {type(e).__name__}: {e}")

    # İş emri GÖREV 5 EK — reviewer_contradiction_unresolved: müfettiş serbest metninde ana
    # rapordan ("...") ALINTILADIĞI bir iddiayı AÇIKÇA "desteklenmiyor/tutarlı değil/abartılı"
    # diye işaretlediyse, bu çelişki rapora GİRMEZ — ana metindeki (Güçlü Yönler) o cümle
    # ÇIKARILIR (ikinci değerlendirici bunu Ek Görüş'te yazdığı halde ana metin değişmeden
    # basılmasın diye — bu turun somut örneği).
    try:
        _gy_m = re.search(r'\*\*Güçlü Yönler:\*\*\s*\n([\s\S]*?)(?=\n\*\*[^\n]{2,60}:\*\*|\Z)', final_report)
        _gy_block = _gy_m.group(1) if _gy_m else ""
        _removed_contradictions = []
        if free_raw and _gy_block:
            _gy_sents = re.split(r'(?<=[.!?])\s+', _gy_block)
            for m in _STR_QUOTE_UNSUPPORTED_RE.finditer(free_raw):
                quoted = m.group(1).strip()
                if not quoted:
                    continue
                for i, s in enumerate(_gy_sents):
                    if s.strip() and _is_near_duplicate(quoted, [s], threshold=0.5):
                        _removed_contradictions.append(s.strip())
                        _gy_sents[i] = ""
                        break
            if _removed_contradictions:
                _new_gy_block = " ".join(s for s in _gy_sents if s.strip())
                final_report = final_report.replace(_gy_block, _new_gy_block, 1)
                record_system_decision(candidate_id, level, "reviewer_contradiction_unresolved_duzeltildi",
                                       "GÖREV 5 EK — ikinci değerlendirici Güçlü Yönler'deki bir iddianın transkriptle desteklenmediğini işaret etti; o cümle ana metinden çıkarıldı.",
                                       {"silinen_cumleler": _removed_contradictions})
    except Exception as e:
        print(f"UYARI (append_reviewer_section reviewer_contradiction taraması c={candidate_id}): {type(e).__name__}: {e}")

    diff_block = build_reviewer_diff_block(rv_scores, rv_gerekce, position_criteria or [], PROFILE_CRITERIA,
                                           pos_table_text, prof_table_text)

    # İkinci değerlendiricinin GENEL pozisyon/profil puanları — Genel Puan'a girer (madde 6),
    # birincilin KENDİ puanını DEĞİŞTİRMEZ (madde 21).
    reviewer_score_position = compute_reviewer_overall(position_criteria or [], pos_table_text, rv_scores, id_prefix="P") if position_criteria else None
    reviewer_score_profile = compute_reviewer_overall(PROFILE_CRITERIA, prof_table_text, rv_scores, id_prefix="K")

    # DOKUNULMAYACAKLAR — müfettişin KENDİ güven düzeyi ana rapora GİRMEZ, yalnız burada
    # (yönetici kaydı, system_decision) tutulur.
    _reviewer_confidence = parse_reviewer_confidence_level(scores_raw or notes_wo_confidence)
    if _reviewer_confidence:
        record_system_decision(candidate_id, level, "mufettis_kendi_guven_duzeyi",
                               "İkinci değerlendiricinin KENDİ değerlendirmesine duyduğu güven düzeyi (ana rapora girmez, yalnız yönetici kaydı).",
                               {"guven_duzeyi": _reviewer_confidence})

    # İŞ 6T — semantik notlar SADECE GÖRÜNTÜLEME: pos_table_text/prof_table_text/awarded/
    # evaluability/Genel Puan/recommendation'a KESİNLİKLE dokunulmadan, yalnız aşağıdaki
    # semantic_block değişkeni üzerinden Ek Görüş'e (varsa) eklenir.
    semantic_block = build_semantic_issue_block(rv_semantic, position_criteria or [], PROFILE_CRITERIA)

    # İŞ EMRİ — "EK GÖRÜŞ" MÜŞTERİ RAPORUNDAN KALDIRILACAK: free_raw (Claude'un birincil rapor
    # hakkındaki serbest iç eleştirisi — "birincil raporda şu hata var" tarzı modeller-arası iç
    # denetim metni) artık müşteri raporuna/PDF'ye YAZILMIYOR. has_view (free_raw var mı) hâlâ
    # HESAPLANIR ve system_decision (yönetici) kaydına düşer — yalnız CUSTOMER-FACING "Ek Görüş"
    # bölümüne eklenmiyor. Claude'un 12 KRITER_PUAN/KRITER_GEREKCE'si (diff_block/"İkinci
    # Değerlendirici Görüşü") ve özgüven notları bu değişiklikten ETKİLENMEDİ.
    # İŞ EMRİ (2. tur) — SEMANTİK İÇ DENETİM NOTLARI DA MÜŞTERİ RAPORUNDAN KALDIRILDI: semantic_block
    # (rv_semantic'ten üretilir) artık "Ek Görüş"e YAZILMIYOR — aynı sebep: "kanıt-kriter uyumsuzluğu
    # tespit edildi" tarzı modeller-arası bulgu, düzeltilmiş olsa bile müşteriye "rapordaki hata"
    # gibi görünüyordu. rv_semantic ÜRETİMİ/PARSE'I/return değeri DEĞİŞMEDİ — yalnız bu customer-
    # facing render noktasına eklenmesi durduruldu.
    if not diff_block and not confidence_text:
        record_system_decision(candidate_id, level, "ikinci_degerlendirici_atlandi",
                               "İkinci değerlendirici somut bir görüş/özgüven izlenimi bildirmedi ve kriter puanları birincille örtüşüyor — bölüm rapora eklenmedi.",
                               {"reviewer_status": status, "free_len": len(free_raw or ""), "has_view": has_view, "semantic_issue_count": len(rv_semantic or {})})
        final_report = final_report.replace(_REVIEWER_SLOT_MARK, "").strip()
        _save(final_report)
    else:
        block = _REVIEWER_HEAD + "\n\n"
        if diff_block:
            block += diff_block + "\n\n"
        # "Ek Görüş" — ARTIK YALNIZ özgüven izlenimi (free_raw serbest iç eleştiri VE semantic_block
        # iç denetim notları DAHİL DEĞİL — bkz. yukarıdaki notlar).
        ek_gorus_parts = []
        if confidence_text:
            ek_gorus_parts.append(confidence_text.strip())
        if ek_gorus_parts:
            block += "**Ek Görüş:**\n" + "\n\n".join(ek_gorus_parts) + "\n"
        block = scrub_forbidden_phrases(block.strip())
        final_report = final_report.replace(_REVIEWER_SLOT_MARK, block)
        if semantic_block:
            record_system_decision(candidate_id, level, "reviewer_semantik_not_tespit_edildi",
                                   "İkinci değerlendirici bir veya daha fazla kriterde semantik tutarlılık sorunu bildirdi (G/K/E/S, evaluability, puan, karar DEĞİŞMEDİ) — YALNIZ yönetici kaydı, müşteri raporuna/PDF'ye artık YAZILMIYOR.",
                                   {"semantic_issues": rv_semantic})
        _save(final_report)
        record_system_decision(candidate_id, level, "ikinci_degerlendirici_eklendi",
                               "İkinci değerlendirici görüşü nihai rapora eklendi; genel puana (varsa) katıldı.",
                               {"reviewer_status": status, "gorus_var": has_view,
                                "reviewer_score_position": reviewer_score_position, "reviewer_score_profile": reviewer_score_profile})

    # İş emri — RAPOR İÇERİK STANDARDI / A2 — KÖK NEDEN: Öneri Gerekçesi finalize_interview'da
    # YALNIZ 1. değerlendiriciyle DONDURULUYORDU; recompute_overall_decision (hemen altta) Genel
    # Puan'ı 2. değerlendiriciyle GÜNCELLEDİĞİNDE bu metin hiç yeniden üretilmiyordu (Puanlama
    # Kapsamı'nın A1'deki hatasıyla AYNI sınıf — bir metin dondurulur, veri sonradan değişir, metin
    # değişmez). Fix: Puanlama Kapsamı'nın kendi re-patch deseniyle (_PUANLAMA_KAPSAMI_RE) AYNI
    # yöntemle Öneri Gerekçesi de recompute_overall_decision'ın DÖNDÜRDÜĞÜ (yani DB'ye yazılanla
    # AYNI) değerlerle yeniden üretilip rapora patchlenir.
    # İŞ 6V-FIX — KÖK NEDEN (İş 6V teşhisi): recompute_overall_decision'ın döndürdüğü 3./4. eleman
    # ('_new_score_pos'/'_new_score_prof' adları YANILTICI) aslında DB satırındaki SAF PRIMARY
    # score_position/score_profile'dır — reviewer'dan HİÇ etkilenmez. Genel Puan (_new_genel_puan)
    # doğru şekilde primary+reviewer harmanlanmışken, Öneri Gerekçesi'ndeki Pozisyon/Profil alt
    # puanları YİNE PRIMARY kalıyordu (matematiksel tutarsızlık — ör. Genel:63 ama Pozisyon:64/
    # Profil:66). Fix: _final_component_score() ile AYNI (primary+reviewer varsa ortalama, yoksa
    # primary) mantık uygulanır — DB'ye YAZILMAZ, yalnız BU render için kullanılır.
    try:
        _rc = recompute_overall_decision(candidate_id, level, reviewer_score_position, reviewer_score_profile)
        if _rc:
            _new_genel_puan, _new_recommendation, _primary_score_pos, _primary_score_prof = _rc
            # İŞ EMRİ — madde 4: _final_component_score AYNI canonical helper (recompute_overall_
            # decision'ın DB'ye PERSIST ettiğiyle birebir aynı hesap) — burada YALNIZ bu render için
            # tekrar çağrılıyor (DB round-trip'ten kaçınmak için), YENİ/farklı bir mantık DEĞİL.
            _final_score_pos = _final_component_score(_primary_score_pos, reviewer_score_position)
            _final_score_prof = _final_component_score(_primary_score_prof, reviewer_score_profile)
            _new_oneri_text = render_oneri_gerekcesi(_new_recommendation, _new_genel_puan, _primary_score_pos, _primary_score_prof,
                                                     reviewer_score_position, reviewer_score_profile,
                                                     _final_score_pos, _final_score_prof)
            if _new_oneri_text and _ONERI_GEREKCESI_HEAD in final_report:
                final_report = _ONERI_GEREKCESI_RE.sub(_ONERI_GEREKCESI_HEAD + "\n" + _new_oneri_text, final_report, count=1)
                _save(final_report)
    except Exception as e:
        print(f"UYARI (append_reviewer_section öneri gerekçesi güncelleme c={candidate_id} L{level}): {type(e).__name__}: {e}")

    # İŞ 6X-1 — reviewer bulgularını çağırana (run_deferred_finish_job → Final Report Quality Gate)
    # AKTAR. YENİ bir AI/DB round-trip GEREKMİYOR — bu üç sözlük zaten yukarıda hesaplandı.
    return {"rv_scores": rv_scores, "rv_gerekce": rv_gerekce, "rv_semantic": rv_semantic}

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

# NOT (2026-09 rapor yeniden tasarımı): build_independent_profile_prompt / run_independent_profile_call /
# splice_profile_region KALDIRILDI — bkz. run_deferred_finish_job içindeki ilgili not (yeni
# ===BAŞLIK=== mimarisinde Pozisyon Yetkinlikleri / Kişisel ve Bilişsel Profil zaten parse
# seviyesinde izole ediliyor, ayrı bir LLM çağrısına gerek kalmadı).

# GÖREV 1.2 — reviewer_flagged_criteria / apply_reviewer_score_revision / annotate_revised_criteria_prose
# ve yardımcıları (_match_strength, _VALUE_JUDGMENT_RE, _ANNOTATE_* , _criterion_fully_mentioned)
# TAMAMEN SİLİNDİ. İkinci model artık puana/metne müdahale etmiyor (bkz. append_reviewer_section).

# ═══════════════════════════════════════════════════════════════════════════════════════════
# İŞ EMRİ — KRİTER GEREKÇESİ: YAPISAL ÜRETİM + DETERMİNİSTİK DOĞRULAMA (2026-09)
# ═══════════════════════════════════════════════════════════════════════════════════════════
# Önceki tur (RAPOR İÇERİK KALİTESİ) klişe/gerekçe sorununu PROMPT talimatı + LOG-ONLY tespitle
# çözmeye çalıştı — YETERSİZ kaldı: paraphrase ile regex atlatıldı, "Ancak" kalıbı 12/12'ye çıktı
# (önceki turdaki 10/12'den DAHA KÖTÜ), aynı kanıt iki kritere kopyalandı, Yönetici Özeti 95
# kelimeye küçüldü (hedef 150-250, önceki tur 107) VE — en ciddisi — HİÇ SORULMAMIŞ bir konu
# ("Vergi ve Mevzuat") için EKSİK uydurulup ondan bir takip sorusu türetildi. Kök neden: modelin
# SERBEST METİN yazdığı TEK bir hücreye "bunu yazma" demek işe yaramıyor — model istemeden ya da
# bilerek talimatın etrafından dolaşabiliyor. Çözüm burada YAPISAL + DENETİMLİ:
#   GÖREV 1 — model artık serbest cümle değil, G(österdiği)/K(anıt)/E(ksik)/S(oru damgası) diye
#             4 AYRI ALAN üretir (bkz. _CRIT_EVIDENCE_HINT / _STRUCTURED_EVIDENCE_FORMAT_INSTRUCTIONS,
#             yukarıda); NİHAİ cümleyi bu alanlardan KOD (render_criterion_rationale) kurar — EN AZ
#             3 farklı şablon rotasyonla, sabit "X. Ancak Y." iskeleti YOK.
#   GÖREV 2 — validate_criterion_fields DETERMİNİSTİK bir KAPI (log değil): geçemeyen içerik rapora
#             HİÇ girmez; apply_structured_rationale_gate SADECE o kriter için max 3 deneme hedefli
#             yeniden üretim tetikler (regenerate_criterion_fields); 3 denemeden sonra hâlâ geçemezse
#             kriter "Değerlendirilemedi (sistem)" sayılır (payda dışına alınır, puan yeniden
#             normalize edilir) — YEDEK ŞABLON GEREKÇE ÜRETİLMEZ ("kötü gerekçe basmaktansa hiç basma").
#   GÖREV 3 — her EKSİK/RİSK/takip-sorusu iddiası check_timestamp_grounded ile transkriptte GERÇEKTEN
#             var olan bir mülakatçı sorusuna bağlanmak ZORUNDA; bağlanamıyorsa iddia SİLİNİR.
#   GÖREV 4 — Yönetici Özeti AYRI, bağımsız bir uzunluk doğrulayıcı (regenerate_yonetici_ozeti) —
#             tek deneme, sonra olduğu gibi basılır + loglanır (otomatik kısaltma/uzatma YOK).
#   GÖREV 5 (EK) — puan artık cevabın KRİTERİ KARŞILAMA DERECESİNE göre: alan dışı/devretme beyanı
#             varken yüksek puan verilmişse (out_of_scope_high_score) DETERMİNİSTİK olarak tavanın
#             %25'ine sabitlenir (LLM'e "puanını düşür" demek GÜVENİLMEZ — 5.2'nin bizzat kanıtladığı
#             gibi model bunu kendiliğinden yapmadı); bulgu Gelişim Alanları'na RİSK olarak ZORUNLU
#             yansıtılır (yoksa sistem kendisi ekler — sessizce geçilemez).

# ---- Zaman damgası yardımcıları (GÖREV 2+3 — KANIT/SORU_DAMGASI'nın transkriptte GERÇEKTEN var
# olup olmadığını doğrulamak için ortak): ----
_TS_RE = re.compile(r"\[?(\d{1,3}):([0-5]\d)\]?")

def _extract_timestamp(s: str) -> Optional[str]:
    m = _TS_RE.search(s or "")
    return f"{m.group(1)}:{m.group(2)}" if m else None

def check_timestamp_grounded(ts: str, transcript_view: list, role: Optional[str] = None, tolerance_s: int = 8) -> bool:
    """GÖREV 2 (evidence_timestamp_invalid) + GÖREV 3 (unsourced_eksik/SORU_DAMGASI) — verilen
    [mm:ss] damgasının transkriptte GERÇEKTEN var olan bir satıra karşılık gelip gelmediğini
    kontrol eder. role='mulakatci' verilirse YALNIZ mülakatçı satırları aranır (SORU_DAMGASI ve
    takip sorusu 'dayanak' damgası için — GÖREV 3: eksiklik/takip-sorusu iddiası GERÇEKTEN sorulmuş
    bir soruya dayanmalı, ADAY'ın kendi cümlesine değil). Tolerans: model/insan yuvarlaması birkaç
    saniye kayabilir; ±tolerance_s içindeki en yakın satır kabul edilir. DAR TUTULUR (8sn): geniş
    bir tolerans (ör. 45sn), soru-cevap turları sık olduğunda ADAY'ın kendi cevabını YANLIŞLIKLA
    'mülakatçı sorusu' gibi doğrulayabiliyordu (sentetik testte yakalandı) — GÖREV 3'ün amacı
    TAM OLARAK bunu önlemek olduğu için tolerans komşu konuşma turunu KAPSAMAYACAK kadar dar olmalı."""
    if not ts:
        return False
    target_s = _TS_RE.search(ts)
    if not target_s:
        return False
    target = int(target_s.group(1)) * 60 + int(target_s.group(2))
    for row in (transcript_view or []):
        if role and row.get("role") != role:
            continue
        if row.get("role") == "baslik":
            continue
        em = row.get("elapsed_ms")
        if em is None:
            continue
        if abs(em // 1000 - target) <= tolerance_s:
            return True
    return False

# İş emri — KANIT BÜTÜNLÜĞÜ VE İKİNCİ DEĞERLENDİRİCİ ÇIKTISI / KALEM 2 — KÖK NEDEN:
# check_timestamp_grounded yalnız damganın role'e YAKIN olup olmadığına bakıyor, alıntılanan
# METNİN o role'e GERÇEKTEN ait olup olmadığına DEĞİL. Gerçek örnek: bir kriterin K alanı
# mülakatçının SORUSUNUN TAMAMINI alıntıladı ("...hangi geçici kontrolü koyarsınız?") ama damga
# ([4:21]) yakınında BİR aday satırı da olduğu için (proximite) doğrulama geçti — alıntının
# İÇERİĞİ hiç kontrol edilmedi. _CRIT_EVIDENCE_HINT formatı K'nin "kısa alıntı/özet" olmasına
# izin verir (her zaman tırnaklı birebir alıntı ZORUNLU değildir) — bu yüzden alıntı YALNIZ
# tırnak işareti VARSA doğrulanır (yoksa geriye uyum: yalnız proximite).
_QUOTE_RE = re.compile(r'["“]([^"”]{5,300})["”]')

# İş emri — KAPI EŞİĞİ VE SON TUTARLILIK / MADDE 1 — KÖK NEDEN: yukarıdaki kapı BİREBİR alt-string
# (_verbatim_in) şart koşuyordu — model çoğu zaman PARAFRAZ ediyor/kısaltıyor, birebir alıntı
# ZATEN K formatının ZORUNLU tuttuğu bir şey DEĞİL (yalnız "kısa alıntı/özet" isteniyor). Sonuç:
# gerçek aday cevabına dayanan ama birebir alıntılanmamış kriterler YANLIŞLIKLA düşüyordu (5 örnek,
# hepsi transkriptte aday cevabı olan konular). Kapının KORUMASI GEREKEN tek durum — alıntı
# MÜLAKATÇI satırıyla örtüşüp ADAY satırıyla örtüşmemesi — değişmedi; yalnız "birebir alıntı
# ZORUNLU" şartı "anlamlı kısmi örtüşme YETERLİ" ile gevşetildi.
_QUOTE_OVERLAP_MIN_WORDS = 2  # en az 2 anlamlı ortak kelime (Türkçe ek-toleranslı, bkz. _stem_overlap) = kısmi örtüşme

def _quote_overlap_words(quote: str, line_text: str) -> int:
    return _stem_overlap(_q_keywords(quote), _q_keywords(line_text or ""))

# İŞ 2 — MÜLAKATÇI CÜMLESİNİN ADAY KANITI OLARAK GEÇMESİNİ ENGELLE (Problem B / "Gültuğ AYDIN"
# vakası): field_text tırnaksız (saf parafraz) olduğunda eski davranış YALNIZ proximite'ye
# bakıyordu — field'ın kendi metni AÇIKÇA "Mülakatçı:"/"Interviewer:" gibi bir konuşmacı etiketiyle
# BAŞKA rolün sözünü anlattığını söylese bile bu hiç okunmuyordu. Aşağıdaki iki regex yalnızca
# AÇIK, kolonla biten GERÇEK bir konuşmacı etiketini yakalar — "Mülakatçının sorusuna..." gibi
# normal anlatı cümlelerindeki çekimli kullanımı (kolon YOK) YAKALAMAZ; timestamp toleransına,
# tırnaklı-alıntı örtüşme kurallarına DOKUNMAZ (yalnız tırnaksız/saf-özet dalına ek bir kapı).
_INTERVIEWER_LABEL_RE = re.compile(r'\b(?:M[üu]lakatç[ıi]|Interviewer|G[öo]r[üu]şmeci)\s*:', re.IGNORECASE)
_CANDIDATE_LABEL_RE = re.compile(r'\b(?:Aday|Candidate|Kat[ıi]l[ıi]mc[ıi])\s*:', re.IGNORECASE)

def _field_claims_opposite_speaker(field_text: str, role: str) -> bool:
    """İŞ 2 — field_text kendi metninde AÇIKÇA karşıt role'ün konuşmacı etiketini taşıyor mu (ör.
    role='aday' istenirken metin 'Mülakatçı: ...' diye başlıyor/içeriyor). Yalnız kolonla biten
    GERÇEK bir etiketi yakalar; kelimenin çekimli/anlatı içinde geçmesini (kolon yok) YAKALAMAZ."""
    if not field_text:
        return False
    if role == "aday":
        return bool(_INTERVIEWER_LABEL_RE.search(field_text))
    if role == "mulakatci":
        return bool(_CANDIDATE_LABEL_RE.search(field_text))
    return False

def _timestamp_field_grounded(field_text: str, transcript_view: list, role: str, tolerance_s: int = 8) -> bool:
    """G/K/E/S alanındaki [mm:ss] damgasının GERÇEK bir <role> satırına yakın olup olmadığını VE
    (alanda tırnaklı bir alıntı varsa) o alıntının o role'e GERÇEKTEN ait olduğunu doğrular.
    MADDE 1 — üç kademeli karar: (1) birebir alt-string VEYA anlamlı kısmi kelime örtüşmesi
    <role> satırıyla varsa -> GEÇERLİ (parafraz/kısaltma tolere edilir). (2) örtüşme YOKSA ama
    KARŞIT role'ün (ör. K için mülakatçı) yakın satırıyla örtüşme VARSA -> GEÇERSİZ (kapının
    koruduğu asıl durum — yanlış konuşmacının sözü kanıt sayılamaz). (3) QUOTE YOKSA (saf
    parafraz/özet, K formatının izin verdiği hâl) ve hiçbir tarafla örtüşmüyorsa -> yalnız
    proximite (eski davranış, geriye uyum) — İŞ 2: BU dalda field_text AÇIK bir karşıt-role
    etiketi taşıyorsa proximite artık TEK BAŞINA yeterli SAYILMAZ (bkz. _field_claims_opposite_speaker).
    İŞ 6U-FIX — KÖK NEDEN (İş 6U teşhisi): QUOTE VARKEN de (yalnız YOKKEN değil) hiçbir tarafla
    örtüşme bulunamadığında kod yanlışlıkla YUKARIDAKİ (3) numaralı 'quote yok, proximite yeterli'
    dalıyla AYNI `return True` sonucuna düşüyordu — gerçek production örneği: K, gerçek bir aday
    timestamp'ına ([2:40]) yakındı ama tırnaklı alıntı o timestamp'ta SÖYLENMEMİŞTİ (asıl cümle
    32 saniye sonra, [3:12]'deydi) ve validator bunu PASS ediyordu. Düzeltme: QUOTE VARSA ve hiçbir
    tarafla (role/opposite) anlamlı örtüşme yoksa artık FAIL (False) — bu tolerans SADECE quote'suz
    (saf özet) dala özgüdür, quote'lu dala hiç UYGULANMAMALIYDI. ±8sn tolerans, role='aday', opposite-
    role guard, overlap eşiği (_QUOTE_OVERLAP_MIN_WORDS), violation adı, retry mekanizması/sayısı,
    model/temperature/max_tokens DEĞİŞMEDİ — yalnız bu TEK dalın sonucu düzeltildi."""
    ts = _extract_timestamp(field_text)
    if not ts:
        return False
    m_ts = _TS_RE.search(ts)
    if not m_ts:
        return False
    target = int(m_ts.group(1)) * 60 + int(m_ts.group(2))
    near_rows = [row for row in (transcript_view or [])
                if row.get("role") == role and row.get("elapsed_ms") is not None
                and abs(row["elapsed_ms"] // 1000 - target) <= tolerance_s]
    if not near_rows:
        return False
    qm = _QUOTE_RE.search(field_text or "")
    if not qm:
        if _field_claims_opposite_speaker(field_text, role):
            return False  # İŞ 2 — açık karşıt-role etiketi (ör. "Mülakatçı: ...") — parafraz DEĞİL
        return True  # alıntı yok (yalnız özet) — proximite yeterli, geriye uyum
    quote = qm.group(1)
    if any(_verbatim_in(quote, row.get("text") or "") for row in near_rows):
        return True
    role_overlap = max((_quote_overlap_words(quote, row.get("text") or "") for row in near_rows), default=0)
    if role_overlap >= _QUOTE_OVERLAP_MIN_WORDS:
        return True
    opposite_role = "mulakatci" if role == "aday" else "aday"
    opp_rows = [row for row in (transcript_view or [])
               if row.get("role") == opposite_role and row.get("elapsed_ms") is not None
               and abs(row["elapsed_ms"] // 1000 - target) <= tolerance_s]
    opp_overlap = max((_quote_overlap_words(quote, row.get("text") or "") for row in opp_rows), default=0)
    if opp_overlap >= _QUOTE_OVERLAP_MIN_WORDS and opp_overlap > role_overlap:
        return False  # asıl korunan durum: alıntı KARŞIT taraftan, kanıt geçersiz
    # İŞ 6U-FIX — quote VARDI (yukarıda qm eşleşti) ama ne <role> ne de karşıt role'ün ±tolerance_s
    # penceresindeki HİÇBİR satırıyla anlamlı örtüşme bulunamadı: alıntı bu timestamp'ta SÖYLENMEMİŞ
    # demektir — quote'suz (saf özet) dalın 'proximite yeterli' toleransı BURAYA UYGULANMAZ.
    return False

# ---- GÖREV 1 — yapısal hücre ayrıştırma + render ----
def parse_structured_evidence_cell(cell_text: str) -> Optional[dict]:
    """'Kanıt ve Analiz' hücresindeki YAPISAL alanları ayrıştırır: G (gösterdiği, zorunlu), K
    (kanıt, zorunlu), E (eksik, isteğe bağlı), S (soru damgası, E doluysa zorunlu). Format:
    'G: ... ~~ K: ... ~~ E: ... ~~ S: ...' (bkz. _CRIT_EVIDENCE_HINT). Ayrıştırılamazsa (G veya K
    yoksa, ya da '~~' hiç yoksa — eski/serbest metin) None döner; çağıran bunu 'structure_invalid'
    kabul eder (GÖREV 2)."""
    if not cell_text or "~~" not in cell_text:
        return None
    fields = {"g": "", "k": "", "e": "", "s": ""}
    for seg in cell_text.split("~~"):
        seg = seg.strip()
        m = re.match(r"^([GKES])\s*:\s*(.*)$", seg, re.IGNORECASE | re.DOTALL)
        if not m:
            continue
        key = {"G": "g", "K": "k", "E": "e", "S": "s"}[m.group(1).upper()]
        fields[key] = m.group(2).strip()
    if not fields["g"] or not fields["k"]:
        return None
    return fields

# GÖREV 2.2 — geçiş bağlacı ailesi. "Bununla birlikte" YALNIZ render_criterion_rationale'ın 3
# şablonundan BİRİNDE (idx 1), rotasyonla, kullanılır — bu yüzden E dolu kriterlerin EN FAZLA
# 1/3'ünde görünür (tasarım gereği; transition_overuse yine de ÇAPRAZ KRİTER bir GÜVENLİK AĞI
# olarak ayrıca kontrol eder — bkz. apply_structured_rationale_gate). Modelin KENDİ G/E metninin
# İÇİNDE bu bağlaçlardan birini kullanması (forbidden_transition_found) AYRI ve HER ZAMAN yasaktır.
_TRANSITION_WORD_RE = re.compile(r"\b(ancak|fakat|ne var ki|bununla birlikte)\b", re.IGNORECASE)

def render_criterion_rationale(fields: dict, template_idx: int) -> str:
    """GÖREV 1 — yapısal alanları (G/K/E) NİHAİ cümleye döker. EN AZ 3 farklı şablon, kriter
    SIRASINA göre rotasyonla seçilir (sabit 'X. Ancak Y.' iskeleti YOK — iki şablonda HİÇ geçiş
    bağlacı yok, yalnız 1 şablonda 'Bununla birlikte' var). EKSİK boşsa hiçbir geçiş/olumsuzlama
    cümlesi EKLENMEZ — yalnızca G+K anlatılır (iş emri: 'EKSİK boşsa sadece GÖSTERDİĞİ+KANIT
    render edilir')."""
    g = (fields.get("g") or "").strip().rstrip(".")
    k = (fields.get("k") or "").strip()
    e = (fields.get("e") or "").strip().rstrip(".")
    idx = template_idx % 3
    if idx == 0:
        base = f"{g} ({k})."
        return base + (f" Gelişime açık yön: {e}." if e else "")
    if idx == 1:
        base = f"{k} üzerinden görülüyor ki {_tr_lower_first(g)}."
        return base + (f" Bununla birlikte {_tr_lower_first(e)}." if e else "")
    base = f"{g}. Kanıt: {k}."
    return base + (f" Eksik kalan yön: {e}." if e else "")

# İş emri — KANIT BÜTÜNLÜĞÜ VE İKİNCİ DEĞERLENDİRİCİ ÇIKTISI / KALEM 1 — G alanı bir OLUMSUZLUK/
# eksik belirtmeden (E boş), AÇIK ve KOŞULSUZ bir olumlu hüküm cümlesiyle ("...olduğunu
# belirtmiştir/göstermiştir/sergilemiştir/ortaya koymuştur/kanıtlamıştır/ifade etmiştir") BİTİYORSA
# ve buna rağmen tavanın ÇOK altında (<%34) bir puan verilmişse, metin ile puan ZIT yöne işaret
# eder. Kasıtlı DAR (yalnız G'nin doğrudan bu kalıpla BİTTİĞİ, ortasında geçen serbest övgü
# DEĞİL) — G format gereği zaten çoğu zaman "adayın ne yapabildiği" gibi olumlu-görünümlü bir
# cümle olduğundan (bu formatın doğası), geniş bir "olumlu kelime" taraması aşırı yanlış-pozitif
# üretirdi; yalnız KOŞULSUZ, hedge'siz bir KAPANIŞ hükmü + boş E + belirgin düşük puan üçlüsü
# yakalanır.
_STRONG_POSITIVE_CLOSING_RE = re.compile(
    r"(?:oldu[ğg]unu|yeteneğine sahip oldu[ğg]unu|yatk[ıi]n oldu[ğg]unu|yeterli oldu[ğg]unu|"
    r"ba[şs]ar[ıi]l[ıi] oldu[ğg]unu)\s*"
    r"(?:belirtmi[şs]tir|g[öo]stermi[şs]tir|sergilemi[şs]tir|ortaya koymu[şs]tur|kan[ıi]tlam[ıi][şs]t[ıi]r|"
    r"ifade etmi[şs]tir)\.?\s*$",
    re.IGNORECASE)
_LOW_SCORE_DIRECTION_RATIO = 0.34  # SCORING_RUBRIC'in DÜŞÜK bant eşiğiyle tutarlı

def _score_direction_conflict(g: str, e: str, cap, awarded) -> bool:
    if not g or (e or "").strip() or cap is None or awarded is None or cap <= 0:
        return False
    if (awarded / cap) >= _LOW_SCORE_DIRECTION_RATIO:
        return False
    return bool(_STRONG_POSITIVE_CLOSING_RE.search(g.strip()))

# ---- GÖREV 2 — DOĞRULAYICI (validator): LOG değil KAPI ----
# İŞ 6M — DUPLICATE_CLAIM'İ GÜVENLİ DETERMİNİSTİK SINIRA ÇEK (system-wide, hiçbir aday/pozisyon/
# kriter/timestamp/transcript içeriğine özel değil). Eski politika (_is_near_duplicate, SequenceMatcher
# >= 0.6) ampirik olarak hem yanlış-pozitif (farklı olay + ortak rapor şablonu → duplicate sayılıyordu)
# hem yanlış-negatif (aynı iddia + farklı paraphrase → kaçıyordu) üretiyordu (bkz. İş 6L teşhisi).
# YENİ politika: "aynı KANITIN farklı kriterlerde kullanılması YASAK DEĞİL; yasak olan aynı G
# metninin BAŞKA kriterde copy/paste seviyesinde TEKRARI." Bu yüzden artık yalnız NORMALIZE EDİLMİŞ
# TAM eşitliğe bakılıyor — stemming/synonym/fuzzy/semantic/LLM KULLANILMIYOR (kasıtlı, bu katman
# semantik karar vermiyor). _is_near_duplicate() (SequenceMatcher tabanlı) GLOBAL olarak SİLİNMEDİ —
# ses/mimik gözlem tekrarı tespitinde (main.py, build_modality_evidence_block civarı) hâlâ kullanılıyor,
# YALNIZ duplicate_claim kararı bu iki yeni fonksiyona taşındı.
def _normalize_claim_text(text: str) -> str:
    """İŞ 6M — yalnız BİÇİMSEL normalizasyon: casefold + baş/son boşluk + çoklu boşluk tekilleştirme
    + sondaki noktalama. ANLAMI DEĞİŞTİRMEZ (stemming/synonym YOK)."""
    t = (text or "").strip().casefold()
    t = re.sub(r"\s+", " ", t)
    t = t.strip(" .,;:!?")
    return t

def _is_exact_duplicate_claim(text: str, existing: list) -> bool:
    """İŞ 6M — duplicate_claim İÇİN TEK doğruluk kaynağı: normalize edilmiş TAM eşitlik. Case/
    fazla-boşluk/son-noktalama farkını tolere eder; bunun DIŞINDA hiçbir benzerlik/fuzzy/semantic
    karar YOKTUR — aynı olay farklı yetkinlik yorumuyla kullanılmışsa veya farklı olaylar ortak bir
    rapor şablonu paylaşıyorsa, metin BİREBİR aynı olmadığı sürece duplicate SAYILMAZ."""
    t = _normalize_claim_text(text)
    if not t:
        return False
    return any(t == _normalize_claim_text(e) for e in existing)

def validate_criterion_fields(fields: Optional[dict], cap: int, awarded: Optional[int], transcript_view: list,
                              prior_claims: list) -> list:
    """Bir kriterin YAPISAL alanlarını (G/K/E/S) denetler. Dönüş: ihlal kodları listesi (boş liste
    = GEÇTİ). Kodlar: structure_invalid, banned_phrase_found, forbidden_transition_found,
    duplicate_claim, evidence_timestamp_invalid, full_score_has_eksik, unsourced_eksik,
    out_of_scope_high_score (GÖREV 5 EK). transition_overuse BURADA YOK — bu ÇAPRAZ KRİTER bir
    kontrol, apply_structured_rationale_gate'te AYRICA yapılır (tek kriterin kendi metnine bakan
    forbidden_transition_found'dan FARKLI: 12/12 kriterin HER BİRİ 'temiz' görünse bile RAPOR
    GENELİNDE oran aşılabilir — bu yüzden ayrı, rapor-seviyesi bir kontrol şart)."""
    violations = []
    if not fields:
        return ["structure_invalid"]
    g, k, e, s = fields.get("g", ""), fields.get("k", ""), fields.get("e", ""), fields.get("s", "")
    if not g or not k:
        return ["structure_invalid"]
    combined = f"{g} {e}"
    if banned_phrase_hits(combined):
        violations.append("banned_phrase_found")
    if _TRANSITION_WORD_RE.search(g) or _TRANSITION_WORD_RE.search(e):
        violations.append("forbidden_transition_found")
    # İş emri — RAPOR İÇERİK STANDARDI / B2 (KALEM GERİYE UYUM) + KANIT BÜTÜNLÜĞÜ / KALEM 2 — KÖK
    # NEDEN: role= verilmiyordu (B2 fix'i role="aday" ekledi) AMA yalnız PROXİMİTE kontrol ediyordu
    # — alıntılanan METNİN o role'e GERÇEKTEN ait olduğu hiç doğrulanmıyordu. Gerçek örnek: K alanı
    # mülakatçının SORUSUNUN TAMAMINI alıntıladı, damga yakınında BİR aday satırı da olduğu için
    # proximite geçti. _timestamp_field_grounded artık (tırnaklı alıntı varsa) alıntının o role'ün
    # GERÇEK bir satırında birebir geçtiğini de doğruluyor (bkz. yukarıdaki tanım/gerekçe).
    k_ts = _extract_timestamp(k)
    if not k_ts or not _timestamp_field_grounded(k, transcript_view, role="aday"):
        violations.append("evidence_timestamp_invalid")
    if e:
        if cap and awarded is not None and cap > 0 and (awarded / cap) >= 0.85:
            violations.append("full_score_has_eksik")
        s_ts = _extract_timestamp(s)
        if not s_ts or not _timestamp_field_grounded(s, transcript_view, role="mulakatci"):
            violations.append("unsourced_eksik")
    # KALEM 2 (devam) — G/E, K/S gibi [mm:ss] taşıması BEKLENMEZ (_CRIT_EVIDENCE_HINT) ama model
    # bazen yine de sızdırıyor; sızdırdıysa AYNI kontrolden geçer (tek kapı — iş emri: "Tek bir
    # kapı kalmayacak"). evidence_timestamp_invalid'i TEKRAR eklemez (zaten varsa).
    if "evidence_timestamp_invalid" not in violations:
        for _extra in (g, e):
            if _extra and _extract_timestamp(_extra) and not _timestamp_field_grounded(_extra, transcript_view, role="aday"):
                violations.append("evidence_timestamp_invalid")
                break
    if _is_exact_duplicate_claim(g, prior_claims):
        violations.append("duplicate_claim")
    # İş emri — KANIT BÜTÜNLÜĞÜ VE İKİNCİ DEĞERLENDİRİCİ ÇIKTISI / KALEM 1 — G AÇIK, KOŞULSUZ bir
    # olumlu hüküm cümlesiyle bitiyor (ör. "...yatkın olduğunu belirtmiştir") VE E (eksik) BOŞ VE
    # puan tavanın ÇOK altındaysa (<%34, DÜŞÜK bant eşiği) — okuyucu puanın neden düşük olduğunu
    # METİNDEN anlayamaz (gerçek örnek: ikinci değerlendirici BAĞIMSIZ olarak aynı çelişkiyi
    # yakaladı: "işbirliğine aktif katkı örneği yok... bağımsızlık eksikliğine işaret ediyor").
    # Yalnız YÖN'e bakar, puanı DEĞİŞTİRMEZ (iş emrinin açık notu) — kriter retry'a girer,
    # düzelmezse 'Değerlendirilemedi (sistem)' sayılır (puan UYDURMA anlatıyla KORUNMAZ).
    if _score_direction_conflict(g, e, cap, awarded):
        violations.append("score_direction_conflict")
    # GÖREV 5 EK — alan dışı/devretme beyanı varken yüksek puan (tavanın >%25'i) verilmişse: bu
    # DETERMİNİSTİK bir ihlal (LLM'e "puanı düşür" demek GÜVENİLMEZ, bkz. apply_structured_rationale_gate
    # içindeki sabit %25 kelepçesi — burada yalnız TESPİT edilir, düzeltme çağıran tarafta).
    if (_OUT_OF_SCOPE_RE.search(k) or _DELEGATION_RE.search(k) or _OUT_OF_SCOPE_RE.search(g) or _DELEGATION_RE.search(g)):
        if cap and awarded is not None and cap > 0 and (awarded / cap) > 0.25:
            violations.append("out_of_scope_high_score")
    return violations

_VIOLATION_TR = {
    "structure_invalid": "Alanlar (G/K/E/S) doğru biçimde ayrıştırılamadı — biçimi TAM UY: 'G: ... ~~ K: ... ~~ E: ... ~~ S: ...'.",
    "banned_phrase_found": "Yasaklı klişe kalıp kullanıldı (ör. 'daha fazla ... gerekmektedir/sunmamıştır', '... beklenmiştir').",
    "forbidden_transition_found": "G veya E alanı İÇİNDE 'ancak/fakat/ne var ki' bağlacı kullanıldı — YASAK, bu bağlaçları rapor derleyici (kod) ekler, sen ASLA ekleme.",
    "evidence_timestamp_invalid": "KANIT alanındaki [mm:ss] damgası transkriptte GERÇEKTEN yok, mülakatçının kendi sözüne ait, YA DA tırnaklı alıntı o andaki ADAY sözüyle BİREBİR UYUŞMUYOR (ör. mülakatçının sorusunu alıntılayıp yakın bir aday anına damga atmak GEÇERSİZDİR) — SADECE ADAYIN GERÇEKTEN söylediği bir cümleyi, doğru damgasıyla alıntıla.",
    "full_score_has_eksik": "Bu kriter tavanın %85+'ini almış ama EKSİK doldurulmuş — tutarsız: EKSİK'i BOŞ bırak (gerçekten tam performans varsa).",
    "unsourced_eksik": "EKSİK doldurulmuş ama SORU_DAMGASI mülakatçının GERÇEKTEN sorduğu bir soruya denk gelmiyor — ya EKSİK'i GERÇEK bir mülakatçı sorusuna dayandır (gerçek [mm:ss] ver) ya da EKSİK'i BOŞ bırak.",
    "duplicate_claim": "GÖSTERDİĞİ alanı başka bir kriterde ZATEN kullanılan kanıtla neredeyse AYNI — bu kriter için FARKLI, bağımsız bir kanıt/gözlem yaz.",
    "out_of_scope_high_score": "Adayın cevabı ALAN DIŞI/DEVRETME beyanı içeriyor (örn. 'benim alanım değil' / 'yöneticime sorarım') — G/K alanların bunu AÇIKÇA yansıtsın (puanı SEN değiştiremezsin, sistem düzeltir).",
    "score_direction_conflict": "G alanı KOŞULSUZ olumlu bir hükümle bitiyor ('...olduğunu belirtmiştir/göstermiştir' vb.) ama puan tavanın ÇOK altında — bu ÇELİŞKİLİ. E alanına GERÇEK bir eksik/zayıflık yaz (neden düşük olduğunu açıkla) YA DA G'yi puanla TUTARLI, hedge'li bir cümleye çevir (ör. 'X konusunda sınırlı bir gözlem sundu').",
}

# İş emri — VALIDATOR KALİBRASYONU / GÖREV 1.2 — retry'da modele YALNIZ ihlal ADI vermek yetersiz
# kaldı ("banned_phrase_found" demek modelin hangi ifadeyi/nasıl düzelteceğini söylemiyor — kanıtlı
# örnek: Murat AYZİT raporunda kanıtlı kriterler (İnisiyatif, Analitik) 3 denemede de düşmüştü).
# Bu fonksiyon HER ihlal için SOMUT (hangi alan bozuk, ne bekleniyor, hangi transkript anına bak)
# talimat üretir — mümkün olduğunca gerçek eşleşen metni/damgayı ALINTILAYARAK.
def _find_relevant_transcript_lines(cname: str, transcript_view: list, role: Optional[str] = None, max_n: int = 5) -> list:
    """Kriter adının anahtar kelimeleriyle (>=4 harf) örtüşen transkript satırlarını, en çok
    örtüşenden başlayarak döner — '[mm:ss] Rol: metin' biçiminde. role verilirse yalnız o rol."""
    kws = [w for w in _norm_name(cname).split() if len(w) >= 4]
    if not kws:
        return []
    hits = []
    for row in (transcript_view or []):
        r = row.get("role")
        if r not in ("aday", "mulakatci") or (role and r != role):
            continue
        norm = _norm_name(row.get("text") or "")
        sc = sum(1 for w in kws if w in norm)
        if sc > 0:
            who = "Aday" if r == "aday" else "Mülakatçı"
            hits.append((sc, row.get("ts") or "", who, (row.get("text") or "")[:160]))
    hits.sort(key=lambda h: -h[0])
    return [f"[{ts}] {who}: {text}" for _, ts, who, text in hits[:max_n]]

def _build_violation_detail_lines(violations: list, cname: str, fields: Optional[dict], transcript_view: list,
                                  accepted_claims: list) -> list:
    """GÖREV 1.2 — her ihlal için SOMUT düzeltme talimatı (hangi alan, ne bekleniyor, hangi
    transkript anı) — yalnızca ihlal kodu/genel açıklama YETERSİZ kaldığı için (bkz. yukarıdaki not)."""
    g, e = (fields or {}).get("g", ""), (fields or {}).get("e", "")
    lines = []
    for v in violations:
        if v == "banned_phrase_found":
            hits = banned_phrase_hits(f"{g} {e}")
            quoted = "; ".join(f"'{h}'" for h in hits[:3]) if hits else "(klişe ifade)"
            lines.append(f"- banned_phrase_found: G veya E alanında ŞU klişeyi yazdın: {quoted}. SİL — yerine adayın TAM OLARAK ne yaptığını/söylediğini somut anlat (klişe kelime YOK).")
        elif v == "evidence_timestamp_invalid":
            # İŞ 6K — hint role'ü, validator'ın K için kabul ettiği role ("aday") ile UYUMLU hale
            # getirildi. Önceden role=None (aday+mülakatçı karışık) veriliyordu — model bazen bir
            # mülakatçı satırını "ilgili an" sanıp K'ya dayandırıyordu, _timestamp_field_grounded
            # (role="aday") bunu YİNE reddediyordu (İş 6I teşhisi, candidate 22). Genel/system-wide
            # bir tutarlılık düzeltmesi — hiçbir aday/pozisyon/kritere özel değil.
            hint = _find_relevant_transcript_lines(cname, transcript_view, role="aday", max_n=4)
            hint_txt = " | ".join(hint) if hint else "(bu konuda transkriptte açık bir satır bulunamadı)"
            lines.append(f"- evidence_timestamp_invalid: K alanındaki [mm:ss] damgası transkriptte YOK. Bu KRİTERE yakın GERÇEK anlar: {hint_txt} — K'yı BUNLARDAN birine dayandır.")
        elif v == "unsourced_eksik":
            hint = _find_relevant_transcript_lines(cname, transcript_view, role="mulakatci", max_n=4)
            hint_txt = " | ".join(hint) if hint else "(bu konuda mülakatçının sorduğu net bir soru bulunamadı — bu durumda EKSİK'i BOŞ bırak)"
            lines.append(f"- unsourced_eksik: EKSİK dolu ama SORU_DAMGASI gerçek bir mülakatçı sorusuna denk gelmiyor. Gerçek mülakatçı soru anları: {hint_txt}")
        elif v == "duplicate_claim":
            near_dup = next((c for c in accepted_claims if _is_exact_duplicate_claim(g, [c])), "")
            lines.append(f"- duplicate_claim: G alanın başka bir kriterde ZATEN kullanılan şu kanıtla neredeyse AYNI: '{near_dup}'. TAMAMEN FARKLI, bu kritere ÖZGÜ bir gözlem yaz.")
        elif v == "forbidden_transition_found":
            lines.append("- forbidden_transition_found: G veya E İÇİNDE 'ancak/fakat/ne var ki/bununla birlikte' var. SİL — G ve E'yi birbirinden BAĞIMSIZ, düz iki cümle yap.")
        else:
            lines.append(f"- {v}: {_VIOLATION_TR.get(v, v)}")
    return lines

# İŞ 6P — CRITERION RETRY ALAN İZOLASYONU (system-wide, hiçbir aday/pozisyon/kritere özel değil).
# İş 6O'da doğrulanan kök neden: regenerate_criterion_fields TÜM G/K/E/S'i her seferinde yeniden
# üretiyordu — tek bir evidence_timestamp_invalid (yalnız K'yi ilgilendiren) düzeltilirken model
# kendi inisiyatifiyle sağlam bir E/S ekleyip YENİ, İLGİSİZ bir violation (unsourced_eksik) yaratmış
# olabiliyordu. Aşağıdaki eşleme + iki yardımcı, HANGİ violation'ın HANGİ alan(lar)la ilişkili
# olduğunu sabitliyor; yalnız o alan(lar)ın değişmesine izin veriliyor, gerisi PROMPT talimatı +
# PARSE-SONRASI deterministik geri-yazma ile korunuyor (prompt'a TEK BAŞINA güvenilmiyor).
_VIOLATION_RELATED_FIELDS = {
    "banned_phrase_found": {"g", "e"},
    "forbidden_transition_found": {"g", "e"},
    "duplicate_claim": {"g"},
    "evidence_timestamp_invalid": {"k"},
    "unsourced_eksik": {"e", "s"},
    "full_score_has_eksik": {"e"},
    "score_direction_conflict": {"g", "e"},
    "out_of_scope_high_score": {"g", "k"},
}

def _fields_related_to_violations(violations: Optional[list]) -> Optional[set]:
    """İŞ 6P — violation kodlarının BİRLEŞİK olarak hangi G/K/E/S alan(lar)ıyla ilişkili olduğunu
    döner. 'structure_invalid' VEYA eşlemede olmayan (bilinmeyen) bir kod varsa None döner —
    bu durumda İZOLASYON UYGULANMAZ (güvenli varsayılan: tüm hücre zaten bozuk/tanımsız sayılır,
    mevcut tam-yeniden-üretim davranışı korunur)."""
    if not violations:
        return None
    related = set()
    for v in violations:
        fset = _VIOLATION_RELATED_FIELDS.get(v)
        if fset is None:
            return None
        related |= fset
    return related

def _enforce_field_isolation(new_fields: dict, prior_fields: Optional[dict], violations: list) -> dict:
    """İŞ 6P — DETERMİNİSTİK alan izolasyonu: yalnız mevcut violation'larla İLİŞKİLİ alan(lar)ın
    değişmesine izin verir; İLİŞKİSİZ alanlar prior_fields'ten AYNEN geri yazılır — model prompt
    talimatına uymayıp sağlam bir alanı değiştirse bile burada düzeltilir (prompt'a TEK BAŞINA
    güvenilmez). related=None ise (structure_invalid/bilinmeyen kod veya prior_fields yok)
    İZOLASYON UYGULANMAZ, new_fields OLDUĞU GİBİ döner (mevcut davranış)."""
    related = _fields_related_to_violations(violations)
    if related is None or not prior_fields:
        return new_fields
    result = dict(new_fields)
    for key in ("g", "k", "e", "s"):
        if key not in related:
            result[key] = prior_fields.get(key, "")
    return result

def regenerate_criterion_fields(candidate_id: int, level: int, provider: str, model: str, crit_name: str, cap: int,
                                transcript_text: str, prior_fields: dict, violations: list,
                                transcript_view: Optional[list] = None, accepted_claims: Optional[list] = None,
                                criterion_id: Optional[str] = None, attempt: Optional[int] = None,
                                source: str = "normal_validator_retry", crit_desc: Optional[str] = None) -> Optional[dict]:
    """GÖREV 2.3 — YALNIZCA bu kriterin G/K/E/S alanlarını, tespit edilen ihlal listesini modele
    AÇIKÇA vererek, HEDEFLİ olarak yeniden ürettirir (rapor genelini DEĞİL). Sağlayıcı, birincil
    rapor üretiminde kullanılanla AYNIDIR (provider run_deferred_finish_job'tan gelir — L1/L3
    Claude, L2 OpenAI; DOKUNULMAYACAKLAR: Level 2 hattına Anthropic çağrısı EKLENMEZ, provider
    zaten 'openai' olarak gelir). Başarısız/istisna/API anahtarı yok → None (çağıran bunu
    'yeniden üretim de başarısız' sayar — GÖREV 1.1'in max-3-deneme sayacını ilerletir).
    GÖREV 1.2 — ihlal listesi artık SOMUT (hangi alan, ne bekleniyor, hangi transkript anına bak),
    yalnız ihlal ADI değil (bkz. _build_violation_detail_lines).
    İŞ 6H — GÖZLEMLENEBİLİRLİK (davranış DEĞİŞMEDİ): `criterion_id`/`attempt`/`source` YALNIZCA
    loglama için — bu fonksiyonun KENDİ mantığını (prompt, model, max_tokens, parse) hiç
    etkilemez. `source='normal_validator_retry'` (varsayılan, apply_structured_rationale_gate'in
    3-deneme döngüsü) veya `source='criterion_recovery'` (İş 4'ün post-diskalifiye recovery'si) —
    ikisi AI_usage_logs'ta ayrı `action` adıyla (criterion_rationale_retry / criterion_rationale_
    recovery), Railway stdout'ta ayrı [CRITERION_AI_RETRY] satırıyla görünür.
    İŞ 6R — SEMANTİK KANIT ÖZ-DENETİMİ: `crit_desc` (kriterin tanımı, criteria_list'teki 'desc'
    alanından — apply_structured_rationale_gate'in döngüsünde `c.get('desc')`) artık prompt'a
    aktarılıyor; ÖNCEDEN retry yalnız `crit_name`+`cap` görüyordu, primary üretimden (build_criteria_
    text) DAHA AZ bilgiyle çalışıyordu (İş 6Q teşhisi). `crit_desc` yoksa/boşsa (None veya "") prompt
    yalnız kriter adıyla devam eder — CRASH YOK, davranış İş 6R ÖNCESİYLE AYNI (bkz. crit_desc=None
    kolu aşağıda). YENİ validator kuralı YOK, YENİ AI çağrısı YOK — yalnız MEVCUT retry çağrısının
    prompt'u güçlendirildi."""
    print(f"[CRITERION_AI_RETRY] c={candidate_id} L{level} criterion={criterion_id or '?'} "
          f"name={crit_name} attempt={attempt if attempt is not None else '?'} "
          f"violations={','.join(violations) if violations else '-'} source={source}")
    _usage_action = "criterion_rationale_retry" if source == "normal_validator_retry" else "criterion_rationale_recovery"
    reasons = "\n".join(_build_violation_detail_lines(violations, crit_name, prior_fields, transcript_view or [], accepted_claims or []))
    _hint_lines = _find_relevant_transcript_lines(crit_name, transcript_view or [], role=None, max_n=6)
    _hint_block = ("\n=== BU KRİTERLE İLGİLİ OLABİLECEK TRANSKRİPT ANLARI (referans için) ===\n" + "\n".join(_hint_lines)) if _hint_lines else ""
    # İŞ 6P — ALAN İZOLASYONU talimatı: yalnız ihlal(ler)le ilişkili alan(lar) değiştirilebilir
    # olduğunu AÇIKÇA belirt. Bu yalnız bir TALİMAT — asıl garanti parse-sonrası
    # _enforce_field_isolation'da (aşağıda) deterministik olarak uygulanıyor.
    _related_fields = _fields_related_to_violations(violations)
    _isolation_instruction = ""
    if _related_fields:
        _fname = {"g": "G", "k": "K", "e": "E", "s": "S"}
        _allowed = ", ".join(_fname[f] for f in ("g", "k", "e", "s") if f in _related_fields)
        _locked = [_fname[f] for f in ("g", "k", "e", "s") if f not in _related_fields]
        if _locked:
            _isolation_instruction = (
                f"\nALAN İZOLASYONU — ÇOK ÖNEMLİ: Bu ihlal(ler) yalnız {_allowed} alan(lar)ını ilgilendiriyor. "
                f"SADECE {_allowed} alan(lar)ını düzelt. {', '.join(_locked)} alan(lar)ını ÖNCEKİ ÜRETİMDEKİYLE "
                f"BİREBİR AYNI (kelimesi kelimesine kopyala) bırak — bu alanlar zaten SAĞLAM, gereksiz bir "
                f"değişiklik YENİ bir hataya yol açabilir. Önceki üretimde bir alan BOŞSA ve bu alan yukarıdaki "
                f"izin verilenler arasında DEĞİLSE, o alanı YİNE BOŞ bırak.")
    # İŞ 6R — crit_desc None/boş olabilir (kriter listesinde 'desc' hiç yoksa) — bu durumda
    # KRİTER satırı ESKİ haliyle (yalnız ad+tavan) kalır, CRASH/format bozulması YOK.
    _desc_line = f"\nKRİTER TANIMI: {crit_desc.strip()}" if crit_desc and crit_desc.strip() else ""
    # İŞ 6T — semantik ilgi öz-denetimine (İş 6R) EK olarak YÖN KONTÖLÜ eklendi: kanıtın olumlu/
    # olumsuz anlamının G/E'ye taşınırken tersine çevrilmemesi/yapay yumuşatılmaması. YENİ validator
    # kuralı/retry/AI çağrısı YOK — yalnız mevcut retry prompt'una talimat eklendi.
    _semantic_selfcheck = (
        "\nKANIT SEÇMEDEN ÖNCE SEMANTİK ÖZ-DENETİM (KESİN): K'yı yazmadan/değiştirmeden önce kendine "
        "sor: 'Bu aday ifadesi GERÇEKTEN bu kriterin tanımını mı destekliyor, yoksa başka bir yetkinliği "
        "mi gösteriyor?' Yukarıdaki KRİTER TANIMINA bak — yalnızca kriter ADINA değil. Zamanca yakın "
        "olması veya kriterin adıyla kelime benzerliği taşıması TEK BAŞINA YETERLİ DEĞİLDİR. Başka bir "
        "yetkinliği (örn. genel ses tonu/üslup, ilgisiz bir konu/anekdot) gösteren bir ifadeyi BU kriter "
        "için KULLANMA.\n"
        "YÖN KONTROLÜ (KESİN): G'yi veya E'yi yazmadan/değiştirmeden önce ayrıca kendine sor: "
        "'Adayın bu ifadesi GERÇEKTEN olumlu bir yetkinlik kanıtı mı, yoksa bir eksiklik/sınırlılık/"
        "belirsizlik/olası olumsuz sinyal mi?' Adayın söylediği olumsuz/zayıf bir ifadeyi SIRF G'yi "
        "doldurmak için olumlu bir yetkinlik cümlesine DÖNÜŞTÜRME; kanıtın doğal anlamından DAHA GÜÇLÜ "
        "bir sonuç ÇIKARMA. G ve E, kanıtın GERÇEK yönünü korumalı. Bu yalnız transkript ile rapor "
        "iddiası arasındaki YÖN tutarlılığıdır — mesleki/regülasyonel doğrulukla İLGİLENME.")
    prompt = f"""Aşağıdaki TEK kriter için, önceki üretimin DOĞRULAYICIDAN GEÇEMEDİĞİ tespit edildi. SADECE bu kriter için YENİDEN üret — rapor genelini yazma, açıklama ekleme.

KRİTER: {crit_name} (tavan: {cap} puan){_desc_line}

ÖNCEKİ ÜRETİM:
G: {(prior_fields or {}).get('g','')}
K: {(prior_fields or {}).get('k','')}
E: {(prior_fields or {}).get('e','')}
S: {(prior_fields or {}).get('s','')}

TESPİT EDİLEN İHLALLER (HER BİRİNİ DÜZELT — SOMUT TALİMAT):
{reasons}
{_isolation_instruction}
{_semantic_selfcheck}
{_hint_block}

ZORUNLU ÇIKTI FORMATI (başka HİÇBİR ŞEY yazma, tam olarak bu 4 satır, sırasıyla):
G: <adayın bu kriterde ne yapabildiği/bildiği/nasıl yaklaştığı — TEK cümle, 'ancak/fakat' YOK>
K: <[mm:ss] transkriptte GERÇEKTEN var olan bir ana damga + kısa somut alıntı/özet>
E: <GERÇEKTEN bir eksik varsa TEK cümle; yoksa bu satırı 'E:' olarak BOŞ bırak>
S: <E doluysa, mülakatçının bu eksikliği ortaya çıkaran sorusunun [mm:ss] damgası; E boşsa bu satırı 'S:' olarak BOŞ bırak>

=== TRANSKRİPT (TAM) ===
{(transcript_text or '')[:TRANSCRIPT_PROMPT_MAX_CHARS]}"""
    raw = None
    try:
        if provider == "claude":
            if not ANTHROPIC_API_KEY:
                return None
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=60.0)
            resp = client.messages.create(model=model or "claude-sonnet-4-6", max_tokens=500, temperature=0,
                                          messages=[{"role": "user", "content": prompt}])
            record_anthropic_usage(candidate_id, level, model or "claude-sonnet-4-6", _usage_action, resp)
            raw = resp.content[0].text
        elif provider == "openai":
            if not OPENAI_API_KEY:
                return None
            resp = openai_call("POST", "https://api.openai.com/v1/chat/completions",
                               json_body={"model": model or OPENAI_REPORT_MODEL,
                                          "messages": [{"role": "user", "content": prompt}],
                                          "max_tokens": 500, "temperature": 0},
                               timeout=45.0, step="criterion_rationale_retry", severity="background", retry=False,
                               context={"candidate_id": candidate_id, "level": level})
            result = resp.json()
            record_openai_chat_usage(candidate_id, level, model or OPENAI_REPORT_MODEL, _usage_action, result)
            raw = result["choices"][0]["message"]["content"]
        else:
            return None
    except Exception as ex:
        print(f"UYARI (regenerate_criterion_fields c={candidate_id} L{level} kriter={crit_name}): {type(ex).__name__}: {ex}")
        return None
    if not raw:
        return None
    g_m = re.search(r"(?im)^\s*G\s*:\s*(.*)$", raw)
    k_m = re.search(r"(?im)^\s*K\s*:\s*(.*)$", raw)
    e_m = re.search(r"(?im)^\s*E\s*:\s*(.*)$", raw)
    s_m = re.search(r"(?im)^\s*S\s*:\s*(.*)$", raw)
    new_fields = {
        "g": (g_m.group(1).strip() if g_m else ""),
        "k": (k_m.group(1).strip() if k_m else ""),
        "e": (e_m.group(1).strip() if e_m else ""),
        "s": (s_m.group(1).strip() if s_m else ""),
    }
    if not new_fields["g"] or not new_fields["k"]:
        return None
    # İŞ 6P — DETERMİNİSTİK alan izolasyonu: prompt talimatına GÜVENMEDEN, ilişkisiz alanları
    # önceki üretimden AYNEN geri yaz. Sonuç YİNE AYNI validate_criterion_fields'tan geçecek
    # (çağıran tarafta, main.py apply_structured_rationale_gate) — validator BYPASS edilmiyor.
    return _enforce_field_isolation(new_fields, prior_fields, violations)

_OUT_OF_SCOPE_SCORE_CAP_RATIO = 0.25  # GÖREV 5.2 — alan dışı/devretme: tavanın EN FAZLA %25'i

# İş emri — VALIDATOR KALİBRASYONU / GÖREV 1.3 — DEVRALMA KURALI. Birincilde 3 denemede de düşen
# (Değerlendirilemedi sayılan) bir kriter için ikinci değerlendirici AYNI kritere GEÇERLİ (puan +
# gerekçe) bir değerlendirme üretmişse, kriter "Değerlendirilemedi" olarak KALMAZ — ikincinin
# puanı+gerekçesi kullanılır (kanıtlı örnek: Murat AYZİT raporunda İnisiyatif/Analitik birincilde
# düştü ama ikinci değerlendirici İnisiyatif için ayrı blok, Baskı altında için [9:11] ile
# sorunsuz gerekçelendirdi — kanıt VARDI, sorun yalnız birincilin doğrulayıcıdan geçememesiydi).
# append_reviewer_section'dan çağrılır (reviewer SONRADAN, async çalıştığı için burası tek yer).
# İş emri — RAPOR İÇERİK STANDARDI / A1 — KÖK NEDEN: bu regex "— doğrulayıcı N denemede..." GEREKÇE
# EKİNİN varlığını ŞART koşuyordu. Ama tablo hücresi CUSTOMER-FACING görünüme (pos_table_display/
# prof_table_display, finalize_interview) girmeden ÖNCE _strip_total_line_for_display bu eki HER
# ZAMAN siliyor ("| Değerlendirilemedi (sistem) — ... |" → "| Değerlendirilemedi (sistem) |") —
# yani interviews.report'a (ve dolayısıyla append_reviewer_section'ın okuduğu final_report'a) kayıtlı
# hücrede bu ek ZATEN YOK. Sonuç: (a) devralma (apply_criterion_takeover) HİÇBİR disklaifiye satırı
# BULAMIYORDU (regex hiç eşleşmediği için tüm satırlar "zaten puanlı" sayılıp atlanıyordu — devralma
# sessizce HİÇ ÇALIŞMIYORDU), (b) Puanlama Kapsamı'nın devralma-sonrası yeniden hesaplaması
# (append_reviewer_section, _dropped_pos2/_dropped_prof2) da aynı nedenle HER ZAMAN boş çıkıyor,
# "12/12 değerlendirildi" yazıyordu — Değerlendirilemeyen Alanlar (ilk üretimdeki doğru listeyi
# hâlâ taşıyan, hiç yeniden hesaplanmayan _dropped_pos_names/_dropped_prof_names'ten türer) ile
# ÇELİŞİYORDU. Fix: regex artık YALNIZ çıplak "Değerlendirilemedi (sistem)" işaretini arar — gerekçe
# eki varsa da yoksa da eşleşir (üç kullanım yerinin tümü zaten yalnız bir tablo SATIRINDA arıyor).
_DISQUALIFIED_CELL_RE = re.compile(r"de[ğg]erlendirilemedi \(sistem\)", re.IGNORECASE)

# NOT — GÖREV 1.4'ün önceki turdaki koşullu ("yalnız %25 aşılınca görünen") notu KALDIRILDI;
# yerine HER ZAMAN üretilen "Puanlama Kapsamı" bölümü geçti (bkz. render_puanlama_kapsami,
# _PUANLAMA_KAPSAMI_HEAD/_PUANLAMA_KAPSAMI_RE — render_beyan_tutarliligi yakınında tanımlı).

def apply_criterion_takeover(table_text: str, criteria_list: list, rv_scores: dict, rv_gerekce: dict,
                             id_prefix: str, transcript_view: Optional[list] = None) -> tuple:
    """GÖREV 1.3 — yalnız apply_structured_rationale_gate'in diskalifiye ettiği (_DISQUALIFIED_CELL_RE
    eşleşen) satırlara dokunur; sistem/aday kaynaklı farklı eksik türlerine (hiç sorulmadı vb.)
    DOKUNMAZ. İkinci değerlendiricide o kimlik (P#/K#) için hem puan HEM gerekçe yoksa devralma
    YAPILMAZ (yalnız puan olup gerekçe yoksa da devralma yapılmaz — iş emri: 'puanı VE gerekçesi
    kullanılır'). Dönüş: (yeni_table_text, yeni_score_veya_None, log[]).
    İş emri — KANIT BÜTÜNLÜĞÜ VE İKİNCİ DEĞERLENDİRİCİ ÇIKTISI / KALEM 2 — KÖK NEDEN (atlanan yol):
    bu fonksiyon hiçbir transcript_view PARAMETRESİ almıyordu — ikinci değerlendiricinin kendi
    gerekçesindeki [mm:ss] referansı (VARSA) HİÇ doğrulanmadan doğrudan müşteri tablosuna
    yazılıyordu. validate_criterion_fields'ın role="aday" + alıntı-içerik kontrolü (bkz.
    _timestamp_field_grounded) BİRİNCİL değerlendiricinin çıktısını korusa da, DEVRALMA yolu bu
    kontrolün TAMAMEN DIŞINDAYDI — 'tek kapı' değildi. Artık transcript_view verilirse, rv_g
    içinde bir [mm:ss] damgası varsa AYNI kontrolden geçirilir; geçemezse devralma REDDEDİLİR
    (kriter 'Değerlendirilemedi (sistem)' olarak KALIR — uydurma/yanlış-konuşmacı kanıtla
    puanlanmaz). Damga yoksa (reviewer yalnız genel bir değerlendirme yazdıysa) davranış DEĞİŞMEZ."""
    if not table_text or not criteria_list:
        return table_text, None, []
    lines = table_text.splitlines()
    log = []
    used_lines = set()
    took_over = False
    row_info = []  # (cap, awarded_veya_None) — normalize hesaplaması için

    for idx, c in enumerate(criteria_list, start=1):
        cid = f"{id_prefix}{idx}"
        cap = _safe_int(c.get("weight"))
        cname = c.get("name", "")
        if cap <= 0 or not cname:
            continue
        best_i, best_s = None, 0.0
        for i, ln in enumerate(lines):
            if i in used_lines or ln.count("|") < 2:
                continue
            cells = [x.strip() for x in ln.strip().strip("|").split("|")]
            if len(cells) < 3:
                continue
            c0 = _norm_name(re.sub(r"[*_`]", "", cells[0]))
            if len(c0) < 2:
                continue
            sc = _name_score(cname, cells[0])
            if sc > best_s:
                best_i, best_s = i, sc
        if best_i is None or best_s < 0.34:
            continue
        used_lines.add(best_i)
        cells = [x.strip() for x in lines[best_i].strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        score_cell = cells[1]
        award_m = re.search(r"(?<![\d/])(\d+)\s*/\s*(\d+)(?![\d/])", score_cell)
        if not _DISQUALIFIED_CELL_RE.search(score_cell):
            # zaten puanlı YA DA farklı bir sistem/aday-kaynaklı eksik türü — devralma KONUSU DEĞİL.
            row_info.append((cap, _safe_int(award_m.group(1)) if award_m else None))
            continue
        rv = rv_scores.get(cid)
        rv_g = rv_gerekce.get(cid)
        if rv is None or not (rv_g or "").strip():
            row_info.append((cap, None))  # devralma yapılamadı — hâlâ diskalifiye
            continue
        # KALEM 2 — rv_g'de bir [mm:ss] damgası varsa AYNI kapıdan (role="aday" + alıntı-içerik)
        # geçmek ZORUNDA; geçemezse devralma REDDEDİLİR, kriter diskalifiye KALIR.
        if _extract_timestamp(rv_g) and not _timestamp_field_grounded(rv_g, transcript_view or [], role="aday"):
            log.append({"kriter": cname, "kimlik": cid, "sonuc": "devralma_reddedildi_kanit_gecersiz"})
            row_info.append((cap, None))
            continue
        rv_awarded = max(0, min(_safe_int(rv[0]), cap))
        cells[1] = f"{rv_awarded}/{cap}"
        # İş emri — KAPI EŞİĞİ VE SON TUTARLILIK / MADDE 4 — KÖK NEDEN: "(ikinci değerlendirici)"
        # eki müşteri tablosuna basılıyordu — bu iç işaretleme dilidir, müşteri metninin parçası
        # OLMAMALI. Devralma bilgisi zaten ayrıca record_system_decision("kriter_devralindi", ...)
        # ile yönetici kaydına (log listesi, aşağıda) geçiyor — müşteri hücresi artık YALNIZ
        # gerekçe metnini taşır.
        cells[2] = rv_g.strip()
        lines[best_i] = "| " + " | ".join(cells) + " |"
        log.append({"kriter": cname, "kimlik": cid, "sonuc": "devralindi", "yeni_puan": f"{rv_awarded}/{cap}"})
        row_info.append((cap, rv_awarded))
        took_over = True

    new_score = None
    if took_over:
        awarded_sum = sum(a for _, a in row_info if a is not None)
        denom = sum(cap for cap, a in row_info if a is not None)
        # İŞ EMRİ — NİHAİ RAPOR TUTARLILIĞI: TEK canonical yuvarlama (_round_half_up, madde 5) —
        # burada yalnız yerleşik round() (banker's rounding) kullanılıyordu, sistemin geri kalanıyla
        # tutarsızdı (ör. 68.5 burada 68, compute_genel_puan'da 69 çıkabiliyordu).
        new_score = max(0, min(100, _round_half_up(awarded_sum / denom * 100))) if denom > 0 else None
        body = "\n".join(lines)
        total_re = (r"(\*\*\s*PROF\S*\s+PUANI\s*[:：]\s*)(\d+)(\s*/\s*)(\d+)(\s*\*\*)" if id_prefix == "K"
                   else r"(\*\*\s*TOPLAM\s+PUAN\s*[:：]\s*)(\d+)(\s*/\s*)(\d+)(\s*\*\*)")
        if new_score is not None:
            body = re.sub(total_re, lambda m: f"{m.group(1)}{new_score}{m.group(3)}100{m.group(5)}", body, count=1, flags=re.IGNORECASE)
        lines = body.splitlines()

    return "\n".join(lines), new_score, log

# İŞ EMRİ — FINAL EVALUATION ARCHITECTURE / madde 2 — L3 SECOND EVALUATOR'I GERÇEK DENETÇİ YAP.
# FATAL AUDIT + bu iş emrinin kendi tespiti: apply_criterion_takeover YALNIZ diskalifiye
# ('Değerlendirilemedi') satırlara dokunuyordu — PASS etmiş bir kriterde reviewer (Claude, L3)
# AÇIK ve KANITLANABİLİR bir hata bulsa bile bunu yalnız 'yorum' (Ek Görüş/diff) olarak
# bırakıyordu, final DURUMA hiç giremiyordu. Bu fonksiyon apply_criterion_takeover'ı GENELLEŞTİRİR:
# ZATEN PUANLI (diskalifiye OLMAYAN) bir satırda da reviewer FARKLI puan+gerekçe verdiyse,
# gerekçe DETERMİNİSTİK olarak grounded ise (AYNI _timestamp_field_grounded kapısı — YENİ bir
# doğrulama icat EDİLMEDİ) düzeltme UYGULANIR. apply_criterion_takeover'dan KASITLI FARKI: burada
# 'kaybedecek bir şey yok' durumu GEÇERLİ DEĞİL (geçerli bir puanın üzerine yazılıyor) — bu yüzden
# grounding takeover'dan DAHA SIKI: [mm:ss] damgası YOKSA da REDDEDİLİR (takeover'da damgasız
# genel değerlendirme kabul edilebiliyordu, burada edilemez). Diskalifiye satırlara HİÇ dokunmaz
# (onlar zaten yukarıdaki apply_criterion_takeover'ın sorumluluğunda — ÇİFT İŞLEME YOK, sorumluluk
# net ayrık). ONE PRIMARY + ONE REVIEWER: reviewer zaten TEK kez çalıştı (run_report_reviewer,
# retry=False) — bu fonksiyon YENİ bir AI çağrısı YAPMAZ, yalnız var olan reviewer çıktısını
# deterministik olarak uygular/reddeder.
def apply_reviewer_criterion_correction(table_text: str, criteria_list: list, rv_scores: dict, rv_gerekce: dict,
                                        id_prefix: str, transcript_view: Optional[list] = None) -> tuple:
    """Dönüş: (yeni_table_text, yeni_score_veya_None, log[]). Yalnız ZATEN PUANLI (diskalifiye
    OLMAYAN) satırlarda çalışır; diskalifiye satırlar apply_criterion_takeover'ın sorumluluğunda
    kalır (bu fonksiyon onlara DOKUNMAZ)."""
    if not table_text or not criteria_list:
        return table_text, None, []
    lines = table_text.splitlines()
    log = []
    used_lines = set()
    changed = False
    row_info = []

    for idx, c in enumerate(criteria_list, start=1):
        cid = f"{id_prefix}{idx}"
        cap = _safe_int(c.get("weight"))
        cname = c.get("name", "")
        if cap <= 0 or not cname:
            continue
        best_i, best_s = None, 0.0
        for i, ln in enumerate(lines):
            if i in used_lines or ln.count("|") < 2:
                continue
            cells = [x.strip() for x in ln.strip().strip("|").split("|")]
            if len(cells) < 3:
                continue
            c0 = _norm_name(re.sub(r"[*_`]", "", cells[0]))
            if len(c0) < 2:
                continue
            sc = _name_score(cname, cells[0])
            if sc > best_s:
                best_i, best_s = i, sc
        if best_i is None or best_s < 0.34:
            continue
        used_lines.add(best_i)
        cells = [x.strip() for x in lines[best_i].strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        score_cell = cells[1]
        if _DISQUALIFIED_CELL_RE.search(score_cell):
            continue  # diskalifiye — apply_criterion_takeover'ın sorumluluğu, burada ATLANIR
        award_m = re.search(r"(?<![\d/])(\d+)\s*/\s*(\d+)(?![\d/])", score_cell)
        current_awarded = _safe_int(award_m.group(1)) if award_m else None

        rv = rv_scores.get(cid)
        rv_g = rv_gerekce.get(cid)
        if rv is None or not (rv_g or "").strip():
            row_info.append((cap, current_awarded))
            continue  # reviewer bu kriter için farklı bir şey söylemedi — primary AYNEN kalır

        rv_awarded = max(0, min(_safe_int(rv[0]), cap))
        if current_awarded is not None and rv_awarded == current_awarded:
            row_info.append((cap, current_awarded))
            continue  # aynı puan — düzeltme DEĞİL, dokunma

        rv_ts = _extract_timestamp(rv_g)
        if not rv_ts or not _timestamp_field_grounded(rv_g, transcript_view or [], role="aday"):
            log.append({"kriter": cname, "kimlik": cid, "sonuc": "reviewer_duzeltmesi_reddedildi_kanit_gecersiz"})
            row_info.append((cap, current_awarded))
            continue

        cells[1] = f"{rv_awarded}/{cap}"
        cells[2] = rv_g.strip()
        lines[best_i] = "| " + " | ".join(cells) + " |"
        log.append({"kriter": cname, "kimlik": cid, "sonuc": "reviewer_duzeltmesi_uygulandi",
                   "onceki_puan": (f"{current_awarded}/{cap}" if current_awarded is not None else None),
                   "yeni_puan": f"{rv_awarded}/{cap}"})
        row_info.append((cap, rv_awarded))
        changed = True

    new_score = None
    if changed:
        awarded_sum = sum(a for _, a in row_info if a is not None)
        denom = sum(cap for cap, a in row_info if a is not None)
        new_score = max(0, min(100, _round_half_up(awarded_sum / denom * 100))) if denom > 0 else None
        body = "\n".join(lines)
        total_re = (r"(\*\*\s*PROF\S*\s+PUANI\s*[:：]\s*)(\d+)(\s*/\s*)(\d+)(\s*\*\*)" if id_prefix == "K"
                   else r"(\*\*\s*TOPLAM\s+PUAN\s*[:：]\s*)(\d+)(\s*/\s*)(\d+)(\s*\*\*)")
        if new_score is not None:
            body = re.sub(total_re, lambda m: f"{m.group(1)}{new_score}{m.group(3)}100{m.group(5)}", body, count=1, flags=re.IGNORECASE)
        lines = body.splitlines()

    return "\n".join(lines), new_score, log

# İŞ 4 — VALIDATOR FAILURE RECOVERY (Problem D): "Değerlendirilemedi (sistem)" bugüne kadar iki
# farklı durumu (A: adaydan gerçekten veri yok, B: aday cevabı var ama AI'ın G/K/E/S çıktısı
# validator'dan geçemedi) AYNI sonuca bağlıyordu. Aşağıdaki LOKAL/İş-4'e-özel yardımcı, VAR OLAN
# _find_relevant_transcript_lines()'ın global davranışına HİÇ dokunmadan, ayrı bir ucuz/deterministik
# ÖN-FİLTRE sağlar: yalnız "bu kriter için bir recovery denemesi yapmaya değer mi" sorusuna cevap
# verir — puan/kanıt/nihai karar ÜRETMEZ. Nihai karar HER ZAMAN (recovery'de de) validate_criterion_
# fields()'tan geçer; bu fonksiyon yalnızca gereksiz LLM çağrısını (veri hiç yoksa) önler.
def _has_candidate_signal_for_recovery(cname: str, transcript_view: list) -> bool:
    """İŞ 4 — yalnız role='aday' satırlarına bakar (mülakatçı/sistem/başlık satırları KANIT
    SAYILMAZ); kriter adının >=4 harfli kelimeleriyle EN AZ BİR örtüşme varsa True döner. Bu NİHAİ
    bir 'kanıt var' kararı DEĞİLDİR — yalnız ucuz bir tetikleyici. Boş/eşleşmesiz -> recovery hiç
    denenmez, mevcut davranış (doğrudan diskalifiye) %100 korunur."""
    kws = [w for w in _norm_name(cname).split() if len(w) >= 4]
    if not kws:
        return False
    for row in (transcript_view or []):
        if row.get("role") != "aday":
            continue
        norm = _norm_name(row.get("text") or "")
        if any(w in norm for w in kws):
            return True
    return False

# İŞ 6J — SAF METİN VIOLATION'LARINI LLM'SİZ DÜZELT: unsourced_eksik / banned_phrase_found /
# forbidden_transition_found HİÇBİRİ semantik değerlendirme GEREKTİRMİYOR (bkz. İş 6I teşhisi) —
# üçü de ya sabit bir kuralın mekanik uygulanması (E/S sil) ya da saf regex-kalıp temizliği
# (bağlaç/klişe cümle sil). Bu fonksiyon YALNIZ bu 3 kodu ele alır; duplicate_claim,
# evidence_timestamp_invalid, structure_invalid, score_direction_conflict, out_of_scope_high_score
# ve diğer TÜM violation'lar OLDUĞU GİBİ (dokunulmadan) döner — onlar için mevcut AI retry akışı
# DEĞİŞMEDEN devam eder. G ve K alanlarına, awarded/cap'e HİÇ dokunmaz. AI çağrısı YAPMAZ.
def _deterministic_repair_criterion_fields(fields: Optional[dict], violations: list) -> tuple:
    """Dönüş: (yeni_fields, repaired_kodlari[]). fields boşsa (legacy serbest-metin hücre) no-op —
    bu repair yalnız yapısal G/K/E/S alanları üzerinde çalışır."""
    if not fields:
        return fields, []
    new_fields = dict(fields)
    repaired = []
    if "unsourced_eksik" in violations:
        # KURAL (main.py _VIOLATION_TR'de zaten yazılı): S grounded değilse EKSİK'i BOŞ bırak.
        new_fields["e"] = ""
        new_fields["s"] = ""
        repaired.append("unsourced_eksik")
    if "forbidden_transition_found" in violations:
        for key in ("g", "e"):
            val = new_fields.get(key) or ""
            if val and _TRANSITION_WORD_RE.search(val):
                cleaned = _TRANSITION_WORD_RE.sub("", val)
                cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,;")
                new_fields[key] = cleaned
        repaired.append("forbidden_transition_found")
    if "banned_phrase_found" in violations:
        for key in ("g", "e"):
            text = new_fields.get(key) or ""
            for hit in banned_phrase_hits(text):
                text = text.replace(hit, "")
            text = re.sub(r"\s{2,}", " ", text).strip(" ,;.")
            new_fields[key] = text
        # Kaldırılan bölüm E'nin TAMAMIYSA, ona bağlı S de artık anlamsız — birlikte boşalt.
        if not (new_fields.get("e") or "").strip():
            new_fields["e"] = ""
            new_fields["s"] = ""
        repaired.append("banned_phrase_found")
    return new_fields, repaired

def apply_structured_rationale_gate(table_text: str, criteria_list: list, id_prefix: str, transcript_view: list,
                                    transcript_text: str, provider: str, model: str, candidate_id: int, level: int):
    """GÖREV 2 — DOĞRULAYICI KAPI. `table_text` (recompute_and_fix_score/recompute_profile_section
    tarafından ZATEN puan-normalize edilmiş tablo) üzerindeki her kriterin 'Kanıt ve Analiz'
    hücresini YAPISAL alanlara ayrıştırır, doğrular; geçemeyeni max 3 deneme HEDEFLİ yeniden
    ürettirir; 3 denemeden sonra hâlâ geçemezse kriteri 'Değerlendirilemedi (sistem)' sayar (payda
    dışına ALINIR, TOPLAM/PROFİL PUANI yeniden normalize edilir) ve YER TUTUCU YEDEK GEREKÇE
    ÜRETMEZ. GÖREV 5.2 — out_of_scope_high_score tespit edilirse puan DETERMİNİSTİK olarak tavanın
    %25'ine kelepçelenir (LLM'in kendiliğinden düşürmesine GÜVENİLMEZ — bu turun kök nedeni).
    GÖREV 2.2 — transition_overuse ÇAPRAZ KRİTER kontrolü ayrıca, tüm kriterler işlendikten SONRA
    yapılır (render seçimiyle düzeltilir, LLM çağrısı GEREKMEZ — bu bir render sorunu, içerik
    sorunu değil). GÖREV 5.5 — alan dışı/devretme LEKSİK imzası (puan zaten düşük olsa BİLE)
    `flagged_scope` listesine toplanır; çağıran (finalize_interview) bunun Gelişim Alanları'nda
    RİSK olarak GERÇEKTEN raporlandığını doğrular, yoksa kendisi ekler (unreported_scope_limitation
    sessizce geçilemez). Dönüş: (yeni_table_text, yeni_score_veya_None, log[], flagged_scope[])."""
    if not table_text or not criteria_list:
        return table_text, None, [], []
    lines = table_text.splitlines()
    log = []
    accepted_claims = []
    rendered_rows = []
    flagged_scope = []
    used_lines = set()

    for idx, c in enumerate(criteria_list, start=1):
        cid = f"{id_prefix}{idx}"
        cap = _safe_int(c.get("weight"))
        cname = c.get("name", "")
        # İŞ 6R — kriter TANIMI (varsa) retry'a aktarılacak; kriter listesinde yoksa/boşsa
        # regenerate_criterion_fields güvenli biçimde boş desc ile devam eder.
        cdesc = c.get("desc") or ""
        if cap <= 0 or not cname:
            continue
        best_i, best_s = None, 0.0
        for i, ln in enumerate(lines):
            if i in used_lines or ln.count("|") < 2:
                continue
            cells = [x.strip() for x in ln.strip().strip("|").split("|")]
            if len(cells) < 3:
                continue
            c0 = _norm_name(re.sub(r"[*_`]", "", cells[0]))
            if len(c0) < 2:
                continue
            sc = _name_score(cname, cells[0])
            if sc > best_s:
                best_i, best_s = i, sc
        if best_i is None or best_s < 0.34:
            continue
        used_lines.add(best_i)
        cells = [x.strip() for x in lines[best_i].strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        score_cell, evidence_cell = cells[1], cells[2]
        award_m = re.search(r"(?<![\d/])(\d+)\s*/\s*(\d+)(?![\d/])", score_cell)
        if not award_m or re.search(r"de[ğg]erlendirilemedi", score_cell, re.IGNORECASE):
            continue  # zaten sistem/aday kaynaklı eksik — yapısal doğrulama gerekmez (kanıt yok)
        awarded = _safe_int(award_m.group(1))

        fields = parse_structured_evidence_cell(evidence_cell)
        if fields is None:
            # GERİYE UYUM (kök neden — sentetik regresyon testinde yakalandı): model henüz YENİ
            # '~~' etiketli formata geçmemiş bir hücre (ör. eski '[dk] örnek analiz sonuç' serbest
            # metni) HER ZAMAN 'structure_invalid' sayıp diskalifiye etmek AŞIRI SERTTİR — TÜM
            # kriterlerin payda dışı kalıp TOPLAM PUAN'ın anlamsızlaşmasına (denom=0) yol açardı.
            # Bunun yerine: klişe/geçiş-bağlacı YOKSA legacy hücre AYNEN KORUNUR (retry/diskalifiye
            # YOK, yapısal alan ayrımının getirdiği ek kontroller — duplicate_claim, SORU_DAMGASI
            # doğrulaması vb. — bu hücreye uygulanamaz, bu bilinen bir kapsam dışılıktır); yalnız
            # GERÇEKTEN sorunlu (klişeli/boş) legacy hücreler retry akışına girer.
            if not evidence_cell.strip() or len(strip_markdown(evidence_cell)) < 3:
                violations = ["structure_invalid"]
            else:
                violations = []
                if banned_phrase_hits(evidence_cell):
                    violations.append("banned_phrase_found")
                if _TRANSITION_WORD_RE.search(evidence_cell):
                    violations.append("forbidden_transition_found")
            if not violations:
                log.append({"kriter": cname, "kimlik": cid, "sonuc": "gecti_eski_format"})
                rendered_rows.append({"line_idx": best_i, "disqualified": False, "cap": cap, "awarded": awarded,
                                      "fields": None, "template_idx": idx - 1, "cid": cid, "cname": cname})
                continue
        else:
            violations = validate_criterion_fields(fields, cap, awarded, transcript_view, accepted_claims)

        # İŞ 6J — AI çağrısından ÖNCE, yalnız saf-metin violation'ları (unsourced_eksik/
        # banned_phrase_found/forbidden_transition_found) için deterministik temizlik dene.
        # Diğer violation'lar (varsa) DOKUNULMADAN kalır; TEMİZLİK SONRASI validator AYNI şekilde
        # tekrar çalıştırılır — kalan violation'lar için aşağıdaki mevcut AI retry akışı DEĞİŞMEDEN
        # devam eder. fields None ise (legacy serbest-metin hücre) no-op.
        _repaired_fields, _repaired_codes = _deterministic_repair_criterion_fields(fields, violations)
        if _repaired_codes:
            fields = _repaired_fields
            violations = validate_criterion_fields(fields, cap, awarded, transcript_view, accepted_claims)
            print(f"[CRITERION_DETERMINISTIC_REPAIR] c={candidate_id} L{level} criterion={cid} "
                  f"repaired={','.join(_repaired_codes)}")
        # İŞ EMRİ — ÇOKLU TALENT MİMARİSİ / madde E/F/G (TEK-PASS AI AKIŞI) — ESKİ davranış: bu
        # kriter validator'dan geçemezse regenerate_criterion_fields'a (AI content-retry) kadar
        # 3 kez gidiliyordu, her seferinde transkriptin TAMAMI yeniden gönderiliyordu (kanıtlı kök
        # neden — teşhis turu: tek raporun TPM limitine çarpmasının asıl nedeni buydu). Bu iş
        # emriyle KALDIRILDI: validator artık yalnız TESPİT eder + (yukarıda) DETERMİNİSTİK onarım
        # dener — "geçemeyen kriteri AI'ya tekrar tekrar sor" NORMAL AKIŞTAN çıkarıldı. Deterministik
        # onarım sonrası hâlâ ihlal varsa kriter doğrudan aşağıdaki 'değerlendirilemedi (sistem)'
        # dalına düşer — scoring/denominator/ROUND_HALF_UP kuralları DEĞİŞMEDİ, yalnız bu kararın
        # ÖNÜNDEKİ gizli AI çağrıları kaldırıldı. regenerate_criterion_fields fonksiyonu (aşağıda)
        # SİLİNMEDİ — normal akıştan çağrılmıyor, davranışı koruma amaçlı yerinde bırakıldı.
        attempt = 0

        # GÖREV 5.2 — out_of_scope_high_score DETERMİNİSTİK kelepçe (retry sonrası da kalabilir;
        # bu tavan LLM'e bırakılmaz — 5. iş emrinin bizzat kanıtladığı gibi model bunu kendiliğinden
        # yapmıyor). Kelepçe uygulanınca ihlal listesinden çıkarılır (artık kural sağlanmış olur).
        if "out_of_scope_high_score" in violations and cap > 0:
            capped = max(0, int(cap * _OUT_OF_SCOPE_SCORE_CAP_RATIO))
            if awarded > capped:
                log.append({"kriter": cname, "kimlik": cid, "sonuc": "alan_disi_puan_kelepceledi",
                           "onceki_puan": f"{awarded}/{cap}", "yeni_puan": f"{capped}/{cap}"})
                awarded = capped
            violations = [v for v in violations if v != "out_of_scope_high_score"]

        # GÖREV 5.5 — alan dışı/devretme leksik imzası puan zaten düşük olsa BİLE (5.2'de
        # "tespit edilirse" diyor, "puan yüksekse" DEĞİL) toplanır — Gelişim Alanları'nda RİSK
        # olarak raporlanmalı; raporlanmadıysa aşağıda (finalize_interview) sistem kendisi ekler.
        # İş emri — RAPOR İÇERİK STANDARDI / B3 — KÖK NEDEN (B2 ile AYNI kök): bu kontrol
        # 'violations' hâlâ dolu olsa (ör. kriter sonunda diskalifiye olsa) BİLE çalışıyordu — K
        # alanı henüz doğrulanmamış/yanlış-konuşmacıya ait bir damga taşısa bile flagged_scope'a
        # (ve oradan CV↔Uyum enjeksiyonuna) sızabiliyordu. Artık yalnız K'nin damgası GERÇEKTEN
        # adaya ait bir transkript satırına denk geliyorsa (B2'nin aynı role="aday" kontrolü)
        # flagged_scope'a eklenir.
        if fields and (_OUT_OF_SCOPE_RE.search(f"{fields.get('k','')} {fields.get('g','')}")
                      or _DELEGATION_RE.search(f"{fields.get('k','')} {fields.get('g','')}")):
            _k_ts_scope = _extract_timestamp(fields.get("k") or "")
            if not _k_ts_scope or _timestamp_field_grounded(fields.get("k") or "", transcript_view, role="aday"):
                flagged_scope.append({"kriter": cname, "kanit": (fields.get("k") or "")[:200]})

        # İŞ EMRİ — ÇOKLU TALENT MİMARİSİ / madde E/F/G: eski "İŞ 4 — VALIDATOR FAILURE RECOVERY"
        # (3 normal retry tükenince 1 ek AI content-retry) KALDIRILDI — aynı gerekçeyle yukarıdaki
        # 3-retry döngüsü kaldırıldı: bu da bir content-retry'ydi (criterion_recovery action'ı),
        # normal akışta artık YOK. _has_candidate_signal_for_recovery fonksiyonu SİLİNMEDİ, burada
        # çağrılmıyor.

        if violations:
            # TEK DÜZELTME — DEĞERLENDİRİLEMEDİ KRİTERLERİ: kriter aslında SORULMUŞ/CEVAPLANMIŞ
            # (aksi halde award_m hiç eşleşmezdi, bkz. yukarıdaki 'zaten sistem/aday kaynaklı eksik'
            # erken çıkışı) — burada başarısız olan yalnızca doğrulayıcının YAPISAL gerekçe formatı
            # (grounding/timestamp/vb.). Bu bir TEKNİK VALİDATOR/PARSER sorunudur — ne "Değerlendiri-
            # lemedi (sistem)" (payda dışı) YAZILIR, NE DE %25 taban puana düşürülür (bu taban puan
            # kuralı YALNIZ adayın gerçekten yetersiz cevap verdiği — bilmiyorum/deneyimim yok/anlamlı
            # cevap yok — durumlar içindir, bkz. CRITERION_SCORING_RULE madde 2; bu SATIR o durum
            # DEĞİL, adayın cevabı zaten var ve puanlanmış). Model tarafından zaten verilmiş `awarded`
            # puan AYNEN KORUNUR.
            # TEK DÜZELTME (2. tur) — MÜŞTERİ RAPORUNDA TEKNİK NOT GÖSTERİLMEZ: violation kodları
            # (duplicate_claim, evidence_timestamp_invalid, vb.) yalnız `log`'a (admin/system_decision
            # kaydı) yazılır — MÜŞTERİ hücresine ASLA. Gerekçe hücresi MÜMKÜN OLDUĞUNCA modelin
            # KENDİ mevcut G/K/E/S içeriğiyle (fields varsa, "gecti" dalıyla AYNI render fonksiyonu —
            # yeni içerik UYDURULMAZ) doldurulur; yapısal alanlar hiç parse edilemediyse (fields=None)
            # modelin ORİJİNAL serbest-metin gerekçesi AYNEN korunur; o da yoksa nötr, jargonsuz tek
            # cümlelik bir not yazılır. Yeni AI çağrısı YOK.
            log.append({"kriter": cname, "kimlik": cid, "sonuc": "teknik_validator_hatasi_puan_korundu",
                       "ihlaller": violations, "puan": f"{awarded}/{cap}"})
            if fields:
                cells[2] = render_criterion_rationale(fields, idx - 1)
            elif evidence_cell.strip():
                cells[2] = evidence_cell.strip()
            else:
                cells[2] = "Bu kriter için ayrıntılı kanıt/gerekçe metni bulunamadı."
            cells[1] = f"{awarded}/{cap}"
            rendered_rows.append({"line_idx": best_i, "disqualified": False, "cap": cap, "awarded": awarded})
        else:
            log.append({"kriter": cname, "kimlik": cid, "sonuc": "gecti", "deneme": attempt})
            accepted_claims.append(fields["g"])
            t_idx = idx - 1
            rendered_rows.append({"line_idx": best_i, "disqualified": False, "cap": cap, "awarded": awarded,
                                  "fields": fields, "template_idx": t_idx, "cid": cid, "cname": cname})
            cells[1] = f"{awarded}/{cap}"
            cells[2] = render_criterion_rationale(fields, t_idx)
        lines[best_i] = "| " + " | ".join(cells) + " |"

    # GÖREV 2.2 — transition_overuse: ÇAPRAZ KRİTER (rapor genelindeki ORAN), tek kriterin kendi
    # metnine bakan forbidden_transition_found'dan AYRI. >1/3 ise fazlalık RENDER'ı değiştirilir.
    # Legacy (yapısal olmayan, 'fields' None) satırlar bu sayıma DAHİL DEĞİL — onlar zaten intake
    # sırasında geçiş-bağlacı için ayrıca kontrol edildi (yukarıda) ve biz onları hiç render ETMİYORUZ.
    _passed = [r for r in rendered_rows if not r["disqualified"] and r.get("fields") is not None]
    n = len(_passed)
    if n:
        hit_idx = [i for i, r in enumerate(_passed) if _TRANSITION_WORD_RE.search(render_criterion_rationale(r["fields"], r["template_idx"]))]
        limit = n // 3
        if len(hit_idx) > limit:
            fixed = 0
            remaining = len(hit_idx)
            for i in reversed(hit_idx):
                if remaining <= limit:
                    break
                r = _passed[i]
                alt_idx = 0 if (r["template_idx"] % 3) != 0 else 2
                new_text = render_criterion_rationale(r["fields"], alt_idx)
                row_cells = [x.strip() for x in lines[r["line_idx"]].strip().strip("|").split("|")]
                if len(row_cells) >= 3:
                    row_cells[2] = new_text
                    lines[r["line_idx"]] = "| " + " | ".join(row_cells) + " |"
                fixed += 1
                remaining -= 1
            log.append({"kriter": "(rapor geneli)", "kimlik": "-", "sonuc": "transition_overuse_rebalance",
                       "ihlaller": ["transition_overuse"],
                       "detay": f"{len(hit_idx)}/{n} kriterde geçiş bağlacı vardı (izin: {limit}); {fixed} kriter farklı şablonla yeniden render edildi"})

    # Diskalifiye edilen / kelepçelenen kriterler varsa: TOPLAM/PROFİL PUANI yeniden normalize edilir.
    # (TEK DÜZELTME — teknik validator hatası artık ne disqualify EDER ne de puanı DEĞİŞTİRİR; awarded
    # aynen korunduğu için bu satır TOPLAM PUAN'ı etkilemez, yeniden normalize tetiklemesine gerek yok.)
    new_score = None
    if any(r["disqualified"] for r in rendered_rows) or any(g.get("sonuc") == "alan_disi_puan_kelepceledi" for g in log):
        awarded_sum = sum(r["awarded"] for r in rendered_rows if not r["disqualified"])
        denom = sum(r["cap"] for r in rendered_rows if not r["disqualified"])
        # İŞ EMRİ — NİHAİ RAPOR TUTARLILIĞI: TEK canonical yuvarlama (_round_half_up, madde 5).
        new_score = max(0, min(100, _round_half_up(awarded_sum / denom * 100))) if denom > 0 else None
        body = "\n".join(lines)
        total_re = (r"(\*\*\s*PROF\S*\s+PUANI\s*[:：]\s*)(\d+)(\s*/\s*)(\d+)(\s*\*\*)" if id_prefix == "K"
                   else r"(\*\*\s*TOPLAM\s+PUAN\s*[:：]\s*)(\d+)(\s*/\s*)(\d+)(\s*\*\*)")
        if new_score is not None:
            body = re.sub(total_re, lambda m: f"{m.group(1)}{new_score}{m.group(3)}100{m.group(5)}", body, count=1, flags=re.IGNORECASE)
        lines = body.splitlines()

    return "\n".join(lines), new_score, log, flagged_scope

# İş emri — VALIDATOR KALİBRASYONU / GÖREV 2 — %25 KELEPÇESİ TRANSKRİPT GENELİNDE. Kök neden
# (kanıtlı): validate_criterion_fields'in out_of_scope_high_score kontrolü yalnız O KRİTERİN
# KENDİ K/G alanına bakıyordu — alan dışı/devretme beyanı transkriptin BAŞKA bir turunda olduğunda
# (kriter başka bir damgayı kanıt seçtiğinde) hiç görünmüyordu (Nakit Akışı 12/25, Uyum 5/10 kaldı;
# ikinci değerlendirici AYNI transkriptten 10/25 ve 3/10 verip beyanı gerekçesinde yazdı — birincil
# doğrulayıcı çalışmadı). Bu fonksiyon beyanı TRANSKRİPT GENELİNDE arar, hangi kritere ait olduğunu
# (mülakatçının O turda SORDUĞU konu üzerinden) belirler, eşleşme belirsizse pozisyonun ÇEKİRDEK
# (en yüksek ağırlıklı) kriterlerine uygular — apply_structured_rationale_gate'ten BAĞIMSIZ, SONRA
# çalışan ikinci bir geçiş (yapısal gate'in K/G'ye bakan kontrolünü DEĞİŞTİRMEZ, TAMAMLAR).
def find_scope_declarations(transcript_view: list) -> list:
    """GÖREV 2.1 — alan dışı/devretme beyanlarını TRANSKRİPTTEKİ TÜM aday satırlarında arar
    (yalnız bir kriterin KANIT alanında değil). Dönüş: [{"ts","elapsed_ms","text","index"}].
    İŞ 3 — 'index' (transcript_view'daki konum) eklendi: elapsed_ms'e bağımlı olmadan geriye
    doğru bağlam penceresi kurulabilsin diye (bkz. _nearby_topic_context). 'text' artık [:200]
    KIRPILMIYOR — declaration'ın tam metni korunur (log/rapor amaçlı kesilme riski kaldırıldı)."""
    out = []
    for idx, row in enumerate(transcript_view or []):
        if row.get("role") != "aday":
            continue
        text = (row.get("text") or "").strip()
        if text and (_OUT_OF_SCOPE_RE.search(text) or _DELEGATION_RE.search(text)):
            out.append({"ts": row.get("ts") or "", "elapsed_ms": row.get("elapsed_ms"), "text": text, "index": idx})
    return out

def _preceding_question(declaration_elapsed_ms, transcript_view: list) -> Optional[dict]:
    """Beyandan HEMEN ÖNCE gelen mülakatçı satırı — 'bu beyan hangi SORUYA cevaben söylendi' (GÖREV 2.2)."""
    if declaration_elapsed_ms is None:
        return None
    best = None
    for row in (transcript_view or []):
        if row.get("role") != "mulakatci":
            continue
        em = row.get("elapsed_ms")
        if em is None or em > declaration_elapsed_ms:
            continue
        if best is None or em > best.get("elapsed_ms", -1):
            best = row
    return best

# İŞ 3 — SCOPE CLAMP BAĞLAM EŞLEŞTİRMESİ (Problem C / "Murat" vakası, GENEL sistem davranışı — hiçbir
# adaya/pozisyona özel değil): _preceding_question TEK BAŞINA yalnızca beyandan hemen önceki bir
# mülakatçı satırını görüyordu — bu bir takip sorusu ("Peki bu konuda?") ise asıl konu bilgisi
# kayboluyor, topic_text içeriksiz kalıyor, eşleşme başarısız olup TÜM çekirdek kriterlere yayılma
# riskini artırıyordu. Aşağıdaki fonksiyon, declaration'ın index'inden GERİYE, yalnız aday/mulakatci
# rollerini dikkate alarak (baslik/sistem satırları atlanır) SINIRLI bir pencere kurar ve o pencere
# içindeki mülakatçı satırlarını birleştirir — _match_criterion_for_topic'in kendisi (name-only,
# eşik) DEĞİŞMEDİ, yalnız ona giden topic_text artık tek satır değil, sınırlı bir bağlam.
def _nearby_topic_context(declaration_index: Optional[int], transcript_view: list,
                          max_mulakatci_turns: int = 2, max_rows_back: int = 8) -> str:
    """İŞ 3 — declaration_index'ten GERİYE, yalnız role in ('aday','mulakatci') satırlarını sayarak
    (baslik/sistem satırları bütçeden düşülmeden atlanır) en fazla max_mulakatci_turns mülakatçı
    satırı TOPLANANA KADAR veya en fazla max_rows_back (aday+mülakatçı) satır geriye bakılana kadar
    (hangisi önce dolarsa) ilerler; toplanan mülakatçı satırlarını KRONOLOJİK sırayla birleştirip
    döner. Pencere KASITLI OLARAK sınırlı — eski/alakasız konuya sınırsız taşma engellenir.
    transcript_view zaten bellekte (yeni DB/LLM çağrısı YOK), tamamen deterministik."""
    if declaration_index is None or not transcript_view:
        return ""
    mulakatci_texts = []
    rows_scanned = 0
    i = declaration_index - 1
    while i >= 0 and rows_scanned < max_rows_back and len(mulakatci_texts) < max_mulakatci_turns:
        row = transcript_view[i]
        role = row.get("role")
        if role in ("aday", "mulakatci"):
            rows_scanned += 1
            if role == "mulakatci":
                t = (row.get("text") or "").strip()
                if t:
                    mulakatci_texts.append(t)
        i -= 1
    return " ".join(reversed(mulakatci_texts))

def _match_criterion_for_topic(topic_text: str, criteria_list: list) -> Optional[dict]:
    """GÖREV 2.2 — beyanın öncesindeki mülakatçı sorusunun HANGİ kriterle ilişkili olduğunu,
    kriter adı/pozisyon tanımı üzerinden (anahtar kelime örtüşmesi) belirler. Belirsizse None
    (çağıran GÖREV 2.3'e — çekirdek kriter varsayımına — düşer)."""
    if not topic_text:
        return None
    norm = _norm_name(topic_text)
    best, best_s = None, 0.0
    for c in (criteria_list or []):
        kws = [w for w in _norm_name(c.get("name", "")).split() if len(w) >= 4]
        if not kws:
            continue
        sc = sum(1 for w in kws if w in norm) / len(kws)
        if sc > best_s:
            best, best_s = c, sc
    return best if best_s >= 0.5 else None

def _core_criteria(criteria_list: list) -> list:
    """GÖREV 2.3 — eşleştirme belirsizse pozisyonun ÇEKİRDEK (en yüksek ağırlıklı) kriterleri."""
    if not criteria_list:
        return []
    max_w = max(_safe_int(c.get("weight")) for c in criteria_list)
    return [c for c in criteria_list if _safe_int(c.get("weight")) == max_w]

def apply_scope_clamp_transcript_wide(table_text: str, criteria_list: list, transcript_view: list, id_prefix: str,
                                      candidate_id: int, level: int) -> tuple:
    """GÖREV 2 — apply_structured_rationale_gate'ten SONRA, BAĞIMSIZ ikinci bir geçiş: transkript
    genelinde alan dışı/devretme beyanı ara, ilgili kritere (veya belirsizse çekirdek kriterlere)
    bağla, puan hâlâ tavanın %25'ini aşıyorsa DETERMİNİSTİK kelepçele. GÖREV 2.4 — sessiz
    uygulanmaz, her kelepçe candidate_id/level ile birlikte döndürülen log'a yazılır (çağıran
    record_system_decision'a iletir). Dönüş: (yeni_table_text, yeni_score_veya_None, log[])."""
    if not table_text or not criteria_list:
        return table_text, None, []
    declarations = find_scope_declarations(transcript_view)
    if not declarations:
        return table_text, None, []
    targets = {}
    log = []
    # İŞ 3 — GÜVENİLİR eşleşme yoksa artık _core_criteria'ya BROADCAST edilmiyor (eski davranış
    # KALDIRILDI). Declaration KAYBOLMAZ: 'log'a (ve dolayısıyla çağıranın record_system_decision
    # çağrısına) görünür bir 'unresolved' kaydı düşülür, puan/tablo DEĞİŞTİRİLMEZ. Belirsiz kanıtın
    # birden fazla kriterin puanını düşürmesindense değiştirilmeden görünür loglanması tercih edilir.
    for d in declarations:
        topic_text = _nearby_topic_context(d.get("index"), transcript_view)
        matched = _match_criterion_for_topic(topic_text, criteria_list)
        if matched:
            targets.setdefault(matched["name"], []).append({**d, "eslesme": "konu"})
        else:
            log.append({"kriter": None, "kimlik": None, "sonuc": "scope_declaration_unresolved",
                       "damga": d.get("ts"), "beyan": d.get("text"),
                       "not": "scope declaration bulundu fakat yakın konuşma bağlamından güvenilir bir kritere eşleştirilemedi — kelepçe UYGULANMADI"})
    if not targets:
        return table_text, None, log

    lines = table_text.splitlines()
    used_lines = set()
    row_info = []
    changed = False
    for idx, c in enumerate(criteria_list, start=1):
        cid = f"{id_prefix}{idx}"
        cap = _safe_int(c.get("weight"))
        cname = c.get("name", "")
        if cap <= 0 or not cname:
            continue
        best_i, best_s = None, 0.0
        for i, ln in enumerate(lines):
            if i in used_lines or ln.count("|") < 2:
                continue
            cells = [x.strip() for x in ln.strip().strip("|").split("|")]
            if len(cells) < 3:
                continue
            c0 = _norm_name(re.sub(r"[*_`]", "", cells[0]))
            if len(c0) < 2:
                continue
            sc = _name_score(cname, cells[0])
            if sc > best_s:
                best_i, best_s = i, sc
        if best_i is None or best_s < 0.34:
            continue
        used_lines.add(best_i)
        cells = [x.strip() for x in lines[best_i].strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        award_m = re.search(r"(?<![\d/])(\d+)\s*/\s*(\d+)(?![\d/])", cells[1])
        if not award_m:
            row_info.append((cap, None))
            continue
        awarded = _safe_int(award_m.group(1))
        row_info.append((cap, awarded))
        decls = targets.get(cname)
        if not decls:
            continue
        capped = max(0, int(cap * _OUT_OF_SCOPE_SCORE_CAP_RATIO))
        if awarded > capped:
            d0 = decls[0]
            cells[1] = f"{capped}/{cap}"
            lines[best_i] = "| " + " | ".join(cells) + " |"
            log.append({"kriter": cname, "kimlik": cid, "sonuc": "transkript_geneli_kelepce",
                       "damga": d0.get("ts"), "beyan": d0.get("text"), "eslesme": d0.get("eslesme"),
                       "onceki_puan": f"{awarded}/{cap}", "yeni_puan": f"{capped}/{cap}"})
            row_info[-1] = (cap, capped)
            changed = True

    new_score = None
    if changed:
        awarded_sum = sum(a for _, a in row_info if a is not None)
        denom = sum(cap for cap, a in row_info if a is not None)
        # İŞ EMRİ — NİHAİ RAPOR TUTARLILIĞI: TEK canonical yuvarlama (_round_half_up, madde 5).
        new_score = max(0, min(100, _round_half_up(awarded_sum / denom * 100))) if denom > 0 else None
        body = "\n".join(lines)
        total_re = (r"(\*\*\s*PROF\S*\s+PUANI\s*[:：]\s*)(\d+)(\s*/\s*)(\d+)(\s*\*\*)" if id_prefix == "K"
                   else r"(\*\*\s*TOPLAM\s+PUAN\s*[:：]\s*)(\d+)(\s*/\s*)(\d+)(\s*\*\*)")
        if new_score is not None:
            body = re.sub(total_re, lambda m: f"{m.group(1)}{new_score}{m.group(3)}100{m.group(5)}", body, count=1, flags=re.IGNORECASE)
        lines = body.splitlines()

    return "\n".join(lines), new_score, log

# ---- GÖREV 4 — Yönetici Özeti: AYRI, bağımsız bir uzunluk doğrulayıcı (kriter gerekçesi
# sistemiyle KARIŞTIRILMAZ — iş emri açıkça "ayrı akış" istiyor). ----
def regenerate_yonetici_ozeti(candidate_id: int, level: int, provider: str, model: str, current_text: str,
                              word_count: int, transcript_text: str, extra_context: str = "") -> Optional[str]:
    """GÖREV 4 — Yönetici Özeti hedef aralık (150-250 kelime) dışındaysa VEYA yasaklı klişe
    içeriyorsa TEK deneme ile yeniden ürettirir; mevcut içeriği koruyarak genişlet/kısalt. Başarısız
    → None (çağıran olduğu gibi basar + loglar — OTOMATİK kısaltma/uzatma YAPILMAZ, cümle ortadan
    kesilmez).
    İş emri — KAYIP ANLATI BÖLÜMLERİ / GÖREV 2.1 (2026-09, sonraki tur) — OLASI KÖK NEDEN (canlı
    log yok, kesin kanıtlanamadı ama en olası açıklama): kısa (ör. 110 kelime) bir özeti "yeni
    bilgi UYDURMA" kısıtı ALTINDA 150-250 kelimeye GENİŞLETMEK modelden zor bir görev istiyordu —
    modelin elinde GENİŞLETECEK somut ek malzeme YOKTU (yalnız kısa özetin kendisi + ham
    transkript verilmişti, model transkripti YENİDEN taramak zorunda kalıyordu). Fix: extra_context
    (kriter tablosundan kısa, GERÇEK bulgular) artık AYRICA veriliyor — model UYDURMADAN
    genişletebileceği somut malzemeye doğrudan sahip oluyor."""
    _extra_block = (f"\n\n=== EK GERÇEK BULGULAR (genişletirken BUNLARDAN gerçek olanları kullanabilirsin, "
                    f"yalnız transkriptte GERÇEKTEN doğrulanmış olanları — uydurma YAPMA) ===\n{extra_context[:3000]}") if extra_context else ""
    prompt = f"""Aşağıdaki 'Yönetici Özeti' metni {word_count} kelime — hedef aralık 150-250 kelime dışında OLABİLİR ve/veya yasaklı klişe/geleceğe-dönük-beklenti ifadesi içerebilir. İçeriği KORUYARAK (yeni bilgi UYDURMA, var olan gerçek bilgiyi SİLME) metni 150-250 kelime aralığına GENİŞLET/KISALT; klişe ifade varsa somut, transkript kanıtına dayalı cümleyle DEĞİŞTİR; "aday X yapmalı/sunmalı/göstermeli" gibi geleceğe dönük beklenti cümlesi varsa "aday NE YAPTI/GÖSTERDİ" biçimine çevir. Karar/öneri kelimesi (Reddet/İşe Al/Değerlendir) YAZMA. Dakika damgası kullanma.

MEVCUT METİN ({word_count} kelime):
{current_text}

=== TRANSKRİPT (ek bağlam için) ===
{(transcript_text or '')[:8000]}
{_extra_block}

SADECE yeni Yönetici Özeti metnini yaz (başlık/etiket/tırnak EKLEME, açıklama yapma)."""
    try:
        if provider == "claude":
            if not ANTHROPIC_API_KEY:
                return None
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=60.0)
            resp = client.messages.create(model=model or "claude-sonnet-4-6", max_tokens=600, temperature=0,
                                          messages=[{"role": "user", "content": prompt}])
            record_anthropic_usage(candidate_id, level, model or "claude-sonnet-4-6", "yonetici_ozeti_retry", resp)
            raw = resp.content[0].text
        elif provider == "openai":
            if not OPENAI_API_KEY:
                return None
            resp = openai_call("POST", "https://api.openai.com/v1/chat/completions",
                               json_body={"model": model or OPENAI_REPORT_MODEL,
                                          "messages": [{"role": "user", "content": prompt}],
                                          "max_tokens": 600, "temperature": 0},
                               timeout=45.0, step="yonetici_ozeti_retry", severity="background", retry=False,
                               context={"candidate_id": candidate_id, "level": level})
            result = resp.json()
            record_openai_chat_usage(candidate_id, level, model or OPENAI_REPORT_MODEL, "yonetici_ozeti_retry", result)
            raw = result["choices"][0]["message"]["content"]
        else:
            return None
    except Exception as ex:
        print(f"UYARI (regenerate_yonetici_ozeti c={candidate_id} L{level}): {type(ex).__name__}: {ex}")
        return None
    # GÖREV 2.1 (devam) — DEFANSİF TEMİZLİK: model talimata rağmen bir başlık/tırnak/açıklama
    # satırı eklerse (ör. 'Yönetici Özeti:' veya tırnak içinde döndürme) bu, kelime sayımını VE
    # klişe taramasını BOZAR — temizlenmeden kullanmak retry'ın kendisini anlamsız kılabilir.
    cleaned = (raw or "").strip()
    cleaned = re.sub(r'(?im)^\s*(?:\*\*)?yönetici özeti(?:\*\*)?\s*[:：]?\s*\n?', '', cleaned).strip()
    cleaned = cleaned.strip('"“”\'')
    return cleaned or None

# İŞ 6D — YÖNETİCİ ÖZETİ ANORMAL/GEÇERSİZ CEVAP KORUMASI: regenerate_yonetici_ozeti()'in çıktısı
# eskiden yalnız truthiness (`if _new_yo:`) ile kabul ediliyordu — HTTP 200 ile gelen 2-30
# kelimelik anormal/bozuk (ama boş-olmayan) bir cevap, mevcut DÜZGÜN Yönetici Özeti'nin üzerine
# doğrudan yazılabiliyordu. Bu eşik prompt'un hedefi olan 150-250 kelimeyi HARD GATE yapmıyor —
# 130-140 kelimelik gerçek/kullanılabilir bir özeti gereksiz reddetmemek için kasıtlı olarak
# gevşek: 80 kelime, akıl yürütmeyle (empirik production ölçümü YOK — bu oturumdan production
# log/DB erişimi yok) seçilmiş bir tampon — gerçek bir model denemesi (kusurlu olsa bile) 150-250
# hedefini bu kadar büyük oranda ıskalaması beklenmez, gerçek bir anomali ise genelde çok daha
# küçük kalır (2-30 kelime). 150-250 dışı ama >=80 kalan özetler için var olan
# 'yonetici_ozeti_uzunluk_disi' log-only kalibrasyon kontrolü (main.py, finalize_interview)
# DEĞİŞMEDEN çalışmaya devam ediyor — bu kapı onun YERİNE geçmiyor, yalnız ÖNÜNE bir güvenlik
# ağı ekliyor. Kasıtlı olarak yalnız kelime SAYISI — ek semantic/NLP doğrulayıcı YOK.
YONETICI_OZETI_USABLE_MIN_WORDS = 80

def _yonetici_ozeti_usable(text: Optional[str]) -> bool:
    """İŞ 6D — regenerate_yonetici_ozeti() çıktısı mevcut özetin üzerine yazılmaya değer mi?
    Boş/whitespace veya None -> DOĞAL OLARAK geçersiz. Kelime sayısı YONETICI_OZETI_USABLE_MIN_WORDS
    altındaysa geçersiz. Başka hiçbir kontrol YOK (kasıtlı — bkz. yukarıdaki gerekçe)."""
    t = (text or "").strip()
    if not t:
        return False
    return len(t.split()) >= YONETICI_OZETI_USABLE_MIN_WORDS

# ---- GÖREV 3 — Takip Mülakatı Soruları: '[dayanak: mm:ss]' ile transkript-temelli doğrulama ----
_DAYANAK_RE = re.compile(r"^\s*[-*•]?\s*\[dayanak\s*:\s*(\d{1,3}:[0-5]\d)\]", re.IGNORECASE)

def _followup_line_grounded(line: str, transcript_view: list) -> bool:
    m = _DAYANAK_RE.search(line)
    if not m:
        return False
    return check_timestamp_grounded(m.group(1), transcript_view, role="mulakatci")

def strip_dayanak_tag(line: str) -> str:
    """'[dayanak: mm:ss]' etiketi İÇ doğrulama içindir — okuyucuya BASILMAZ, temiz satır döner."""
    return _DAYANAK_RE.sub("", line).strip()

# ---- GÖREV 3 — Gelişim Alanları: RİSK paragraflarının transkript-temelli doğrulanması ----
def _strip_ungrounded_risk_paragraphs(text: str, transcript_view: list) -> tuple:
    """RİSK: ile başlayan bir paragrafın İÇİNDE transkriptte GERÇEKTEN var olan bir [mm:ss] damgası
    yoksa (uydurma/dayanaksız risk iddiası — GÖREV 3), o paragraf ÇIKARILIR. Dönüş: (yeni_metin,
    çıkarılan_paragraflar[])."""
    if not text or "RİSK" not in _tr_upper(text):
        return text, []
    paras = re.split(r"\n\s*\n", text)
    kept, dropped = [], []
    for p in paras:
        if _tr_upper(p).lstrip().startswith("RİSK"):
            ts_list = [f"{m.group(1)}:{m.group(2)}" for m in _TS_RE.finditer(p)]
            if not ts_list or not any(check_timestamp_grounded(t, transcript_view) for t in ts_list):
                dropped.append(p.strip()[:200])
                continue
        kept.append(p)
    return "\n\n".join(kept).strip(), dropped

def render_scope_risk_paragraph(flagged: list) -> str:
    """GÖREV 5.5 — alan dışı/devretme beyanı tespit edilip modelin KENDİSİ bunu Gelişim Alanları'na
    RİSK olarak yazmadıysa, sistem bunu KENDİSİ ekler (sessizce geçilemez — 'işe alım kararını
    belirleyen en önemli sinyal'). `flagged`: [{"kriter","kanit"}]."""
    parts = []
    for f in flagged:
        _label = _scope_declaration_label(f.get("kanit", ""))
        if _label == "devretme":
            _iddia = "kararı başkasına devrettiğini"
        elif _label == "alan dışı":
            _iddia = "bu alanın kendi mesleki alanı olmadığını"
        else:
            _iddia = "bu alanın kendi sorumluluğunda olmadığını veya kararı başkasına devrettiğini"
        parts.append(f"RİSK: \"{f['kriter']}\" kriterinde aday, {_iddia} beyan etti ({f['kanit']}). "
                     f"Pozisyonun bu alandaki beklentisi bu mülakatta doğrulanamadı.")
    return "\n\n".join(parts)

# ---- GÖREV 5 EK — Güçlü Yönler: dayanaksız/damgasız genel övgü cümleleri ----
_UNSUPPORTED_STRENGTH_RE = re.compile(
    r"[öo]rneklerle destekle(?:mi[şs]tir|di)|beklenmi[şs]tir", re.IGNORECASE)

# GÖREV 5 EK — reviewer_contradiction_unresolved: müfettişin serbest metninde ana rapordan
# ALINTILADIĞI ("...") bir iddiayı desteklenmiyor/tutarlı değil/abartılı diye işaretlediği
# kalıp (bkz. append_reviewer_section).
_STR_QUOTE_UNSUPPORTED_RE = re.compile(
    r'["“]([^"”]{15,200})["”][^.\n]{0,60}(?:desteklenmiyor|desteklenmemektedir|'
    r'kar[şs][ıi]l[ıi][ğg][ıi]\s+yok|transkriptte\s+yok|tutarl[ıi]\s+de[ğg]il|abart[ıi]l[ıi])',
    re.IGNORECASE)

def _strip_unsupported_strength_sentences(text: str) -> tuple:
    """GÖREV 5 EK (unsupported_strength) — bir cümle 'örneklerle desteklemiştir' gibi genel bir
    övgü kalıbı içeriyor AMA cümlenin kendi içinde [mm:ss] damgası YOKSA (yani GERÇEKTEN hangi
    örnek olduğu belirtilmemiş — bu turun raporunda görülen 'bu deneyimini mülakat sırasında
    belirttiği örneklerle desteklemiştir' tam olarak budur), o cümle ÇIKARILIR. Dönüş:
    (yeni_metin, çıkarılan_cümleler[])."""
    if not text:
        return text, []
    sents = re.split(r'(?<=[.!?])\s+', text)
    kept, dropped = [], []
    for s in sents:
        if _UNSUPPORTED_STRENGTH_RE.search(s) and not _TS_RE.search(s):
            dropped.append(s.strip())
            continue
        kept.append(s)
    return " ".join(kept).strip(), dropped

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
    # TUR 4 / GÖREV 2 — HAM transkript BİR KEZ yazılır (write-once, tüm seviyeler için TEK
    # ortak nokta). L2/L3-sesli akışta create_l2_report bunu daha erken (temizlemeden ÖNCE)
    # zaten yapmış olabilir — capture_transcript_raw dolu sütuna ASLA yazmadığı için burada
    # tekrar çağrılması zararsızdır. L1/L3-metin akışında bu, tek yazma noktasıdır.
    try:
        _raw_at_finish = transcript_to_text(build_transcript_view(
            interview["messages"] if "messages" in interview.keys() else "[]", level,
            interview["started_at"] if "started_at" in interview.keys() else None))
        capture_transcript_raw(candidate_id, level, _raw_at_finish)
    except Exception as e:
        print(f"UYARI (run_deferred_finish_job ham transkript yakalama c={candidate_id} L{level}): {type(e).__name__}: {e}")
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

        # GÖREV 8 — rapor çıktı token tavanı 8000 → 16000 (Claude Sonnet ve gpt-4o çıktı sınırının
        # altında; tam rapor + iki tablo + görüş ayrılıkları rahatça sığar). NOT: bu, önceki turda
        # eklenen REALTIME_MAX_RESPONSE_TOKENS (realtime YANIT tavanı) ile İLGİSİZDİR.
        REPORT_MAX_TOKENS = 16000
        if provider == "claude":
            if not ANTHROPIC_API_KEY:
                raise RuntimeError("ANTHROPIC_API_KEY tanımlı değil")
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=120.0)
            # TUR 4 / GÖREV 4.2+4.4 — rapor üretimi bir DEĞERLENDİRME görevi; temperature=0 ile
            # AYNI transkript AYNI puanı üretsin diye en deterministik ayara çekildi (eskiden bu
            # dalda temperature hiç verilmiyordu → SDK varsayılanı 1.0 kullanılıyordu).
            response = client.messages.create(
                model=model or "claude-sonnet-4-6", max_tokens=REPORT_MAX_TOKENS, temperature=0,
                system=cached_system(system) if system else anthropic.NOT_GIVEN,
                messages=[{"role": "user", "content": primary_payload}]
            )
            record_anthropic_usage(candidate_id, level, model or "claude-sonnet-4-6", "report_generation_primary", response)
            reply = response.content[0].text
            # TUR 2 / GÖREV A.3 — gerçek çıktı token'ı + stop_reason her zaman loglanır.
            _u = getattr(response, "usage", None)
            print(f"[REPORT_USAGE] c={candidate_id} L{level} provider=claude model={model or 'claude-sonnet-4-6'} "
                  f"out_tokens={getattr(_u, 'output_tokens', '?')} stop_reason={getattr(response, 'stop_reason', '?')} "
                  f"max_tokens={REPORT_MAX_TOKENS} bitti={'---RAPORSON---' in reply}")
            # GÖREV 8 — KESİLME → FALLBACK'E DÜŞMEDEN ÖNCE DEVAM ÇAĞRISI (continuation).
            _cont_tries = 0
            while (getattr(response, "stop_reason", None) == "max_tokens" or "---RAPORSON---" not in reply) and _cont_tries < 2:
                _cont_tries += 1
                print(f"[REPORT_CONTINUATION] c={candidate_id} L{level} deneme {_cont_tries}")
                _cont = client.messages.create(
                    model=model or "claude-sonnet-4-6", max_tokens=REPORT_MAX_TOKENS, temperature=0,
                    system=cached_system(system) if system else anthropic.NOT_GIVEN,
                    messages=[{"role": "user", "content": primary_payload},
                              {"role": "assistant", "content": reply},
                              {"role": "user", "content": "Kaldığın yerden AYNEN devam et; hiçbir şeyi tekrar etme, başa dönme. Raporu ---RAPORSON--- ile bitir."}]
                )
                record_anthropic_usage(candidate_id, level, model or "claude-sonnet-4-6", "report_generation_continuation", _cont)
                reply = reply + _cont.content[0].text
                response = _cont
            if getattr(response, "stop_reason", None) == "max_tokens" or "---RAPORSON---" not in reply:
                print(f"[REPORT_TRUNCATED] c={candidate_id} L{level} stop_reason={getattr(response,'stop_reason',None)} (devam çağrıları yetmedi)")
                record_system_decision(candidate_id, level, "rapor_kesildi",
                                       "Rapor üretimi token sınırına takıldı; devam çağrıları da tamamlayamadı, eksik bölümler deterministik tamamlandı.",
                                       {"stop_reason": getattr(response, "stop_reason", None), "continuation_tries": _cont_tries},
                                       warnings=["Rapor üretimi token sınırına takıldı (bkz. report_tech_note)."])
            # Normal kapanış çağrısı bile [ADAY_CIKIS_TALEBI] üretebilir (mevcut senkron
            # interview_chat akışıyla aynı davranış) — öyleyse ikinci bir "gerçek bitiş" çağrısı yap.
            if "[ADAY_CIKIS_TALEBI]" in reply:
                exit_payload = f"""ÖNCEKİ KISA HAFIZA:
{interview["compact_memory"] or "Henüz yok."}

GÖREV: Aday mülakatı sonlandırmak istediğini net şekilde belirtti (bu bir teknik arıza bildirimi de olabilir). Mülakatı şimdi bitir ve mevcut bilgilere göre raporu üret. Adayı ikna etmeye çalışma, sadece elindeki bilgiyle adil bir değerlendirme yap; eksik kalan kısımları düşük puan nedeni yapma, sadece "yeterli veri toplanamadı" notu düş. [MÜLAKATBİTTİ] etiketini kullan."""
                exit_response = client.messages.create(
                    model=model or "claude-sonnet-4-6", max_tokens=REPORT_MAX_TOKENS, temperature=0,
                    system=cached_system(system) if system else anthropic.NOT_GIVEN,
                    messages=[{"role": "user", "content": exit_payload}]
                )
                record_anthropic_usage(candidate_id, level, model or "claude-sonnet-4-6", "report_generation_primary", exit_response)
                reply = exit_response.content[0].text
                terminated_reason = terminated_reason or "Aday talebiyle erken sonlandırıldı"
        elif provider == "openai":
            # İŞ EMRİ — FINAL EVALUATION ARCHITECTURE: L1 birincil rapor/değerlendirme artık bu dala
            # (OpenAI) giriyor ve L1'in 'system' alanı (get_system_prompt — pozisyon/kriter/CV/
            # talimat) DOLU geliyor; L2/L3'te 'system' zaten None (build_l2_report_prompt TEK
            # kendi-içinde-tam user mesajı üretir) — bu satır L2/L3 davranışını DEĞİŞTİRMEZ, yalnız
            # L1 için system'in SESSİZCE ATLANMASINI (ve dolayısıyla pozisyon/kriterlerin modele hiç
            # gitmemesini) önler.
            _msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": primary_payload}]
            resp = openai_call(
                "POST", "https://api.openai.com/v1/chat/completions",
                json_body={"model": model or OPENAI_REPORT_MODEL, "messages": _msgs, "max_tokens": REPORT_MAX_TOKENS, "temperature": 0},  # GÖREV 4.4 — determinizm
                timeout=150.0, step="report_generation", severity="user", retry=True,
                context={"candidate_id": candidate_id, "level": level},
            )
            result = resp.json()
            record_openai_chat_usage(candidate_id, level, model or OPENAI_REPORT_MODEL, "l2_report_generation_deferred", result)
            reply = result["choices"][0]["message"]["content"]
            _fr = (result.get("choices") or [{}])[0].get("finish_reason")
            # TUR 2 / GÖREV A.3 — gerçek çıktı token'ı + finish_reason her zaman loglanır.
            _uo = (result.get("usage") or {})
            print(f"[REPORT_USAGE] c={candidate_id} L{level} provider=openai model={model or OPENAI_REPORT_MODEL} "
                  f"completion_tokens={_uo.get('completion_tokens', '?')} finish_reason={_fr} "
                  f"max_tokens={REPORT_MAX_TOKENS} bitti={'---RAPORSON---' in reply}")
            # İŞ 6A — TEŞHİS LOGU (yalnız log, davranış DEĞİŞMEDİ): çıktı anormal derecede kısaysa
            # (<=50 token VEYA metin <100 karakter) modelin GERÇEK kısa cevabını Railway stdout'a
            # bas — yalnız MODEL cevabı, CV/transcript/prompt ASLA loglanmaz. DB'ye yazılmaz.
            _out_tok_primary = _safe_int(_uo.get('completion_tokens', 0))
            if _out_tok_primary <= 50 or len((reply or "").strip()) < 100:
                print(f"[REPORT_SHORT_RESPONSE] c={candidate_id} L{level} action=primary finish={_fr} "
                      f"out={_out_tok_primary} text={repr(reply)[:500]}")
            # İŞ 6B — ANORMAL KISA CEVAPTA FULL-CONTEXT CONTINUATION YAPMA: finish_reason=="stop" +
            # ---RAPORSON--- yok + completion_tokens<=50 GERÇEK bir kesilme (length) DEĞİL — modelin
            # anormal şekilde erken durması. Bu durumda ~16K bağlamı continuation olarak tekrar
            # göndermek TPM/429'a çarpıyordu (bkz. teşhis turu). Bunun yerine AYNI parametrelerle
            # yalnız TEK bir retry yapılır; retry de anormal kısa kalırsa continuation'a HİÇ
            # girilmeden kontrollü failure — mevcut raw_report EZİLMEZ (bu noktadan sonra fonksiyon
            # return ile çıkar, raw_report write'ına hiç ulaşılmaz).
            _is_anomalous_short = (_fr == "stop" and "---RAPORSON---" not in reply and _out_tok_primary <= 50)
            if _is_anomalous_short:
                print(f"[REPORT_SHORT_RETRY] c={candidate_id} L{level} attempt=1")
                resp_retry = openai_call(
                    "POST", "https://api.openai.com/v1/chat/completions",
                    json_body={"model": model or OPENAI_REPORT_MODEL, "messages": _msgs, "max_tokens": REPORT_MAX_TOKENS, "temperature": 0},  # GÖREV 4.4 — determinizm, primary ile AYNI parametreler
                    timeout=150.0, step="report_generation", severity="user", retry=True,
                    context={"candidate_id": candidate_id, "level": level},
                )
                result_retry = resp_retry.json()
                record_openai_chat_usage(candidate_id, level, model or OPENAI_REPORT_MODEL, "l2_report_generation_short_retry", result_retry)
                reply_retry = result_retry["choices"][0]["message"]["content"]
                _fr_retry = (result_retry.get("choices") or [{}])[0].get("finish_reason")
                _uo_retry = (result_retry.get("usage") or {})
                _out_tok_retry = _safe_int(_uo_retry.get('completion_tokens', 0))
                print(f"[REPORT_USAGE] c={candidate_id} L{level} provider=openai model={model or OPENAI_REPORT_MODEL} "
                      f"completion_tokens={_out_tok_retry} finish_reason={_fr_retry} "
                      f"max_tokens={REPORT_MAX_TOKENS} bitti={'---RAPORSON---' in reply_retry} (short-retry)")
                _retry_still_anomalous = (_fr_retry == "stop" and "---RAPORSON---" not in reply_retry and _out_tok_retry <= 50)
                if _retry_still_anomalous:
                    print(f"[REPORT_SHORT_FAILED] c={candidate_id} L{level} out1={_out_tok_primary} out2={_out_tok_retry} "
                          f"text2={repr(reply_retry)[:500]}")
                    _mark_finish_failed(candidate_id, level,
                                        "Rapor üretimi iki denemede de anormal derecede kısa cevap döndürdü "
                                        "(olası model/API anomalisi) — continuation'a girilmedi, eski rapor korunuyor.")
                    return
                # Retry ya TAM raporu üretti (---RAPORSON--- var) ya da GERÇEKTEN uzun/kesilmiş bir
                # cevaba döndü (ör. finish=length) — her iki durumda da madde 5 gereği NORMAL pipeline
                # (aşağıdaki, DEĞİŞMEYEN continuation döngüsü dahil) retry sonucuyla devam eder.
                reply, _fr, _uo = reply_retry, _fr_retry, _uo_retry
            # GÖREV 8 — KESİLME → FALLBACK'E DÜŞMEDEN ÖNCE DEVAM ÇAĞRISI (continuation).
            _cont_tries = 0
            while (_fr == "length" or "---RAPORSON---" not in reply) and _cont_tries < 2:
                _cont_tries += 1
                print(f"[REPORT_CONTINUATION] c={candidate_id} L{level} deneme {_cont_tries}")
                _cmsgs = _msgs + [{"role": "assistant", "content": reply},
                                  {"role": "user", "content": "Kaldığın yerden AYNEN devam et; hiçbir şeyi tekrar etme, başa dönme. Raporu ---RAPORSON--- ile bitir."}]
                _cr = openai_call("POST", "https://api.openai.com/v1/chat/completions",
                                  json_body={"model": model or OPENAI_REPORT_MODEL, "messages": _cmsgs, "max_tokens": REPORT_MAX_TOKENS, "temperature": 0},  # GÖREV 4.4
                                  timeout=150.0, step="report_continuation", severity="user", retry=True,
                                  context={"candidate_id": candidate_id, "level": level}).json()
                record_openai_chat_usage(candidate_id, level, model or OPENAI_REPORT_MODEL, "l2_report_continuation", _cr)
                _cont_text = _cr["choices"][0]["message"]["content"] or ""
                _fr = (_cr.get("choices") or [{}])[0].get("finish_reason")
                # İŞ 6A — TEŞHİS LOGU: continuation cevabının KENDİSİ (kümülatif değil) anormal
                # kısaysa aynı şekilde logla.
                _out_tok_cont = _safe_int((_cr.get('usage') or {}).get('completion_tokens', 0))
                if _out_tok_cont <= 50 or len(_cont_text.strip()) < 100:
                    print(f"[REPORT_SHORT_RESPONSE] c={candidate_id} L{level} action=continuation finish={_fr} "
                          f"out={_out_tok_cont} text={repr(_cont_text)[:500]}")
                reply = reply + _cont_text
            if _fr == "length" or "---RAPORSON---" not in reply:
                print(f"[REPORT_TRUNCATED] c={candidate_id} L{level} finish_reason={_fr} (devam çağrıları yetmedi)")
                record_system_decision(candidate_id, level, "rapor_kesildi",
                                       "Rapor üretimi token sınırına takıldı; devam çağrıları da tamamlayamadı, eksik bölümler deterministik tamamlandı.",
                                       {"finish_reason": _fr, "continuation_tries": _cont_tries},
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

        # NOT (2026-09 rapor yeniden tasarımı): eski "bağımsız profil çağrısı" (Kişisel ve Bilişsel
        # Profil'i Pozisyon Yetkinlikleri sonucunu görmeden AYRI bir LLM çağrısında üretme) KALDIRILDI.
        # Yeni mimaride her iki bölüm zaten TEK çağrıda ===BAŞLIK=== ayraçlarıyla AYRI, İZOLE metin
        # blokları olarak üretiliyor ve finalize_interview'da her biri KENDİ tablosu üzerinden ayrı
        # ayrı normalize ediliyor (bkz. recompute_and_fix_score / recompute_profile_section) — bir
        # bölümün puanı diğerinin puanını hesaba katmıyor zaten. Karar: ek çağrının getirdiği maliyet/
        # karmaşıklık bu mimaride gerekli değil (iş emri madde 21 zaten "hiçbir katman diğerinin
        # puanını değiştirmemeli" diyor — bu izolasyon parse seviyesinde sağlanıyor, ayrı çağrı gerekmez).

        # ═══ GÖREV 1.2 — MÜFETTİŞİN PUANA MÜDAHALESİ KALDIRILDI ═══
        # Eskiden burada müfettiş (ikinci model) ham taslağı görüp _REVIEWER_HAIRCUT (%40) ile
        # kriter puanı düşürüyordu; "kanıtsız iddia" (ters yön) ile "puanı fazla düşük" (ters yön)
        # listeleri aynı düşürme fonksiyonuna giriyordu (bozuk eşleştirme). BU TÜM YOL SİLİNDİ.
        # Puan = GPT'nin ham puanı. Sunucu YALNIZCA aritmetik bütünlük uygular (finalize_interview
        # içindeki recompute_and_fix_score: kriter tavanını aşan puanı sabitler, TOPLAM = kriter
        # toplamı yapar, normalize eder — bu bir YARGI revizyonu değildir).

        # ═══ GÖREV 1.3 — NİHAİ RAPORU ÜRET (karar + tüm deterministik düzenleme burada biter) ═══
        finalize_interview(candidate_id, reply, terminated_reason=terminated_reason, level=level, regen=regen)

        # ═══ İŞ EMRİ — FINAL EVALUATION ARCHITECTURE / DEĞİŞMEZ LEVEL MİMARİSİ ═══
        # Second evaluator (Claude reviewer) ve Final Report Quality Gate ARTIK YALNIZ L3'te
        # çalışır — L1/L2'de bu adımlar HİÇ ÇAĞRILMAZ (gereksiz AI çağrısı/maliyet YOK). Çağrı
        # sitesindeki bu 'if level == 3' kontrolüne EK olarak her iki fonksiyon da KENDİ İÇİNDE
        # level != 3 ise erken döner (savunma amaçlı ikinci kapı — doğrudan/beklenmedik çağrıya
        # karşı da güvenli).
        _reviewer_findings = {}
        if level == 3:
            # Müfettiş artık taslağı DEĞİL, basılacak nihai raporu (kriter tabloları + KARAR +
            # gerekçe dahil) görür. Atlanır/patlarsa rapor DENETÇİSİZ ve DEĞİŞMEDEN kalır.
            try:
                _reviewer_findings = append_reviewer_section(candidate_id, level, transcript_text, modality_block, _pcrit) or {}
            except Exception as e:
                print(f"UYARI (müfettiş bölümü ekleme c={candidate_id} L{level}): {type(e).__name__}: {e}")

        # İŞ 5 — validator+reviewer+takeover TAMAMLANDIKTAN SONRA, yalnız 'Öne Çıkan Proje ve
        # Deneyimler' hâlâ fallback'teyse TEK hedefli recovery denemesi (tüm level'larda çalışır —
        # bu iş emrinin kapsamı DIŞINDA, DEĞİŞMEDİ).
        try:
            run_one_cikan_proje_recovery(candidate_id, level, _pcrit)
        except Exception as e:
            print(f"UYARI (one_cikan_proje recovery c={candidate_id} L{level}): {type(e).__name__}: {e}")

        # İŞ EMRİ — L3 RAPOR AKIŞINI SADELEŞTİR: Final QA/QG AI çağrısı DEVREDEN ÇIKARILDI (bilinçli,
        # geri alınabilir — geri almak için "False and " kısmını silmek yeterli). Fonksiyonun kendisi
        # SİLİNMEDİ/değiştirilmedi; yalnız bu çağrı sitesi artık tetiklenmiyor, dolayısıyla Claude'dan
        # sonra ayrı bir OpenAI Quality Gate AI çağrısı ARTIK OLUŞMUYOR. Deterministic final integrity
        # check (aşağıda, AI çağrısı yapmaz) buna DOKUNULMADAN aynen çalışmaya devam eder.
        if False and level == 3:
            # ═══ FINAL REPORT QUALITY GATE (pipeline'ın EN SONU, yalnız L3) ═══
            # Evaluator/validator/reviewer/takeover/proje-kurtarma TAMAMLANDIKTAN SONRA çalışan
            # bağımsız SON kalite denetçisi. Hata/atlama → rapor DEĞİŞMEDEN kalır (fail-closed,
            # retry YOK — bkz. fonksiyon docstring'i).
            try:
                run_final_report_quality_gate(candidate_id, level, _pcrit, _reviewer_findings)
            except Exception as e:
                print(f"UYARI (final report quality gate c={candidate_id} L{level}): {type(e).__name__}: {e}")

        # ═══ İŞ EMRİ — madde 11: FINAL DETERMINISTIC INTEGRITY GATE (pipeline'ın GERÇEK SONU,
        # TÜM level'larda — AI Quality Gate'ten SONRA, ama L1/L2'de kendisi de zaten no-op'a
        # yakın çalışır çünkü reviewer alanları hiç dolmaz). AI ÇAĞRISI YAPMAZ. FAIL olsa bile
        # rapor SİLİNMEZ/geri ALINMAZ — yalnız işaretlenir, pipeline ÇÖKMEZ.
        try:
            run_final_deterministic_integrity_check(candidate_id, level)
        except Exception as e:
            print(f"UYARI (final integrity check c={candidate_id} L{level}): {type(e).__name__}: {e}")

        # İŞ EMRİ — PRIMARY DEĞERLENDİRME VE KANIT SEÇİMİ GÜVENİLİRLİĞİ / FAZ 4 madde 19-22:
        # kaynaksız [mm:ss] damgalarının KULLANICIYA GÖSTERİLEN final report'tan kaldırılması —
        # BİLEREK run_final_deterministic_integrity_check'TEN SONRA çalışır (grounding kontrolü
        # HAM/temizlenmemiş final report üzerinde çalışmış, final_integrity_status/grounding_fail
        # ZATEN persist edilmiş olmalı — bu adım onları HİÇ okumaz/değiştirmez). raw_report bu
        # noktada çoktan (madde 19, ~line 8724) değişmeden persist edilmişti, BURADAN ETKİLENMEZ.
        # NORMAL FINISH ve REGENERATE bu fonksiyonu (run_deferred_finish_job) PAYLAŞTIĞI için tek
        # bir çağrı noktası HER İKİ yol için de AYNI sırayı garanti eder.
        try:
            _db_ts = get_db()
            try:
                _iv_ts = _db_ts.execute(
                    "SELECT report, transcript_raw FROM interviews WHERE candidate_id=? AND level=?",
                    (candidate_id, level)).fetchone()
            finally:
                _db_ts.close()
            if _iv_ts and _iv_ts["report"]:
                _cleaned_report = _strip_unsourced_timestamps_for_display(_iv_ts["report"], _iv_ts["transcript_raw"] or "")
                if _cleaned_report != _iv_ts["report"]:
                    _db_ts2 = get_db()
                    try:
                        _db_ts2.execute("UPDATE interviews SET report=? WHERE candidate_id=? AND level=?",
                                       (_cleaned_report, candidate_id, level))
                        _db_ts2.commit()
                    finally:
                        _db_ts2.close()
        except Exception as e:
            print(f"UYARI (timestamp display cleanup c={candidate_id} L{level}): {type(e).__name__}: {e}")

        # TEK DÜZELTME — L3 processing_status ZAMANLAMASI: finalize_interview L3'te bilerek
        # processing_status'u 'processing' bırakmıştı (bkz. finalize_interview) — second evaluator
        # + Quality Gate + final integrity check dahil TÜM L3 pipeline'ı burada bittiğine göre
        # EN SON 'completed' burada yazılır. L1/L2'de finalize_interview zaten yazdı, dokunulmaz.
        if level == 3:
            db_done = get_db()
            try:
                db_done.execute(
                    "UPDATE interviews SET processing_status='completed' WHERE candidate_id=? AND level=?",
                    (candidate_id, level))
                db_done.commit()
            finally:
                db_done.close()

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

def recover_stale_processing_interviews(stale_after_seconds: int = _FINISH_JOB_STALE_SECONDS):
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
    # 2026-09 rapor yeniden tasarımı — model artık ===BAŞLIK=== ayraçlı TEK gövde üretir (eski
    # ---STANDARTCV--- ayrı bloğu KALDIRILDI). parse_llm_report_sections 7 isimli bölüme ayırır;
    # eksik/'YOK' işaretli bölümler sözlükte hiç yer almaz (uydurma yok — iş emri madde 2.1).
    report_match = re.search(r'---RAPOR---([\s\S]*?)(?:---RAPORSON---|\Z)', reply)
    raw_body = report_match.group(1).strip() if report_match else ""
    sections = parse_llm_report_sections(raw_body)

    _dbc = get_db()
    candidate = _dbc.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    _ivr = _dbc.execute("SELECT criteria_coverage_json, messages, started_at, pending_finish_provider, pending_finish_model "
                        "FROM interviews WHERE candidate_id=? AND level=?",
                        (candidate_id, level)).fetchone()
    _dbc.close()
    _pos = get_position(candidate["position"]) if candidate else None
    _crit = (_pos or {}).get("criteria") or []
    try:
        _fcov = json.loads(_ivr["criteria_coverage_json"]) if (_ivr and _ivr["criteria_coverage_json"]) else None
    except Exception:
        _fcov = None
    try:
        _tview = build_transcript_view(_ivr["messages"] if _ivr else "[]", level, _ivr["started_at"] if _ivr else None)
        _ftx = transcript_to_text(_tview)
    except Exception:
        _tview, _ftx = [], ""
    # İş emri — KRİTER GEREKÇESİ: sağlayıcı/model, hedefli yeniden-üretim çağrıları (GÖREV 2.3
    # regenerate_criterion_fields / GÖREV 4 regenerate_yonetici_ozeti) için — DB'deki kayıtlı
    # pending_finish_* alanlarından (run_deferred_finish_job'ın ZATEN kullandığı sağlayıcı, aynı
    # DOKUNULMAYACAKLAR — Level 2 hattına Anthropic EKLENMEZ). Kayıt yoksa (ör. eski satır/test)
    # varsayılan: openai — İŞ EMRİ L1 OPENAI-ONLY MİMARİSİ itibarıyla L1/L2/L3'ün ÜÇÜ de birincil
    # rapor/değerlendirmede OpenAI kullanıyor (Claude yalnız L3'ün AYRI ikinci-değerlendirici
    # akışında var, bu değişken onu etkilemez) — "claude" varsayımı artık HİÇBİR seviye için doğru
    # değil, L1'e yanlışlıkla Anthropic çağrısı sızdırmasın diye kaldırıldı.
    _gate_provider = (_ivr["pending_finish_provider"] if _ivr and "pending_finish_provider" in _ivr.keys() else None) \
        or "openai"
    _gate_model = _ivr["pending_finish_model"] if _ivr and "pending_finish_model" in _ivr.keys() else None

    # --- Pozisyon Yetkinlikleri: doğrulanır + normalize edilir (motor DEĞİŞMEDİ, yalnız girdi
    #     artık İZOLE bölüm metni — split_report_regions'a gerek yok, karışma riski yok). ---
    _score_warnings = []
    _scope_flagged = []  # GÖREV 5.5 — alan dışı/devretme leksik bulguları (pozisyon+profil toplu)
    _val_log_pos, _val_log_prof = [], []  # GÖREV 1.4/1.5 (VALIDATOR KALİBRASYONU) — düşme oranı + ihlal dağılımı için
    score_position = None
    pos_table_display = ""
    pos_raw = sections.get("pozisyon_yetkinlikleri", "")
    # İş emri — PRIMARY DEĞERLENDİRME VE KANIT SEÇİMİ GÜVENİLİRLİĞİ / FAZ 1 madde 6-9: kanıt
    # havuzu (varsa) tablo metninden AYIKLANIR ve evidence'lar BİREBİR eşleşen kriter satırının
    # 3. hücresine taşınır — BUNDAN SONRAKİ recompute_and_fix_score çağrısı havuz satırlarını
    # (KRİTER:/E1:...) GÖRMEZ, yalnız temiz tabloyu görür (madde 25). Havuz yoksa/bozuksa
    # _extract_evidence_pool boş sözlük + değişmemiş metin döner — mevcut davranış AYNEN korunur.
    _pos_pool, pos_raw = _extract_evidence_pool(pos_raw, _KANIT_HAVUZU_POZISYON_START, _KANIT_HAVUZU_POZISYON_SON)
    if _pos_pool:
        pos_raw, _pos_pool_warn = _merge_evidence_pool_into_table(pos_raw, _pos_pool)
        if _pos_pool_warn:
            try:
                record_system_decision(candidate_id, level, "kanit_havuzu_eslesmedi_pozisyon",
                                       "Kanıt havuzundaki bazı kriter adları pozisyon tablosuyla birebir eşleşmedi (tahmin yapılmadı).",
                                       {"uyarilar": _pos_pool_warn})
            except Exception:
                pass
    if pos_raw and _crit:
        try:
            _fixed_pos, score_position, _w1 = recompute_and_fix_score(
                pos_raw, _crit, extract_score(pos_raw), criteria_coverage=_fcov, transcript=_ftx,
                candidate_id=candidate_id, level=level)
            _score_warnings += list(_w1)
            # İş emri GÖREV 2 — DETERMİNİSTİK DOĞRULAYICI KAPI: yapısal G/K/E/S alanlarını denetler,
            # geçemeyeni HEDEFLİ (max 3 deneme) yeniden ürettirir, hâlâ geçemezse kriteri payda
            # dışına alır (bkz. apply_structured_rationale_gate tanımı — eski log-only
            # detect_evidence_cliches çağrısının YERİNE geçer, aşağıda kaldırıldı).
            try:
                _fixed_pos, _new_score_pos, _val_log_pos, _flag_pos = apply_structured_rationale_gate(
                    _fixed_pos, _crit, "P", _tview, _ftx, _gate_provider, _gate_model, candidate_id, level)
                _scope_flagged.extend(_flag_pos)
                if _new_score_pos is not None:
                    score_position = _new_score_pos
                if _val_log_pos:
                    record_system_decision(candidate_id, level, "kriter_gerekcesi_dogrulayici_pozisyon",
                                           "GÖREV 2 — Pozisyon Yetkinlikleri kriterleri için yapısal doğrulayıcı çalıştı (bkz. meta.kayitlar).",
                                           {"kayitlar": _val_log_pos})
            except Exception as e:
                print(f"UYARI (finalize_interview pozisyon doğrulayıcı c={candidate_id}): {type(e).__name__}: {e}")
            # İş emri GÖREV 2 (VALIDATOR KALİBRASYONU) — yapısal gate'ten BAĞIMSIZ, SONRA çalışan
            # transkript-geneli alan dışı/devretme kelepçesi (bkz. apply_scope_clamp_transcript_wide
            # tanımı — kök neden: gate yalnız O KRİTERİN kendi K/G'sine bakıyordu, beyan başka turda
            # olduğunda kaçıyordu).
            try:
                _fixed_pos, _new_score_pos2, _val_log_pos2 = apply_scope_clamp_transcript_wide(
                    _fixed_pos, _crit, _tview, "P", candidate_id, level)
                if _new_score_pos2 is not None:
                    score_position = _new_score_pos2
                if _val_log_pos2:
                    record_system_decision(candidate_id, level, "transkript_geneli_kelepce_pozisyon",
                                           "GÖREV 2 — transkript genelinde alan dışı/devretme beyanı tespit edildi, ilgili pozisyon kriteri/kriterleri kelepçelendi (bkz. meta.kayitlar).",
                                           {"kayitlar": _val_log_pos2})
            except Exception as e:
                print(f"UYARI (finalize_interview transkript geneli kelepçe pozisyon c={candidate_id}): {type(e).__name__}: {e}")
            pos_table_display = _strip_total_line_for_display(_fixed_pos, is_profile=False)
        except Exception as e:
            print(f"UYARI (finalize_interview pozisyon puanlama c={candidate_id}): {type(e).__name__}: {e}")
            pos_table_display = pos_raw
    elif pos_raw:
        pos_table_display = pos_raw

    # --- Kişisel ve Bilişsel Profil: aynı yöntemle, TAMAMEN AYRI bölüm metninden. ---
    score_profile = None
    prof_table_display = ""
    prof_raw = sections.get("profil", "")
    _prof_pool, prof_raw = _extract_evidence_pool(prof_raw, _KANIT_HAVUZU_PROFIL_START, _KANIT_HAVUZU_PROFIL_SON)
    if _prof_pool:
        prof_raw, _prof_pool_warn = _merge_evidence_pool_into_table(prof_raw, _prof_pool)
        if _prof_pool_warn:
            try:
                record_system_decision(candidate_id, level, "kanit_havuzu_eslesmedi_profil",
                                       "Kanıt havuzundaki bazı kriter adları profil tablosuyla birebir eşleşmedi (tahmin yapılmadı).",
                                       {"uyarilar": _prof_pool_warn})
            except Exception:
                pass
    if prof_raw:
        try:
            _fixed_prof, score_profile, _w2 = recompute_profile_section(
                prof_raw, transcript=_ftx, criteria_coverage=_fcov, candidate_id=candidate_id, level=level)
            _score_warnings += list(_w2)
            # İş emri GÖREV 2 — AYNI doğrulayıcı kapı, K1..K6 (PROFILE_CRITERIA) için.
            try:
                _fixed_prof, _new_score_prof, _val_log_prof, _flag_prof = apply_structured_rationale_gate(
                    _fixed_prof, PROFILE_CRITERIA, "K", _tview, _ftx, _gate_provider, _gate_model, candidate_id, level)
                _scope_flagged.extend(_flag_prof)
                if _new_score_prof is not None:
                    score_profile = _new_score_prof
                if _val_log_prof:
                    record_system_decision(candidate_id, level, "kriter_gerekcesi_dogrulayici_profil",
                                           "GÖREV 2 — Kişisel ve Bilişsel Profil kriterleri için yapısal doğrulayıcı çalıştı (bkz. meta.kayitlar).",
                                           {"kayitlar": _val_log_prof})
            except Exception as e:
                print(f"UYARI (finalize_interview profil doğrulayıcı c={candidate_id}): {type(e).__name__}: {e}")
            # İş emri GÖREV 2 (VALIDATOR KALİBRASYONU) — AYNI transkript-geneli kelepçe, profil
            # kriterleri için de (jenerik mekanizma — beyan hiçbir profil kriteriyle eşleşmezse no-op).
            try:
                _fixed_prof, _new_score_prof2, _val_log_prof2 = apply_scope_clamp_transcript_wide(
                    _fixed_prof, PROFILE_CRITERIA, _tview, "K", candidate_id, level)
                if _new_score_prof2 is not None:
                    score_profile = _new_score_prof2
                if _val_log_prof2:
                    record_system_decision(candidate_id, level, "transkript_geneli_kelepce_profil",
                                           "GÖREV 2 — transkript genelinde alan dışı/devretme beyanı tespit edildi, ilgili profil kriteri/kriterleri kelepçelendi (bkz. meta.kayitlar).",
                                           {"kayitlar": _val_log_prof2})
            except Exception as e:
                print(f"UYARI (finalize_interview transkript geneli kelepçe profil c={candidate_id}): {type(e).__name__}: {e}")
            prof_table_display = _strip_total_line_for_display(_fixed_prof, is_profile=True)
        except Exception as e:
            print(f"UYARI (finalize_interview profil puanlama c={candidate_id}): {type(e).__name__}: {e}")
            prof_table_display = prof_raw

    # İş emri GÖREV 1.5 (VALIDATOR KALİBRASYONU) — İHLAL DAĞILIMI: hangi ihlal kaç kez, hangi
    # kriterde — validator'ın hangi kuralının fazla sert olduğu VERİYLE görülebilsin (log-only,
    # rapor metnini etkilemez).
    _all_gate_log = (_val_log_pos or []) + (_val_log_prof or [])
    _disqualified_entries = [l for l in _all_gate_log if l.get("sonuc") == "degerlendirilemedi_sistem"]
    try:
        if _all_gate_log:
            _tally = {}
            for l in _disqualified_entries:
                for v in l.get("ihlaller") or []:
                    _tally[v] = _tally.get(v, 0) + 1
            if _tally:
                record_system_decision(candidate_id, level, "ihlal_dagilimi",
                                       "GÖREV 1.5 — kriter doğrulayıcısının hangi ihlali kaç kez, hangi kriterde tetiklediği (validator kalibrasyonu için veri).",
                                       {"ihlal_sayilari": _tally,
                                        "kriter_bazinda": [{"kriter": l["kriter"], "kimlik": l["kimlik"], "ihlaller": l.get("ihlaller")} for l in _disqualified_entries]})
    except Exception as e:
        print(f"UYARI (finalize_interview ihlal dağılımı c={candidate_id}): {type(e).__name__}: {e}")

    # İş emri GÖREV 1.4 (VALIDATOR KALİBRASYONU, 2026-09) — kanıtlı örnek: Murat AYZİT raporunda
    # 12 kriterin 5'i (%41.7) düşmüştü. Yönetici kaydına uyarı YİNE yazılır (log-only).
    try:
        _total_crit_n = len(_crit) + len(PROFILE_CRITERIA)
        _dropped_n = len(_disqualified_entries)
        if _total_crit_n and (_dropped_n / _total_crit_n) > 0.25:
            record_system_decision(candidate_id, level, "yuksek_dusme_orani",
                                   f"GÖREV 1.4 — kriterlerin %25'inden fazlası değerlendirilemedi ({_dropped_n}/{_total_crit_n}) — bu SİSTEM HATASI sayılır, normal sonuç değildir.",
                                   {"dusen_kriterler": [l["kriter"] for l in _disqualified_entries], "toplam_kriter": _total_crit_n, "dusen_sayisi": _dropped_n})
    except Exception as e:
        print(f"UYARI (finalize_interview düşme oranı c={candidate_id}): {type(e).__name__}: {e}")

    # İş emri — KAYIP ANLATI BÖLÜMLERİ / GÖREV 1.4 (2026-09, sonraki tur) — Puanlama Kapsamı ARTIK
    # HER ZAMAN üretilir (düşme oranından BAĞIMSIZ — eski raporda da her zaman vardı, "Değerlendir-
    # ilmeyen kriter olmadı" durumunda bile). Bu, bir önceki turun YALNIZ %25 aşılınca görünen
    # koşullu notunun YERİNİ alır (madde 1.4: "GÖREV 3'teki düşme oranı notunun yerini alır").
    # NOT (bug — sentetik testte yakalandı, düzeltildi): bölümün TAMAMI hiç üretilmediyse (model
    # "YOK" dediyse veya bölüm ayracı hiç gelmediyse) score_position/score_profile None kalır ve
    # apply_structured_rationale_gate hiç ÇALIŞMAZ (_val_log_pos/_val_log_prof boş) — bu durumda
    # "0 kriter düştü, N/N değerlendirildi" YANLIŞ olurdu (aslında hiçbiri değerlendirilmedi).
    # İŞ EMRİ — RAPORLAMA VE PRIMARY DEĞERLENDİRME TUTARLILIĞI / madde 1-2 (düzeltildi): önceki
    # sürüm _dropped_pos_names/_dropped_prof_names'i YALNIZ _val_log_pos/_val_log_prof'tan (yani
    # SADECE apply_structured_rationale_gate'in diskalifiye ettiği satırlardan) çıkarıyordu.
    # recompute_and_fix_score/recompute_profile_section'ın KENDİ "not_asked" dalı (main.py ~12114)
    # bir kriteri "Değerlendirilemedi (sistem)" yapabiliyor ve BU gate'ten geçmeden pos_table_display/
    # prof_table_display'e yazılıyor — o zaman _val_log_pos bunu HİÇ görmüyor, kriter gerçekte payda
    # dışı olduğu halde Puanlama Kapsamı/Değerlendirilemeyen Alanlar onu "değerlendirildi" sayıyordu
    # (gerçek production kaydıyla doğrulandı). Artık TEK GERÇEK KAYNAK, append_reviewer_section'ın
    # devralma-sonrası aynı amaç için zaten kullandığı _extract_disqualified_criteria_names() —
    # final tablo METNİNİN kendisini tarar, hangi validator/dal diskalifiye ettiğinden BAĞIMSIZDIR.
    if score_position is None:
        _dropped_pos_names = [c.get("name") for c in (_crit or []) if c.get("name")]
    else:
        _dropped_pos_names = _extract_disqualified_criteria_names(pos_table_display)
    if score_profile is None:
        _dropped_prof_names = [pc["name"] for pc in PROFILE_CRITERIA]
    else:
        _dropped_prof_names = _extract_disqualified_criteria_names(prof_table_display)
    try:
        _puanlama_kapsami_text = render_puanlama_kapsami(_crit or [], PROFILE_CRITERIA, _dropped_pos_names, _dropped_prof_names)
    except Exception as e:
        print(f"UYARI (finalize_interview puanlama kapsamı c={candidate_id}): {type(e).__name__}: {e}")
        _puanlama_kapsami_text = ""

    # --- TEK KARAR KAYNAĞI (iş emri madde 6+21): Genel Puan = mevcut puanların eşit ağırlıklı
    #     ortalaması; 2. değerlendirici HENÜZ çalışmadı (append_reviewer_section SONRA çalışır ve
    #     recompute_overall_decision ile bu sayıyı GÜNCELLER) — burada yalnız 1. değerlendiriciyle
    #     GEÇİCİ (ama yanlış olmayan) bir Genel Puan hesaplanır. ---
    score = compute_genel_puan(score_position, score_profile)
    recommendation = decide_recommendation(score) or "Değerlendirilemedi"

    # NOT — İş emri KRİTER GEREKÇESİ (2026-09): eski log-only klişe taraması (detect_evidence_cliches
    # üzerinden, içerik değiştirilmeden yalnız loglanıyordu) BURADAN KALDIRILDI. Artık denetim
    # apply_structured_rationale_gate içinde bir KAPI olarak çalışıyor (yukarıda, pos/profil
    # tablolarının HEMEN normalize edilmesinin ardından) — geçemeyen içerik rapora hiç girmiyor,
    # log-only davranış GÖREV 2'nin doğrudan hedefiydi ("log değil kapı").

    db = get_db()
    messages = get_interview_messages(db, candidate_id, level)

    # --- Rapor gövdesi: yalnız GERÇEKTEN içeriği olan bölümler, kanonik sırada (iş emri madde 3). ---
    yo_text = sections.get("yonetici_ozeti", "")
    gy_text = sections.get("guclu_yonler", "")
    ga_text = sections.get("gelisim_alanlari", "")
    tm_text = sections.get("takip_sorulari", "")
    # İş emri — KAYIP ANLATI BÖLÜMLERİ / GÖREV 1.1-1.3 (2026-09, sonraki tur) — eski (2026-09-08
    # öncesi) formatta var olan, yeniden tasarımda kaybolan anlatı katmanı GERİ eklendi. Her biri
    # GÖREV 1.2'nin YENİ kalite kurallarına (yasak kalıp, tekrar yasağı) tabi — cümle bazlı temizlik
    # (strip_banned_phrase_sentences, kriter hücrelerinin diskalifiye mantığından FARKLI: bu
    # bölümler serbest paragraf, tüm bölümü değil yalnız ihlalli CÜMLEYİ çıkarır).
    _narrative_sections = {}
    for _key in ("analitik_dusunme", "problem_cozme", "kavrama_iletisim", "one_cikan_proje",
                "cv_mulakat_pozisyon_uyumu", "dil_gozlemi", "genel_kani"):
        _txt = sections.get(_key, "")
        if _txt:
            try:
                _txt, _dropped_sents = strip_banned_phrase_sentences(_txt)
                if _dropped_sents:
                    record_system_decision(candidate_id, level, f"{_key}_klise_cumle_silindi",
                                           f"GÖREV 1.2 — '{_key}' bölümünde yasaklı klişe içeren cümle(ler) tespit edilip çıkarıldı.",
                                           {"silinen_cumleler": _dropped_sents})
            except Exception as e:
                print(f"UYARI (finalize_interview {_key} klişe taraması c={candidate_id}): {type(e).__name__}: {e}")
        _narrative_sections[_key] = _txt
    # İş emri GÖREV 5.2 — yasaklı genel-gelişim kalıbı tespiti + AKTİF MÜDAHALE (önceki turda
    # yalnız logluyordu). Her yasaklı soru KENDİ SATIRIDIR (bağımsız madde) — bu satırı bütünüyle
    # çıkarmak, GÖREV 1'deki cümle-İÇİ klişelerin aksine, gramer BOZMAZ (o yüzden orada hâlâ
    # yalnız loglama tercih edildi — bkz. detect_evidence_cliches, burada ise satır tamamen atılır).
    try:
        _forbidden_followups = detect_forbidden_followup_patterns(tm_text)
        if _forbidden_followups:
            _kept = [ln for ln in tm_text.splitlines()
                    if not (ln.strip() and (_FORBIDDEN_FOLLOWUP_RE.search(ln) or banned_phrase_hits(ln)))]
            tm_text = "\n".join(_kept).strip()
            record_system_decision(candidate_id, level, "takip_sorulari_yasakli_kalip_silindi",
                                   "Takip Mülakatı Soruları'nda genel gelişim-koçluğu kalıbı tespit edildi ve o SORU rapordan çıkarıldı (GÖREV 5.3 — tümü çıkarsa bölüm hiç basılmaz).",
                                   {"silinen_satirlar": _forbidden_followups})
    except Exception as e:
        print(f"UYARI (finalize_interview takip sorulari kalip taramasi c={candidate_id}): {type(e).__name__}: {e}")
    # İş emri GÖREV 3.4/3.5 — her takip sorusu GERÇEKTEN sorulmuş bir mülakatçı sorusuna
    # ('[dayanak: mm:ss]') dayanmak ZORUNDA; dayanaksız/uydurma damgalı sorular SİLİNİR (bu tur
    # kök neden örneği: sorulmamış "Vergi ve Mevzuat" konusundan türetilmiş takip sorusu). Etiket
    # doğrulama içindir — okuyucuya BASILMAZ (strip_dayanak_tag).
    try:
        _tm_lines = [ln for ln in tm_text.splitlines() if ln.strip()]
        _grounded_lines, _ungrounded_lines = [], []
        for ln in _tm_lines:
            (_grounded_lines if _followup_line_grounded(ln, _tview) else _ungrounded_lines).append(ln)
        if _ungrounded_lines:
            tm_text = "\n".join(strip_dayanak_tag(ln) for ln in _grounded_lines).strip()
            record_system_decision(candidate_id, level, "takip_sorulari_dayanaksiz_silindi",
                                   "GÖREV 3 — Takip Mülakatı Soruları'nda geçerli [dayanak: mm:ss] damgası (gerçek mülakatçı sorusuna karşılık gelen) taşımayan sorular rapordan çıkarıldı.",
                                   {"silinen_satirlar": [strip_dayanak_tag(ln) for ln in _ungrounded_lines]})
        else:
            tm_text = "\n".join(strip_dayanak_tag(ln) for ln in _tm_lines).strip()
    except Exception as e:
        print(f"UYARI (finalize_interview takip sorulari dayanak taramasi c={candidate_id}): {type(e).__name__}: {e}")
    cv_ozeti_text = ""
    if not raw_body or not (yo_text or pos_table_display):
        # AI rapor bloğu eksik/bozuk geldi — kanıta dayalı yedek rapor (eski davranış korunur).
        report = build_fallback_report(dict(candidate) if candidate else {}, messages, score, recommendation, "AI rapor bloğu eksik/bozuk geldi")
    else:
        # GÖREV 9.2 — boşluk kaybı onarımı (kriter adları glue_terms olarak verilir)
        try:
            _glue = [c.get("name") for c in (_crit or []) if c.get("name")] + [pc["name"] for pc in PROFILE_CRITERIA]
            yo_text = repair_report_spacing(yo_text, glue_terms=_glue)
            gy_text = repair_report_spacing(gy_text, glue_terms=_glue)
            ga_text = repair_report_spacing(ga_text, glue_terms=_glue)
        except Exception as e:
            print(f"UYARI (finalize_interview boşluk onarımı c={candidate_id}): {type(e).__name__}: {e}")

        # İş emri GÖREV 4 — Yönetici Özeti: AYRI bağımsız akış (kriter gerekçesi sistemiyle
        # KARIŞTIRILMAZ). Hedef aralık (150-250 kelime) dışındaysa VEYA yasaklı klişe içeriyorsa
        # TEK deneme ile yeniden ürettirilir (mevcut kelime sayısı + hedef AÇIKÇA modele verilir);
        # ikinci denemede de düzelmezse OLDUĞU GİBİ basılır + loglanır — OTOMATİK kısaltma/uzatma
        # YOK (cümleyi yarıda keser, daha kötü olur).
        try:
            _wc = len(yo_text.split())
            _yo_violations_before = detect_future_expectation(yo_text)
            _yo_bad = yo_text and (not (150 <= _wc <= 250) or _yo_violations_before)
            if _yo_bad:
                # İŞ EMRİ — ÇOKLU TALENT MİMARİSİ / madde E/G (TEK-PASS AI AKIŞI): ESKİ davranış
                # burada regenerate_yonetici_ozeti'i (AI content-retry) 2 kez çağırıyordu — bu
                # iş emriyle KALDIRILDI. Uzunluk/klişe sorunu artık AI'ya tekrar sorulmadan
                # DOĞRUDAN loglanır; yo_text OLDUĞU GİBİ kalır — mevcut TEK geçişlik Final QG
                # (run_final_report_quality_gate) raporun TAMAMINI zaten görüyor, bu sorunu da
                # kendi tek AI geçişinde değerlendirebilir. regenerate_yonetici_ozeti fonksiyonu
                # SİLİNMEDİ — normal akıştan çağrılmıyor, davranışı koruma amaçlı yerinde bırakıldı.
                if not (150 <= _wc <= 250):
                    record_system_decision(candidate_id, level, "yonetici_ozeti_uzunluk_disi",
                                           f"Yönetici Özeti {_wc} kelime — hedef aralık (150-250) dışında "
                                           "(AI content-retry YAPILMADI — İŞ EMRİ madde E; olduğu gibi basıldı, Final QG denetleyecek).",
                                           {"kelime_sayisi": _wc})
                if _yo_violations_before:
                    record_system_decision(candidate_id, level, "yonetici_ozeti_klise_tespit",
                                           "Yönetici Özeti'nde yasaklı klişe/geleceğe-dönük-beklenti kalıbı tespit edildi; "
                                           "AI content-retry YAPILMADI (İŞ EMRİ madde E) — olduğu gibi basıldı, Final QG denetleyecek.",
                                           {"kalip_eslesmeleri": _yo_violations_before})
        except Exception as e:
            print(f"UYARI (finalize_interview yönetici özeti uzunluk kontrolü c={candidate_id}): {type(e).__name__}: {e}")

        # CV Özeti — model ürettiyse kullan, yoksa/çok kısaysa deterministik yedek (iş emri madde 13).
        cv_ozeti_text = (sections.get("cv_ozeti") or "").strip()
        if not cv_ozeti_text or len(strip_markdown(cv_ozeti_text)) < 15:
            try:
                cv_ozeti_text = render_cv_ozeti_fallback(dict(candidate) if candidate else {}, _ftx)
            except Exception as e:
                print(f"UYARI (finalize_interview CV özeti yedek c={candidate_id}): {type(e).__name__}: {e}")
                cv_ozeti_text = ""
        # İş emri GÖREV 4.1 (KAYIP ANLATI BÖLÜMLERİ) — ayraç DETERMİNİSTİK olarak ':' yapılır
        # (kök neden: prompt'taki etiket listesi "Eğitim / Deneyim / ..." biçiminde YAZILMIŞTI —
        # model bunu görsel örnek sanıp '/' ayracını satır formatına da uyguladı).
        try:
            cv_ozeti_text = normalize_cv_ozeti_separators(cv_ozeti_text)
        except Exception as e:
            print(f"UYARI (finalize_interview CV özeti ayraç normalize c={candidate_id}): {type(e).__name__}: {e}")
        # İş emri GÖREV 4.2 — sözlü beyandaki SPESİFİK okul türü ("ticaret meslek lisesi") genel
        # bir kategoriye ("Lise mezunu") indirgenmişse geri konur (bilinen okul-türü listesiyle
        # sınırlı, bounded bir düzeltme — genel bir anlam-sadakati denetleyicisi DEĞİLDİR).
        try:
            cv_ozeti_text, _cv_restored = restore_specific_school_type(cv_ozeti_text, _ftx)
            if _cv_restored:
                record_system_decision(candidate_id, level, "cv_ozeti_spesifik_bilgi_geri_kondu",
                                       "GÖREV 4.2 — CV Özeti'ndeki 'Eğitim' satırı sözlü beyandaki daha spesifik okul türünü genelleştirmişti; spesifik ifade geri kondu.",
                                       {})
        except Exception as e:
            print(f"UYARI (finalize_interview CV özeti spesifik bilgi c={candidate_id}): {type(e).__name__}: {e}")

        # Beyan Tutarlılığı — TAMAMEN deterministik (iş emri madde 14); model bunu YAZMAZ.
        try:
            _disc = compute_field_discrepancies(dict(candidate) if candidate else {}, _ftx)
            beyan_tutarliligi_text = render_beyan_tutarliligi(_disc)
        except Exception as e:
            print(f"UYARI (finalize_interview beyan tutarlılığı c={candidate_id}): {type(e).__name__}: {e}")
            beyan_tutarliligi_text = ""
            _disc = {}

        # İş emri — RAPOR İÇERİK STANDARDI / B4 — Yönetici Özeti, Beyan Tutarlılığı'nın çelişki
        # bulduğu bir alanda (henüz kendisi çelişkiden HABERSİZ üretildiği için) taraf tutmuşsa
        # düzeltilir (bkz. strip_yonetici_ozeti_discrepancy_bias) — çelişkiye NÖTR atıf yapılır.
        try:
            yo_text, _yo_bias_changed = strip_yonetici_ozeti_discrepancy_bias(yo_text, _disc)
            if _yo_bias_changed:
                record_system_decision(candidate_id, level, "yonetici_ozeti_celiski_tarafsizlastirildi",
                                       "GÖREV B4 — Yönetici Özeti, Beyan Tutarlılığı'nın çelişki bulduğu bir alanda tek bir kaynağın değerini iddia ediyordu; nötr atıfla değiştirildi.",
                                       {})
        except Exception as e:
            print(f"UYARI (finalize_interview yönetici özeti çelişki tarafsızlaştırma c={candidate_id}): {type(e).__name__}: {e}")

        # Görüntü ve Ses Gözlemi — insan diliyle, ham sayı yok (TUR 3/4'ten değişmedi).
        try:
            modality_prose = build_modality_prose(candidate_id, level)
        except Exception as e:
            print(f"UYARI (finalize_interview modalite prose c={candidate_id}): {type(e).__name__}: {e}")
            modality_prose = ""

        # İş emri GÖREV 5 EK — Güçlü Yönler: dayanaksız/damgasız genel övgü cümleleri ÇIKARILIR
        # (unsupported_strength — bu turun somut örneği: "bu deneyimini mülakat sırasında
        # belirttiği örneklerle desteklemiştir" — hiçbir [dk] damgası yok, hangi örnek belirsiz).
        try:
            gy_text, _dropped_strength = _strip_unsupported_strength_sentences(gy_text)
            if _dropped_strength:
                record_system_decision(candidate_id, level, "guclu_yonler_dayanaksiz_silindi",
                                       "GÖREV 5 EK (unsupported_strength) — Güçlü Yönler'de damgasız/dayanaksız genel övgü cümlesi tespit edildi ve çıkarıldı.",
                                       {"silinen_cumleler": _dropped_strength})
        except Exception as e:
            print(f"UYARI (finalize_interview güçlü yönler dayanaksızlık taraması c={candidate_id}): {type(e).__name__}: {e}")

        # İş emri GÖREV 5 — evaluated_vs_narrative_conflict: bir kriter "Değerlendirilemedi
        # (sistem)" ise o kriterin KONUSUNDA olumlu hüküm cümlesi Güçlü Yönler'de/Yönetici
        # Özeti'nde BASILMAZ (kanıtlı örnek: "İletişim" Değerlendirilemedi iken Güçlü Yönler
        # "iletişim becerileri...olumlu bir izlenim bırakmıştır" diyordu).
        try:
            _all_dropped_names = _dropped_pos_names + _dropped_prof_names
            gy_text, _dropped_conflict_gy = strip_narrative_conflicts_with_disqualified(gy_text, _all_dropped_names)
            yo_text, _dropped_conflict_yo = strip_narrative_conflicts_with_disqualified(yo_text, _all_dropped_names)
            _dropped_conflict = _dropped_conflict_gy + _dropped_conflict_yo
            if _dropped_conflict:
                record_system_decision(candidate_id, level, "evaluated_vs_narrative_conflict",
                                       "GÖREV 5 — Değerlendirilemedi sayılan bir kriterin konusunda Güçlü Yönler/Yönetici Özeti'nde olumlu hüküm cümlesi tespit edildi ve çıkarıldı.",
                                       {"silinen_cumleler": _dropped_conflict, "dusen_kriterler": _all_dropped_names})
        except Exception as e:
            print(f"UYARI (finalize_interview evaluated_vs_narrative_conflict c={candidate_id}): {type(e).__name__}: {e}")

        # İş emri GÖREV 3 — Gelişim Alanları'ndaki RİSK paragrafları transkriptte GERÇEKTEN var
        # olan bir [mm:ss] damgasına dayanmak ZORUNDA; dayanaksız/uydurma risk iddiası ÇIKARILIR.
        try:
            ga_text, _dropped_risk = _strip_ungrounded_risk_paragraphs(ga_text, _tview)
            if _dropped_risk:
                record_system_decision(candidate_id, level, "risk_iddia_dayanaksiz_silindi",
                                       "GÖREV 3 — Gelişim Alanları'ndaki RİSK ifadesi transkriptte doğrulanabilir bir [dk] damgasına dayanmıyordu, paragraf rapordan çıkarıldı.",
                                       {"silinen_paragraflar": _dropped_risk})
        except Exception as e:
            print(f"UYARI (finalize_interview risk dayanaksızlık taraması c={candidate_id}): {type(e).__name__}: {e}")

        # İş emri GÖREV 5.5 — alan dışı/devretme beyanı tespit edildiyse (apply_structured_rationale_gate
        # → _scope_flagged) ama Gelişim Alanları'nda HİÇ RİSK olarak raporlanmadıysa (unreported_scope_
        # limitation), sistem BU BULGUYU KENDİSİ ekler — "sessizce geçilemez" (işe alım kararını
        # belirleyen en önemli sinyal). ga_text zaten yukarıda dayanaksız risklerden temizlendi.
        try:
            if _scope_flagged and "RİSK" not in _tr_upper(ga_text):
                _injected = render_scope_risk_paragraph(_scope_flagged)
                ga_text = (ga_text.strip() + "\n\n" + _injected).strip() if ga_text.strip() else _injected
                record_system_decision(candidate_id, level, "alan_disi_risk_zorunlu_eklendi",
                                       "GÖREV 5.5 — alan dışı/devretme beyanı tespit edildi ama Gelişim Alanları'nda RİSK olarak raporlanmamıştı (unreported_scope_limitation); sistem bunu kendisi ekledi.",
                                       {"kriterler": [f["kriter"] for f in _scope_flagged]})
        except Exception as e:
            print(f"UYARI (finalize_interview zorunlu risk ekleme c={candidate_id}): {type(e).__name__}: {e}")

        # İş emri GÖREV 1.7 — Profil Veto Kontrolü: DETERMİNİSTİK, profil tablosunun kendisinden
        # (bkz. render_profile_veto_control). Ayrı bir bölüm/başlık DEĞİL — eski mimaride de PUAN 2
        # bloğunun bir PARÇASIYDI; burada da Kişisel ve Bilişsel Profil metninin sonuna eklenir.
        try:
            _profile_veto_text = render_profile_veto_control(prof_table_display) if prof_table_display.strip() else ""
        except Exception as e:
            print(f"UYARI (finalize_interview profil veto kontrolü c={candidate_id}): {type(e).__name__}: {e}")
            _profile_veto_text = ""

        # İş emri GÖREV 1.5 — Öneri Gerekçesi: DETERMİNİSTİK, skor/öneriden TÜRETİLİR (bkz.
        # render_oneri_gerekcesi) — öneriyle ÇELİŞEN bir gerekçe YAPI GEREĞİ imkânsız.
        try:
            _oneri_gerekcesi_text = render_oneri_gerekcesi(recommendation, score, score_position, score_profile)
        except Exception as e:
            print(f"UYARI (finalize_interview öneri gerekçesi c={candidate_id}): {type(e).__name__}: {e}")
            _oneri_gerekcesi_text = ""

        # İş emri — RAPOR ANLATI KATMANI GERİ EKLEME (2026-09, sonraki tur) / ADIM 2 — bölümler
        # her raporda KOŞULSUZ basılır (madde: "veri yoksa bölüm başlığı kalır, içine deterministik
        # 'değerlendirilemedi' metni girer — bölüm tamamen gizlenmez"); önceki turun "içerik yoksa
        # YOK yaz, sistem bölümü rapordan çıkarır" davranışı bu 7 anlatı bölümü için TERSİNE çevrildi.
        # Analitik/Problem Çözme/Kavrama-İletişim: EN AZ BİR [dk] referansı ZORUNLU — yoksa hüküm
        # cümlesi KURULMAZ, yerine sabit "Doğrulanabilir kanıt bulunamadı." yazılır.
        try:
            for _s in ("analitik_dusunme", "problem_cozme", "kavrama_iletisim"):
                _narrative_sections[_s] = finalize_narrative_section(_narrative_sections.get(_s, ""), require_timestamp=True)
            for _s in ("one_cikan_proje", "dil_gozlemi", "genel_kani"):
                _narrative_sections[_s] = finalize_narrative_section(_narrative_sections.get(_s, ""), require_timestamp=False)
            # CV ↔ Mülakat ↔ Pozisyon Uyumu — GÖREV kuralı: alan dışı/devretme beyanı varsa ZORUNLU
            # belirtilir (Gelişim Alanları'ndaki ZORUNLU enjeksiyon mekanizmasıyla AYNI ilke, burada da
            # uygulanır — bkz. _scope_flagged, GÖREV 5.5'ten beri toplanıyor).
            _cv_uyum = finalize_narrative_section(_narrative_sections.get("cv_mulakat_pozisyon_uyumu", ""), require_timestamp=False)
            if _scope_flagged:
                _mentioned = any(f["kriter"] in _cv_uyum for f in _scope_flagged) or re.search(r"alan d[ıi][şs][ıi]|devret", _cv_uyum, re.IGNORECASE)
                if not _mentioned:
                    # B3 — "devretme" ve "alan dışı" kavramsal olarak ayrı; hangisi geçerliyse O yazılır
                    # (bkz. _scope_declaration_label — Gelişim Alanları'ndaki RİSK enjeksiyonuyla AYNI ilke).
                    # MADDE 5 — aynı alıntı/beyan türünü paylaşan kriterler TEK cümlede birlikte sayılır.
                    _add = _render_scope_flagged_sentences(_scope_flagged)
                    _cv_uyum = (_add if _cv_uyum in (_NO_NARRATIVE_EVIDENCE_FALLBACK,) else _cv_uyum + "\n\n" + _add)
                    record_system_decision(candidate_id, level, "cv_uyum_alan_disi_zorunlu_eklendi",
                                           "CV ↔ Mülakat ↔ Pozisyon Uyumu'nda alan dışı/devretme beyanı tespit edildi ama belirtilmemişti; sistem ekledi.",
                                           {"kriterler": [f["kriter"] for f in _scope_flagged]})
            _narrative_sections["cv_mulakat_pozisyon_uyumu"] = _cv_uyum
        except Exception as e:
            print(f"UYARI (finalize_interview anlatı bölümleri koşulsuz basım c={candidate_id}): {type(e).__name__}: {e}")

        # İş emri ADIM 2 — Tutarlılık/Çelişki Analizi (Beyan Tutarlılığı ile AYNI kaynaktan — _disc
        # bu noktada henüz hesaplanmadı, birazdan hesaplanacak Beyan Tutarlılığı bloğuyla AYNI
        # compute_field_discrepancies çağrısını PAYLAŞMASI için önce buraya taşındı) ve
        # Değerlendirilemeyen Alanlar (Puanlama Kapsamı ile AYNI _dropped_pos_names/_dropped_prof_names).
        try:
            _disc_for_tutarlilik = compute_field_discrepancies(dict(candidate) if candidate else {}, _ftx)
        except Exception as e:
            print(f"UYARI (finalize_interview tutarlılık/çelişki c={candidate_id}): {type(e).__name__}: {e}")
            _disc_for_tutarlilik = {}
        _tutarlilik_text = render_tutarlilik_celiski_analizi(_disc_for_tutarlilik)
        _degerlendirilemeyen_text = render_degerlendirilemeyen_alanlar(_dropped_pos_names, _dropped_prof_names)

        parts = []
        if yo_text:
            parts.append("**Yönetici Özeti:**\n" + yo_text)
        # İş emri — KAYIP ANLATI BÖLÜMLERİ / GÖREV 1.1 — Puanlama Kapsamı (deterministik, HER
        # ZAMAN) + anlatı bölümleri, Yönetici Özeti'nden SONRA, kriter tablolarından ÖNCE (eski
        # rapor formatındaki yerleri; ADIM 2 ile Genel Kanı da BU bloğa taşındı).
        if _puanlama_kapsami_text:
            parts.append(f"{_PUANLAMA_KAPSAMI_HEAD}\n{_puanlama_kapsami_text}")
        parts.append("**Analitik Düşünme ve Muhakeme:**\n" + _narrative_sections["analitik_dusunme"])
        parts.append("**Problem Çözme ve Karar Verme Yaklaşımı:**\n" + _narrative_sections["problem_cozme"])
        parts.append("**Kavrama ve İletişim:**\n" + _narrative_sections["kavrama_iletisim"])
        parts.append("**Tutarlılık / Çelişki Analizi:**\n" + _tutarlilik_text)
        parts.append("**Öne Çıkan Proje ve Deneyimler:**\n" + _narrative_sections["one_cikan_proje"])
        parts.append("**CV ↔ Mülakat ↔ Pozisyon Uyumu:**\n" + _narrative_sections["cv_mulakat_pozisyon_uyumu"])
        parts.append("**Değerlendirilemeyen Alanlar:**\n" + _degerlendirilemeyen_text)
        parts.append("**Dil Gözlemi:**\n" + _narrative_sections["dil_gozlemi"])
        parts.append("**Genel Kanı:**\n" + _narrative_sections["genel_kani"])
        if pos_table_display.strip():
            parts.append("**Pozisyon Yetkinlikleri:**\n" + pos_table_display.strip())
        if prof_table_display.strip():
            _prof_block = "**Kişisel ve Bilişsel Profil:**\n" + prof_table_display.strip()
            if _profile_veto_text:
                _prof_block += "\n\nProfil Veto Kontrolü: " + _profile_veto_text
            parts.append(_prof_block)
        # İkinci Değerlendirici Görüşü buraya (Profil'den hemen sonra) ait — henüz üretilmedi;
        # append_reviewer_section bu YER TUTUCUYU bulup değiştirir/kaldırır (bkz. tanımı).
        parts.append(_REVIEWER_SLOT_MARK)
        if gy_text:
            parts.append("**Güçlü Yönler:**\n" + gy_text)
        if ga_text:
            parts.append("**Gelişim Alanları:**\n" + ga_text)
        if modality_prose:
            parts.append(modality_prose)
        if cv_ozeti_text.strip():
            parts.append("**CV Özeti:**\n" + cv_ozeti_text.strip())
        if beyan_tutarliligi_text:
            parts.append("**Beyan Tutarlılığı:**\n" + beyan_tutarliligi_text)
        # İş emri RAPOR ANLATI KATMANI GERİ EKLEME / ADIM 2 — Genel Kanı ARTIK üst blokta (Yönetici
        # Özeti sonrası, bu iş emrinin AÇIKÇA verdiği "Bölüm sırası" listesine göre — önceki turda
        # rapor SONUNA konmuştu, bu KARARIN İPTALİ, açıkça belirtilir). Öneri Gerekçesi
        # (deterministik) burada, raporun sonunda kalır.
        if _oneri_gerekcesi_text:
            parts.append("**Öneri Gerekçesi:**\n" + _oneri_gerekcesi_text)
        if tm_text:
            parts.append("**Takip Mülakatı İçin Önerilen Sorular:**\n" + tm_text)
        report = scrub_forbidden_phrases("\n\n".join(parts))

    standard_cv = cv_ozeti_text  # e-posta gövdesi için AYNI CV Özeti — ikinci bir LLM bloğu YOK.

    # HAM metrikler + değerlendirilemeyen kriter listesi: AYRI teknik ek (EK 3), gövdeye GİRMEZ.
    _technical_annex = None
    try:
        _technical_annex = build_technical_annex(candidate_id, level)
        _eksiklik_lines = [w for w in _score_warnings if w.startswith(("'", "Değerlendirilemeyen", "Yetersiz", "[PROFİL]"))]
        if _eksiklik_lines:
            _technical_annex = (_technical_annex or "**Teknik Ek (yalnızca yönetici — ham veri):**\n") \
                + "\n- Değerlendirilemeyen/yetersiz kriterler: " + "; ".join(_eksiklik_lines)
    except Exception as e:
        print(f"UYARI (finalize_interview teknik ek c={candidate_id}): {type(e).__name__}: {e}")
        _technical_annex = None

    # KALEM 5 — müşteri raporundan iç sistem satırlarını çıkar (halüsinasyon işaretleri).
    # Modele giden interviews.messages DEĞİŞMEZ.
    report = strip_report_system_lines(report)
    standard_cv = strip_report_system_lines(standard_cv)
    _tech_note = None

    if regen:
        # İŞ EMRİ — FINAL EVALUATION ARCHITECTURE / madde 6 — REGENERATE STALE REVIEWER FIX.
        # FATAL AUDIT (HIGH) bulgusu: regenerate edilip o turda second evaluator (Claude, yalnız
        # L3) BAŞARISIZ olursa, ESKİ reviewer_score_position/profile bu satırda kalıyor, YENİ
        # primary state ile SESSİZCE karışabiliyordu. Fix: HER primary yazımı (regen dahil)
        # reviewer'a bağlı TÜM alanları BASELINE'a (final=primary, reviewer=NULL, gate
        # durumları=NULL) SIFIRLAR — yalnız BU TURUN reviewer'ı (varsa, L3) GERÇEKTEN başarıyla
        # tamamlanırsa (append_reviewer_section → recompute_overall_decision) final_score_*
        # blended değerle YENİDEN yazılır. Eski değere fallback YOK.
        # TEK DÜZELTME — L3 processing_status ZAMANLAMASI: L3'te second evaluator (Claude) + Quality
        # Gate finalize_interview'DAN SONRA (run_deferred_finish_job içinde) çalışır — 'completed'
        # burada yazılırsa frontend pipeline bitmeden "Tamamlandı" + ara (primary-only) skor gösterir.
        # L3'te bilerek 'processing' bırakılır; run_deferred_finish_job pipeline'ın GERÇEK sonunda
        # (Quality Gate + final integrity check'ten sonra) 'completed' yazar. L1/L2'de ek aşama
        # olmadığı için davranış DEĞİŞMEDİ.
        _proc_status = 'processing' if level == 3 else 'completed'
        db.execute("""
            UPDATE interviews SET report=?, standard_cv=?, score=?, score_position=?, score_profile=?, recommendation=?,
                   final_score_position=?, final_score_profile=?,
                   reviewer_score_position=NULL, reviewer_score_profile=NULL,
                   quality_gate_status=NULL, final_integrity_status=NULL,
                   report_regenerated_at=CURRENT_TIMESTAMP, technical_annex=?,
                   report_tech_note=?, processing_status=?, processing_error=NULL
            WHERE candidate_id=? AND level=?
        """, (report, standard_cv, score, score_position, score_profile, recommendation,
              score_position, score_profile, _technical_annex, _tech_note, _proc_status, candidate_id, level))
        db.commit()
        db.close()
        record_system_decision(candidate_id, level, "rapor_yeniden_uretildi",
                               "Geriye dönük yeniden üretim tamamlandı; orijinal bitiş saati korundu.",
                               {"score": score, "score_position": score_position, "score_profile": score_profile,
                                "recommendation": recommendation}, warnings=_score_warnings)
        _ensure_result_reason(candidate_id, level, score, recommendation, terminated_reason)
        return {"message": "Rapor yeniden üretildi.", "completed": True, "score": score, "recommendation": recommendation}

    # EŞZAMANLILIK GÜVENLİK AĞI: interviews.completed_at hâlâ NULL ise finalize et (WHERE koşulu
    # ile atomik). Eğer bu satır başka bir eşzamanlı çağrı tarafından zaten tamamlanmışsa
    # (rowcount=0), üzerine yazma ve tekrar e-posta gönderme — mevcut kayıtlı sonucu dön.
    # KALEM 4 — completed_at = mülakatın GERÇEK bitiş anı (interview_ended_at); rapor üretimi
    # arka planda saatler sonra bitse bile completed_at o zamanı yansıtmaz. report_generated_at
    # ayrı alan: raporun fiilen üretildiği an.
    # İŞ EMRİ — madde 3/6: ilk üretimde de final_score_position/profile BASELINE olarak
    # primary'ye eşitlenir (L1/L2 için bu NİHAİ değerdir — second evaluator hiç çalışmaz);
    # reviewer'a bağlı alanlar taze satırda zaten NULL, açıkça da sıfırlanır (tutarlılık).
    # TEK DÜZELTME — L3 processing_status ZAMANLAMASI (bkz. regen dalındaki aynı not): completed_at
    # semantiği (mülakatın GERÇEK bitiş anı) DEĞİŞMEDİ — yalnız processing_status L3'te pipeline
    # tam bitene kadar 'processing' kalır.
    _proc_status = 'processing' if level == 3 else 'completed'
    cur = db.execute("""
        UPDATE interviews SET report=?, standard_cv=?, score=?, score_position=?, score_profile=?, recommendation=?,
               final_score_position=?, final_score_profile=?,
               reviewer_score_position=NULL, reviewer_score_profile=NULL,
               quality_gate_status=NULL, final_integrity_status=NULL,
               completed_at=COALESCE(interview_ended_at, CURRENT_TIMESTAMP), report_generated_at=CURRENT_TIMESTAMP,
               technical_annex=?, report_tech_note=?, processing_status=?, processing_error=NULL
        WHERE candidate_id=? AND level=? AND completed_at IS NULL
    """, (report, standard_cv, score, score_position, score_profile, recommendation,
          score_position, score_profile, _technical_annex, _tech_note, _proc_status, candidate_id, level))
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
        # İŞ EMRİ — FINAL EVALUATION ARCHITECTURE: L1 birincil DEĞERLENDİRME/RAPOR artık OpenAI.
        log_ai_provider(candidate_level, "openai", "analysis")
        _job_id = _mark_finish_pending(data.candidate_id, candidate_level, provider="openai", model=OPENAI_REPORT_MODEL,
                                       system=system, payload=force_msg, terminated_reason=terminated_reason, reason="violation")
        if _job_id:
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
        cap, count_filter = 32, "reason='mimic_sample'"   # GÖREV 3.4 — 24→32 üst sınır (kısa mülakatta bile yeterli havuz)
    else:
        cap, count_filter = 6, "(reason IS NULL OR reason<>'mimic_sample')"   # eski fallback seti

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

# İŞ EMRİ — ÇOKLU TALENT MİMARİSİ / madde B: aynı candidate+level için aktif Realtime "sahiplik"
# eşiği. Frontend heartbeat'i (/api/realtime/sync) ~25sn'de bir gelir; bu eşik bir heartbeat'in
# kaçmasına (ağ gecikmesi vb.) tolerans tanıyacak kadar geniş, ama gerçekten terk edilmiş bir
# sekmeyi makul sürede "stale" sayacak kadar dar tutuldu.
REALTIME_OWNER_STALE_SECONDS = 45


def _prepare_realtime_session_sync(candidate_id: int) -> dict:
    """İŞ EMRİ — ÇOKLU TALENT MİMARİSİ / madde C+B: /api/realtime/session'ın TÜM senkron DB işini
    TEK blokta toplar — async route handler bunu `asyncio.to_thread` ile çağırır, event loop'u DB
    round-trip'i kadar BLOKE ETMEZ. Bağlantı bu fonksiyon İÇİNDE açılıp kapanır (thread'ler arası
    paylaşılan bağlantı YOK — hem SQLite hem Postgres için güvenli; DB abstraction/transaction
    davranışı DEĞİŞMEDİ, yalnız senkron kod artık worker thread'de çalışıyor).
    Ayrıca (madde B) aynı candidate+level'a aynı anda İKİNCİ bir Realtime oturumunun bağlanmasını
    engelleyen atomik ownership claim'i de burada yapılır (transcript/skor SİLİNMEZ; yalnız ikinci
    bağlantı reddedilir — bkz. çağıran route'taki 409 dalı)."""
    db = get_db()
    try:
        candidate = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if not candidate:
            return {"found": False}
        candidate = dict(candidate)
        candidate_level = candidate.get("level") or 1
        result = {"found": True, "candidate": candidate, "level": candidate_level}
        if candidate_level not in (2, 3):
            result["level_invalid"] = True
            return result
        if not (candidate.get("cv_text") and len((candidate.get("cv_text") or "").strip()) > 20):
            result["cv_missing"] = True
            return result

        depth_tier = candidate.get("depth_tier") or "standart"
        # BUG FIX (started_at): interviews satırı önceden sadece ilk heartbeat (/api/realtime/sync,
        # 25sn'de bir) ya da hiç heartbeat gelmezse finalize (/api/realtime/report) anında oluşuyordu.
        # started_at kolonu DEFAULT CURRENT_TIMESTAMP olduğu için satır geç oluşursa gerçek mülakat
        # süresi (dakikalar) kayboluyor, DB'de birkaç saniyeymiş gibi görünüyordu. Artık oturum
        # (WebRTC bağlantısı) kurulur kurulmaz satır burada, gerçek başlangıç anında oluşturuluyor.
        existing_interview = db.execute(
            "SELECT completed_at FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, candidate_level)
        ).fetchone()
        if not existing_interview:
            db.execute(
                "INSERT INTO interviews (candidate_id, level, messages, depth_tier) VALUES (?, ?, '[]', ?)",
                (candidate_id, candidate_level, depth_tier)
            )
            # Teşebbüs sayacı: yeni oturum = bir başlatma denemesi (Mülakat Denemeleri ekranı).
            db.execute(
                "UPDATE candidates SET interview_start_count = COALESCE(interview_start_count, 0) + 1, last_start_at = ? WHERE id = ?",
                (_now_ts(), candidate_id)
            )
            db.commit()
        elif not existing_interview["completed_at"]:
            # Satır zaten var ama tamamlanmamış (örn. sayfa yenilendi, yeniden bağlanıldı) —
            # started_at'i EZME; ilk gerçek başlangıç zaten kayıtlı kalsın.
            # Yine de yeniden bağlanma denemesinin zamanını izle.
            db.execute("UPDATE candidates SET last_start_at = ? WHERE id = ?", (_now_ts(), candidate_id))
            db.commit()

        # madde B — RACE'E DAYANIKLI OWNERSHIP CLAIM: tek atomik UPDATE...WHERE (DB seviyesinde,
        # process-local kilide DAYANMAZ — çoklu worker/replica güvenli). rowcount>0 ise BU çağrı
        # sahipliği aldı; 0 ise başka (yakın zamanda aktif) bir sahip var — reddedilmeli.
        owner_token = secrets.token_hex(16)
        stale_sql = _staleness_clause("realtime_owner_at", REALTIME_OWNER_STALE_SECONDS)
        claim_cur = db.execute(
            "UPDATE interviews SET realtime_owner_token=?, realtime_owner_at=CURRENT_TIMESTAMP "
            "WHERE candidate_id=? AND level=? AND completed_at IS NULL AND ("
            f"  realtime_owner_token IS NULL OR realtime_owner_at IS NULL OR {stale_sql}"
            ")",
            (owner_token, candidate_id, candidate_level)
        )
        db.commit()
        result["owner_claimed"] = (claim_cur.rowcount or 0) > 0
        return result
    finally:
        db.close()


@app.post("/api/realtime/session")
async def create_realtime_session(payload=Depends(verify_token)):
    if payload.get("role") != "candidate":
        raise HTTPException(status_code=403, detail="Yetkisiz")
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="Sesli mülakat (OpenAI Realtime) için OPENAI_API_KEY tanımlı değil.")

    candidate_id = payload["candidate_id"]
    prep = await asyncio.to_thread(_prepare_realtime_session_sync, candidate_id)
    if not prep.get("found"):
        raise HTTPException(status_code=404, detail="Aday kaydı bulunamadı")
    candidate = prep["candidate"]
    candidate_level = prep["level"]
    if prep.get("level_invalid"):
        raise HTTPException(status_code=400, detail="Bu uç nokta Level 2 ve Level 3 adaylar için geçerlidir.")
    if prep.get("cv_missing"):
        raise HTTPException(status_code=400, detail="Bu seviyedeki mülakata başlamadan önce CV yüklemeniz gerekiyor.")
    if not prep.get("owner_claimed"):
        # İŞ EMRİ madde B — aynı candidate+level için zaten (yakın zamanda aktif) bir canlı oturum
        # var: transcript SİLİNMEDİ, mevcut sahip ETKİLENMEDİ, yalnız bu İKİNCİ bağlantı reddedildi.
        # detail şekli mevcut AIError şekliyle AYNI ({message, error_class, retryable}) — frontend
        # mapConnectError bunu zaten tanıyor (retryable:false -> otomatik retry döngüsüne GİRMEZ).
        raise HTTPException(status_code=409, detail={
            "message": "Bu mülakat için zaten aktif bir canlı oturum var. Lütfen diğer sekmeyi/pencereyi kapatıp birkaç saniye sonra tekrar deneyin.",
            "error_class": "session_already_active", "retryable": False,
        })

    depth_tier = candidate.get("depth_tier") or "standart"
    depth_cfg = get_effective_level_config(candidate_level, depth_tier)

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
            "max_output_tokens": REALTIME_MAX_RESPONSE_TOKENS,
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
    OpenAI'a HİÇ istek atmadan, ücretsiz bir şablon raporla direkt döner. 2026-09 rapor
    yeniden tasarımı — ===BAŞLIK=== formatına güncellendi (eski ---STANDARTCV--- bloğu ve
    düz **Aday:**/**Öneri:** satırları artık parse_llm_report_sections'ın tanıdığı bir
    şey DEĞİL; bu şablon eski kalırsa finalize_interview onu sessizce build_fallback_report'a
    düşürüp bu fonksiyonun verdiği SOMUT nedeni kaybediyordu — kök neden, GÖREV: rapor
    yeniden üretimi çalışmıyor teşhisi sırasında yakalandı)."""
    return f"""[MÜLAKATBİTTİ]
---RAPOR---
===YÖNETİCİ ÖZETİ===
{reason}

===POZİSYON YETKİNLİKLERİ===
YOK

===KİŞİSEL VE BİLİŞSEL PROFİL===
YOK

===GÜÇLÜ YÖNLER===
YOK

===GELİŞİM ALANLARI===
YOK

===CV ÖZETİ===
YOK

===TAKİP MÜLAKATI SORULARI===
YOK
===BÖLÜM SONU===
---RAPORSON---"""

def _sync_realtime_progress_sync(effective_candidate_id: int, transcript: Optional[str]) -> dict:
    """İŞ EMRİ — ÇOKLU TALENT MİMARİSİ / madde C+B: /api/realtime/sync'in TÜM senkron DB işini
    (candidate/interview kontrolü + gerekirse satır oluşturma + ownership heartbeat tazeleme +
    transcript yazımı) TEK bağlantı/TEK thread bloğunda toplar — async route handler bunu
    `asyncio.to_thread` ile çağırır, event loop'u BLOKE ETMEZ. DB abstraction/transaction
    davranışı DEĞİŞMEDİ (aynı sorgular, aynı sıra); yalnız artık worker thread'de çalışıyor ve
    (verimlilik için, davranış değişmeden) TEK bağlantı üzerinden art arda commit ediyor."""
    db = get_db()
    try:
        candidate = db.execute("SELECT * FROM candidates WHERE id=?", (effective_candidate_id,)).fetchone()
        candidate_level = (candidate["level"] or 1) if candidate else None
        if not candidate or candidate_level not in (2, 3):
            return {"ok": False}
        interview = db.execute("SELECT completed_at FROM interviews WHERE candidate_id=? AND level=?",
                               (effective_candidate_id, candidate_level)).fetchone()
        if interview and interview["completed_at"]:
            # Zaten finalize edilmiş bir görüşmeye geç kalan bir heartbeat gelmiş olabilir; sessizce yoksay.
            return {"ok": True, "already_completed": True}
        if not interview:
            db.execute("INSERT INTO interviews (candidate_id, level, messages) VALUES (?, ?, '[]')",
                       (effective_candidate_id, candidate_level))
            db.commit()
        # İŞ EMRİ madde B — bu heartbeat'in geldiği satırın GERÇEK/aktif canlı sahibi olduğumuzu
        # yansıt: realtime_owner_at'i tazeler ki create_realtime_session'daki stale-eşiği doğru
        # çalışsın (yalnız YENİ bir /session çağrısı kendi owner_token'ını yazar — burada token
        # DEĞİŞTİRİLMEZ, yalnız "son görülme" zamanı ilerletilir).
        db.execute("UPDATE interviews SET realtime_owner_at=CURRENT_TIMESTAMP "
                   "WHERE candidate_id=? AND level=? AND completed_at IS NULL",
                   (effective_candidate_id, candidate_level))
        db.commit()
        if transcript:
            save_interview_state(db, effective_candidate_id, [{"role": "user", "content": transcript}], candidate_level)
            db.commit()
        return {"ok": True, "level": candidate_level}
    finally:
        db.close()


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

    result = await asyncio.to_thread(_sync_realtime_progress_sync, effective_candidate_id, data.transcript)
    if not result.get("ok"):
        return {"ok": False}
    if result.get("already_completed"):
        return {"ok": True, "already_completed": True}
    candidate_level = result["level"]

    if data.usage_delta:
        await asyncio.to_thread(record_realtime_usage_summary, effective_candidate_id, candidate_level,
                                get_realtime_model(candidate_level), data.usage_delta, action="realtime_heartbeat")

    await asyncio.to_thread(record_realtime_events, effective_candidate_id, candidate_level, data.events)

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
    # GÖREV 4.3 — Kader raporunda görülen sessizlik halüsinasyonları + yaygın altyazı imzaları
    "i'll see you", "i'll see you.", "see you next time", "see you soon", "please do that",
    "please do that.", "please do", "you're welcome", "thank you for watching", "subscribe",
    "like and subscribe", "don't forget to subscribe", "altyazı m.k", "altyazi m.k.",
    "altyazi m.k", "türkçe altyazı", "çeviri", "www", ".com", "have a nice day", "take care",
    "good luck", "here we go", "let's go", "come on",
}

# GÖREV 4.2 — Realtime akışına sızabilen İÇ TALİMAT kalıpları. Bunlar seslendirilmeyen, modele
# verilen yönlendirmelerdir; transkripte GİRMEMELİDİR. (Kısa + soru işareti YOK + emir kipiyle biten.)
_PROMPT_LEAK_RE = re.compile(
    r"^\s*(burada|şimdi|sıradaki|bir sonraki|devam(ında)?|ardından)?\s*[\wçğıöşüİ ,]{0,30}?"
    r"\b(sor|sorun|sorabilirsin|geç|geçebilirsin|aç|kapat|derinleştir|netleştir|kontrol et|not al|yokla|teyit et|onayla|doğrula)\.?\s*$"
    r"|^\s*\[?(talimat|not|sistem|iç not|instruction|reminder|hatırlatma)\s*[:\]]"
    r"|^\s*(kriteri?|konuyu|bu konuyu)\s+(derinleştir|aç|kapat|geç|atla)\.?\s*$",
    re.IGNORECASE)

# GÖREV 4.1 — mülakatçı ağzından konuşma işaretleri (yüksek güven). Bir satır "Aday" etiketli
# ama bu işaretlerden 2+ taşıyorsa ve cevap niteliği yoksa → yanlış etiket / yankı.
# (Transkript Türkçe ama halüsinasyon/ascii ihtimaline karşı ı/i, ü/u toleransı var.)
_INTERVIEWER_MARKERS = [
    r"sor[uy](yorum|yu|muz)", r"sor[ae]y[ıi]m\b", r"sor[ae]l[ıi]m\b", r"soracağ[ıi]m\b",
    r"diyelim ki", r"senaryo", r"sadele[şs]tir(iyorum|elim|iyorum)", r"netle[şs]tir(elim|eyim|elim)",
    r"ge[çc](elim|iyorum)\b", r"[şs]imdi (farkl[ıi]|ba[şs]ka|[çc]ok k[ıi]sa|zorlay[ıi]c[ıi])",
    r"yetkinlik alan[ıi]na", r"son (olarak|bir)", r"te[şs]ekk[üu]r ederim.{0,30}(g[öo]r[üu][şs]me|m[üu]lakat)",
    r"eklemek.{0,20}ister misiniz", r"[öo]rnek ver(ir misiniz|in)\b", r"anlat[ıi]r m[ıi]s[ıi]n[ıi]z",
    r"\bne yapar(s[ıi]n[ıi]z|d[ıi]n[ıi]z)\b", r"nas[ıi]l (yakla[şs][ıi]r|ilerlersin)",
]
_ANSWER_MARKERS = [
    r"\bben\b", r"\bbizde\b", r"\byapt[ıi]m\b", r"\bettim\b", r"\bderim\b", r"\b[şs][öo]yle\b",
    r"\b[öo]nce\b.{0,30}\bsonra\b", r"\bkontrol eder", r"\bbakar[ıi]m\b", r"\bevet\b", r"\bhay[ıi]r\b",
    r"\bmezunuyum\b", r"\b[çc]al[ıi][şs]t[ıi]m\b", r"\bde[ğg]ilim\b", r"\bbilmiyorum\b", r"\bsan[ıi]r[ıi]m\b",
]

def _norm_line_text(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\wçğıöşüİ ]", "", (s or "").lower())).strip()

def _infer_line_role(low: str):
    """Damgasız/etiketsiz bir satırın rolünü işaretlerden tahmin eder. Belirsizse None."""
    if not low or len(low.split()) < 3:
        return None
    im = sum(1 for pat in _INTERVIEWER_MARKERS if re.search(pat, low))
    am = sum(1 for pat in _ANSWER_MARKERS if re.search(pat, low))
    _q_end = bool(re.search(r"(sor[ae]y[ıi]m|soruyorum|sorar[ıi]m|soral[ıi]m)\s*\.?\s*$", low))
    if am == 0 and (im >= 2 or (im >= 1 and _q_end)):
        return "mulakatci"
    if im == 0 and am >= 2:
        return "aday"
    return None

def fix_transcript_speaker_and_leaks(transcript_text: str, lang: str = "tr"):
    """TUR 1 GÖREV 4.1/4.2/4.4 + TUR 4 GÖREV 1 — sesli mülakat transkriptinde:
      - iç talimat sızıntısı satırlarını KALDIRIR (4.2)
      - hoparlör YANKISI satırlarını KALDIRIR (4.1)
      - 'Aday'↔'Mülakatçı' yanlış etiketleri çevirir; kendi damgası OLMAYAN satırlara da (1.4)
      - TUR 4 (GENEL KURAL — TAHMİN MEKANİZMASI KALDIRILDI): bir satırın KENDİ '[mm:ss]'
        damgası yoksa, o damga ASLA uydurulmaz/devralınmaz/yeniden yerleştirilmeye
        ÇALIŞILMAZ (1.1/1.2). Böyle satırlar transkriptin SONUNDA, ayrı bir başlık altında
        (UNPLACED_LINES_HEADING), damgasız ve ETİKETİ DÜZELTİLMİŞ olarak toplanır (1.3);
        ana akışa KARIŞMAZ. Hiç yoksa başlık hiç basılmaz.
      - kendi damgalı satırlar arasında aynı damgayı iki konuşmacıya vermez; ardışık
        birebir tekrarları eler (4.4)
    Dönüş: (duzeltilmis_metin, [{tip, ts, text}] değişiklik kayıtları)."""
    if not transcript_text or not transcript_text.strip():
        return transcript_text or "", []
    parsed = []
    _line_no = 0
    for ln in transcript_text.splitlines():
        raw = ln.rstrip()
        if not raw.strip() or raw.strip() == UNPLACED_LINES_HEADING:
            continue
        _line_no += 1
        m = _VOICE_LINE_RE.match(raw)
        if m:
            secs = int(m.group(1)) * 60 + int(m.group(2))
            role = "aday" if m.group(3).startswith("Ada") else "mulakatci"
            parsed.append({"secs": secs, "role": role, "text": m.group(4).strip(), "had_ts": True, "_ord": _line_no})
        elif raw.strip().startswith(("Aday:", "Mülakatçı:")):
            role = "aday" if raw.strip().startswith("Aday:") else "mulakatci"
            # TUR 4 / GÖREV 1.2 — damga YOK, UYDURULMAYACAK. secs=None kalıcıdır.
            parsed.append({"secs": None, "role": role, "text": raw.split(":", 1)[1].strip(), "had_ts": False, "_ord": _line_no})
        else:
            parsed.append({"secs": None, "role": None, "text": raw.strip(), "had_ts": False, "_ord": _line_no})
    if not parsed:
        return transcript_text, []
    in_count = len(parsed)

    changes = []
    kept = []
    for i, p in enumerate(parsed):
        txt, low = p["text"], _norm_line_text(p["text"])
        # 4.2 — iç talimat sızıntısı: kısa + soru işareti YOK + emir kipiyle biten yönerge
        if "?" not in txt and len(low.split()) <= 8 and _PROMPT_LEAK_RE.search(txt):
            changes.append({"tip": "prompt_sizintisi", "ts": p["secs"], "text": txt[:160]})
            continue
        # 4.1 — hoparlör yankısı: ORİJİNAL METİNDEKİ konum yakınlığına bakar (±4 satır),
        # zaman damgasına değil — damgasız satırlarda da çalışır.
        if p["role"] == "aday" and low and len(low) >= 12:
            echo = False
            for j in range(max(0, i - 4), min(len(parsed), i + 5)):
                q = parsed[j]
                if j == i or q["role"] != "mulakatci":
                    continue
                if p["secs"] is not None and q["secs"] is not None and abs(p["secs"] - q["secs"]) > 20:
                    continue
                ql = _norm_line_text(q["text"])
                if not ql:
                    continue
                a, b = set(low.split()), set(ql.split())
                if low in ql or ql in low or (a and len(a & b) / max(1, min(len(a), len(b))) >= 0.7):
                    echo = True
                    break
            if echo:
                changes.append({"tip": "hoparlor_yankisi", "ts": p["secs"], "text": txt[:160]})
                continue
        # 1.4 — yanlış/eksik etiket (İKİ YÖNLÜ, DAMGASIZ satırlar DAHİL): işaretlerden çıkar
        _inf = _infer_line_role(low)
        if p["role"] is None and _inf:
            changes.append({"tip": f"etiket_atandi_->{ _inf}", "ts": p["secs"], "text": txt[:160]})
            p = dict(p, role=_inf)
        elif p["role"] == "aday" and _inf == "mulakatci":
            changes.append({"tip": "etiket_duzeltildi_aday->mulakatci", "ts": p["secs"], "text": txt[:160]})
            p = dict(p, role="mulakatci")
        elif p["role"] == "mulakatci" and _inf == "aday":
            changes.append({"tip": "etiket_duzeltildi_mulakatci->aday", "ts": p["secs"], "text": txt[:160]})
            p = dict(p, role="aday")
        elif p["role"] is None:
            # rol işaretlerden çıkarılamadı — hiç prefiksi olmayan, önceki satırın DOĞRUDAN
            # devamı bir metin parçası (ör. çok satırlı Whisper metni). Bu SADECE kimin
            # konuştuğuna dair bir varsayımdır, KONUM/DAMGA varsayımı DEĞİLDİR.
            p = dict(p, role=(kept[-1]["role"] if kept else "aday"))
        kept.append(p)

    # TUR 4 / GÖREV 1.1 — "kurtarma re-interleave" TAMAMEN KALDIRILDI. Kendi damgası olan
    # satırlar (dated) zaman sırasına, kendi damgası OLMAYANLAR (undated) transkriptin SONUNA,
    # ayrı başlık altında, orijinal göreli sıralarıyla (_ord) — YER TAHMİNİ YOK.
    dated = sorted((p for p in kept if p["secs"] is not None), key=lambda p: (p["secs"], p["_ord"]))
    undated = sorted((p for p in kept if p["secs"] is None), key=lambda p: p["_ord"])

    def _emit(rows, allow_ts):
        lines, last_sig, last_secs_by_role = [], None, {}
        for p in rows:
            sig = (p["role"], _norm_line_text(p["text"]))
            if sig == last_sig:
                continue
            last_sig = sig
            role_label = "Aday" if p["role"] == "aday" else ("Mülakatçı" if p["role"] == "mulakatci" else "Aday")
            if allow_ts and p["secs"] is not None:
                secs = p["secs"]
                while last_secs_by_role.get(secs) not in (None, p["role"]):
                    secs += 1
                last_secs_by_role[secs] = p["role"]
                lines.append(f"[{secs // 60}:{secs % 60:02d}] {role_label}: {p['text']}")
            else:
                lines.append(f"{role_label}: {p['text']}")
        return lines

    out_lines = _emit(dated, allow_ts=True)
    if undated:
        out_lines.append("")
        out_lines.append(UNPLACED_LINES_HEADING)
        out_lines.extend(_emit(undated, allow_ts=False))
        print(f"[TRANSCRIPT_FIX] {len(undated)} satırın konumu belirlenemedi — "
              f"'{UNPLACED_LINES_HEADING}' altında damgasız toplandı (tahmin YAPILMADI).")

    # TUR 3/4 / GÖREV 1.5 — öncesi/sonrası satır sayısı + kaç düzeltme
    out_count = len(dated) + len(undated)
    _tip_ozet = ", ".join(sorted({c["tip"] for c in changes})) or "yok"
    print(f"[TRANSCRIPT_FIX] giriş {in_count} satır → {len(dated)} damgalı + {len(undated)} konumsuz "
          f"({in_count - out_count} kaldırıldı/birleşti); düzeltme türleri: {_tip_ozet}")
    return "\n".join(out_lines), changes

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

    # TUR 4 / GÖREV 6.6 — YAPISAL (DİL BAĞIMSIZ) sinyal: kısa bir kelimenin art arda AYNEN 3+ kez
    # tekrarı ("bye bye bye", "evet evet evet evet") — hangi dilde olursa olsun bir transkripsiyon/
    # sessizlik halüsinasyonu belirtisidir, İngilizce kelime listesine ihtiyaç duymaz. Aşağıdaki
    # dil-özel (leksik) sezgilerin aksine TÜM diller için (TR dahil) geçerlidir.
    _all_words = [w for w in norm.split(" ") if w]
    if len(_all_words) >= 3 and len(set(_all_words)) == 1 and len(_all_words[0]) <= 8:
        return True

    if not (lang or "tr").lower().startswith("tr"):
        # TUR 4 / GÖREV 6.6 — Türkçe OLMAYAN (ör. İngilizce) mülakatta LEKSİK (İngilizce kelime
        # listesi tabanlı) sezgiler KAPALI: bunlar TÜRKÇE bir oturuma sızan İngilizce dolgu metnini
        # yakalamak için kalibre edilmiştir; İngilizce bir mülakatta aynı sezgiler adayın GERÇEK
        # cevaplarını (kısa/gündelik İngilizce ifadeler dahil) yanlışlıkla halüsinasyon işaretler.
        # Bilinçli karar: yanlış filtreleme, filtrelememekten kötüdür — bu dilde yalnızca yukarıdaki
        # YAPISAL sinyal geçerlidir; leksik İngilizce-dolgu tespiti bu dilde YAPILMAZ (bkz. TUR 4
        # teslim notu — gerçek ses verisiyle doğrulanamadığı için TAHMİN edilmedi).
        return False
    if norm in _HALLUCINATION_PHRASES:
        return True
    words = [w for w in norm.split(" ") if w]
    ascii_only = re.fullmatch(r"[a-z0-9\s'.\-]+", norm) is not None
    if ascii_only and len(words) <= 2 and len(letters) <= 6:
        return True
    # GÖREV 4.3 — TR oturumunda saf İngilizce KISA segment (Türkçe harf yok, ≤6 kelime) ve içinde
    # meslek/teknik terim yoksa → sessizlik halüsinasyonu. Yaygın Whisper kalıpları (see/thank/
    # please/subscribe/watching/welcome) varsa kelime sayısına bakma.
    _EN_HALL_HINT = re.compile(r"\b(see you|thank|thanks|please|subscribe|watching|welcome|bye|goodbye|next time|take care|good luck|nice day|the end)\b")
    _TECH_KEEP = re.compile(r"\b(sap|erp|excel|sql|kdv|sgk|ifrs|iso|api|crm|hr|it|pdf|word|logo|netsis|mikro|luca|zirve)\b")
    if ascii_only and not _TECH_KEEP.search(norm):
        if _EN_HALL_HINT.search(norm):
            return True
        if len(words) <= 4 and re.fullmatch(r"[a-z'\-. ]+", norm) and re.search(r"\b(i|you|we|the|is|are|was|will|would|do|does|did|see|me|my|it|that|this)\b", norm):
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
        # TUR 2 / GÖREV C.6 — ZATEN işaretli satırı yeniden işaretleme (idempotent).
        if role and role.startswith("Ada") and spoken is not None and not is_hallucination_marker_line(line) \
                and is_likely_hallucination(spoken, lang):
            filtered.append({"ts": ts or "", "text": spoken.strip()[:200]})
            prefix = f"[{ts}] " if ts else ""
            out_lines.append(f'{prefix}Aday: [SİSTEM: bu satır olası transkripsiyon halüsinasyonudur — ADAY CEVABI SAYMA] "{spoken.strip()}"')
        else:
            out_lines.append(line)
    return "\n".join(out_lines), filtered, len(filtered)

def normalize_transcript_for_report(raw_text: str, lang: str = "tr"):
    """TUR 2 / GÖREV C — RAPOR ÜRETİM YOLUNUN TAMAMINDA kullanılan TEK transkript temizleyici.
    Sırayla: (1) konuşmacı etiketi / hoparlör yankısı / iç talimat sızıntısı / zaman damgası
    düzeltmesi (fix_transcript_speaker_and_leaks), (2) Whisper halüsinasyon filtresi.
    İDEMPOTENT: zaten temiz bir transkripte ikinci kez uygulanması sorun çıkarmaz.
    Dönüş: (clean_text, speaker_changes[list], hall_filtered[list], hall_n[int])."""
    if not raw_text or not raw_text.strip():
        return raw_text or "", [], [], 0
    spk_fixed, spk_changes = fix_transcript_speaker_and_leaks(raw_text, lang)
    clean, hall_filtered, hall_n = filter_transcript_hallucinations(spk_fixed, lang)
    return clean, spk_changes, hall_filtered, hall_n

# ═══ TUR 4 / GÖREV 2 — HAM TRANSKRİPT KORUNUR (GENEL KURAL, her aday için) ═══
# interviews.transcript_raw BİR KEZ yazılır (mülakat bitişinde, hiç temizlenmeden) ve BİR DAHA
# ÜZERİNE YAZILMAZ. Her düzeltme/yeniden-üretim HER SEFERİNDE bu ham veriden başlar — önceki
# çalıştırmanın ÇIKTISINDAN değil. Böylece hatalar turlar arasında BİRİKMEZ (2.1-2.4).
def capture_transcript_raw(candidate_id: int, level: int, raw_text: str) -> None:
    """Yalnızca transcript_raw HÂLÂ BOŞSA yazar (write-once). Doluysa dokunmaz."""
    if not raw_text or not raw_text.strip():
        return
    try:
        db = get_db()
        row = db.execute("SELECT transcript_raw FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, level)).fetchone()
        if row and not (row["transcript_raw"] or "").strip():
            db.execute("UPDATE interviews SET transcript_raw=? WHERE candidate_id=? AND level=?", (raw_text, candidate_id, level))
            db.commit()
        db.close()
    except Exception as e:
        print(f"UYARI (capture_transcript_raw c={candidate_id} L{level}): {type(e).__name__}: {e}")

def get_transcript_raw(candidate_id: int, level: int) -> str:
    """Ham transkripti döner. transcript_raw boşsa (eski kayıt — kolon bu turda eklendi),
    GÖREV 2.5: eldeki en iyi veriden (mevcut messages) BİR KEZ geriye dönük doldurur ve
    bundan sonra bir daha üzerine yazılmaz. Tahmin/onarım YAPILMAZ — olduğu gibi kopyalanır."""
    try:
        db = get_db()
        row = db.execute("SELECT transcript_raw, messages, started_at FROM interviews WHERE candidate_id=? AND level=?",
                         (candidate_id, level)).fetchone()
        db.close()
    except Exception as e:
        print(f"UYARI (get_transcript_raw fetch c={candidate_id} L{level}): {type(e).__name__}: {e}")
        return ""
    if not row:
        return ""
    existing = (row["transcript_raw"] or "").strip()
    if existing:
        return existing
    # GÖREV 2.5 — backfill: elde olan en iyi veri (mevcut messages), tahmin/onarım YOK.
    try:
        fallback = transcript_to_text(build_transcript_view(row["messages"] or "[]", level, row["started_at"]))
    except Exception as e:
        print(f"UYARI (get_transcript_raw backfill üretimi c={candidate_id} L{level}): {type(e).__name__}: {e}")
        fallback = ""
    if fallback.strip():
        capture_transcript_raw(candidate_id, level, fallback)
        print(f"[TRANSCRIPT_RAW_BACKFILL] c={candidate_id} L{level} — transcript_raw boştu, mevcut messages'tan BİR KEZ dolduruldu.")
    return fallback

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
    r"fikrim\s+yok|geçelim|geç(ebilir|ebilir\s*miy)|bir\s+sonrakine|hat[ıi]rlam[ıi]yorum|"
    # İŞ EMRİ — KRİTER KAPSAMA + YETERSİZ CEVAP PUANLAMA: "bu konuda deneyimim/tecrübem/bilgim yok"
    # türü beyanlar da değerlendirilebilir bir kanıt İÇERMEZ (bilmiyorum ile AYNI sınıf) — normal
    # puanlama yerine taban puan kuralına girsin diye burada da yakalanır.
    r"(bu\s+konuda\s+)?(deneyim|tecr[üu]be|bilgi)im\s+(hiç\s+)?yok)", re.IGNORECASE)

def _is_substantive_answer(txt: str) -> bool:
    t = (txt or "").strip()
    if len(re.sub(r"\s+", "", t)) < 12:
        return False
    if is_hallucination_marker_line(t) or is_likely_hallucination(t, "tr"):
        return False
    if _NONANSWER_RE.match(t):
        return False
    return True

# İŞ EMRİ — KRİTER KAPSAMA + YETERSİZ CEVAP PUANLAMA / madde 3 — TEK canonical taban puan hesabı:
# kriter yeterince sorgulanmış (ve INTERVIEWER_REASK_RULES'a göre uygun takip fırsatı verilmiş)
# olmasına rağmen değerlendirilebilir bir aday cevabı alınamıyorsa PUAN = tavanın %25'i. "Değerlen-
# dirilemedi" (payda dışı) DEĞİLDİR — kriter DEĞERLENDİRİLMİŞ sayılır, payda İÇİNDE kalır. Kesirli
# sonuç sistemin TEK canonical yuvarlama kuralından (_round_half_up, ROUND_HALF_UP) geçer — burada
# yeni/bağımsız bir yuvarlama YOK.
def _insufficient_answer_floor_score(cap: int) -> int:
    cap = _safe_int(cap)
    if cap <= 0:
        return 0
    return max(0, min(cap, _round_half_up(cap * 0.25)))

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
    İŞ EMRİ — KRİTER KAPSAMA + YETERSİZ CEVAP PUANLAMA: yalnız 'not_asked' → SİSTEM kaynaklı eksik
    (PAYDA DIŞI, 'Değerlendirilemedi'). 'asked_no_valid_answer' artık 'Değerlendirilemedi' DEĞİL —
    kriter SORULMUŞ sayılır, TABAN PUAN (tavanın %25'i, bkz. _insufficient_answer_floor_score) ile
    PAYDA İÇİNDE puanlanır (çağıranlar: recompute_and_fix_score, recompute_profile_section)."""
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

# ============ 2026-09 RAPOR YENİDEN TASARIMI — sunum katmanı yardımcıları ============
# recompute_and_fix_score/recompute_profile_section (aşağıda, DEĞİŞTİRİLMEDİ) hâlâ TOPLAM
# PUAN/PROFİL PUANI satırını + 'Kriter Eksiklik Ayrımı' bloğunu metne EKLİYOR — bu, extract_score/
# extract_profile_score'un okuduğu iç işaretleyicidir ve puanlama motorunun kendisi hâlâ güvenilir,
# test edilmiş haliyle DOKUNULMADAN çalışıyor (asgari müdahale). Ama YENİ rapor tasarımında toplam
# puan yalnızca üstteki 'Değerlendirme Puanları' tablosunda görünür (iş emri madde 2.3 — tekrar
# yapma) ve eksiklik detayları EK 3'e taşınır (madde 7) — bu yüzden GÖRÜNÜR gövdeye basmadan önce
# bu iki bloğu metinden ayıklıyoruz. Puanlama SONUCU (final_score/final_profile) ETKİLENMEZ; yalnız
# GÖRÜNÜM temizleniyor.
def _strip_total_line_for_display(text: str, is_profile: bool) -> str:
    if not text:
        return text or ""
    total_re = (r"(?m)^\s*\**\s*PROF\S*\s+PUANI\s*[:：][^\n]*\**[^\n]*$" if is_profile
               else r"(?m)^\s*\**\s*TOPLAM\s+PUAN\s*[:：][^\n]*\**[^\n]*$")
    text = re.sub(total_re, "", text)
    text = re.sub(r"(?ms)^\s*\**\s*(?:Kriter Eksiklik Ayrımı|Profil Veto Kontrolü)\s*\(sistem\)\s*:?\**.*?(?=\n\s*\n|\Z)", "", text)
    text = re.sub(r"(?m)^\s*[-•]\s*(Değerlendirilemedi \(sistem\)|0 puan \(paydada\))[^\n]*$", "", text)
    text = re.sub(r"(?m)^\s*Veto yok\.?\s*$", "", text)
    # İş emri madde 7 — "Neden değerlendirilemediğini Teknik Veriler ekine koy": tablo
    # HÜCRESİNDE yalnız kısa işaret kalır ("değerlendirilmedi"), uzun sistem gerekçesi EK 3'e
    # taşınmıştı zaten (_score_warnings → build_technical_annex'e eklenen liste, finalize_interview).
    text = re.sub(r"\|([^|]*?)Değerlendirilemedi \(sistem\)\s*[—\-–:][^|\n]*\|", r"|\1Değerlendirilemedi (sistem) |", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text

def _extract_longest_table_block(lines: list) -> tuple:
    """En uzun ardışık 'en az 2 boru (|) içeren satır' grubunu döner: (table_lines, rest_lines)."""
    idx = [i for i, ln in enumerate(lines) if ln.count("|") >= 2]
    if not idx:
        return [], list(lines)
    runs, cur = [], [idx[0]]
    for i in idx[1:]:
        if i == cur[-1] + 1:
            cur.append(i)
        else:
            runs.append(cur)
            cur = [i]
    runs.append(cur)
    best = max(runs, key=len)
    return lines[best[0]:best[-1] + 1], lines[:best[0]] + lines[best[-1] + 1:]

def _criterion_award(name: str, table_text: str):
    """Bir kriter tablosu METNİNDEN (Pozisyon Yetkinlikleri ya da Kişisel/Bilişsel Profil, ayrı
    ayrı — asla ikisi birden) verilen kritere en iyi eşleşen satırın puan hücresini okur.
    Dönüş: (awarded:int, cap:int) | None (satır bulunamadı / sayısal puan yok — sistem/aday
    kaynaklı eksik, açık metin)."""
    nn = _norm_name(name)
    best = None
    for ln in (table_text or "").splitlines():
        if ln.count("|") < 2:
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        c0 = _norm_name(re.sub(r"[*_`]", "", cells[0]))
        if len(c0) < 3 or set(cells[0].replace(" ", "")) <= set("-:|"):
            continue
        score_c0 = _name_score(name, cells[0])
        if score_c0 < 0.55 and not (nn and nn[:14] in c0):
            continue
        mm = re.search(r"(?<![\d:/])(\d{1,3})\s*/\s*(\d{1,3})(?![\d:/])", cells[1])
        if not mm:
            continue
        cand = (int(mm.group(1)), int(mm.group(2)))
        if best is None or score_c0 > best[0]:
            best = (score_c0, cand)
    return best[1] if best else None

def compute_reviewer_overall(criteria_list: list, primary_table_text: str, reviewer_scores: dict, id_prefix: str = "P"):
    """İkinci değerlendiricinin GENEL puanını (pozisyon YA DA profil — çağıran hangi kriter
    listesini/tabloyu/kimlik önekini ('P' ya da 'K') verirse o) türetir: her kriter için
    reviewer'ın KENDİ puanı varsa onu, yoksa birincilin (zaten normalize edilmiş) puanını
    kullanır — reviewer'ın hiç değinmediği kriterlerde 'sessizce aynı fikirde' varsayımı GENEL
    KURAL olarak uygulanır (madde 21 — bir katman diğerinin puanını DEĞİŞTİRMEZ; bu yalnızca
    reviewer'ın KENDİ toplamını hesaplamak için birincilin sayısını ödünç alır).
    GÖREV 6.1+6.2 — eşleştirme KİMLİK üzerinden (ADA göre değil); tavan HER ZAMAN kriter
    listesinden (modelin kendi yazdığı 'maksimum' asla güvenilmez).
    Dönüş: normalize edilmiş puan (0-100) | None (değerlendirilebilir kriter yok)."""
    awarded_sum, cap_sum = 0, 0
    for i, c in enumerate(criteria_list, start=1):
        cid = f"{id_prefix}{i}"
        name = c["name"] if isinstance(c, dict) else c
        cap = _safe_int(c.get("weight")) if isinstance(c, dict) else None
        rv = reviewer_scores.get(cid)
        if rv is not None:
            awarded, _rcap_reported_by_model = rv
            eff_cap = cap
            if eff_cap is None or eff_cap <= 0:
                continue
            awarded = max(0, min(awarded, eff_cap))
        else:
            prim = _criterion_award(name, primary_table_text)
            if prim is None:
                continue
            awarded, eff_cap = prim
            if cap:
                eff_cap = cap
        if eff_cap is None or eff_cap <= 0:
            continue
        awarded_sum += awarded
        cap_sum += eff_cap
    if cap_sum <= 0:
        return None
    # İŞ EMRİ — NİHAİ RAPOR TUTARLILIĞI: TEK canonical yuvarlama (_round_half_up, madde 5) —
    # ikinci değerlendiricinin kendi pozisyon/profil puanı da bu final skorlara girdi olduğu için
    # aynı kuraldan geçmeli.
    return max(0, min(100, _round_half_up(awarded_sum / cap_sum * 100)))

def render_cv_ozeti_fallback(candidate: dict, transcript: str = "") -> str:
    """CV Özeti — model üretemediyse/çok kısaysa DETERMİNİSTİK yedek. İş emri madde 13: boş alan
    YAZILMAZ — yalnızca gerçekten bilgi olan satırlar basılır."""
    c = candidate or {}
    fields = resolve_cv_fields(c, transcript)
    lines = []
    def _add(label, key):
        val, src = fields.get(key, (None, ""))
        if val in (None, "", 0):
            return
        suffix = "" if src == "form" else "  (kaynak: CV/mülakat tahmini)"
        lines.append(f"{label}: {val}{suffix}")
    _add("Eğitim", "education")
    exp_val, exp_src = fields.get("experience_years", (None, ""))
    if exp_val not in (None, "", 0):
        lines.append(f"Deneyim: {exp_val} yıl" + ("" if exp_src == "form" else "  (kaynak: CV/mülakat tahmini)"))
    cv_text = normalize_cv_for_analysis((c.get("cv_text") or "").strip())
    certs = sorted({m.group(0) for m in re.finditer(
        r"\b(CFA|CPA|SMMM|ACCA|CMA|PMP|CIA|FRM|SPK|IFRS|ISO\s?\d+|Six Sigma|Prince2|ITIL|AWS|Azure|GCP|PMI)\b",
        cv_text, re.IGNORECASE)})
    if certs:
        lines.append("Sertifikalar: " + "; ".join(certs))
    langs = sorted({m.group(0).capitalize() for m in re.finditer(
        r"\b(İngilizce|English|Almanca|German|Deutsch|Fransızca|French|İspanyolca|Arapça|Rusça)\b",
        cv_text, re.IGNORECASE)})
    if langs:
        lines.append("Diller: " + "; ".join(langs))
    return "\n".join(lines)

# İş emri — KAYIP ANLATI BÖLÜMLERİ / GÖREV 4 (2026-09, sonraki tur) — CV Özeti ayraç formatı.
# KÖK NEDEN: prompt, etiketleri "Eğitim / Deneyim / Teknik Yetkinlikler / ..." biçiminde
# LİSTELİYORDU (etiket adlarını "/" ile ayırarak) — model bunu satır formatının KENDİSİ sanıp
# "Eğitim / Lise mezunu" yazdı (ayraç ":" olacaktı). Prompt netleştirildi (bkz.
# build_report_content_prompt) AMA model yine de eski davranışı sürdürebilir — bu yüzden ayraç
# burada DA deterministik olarak düzeltilir (modelin yazdığı ayraç GÜVENİLMEZ).
_CV_OZETI_LABELS = ["Eğitim", "Deneyim", "Teknik Yetkinlikler", "Sektör Yetkinlikleri", "Diller", "Sertifikalar"]
_CV_OZETI_LABEL_SEP_RE = re.compile(
    r"^(\s*(?:" + "|".join(re.escape(l) for l in _CV_OZETI_LABELS) + r"))\s*[/\-–—]\s*", re.IGNORECASE)

def normalize_cv_ozeti_separators(text: str) -> str:
    """GÖREV 4.1 — CV Özeti satırlarındaki etiket/içerik ayracı DETERMİNİSTİK olarak ':' yapılır;
    modelin yazdığı ayraç (ör. '/') güvenilmez. Yalnız BİLİNEN etiketlerle (Eğitim/Deneyim/...)
    başlayan satırlara uygulanır — zaten ':' kullanan satırlarda no-op, başka metni bozmaz."""
    if not text:
        return text
    out = []
    for ln in text.splitlines():
        m = _CV_OZETI_LABEL_SEP_RE.match(ln)
        out.append(_CV_OZETI_LABEL_SEP_RE.sub(m.group(1).strip() + ": ", ln, count=1) if m else ln)
    return "\n".join(out)

# GÖREV 4.2 — bilinen okul-türü adları; SINIRLI/bounded bir liste — genel bir anlam-sadakati
# denetleyicisi DEĞİLDİR, yalnız bu turda raporlanan somut vakayı ("ticaret meslek lisesi" →
# "Lise mezunu" genellemesi) ve benzer sık görülen okul türlerini kapsar.
_SPECIFIC_SCHOOL_TYPE_RE = re.compile(
    r"(ticaret meslek lisesi|anadolu lisesi|fen lisesi|imam hatip lisesi|meslek lisesi|"
    r"end[üu]stri meslek lisesi|sosyal bilimler lisesi|g[üu]zel sanatlar lisesi|spor lisesi)",
    re.IGNORECASE)

def restore_specific_school_type(cv_ozeti_text: str, transcript: str) -> tuple:
    """GÖREV 4.2 — aday transkriptte SPESİFİK bir okul türü belirtmişse ama CV Özeti'nin Eğitim
    satırı bunu genel bir kategoriye ('Lise mezunu') İNDİRGEMİŞSE, spesifik ifade GERİ konur.
    Dönüş: (yeni_metin, değişti_mi)."""
    if not cv_ozeti_text or not transcript:
        return cv_ozeti_text, False
    m_specific = _SPECIFIC_SCHOOL_TYPE_RE.search(transcript)
    if not m_specific:
        return cv_ozeti_text, False
    specific = m_specific.group(1)
    lines = cv_ozeti_text.splitlines()
    changed = False
    for i, ln in enumerate(lines):
        if re.match(r"^\s*E[ğg]itim\s*:", ln, re.IGNORECASE) and specific.lower() not in ln.lower():
            if re.search(r"\blise\b", ln, re.IGNORECASE):
                lines[i] = re.sub(r"\blise\b", specific.title(), ln, count=1, flags=re.IGNORECASE)
                changed = True
    return ("\n".join(lines), True) if changed else (cv_ozeti_text, False)

def render_beyan_tutarliligi(disc: dict) -> str:
    """Beyan Tutarlılığı — TAMAMEN deterministik (iş emri madde 14): yalnızca GERÇEK çelişki
    varsa satır üretir; 'tutarlı'/'karşılaştırılamadı' satırları hiç basılmaz (rapora tekrar/
    gürültü eklemez), çelişki yoksa fonksiyon boş döner (bölüm hiç oluşturulmaz)."""
    rows = [r for r in ((disc or {}).get("rows") or []) if r.get("durum") == "çelişki"]
    if not rows:
        return ""
    lines = []
    for r in rows:
        src = "; ".join(f"{k}: {v}" for k, v in (r.get("kaynaklar") or {}).items())
        lines.append(f"{r['alan']} — {src}" + (f" ({r['not']})" if r.get("not") else ""))
    return "\n".join(lines)

# İş emri — RAPOR İÇERİK STANDARDI / B4 — KÖK NEDEN: Yönetici Özeti serbest metin olarak üretiliyor,
# Beyan Tutarlılığı'nın (deterministik) bir alanda ÇELİŞKİ tespit ettiğinden HABERSİZ — model
# genellikle kaynaklardan BİRİNİ (ör. CV'deki "10 yıl") seçip özete taraf tutarak yazıyor (gerçek
# örnek: özet "yaklaşık 10 yıllık deneyim" derken Beyan Tutarlılığı "Sözlü 8 / CV 10" çelişkisini
# ayrıca kaydediyordu — okuyucu ikisini yan yana görmeden fark edemez). Model bunu YAZAMAZ çünkü
# çelişki tespiti kendisinden SONRA, deterministik olarak yapılıyor — çözüm PROMPT değil, ÜRETİLEN
# METNİ bu bilgiyle SONRADAN denetlemek.
_FIELD_TOPIC_HINT_RE = {
    "Deneyim yılı": re.compile(r"deneyim|tecr[üu]be|y[ıi]ld[ıi]r|senedir", re.IGNORECASE),
    "Eğitim (seviye)": re.compile(r"mezun|e[ğg]itim|okul|lise|lisans|[üu]niversite|yüksekokul", re.IGNORECASE),
}

def strip_yonetici_ozeti_discrepancy_bias(text: str, disc: dict) -> tuple:
    """B4 — Yönetici Özeti'nde, Beyan Tutarlılığı'nın 'çelişki' işaretlediği bir alan (Deneyim
    yılı/Eğitim seviyesi) hakkında TEK BİR KAYNAĞIN değerini iddia eden cümle varsa (taraf
    seçiyorsa) bu cümle çıkarılır, yerine çelişkiye NÖTR biçimde atıf yapan bir cümle konur.
    Dönüş: (yeni_metin, değişti_mi)."""
    rows = [r for r in ((disc or {}).get("rows") or []) if r.get("durum") == "çelişki"]
    if not rows or not text:
        return text, False
    sents = re.split(r'(?<=[.!?])\s+', text)
    changed = False
    out_sents = []
    for s in sents:
        hit = None
        for r in rows:
            hint = _FIELD_TOPIC_HINT_RE.get(r.get("alan", ""))
            if not hint or not hint.search(s):
                continue
            for v in (r.get("kaynaklar") or {}).values():
                v = str(v).strip()
                if v and re.search(r'(?<!\w)' + re.escape(v) + r'(?!\w)', s, re.IGNORECASE):
                    hit = r
                    break
            if hit:
                break
        if hit:
            changed = True
            out_sents.append(f"{hit['alan']} konusunda kaynaklar arasında çelişki bulunmaktadır (bkz. Tutarlılık / Çelişki Analizi).")
        else:
            out_sents.append(s)
    return (" ".join(out_sents).strip(), changed)

# İş emri — KAYIP ANLATI BÖLÜMLERİ / GÖREV 1.4 (2026-09, sonraki tur) — Puanlama Kapsamı TAMAMEN
# DETERMİNİSTİK: hangi kriterler değerlendirildi/değerlendirilemedi, puan kaç kriter üzerinden
# hesaplandı — bu bilgi zaten sistemde var (apply_structured_rationale_gate/apply_criterion_
# takeover'ın log'ları), modelden İSTENMEZ. Bu bölüm hem eski "Puanlama Kapsamı" hem "Değerlendir-
# ilemeyen Alanlar" bölümlerinin YERİNİ alır (aynı bilgiyi iki ayrı başlıkta TEKRARLAMAMAK için —
# bkz. _REPORT_SECTION_ALIASES'taki not) HEM DE bir önceki turun "yuksek_dusme_orani" görünür
# notunun yerini alır (GÖREV 1.4: "Bu madde GÖREV 3'teki düşme oranı notunun yerini alır").
_PUANLAMA_KAPSAMI_HEAD = "**Puanlama Kapsamı:**"
_PUANLAMA_KAPSAMI_RE = re.compile(re.escape(_PUANLAMA_KAPSAMI_HEAD) + r".*?(?=\n\n|\Z)", re.DOTALL)

# İş emri — RAPOR İÇERİK STANDARDI / A2 — Puanlama Kapsamı ile AYNI desen: append_reviewer_section
# 2. değerlendirici Genel Puan'ı güncellediğinde Öneri Gerekçesi'ni bu regex'le YERİNDE yeniden yazar.
_ONERI_GEREKCESI_HEAD = "**Öneri Gerekçesi:**"
_ONERI_GEREKCESI_RE = re.compile(re.escape(_ONERI_GEREKCESI_HEAD) + r".*?(?=\n\n|\Z)", re.DOTALL)

# İş emri — KAPI EŞİĞİ VE SON TUTARLILIK / MADDE 2 — Puanlama Kapsamı ile AYNI desen: devralma
# sonrası Puanlama Kapsamı yeniden yazılırken Değerlendirilemeyen Alanlar da AYNI (yeniden
# hesaplanmış) _dropped_pos2/_dropped_prof2 listesiyle yeniden yazılır — önceden yalnız Puanlama
# Kapsamı güncelleniyordu, bu bölüm devralma ÖNCESİ listede donuyordu (A1'de kapatılan çelişki
# farklı bir yoldan geri geldi).
_DEGERLENDIRILEMEYEN_ALANLAR_HEAD = "**Değerlendirilemeyen Alanlar:**"
_DEGERLENDIRILEMEYEN_ALANLAR_RE = re.compile(re.escape(_DEGERLENDIRILEMEYEN_ALANLAR_HEAD) + r".*?(?=\n\n|\Z)", re.DOTALL)

def render_puanlama_kapsami(pos_criteria: list, prof_criteria: list, dropped_pos_names: list, dropped_prof_names: list) -> str:
    """GÖREV 1.4 — eski raporun 'Puanlama Kapsamı' bölümünün TAMAMEN deterministik karşılığı. Eski
    raporun kendi cümle kalıbına sadık kalır ('... kriterleri değerlendirildi. Değerlendirilmeyen
    kriter olmadı. ...').
    NOT (RAPOR ANLATI KATMANI GERİ EKLEME turu, sonraki iş emri) — önceki turda bu fonksiyon
    'Değerlendirilemeyen Alanlar'ın da YERİNE geçiyordu (ayrı bölüm açılmamıştı); bu turda iş emri
    AÇIKÇA 'Değerlendirilemeyen Alanlar' bölümünü AYRI istedi — render_degerlendirilemeyen_alanlar
    AYNI dropped_pos_names/dropped_prof_names girdisini kullanır (iş emri: 'ikisi asla farklı şey
    söylemeyecek') ama KENDİ başlığı altında basılır.
    NOT (RAPOR İÇERİK STANDARDI turu, BÖLÜM C — normalizasyon şeffaflığı): önceki sürüm yalnızca
    kriter SAYISI (int) alıyordu, PAYDA (ağırlık toplamı) hiç yazılmıyordu — aynı transkript, düşen
    kriter sayısı 3'ten 4'e çıkınca farklı paydayla (50→30 puan) puanlanabiliyordu ve rapor bunu HİÇ
    açıklamıyordu. Artık kriter LİSTESİ (ad+ağırlık) alınır, değerlendirilen kriterlerin toplam
    ağırlığı (asıl normalizasyon paydası) AÇIKÇA yazılır."""
    pos_criteria = pos_criteria or []
    prof_criteria = prof_criteria or []
    total_pos, total_prof = len(pos_criteria), len(prof_criteria)
    dropped = list(dropped_pos_names) + list(dropped_prof_names)
    total = total_pos + total_prof
    evaluated = total - len(dropped)
    lines = [f"{evaluated}/{total} kriter değerlendirildi (pozisyon: {total_pos - len(dropped_pos_names)}/{total_pos}, "
            f"profil: {total_prof - len(dropped_prof_names)}/{total_prof})."]
    if dropped:
        lines.append(f"Değerlendirilemeyen kriterler: {', '.join(dropped)}.")
    else:
        lines.append("Değerlendirilmeyen kriter olmadı.")
    pos_w = sum(_safe_int(c.get("weight")) for c in pos_criteria if c.get("name") not in dropped_pos_names)
    prof_w = sum(_safe_int(c.get("weight")) for c in prof_criteria if c.get("name") not in dropped_prof_names)
    if pos_criteria:
        lines.append(f"Pozisyon puanı, değerlendirilen {total_pos - len(dropped_pos_names)} kriterin toplam {pos_w} ağırlık puanı üzerinden normalize edilmiştir.")
    if prof_criteria:
        lines.append(f"Profil puanı, değerlendirilen {total_prof - len(dropped_prof_names)} kriterin toplam {prof_w} ağırlık puanı üzerinden normalize edilmiştir.")
    return "\n".join(lines)

def render_degerlendirilemeyen_alanlar(dropped_pos_names: list, dropped_prof_names: list) -> str:
    """ADIM 2 — Puanlama Kapsamı İLE AYNI kaynak veriden (dropped_pos_names/dropped_prof_names)
    türer; iş emri kuralı: 'ikisi asla farklı şey söylemeyecek' — aynı iki listeyi girdi alarak
    YAPI GEREĞİ garanti edilir (iki AYRI hesaplama YOK)."""
    dropped = list(dropped_pos_names) + list(dropped_prof_names)
    if not dropped:
        return "Değerlendirilemeyen kriter yok — tüm kriterler değerlendirildi."
    return "Değerlendirilemeyen kriterler: " + ", ".join(dropped) + "."

def render_tutarlilik_celiski_analizi(disc: dict) -> str:
    """ADIM 2 — Beyan Tutarlılığı (render_beyan_tutarliligi) İLE AYNI kaynak veriden (disc —
    compute_field_discrepancies çıktısı) türer; iş emri kuralı: 'Beyan Tutarlılığı bölümündeki
    tespitlerle aynı kaynaktan beslenecek, çelişki varsa burada da görünecek. Çelişki yoksa bunu
    açıkça yazacak.'
    NOT (RAPOR İÇERİK STANDARDI turu, B1 — KÖK NEDEN): önceki sürüm satırları " | " ile TEK SATIRDA
    birleştiriyordu. _make_report_pdf'in _emit_report_block'u her bölüm içeriğini önce
    parse_markdown_table'a verir; o fonksiyon YALNIZ "bir satırda en az bir '|' var mı, bölününce
    ≥2 hücre çıkıyor mu" bakar (başlık/ayraç satırı ZORUNLU DEĞİL) — 2 çelişkili kriterle birleşen
    tek satır YANLIŞLIKLA 2 hücreli bir tablo SATIRI sanılıyordu; ne "Kriter" içerdiği için tabloya
    (clean_rows) alınıyor ne düz metin olarak akıyordu (consumed edildiği için) — İÇERİK TAMAMEN
    KAYBOLUYORDU, yalnız başlık kalıyordu (gerçek PDF'te doğrulandı). Fix: satırları "\\n" ile ayır
    (Beyan Tutarlılığı'nın zaten yaptığı gibi) — hiçbir satırda "|" karakteri OLMAZ."""
    rows = [r for r in ((disc or {}).get("rows") or []) if r.get("durum") == "çelişki"]
    if not rows:
        return "Çelişki taraması yapıldı; CV, sözlü beyan ve kayıt formu arasında belirgin bir çelişki tespit edilmedi."
    lines = []
    for r in rows:
        src = "; ".join(f"{k}: {v}" for k, v in (r.get("kaynaklar") or {}).items())
        lines.append(f"{r['alan']} — {src}" + (f" ({r['not']})" if r.get("not") else ""))
    return "Beyan Tutarlılığı bölümündeki tespitlerle aynı kaynaktan:\n" + "\n".join(lines)

# ADIM 2 — "Dil Gözlemi kendi içinde çelişmeyecek (gözlem yazıp ardından 'belirtilecek dil gözlemi
# yok' demek yasak)". Model artık UNCONDITIONAL olarak basılan bölümlerde bazen gerçek bir gözlem
# YAZIP ardından eski alışkanlıkla kendini çürüten bir dolgu cümlesi de ekleyebilir — bu cümle
# (yalnız KENDİSİ, gerçek gözlem cümlesi DEĞİL) çıkarılır.
_SELF_NEGATING_FILLER_RE = re.compile(r"belirtilecek (?:bir )?[\wçğıöşü ]{0,30}\byok\b\.?", re.IGNORECASE)
# İş emri — RAPOR İÇERİK STANDARDI / A3 — KÖK NEDEN: önceki sürüm eşleşen cümleyi BÜTÜNÜYLE atıyordu.
# Model çoğunlukla TEK bir cümle içinde gerçek gözlemi bir bağlaçla ("ancak/fakat/ayrıca/bunun
# dışında") dolgu ifadesine bağlıyor ("Aday X yaptı ancak ... belirtilecek bir gözlem yok.") —
# cümle bazlı atma bu durumda GERÇEK GÖZLEMİ DE siliyordu (gerçek örnekte Dil Gözlemi İKİ raporda
# da tamamen boş kaldı, "başka bölümler iletişim netliği hakkında bolca hüküm veriyor" olmasına
# rağmen). Fix: cümle TAMAMEN dolgu değilse (eşleşmeden ÖNCE bağlaçtan arındırılmış, en az 2
# kelimelik GERÇEK içerik varsa) yalnız bağlaç+dolgu kısmı çıkarılır, öndeki gözlem KORUNUR.
_FILLER_LEADING_CONNECTOR_RE = re.compile(r"[,;]?\s*(?:ancak|fakat|ama|ayr[ıi]ca|bunun d[ıi][şs][ıi]nda|ve)\s*$", re.IGNORECASE)

def strip_self_negating_filler(text: str) -> tuple:
    if not text:
        return text, []
    sents = re.split(r'(?<=[.!?])\s+', text)
    kept, dropped = [], []
    for s in sents:
        m = _SELF_NEGATING_FILLER_RE.search(s)
        if not m:
            kept.append(s)
            continue
        before = _FILLER_LEADING_CONNECTOR_RE.sub("", s[:m.start()].rstrip()).rstrip(" ,;")
        if len(before.split()) >= 2:
            # gerçek gözlem + sonradan eklenmiş dolgu köprüsü -> yalnız köprü+dolgu çıkar, gözlem KALIR.
            kept.append(before if before.endswith((".", "!", "?")) else before + ".")
        dropped.append(s.strip())
    return " ".join(kept).strip(), dropped

_NO_TIMESTAMP_EVIDENCE_FALLBACK = "Doğrulanabilir kanıt bulunamadı."
_NO_NARRATIVE_EVIDENCE_FALLBACK = "Bu bölüm için mülakatta doğrulanabilir bir bulgu tespit edilmedi."

def finalize_narrative_section(raw_text: str, require_timestamp: bool = False) -> str:
    """ADIM 2 — bölümler artık KOŞULSUZ basılır: veri yoksa/klişe temizliği sonrası boş kalırsa
    (ya da require_timestamp=True iken hiç [mm:ss] damgası yoksa — Analitik/Problem Çözme/Kavrama
    ve İletişim için ZORUNLU) sabit bir 'değerlendirilemedi' metni döner; bölüm BAŞLIĞI hiçbir
    zaman gizlenmez (finalize_interview'daki çağıran artık bu bölümleri koşulsuz parts.append eder)."""
    text = (raw_text or "").strip()
    if text:
        text, _ = strip_self_negating_filler(text)
    if not text:
        return _NO_TIMESTAMP_EVIDENCE_FALLBACK if require_timestamp else _NO_NARRATIVE_EVIDENCE_FALLBACK
    if require_timestamp and not _TS_RE.search(text):
        return _NO_TIMESTAMP_EVIDENCE_FALLBACK
    return text

# İŞ 5 — "ÖNE ÇIKAN PROJE VE DENEYİMLER" HEDEFLİ RECOVERY. İlk üretimde bu bölüm fallback'e
# düşmüş olabilir (main.py'deki ilk-üretim akışı DEĞİŞMEDİ) — ama validator+reviewer+takeover
# tamamlandıktan SONRA final kriter tablosunda zengin, doğrulanmış kanıt varsa, bölüme TEK bir
# hedefli "tekrar bak" şansı veriyoruz. Amaç bölümü ZORLA doldurmak DEĞİL: çıktı deterministik
# bir kapıdan (aşağıda) geçemezse mevcut fallback AYNEN korunur. Validator/scoring/reviewer/
# takeover/prompt/PDF'e ve İş 1-4 fonksiyonlarına DOKUNULMADI — bu, run_deferred_finish_job'da
# append_reviewer_section'dan SONRA çalışan, TAMAMEN AYRI bir ek adım.
_ONE_CIKAN_PROJE_HEAD = "**Öne Çıkan Proje ve Deneyimler:**"
_ONE_CIKAN_PROJE_RE = re.compile(re.escape(_ONE_CIKAN_PROJE_HEAD) + r"(.*?)(?=\n\n|\Z)", re.DOTALL)

def _build_candidate_evidence_context(transcript_view: list) -> str:
    """İŞ 6N-1 — one_cikan_proje recovery için KÜÇÜLTÜLMÜŞ context: yalnız role='aday' satırları,
    [mm:ss] damgasıyla. Mülakatçı turları (sorular) bu context'e HİÇ girmiyor — gate zaten yalnız
    role='aday' grounding arıyor (_timestamp_field_grounded, İş 2), mülakatçı metni modele gösterip
    sonra reddetmenin anlamı yok. HİÇBİR sektör/domain kelimesi ARANMIYOR (CRA/GCP/klinik vb. YOK,
    system-wide) — yalnız KONUŞMACI rolüne göre filtrelenir, TÜM aday turları dahil edilir."""
    lines = []
    for row in (transcript_view or []):
        if row.get("role") != "aday":
            continue
        text = (row.get("text") or "").strip()
        if not text:
            continue
        ts = row.get("ts") or ""
        lines.append(f"[{ts}] Aday: {text}" if ts else f"Aday: {text}")
    return "\n".join(lines)

def regenerate_one_cikan_proje(candidate_id: int, level: int, provider: str, model: str,
                               candidate_evidence_text: str, validated_evidence_block: str) -> Optional[str]:
    """İŞ 5/6N-1 — YALNIZ 'Öne Çıkan Proje ve Deneyimler' metnini hedefli olarak yeniden ürettirir
    (tam rapor DEĞİL — regenerate_criterion_fields/regenerate_yonetici_ozeti ile AYNI desen).
    İŞ 6N-1 — girdi artık TAM transkript değil, yalnız ADAYIN SÖZLERİ (bkz.
    _build_candidate_evidence_context) — mülakatçı turları context'e hiç girmiyor, token boyutu
    küçülüyor. Prompt, tekrar eden ağır negatif çerçevelemeyi ve "proje" kelimesi zorunluluğunu
    KALDIRDI — genel/soyut ifadeler yine YETERSİZ sayılır ama somut deneyim türü GENİŞLETİLDİ
    (yürütülen görev/operasyonel sorumluluk/kriz-problem çözümü/süreç iyileştirme/denetim/
    mentörlük/uygulanan değişiklik — hiçbiri hard-code bir anahtar kelime değil, örnek listesi).
    Başarısız/istisna/API anahtarı yok → None (çağıran bunu 'recovery başarısız' sayar, mevcut
    fallback korunur)."""
    prompt = f"""Aşağıda bir işe alım mülakatında ADAYIN KENDİ SÖZLERİ (yalnız aday, zaman damgalarıyla) ve varsa o mülakattan zaten DOĞRULANMIŞ kriter kanıtları var. Görevin: "Öne Çıkan Proje ve Deneyimler" bölümünü yazmak.

Bu bölüm, adayın MÜLAKATTA BİZZAT ANLATTIĞI, doğrulanabilir ve anlamlı bir profesyonel deneyimi özetler — genel/soyut yetkinlik ifadesi ("20 yıl deneyimli", "iletişimi güçlü" gibi TEK BAŞINA) DEĞİL. "Proje" kelimesinin geçmesi ZORUNLU DEĞİL — şunlardan HERHANGİ biri geçerli sayılır: yürütülen bir çalışma/görev, üstlenilen operasyonel bir sorumluluk, bir kriz/problem çözümü, bir süreç iyileştirmesi, bir denetim/audit deneyimi, bir eğitim/mentörlük süreci, uygulanan bir değişiklik, ya da başka somut bir profesyonel deneyim.

Yaz: adayın NE YAPTIĞI, hangi BAĞLAMDA (rolü/sorumluluğu neydi), varsa SONUÇ/ETKİ (bu TERCİH edilir, ZORUNLU DEĞİL). Her cümlede en az bir GERÇEK [mm:ss] damgası kullan (aşağıdaki ADAY sözlerinde GERÇEKTEN var olan bir ana karşılık gelmeli). Transkriptte GERÇEKTEN OLMAYAN hiçbir bilgi/sonuç UYDURMA; CV'de geçse bile adayın SÖZLÜ doğrulamadığı bir bilgiyi mülakat kanıtı gibi sunma.

Yalnız gerçekten HİÇBİR anlamlı, adaya ait, doğrulanabilir deneyim yoksa (yalnız soyut/genel ifadeler varsa) "YOK" yaz.

=== DOĞRULANMIŞ KRİTER KANITLARI (referans — BURAYA aynen KOPYALAMA, yalnız ipucu) ===
{(validated_evidence_block or '(yok)')[:4000]}

=== ADAYIN SÖZLERİ (yalnız aday, zaman damgalı) ===
{(candidate_evidence_text or '')[:TRANSCRIPT_PROMPT_MAX_CHARS]}

SADECE bölüm metnini yaz (başlık/etiket/tırnak EKLEME, açıklama yapma)."""
    raw = None
    try:
        if provider == "claude":
            if not ANTHROPIC_API_KEY:
                return None
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=60.0)
            resp = client.messages.create(model=model or "claude-sonnet-4-6", max_tokens=500, temperature=0,
                                          messages=[{"role": "user", "content": prompt}])
            record_anthropic_usage(candidate_id, level, model or "claude-sonnet-4-6", "one_cikan_proje_recovery", resp)
            raw = resp.content[0].text
        elif provider == "openai":
            if not OPENAI_API_KEY:
                return None
            # İŞ EMRİ — ÇOKLU TALENT MİMARİSİ / madde G: bu ÇAĞRININ KENDİSİ content-retry DEĞİL
            # (validator+reviewer'dan sonra, eksik bir bölüm için TEK seferlik üretim — madde E
            # kapsamında incelendi, DEĞİŞTİRİLMEDİ). Ama 429/timeout/geçici 5xx gibi TEKNİK
            # hatalarda artık merkezi katmanın bounded teknik retry'ından (Retry-After farkında)
            # faydalanır — retry=False -> True (yalnız TEKNİK hata sınıfları tekrar dener).
            resp = openai_call("POST", "https://api.openai.com/v1/chat/completions",
                               json_body={"model": model or OPENAI_REPORT_MODEL,
                                          "messages": [{"role": "user", "content": prompt}],
                                          "max_tokens": 500, "temperature": 0},
                               timeout=45.0, step="one_cikan_proje_recovery", severity="background", retry=True,
                               context={"candidate_id": candidate_id, "level": level})
            result = resp.json()
            record_openai_chat_usage(candidate_id, level, model or OPENAI_REPORT_MODEL, "one_cikan_proje_recovery", result)
            raw = result["choices"][0]["message"]["content"]
        else:
            return None
    except Exception as ex:
        print(f"UYARI (regenerate_one_cikan_proje c={candidate_id} L{level}): {type(ex).__name__}: {ex}")
        return None
    return (raw or "").strip()

def _one_cikan_proje_recovery_grounded(text: str, transcript_view: list) -> bool:
    """İŞ 5 — recovery çıktısının HER [mm:ss] damgalı CÜMLESİNİN gerçekten bir 'aday' satırına
    yakın olduğunu doğrular. _timestamp_field_grounded PAYLAŞILAN (İş 2'de sıkılaştırılmış,
    DOKUNULMAYAN) fonksiyonu kullanır — mülakatçı sözü/etiketi otomatik olarak reddedilir. En az
    bir damga bulunmalı VE bulunan HİÇBİR damga geçersiz olmamalı (kısmi güven YOK)."""
    sentences = re.split(r'(?<=[.!?])\s+', text or "")
    found_any_ts = False
    for s in sentences:
        if _TS_RE.search(s):
            found_any_ts = True
            if not _timestamp_field_grounded(s, transcript_view, role="aday"):
                return False
    return found_any_ts

def _accept_one_cikan_proje_recovery(text: str, transcript_view: list) -> bool:
    """İŞ 5 — deterministik kabul kapısı: boş/'YOK'/fallback metni KABUL EDİLMEZ; en az bir GERÇEK,
    aday-satırına GROUNDED [mm:ss] damgası taşımalı. Bu kapıdan geçemeyen recovery kabul edilmez,
    mevcut fallback AYNEN korunur — recovery hiçbir durumda 'zorla doldurma' yapmaz."""
    t = (text or "").strip()
    if not t:
        return False
    if _tr_upper(t) in (_tr_upper("YOK"), _tr_upper(_NO_NARRATIVE_EVIDENCE_FALLBACK), _tr_upper(_NO_TIMESTAMP_EVIDENCE_FALLBACK)):
        return False
    return _one_cikan_proje_recovery_grounded(t, transcript_view)

def run_one_cikan_proje_recovery(candidate_id: int, level: int, position_criteria: Optional[list] = None) -> None:
    """İŞ 5/6N-1 — orkestrasyon: validator+reviewer+takeover TAMAMLANDIKTAN SONRA (run_deferred_finish_job
    içinde append_reviewer_section'dan HEMEN SONRA çağrılır), FİNAL kaydedilmiş raporda 'Öne Çıkan
    Proje ve Deneyimler' HÂLÂ fallback ise TEK bir hedefli recovery denemesi yapar. Bölüm zaten
    doluysa (fallback DEĞİLSE) HİÇBİR ÇAĞRI YAPMAZ. Recovery kabul edilirse YALNIZ bu bölüm
    değiştirilir — başka hiçbir bölüme (Güçlü Yönler, Gelişim Alanları, Genel Kanı, vb.) dokunulmaz.
    Hata/atlama → rapor DEĞİŞMEDEN kalır, sessizce değil (record_system_decision loglar).
    İŞ 6N-1 — ARTIK YALNIZ TEK ÇAĞRI: aynı girdiyle (aynı prompt/model/context/temperature) ikinci
    bir AI çağrısı YAPILMIYOR (eski İş 6C semantic-retry'si kaldırıldı — girdi tamamen aynı olduğu
    için gerçek bilgi kazancı yoktu, bkz. İş 6N teşhisi). İlk çağrı kabul edilmezse mevcut güvenli
    fallback davranışı AYNEN devam eder. Ayrıca modele artık TAM transkript değil, yalnız ADAY
    turlarından oluşan küçültülmüş bir context veriliyor (bkz. _build_candidate_evidence_context) —
    grounding kapısı (_accept_one_cikan_proje_recovery) DEĞİŞMEDEN, hâlâ TAM transcript_view ile
    çalışıyor (yalnız MODELE giden PROMPT küçüldü, doğrulama küçülmedi)."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT report, messages, started_at, pending_finish_provider, pending_finish_model "
            "FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, level)).fetchone()
    finally:
        db.close()
    final_report = (row["report"] if row else "") or ""
    if not final_report.strip() or _ONE_CIKAN_PROJE_HEAD not in final_report:
        return
    m = _ONE_CIKAN_PROJE_RE.search(final_report)
    current_text = (m.group(1).strip() if m else "")
    if _tr_upper(current_text) != _tr_upper(_NO_NARRATIVE_EVIDENCE_FALLBACK):
        return  # bölüm zaten dolu/geçerli — recovery TETİKLENMEZ, hiçbir çağrı yapılmaz

    try:
        transcript_view = build_transcript_view(
            row["messages"] if row and "messages" in row.keys() else "[]", level,
            row["started_at"] if row and "started_at" in row.keys() else None, for_report=True)
        candidate_evidence_text = _build_candidate_evidence_context(transcript_view)
    except Exception as e:
        print(f"UYARI (run_one_cikan_proje_recovery transkript c={candidate_id} L{level}): {type(e).__name__}: {e}")
        return

    _pos_m = re.search(r'\*\*Pozisyon Yetkinlikleri:\*\*\s*\n([\s\S]*?)(?=\n\*\*[^\n]{2,60}:\*\*|\Z)', final_report)
    validated_evidence_block = strip_markdown(_pos_m.group(1)) if _pos_m else ""

    # İŞ EMRİ L1 OPENAI-ONLY MİMARİSİ: L1/L2/L3 birincil rapor artık her zaman OpenAI —
    # "claude" varsayımı hiçbir seviye için doğru değil, kayıt eksikse openai varsayılır.
    provider = (row["pending_finish_provider"] if row and "pending_finish_provider" in row.keys() else None) \
        or "openai"
    model = row["pending_finish_model"] if row and "pending_finish_model" in row.keys() else None

    try:
        recovery_text = regenerate_one_cikan_proje(candidate_id, level, provider, model, candidate_evidence_text, validated_evidence_block)
    except Exception as e:
        print(f"UYARI (run_one_cikan_proje_recovery çağrı c={candidate_id} L{level}): {type(e).__name__}: {e}")
        recovery_text = None

    # İŞ 6N-1 — TEK çağrı: kabul edilmezse (boş/'YOK'/grounding başarısız fark etmeksizin) İKİNCİ
    # bir AI çağrısı YAPILMAZ (eski İş 6C retry'si kaldırıldı — aynı girdiyle ikinci çağrının gerçek
    # bilgi kazancı yoktu, bkz. İş 6N teşhisi). Grounding kapısı (_accept_one_cikan_proje_recovery)
    # DEĞİŞMEDEN, TAM transcript_view ile çalışmaya devam ediyor.
    accepted = bool(recovery_text) and _accept_one_cikan_proje_recovery(recovery_text, transcript_view)

    if accepted:
        new_report = _ONE_CIKAN_PROJE_RE.sub(lambda mm: _ONE_CIKAN_PROJE_HEAD + "\n" + recovery_text, final_report, count=1)
        db2 = get_db()
        try:
            db2.execute("UPDATE interviews SET report=? WHERE candidate_id=? AND level=?", (new_report, candidate_id, level))
            db2.commit()
        finally:
            db2.close()
        record_system_decision(candidate_id, level, "one_cikan_proje_recovery_success",
                               "İş 5/6N-1 — 'Öne Çıkan Proje ve Deneyimler' ilk üretimde fallback'teydi; "
                               "TEK hedefli recovery denemesi (yalnız aday turları context'iyle) "
                               "grounded/somut bir bulgu üretti ve bölüm güncellendi.",
                               {"onceki_metin": current_text, "yeni_metin": recovery_text})
    else:
        record_system_decision(candidate_id, level, "one_cikan_proje_recovery_failed",
                               "İş 5/6N-1 — 'Öne Çıkan Proje ve Deneyimler' ilk üretimde fallback'teydi; "
                               "TEK recovery denemesi somut/grounded bir bulgu üretemedi (ya da hiç "
                               "çağrılamadı) — mevcut fallback metni AYNEN korundu (ikinci deneme YAPILMADI).",
                               {"recovery_ham_cikti": (recovery_text or "")[:500]})

# ============================================================================
# İŞ 6X-1 — FINAL REPORT QUALITY GATE (system-wide, hiçbir aday/pozisyon/kritere özel değil).
# ============================================================================
# Pipeline'ın EN SONUNDA (run_one_cikan_proje_recovery'den HEMEN SONRA, run_deferred_finish_job
# içinde) çalışır. Evaluator/validator/reviewer/takeover/proje-kurtarma TAMAMLANMIŞ, kaydedilmiş
# interviews.report üzerinde BAĞIMSIZ bir SON kalite denetçisidir — YENİ bir değerlendirme YAPMAZ,
# yalnız aşağıdaki WHITELIST'teki narrative bölümlerinde CİDDİ (BLOCKING), doğrulanabilir hatayı
# DAR bir patch ile düzeltir. Kriter tabloları/puanlar/Genel Puan/karar/evaluability/Puanlama
# Kapsamı/Öneri Gerekçesi/Öne Çıkan Proje — HİÇBİRİNE dokunamaz (whitelist dışı, KOD seviyesinde
# garanti — İş 6P'nin alan-izolasyonu ile AYNI felsefe: prompt talimatına GÜVENMEDEN, parse-sonrası
# deterministik doğrulama TEK gerçek garanti).
_QUALITY_GATE_SECTION_HEADS = {
    "YONETICI_OZETI": "Yönetici Özeti",
    "ANALITIK_DUSUNME": "Analitik Düşünme ve Muhakeme",
    "PROBLEM_COZME": "Problem Çözme ve Karar Verme Yaklaşımı",
    "ILETISIM": "Kavrama ve İletişim",
    "CV_MULAKAT_POZISYON_UYUMU": "CV ↔ Mülakat ↔ Pozisyon Uyumu",
    "GUCLU_YONLER": "Güçlü Yönler",
    "GELISIM_ALANLARI": "Gelişim Alanları",
    "GENEL_KANI": "Genel Kanı",
    "CV_OZETI": "CV Özeti",
}
QUALITY_GATE_MAX_TOKENS = 2000

_QG_STATUS_RE = re.compile(r"(?im)^\s*QUALITY_GATE_STATUS\s*:\s*(PASS|PATCH)\s*$")
_QG_ISSUE_RE = re.compile(r"(?im)^\s*ISSUE\s*:\s*([A-Z_]+)\s*=\s*(BLOCKING|NON_BLOCKING)\s*\|\s*(.+)$")
_QG_PATCH_RE = re.compile(
    r"(?ms)^\s*PATCH\s*:\s*([A-Z_]+)\s*=\s*(.+?)"
    r"(?=\n\s*ISSUE\s*:|\n\s*PATCH\s*:|\n\s*QUALITY_GATE_STATUS\s*:|\Z)")
# Genel başlık deseni — mevcut '\*\*[^\n]{2,60}:\*\*' ailesiyle AYNI (İş 1/6V/6T'de defalarca
# kanıtlanmış), YENİ bir regex ailesi İCAT EDİLMEDİ.
_QG_GENERIC_HEADING_RE = re.compile(r'(?m)^\*\*([^\n*]{2,60}):\*\*[ \t]*$')

def parse_quality_gate_output(raw: str) -> dict:
    """İŞ 6X-1 — line-grammar çıktısını ayrıştırır (KRITER_PUAN/SEMANTIC_ISSUE ile AYNI aile — YENİ
    bir JSON/parser mimarisi DEĞİL). Dönüş: {'status': 'PASS'|'PATCH'|None, 'issues':
    {SECTION_KEY: (severity, reason)}, 'patches': {SECTION_KEY: replacement_text}}. 'status' None ise
    ÇIKTI MALFORMED sayılır — çağıran HİÇBİR mutasyon uygulamaz (fail-closed)."""
    raw = raw or ""
    sm = _QG_STATUS_RE.search(raw)
    status = sm.group(1).upper() if sm else None
    issues = {}
    for m in _QG_ISSUE_RE.finditer(raw):
        issues[m.group(1).upper()] = (m.group(2).upper(), m.group(3).strip())
    patches = {}
    for m in _QG_PATCH_RE.finditer(raw):
        text = m.group(2).strip()
        if text:
            patches[m.group(1).upper()] = text
    return {"status": status, "issues": issues, "patches": patches}

def _split_report_into_heading_blocks(report_text: str) -> list:
    """İŞ 6X-1 — raporu [(başlık_veya_None, TAM_blok_metni), ...] listesine böler (başlık satırı
    dahil). Post-gate doğrulamanın TEK genel mekanizması buradan geçer: whitelist DIŞINDAKİ her
    başlığın (kriter tabloları/Puanlama Kapsamı/Öneri Gerekçesi/Öne Çıkan Proje DAHİL) gövdesinin
    byte-birebir korunduğunu doğrulamak için kullanılır."""
    text = report_text or ""
    matches = list(_QG_GENERIC_HEADING_RE.finditer(text))
    if not matches:
        return [(None, text)]
    blocks = []
    if matches[0].start() > 0:
        blocks.append((None, text[:matches[0].start()]))
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        blocks.append((m.group(1).strip(), text[start:end]))
    return blocks

def _quality_gate_apply_patches(report_text: str, patches: dict) -> Optional[str]:
    """İŞ 6X-1 — yalnız whitelist SECTION_KEY'lerin gövdesini (başlık satırı SABİT kalarak)
    değiştirir. Hedef başlık raporda HİÇ YOKSA (bölüm hiç üretilmemişse) None döner — YENİ bir
    bölüm İCAT EDİLMEZ, patch set'i BAŞTAN reddedilir (çağıran tarafta)."""
    new_text = report_text
    for key, replacement in patches.items():
        head_label = _QUALITY_GATE_SECTION_HEADS.get(key)
        if not head_label:
            continue  # whitelist dışı — çağıran tarafta zaten filtrelenir, savunma amaçlı ek kontrol
        pattern = re.compile(r'(\*\*' + re.escape(head_label) + r':\*\*[ \t]*\n)([\s\S]*?)(?=\n\*\*[^\n*]{2,60}:\*\*|\Z)')
        if not pattern.search(new_text):
            return None
        new_text = pattern.sub(lambda mm, _r=replacement: mm.group(1) + _r.strip() + "\n", new_text, count=1)
    return new_text

def _quality_gate_locked_intact(pre_text: str, post_text: str, patched_keys) -> bool:
    """İŞ 6X-1 — POST-GATE DETERMİNİSTİK DOĞRULAMA (madde 1/2/3/5/6/7/8/10, TEK genel mekanizmada):
    (a) başlık listesi (sıra + ad) pre/post BİREBİR aynı olmalı — hiçbir başlık kaybolmadı/eklenmedi/
    yer değiştirmedi (kriter tabloları, Puanlama Kapsamı, Öneri Gerekçesi, Öne Çıkan Proje DAHİL
    HEPSİ birer başlık; whitelist'te OLMAYANLARIN LOCKED kaldığı burada garanti edilir); (b) whitelist
    dışı VEYA whitelist'te olup BU TURDA patch EDİLMEMİŞ her başlığın gövdesi byte-birebir aynı
    kalmalı. False dönerse çağıran TÜM patch set'ini reddeder (partial save YOK)."""
    pre_blocks = _split_report_into_heading_blocks(pre_text)
    post_blocks = _split_report_into_heading_blocks(post_text)
    if [h for h, _ in pre_blocks] != [h for h, _ in post_blocks]:
        return False
    patched_labels = {_QUALITY_GATE_SECTION_HEADS[k] for k in patched_keys if k in _QUALITY_GATE_SECTION_HEADS}
    for (h1, b1), (h2, b2) in zip(pre_blocks, post_blocks):
        if h1 in patched_labels:
            continue
        if b1 != b2:
            return False
    return True

def _quality_gate_new_evidence_safe(pre_body: str, new_body: str, transcript_view: list) -> bool:
    """İŞ 6X-1 — madde 9: patch YENİ bir [mm:ss] damgası veya YENİ bir tırnaklı alıntı içeriyorsa
    (önceki gövdede yoktu), MEVCUT grounding helper'larıyla (check_timestamp_grounded / _verbatim_in
    — YENİ bir doğrulama mantığı İCAT EDİLMEDİ) doğrulanır. Doğrulanamayan HERHANGİ bir yeni kanıt
    varsa False döner — çağıran TÜM patch set'ini reddeder."""
    pre_ts = {f"{mm}:{ss}" for mm, ss in _TS_RE.findall(pre_body or "")}
    new_ts = {f"{mm}:{ss}" for mm, ss in _TS_RE.findall(new_body or "")}
    for ts in (new_ts - pre_ts):
        if not check_timestamp_grounded(ts, transcript_view, role=None, tolerance_s=8):
            return False
    pre_quotes = {qm.group(1) for qm in _QUOTE_RE.finditer(pre_body or "")}
    new_quotes = {qm.group(1) for qm in _QUOTE_RE.finditer(new_body or "")}
    for q in (new_quotes - pre_quotes):
        if not any(_verbatim_in(q, row.get("text") or "") for row in (transcript_view or [])):
            return False
    return True

def _build_quality_gate_reviewer_findings_block(reviewer_findings: dict, position_criteria: list, profile_criteria: list) -> str:
    """İŞ 6X-1 — reviewer'ın (append_reviewer_section'ın döndürdüğü) puan/gerekçe/semantik bulgu
    sözlüklerini Quality Gate promptuna kompakt, kritere-bağlı satırlar halinde verir."""
    rv_scores = (reviewer_findings or {}).get("rv_scores") or {}
    rv_gerekce = (reviewer_findings or {}).get("rv_gerekce") or {}
    rv_semantic = (reviewer_findings or {}).get("rv_semantic") or {}
    lines = []
    for criteria_list, prefix in ((position_criteria or [], "P"), (profile_criteria or [], "K")):
        for i, c in enumerate(criteria_list, start=1):
            cid = f"{prefix}{i}"
            parts = []
            if cid in rv_scores:
                awarded, mx = rv_scores[cid]
                parts.append(f"reviewer_puan={awarded}/{mx}")
            if cid in rv_gerekce:
                parts.append(f"gerekce={rv_gerekce[cid]}")
            if cid in rv_semantic:
                parts.append(f"semantik_not={rv_semantic[cid]}")
            if parts:
                name = c.get("name") if isinstance(c, dict) else c
                lines.append(f"- {cid} ({name}): " + " | ".join(parts))
    return "\n".join(lines) if lines else "(İkinci değerlendirici bu kriterlerin hiçbirinde birincilden FARKLI bir bulgu bildirmedi.)"

_QUALITY_GATE_STATUSES = ("PASS", "PATCHED_AND_VALIDATED", "BLOCKED_INTEGRITY")

def _set_quality_gate_status(candidate_id: int, level: int, status: str) -> None:
    """İŞ EMRİ — madde 10: üç durumlu Quality Gate status'u (PASS | PATCHED_AND_VALIDATED |
    BLOCKED_INTEGRITY) DB'ye yazar. Admin panel zaten 'SELECT i.*' ile TÜM interview alanlarını
    döndürdüğü için bu sütun EK bir endpoint/frontend değişikliği GEREKMEDEN admin'de görünür
    olur (bkz. get_interview)."""
    if status not in _QUALITY_GATE_STATUSES:
        return
    db = get_db()
    try:
        db.execute("UPDATE interviews SET quality_gate_status=? WHERE candidate_id=? AND level=?",
                  (status, candidate_id, level))
        db.commit()
    finally:
        db.close()

def run_final_report_quality_gate(candidate_id: int, level: int, position_criteria: Optional[list] = None,
                                  reviewer_findings: Optional[dict] = None) -> None:
    """İŞ 6X-1 — bkz. modül başlığındaki not. TEK deneme (retry YOK); hata/timeout/malformed/
    geçersiz patch/post-validation fail HER DURUMDA interviews.report'u DEĞİŞTİRMEDEN bırakır
    (fail-closed) — rapor üretimi bu adım yüzünden ASLA çökmez.
    İŞ EMRİ — FINAL EVALUATION ARCHITECTURE / DEĞİŞMEZ LEVEL MİMARİSİ: Quality Gate ARTIK YALNIZ
    L3'te çalışır. Çağıranın (run_deferred_finish_job) level kontrolüne EK olarak fonksiyon
    KENDİSİ de level != 3 ise hiçbir AI çağrısı yapmadan erken döner (savunma amaçlı ikinci kapı —
    doğrudan/yanlış çağrıyla L1/L2'de tetiklenemez)."""
    if level != 3:
        return
    if not OPENAI_API_KEY:
        return
    db = get_db()
    try:
        interview = db.execute(
            "SELECT report, messages, started_at, score, score_position, score_profile, recommendation, "
            "reviewer_score_position, reviewer_score_profile, final_score_position, final_score_profile "
            "FROM interviews WHERE candidate_id=? AND level=?",
            (candidate_id, level)).fetchone()
        candidate = db.execute(
            "SELECT position, cv_text, email, education, university, department, experience_years "
            "FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    finally:
        db.close()
    if not interview:
        return
    pre_report = (interview["report"] or "").strip()
    if not pre_report or _REVIEWER_SLOT_MARK in pre_report:
        return  # reviewer bloğu henüz işlenmemiş (beklenmedik sıralama) — güvenli no-op

    try:
        transcript_view = build_transcript_view(interview["messages"] or "[]", level, interview["started_at"], for_report=True)
        transcript_text_full = transcript_to_text(transcript_view)
    except Exception as e:
        print(f"UYARI (run_final_report_quality_gate transkript c={candidate_id} L{level}): {type(e).__name__}: {e}")
        record_system_decision(candidate_id, level, "quality_gate_atlandi",
                               "Transkript hazırlanamadı — Final Report Quality Gate atlandı, rapor DEĞİŞMEDEN kaldı.", {})
        return

    # İŞ 6X-1 — mevcut gpt-4.1 çağrılarında (reviewer/retry) ZATEN kullanılan AYNI güvenli sınır
    # (TRANSCRIPT_PROMPT_MAX_CHARS) yeniden kullanılır — YENİ bir transkript formatı/sınırı İCAT
    # EDİLMEDİ. Kırpıldıysa modele AÇIKÇA bildirilir (TRANSCRIPT_PARTIAL) — görmediği kısım için
    # kesin "uydurma/yanlış" hükmü vermemesi istenir.
    transcript_partial = len(transcript_text_full) > TRANSCRIPT_PROMPT_MAX_CHARS
    transcript_text = transcript_text_full[:TRANSCRIPT_PROMPT_MAX_CHARS]

    # İŞ EMRİ — L3 FINAL QUALITY GATE / madde 3 — KÖK NEDEN (bu turda bulundu): denetlenen METNİN
    # KENDİSİ (pre_report) sabit 16000 KARAKTERE (transkriptin kendi sınırı olan
    # TRANSCRIPT_PROMPT_MAX_CHARS'tan bile DAHA DÜŞÜK) sessizce kırpılıyordu — transkriptin aksine
    # hiçbir "PARTIAL" bildirimi YOKTU; uzun bir L3 raporunda (özellikle 'derin' depth_tier) rapor
    # bu sınırı aşarsa QG, raporun SONUNU (ör. Genel Kanı/Öneri Gerekçesi'ne en yakın bölümler) hiç
    # GÖRMEDEN "PASS" diyebiliyordu. QG'nin denetlediği ARTEFAKTIN KENDİSİ olduğu için transkriptle
    # AYNI cömertlikte bir sınır (TRANSCRIPT_PROMPT_MAX_CHARS) kullanılır + kırpılırsa AÇIKÇA
    # bildirilir — YENİ bir sınır/format İCAT EDİLMEDİ, var olan aynen yeniden kullanıldı.
    report_partial = len(pre_report) > TRANSCRIPT_PROMPT_MAX_CHARS
    report_text_for_gate = pre_report[:TRANSCRIPT_PROMPT_MAX_CHARS]

    pos_criteria = position_criteria or []
    cv_excerpt = (candidate["cv_text"] or "").strip()[:1800] if (candidate and candidate["cv_text"]) else ""
    # İŞ EMRİ — L3 İKİNCİ DEĞERLENDİRME TUTARLILIĞI + SOURCE VISIBILITY / madde 4 — QG de (reviewer
    # ile AYNI TEK yerden üretilen blok üzerinden) başvuru formu alanlarını görür; bu bilginin CV/
    # transkriptte AYRICA geçmemesi TEK BAŞINA "kaynaksız" saymasına yol açmaz.
    basvuru_formu_block = _basvuru_formu_beyani_block(candidate)
    reviewer_block = _build_quality_gate_reviewer_findings_block(reviewer_findings or {}, pos_criteria, PROFILE_CRITERIA)
    section_list_text = "\n".join(f"- {k}: \"{v}\"" for k, v in _QUALITY_GATE_SECTION_HEADS.items())
    # İŞ EMRİ — L3 FINAL QUALITY GATE / madde 3+7 — KÖK NEDEN (bu turda bulundu): bu blok yalnız
    # birincil ve ikinci değerlendiricinin KENDİ bileşen puanlarını veriyordu; canonical NİHAİ
    # (final_score_position/profile — raporun "Öneri Gerekçesi" bölümünde zaten "Nihai Pozisyon/
    # Profil Puanı" olarak AÇIKÇA yazan, DB'nin tek gerçek kaynağı) hiç verilmiyordu. QG bu yüzden
    # "nihai skorla anlatı açıkça çelişiyor mu" sorusunu YALNIZ birincil+ikinciden kendi kendine
    # ORTALAMA ALARAK çıkarmak zorunda kalıyordu — bu, QG'nin KENDİ BAŞINA bir ikinci puanlama
    # motoruna dönüşmesi riski taşır (madde 7'nin yasakladığı şey). Düzeltme: canonical final
    # değerler DOĞRUDAN, DB'den okunmuş halleriyle veriliyor — QG hesaplamıyor, yalnız KARŞILAŞTIRIYOR.
    state_lines = [
        f"Genel Puan: {interview['score']}",
        f"Pozisyon Puanı (birincil): {interview['score_position']}",
        f"Profil Puanı (birincil): {interview['score_profile']}",
        f"İkinci değerlendirici Pozisyon Puanı: {interview['reviewer_score_position']}",
        f"İkinci değerlendirici Profil Puanı: {interview['reviewer_score_profile']}",
        f"Nihai (canonical) Pozisyon Puanı: {interview['final_score_position']}",
        f"Nihai (canonical) Profil Puanı: {interview['final_score_profile']}",
        f"Öneri: {interview['recommendation']}",
    ]

    prompt = f"""Sen bitmiş bir işe alım raporunun BAĞIMSIZ SON KALİTE DENETÇİSİSİN (Final Report Quality Gate). Yeni bir DEĞERLENDİRME YAPMIYORSUN — bitmiş, zaten puanlanmış/onaylanmış bir raporu, transkriptle karşılaştırarak, CİDDİ ve DOĞRULANABİLİR hatalara karşı incelersin.

GÖREVİN DEĞİL: puanı/kararı/kriterleri yeniden değerlendirmek, stil önerisi yapmak, farklı bir yorum sunmak.
GÖREVİN: aşağıdaki türde CİDDİ (BLOCKING), doğrulanabilir hataları tespit et:
- kanıt/timestamp transkriptle uyuşmuyor (alıntı o anda söylenmemiş)
- kanıt bu kriterle AÇIKÇA ilgisiz
- kanıttan AÇIKÇA daha güçlü bir sonuç çıkarılmış
- olumsuz/sınırlı bir ifade AÇIKÇA olumluya çevrilmiş
- kriter tablosu ile anlatı bölümleri AÇIKÇA çelişiyor
- ikinci değerlendirici bulgusu ile anlatı AÇIKÇA çelişiyor
- anlatı bölümleri birbiriyle AÇIKÇA çelişiyor
- doğrulanmış puan/karar ile anlatı AÇIKÇA çelişiyor
- önemli, transkriptte KARŞILIĞI olmayan bir olgusal iddia
- CV/kaynak metinde OLMAYAN önemli bir CV iddiası

NOT — KAYNAK AYRIMI (KESİN): Aşağıda TRANSKRİPT, CV/KAYNAK METNİ ve BAŞVURU FORMU BEYANI AYRI AYRI verilmiştir. BAŞVURU FORMU BEYANI ne CV ne transkript — adayın/adminin kayıt formuna girdiği, KENDİ BAŞINA geçerli bir kaynaktır. Bir bilginin yalnız CV'de veya yalnız transkriptte GEÇMEMESİ, o bilgi BAŞVURU FORMUNDA VARSA, TEK BAŞINA "kaynaksız/uydurma" SAYILMAZ — yalnız bir kaynağın GERÇEKTEN ÇELİŞTİĞİ (ör. formda "20 yıl" derken transkriptte aday açıkça "3 yıl" dediyse) durumlar BLOCKING'tir.

Stil tercihi, farklı yorumlanabilecek bir değerlendirme, küçük tekrar BLOCKING DEĞİLDİR — bunlar için PATCH ÖNERME.

SADECE aşağıdaki SECTION_KEY'lerden birini, YALNIZ bu türde bir hata GERÇEKTEN varsa düzelt:
{section_list_text}

Bunların DIŞINDAKİ hiçbir bölümü (kriter tabloları, Puanlama Kapsamı, Öneri Gerekçesi, Öne Çıkan Proje ve Deneyimler, Değerlendirilemeyen Alanlar dahil) PATCH ETMEYİ TEKLİF ETME — bunlar senin yetkinin DIŞINDA, kilitli.

DÜZELTME KURALI (KESİN): Yeni bir değerlendirme/iddia/yetkinlik İCAT ETME. Yalnız: (a) yanlış iddiayı KALDIR, (b) aşırı güçlü ifadeyi ZAYIFLAT, (c) doğrulanmış duruma (puan/kriter tablosu/ikinci değerlendirici) UYUMLU hale getir, (d) kaynaksız CV bilgisini KALDIR. GÜÇLÜ YÖNLER'de yalnız SİLME/zayıflatma yap, YENİ güçlü yön EKLEME. CV ÖZETİ'nde yalnız desteklenmeyen kısmı SİL, CV/kaynakta olmayan YENİ bilgi EKLEME. YENİ bir [mm:ss] damgası veya YENİ bir tırnaklı alıntı EKLEMEN gerekiyorsa bu KESİNLİKLE transkriptte GERÇEKTEN var olmalı — uydurma damga/alıntı YASAK, sistem bunu ayrıca doğrular.

TRANSCRIPT_PARTIAL={"true" if transcript_partial else "false"}
{"NOT: Transkript uzunluk sınırı nedeniyle KISALTILDI — görmediğin bölüm hakkında 'uydurma/yanlış' diye KESİN hüküm VERME." if transcript_partial else ""}

ÇIKTI FORMATI (KESİN, başka HİÇBİR ŞEY yazma):
Ciddi/doğrulanabilir bir sorun YOKSA yalnız:
QUALITY_GATE_STATUS: PASS

Sorun VARSA:
QUALITY_GATE_STATUS: PATCH
ISSUE: <SECTION_KEY> = BLOCKING | <çok kısa (1 cümle) neden>
PATCH: <AYNI SECTION_KEY> = <o bölümün TAMAMININ yeni, düzeltilmiş hali>
(Birden fazla bölümde sorun varsa her biri için ayrı ISSUE+PATCH çifti yaz. NON_BLOCKING bir gözlemin varsa ISSUE olarak yaz ama PATCH ÜRETME — sistem NON_BLOCKING için patch uygulamaz.)

=== DOĞRULANMIŞ FINAL DURUM (DEĞİŞTİRİLEMEZ) ===
{chr(10).join(state_lines)}

=== İKİNCİ DEĞERLENDİRİCİ BULGULARI (yalnız referans, sen değiştiremezsin) ===
{reviewer_block}

=== CV/KAYNAK METNİ (varsa) ===
{cv_excerpt or "(CV metni yok veya çok kısa — bu durumda CV Özeti hakkında 'kaynaksız/uydurma' diye KESİN hüküm VERME, yalnız AÇIKÇA ve KESİNLİKLE çelişen bir şey varsa işaretle.)"}

=== BAŞVURU FORMU BEYANI ===
{basvuru_formu_block}

=== TRANSKRİPT{" (KISALTILMIŞ)" if transcript_partial else ""} ===
{transcript_text}

=== BİTMİŞ NİHAİ RAPOR{" (KISALTILMIŞ)" if report_partial else ""} (incelediğin metin) ===
{"NOT: Rapor uzunluk sınırı nedeniyle KISALTILDI — görmediğin sondaki bölümler hakkında 'uydurma/yanlış/çelişkili' diye KESİN hüküm VERME." if report_partial else ""}
{report_text_for_gate}"""

    try:
        resp = openai_call(
            "POST", "https://api.openai.com/v1/chat/completions",
            json_body={"model": OPENAI_REVIEWER_MODEL, "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": QUALITY_GATE_MAX_TOKENS, "temperature": 0},
            timeout=60.0, step="quality_gate", severity="background", retry=False,
            context={"candidate_id": candidate_id, "level": level},
        )
        result = resp.json()
        record_openai_chat_usage(candidate_id, level, OPENAI_REVIEWER_MODEL, "quality_gate", result)
        raw_out = (result["choices"][0]["message"]["content"] or "").strip()
        print(f"[QUALITY_GATE_RAW] c={candidate_id} L{level} len={len(raw_out)}\n{raw_out[:1500]}")
    except Exception as e:
        print(f"UYARI (run_final_report_quality_gate çağrı c={candidate_id} L{level}): {type(e).__name__}: {e}")
        record_system_decision(candidate_id, level, "quality_gate_hata",
                               "Final Report Quality Gate çağrısı başarısız/zaman aşımı — rapor DEĞİŞMEDEN korundu.",
                               {"hata": f"{type(e).__name__}: {e}"})
        return

    parsed = parse_quality_gate_output(raw_out)
    status = parsed["status"]
    if status == "PASS":
        _set_quality_gate_status(candidate_id, level, "PASS")
        record_system_decision(candidate_id, level, "quality_gate_pass",
                               "Final Report Quality Gate: ciddi/doğrulanabilir bir sorun bulunmadı, rapora dokunulmadı.", {})
        return
    if status != "PATCH":
        # İŞ EMRİ — madde 10: "BLOCKING ama raporu yine ver" açığı — malformed çıktı, gate'in
        # GERÇEKTEN temiz mi BLOCKING mi olduğunu belirleyemediği bir durumdur; GÜVENLİ TARAF
        # BLOCKED_INTEGRITY'dir (rapor sessizce 'başarılı final' sayılmaz, ama silinmez/çökmez).
        _set_quality_gate_status(candidate_id, level, "BLOCKED_INTEGRITY")
        record_system_decision(candidate_id, level, "quality_gate_malformed",
                               "Final Report Quality Gate çıktısı ayrıştırılamadı (beklenen QUALITY_GATE_STATUS satırı yok) — rapor DEĞİŞMEDEN korundu, BLOCKED_INTEGRITY olarak işaretlendi.",
                               {"ham_cikti": raw_out[:1000]})
        return

    blocking_keys = {k for k, (sev, _r) in parsed["issues"].items() if sev == "BLOCKING"}
    applicable_patches = {}
    unresolved = []
    for key, replacement in parsed["patches"].items():
        if key not in blocking_keys:
            continue  # NON_BLOCKING veya ISSUE'suz PATCH -> asla uygulanmaz
        if key not in _QUALITY_GATE_SECTION_HEADS:
            unresolved.append((key, "bilinmeyen SECTION_KEY"))
            continue
        applicable_patches[key] = replacement
    for key in blocking_keys:
        if key not in applicable_patches and key not in [k for k, _ in unresolved]:
            unresolved.append((key, "BLOCKING işaretlendi ama uygulanabilir PATCH verilmedi"))

    if not applicable_patches:
        if blocking_keys:
            print(f"QUALITY_GATE_BLOCKING_UNRESOLVED c={candidate_id} L{level} keys={list(blocking_keys)}")
            _set_quality_gate_status(candidate_id, level, "BLOCKED_INTEGRITY")
            record_system_decision(candidate_id, level, "quality_gate_blocking_cozulemedi",
                                   "Final Report Quality Gate BLOCKING sorun bildirdi ama uygulanabilir/whitelist içi bir PATCH üretmedi — rapor DEĞİŞMEDEN korundu, BLOCKED_INTEGRITY olarak işaretlendi, insan incelemesi önerilir.",
                                   {"issues": {k: v for k, v in parsed["issues"].items()}, "cozulemeyenler": unresolved})
        else:
            _set_quality_gate_status(candidate_id, level, "PASS")
            record_system_decision(candidate_id, level, "quality_gate_no_op",
                                   "Final Report Quality Gate PATCH döndü ama uygulanabilir bir BLOCKING+whitelist patch yoktu — rapor DEĞİŞMEDEN korundu.", {})
        return

    patched_report = _quality_gate_apply_patches(pre_report, applicable_patches)
    if patched_report is None:
        print(f"QUALITY_GATE_BLOCKING_UNRESOLVED c={candidate_id} L{level} keys={list(applicable_patches)} sebep=heading_bulunamadi")
        _set_quality_gate_status(candidate_id, level, "BLOCKED_INTEGRITY")
        record_system_decision(candidate_id, level, "quality_gate_patch_reddedildi",
                               "Final Report Quality Gate patch'i uygulanamadı (hedef bölüm başlığı raporda yok) — TÜM patch set'i reddedildi, rapor DEĞİŞMEDEN korundu, BLOCKED_INTEGRITY olarak işaretlendi.",
                               {"denenen_anahtarlar": list(applicable_patches.keys())})
        return

    # POST-GATE DETERMİNİSTİK DOĞRULAMA — herhangi biri FAIL ederse TÜM patch set'i reddedilir,
    # partial save YOK (madde: "Herhangi biri FAIL: TÜM Quality Gate patch setini reddet").
    if not _quality_gate_locked_intact(pre_report, patched_report, set(applicable_patches.keys())):
        print(f"QUALITY_GATE_BLOCKING_UNRESOLVED c={candidate_id} L{level} sebep=locked_section_degisti")
        _set_quality_gate_status(candidate_id, level, "BLOCKED_INTEGRITY")
        record_system_decision(candidate_id, level, "quality_gate_patch_reddedildi",
                               "Final Report Quality Gate patch'i LOCKED bir bölümü değiştirdi (veya başlık bütünlüğünü bozdu) — TÜM patch set'i reddedildi, rapor DEĞİŞMEDEN korundu, BLOCKED_INTEGRITY olarak işaretlendi.",
                               {"denenen_anahtarlar": list(applicable_patches.keys())})
        return

    for key, replacement in applicable_patches.items():
        head_label = _QUALITY_GATE_SECTION_HEADS[key]
        pre_pattern = re.compile(r'\*\*' + re.escape(head_label) + r':\*\*[ \t]*\n([\s\S]*?)(?=\n\*\*[^\n*]{2,60}:\*\*|\Z)')
        pre_m = pre_pattern.search(pre_report)
        pre_body = pre_m.group(1) if pre_m else ""
        if not _quality_gate_new_evidence_safe(pre_body, replacement, transcript_view):
            print(f"QUALITY_GATE_BLOCKING_UNRESOLVED c={candidate_id} L{level} sebep=dogrulanamayan_yeni_kanit key={key}")
            _set_quality_gate_status(candidate_id, level, "BLOCKED_INTEGRITY")
            record_system_decision(candidate_id, level, "quality_gate_patch_reddedildi",
                                   "Final Report Quality Gate patch'i transkriptte doğrulanamayan YENİ bir timestamp/alıntı içeriyordu — TÜM patch set'i reddedildi, rapor DEĞİŞMEDEN korundu, BLOCKED_INTEGRITY olarak işaretlendi.",
                                   {"denenen_anahtarlar": list(applicable_patches.keys()), "sorunlu_key": key})
            return

    db3 = get_db()
    try:
        _row_check = db3.execute(
            "SELECT score, score_position, score_profile, recommendation FROM interviews WHERE candidate_id=? AND level=?",
            (candidate_id, level)).fetchone()
        if _row_check and (
            _row_check["score"] != interview["score"] or _row_check["score_position"] != interview["score_position"]
            or _row_check["score_profile"] != interview["score_profile"] or _row_check["recommendation"] != interview["recommendation"]
        ):
            # Savunma amaçlı: Quality Gate KENDİSİ bu alanlara hiç yazmaz; bu satırlar arada BAŞKA bir
            # işlemle değişmişse (ör. eşzamanlı regenerate) patch güvenli tarafta bırakılır.
            print(f"QUALITY_GATE_BLOCKING_UNRESOLVED c={candidate_id} L{level} sebep=eszamanli_skor_degisikligi")
            db3.close()
            _set_quality_gate_status(candidate_id, level, "BLOCKED_INTEGRITY")
            record_system_decision(candidate_id, level, "quality_gate_patch_reddedildi",
                                   "Final Report Quality Gate patch'i uygulanmadan önce skor/karar alanları başka bir işlemle değişmiş görünüyor — güvenlik için patch reddedildi, BLOCKED_INTEGRITY olarak işaretlendi.", {})
            return
        db3.execute("UPDATE interviews SET report=?, quality_gate_status=? WHERE candidate_id=? AND level=?",
                   (patched_report, "PATCHED_AND_VALIDATED", candidate_id, level))
        db3.commit()
    finally:
        db3.close()

    record_system_decision(candidate_id, level, "quality_gate_patch_uygulandi",
                           "Final Report Quality Gate CİDDİ/doğrulanabilir bir sorun tespit etti ve whitelist içi narrative bölüm(ler)i düzeltti (skor/kriter/karar DEĞİŞMEDİ). Durum: PATCHED_AND_VALIDATED.",
                           {"uygulanan_anahtarlar": list(applicable_patches.keys()),
                            "issues": {k: v for k, v in parsed["issues"].items() if k in applicable_patches}})

# İŞ EMRİ — FINAL EVALUATION ARCHITECTURE / madde 11 — FINAL DETERMINISTIC INTEGRITY GATE.
# AI Quality Gate'ten (varsa, YALNIZ L3) SONRA VE DB finalization'dan (bu fonksiyonun kendisi
# finalization'ın SON adımıdır) ÖNCE çalışan, hiçbir AI çağrısı YAPMAYAN son kapı. L1/L2 için de
# çalışır (daha hafif — reviewer/Quality Gate'e özel kontroller o levellerda doğal olarak no-op,
# çünkü reviewer alanları zaten None). Yalnız MEVCUT deterministik helper'ları (compute_genel_puan,
# decide_recommendation, _final_component_score, check_timestamp_grounded) yeniden kullanır — YENİ
# bir doğrulama mantığı İCAT EDİLMEDİ. FAIL durumunda rapor SİLİNMEZ/geri ALINMAZ — yalnız
# final_integrity_status='FAIL' olarak İŞARETLENİR (yönetici görünür, bkz. çağıran).
def run_final_deterministic_integrity_check(candidate_id: int, level: int) -> str:
    """Dönüş: 'PASS' | 'FAIL'. Kontrol listesi (madde 11): final_score_position/profile canonical
    hesapla aynı mı; General canonical hesapla aynı mı; Recommendation General ile uyumlu mu;
    reviewer alanları 'yarı dolu' bir tutarsız durumda mı; zorunlu başlıklar (Öneri Gerekçesi/
    Puanlama Kapsamı) var mı; kriter tablolarındaki [mm:ss] damgaları transkriptte grounded mı."""
    db = get_db()
    try:
        interview = db.execute(
            "SELECT report, score, score_position, score_profile, recommendation, "
            "reviewer_score_position, reviewer_score_profile, final_score_position, final_score_profile, "
            "messages, started_at FROM interviews WHERE candidate_id=? AND level=?",
            (candidate_id, level)).fetchone()
    finally:
        db.close()
    if not interview:
        return "FAIL"
    problems = []
    report_text = interview["report"] or ""
    if not report_text.strip():
        problems.append("rapor_bos")
    if _ONERI_GEREKCESI_HEAD not in report_text:
        problems.append("oneri_gerekcesi_baslik_eksik")
    if _PUANLAMA_KAPSAMI_HEAD not in report_text:
        problems.append("puanlama_kapsami_baslik_eksik")

    expected_general = compute_genel_puan(interview["score_position"], interview["score_profile"],
                                          interview["reviewer_score_position"], interview["reviewer_score_profile"])
    if expected_general is not None and interview["score"] is not None and interview["score"] != expected_general:
        problems.append(f"general_score_mismatch(db={interview['score']},canonical={expected_general})")

    if interview["score"] is not None:
        expected_rec = decide_recommendation(interview["score"])
        if expected_rec and interview["recommendation"] and interview["recommendation"] not in (expected_rec, "Değerlendirilemedi"):
            problems.append(f"recommendation_mismatch(db={interview['recommendation']},canonical={expected_rec})")

    expected_final_pos = _final_component_score(interview["score_position"], interview["reviewer_score_position"])
    expected_final_prof = _final_component_score(interview["score_profile"], interview["reviewer_score_profile"])
    if interview["final_score_position"] != expected_final_pos:
        problems.append(f"final_score_position_mismatch(db={interview['final_score_position']},canonical={expected_final_pos})")
    if interview["final_score_profile"] != expected_final_prof:
        problems.append(f"final_score_profile_mismatch(db={interview['final_score_profile']},canonical={expected_final_prof})")

    if (interview["reviewer_score_position"] is None) != (interview["reviewer_score_profile"] is None):
        problems.append("reviewer_yari_durum")

    try:
        transcript_view = build_transcript_view(interview["messages"] or "[]", level, interview["started_at"], for_report=True)
        _pos_m = re.search(r'\*\*Pozisyon Yetkinlikleri:\*\*\s*\n([\s\S]*?)(?=\n\*\*[^\n]{2,60}:\*\*|\Z)', report_text)
        _prof_m = re.search(r'\*\*Kişisel ve Bilişsel Profil:\*\*\s*\n([\s\S]*?)(?=\n\*\*[^\n]{2,60}:\*\*|\Z)', report_text)
        for _tbl in (_pos_m.group(1) if _pos_m else "", _prof_m.group(1) if _prof_m else ""):
            for ln in _tbl.splitlines():
                if ln.count("|") < 2:
                    continue
                cells = [c.strip() for c in ln.strip().strip("|").split("|")]
                if len(cells) < 3:
                    continue
                for mm, ss in _TS_RE.findall(cells[2]):
                    ts = f"{mm}:{ss}"
                    if not check_timestamp_grounded(ts, transcript_view, role=None, tolerance_s=8):
                        problems.append(f"grounding_fail({ts})")
        # İŞ EMRİ — madde 8 — ÖNE ÇIKAN PROJE AÇIĞINI KAPAT: bu bölüm PRIMARY tarafından
        # doğrudan üretilmiş (fallback'e hiç düşmemiş) olsa bile artık AYNI temel grounding
        # kontrolünden (mevcut check_timestamp_grounded — YENİ mantık İCAT EDİLMEDİ) geçer. Quality
        # Gate bu bölümü LOCKED tutmaya devam eder (whitelist'te YOK) — düzeltme burada YAPILMAZ,
        # yalnız SORUN varsa final_integrity_status='FAIL' ile İŞARETLENİR.
        _proj_m = _ONE_CIKAN_PROJE_RE.search(report_text)
        _proj_text = _proj_m.group(1) if _proj_m else ""
        if _proj_text and _tr_upper(_proj_text.strip()) != _tr_upper(_NO_NARRATIVE_EVIDENCE_FALLBACK):
            for mm, ss in _TS_RE.findall(_proj_text):
                ts = f"{mm}:{ss}"
                if not check_timestamp_grounded(ts, transcript_view, role="aday", tolerance_s=8):
                    problems.append(f"proje_grounding_fail({ts})")
    except Exception as e:
        print(f"UYARI (final integrity grounding taraması c={candidate_id} L{level}): {type(e).__name__}: {e}")

    result = "FAIL" if problems else "PASS"
    db2 = get_db()
    try:
        db2.execute("UPDATE interviews SET final_integrity_status=? WHERE candidate_id=? AND level=?",
                   (result, candidate_id, level))
        db2.commit()
    finally:
        db2.close()
    if problems:
        print(f"[FINAL_INTEGRITY_FAIL] c={candidate_id} L{level} problems={problems}")
        record_system_decision(candidate_id, level, "final_integrity_fail",
                               "Final Deterministic Integrity Gate BAŞARISIZ — rapor SİLİNMEDİ/geri ALINMADI, yalnız işaretlendi; insan incelemesi önerilir.",
                               {"problemler": problems})
    else:
        record_system_decision(candidate_id, level, "final_integrity_pass",
                               "Final Deterministic Integrity Gate PASS.", {})
    return result

# GÖREV 1.7 — Profil Veto Kontrolü: eski mimaride modelin kendi yazdığı "[VETO: ...]" etiketine
# dayanıyordu (detect_profile_veto, artık orphan — 2026-09 yeniden tasarımında ÇAĞRILMAZ hale
# geldi). Yeni mimaride profil kriterleri artık G/K/E/S yapısıyla GERÇEK, normalize edilmiş bir
# puana sahip — bu yüzden veto artık TABLODAN doğrudan, DETERMİNİSTİK hesaplanır (modelin kendi
# etiketlemesine güvenmek gerekmiyor): bir profil kriteri tavanının %20'sinin altına düşerse uyarı.
_VETO_THRESHOLD_RATIO = 0.20

def render_profile_veto_control(profile_table_text: str) -> str:
    """GÖREV 1.7 — profil tablosundaki (zaten normalize edilmiş) puanları tarar; herhangi bir
    kriter tavanının %20'sinin ALTINDAYSA (ve gerçekten puanlanmışsa — 'Değerlendirilemedi' olan
    satırlar hariç, onlar veri eksikliğidir, düşük performans değil) veto uyarısı üretir."""
    if not profile_table_text:
        return "Veto yok."
    worst = None
    for ln in profile_table_text.splitlines():
        if ln.count("|") < 2:
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        m = re.search(r"(?<![\d/])(\d+)\s*/\s*(\d+)(?![\d/])", cells[1])
        if not m:
            continue
        awarded, cap = _safe_int(m.group(1)), _safe_int(m.group(2))
        if cap <= 0:
            continue
        ratio = awarded / cap
        if ratio < _VETO_THRESHOLD_RATIO and (worst is None or ratio < worst[2]):
            worst = (cells[0], awarded, ratio, cap)
    if worst:
        name, awarded, _ratio, cap = worst
        return f"[VETO UYARISI: \"{name}\" kriteri tavanının %20'sinin altında kaldı ({awarded}/{cap}) — kurumsal ortamda çalışmaya engel olabilecek düzeyde ciddi bir zayıflık işareti.]"
    return "Veto yok."

# GÖREV 1.5 — Öneri Gerekçesi: TAMAMEN deterministik, skor/öneriden TÜRETİLİR — bu, "öneriyle
# TUTARLI olmak zorunda" şartını (madde 1.5) YAPI GEREĞİ sağlar (modelin bağımsız yazdığı bir
# gerekçe metniyle karar arasında çelişki riski hiç oluşmaz, çünkü ikisi AYNI sayılardan üretilir).
# İŞ EMRİ — FINAL EVALUATION ARCHITECTURE / madde 4 — PUAN ETİKETLERİNİ SEMANTİK OLARAK AYIR.
# FATAL AUDIT kesin bulgusu: aynı raporda "Pozisyon Puanı" adı altında üç FARKLI semantik değer
# (birincil/ikincil/blended) açıklamasız gösterilebiliyordu. Artık HER katman AÇIKÇA etiketli:
# "Birinci Değerlendirici" / "İkinci Değerlendirici" (yalnız GERÇEKTEN varsa — L1/L2'de reviewer
# hiç çalışmadığı için bu blok TAMAMEN ATLANIR, boş kart gösterilmez) / "Nihai". Nihai değerler
# ARTIK render-anında YENİDEN HESAPLANMIYOR — çağıran (finalize_interview / append_reviewer_section)
# DB'ye PERSIST edilmiş final_score_position/final_score_profile'ı bu fonksiyona doğrudan verir
# (bkz. _persist_final_scores / recompute_overall_decision) — TEK canonical kaynak.
def render_oneri_gerekcesi(recommendation: str, score, score_position, score_profile,
                           reviewer_score_position=None, reviewer_score_profile=None,
                           final_score_position=None, final_score_profile=None) -> str:
    parts_ = []
    if recommendation == "Reddet":
        parts_.append(f"Adayın Genel Puanı ({score}/100) pozisyon için gerekli eşiğin (40) altında kalmıştır.")
    elif recommendation == "İşe Al":
        parts_.append(f"Adayın Genel Puanı ({score}/100) pozisyon için gerekli eşiğin (80) üzerindedir.")
    elif recommendation == "Değerlendir":
        parts_.append(f"Adayın Genel Puanı ({score}/100), doğrudan işe alım veya ret için yeterli olmayan, değerlendirmeye açık bir aralıktadır (40-79).")
    else:
        return ""
    _has_second = reviewer_score_position is not None or reviewer_score_profile is not None
    if _has_second:
        # L3 — second evaluator (Claude) GERÇEKTEN çalıştı: üç katman da AÇIKÇA etiketli gösterilir.
        if score_position is not None or score_profile is not None:
            parts_.append(f"Birinci Değerlendirici — Pozisyon: {score_position if score_position is not None else '-'}/100 · "
                          f"Profil: {score_profile if score_profile is not None else '-'}/100.")
        parts_.append(f"İkinci Değerlendirici — Pozisyon: {reviewer_score_position if reviewer_score_position is not None else '-'}/100 · "
                      f"Profil: {reviewer_score_profile if reviewer_score_profile is not None else '-'}/100.")
        if final_score_position is not None:
            parts_.append(f"Nihai Pozisyon Puanı: {final_score_position}/100.")
        if final_score_profile is not None:
            parts_.append(f"Nihai Profil Puanı: {final_score_profile}/100.")
    else:
        # L1/L2 — second evaluator YOK: boş/placeholder "İkinci Değerlendirici" kartı GÖSTERİLMEZ.
        # Tek katman var (= primary = nihai) — "Nihai" etiketiyle, ileride second evaluator
        # eklenirse bile GEÇMİŞ metinlerle karışmayacak şekilde baştan net.
        _fp = final_score_position if final_score_position is not None else score_position
        _fpr = final_score_profile if final_score_profile is not None else score_profile
        if _fp is not None:
            parts_.append(f"Nihai Pozisyon Puanı: {_fp}/100.")
        if _fpr is not None:
            parts_.append(f"Nihai Profil Puanı: {_fpr}/100.")
    return " ".join(parts_)

# GÖREV 1.2/1.3 — bu turda geri eklenen anlatı bölümleri (Analitik Düşünme, Problem Çözme,
# Kavrama ve İletişim, Öne Çıkan Proje, CV↔Mülakat↔Pozisyon Uyumu, Dil Gözlemi, Genel Kanı) SERBEST
# PARAGRAF — G/K/E/S yapısında DEĞİL. Kriter hücreleri gibi tüm hücreyi diskalifiye etmek yerine
# CÜMLE bazlı temizlik yeterli ve daha az yıkıcı (bkz. _strip_unsupported_strength_sentences'ın
# GÜÇLÜ YÖNLER için kurduğu emsal — burada GENELLEŞTİRİLDİ).
def strip_banned_phrase_sentences(text: str) -> tuple:
    """Serbest metin bölümlerinde yasak kalıp/klişe içeren CÜMLEYİ çıkarır. Dönüş: (yeni_metin, çıkarılan[])."""
    if not text:
        return text, []
    sents = re.split(r'(?<=[.!?])\s+', text)
    kept, dropped = [], []
    for s in sents:
        if banned_phrase_hits(s):
            dropped.append(s.strip())
        else:
            kept.append(s)
    return " ".join(kept).strip(), dropped

# GÖREV 5 — evaluated_vs_narrative_conflict: bir kriter "Değerlendirilemedi (sistem)" ise, o
# kriterin KONUSU hakkında Güçlü Yönler/Yönetici Özeti'nde OLUMLU HÜKÜM cümlesi basılmaz (kanıtlı
# örnek: "İletişim" kriteri Değerlendirilemedi iken Güçlü Yönler'de "iletişim becerileri...olumlu
# bir izlenim bırakmıştır" yazılmıştı — sistem BİR yandan ölçemediğini söylüyor, ÖTE yandan o
# konuda olumlu hüküm veriyordu).
_POSITIVE_JUDGMENT_CUE_RE = re.compile(
    r"olumlu\b|başar[ıi]l[ıi]|g[öo]stermektedir|izlenim b[ıi]rak|yetene[ğg]ini g[öo]ster|"
    r"g[üu][çc]l[üu] bir [şs]ekilde|etkili\b|iyi bir [şs]ekilde",
    re.IGNORECASE)

def strip_narrative_conflicts_with_disqualified(text: str, disqualified_names: list) -> tuple:
    """GÖREV 5.1/5.2 (evaluated_vs_narrative_conflict) — diskalifiye edilmiş kriterlerin
    KONUSUYLA örtüşen (EN AZ 1 ortak anahtar kelime) VE olumlu-hüküm ipucu taşıyan cümleleri
    çıkarır. NOT (sentetik testte yakalandı): ilk tasarımda %50 ORAN eşiği kullanılıyordu — çok
    kelimeli kriter adlarında (ör. "İletişim ve ifade netliği" → 3 anahtar kelime: iletişim/ifade/
    netliği) gerçek bir çelişki cümlesi genelde SADECE kriterin ANA kelimesini (ör. "iletişim")
    tekrarlar, diğer ikisini değil — 1/3 oranı %50'nin altında kaldığı için GERÇEK bir çelişki
    KAÇIYORDU. _POSITIVE_JUDGMENT_CUE_RE zaten güçlü bir ikinci filtre olduğu için EN AZ 1 ortak
    kelime + olumlu-hüküm ipucu BİRLİKTE yeterli kabul edildi (yanlış pozitif riski düşük).
    Dönüş: (yeni_metin, çıkarılan[])."""
    if not text or not disqualified_names:
        return text, []
    name_kws = [(name, [w for w in _norm_name(name).split() if len(w) >= 4]) for name in disqualified_names]
    name_kws = [(n, k) for n, k in name_kws if k]
    if not name_kws:
        return text, []
    sents = re.split(r'(?<=[.!?])\s+', text)
    kept, dropped = [], []
    for s in sents:
        norm = _norm_name(s)
        conflict = False
        if _POSITIVE_JUDGMENT_CUE_RE.search(s):
            for _name, kws in name_kws:
                if sum(1 for w in kws if w in norm) >= 1:
                    conflict = True
                    break
        if conflict:
            dropped.append(s.strip())
        else:
            kept.append(s)
    return " ".join(kept).strip(), dropped

# İş emri madde 3 — rapor gövdesinin bölüm başlıkları TEK KAYNAK burada listelenir; PDF renderer
# assemble_final_report/finalize_interview ile AYNI isimleri kullanır (strip_markdown '**' işaretini
# kaldırdığı için burada çıplak "Ad:" biçiminde eşleşir).
_KNOWN_REPORT_HEADINGS = ("Yönetici Özeti", "Puanlama Kapsamı", "Analitik Düşünme ve Muhakeme",
                          "Problem Çözme ve Karar Verme Yaklaşımı", "Kavrama ve İletişim",
                          "Tutarlılık / Çelişki Analizi",
                          "Öne Çıkan Proje ve Deneyimler", "CV ↔ Mülakat ↔ Pozisyon Uyumu",
                          "Değerlendirilemeyen Alanlar",
                          "Dil Gözlemi", "Pozisyon Yetkinlikleri", "Kişisel ve Bilişsel Profil",
                          "İkinci Değerlendirici Görüşü", "Güçlü Yönler", "Gelişim Alanları",
                          "Görüntü ve Ses Gözlemi", "CV Özeti", "Beyan Tutarlılığı", "Genel Kanı",
                          "Öneri Gerekçesi", "Takip Mülakatı İçin Önerilen Sorular")
_REPORT_HEAD_LOOKUP = {h + ":": h for h in _KNOWN_REPORT_HEADINGS}

def _split_report_sections(lines: list) -> dict:
    """Rapor gövdesini (düz metin satırları, '**' zaten temizlenmiş) bilinen başlıklara göre
    {başlık: [satırlar]} sözlüğüne böler; sıra korunur (Python 3.7+ dict). Bilinen ilk başlıktan
    ÖNCEKİ içerik None anahtarında toplanır (kaybolmaz, PDF renderer ayrıca basar)."""
    sections: dict = {}
    cur_key, cur_lines = None, []
    for ln in lines:
        key = _REPORT_HEAD_LOOKUP.get(ln.strip())
        if key:
            if cur_lines:
                sections.setdefault(cur_key, []).extend(cur_lines)
            cur_key, cur_lines = key, []
        else:
            cur_lines.append(ln)
    if cur_lines:
        sections.setdefault(cur_key, []).extend(cur_lines)
    return sections

# İŞ EMRİ — PRIMARY PUAN FORMATI + 3 SÜTUN REGRESYON DÜZELTMESİ (FAZ 1): recompute_and_fix_score/
# recompute_profile_section'ın taban-puan satırlarını düzeltirken kullandığı ESKİ desen
# (`lines[li].replace(f"| {puan_cell} |", f"| {puan}/{cap} | Taban puan (...): {gerekce} |", 1)`)
# GPT'nin ZATEN 3 sütun yazdığı (Kriter | Puan | Kanıt ve Analiz) satırlarda YENİ bir 4. sütun
# üretiyordu (commit 41d26b8'de fark edilmeyen regresyon — bkz. teşhis) — kendi içinde bir "|"
# taşıyan değiştirme metni, mevcut 3. hücreyi (gerçek kanıt) itip 4. hücreye dönüştürüyordu; PDF
# renderer da (_emit_report_block, row[:3]) bu 4. hücreyi sessizce atıyordu. Bu fonksiyon satırı
# HÜCRE BAZINDA (isim/puan/kanıt) yeniden kurar: PUAN hücresi DEĞİŞİR, açıklama (note) VARSA 3.
# hücrenin (Kanıt ve Analiz) BAŞINA eklenir, var olan kanıt METNİ KORUNUR — satır HER ZAMAN 3
# mantıksal sütun olarak kalır (2 sütunlu eski girdilerde 3. hücre bu note'tan yeni oluşturulur).
# Floor/insufficient-answer KARAR mantığı (bu fonksiyonu ÇAĞIRAN kod) DEĞİŞMEDİ — bu yalnız
# SEÇİLEN puan/not'un satıra NASIL YAZILDIĞINI (biçim) düzeltir.
def _rewrite_criterion_cell(line: str, old_cell: str, new_score: str, note: str = "") -> str:
    idx = line.find(f"| {old_cell} |")
    if idx == -1:
        # beklenmeyen biçim — davranış ESKİSİNDEN kötü olmasın diye eski basit değiştirme
        return line.replace(f"| {old_cell} |", f"| {new_score} |", 1)
    prefix = line[:idx]
    rest = line[idx + len(f"| {old_cell} |"):]
    if not note:
        return f"{prefix}| {new_score} |{rest}"
    # rest, üçüncü VE varsa sonraki TÜM hücreleri (| ile ayrılmış, legacy/bozuk girdi) içerebilir —
    # hepsi TEK Kanıt ve Analiz hücresinde birleştirilir (_emit_report_block render düzeltmesiyle
    # AYNI ilke) — hiçbir trailing hücre kaybolmaz/ayrı sütun olarak KALMAZ.
    rest_stripped = rest.strip()
    if rest_stripped.endswith("|"):
        rest_stripped = rest_stripped[:-1].strip()
    trailing_cells = [c.strip() for c in rest_stripped.split("|") if c.strip()]
    merged = " ".join([note] + trailing_cells) if trailing_cells else note
    return f"{prefix}| {new_score} | {merged} |"

# İş emri — PRIMARY DEĞERLENDİRME VE KANIT SEÇİMİ GÜVENİLİRLİĞİ / FAZ 1: kanıt havuzu ayıklama +
# taşıma. Yalnız açık marker'ları kullanır (madde 24), fuzzy/semantic eşleştirme YAPMAZ (madde 5,
# 9), PUAN ÜRETMEZ/DEĞİŞTİRMEZ (madde 11) — yalnız modelin ZATEN ürettiği evidence set'ini
# deterministik olarak doğru kriter satırının 3. hücresine taşır.
_EVIDENCE_POOL_CRIT_LINE_RE = re.compile(r'^\s*KRİTER\s*:\s*(.+?)\s*$', re.IGNORECASE)
_EVIDENCE_POOL_E_LINE_RE = re.compile(r'^\s*E\d*\s*:\s*"?(.*?)"?\s*$', re.IGNORECASE)

def _extract_evidence_pool(section_text: str, start_marker: str, son_marker: str):
    """<<<...>>> ... <<<..._SON>>> bloğunu METİNDEN AYIKLAR (madde 7 — final report gövdesinde
    kalmaz). Dönüş: ({kriter_adı: [alıntı, ...]}, havuzsuz_metin). Başlangıç marker'ı yoksa VEYA
    kapanış marker'ı bulunamıyorsa (bozuk/malformed, madde 10) DOKUNMADAN (boş sözlük, metin
    AYNEN) döner — mevcut davranış korunur, rapor kırılmaz."""
    if not section_text or start_marker not in section_text:
        return {}, section_text
    start_idx = section_text.find(start_marker)
    end_idx = section_text.find(son_marker, start_idx)
    if end_idx == -1:
        return {}, section_text
    pool_block = section_text[start_idx + len(start_marker): end_idx]
    remaining = section_text[:start_idx] + section_text[end_idx + len(son_marker):]
    pool = {}
    current = None
    for ln in pool_block.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        mcrit = _EVIDENCE_POOL_CRIT_LINE_RE.match(ln)
        if mcrit:
            current = mcrit.group(1).strip()
            pool.setdefault(current, [])
            continue
        mev = _EVIDENCE_POOL_E_LINE_RE.match(ln)
        if mev and current is not None:
            txt = mev.group(1).strip()
            if txt and txt.upper() != "YOK":
                pool[current].append(txt)
    return pool, remaining

def _merge_evidence_pool_into_table(table_text: str, pool: dict):
    """Havuzdaki E1/E2/... alıntılarını, KRİTER adı tablo satırının 1. hücresiyle BİREBİR
    (fuzzy/semantic DEĞİL — madde 5, 9) eşleşiyorsa o satırın 3. hücresine EKLER; mevcut gerekçe
    metni varsa KORUNUR (silinmez), 4. sütun OLUŞMAZ. Eşleşmeyen kriter adı için hiçbir hücre
    değiştirilmez, yalnız warning döner (madde 9) — rapor/score ETKİLENMEZ."""
    if not pool or not table_text:
        return table_text, []
    warnings = []
    lines = table_text.splitlines()
    row_idx_by_name = {}
    for i, ln in enumerate(lines):
        if ln.count("|") < 2:
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 2 or not cells[0]:
            continue
        row_idx_by_name.setdefault(cells[0], i)
    for crit_name, quotes in pool.items():
        if not quotes:
            continue
        li = row_idx_by_name.get(crit_name)
        if li is None:
            warnings.append(f"[KANIT HAVUZU] '{crit_name}' tablo satırıyla BİREBİR eşleşmedi — kanıt taşınmadı (tahmin yapılmadı).")
            continue
        cells = [c.strip() for c in lines[li].strip().strip("|").split("|")]
        evidence_add = " ".join(f'"{q}"' for q in quotes)
        if len(cells) >= 3:
            cells[2] = f"{cells[2]} {evidence_add}".strip() if cells[2] else evidence_add
        else:
            while len(cells) < 2:
                cells.append("")
            cells.append(evidence_add)
        lines[li] = "| " + " | ".join(cells) + " |"
    return "\n".join(lines), warnings

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
            # ── SAYI YOK → İŞ EMRİ — KRİTER KAPSAMA + YETERSİZ CEVAP PUANLAMA: ÜÇ DURUM.
            #    (a) HİÇ SORULMADI (transkriptte/coverage'da kanıt yok) → SİSTEM kaynaklı, PAYDA
            #        DIŞI, 'Değerlendirilemedi (sistem)'.
            #    (b) AÇIK RET / tamamen alakasız cevap (dar koşul, DEĞİŞMEDİ) → 0/cap, PAYDA İÇİNDE.
            #    (c) SORULDU (yeterli fırsat/takip verilmiş — bkz. INTERVIEWER_REASK_RULES) ama
            #        değerlendirilebilir bir cevap alınamadı (boş/'bilmiyorum'/anlamadım/mülakatçı
            #        ısrarı, VEYA valid_ask olduğu halde model puan yazmamış) → TABAN PUAN (tavanın
            #        %25'i, _insufficient_answer_floor_score), PAYDA İÇİNDE — 'Değerlendirilemedi'
            #        DEĞİL, adayın kusuru da DEĞİL; sorgulama GERÇEKTEN yapıldı.
            gm = re.search(r"[—:–\-]\s*(.+)$", re.sub(r"\((?:sorulmad[ıi]|soruldu[^)]*|sistem)\)", "", puan_cell, flags=re.IGNORECASE))
            reason = (gm.group(1).strip() if gm else "")
            status = _criterion_ask_status(cname, criteria_coverage, transcript, _repeated_unanswered)
            refusal = bool(_OPEN_REFUSAL_RE.search(puan_cell))
            _found = best is not None and best_s >= 0.34

            if _found and status == "valid_ask" and refusal:
                # (b) ADAY kaynaklı 0 — DAR KOŞUL, DEĞİŞMEDİ
                denom_cap += cap
                _gk = reason or "aday cevap vermeyi reddetti / tamamen alakasız cevap verdi"
                cand_missing.append({"kriter": cname, "gerekce": _gk})
                li = best["line_idx"]
                lines[li] = lines[li].replace(f"| {puan_cell} |", f"| 0/{cap} — Yetersiz (aday): {_gk} |", 1)
            elif status == "not_asked":
                # (a) SİSTEM kaynaklı — PAYDA DIŞI, DEĞİŞMEDİ
                _sysreason = "bu kriter mülakatta sorulmadı"
                if _found and refusal:
                    warnings.append(f"'{cname}' hücrede 'ret' geçiyor ama geçerli aday cevabı yok → sistem kaynaklı eksik sayıldı (payda dışı).")
                if not _found:
                    warnings.append(f"'{cname}' kriteri rapor tablosunda bulunamadı — sistem kaynaklı eksik (payda dışı).")
                sys_missing.append({"kriter": cname, "gerekce": _sysreason})
                if _found and puan_cell:
                    li = best["line_idx"]
                    lines[li] = lines[li].replace(f"| {puan_cell} |", f"| Değerlendirilemedi (sistem) — {_sysreason} |", 1)
            else:
                # (c) YENİ — TABAN PUAN (%25), PAYDA İÇİNDE
                floor_awarded = _insufficient_answer_floor_score(cap)
                _gk = reason or "kriter sorulmuş, sorgulama/takip fırsatına rağmen değerlendirilebilir bir aday cevabı alınamadı"
                denom_cap += cap
                awarded_sum += floor_awarded
                cand_missing.append({"kriter": cname, "gerekce": _gk, "puan_turu": "taban_puan_25"})
                if not _found:
                    warnings.append(f"'{cname}' kriteri rapor tablosunda bulunamadı ama sorulduğuna dair kanıt var — taban puan ({floor_awarded}/{cap}) uygulandı, payda içinde sayıldı.")
                elif puan_cell:
                    li = best["line_idx"]
                    lines[li] = _rewrite_criterion_cell(lines[li], puan_cell, f"{floor_awarded}/{cap}",
                                                        f"Taban puan (sorgulandı, yeterli cevap alınamadı): {_gk}")
            continue

        awarded = _safe_int(mm.group(1))
        written_cap = _safe_int(mm.group(2)) if (mm.re.groups >= 2 and mm.group(2)) else None
        if awarded > cap:
            warnings.append(f"'{cname}' puanı {awarded} kendi tavanını ({cap}) aşıyordu → {cap}'e sabitlendi.")
            awarded = cap
        elif written_cap is not None and written_cap != cap:
            warnings.append(f"'{cname}' payda {written_cap} yazılmış, gerçek tavan {cap} → düzeltildi.")
        # YENİ KURAL — modelin verdiği 0: hiç sorulmadıysa SİSTEM kaynaklı eksiktir (paydaya
        # girmez). SORULDU ama geçerli cevap yoksa İŞ EMRİ — KRİTER KAPSAMA + YETERSİZ CEVAP
        # PUANLAMA gereği artık 'Değerlendirilemedi' DEĞİL — TABAN PUAN (%25, payda İÇİNDE).
        # Geçerli cevap varsa (valid_ask) modelin 0'ı olduğu gibi korunur (aşağıya düşer).
        if awarded == 0:
            _st0 = _criterion_ask_status(cname, criteria_coverage, transcript, _repeated_unanswered)
            if _st0 == "not_asked":
                _sr0 = "bu kriter mülakatta sorulmadı"
                warnings.append(f"'{cname}' modelce 0/{cap} verilmiş ama bu kriter hiç sorulmamış → 'Değerlendirilemedi (sistem)', payda dışı.")
                sys_missing.append({"kriter": cname, "gerekce": _sr0})
                li = best["line_idx"]
                lines[li] = lines[li].replace(f"| {puan_cell} |", f"| Değerlendirilemedi (sistem) — {_sr0} |", 1)
                continue
            elif _st0 != "valid_ask":
                floor_awarded = _insufficient_answer_floor_score(cap)
                _sr0 = "kriter sorulmuş, sorgulama/takip fırsatına rağmen değerlendirilebilir bir aday cevabı alınamadı"
                warnings.append(f"'{cname}' modelce 0/{cap} verilmiş, kriter sorulmuş ama geçerli cevap yok → taban puan {floor_awarded}/{cap} uygulandı (payda içinde, 'Değerlendirilemedi' DEĞİL).")
                cand_missing.append({"kriter": cname, "gerekce": _sr0, "puan_turu": "taban_puan_25"})
                li = best["line_idx"]
                lines[li] = _rewrite_criterion_cell(lines[li], puan_cell, f"{floor_awarded}/{cap}",
                                                    f"Taban puan (sorgulandı, yeterli cevap alınamadı): {_sr0}")
                awarded_sum += floor_awarded
                denom_cap += cap
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
    # İŞ EMRİ — NİHAİ RAPOR TUTARLILIĞI: TEK canonical yuvarlama (_round_half_up, madde 5) —
    # bu, birincil score_position'ın kendisi; sonraki tüm final hesaplar buna dayanır.
    normalized = max(0, min(100, _round_half_up(awarded_sum / evaluated_cap * 100)))
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

# TUR 4 / GÖREV 5.1+5.3 — PUAN 2 bölgesinin GÖRÜNTÜLENME SIRASINI garanti altına alır.
_PUANI_LINE_RE = re.compile(r'\*\*\s*PROF\S*\s+PUANI\s*[:：]', re.IGNORECASE)
_VETO_LINE_RE = re.compile(r'Profil Veto Kontrol|\[VETO\s*[:：]|^\s*Veto yok\.?\s*$', re.IGNORECASE)

def _canonicalize_profile_region_order(p2_text: str) -> str:
    """PUAN 2 bölgesini KAYIPSIZ olarak REPORT_BODY_SECTIONS'taki şablon sırasına dizer:
    başlık/açıklama → KRİTER TABLOSU → PROFİL PUANI → Veto Kontrolü. Model bazen tabloyu
    atlıyor/geç üretiyor ya da sırayı karıştırıyor olabilir — bu, raporda 'PUAN 2 tablosu
    kayıp, yalnızca PROFİL PUANI + Veto yok. var' görünümüne yol açar (GÖREV 5 kök nedeni).
    Fonksiyon METNİ DEĞİŞTİRMEZ, yalnız blokların SIRASINI düzeltir; hiçbir içerik silinmez —
    sınıflanamayan paragraflar 'tablo/puanı/veto'dan sonra geldiyse aralarında korunur.
    GENEL KURAL: her aday/rapor için çalışır, Kader'e özel bir dal İÇERMEZ."""
    if not p2_text or not p2_text.strip():
        return p2_text
    lines = p2_text.splitlines()

    # 1) Tablo — en uzun ardışık "en az 2 '|' içeren satır" grubu.
    table_idx = [i for i, ln in enumerate(lines) if ln.count("|") >= 2]
    table_block, rest_lines = [], list(lines)
    if table_idx:
        runs, cur = [], [table_idx[0]]
        for i in table_idx[1:]:
            if i == cur[-1] + 1:
                cur.append(i)
            else:
                runs.append(cur)
                cur = [i]
        runs.append(cur)
        best = max(runs, key=len)
        table_block = lines[best[0]:best[-1] + 1]
        rest_lines = lines[:best[0]] + lines[best[-1] + 1:]

    # 2) Kalan satırları boş satırlarla ayrılmış paragraflara böl, her paragrafı sınıflandır.
    paragraphs, cur_p = [], []
    for ln in rest_lines:
        if ln.strip():
            cur_p.append(ln)
        elif cur_p:
            paragraphs.append(cur_p); cur_p = []
    if cur_p:
        paragraphs.append(cur_p)

    heading_paras, puani_paras, veto_paras, other_paras = [], [], [], []
    for p in paragraphs:
        joined = "\n".join(p)
        if _PUANI_LINE_RE.search(joined):
            puani_paras.append(p)
        elif _VETO_LINE_RE.search(joined):
            veto_paras.append(p)
        elif puani_paras or veto_paras:
            other_paras.append(p)  # tablo/puanı/veto'dan SONRA gelen sınıflanamayan içerik — kaybetme
        else:
            heading_paras.append(p)

    def _flat(paras):
        return "\n\n".join("\n".join(p) for p in paras)

    blocks = [_flat(heading_paras), "\n".join(table_block).strip(), _flat(puani_paras), _flat(other_paras), _flat(veto_paras)]
    out = "\n\n".join(b for b in blocks if b.strip())
    return out if out.strip() else p2_text

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
    # 2026-09 rapor yeniden tasarımı — eski kod burada 'PROFİL PUANI' satırının ÖNCEDEN var
    # olmasını şart koşuyordu (o zamanki model her zaman tabloyla BİRLİKTE bu satırı da
    # yazıyordu). Yeni mimaride finalize_interview bu fonksiyona artık İZOLE, SADECE tablo
    # içeriğini veriyor (===KİŞİSEL VE BİLİŞSEL PROFİL=== ayracından çıkan metin — 'PROFİL
    # PUANI' satırı YOK, çünkü modelden artık İSTENMİYOR) — eski şart puanlamayı hep None
    # bırakıyordu (kök neden, sentetik testte yakalandı). Artık: metin gerçek bir kriter
    # tablosu İÇERİYORSA (yeterli sayıda '|' satırı) devam edilir; hiçbiri yoksa hâlâ vazgeçilir.
    if not re.search(r'PROF\S*\s+PUANI', profile_region, re.IGNORECASE) and profile_region.count("|") < 4:
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
            # İŞ EMRİ — KRİTER KAPSAMA + YETERSİZ CEVAP PUANLAMA (PUAN 1 ile AYNI ÜÇ DURUM):
            #    (a) HİÇ SORULMADI → SİSTEM kaynaklı, PAYDA DIŞI, 'Değerlendirilemedi (sistem)'.
            #    (b) AÇIK RET / tamamen alakasız cevap (dar koşul, DEĞİŞMEDİ) → 0/cap, PAYDA İÇİNDE.
            #    (c) SORULDU ama değerlendirilebilir cevap alınamadı → TABAN PUAN (%25), PAYDA İÇİNDE.
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
            elif status == "not_asked":
                _sr = "bu kriter mülakatta ölçülmedi"
                if not _found:
                    warnings.append(f"[PROFİL] '{cname}' profil tablosunda bulunamadı — sistem kaynaklı eksik (payda dışı).")
                sys_missing.append({"kriter": cname, "gerekce": _sr})
                if _found and puan_cell:
                    li = best["line_idx"]
                    lines[li] = lines[li].replace(f"| {puan_cell} |", f"| Değerlendirilemedi (sistem) — {_sr} |", 1)
            else:
                floor_awarded = _insufficient_answer_floor_score(cap)
                _gk = reason or "kriter sorulmuş, sorgulama/takip fırsatına rağmen değerlendirilebilir bir aday cevabı alınamadı"
                denom_cap += cap
                awarded_sum += floor_awarded
                cand_missing.append({"kriter": cname, "gerekce": _gk, "puan_turu": "taban_puan_25"})
                if not _found:
                    warnings.append(f"[PROFİL] '{cname}' profil tablosunda bulunamadı ama sorulduğuna dair kanıt var — taban puan ({floor_awarded}/{cap}) uygulandı, payda içinde sayıldı.")
                elif puan_cell:
                    li = best["line_idx"]
                    lines[li] = _rewrite_criterion_cell(lines[li], puan_cell, f"{floor_awarded}/{cap}",
                                                        f"Taban puan (sorgulandı, yeterli cevap alınamadı): {_gk}")
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
            if _st0 == "not_asked":
                _sr0 = "bu kriter mülakatta ölçülmedi"
                warnings.append(f"[PROFİL] '{cname}' modelce 0/{cap} verilmiş ama bu kriter hiç sorulmamış → 'Değerlendirilemedi (sistem)', payda dışı.")
                sys_missing.append({"kriter": cname, "gerekce": _sr0})
                lines[best["line_idx"]] = lines[best["line_idx"]].replace(f"| {puan_cell} |", f"| Değerlendirilemedi (sistem) — {_sr0} |", 1)
                continue
            elif _st0 != "valid_ask":
                # İŞ EMRİ — KRİTER KAPSAMA + YETERSİZ CEVAP PUANLAMA: sorulmuş ama geçerli cevap
                # alınamamışsa 'Değerlendirilemedi' DEĞİL — TABAN PUAN (%25), payda İÇİNDE.
                floor_awarded = _insufficient_answer_floor_score(cap)
                _sr0 = "kriter sorulmuş, sorgulama/takip fırsatına rağmen değerlendirilebilir bir aday cevabı alınamadı"
                warnings.append(f"[PROFİL] '{cname}' modelce 0/{cap} verilmiş, kriter sorulmuş ama geçerli cevap yok → taban puan {floor_awarded}/{cap} uygulandı (payda içinde, 'Değerlendirilemedi' DEĞİL).")
                cand_missing.append({"kriter": cname, "gerekce": _sr0, "puan_turu": "taban_puan_25"})
                lines[best["line_idx"]] = _rewrite_criterion_cell(lines[best["line_idx"]], puan_cell, f"{floor_awarded}/{cap}",
                                                                  f"Taban puan (sorgulandı, yeterli cevap alınamadı): {_sr0}")
                awarded_sum += floor_awarded
                denom_cap += cap
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
    # İŞ EMRİ — NİHAİ RAPOR TUTARLILIĞI: TEK canonical yuvarlama (_round_half_up, madde 5) —
    # bu, birincil score_profile'ın kendisi; sonraki tüm final hesaplar buna dayanır.
    normalized = max(0, min(100, _round_half_up(awarded_sum / denom_cap * 100)))
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

    # TUR 2 / GÖREV D — DOĞRULAMA kareleri: PDF ile TEK KAYNAK. select_verification_frames
    # (mimik havuzundan, süreye yayılmış, seviye bazlı) — eski non-mimic snapshot seti DEĞİL.
    try:
        _vframes = select_verification_frames(candidate_id, level)
    except Exception as e:
        print(f"UYARI (compute_modality_coverage: select_verification_frames c={candidate_id}): {type(e).__name__}: {e}")
        _vframes = []
    v_min = []
    for f in _vframes:
        if f.get("elapsed_ms") is not None:
            v_min.append(_safe_int(f["elapsed_ms"]) / 60000)
        elif started and f.get("captured_at"):
            ca = _parse_iso(f["captured_at"])
            if ca:
                v_min.append(max(0.0, (ca - started).total_seconds() / 60))
    out["dogrulama"] = _frame_distribution(sorted(v_min), total_min)
    out["dogrulama"]["n"] = len(_vframes)
    out["dogrulama"]["kaynak"] = "mimik havuzundan, mülakat süresine yayılmış"

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

def build_technical_annex(candidate_id: int, level: int) -> str:
    """TUR 3 / GÖREV 5.2 — HAM metrikler + kare sayıları + tur sayaçları + soru-tekrarı tespiti.
    Rapor GÖVDESİNE GİRMEZ; interviews.technical_annex kolonuna yazılır, PDF'te EN SONDA
    (kamera karelerinden sonra) 'Teknik Ek (yalnızca yönetici)' başlığıyla basılır.
    TUR 4 / GÖREV 6.3 — seviye kamera/ses YAKALAMIYORSA (Level 1 — metin tabanlı) bu ek hiç
    üretilmez: "hiç alınmadı / TOPLANAMADI" gibi satırlar hiç beklenmeyen bir veriyi eksik gibi
    gösterip yanıltır. GENEL KURAL, her aday/seviye için geçerli."""
    if not _level_has_camera(level) and not _level_has_voice(level):
        return ""
    c = compute_modality_coverage(candidate_id, level)
    lines = ["**Teknik Ek (yalnızca yönetici — ham veri; müşteri raporuna girmez):**", ""]

    def _frame_line(label, d):
        if d["n"] == 0:
            return f"- {label}: hiç alınmadı."
        span = f" — {d['ilk_dk']}.–{d['son_dk']}. dk" if d["ilk_dk"] is not None else ""
        src = f" ({d['kaynak']})" if d.get("kaynak") else ""
        warn = "  [dar aralık uyarısı]" if d["kumelenme"] else ""
        return f"- {label}: {d['n']} kare{span}{src}.{warn}"

    lines.append(_frame_line("Kamera doğrulama kareleri (PDF galerisi)", c["dogrulama"]))
    lines.append(_frame_line("Mimik analiz kareleri (kaynak havuz)", c["mimik"]))

    if c["ses_metrikleri_var"] and c["ses_ozet"]:
        s = c["ses_ozet"]
        def _n(x, unit=""):
            return f"{x}{unit}" if x is not None else "—"
        _ev = s.get('hesaba_katilan_tur') if s.get('hesaba_katilan_tur') is not None else s.get('tur_sayisi')
        _cevapsiz = s.get('cevapsiz_tur_sayisi')
        _tot = (_ev + _cevapsiz) if (_ev is not None and _cevapsiz is not None) else s.get('toplam_tur')
        _basis = f"{_n(_ev)}" + (f" (toplam {_tot} ses turu = {_n(_ev)} değerlendirilen + {_n(_cevapsiz)} cevapsız/halüsinasyon)" if _tot is not None else "")
        lines.append(
            "- Ses metrikleri (tur bazlı): "
            f"hesaba katılan tur {_basis}; aday konuşma toplam {_n(s['aday_konusma_toplam_sn'],' sn')}; "
            f"ort. tur uzunluğu {_n(s['ortalama_tur_uzunlugu_sn'],' sn')}; "
            f"yanıt gecikmesi ort. {_n(s['yanit_gecikmesi_ort_sn'],' sn')}; "
            f"AI düşünme süresi ort. {_n(s['ai_dusunme_suresi_ort_sn'],' sn')}; "
            f"söz kesme {_n(s['soz_kesme_sayisi'])} (güven: {_n(s['guven'])})."
        )
    else:
        lines.append("- Ses metrikleri: TOPLANAMADI (realtime_events'te konuşma başlangıç/bitiş olayı yok).")

    try:
        rep = detect_repeated_questions(candidate_id, level)
        for r in rep:
            lines.append(f"- Soru tekrarı: Mülakatçı \"{r['kriter']}\" konusunda {r['count']} kez ısrar etti"
                         f"{' (' + r['span'] + ')' if r.get('span') else ''}; aday bu turlarda yeterli yanıt vermedi. (Gözlem — puana etki etmez.)")
    except Exception as e:
        print(f"UYARI (technical_annex soru tekrarı c={candidate_id} L{level}): {type(e).__name__}: {e}")

    mimic, metrics, obs = _read_modality_json(candidate_id, level)
    if mimic:
        lines.append("- Mimik analizi (ham JSON): " + json.dumps(mimic, ensure_ascii=False))
    if obs:
        lines.append("- Mülakatçı ses gözlemleri (ham): " + json.dumps(obs, ensure_ascii=False))
    return "\n".join(lines)

def build_modality_coverage_note(candidate_id: int, level: int) -> str:
    """GERİYE UYUMLULUK — TUR 3'te işlevi build_modality_prose + build_technical_annex'e bölündü.
    Artık rapor gövdesine RAW blok EKLENMEZ. Bu fonksiyon boş döner (eski çağrılar no-op)."""
    return ""

# ═══ B2 — SORU TEKRARI TESPİTİ (sunucu tarafı, L2/L3 sesli) ═══
# L1'de [YENIDEN] etiketini sayan sunucu sayacı var; L2/L3 sesli hatta yok. Kayıtlı transkript
# üzerinden art arda gelen mülakatçı sorularının kelime örtüşmesine bakarak "aynı soru N kez"
# durumunu tespit eder. SES HATTINA DOKUNMAZ. PUANA ETKİ ETMEZ — yalnız gözlem.
_QREPEAT_OVERLAP_THRESHOLD = 0.55   # ardışık mülakatçı soruları arasında anlamlı kelime Jaccard eşiği
_QREPEAT_MIN_RUN = 3               # "2 denemeyi aşan" = aynı sorunun 3+ kez sorulması

# (eski _ANNOTATE_STOPWORDS GÖREV 1.2'de silindi; soru-tekrarı tespiti için gereken küçük küme burada)
_QREPEAT_STOPWORDS = {"ve", "ile", "veya", "ya", "da", "de", "için", "bir",
                      "karar", "yaklasim", "yaklasimi", "verme", "yonetim", "yonetimi"}

def _q_keywords(text: str) -> set:
    return {w for w in _norm_name(text).split() if len(w) >= 4 and w not in _QREPEAT_STOPWORDS}

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
            # İş emri — RAPOR İÇERİK STANDARDI / A5 — KÖK NEDEN: best_crit bulunamadığında ("bir konu"
            # yer tutucusu) bu satır ESKİDEN yine de eklenip EK 3'e "Mülakatçı 'bir konu' konusunda
            # N kez ısrar etti" diye AYNEN basılıyordu — literal placeholder müşteri raporunda kaldı.
            # Fix: eşleşen kriter YOKSA bu bulgu tamamen ATLANIR (uydurma/placeholder isim BASILMAZ).
            if not best_crit:
                continue
            ts0 = q_items[run[0]]["ts"]
            ts1 = q_items[run[-1]]["ts"]
            span = f"[{ts0}]–[{ts1}]" if ts0 and ts1 else ""
            out.append({"kriter": best_crit, "count": len(run), "span": span})
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
    if candidate["ai_note"] and candidate["ai_note"].strip():
        ai_note_section = (f"\n\nADAY ÖZEL AI NOTU (bu mülakatta bu konuya öncelik verilmiş olmalı; transkriptte nasıl ele "
                           f"alındığını değerlendir ve Yönetici Özeti'nde veya ilgili olduğu Güçlü Yönler/Gelişim Alanları "
                           f"maddesinde somut olarak yansıt — ayrı bir başlık AÇMA):\n{candidate['ai_note'].strip()[:1200]}")
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
    # 2026-09 rapor yeniden tasarımı — TEK gövde şablonu (bkz. build_report_content_prompt);
    # kimlik/tarih alanları modelden İSTENMEZ (Başlık Şeridi sistem tarafından deterministik
    # üretilir), Tutarlılık/Çelişki artık tamamen deterministik "Beyan Tutarlılığı" bölümü
    # (compute_field_discrepancies) — modele SİSTEM ALAN KARŞILAŞTIRMASI bloğu VERİLMEZ, sen
    # bunu yazmazsın.
    _has_real_ts = _transcript_has_real_timestamps(transcript)
    report_body_l2 = build_report_content_prompt(
        build_criteria_table_filled(pos["criteria"]), build_profile_table_filled(), has_real_timestamps=_has_real_ts)
    return f"""Aşağıda bir sesli iş mülakatının transkripti, aday CV'si, pozisyon kriterleri ve derinlik bilgisi vardır. İnsan kaynakları yöneticisinin karar vermesine yardım edecek, adaya özgü ve ayrıntılı bir değerlendirme raporu üret.

Aday: {candidate['name']}
Pozisyon: {candidate['position']}
Mülakat seviyesi: Level {candidate_level}
Derinlik: {depth_tier}
Kriterler ({total_weight} puan):
{criteria_text}

KAYIT FORMU BEYANI (adayın/adminin başvuru formunda girdiği bilgi — çelişki taramasını SEN yapmazsın, sistem deterministik yapar, bu bilgiyi yalnızca bağlam için al):
- E-posta: {candidate['email'] or '—'}
- Eğitim: {candidate['education'] or '—'} · Üniversite: {candidate['university'] or '—'} · Bölüm: {candidate['department'] or '—'}
- Deneyim yılı (beyan): {candidate['experience_years'] if candidate['experience_years'] not in (None, '', 0) else '—'}

ADAYIN CV'Sİ:
{cv_for_report}{ai_note_section}{coverage_block}{unanswered_block}{extra_notes}

TRANSKRİPT:
{(transcript or '')[:TRANSCRIPT_PROMPT_MAX_CHARS]}

TEMEL KURALLAR:
- Değerlendirmeyi dikkatli ve kanıta dayalı yap. Her kriteri yalnızca o kriterle doğrudan ilişkili mülakat kanıtlarıyla değerlendir. İlgisiz kanıtları kriterler arasında taşıma ve kanıtın desteklemediği çıkarımlar yapma. Teknik bilgi veya mevcut yazılım kullanımını tek başına analitik düşünme, öğrenme/adaptasyon, inisiyatif veya başka bir davranışsal yetkinliğin kanıtı sayma. Aynı kanıtı birden fazla kriterde ancak kanıt her kriteri bağımsız ve doğrudan destekliyorsa kullan. Olumlu ve olumsuz kanıtları birlikte değerlendir. Puan, gerekçe ve kullanılan kanıt birbiriyle tutarlı olsun.
- Rapor {report_lang} dilinde yazılacak.
- Yalnızca adayın gerçekten söylediği sözler mülakat kanıtıdır. Mülakatçının açıklamalarını adaya mal etme.
- CV bilgisi ile mülakat kanıtını ayır: “CV'de belirtilmiştir” ve “mülakatta doğrulanmıştır/doğrulanamamıştır” ifadelerini açık kullan.
- Adayın söylemediği deneyim, beceri, sonuç, motivasyon veya kişilik özelliği uydurma.
- ADAYIN KENDİ BEYAN ETTİĞİ BİLGİ EKSİKLİKLERİ: Aday transkriptte kendi ağzıyla bir konuda bilgisi/deneyimi olmadığını söylediyse (ör. "kurumlar vergisi ve e-defter kısmında bilgim yok"), bu İLGİLİ kriterin kanıt hücresinde AÇIKÇA yer alacak ve dakika damgasıyla alıntılanacak. Bu otomatik düşük puan demek değildir — ama kanıtın GÖRÜLMESİ ve analize dahil edilmesi zorunludur; sessizce atlama.
- Aynı kalıp cümleleri her bölümde tekrar etme. Rapor bu adaya özgü olmalı; somut proje, karar, örnek ve ifadeleri kullan.
{CRITERION_SCORING_RULE}
{SCORING_RUBRIC}
- Toplam puanı yalnızca PUANLANAN kriterlerin ağırlığına göre normalize et. 'Değerlendirilemedi (sistem)' kriterleri hesaba KATMA.
- PUAN TAVANI (KESİN): Hiçbir kriter puanı kendi tavanını (ağırlığını) AŞAMAZ. "Uyum 12/10" gibi bir şey ASLA yazma; en fazla "10/10". TOPLAM PUAN = alınan puanların toplamı. Bunu doğru hesapla, sistem ayrıca doğrular.
- Pozisyon Yetkinlikleri ve Kişisel/Bilişsel Profil AYRI İKİ TABLODUR — bir kriteri diğerinin tablosuna YAZMA, KARIŞTIRMA.
- Rapor bir KARAR/ÖNERİ (İşe Al/Reddet/vb.) İÇERMEZ — bunu sen yazmazsın, sistem puanlardan üretir.
- KRİTER TABLOSU DETERMİNİSTİK: Rapordaki kriter tablosunun satırları YUKARIDA verilen tablonun BİREBİR AYNISI olacak — aynı kriter adları, aynı sıra, aynı tavanlar. Satır ekleme, çıkarma, birleştirme veya yeniden adlandırma YOK. Gerekçesiz eksik-işaretleme YASAK.
- Erken sonlandırma, davranış gözlemi veya pozisyon uyumsuzluğu notu verildiyse: raporda ilgili olduğu bölümde SOMUT (dakika + transkriptteki söz) yaz; bunları TEK BAŞINA puan düşürme gerekçesi yapma.
- Her puan için Kanıt → Analiz → Sonuç zinciri kur.
- Analitik düşünme, kavrama, muhakeme, neden-sonuç kurma, problem çözme, düşünce esnekliği, öğrenme çevikliği ve belirsizlikte karar verme hakkında yalnızca transkriptte gözlenebilen sinyalleri yaz. IQ, zekâ puanı, psikiyatrik tanı, yalan tespiti veya kesin kişilik teşhisi yapma.
- Görüşme kalitesi veya teknik kesinti değerlendirmeyi etkilediyse bunu ayrıca belirt; adayı bunun için cezalandırma.
- HAM SAYI YASAĞI (KESİN): Rapor gövdesine ses/mimik/kamera METRİĞİ SAYISI (konuşma süresi sn, tur sayısı, yanıt gecikmesi sn, söz kesme sayısı, kare sayısı vb.) YAZMA — sistem bunu ayrı 'Görüntü ve Ses Gözlemi' bölümünde insan diliyle, ekte ham olarak veriyor. Rapor gövdesinde İZİN VERİLEN sayılar YALNIZCA: kriter puanları (16/20 gibi) ve dakika damgaları ([1:21] gibi).
- En az üç anlamlı aday cevabı yoksa [DEĞERLENDİRİLEMEDİ] üret.
- Derinlik “derin” ise rapor daha kapsamlı, daha fazla çapraz kanıtlı ve daha ayrıntılı olmalı; standart rapor da kesinlikle yüzeysel olmamalı.

TAM FORMAT:
[MÜLAKATBİTTİ]
---RAPOR---
{report_body_l2}
---RAPORSON---

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

    _lang = (candidate["interview_language"] if "interview_language" in candidate.keys() else "tr") or "tr"
    # TUR 4 / GÖREV 2 — HAM transkript BİR KEZ yazılır (write-once); temizleme HER ZAMAN
    # bu ham veriden başlar, önceki çalıştırmanın çıktısından DEĞİL.
    capture_transcript_raw(effective_candidate_id, candidate_level, data.transcript)
    _raw_transcript = get_transcript_raw(effective_candidate_id, candidate_level) or data.transcript
    # TUR 2 / GÖREV C — TEK transkript temizleyici (konuşmacı etiketi + yankı + sızıntı + damga
    # + halüsinasyon), rapor üretim yolunun her noktasında AYNI.
    _clean_transcript, _spk_changes, _hall_filtered, _hall_n = normalize_transcript_for_report(_raw_transcript, _lang)
    if _spk_changes:
        record_realtime_events(effective_candidate_id, candidate_level,
                               [{"type": "transcript_speaker_fix", "data": ch, "elapsed_ms": _safe_int(ch.get("ts")) * 1000 if ch.get("ts") else 0} for ch in _spk_changes])
        print(f"[TRANSCRIPT_FIX server] c={effective_candidate_id} {len(_spk_changes)} satır düzeltildi/kaldırıldı: "
              + ", ".join(sorted({c['tip'] for c in _spk_changes})))
    if _hall_n:
        _dedup_filter_events(effective_candidate_id, candidate_level, _hall_filtered)
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
    _job_id = _mark_finish_pending(effective_candidate_id, candidate_level, provider="openai", model=OPENAI_REPORT_MODEL,
                                   system=None, payload=report_prompt, terminated_reason=None, reason="l2_normal")
    if _job_id:
        background_tasks.add_task(run_deferred_finish_job, effective_candidate_id, candidate_level)
    return {
        "message": "Mülakatınız tamamlandı, teşekkür ederiz. Raporunuz hazırlanıyor.",
        "completed": True, "processing": True, "score": None, "recommendation": None,
    }

@app.get("/api/admin/snapshots/{candidate_id}")
def get_snapshots(candidate_id: int, level: Optional[int] = None, payload=Depends(verify_admin), db=Depends(db_dep)):
    # GÖREV 3 — doğrulama kareleri MİMİK havuzundan, mülakat süresine yayılmış olarak seçilir
    # (L1=0, L2=4, L3=6). Ayrı bir doğrulama seti kullanılmaz.
    lvl = level
    if lvl is None:
        cr = db.execute("SELECT level FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        lvl = (cr["level"] if cr else None) or 1
    frames = select_verification_frames(candidate_id, lvl)
    return [{"id": r["id"], "image_base64": r["image_base64"], "captured_at": r.get("captured_at"),
             "elapsed_ms": r.get("elapsed_ms")} for r in frames]


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
        from reportlab.lib.enums import TA_CENTER, TA_RIGHT
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
    # EK GÖREV 8.3/8.4 — dört özet hücresi (PUAN 1/2, ORTALAMA, ÖNERİ) AYNI, KÜÇÜLTÜLMÜŞ boyut;
    # kelime-ortası bölme kapalı ("Değerlendirmeye Al" gibi uzun metin hücreyi taşırmasın).
    _metric_style = ParagraphStyle(name="Metric", parent=styles["BodyText"], fontName=font_bold, fontSize=13, leading=16, alignment=TA_CENTER, textColor=rl_colors.HexColor("#1e3a5f"))
    _metric_style.splitLongWords = 0
    _metric_style.wordWrap = None
    styles.add(_metric_style)
    styles.add(ParagraphStyle(name="MiniHeading", parent=styles["BodyText"], fontName=font_bold, fontSize=9.5, leading=12, textColor=rl_colors.HexColor("#92400e"), spaceBefore=2, spaceAfter=4))

    story = []
    story.append(Paragraph("MedeX AI Interview Report", styles["BrandTitle"]))
    story.append(Paragraph("Aday mülakat değerlendirme raporu", styles["Subtitle"]))
    story.append(Spacer(1, 10))

    styles.add(ParagraphStyle(name="MetricRight", parent=styles["Metric"], alignment=TA_RIGHT))

    # ============ BAŞLIK ŞERİDİ (iş emri madde 4) — sol: kimlik, sağ: SADECE Genel Puan + Öneri ============
    score = interview.get("score")
    recommendation = interview.get("recommendation")
    if not recommendation and score is not None:
        recommendation = decide_recommendation(score)
    # Eski kayıtlarda kanonik orta-bant etiketi "Değerlendirmeye Al" olabilir — göster: "Değerlendir".
    rec_display = "Değerlendir" if recommendation == "Değerlendirmeye Al" else (recommendation or "")

    _iv_date = format_pdf_datetime(interview.get("started_at")) if interview.get("started_at") else ""
    # ACİL — DÜZELTME: "Rapor Oluşturulma Tarihi" PDF'in indirildiği/üretildiği an DEĞİL, AI
    # raporunun GERÇEKTEN başarıyla üretildiği an olmalı — bu yüzden DB'deki mevcut alanlar
    # kullanılır (yeni kolon YOK): report_regenerated_at (regenerate_report → finalize_interview
    # regen dalı, main.py ~9327) varsa O; yoksa report_generated_at (ilk üretim, finalize_interview
    # normal dalı, main.py ~9355). İkisi de CURRENT_TIMESTAMP (DB motoru, UTC) ile yazılıyor —
    # görüntüde Türkiye saatine (sabit UTC+3, 2016'dan beri DST yok) çevrilir. PDF'in KENDİSİ her
    # indirmede yeniden üretilse de bu iki DB alanı yalnız GERÇEK (yeniden) rapor üretiminde
    # değiştiği için aynı rapor tekrar indirildiğinde tarih AYNI kalır.
    def _format_pdf_datetime_istanbul(value):
        if not value:
            return None
        if hasattr(value, "strftime"):
            dt = value
        else:
            s = str(value).strip().split(".")[0].replace("T", " ")
            dt = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    continue
            if dt is None:
                return None
        return (dt + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")

    _report_created_raw = interview.get("report_regenerated_at") or interview.get("report_generated_at")
    _report_created_at = _format_pdf_datetime_istanbul(_report_created_raw)
    left_lines = [f"<b>Aday:</b> {ptxt(candidate.get('name') or '')}",
                  f"<b>Pozisyon:</b> {ptxt(candidate.get('position') or '')}"]
    if _iv_date:
        left_lines.append(f"<b>Mülakat Tarihi:</b> {ptxt(_iv_date)}")
    if _report_created_at:
        left_lines.append(f"<b>Rapor Oluşturulma Tarihi:</b> {ptxt(_report_created_at)}")
    # İŞ EMRİ — MÜLAKAT LEVEL VE DERİNLİK BİLGİSİNİ GÖSTER: bu PDF'in ürettiği `interview` dict
    # BU PDF'in ait olduğu tam interview satırıdır (download_interview_pdf: candidate_id+target_level
    # ile SELECT edilir) — Level/Derinlik BURADAN okunur, candidate'ın güncel/genel alanından DEĞİL.
    # Değer yoksa/tanınmıyorsa TAHMİN EDİLMEZ, "—" gösterilir.
    _iv_level = interview.get("level")
    _level_display = f"L{_iv_level}" if _iv_level in (1, 2, 3) else "—"
    _iv_depth_key = (interview.get("depth_tier") or "").strip().lower()
    _depth_display = DEPTH_TIER_CONFIG.get(_iv_depth_key, {}).get("label") or "—"
    left_lines.append(f"<b>Level:</b> {ptxt(_level_display)}")
    left_lines.append(f"<b>Derinlik:</b> {ptxt(_depth_display)}")
    left_para = Paragraph("<br/>".join(left_lines), styles["BodyWrap"])

    right_lines = []
    if score is not None:
        right_lines.append(f"GENEL PUAN: {score}/100")
    if rec_display:
        right_lines.append(f"ÖNERİ: {ptxt(rec_display)}")
    right_para = Paragraph("<br/>".join(right_lines) if right_lines else "", styles["MetricRight"])

    header_strip = Table([[left_para, right_para]], colWidths=[10.0*cm, 6.8*cm])
    header_strip.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
    ]))
    story.append(header_strip)

    # Karar sınırına (40/80) ±2 yakınlık uyarısı — YALNIZ bu durumda, tek satır (madde 4).
    if score is not None:
        _near_edge = next((e for e in (40, 80) if abs(score - e) <= 2), None)
        if _near_edge is not None:
            story.append(Spacer(1, 4))
            story.append(Paragraph("Puan karar sınırına yakın — ikinci görüşme önerilir.",
                                   ParagraphStyle(name="NearEdge", parent=styles["BodyWrap"], textColor=rl_colors.HexColor("#b45309"), fontName=font_bold)))
    story.append(Spacer(1, 10))

    # ============ Kimlik/Oturum bilgileri (başlık şeridinin PARÇASI değil — ayrı, nötr blok) ============
    info = [
        ["E-posta", candidate.get("email") or "", "Telefon", candidate.get("phone") or ""],
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
    # adayın/adminin form üzerinden girdiği beyan (rapor gövdesindeki CV Özeti'nden AYRI kaynak,
    # bilinçli olarak burada uzlaştırılmıyor — görsel olarak ayrı blok/renk/başlıkla belli edilir).
    # Boş alan hiç basılmaz.
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

    # Sonuç Gerekçesi / İhlal Kaydı — deterministik, sistem üretimi (LLM içeriği değil); rapor
    # gövdesinin 14 bölümünden biri DEĞİLDİR, olay varsa (ihlal/erken bitiş) bağlam olarak önce gelir.
    try:
        _events = visible_result_events(json.loads(interview.get("result_events_json") or "[]"))
    except Exception:
        _events = []
    _rreason = sanitize_result_reason_for_customer((interview.get("result_reason") or "").strip())
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

    def _emit_report_block(block_lines):
        tr, tc = parse_markdown_table(block_lines)
        if tr:
            story.extend(flow_report_lines(block_lines, consumed=tc))
            clean_rows = []
            for row in tr:
                if any("Kriter" in c for c in row) or len(row) >= 3:
                    # İŞ EMRİ — PDF/REPORT RENDER KANIT KAYBI DÜZELTMESİ: 4+ mantıksal hücre
                    # gelirse (ör. bozuk/legacy format) 3. ve sonraki hücreler Kanıt ve Analiz
                    # olarak BİRLEŞTİRİLİR — row[:3] gibi sessizce ATILMAZ (trailing kanıt kaybı).
                    if len(row) > 3:
                        clean_rows.append([row[0], row[1], " ".join(c for c in row[2:] if c)])
                    else:
                        clean_rows.append(row)
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

    def _render_score_table():
        # İş emri madde 6 — Değerlendirme Puanları: TAMAMEN deterministik, LLM üretmez.
        s_pos, s_prof = interview.get("score_position"), interview.get("score_profile")
        r_pos, r_prof = interview.get("reviewer_score_position"), interview.get("reviewer_score_profile")
        if s_pos is None and s_prof is None:
            return
        def _c(v):
            return f"{v}/100" if v is not None else ""
        story.append(Paragraph("Değerlendirme Puanları", styles["Section"]))
        rows = [["Değerlendirici", "Pozisyon Yetkinliği", "Kişisel ve Bilişsel Profil"],
                ["Birinci", _c(s_pos), _c(s_prof)]]
        has_second = r_pos is not None or r_prof is not None
        if has_second:
            rows.append(["İkinci", _c(r_pos), _c(r_prof)])
        # İŞ EMRİ — L3 İKİNCİ DEĞERLENDİRME TUTARLILIĞI + SOURCE VISIBILITY / madde 6: PDF'in
        # yapısal "Değerlendirme Puanları" tablosundan görsel "Nihai" satırı KALDIRILDI (yalnız bu
        # tablo satırı — final_score_position/final_score_profile DB alanları, hesaplanmaları ve
        # rapor METNİNDEKİ ("Öneri Gerekçesi" → "Nihai Pozisyon/Profil Puanı") canonical ifadeleri
        # AYNEN KALIYOR, DEĞİŞMEDİ). Tablo artık yalnız Birinci / İkinci / Genel Puan gösterir.
        genel_idx = len(rows)
        rows.append(["Genel Puan", (f"{score}/100" if score is not None else ""), ""])
        st = Table([[Paragraph(ptxt(c), styles["BodyWrap"]) for c in row] for row in rows],
                   colWidths=[5.5*cm, 5.65*cm, 5.65*cm])
        st.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), rl_colors.HexColor("#eff6ff")),
            ("FONTNAME", (0,0), (-1,0), font_bold),
            ("FONTNAME", (0,genel_idx), (0,genel_idx), font_bold),
            ("SPAN", (1,genel_idx), (2,genel_idx)),
            ("ALIGN", (1,genel_idx), (2,genel_idx), "CENTER"),
            ("GRID", (0,0), (-1,-1), 0.3, rl_colors.HexColor("#dbeafe")),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
            ("TOPPADDING", (0,0), (-1,-1), 6),
            ("BOTTOMPADDING", (0,0), (-1,-1), 6),
        ]))
        story.append(st)
        story.append(Spacer(1, 4))

    # ============ RAPOR GÖVDESİ (iş emri madde 3) — yalnız gerçekten üretilen bölümler ============
    # Savunma amaçlı: reviewer henüz çalışmadıysa (arka plan işi bitmeden PDF istenirse) yer
    # tutucu asla görünmez.
    _report_raw = (interview.get("report") or "").replace(_REVIEWER_SLOT_MARK, "")
    report_text = strip_markdown(_insert_heading_breaks(_report_raw)) or "Rapor bulunamadı."
    lines = [ln.rstrip() for ln in report_text.split("\n") if ln.strip()]
    secs = _split_report_sections(lines)

    if not any(k in secs for k in _KNOWN_REPORT_HEADINGS):
        # Tanınan hiçbir bölüm başlığı yok (yedek/eski biçim rapor) — ham blok olarak bas.
        story.append(Paragraph("Değerlendirme Raporu", styles["Section"]))
        _emit_report_block(lines)
    else:
        if secs.get(None):
            _emit_report_block(secs[None])
        if "Yönetici Özeti" in secs:
            story.append(Paragraph("Yönetici Özeti", styles["Section"]))
            _emit_report_block(secs["Yönetici Özeti"])
        # İş emri — RAPOR ANLATI KATMANI GERİ EKLEME (2026-09) / ADIM 1 KÖK NEDEN, ADIM 2 DÜZELTME:
        # bu döngü ("Pozisyon Yetkinlikleri", ...) ile _KNOWN_REPORT_HEADINGS (_split_report_sections'ın
        # kullandığı, GÖREV1 turunda 10 yeni başlıkla güncellenen liste) İKİ AYRI, BAĞIMSIZ liste idi.
        # _split_report_sections yeni başlıkları DOĞRU ayırıyordu (secs sözlüğünde vardı) ama BU DÖNGÜ
        # onları hiç ZİYARET ETMİYORDU — içerik VARDI, PDF'e hiç YAZILMIYORDU (kanıt: Profil Veto
        # Kontrolü, "Kişisel ve Bilişsel Profil" bloğunun İÇİNE gömülü olduğu için basılıyordu, ama
        # bağımsız "Puanlama Kapsamı" gibi başlıklar hiç görünmüyordu — aynı finalize_interview
        # çalıştırmasında biri basılıp diğerinin basılmaması bu ayrımı KANITLADI). Mevcut 30 test bunu
        # YAKALAMADI çünkü hepsi `interviews.report` (finalize_interview'ın yazdığı DB metni) OKUYORDU,
        # PDF'i HİÇ ÜRETMİYORDU — bu döngü yalnız _make_report_pdf İÇİNDE, ayrı bir kod yolu.
        # ADIM 3 koruma testiyle bulundu (bu turda): başlık metni Paragraph'a DOĞRUDAN veriliyordu,
        # ptxt() ÜZERİNDEN GEÇMİYORDU — "CV ↔ Mülakat ↔ Pozisyon Uyumu" başlığındaki ↔ karakteri
        # Vera yedek fontunda (DejaVu/Noto bulunamayan ortamlarda, bkz. register_unicode_font)
        # glif karşılığı olmadığı için sessizce .notdef'e düşüyor, PDF metninde \x00 olarak çıkıyordu
        # (pdfminer ile doğrulandı) — gövde metninde zaten var olan ptxt() ok-karakteri yedeğini
        # başlıklara da uygula.
        for _head in ("Puanlama Kapsamı", "Analitik Düşünme ve Muhakeme", "Problem Çözme ve Karar Verme Yaklaşımı",
                     "Kavrama ve İletişim", "Tutarlılık / Çelişki Analizi", "Öne Çıkan Proje ve Deneyimler",
                     "CV ↔ Mülakat ↔ Pozisyon Uyumu", "Değerlendirilemeyen Alanlar", "Dil Gözlemi", "Genel Kanı"):
            if _head in secs:
                story.append(Paragraph(ptxt(_head), styles["Section"]))
                _emit_report_block(secs[_head])
        _render_score_table()
        for _head in ("Pozisyon Yetkinlikleri", "Kişisel ve Bilişsel Profil", "İkinci Değerlendirici Görüşü",
                     "Güçlü Yönler", "Gelişim Alanları", "Görüntü ve Ses Gözlemi", "CV Özeti",
                     "Beyan Tutarlılığı", "Öneri Gerekçesi", "Takip Mülakatı İçin Önerilen Sorular"):
            if _head in secs:
                story.append(Paragraph(ptxt(_head), styles["Section"]))
                _emit_report_block(secs[_head])

    # ============ Metodoloji Notu (iş emri madde 16) — ana raporun sonu, DETERMİNİSTİK ============
    if score is not None:
        story.append(Spacer(1, 8))
        story.append(Paragraph("Metodoloji Notu", styles["Section"]))
        _has_second = interview.get("reviewer_score_position") is not None or interview.get("reviewer_score_profile") is not None
        _mn = ["Bu rapor iki bağımsız yapay zekâ değerlendirmesinden üretilmiştir." if _has_second else
              "Bu rapor bir birincil yapay zekâ değerlendirmesinden üretilmiştir; ikinci (bağımsız) değerlendirici bu mülakat için çalışmamıştır.",
              "Genel Puan, mevcut puanların eşit ağırlıklı ortalamasıdır.",
              "Görüntü ve ses gözlemleri niteliksel bilgi amaçlıdır, puana dahil edilmez.",
              "Bu rapor bir karar desteği aracıdır; tek başına işe alım kararı yerine geçmez."]
        story.append(Paragraph(" ".join(_mn), styles["BodyWrap"]))

    # ============ EKLER (iş emri madde 17) — yeni sayfada, DİNAMİK numaralandırma ============
    _ek_state = {"n": 0}
    def _next_ek():
        _ek_state["n"] += 1
        return _ek_state["n"]

    try:
        _tview = build_transcript_view(interview.get("messages") or "[]", interview.get("level") or 1, interview.get("started_at"), for_report=True)
    except Exception as e:
        print(f"UYARI (PDF transkript görünümü): {type(e).__name__}: {e}")
        _tview = []
    _lvl = interview.get("level") or 1
    _want = VERIFICATION_FRAME_COUNT.get(_lvl, 4)
    _has_camera = _lvl != 1 and _want > 0 and bool(snapshots)
    _annex = (interview.get("technical_annex") or "").strip()

    if _has_camera or _tview or _annex:
        story.append(PageBreak())
        story.append(Paragraph("Ekler", styles["Section"]))

    if _has_camera:
        # İŞ EMRİ — RAPORLAMA VE PRIMARY DEĞERLENDİRME TUTARLILIĞI / madde 5 (düzeltildi): başlık
        # önceden HER ZAMAN "mülakat süresine yayılmış" diyordu — mevcut dar-aralık sinyali
        # (compute_modality_coverage → _frame_distribution'ın "kumelenme" bayrağı, main.py ~12475)
        # kontrol edilmeden. Yeni bir dağılım analizi YAZILMADI — yalnız bu MEVCUT sinyal okunup
        # başlık ona göre seçiliyor; sinyal yoksa/hesaplanamazsa eski (varsayılan) ifade korunur.
        _kaynak_ifadesi = "mimik havuzundan, mülakat süresine yayılmış"
        try:
            _dogrulama_dagilim = compute_modality_coverage(candidate.get("id"), _lvl).get("dogrulama") or {}
            if _dogrulama_dagilim.get("kumelenme"):
                _kaynak_ifadesi = "mimik havuzundan, dar bir zaman aralığında kümelenmiş"
        except Exception as e:
            print(f"UYARI (PDF kamera başlığı dar-aralık kontrolü): {type(e).__name__}: {e}")
        story.append(Paragraph(f"EK {_next_ek()} — Kamera Doğrulama Kareleri ({len(snapshots[:_want])}/{_want} — {_kaynak_ifadesi})", styles["Section"]))
        rows, row = [], []
        for idx, snap in enumerate(snapshots[:_want], start=1):
            try:
                data_url = snap.get("image_base64", "")
                raw = data_url.split(",", 1)[1] if "," in data_url else data_url
                img_bytes = base64.b64decode(raw)
                img = Image(io.BytesIO(img_bytes), width=7.4*cm, height=5.4*cm)
                _ems = _safe_int(snap.get("elapsed_ms"))
                _mmss = f" · {_ems // 60000}:{(_ems // 1000) % 60:02d}. dk" if _ems else ""
                cell = [Paragraph(f"<b>Kare {idx}</b>{_mmss}<br/><font size=7>{ptxt(format_pdf_datetime(snap.get('captured_at')))}</font>", styles["Small"]), img]
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
        story.append(PageBreak())

    if _tview:
        story.append(Paragraph(f"EK {_next_ek()} — Konuşma Metni ({len(_tview)} satır)", styles["Section"]))
        for row in _tview:
            # "Konumu belirlenemeyen satırlar" başlığı bir konuşmacı SATIRI DEĞİL; kendi bölüm
            # başlığı olarak, konuşmacı etiketi/damga OLMADAN basılır.
            if row["role"] == "baslik":
                story.append(Spacer(1, 6))
                story.append(Paragraph(f"<b>{ptxt(row['text'])}</b>", styles["BodyWrap"]))
                continue
            who = "Aday" if row["role"] == "aday" else "Mülakatçı"
            stamp = f"[{row['ts']}] " if row.get("ts") else ""
            story.append(Paragraph(f"<font size=7 color='#64748b'>{ptxt(stamp)}</font><b>{who}:</b> {ptxt(row['text'])}", styles["BodyWrap"]))
            story.append(Spacer(1, 2))
        if _annex:
            story.append(PageBreak())

    if _annex:
        story.append(Paragraph(f"EK {_next_ek()} — Teknik Veriler (yalnızca yönetici)", styles["Section"]))
        for _ln in _annex.split("\n"):
            _ln = _ln.strip()
            if not _ln:
                continue
            _is_h = _ln.endswith(":") or _ln.startswith("**")
            story.append(Paragraph(("<b>" + ptxt(_ln.replace("**", "")) + "</b>") if _is_h else f"<font size=8>{ptxt(_ln)}</font>",
                                   styles["Small"] if not _is_h else styles["BodyWrap"]))
            story.append(Spacer(1, 2))

    # KALEM 5 — teknik not (yalnız yönetici PDF'i): token kesilmesi vb.
    if interview.get("report_tech_note"):
        story.append(Spacer(1, 8))
        story.append(Paragraph(f"<font size=7>Teknik not (yalnızca yönetici): {ptxt(interview.get('report_tech_note'))}</font>", styles["Small"]))

    story.append(Spacer(1, 14))
    # KALEM 4 — mülakat tarihi/saati (started_at–completed_at) ile RAPOR üretim tarihi ayrı satırlar.
    # İŞ EMRİ — RAPORLAMA VE PRIMARY DEĞERLENDİRME TUTARLILIĞI / madde 4 (düzeltildi): bu satır
    # önceden datetime.now() (PDF'in İNDİRİLDİĞİ an, sunucu saatiyle/UTC — Europe/Istanbul DEĞİL)
    # kullanıyordu ve _gen_txt de format_pdf_datetime() ile HAM (UTC, +3 çevrilmeMİş) saat
    # basıyordu — üstteki "Rapor Oluşturulma Tarihi" (bu fonksiyonun başında _report_created_at
    # olarak, +3 ile Europe/Istanbul'a çevrilerek hesaplanan AYNI DB alanı) ile PDF'in alt kısmı
    # farklı saat/farklı kaynak gösterebiliyordu. Artık ikisi TEK KAYNAKTAN (_report_created_at,
    # _report_created_raw ile AYNI öncelik: report_regenerated_at varsa o, yoksa report_generated_at)
    # ve AYNI Europe/Istanbul dönüşümünden üretiliyor — üst ve alt HER ZAMAN aynı anı gösterir.
    _gen_txt = f" (rapor {_report_created_at} tarihinde üretildi)" if _report_created_at else ""
    story.append(Paragraph(f"Bu rapor MedeX AI Interview Platform tarafından oluşturulmuştur.{_gen_txt}", styles["Small"]))

    # İş emri madde 19 — her sayfada sayfa numarası.
    def _add_page_number(canvas, _doc):
        canvas.saveState()
        canvas.setFont(font_regular, 8)
        canvas.setFillColor(rl_colors.HexColor("#94a3b8"))
        canvas.drawRightString(A4[0] - 1.4*cm, 0.6*cm, f"Sayfa {_doc.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_add_page_number, onLaterPages=_add_page_number)
    buffer.seek(0)
    return buffer

@app.get("/api/admin/interviews/{candidate_id}/pdf")
def download_interview_pdf(candidate_id: int, level: Optional[int] = None, payload=Depends(verify_admin), db=Depends(db_dep)):
    scoped_org_id = get_org_id_for_admin(db, payload)
    candidate = db.execute("SELECT * FROM candidates WHERE id=? AND org_id=?", (candidate_id, scoped_org_id)).fetchone()
    target_level = level if level is not None else ((candidate["level"] or 1) if candidate else 1)
    interview = db.execute("SELECT * FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, target_level)).fetchone()
    # GÖREV 3 — doğrulama kareleri: mimik havuzundan, süreye yayılmış, seviye bazlı (L1=0, L2=4, L3=6)
    snapshots = select_verification_frames(candidate_id, target_level)
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

    # TUR 4 / GÖREV 2 — YENİDEN ÜRETİM HER ZAMAN HAM (write-once) transkriptten başlar, bir
    # önceki yeniden-üretimin ÇIKTISINDAN değil. Aksi halde her regenerate bir öncekinin
    # düzeltme hatalarını üstüne katlayarak büyütür (kök neden — bkz. GÖREV 2 notu). Ham veri
    # yoksa (ilk çağrı) get_transcript_raw mevcut messages'tan BİR KEZ geriye dönük doldurur.
    raw_transcript = get_transcript_raw(candidate_id, level)
    if not raw_transcript or len(raw_transcript.strip()) < 40:
        raise HTTPException(status_code=400, detail="Kayıtlı transkript yok veya rapor üretmek için çok kısa")

    _lang = cand["interview_language"] or "tr"
    _regen_notes = []

    # TUR 2 / GÖREV C.4 — YENİDEN ÜRETİM YOLU da TAM transkript temizleyiciden geçer:
    # konuşmacı etiketi + hoparlör yankısı + iç talimat sızıntısı + zaman damgası + halüsinasyon.
    # Eskiden burada YALNIZCA halüsinasyon filtresi vardı; Kader'in ham (etiketleri karışık)
    # transkripti bu yüzden raporda bozuk basılıyordu.
    clean_transcript, spk_changes, hall_filtered, hall_n = normalize_transcript_for_report(raw_transcript, _lang)
    _transcript_changed = (clean_transcript.strip() != (raw_transcript or "").strip())
    if spk_changes:
        _tips = sorted({c["tip"] for c in spk_changes})
        _regen_notes.append(f"{len(spk_changes)} transkript satırı düzeltildi/kaldırıldı (konuşmacı etiketi / yankı / iç talimat sızıntısı / zaman damgası).")
        record_realtime_events(candidate_id, level,
                               [{"type": "transcript_speaker_fix", "data": ch, "elapsed_ms": _safe_int(ch.get("ts")) * 1000 if ch.get("ts") else 0} for ch in spk_changes])
        print(f"[TRANSCRIPT_FIX regenerate] c={candidate_id} {len(spk_changes)} satır: " + ", ".join(_tips))
    if hall_n:
        _regen_notes.append(f"{hall_n} aday satırı olası transkripsiyon halüsinasyonu olarak işaretlendi (bunlar aday cevabı sayılmadı).")
        _dedup_filter_events(candidate_id, level, hall_filtered)
    if _transcript_changed:
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
            # İş emri — RAPOR ANLATI KATMANI GERİ EKLEME / ADIM 2 ("Örnekten taşınmayacak kusurlar" —
            # iç debug metni müşteri raporuna girmeyecek): eski metin "(rapor yeniden üretiminde
            # düzeltildi: önceki 'erken sonlandırma' tespiti hatalıydı)" iç SÜREÇ dilinde yazılmıştı
            # ve müşteriye giden "Sonuç Gerekçesi / İhlal Kaydı" bölümünde AYNEN basılıyordu
            # (Murat AYZİT raporunda kanıtlandı). Müşteriye giden cümle artık yalnız SONUCU söyler;
            # düzeltme sürecinin kendisi (iç bilgi) system_decision'a ayrıca loglanır.
            try:
                db.execute("UPDATE interviews SET result_events_json=?, result_reason=?, partial=0 WHERE candidate_id=? AND level=?",
                           (json.dumps(_events, ensure_ascii=False)[:12000],
                            "Mülakat normal şekilde tamamlanmıştır.",
                            candidate_id, level))
                db.commit()
                record_system_decision(candidate_id, level, "erken_sonlandirma_tespiti_geri_alindi",
                                       "Yönetici talebiyle yeniden üretimde önceki 'erken sonlandırma' tespiti hatalı bulundu ve geri alındı (iç kayıt — müşteri raporuna girmez).",
                                       {})
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
        # İŞ EMRİ — FINAL EVALUATION ARCHITECTURE: L1 birincil DEĞERLENDİRME/RAPOR artık OpenAI
        # (system+prompt biçimi DEĞİŞMEDİ — yalnız provider/model OpenAI'ye taşındı; OpenAI dalı
        # artık 'system' alanını da gönderiyor, bkz. run_deferred_finish_job).
        if not OPENAI_API_KEY:
            raise HTTPException(status_code=503, detail="OPENAI_API_KEY tanımlı değil")
        system = get_system_prompt(cand["position"], cand["name"], cand["cv_text"], cand["ai_note"],
                                   cand["education"], cand["university"], cand["department"], cand["experience_years"],
                                   level, cand["interview_language"] or "tr", cand["report_language"] or "tr",
                                   (cand["depth_tier"] if "depth_tier" in cand.keys() else "standart") or "standart",
                                   email=(cand["email"] if "email" in cand.keys() else None))
        prompt = (f"GÖREV: Aşağıdaki tam transkriptten mülakatı bitir ve raporu üret (yönetici talebiyle YENİDEN üretim). "
                  f"Elindeki veriyle adil değerlendir; sorulmamış kriterleri 'değerlendirilemedi' işaretle. [MÜLAKATBİTTİ] etiketini kullan.{_regen_note_txt}\n\n"
                  f"=== TAM TRANSKRİPT ===\n{clean_transcript[:TRANSCRIPT_PROMPT_MAX_CHARS]}")
        prov, mdl = "openai", OPENAI_REPORT_MODEL

    # EK — completed_at (orijinal bitiş saati) EZİLMEZ. run_deferred_finish_job regen=True ile
    # guard'ı atlar; finalize_interview regen=True completed_at'e dokunmaz, report_regenerated_at yazar.
    _term = None if corrected_end_reason else (cand["terminated_reason"] if "terminated_reason" in cand.keys() else None)
    _job_id = _mark_finish_pending(candidate_id, level, provider=prov, model=mdl, system=system, payload=prompt,
                                   terminated_reason=_term, reason="admin_regenerate")
    # İŞ EMRİ madde I — bu, ÇİFT admin-tetikli regenerate'in klasik race'idir (ör. iki kez tıklama):
    # zaten aktif bir işlem varsa açıkça 409 döner — sessizce hiçbir şey yapıp "başladı" YALANI
    # söylenmez, mevcut işlem de bozulmaz/tekrarlanmaz.
    if not _job_id:
        raise HTTPException(status_code=409, detail={
            "message": "Bu aday/seviye için zaten devam eden bir rapor işlemi var. Lütfen tamamlanmasını bekleyin.",
            "error_class": "report_job_already_active", "retryable": False,
        })
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

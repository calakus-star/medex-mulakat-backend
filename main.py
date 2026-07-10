from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Any
import sqlite3
import hashlib
import secrets
import string
import os
import jwt
import anthropic
import httpx
import resend
from datetime import datetime, timedelta
import json
import re
import io
import base64
from xml.sax.saxutils import escape as xml_escape

app = FastAPI(title="MedeX Mülakat Sistemi")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============ CONFIG ============
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")  # Sesli mod (Whisper STT + TTS, L2 Realtime) için, Anthropic'ten bağımsız
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1-mini")
OPENAI_REALTIME_VOICE = os.getenv("OPENAI_REALTIME_VOICE", "marin")  # Doğal ses: Railway env ile değiştirilebilir (örn. marin/verse)
OPENAI_REPORT_MODEL = os.getenv("OPENAI_REPORT_MODEL", "gpt-4o")  # L2 kaliteli OpenAI raporu; env ile değiştirilebilir

def log_ai_provider(level: int, provider: str, action: str):
    """Görev dokümanı zorunluluğu: L2'de Claude çağrısı yapılmadığını denetlenebilir kılmak için."""
    print(f"[AI_PROVIDER] level=L{level} provider={provider} action={action}")
JWT_SECRET = os.getenv("JWT_SECRET", "medex-secret-key-2024")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@medex-smo.com")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "medex2024")
REPORT_EMAILS = os.getenv("REPORT_EMAILS", "hr@medex-smo.com").split(",")
FROM_EMAIL = os.getenv("FROM_EMAIL", "onboarding@resend.dev")
BASE_URL = os.getenv("BASE_URL", "http://localhost:3000")

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
DB_PATH = os.getenv("DB_PATH", "medex_mulakat.db")  # Railway'de kalıcı Volume'a işaret etsin (örn. /data/medex_mulakat.db)

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
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
            previous_candidate_id INTEGER,
            is_archived INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            violation_count INTEGER DEFAULT 0,
            terminated_reason TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS interviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER,
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
            started_at TEXT DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT,
            FOREIGN KEY (candidate_id) REFERENCES candidates(id)
        );

        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER,
            image_base64 TEXT,
            captured_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (candidate_id) REFERENCES candidates(id)
        );

        CREATE TABLE IF NOT EXISTS ai_usage_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER,
            level INTEGER DEFAULT 1,
            provider TEXT,
            model TEXT,
            action TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            audio_input_tokens INTEGER DEFAULT 0,
            audio_output_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            estimated_cost_usd REAL DEFAULT 0,
            raw_json TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (candidate_id) REFERENCES candidates(id)
        );
    """)
    conn.commit()

    # Güvenli migration: eski SQLite dosyası kullanılıyorsa eksik kolonları ekle.
    # SQLite ADD COLUMN mevcut kolonda hata verir; bu hata bilinçli olarak yutulur.
    migrations = [
        ("candidates", "email", "TEXT"),
        ("candidates", "phone", "TEXT"),
        ("candidates", "education", "TEXT"),
        ("candidates", "university", "TEXT"),
        ("candidates", "department", "TEXT"),
        ("candidates", "experience_years", "INTEGER DEFAULT 0"),
        ("candidates", "ai_note", "TEXT"),
        ("candidates", "plain_password", "TEXT"),
        ("candidates", "cv_text", "TEXT"),
        ("candidates", "cv_filename", "TEXT"),
        ("candidates", "violation_count", "INTEGER DEFAULT 0"),
        ("candidates", "terminated_reason", "TEXT"),
        ("candidates", "reapply_allowed", "INTEGER DEFAULT 0"),
        ("candidates", "previous_candidate_id", "INTEGER"),
        ("candidates", "is_archived", "INTEGER DEFAULT 0"),
        ("candidates", "level", "INTEGER DEFAULT 1"),
        ("candidates", "depth_tier", "TEXT DEFAULT 'standart'"),
        ("candidates", "interview_language", "TEXT DEFAULT 'tr'"),
        ("candidates", "report_language", "TEXT DEFAULT 'tr'"),
        ("interviews", "level", "INTEGER DEFAULT 1"),
        ("interviews", "closing_asked", "INTEGER DEFAULT 0"),
        ("interviews", "total_input_tokens", "INTEGER DEFAULT 0"),
        ("interviews", "total_output_tokens", "INTEGER DEFAULT 0"),
        ("positions", "category", "TEXT DEFAULT 'Genel'"),
        ("interviews", "standard_cv", "TEXT"),
        ("interviews", "compact_memory", "TEXT DEFAULT ''"),
        ("interviews", "question_count", "INTEGER DEFAULT 0"),
        ("interviews", "depth_tier", "TEXT DEFAULT 'standart'"),
        ("ai_usage_logs", "estimated_cost_usd", "REAL DEFAULT 0"),
    ]
    for table, column, definition in migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        except sqlite3.OperationalError:
            pass
    conn.commit()

    def infer_position_category(name: str) -> str:
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
        if any(k in n for k in ["finance", "accountant", "accounting"]):
            return "Finans"
        if any(k in n for k in ["sales", "marketing", "customer", "product specialist", "representative"]):
            return "Satış & Pazarlama"
        if any(k in n for k in ["kasa", "kasiyer", "retail", "mağaza", "magaza"]):
            return "Perakende & Operasyon"
        return "Genel"

    # Varsayılan pozisyonları her açılışta eksikse ekle.
    # Not: Mevcut pozisyonları bozmaz; sadece isim bazlı eksik olanları tamamlar.
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
        ("Project Manager", "Proje planlama, ekip koordinasyonu, risk, zaman ve paydaş yönetiminden sorumlu rol.", [("Planlama",25,"Kapsam (scope), timeline ve kaynak planı oluşturma (Gantt chart vb.)"),("Risk Yönetimi",20,"Risk kaydı (risk register), issue takibi ve aksiyon planı"),("İletişim",20,"Paydaş ve ekip ile düzenli status raporlama"),("Liderlik",15,"Ekip motivasyonu, önceliklendirme ve karar alma"),("Bütçe",10,"Maliyet takibi ve kaynak optimizasyonu"),("Araç Kullanımı",10,"Jira, MS Project veya benzeri araçlarla iş takibi")]),
        ("Product Manager", "Ürün vizyonu, roadmap, kullanıcı ihtiyacı ve iş önceliklendirme rolü.", [("Ürün Stratejisi",25,"Ürün vizyonu ve roadmap önceliklendirme (RICE, MoSCoW vb.)"),("Kullanıcı Anlayışı",20,"Kullanıcı araştırması, UX testleri ve ihtiyaç analizi"),("Analitik",15,"Metric/funnel analizi (conversion rate vb.) ile veri odaklı karar"),("Teknik İletişim",15,"Geliştirici ekiple teknik kısıt ve önceliklendirme uyumu"),("Stakeholder Yönetimi",15,"İş birimleri ve yönetimle beklenti yönetimi"),("Problem Çözme",10,"Trade-off analizleri ve önceliklendirme kararları")]),
        ("Business Analyst", "İş gereksinimlerini analiz eden, süreç modelleyen ve teknik ekibe aktaran rol.", [("Gereksinim Yönetimi",20,"İşletme ihtiyaçlarını doğru toplama ve belgeleme"),("Süreç Modelleme",15,"Akış şemaları ve senaryolar oluşturma (UML, BPMN)"),("Veri Analitiği",15,"Verileri yorumlama ve trend çıkarma; SQL, Tableau veya PowerBI gibi araçlara hakimiyet"),("Çevik (Agile) Metodolojiler",15,"Scrum ve Kanban süreçlerinde aktif rol alma, kullanıcı hikayeleri (user story) yazma"),("Paydaş Yönetimi",15,"Müşteriler ve geliştirici ekipler arasında net dil kullanma ve koordinasyon"),("Müzakere ve Problem Çözme",10,"Çatışma yönetimi ve karmaşık iş problemlerine rasyonel çözüm üretme"),("Sunum Becerileri",10,"Teknik olmayan yöneticilere analiz ve iş değerini net şekilde aktarma")]),
        ("HR Specialist", "İşe alım, çalışan ilişkileri, eğitim, performans ve insan kaynakları operasyonları rolü.", [("İşe Alım",25,"Aday tarama, mülakat süreci tasarımı ve işe alım metrikleri (time-to-hire vb.)"),("İletişim",20,"Çalışan ve yönetici arasında net ve empatik iletişim"),("Organizasyon",15,"Özlük dosyası, süreç takibi ve dokümantasyon"),("Mevzuat & Uyum",15,"İş Kanunu ve şirket politikalarına hakimiyet"),("Analitik",10,"HR metrikleri (turnover, engagement) analizi"),("Gizlilik",15,"KVKK kapsamında çalışan verisi gizliliği ve etik yaklaşım")]),
        ("Finance Specialist", "Finansal kayıt, raporlama, bütçe, ödeme ve mali kontrol süreçlerinden sorumlu rol.", [("Finansal Bilgi",25,"Muhasebe kayıtları, bütçe hazırlığı ve finansal raporlama"),("Dikkat & Doğruluk",25,"Fatura/ödeme kontrolü, hata önleme ve mutabakat"),("Analitik",20,"Finansal veri analizi ve trend yorumlama"),("Araç Kullanımı",10,"Excel (pivot, formül) ve ERP (SAP, Logo, Odoo vb.) kullanımı"),("Uyum",10,"Vergi mevzuatı ve iç kontrol standartlarına uyum"),("İletişim",10,"Ekip ve yönetimle finansal durum raporlama")]),
        ("Sales Manager", "Satış hedefleri, ekip, müşteri ilişkileri ve gelir büyümesinden sorumlu rol.", [("Satış Stratejisi",25,"Hedef belirleme, segment analizi ve pipeline yönetimi"),("Ekip Yönetimi",20,"Satış ekibi koçluğu ve performans değerlendirmesi"),("Müşteri İlişkileri",20,"Güven inşası, müzakere ve müşteri sorunu çözümü"),("Analitik",15,"CRM verisi (Salesforce, HubSpot vb.), forecast ve KPI takibi"),("Sonuç Odaklılık",10,"Satış hedefine yönelik aksiyon planı takibi"),("İletişim",10,"Sunum ve ikna becerisiyle müşteri/ekip yönetimi")]),
        ("Marketing Manager", "Pazarlama stratejisi, kampanya, marka, içerik ve performans yönetimi rolü.", [("Strateji",25,"Pazar analizi, hedef kitle segmentasyonu ve konumlandırma"),("Kampanya Yönetimi",20,"Kampanya planlama, uygulama ve optimizasyon"),("Dijital Pazarlama",15,"SEO, ads (Google/Meta) ve sosyal medya yönetimi"),("Analitik",15,"Metric (CTR, ROI) analizi ve raporlama (Google Analytics vb.)"),("Yaratıcılık",15,"İçerik ve mesaj stratejisi geliştirme"),("İletişim",10,"Ekip ve ajans koordinasyonu")]),
        ("Kasa Yöneticisi", "Kasa operasyonlarını, nakit akışını ve kasa personelini yöneten; günlük/haftalık kasa mutabakatı ile veri güvenliğinden sorumlu rol.", [("Finansal Okuryazarlık & Nakit Yönetimi",25,"Nakit akışı takibi, kasa mutabakatı, kasa açığı/fazlası kontrolü"),("Dikkat & Doğruluk",20,"Kasa sayımı, veri girişi ve işlem hatasını önleme"),("Sorumluluk & Güvenilirlik",15,"İşletmenin nakit varlığını yönetme ve veri güvenliği"),("Ekip & Vardiya Yönetimi",15,"Yoğun temoda vardiya planlama ve kasa personelini yönlendirme"),("Teknoloji Hakimiyeti",15,"MS Office (özellikle Excel) ve ERP/POS sistemleri kullanımı"),("İletişim & Müşteri İlişkileri",10,"Müşteri memnuniyeti ve ödeme sorunu çözümü")]),
    ]
    for name, desc, criteria_pairs in defaults:
        criteria = [{"name": n, "weight": w, "desc": d} for n, w, d in criteria_pairs]
        category = infer_position_category(name)
        conn.execute(
            "INSERT OR IGNORE INTO positions (name, category, role_description, criteria_json) VALUES (?, ?, ?, ?)",
            (name, category, desc, json.dumps(criteria, ensure_ascii=False))
        )
        conn.execute("UPDATE positions SET category=? WHERE name=? AND (category IS NULL OR category='' OR category='Genel')", (category, name))
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
        conn.execute("UPDATE positions SET criteria_json=? WHERE name=?", (forced_json, name))
    conn.commit()
    conn.close()

init_db()

# ============ MODELS ============
class AdminLogin(BaseModel):
    email: str
    password: str

class CriterionItem(BaseModel):
    name: str
    weight: int
    desc: str = ""

class PositionCreate(BaseModel):
    name: str
    category: str = "Genel"
    role_description: str = ""
    criteria: List[CriterionItem]

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

class SnapshotData(BaseModel):
    candidate_id: int
    image_base64: str
    reason: Optional[str] = None

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

def get_position(name: str, db=None):
    close = False
    if db is None:
        db = get_db(); close = True
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
    except Exception:
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



def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0

# Yaklaşık USD fiyatlandırma (1M token başına). Sadece log/panel için kaba maliyet tahmini içindir,
# fatura değildir — gerçek fiyatlar değişirse burası güncellenmeli.
AI_PRICING_PER_1M = {
    # Resmî rate-card: text ve audio ayrı ücretlenir.
    ("openai", "gpt-realtime-2.1"):      {"input": 4.0, "output": 24.0, "audio_input": 32.0, "audio_output": 64.0},
    ("openai", "gpt-realtime-2.1-mini"): {"input": 0.6, "output": 2.4,  "audio_input": 10.0, "audio_output": 20.0},
    # Eski env kullanan kurulumlar için geriye uyumlu yaklaşık kayıt.
    ("openai", "gpt-realtime-2"):        {"input": 4.0, "output": 24.0, "audio_input": 32.0, "audio_output": 64.0},
    ("openai", "gpt-4o"):                {"input": 2.5, "output": 10.0, "audio_input": 0.0, "audio_output": 0.0},
    ("anthropic", "claude-sonnet-4-6"): {"input": 3.0, "output": 15.0, "audio_input": 3.0, "audio_output": 15.0},
}

def _estimate_cost_usd(provider: str, model: str, input_tokens: int, output_tokens: int,
                        audio_input_tokens: int, audio_output_tokens: int) -> float:
    rates = AI_PRICING_PER_1M.get((provider, model))
    if not rates:
        return 0.0
    return round(
        (input_tokens * rates["input"] + output_tokens * rates["output"] +
         audio_input_tokens * rates["audio_input"] + audio_output_tokens * rates["audio_output"]) / 1_000_000,
        4
    )

def record_ai_usage(candidate_id: int, level: int, provider: str, model: str, action: str,
                    input_tokens: int = 0, output_tokens: int = 0,
                    audio_input_tokens: int = 0, audio_output_tokens: int = 0,
                    raw: Optional[Any] = None):
    """Her AI çağrısını/mülakat realtime kullanımını ayrı satır olarak kaydeder.
    Amaç: Her mülakat sonunda hangi işlem kaç token kullanmış net görülsün.
    Hata olursa mülakat akışını bozmaz.
    NOT: audio token'lar metin token'larından ~6-13x daha pahalı olduğu için ayrı tutuluyor;
    tabloda/panelde "toplam token" tek başına maliyeti temsil etmez, bu yüzden estimated_cost_usd de kaydediliyor."""
    try:
        input_tokens = _safe_int(input_tokens)
        output_tokens = _safe_int(output_tokens)
        audio_input_tokens = _safe_int(audio_input_tokens)
        audio_output_tokens = _safe_int(audio_output_tokens)
        total_tokens = input_tokens + output_tokens + audio_input_tokens + audio_output_tokens
        cost_usd = _estimate_cost_usd(provider, model, input_tokens, output_tokens, audio_input_tokens, audio_output_tokens)
        db = get_db()
        db.execute("""
            INSERT INTO ai_usage_logs
            (candidate_id, level, provider, model, action, input_tokens, output_tokens, audio_input_tokens, audio_output_tokens, total_tokens, estimated_cost_usd, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (candidate_id, level, provider, model, action, input_tokens, output_tokens, audio_input_tokens, audio_output_tokens, total_tokens, cost_usd,
              json.dumps(raw or {}, ensure_ascii=False)[:8000]))
        db.execute("""
            UPDATE interviews
            SET total_input_tokens = total_input_tokens + ?,
                total_output_tokens = total_output_tokens + ?
            WHERE candidate_id=? AND level=?
        """, (input_tokens + audio_input_tokens, output_tokens + audio_output_tokens, candidate_id, level))
        db.commit(); db.close()
        print(f"[AI_USAGE] c={candidate_id} L{level} {provider}/{model} {action} in={input_tokens} out={output_tokens} audio_in={audio_input_tokens} audio_out={audio_output_tokens} total={total_tokens} ~${cost_usd}")
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
        raw=summary
    )

def save_interview_state(db, candidate_id: int, messages: list, level: int = 1):
    compact = build_compact_memory(messages)
    q_count = sum(1 for m in messages if m.get("role") == "assistant" and "---RAPOR---" not in (m.get("content") or ""))
    db.execute(
        "UPDATE interviews SET messages=?, compact_memory=?, question_count=? WHERE candidate_id=? AND level=?",
        (json.dumps(messages, ensure_ascii=False), compact, q_count, candidate_id, level)
    )

# ============ FILE PARSING ============
def extract_text_from_pdf(content: bytes) -> str:
    try:
        import pdfplumber
        text = ""
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                text += (page.extract_text() or "") + "\n"
        return text.strip()
    except Exception as e:
        return f"[PDF okunamadı: {e}]"

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
        resend.Emails.send({
            "from": FROM_EMAIL, "to": email,
            "subject": f"MedeX SMO - {position} Pozisyonu Mülakat Daveti",
            "html": html
        })
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
        resend.Emails.send({
            "from": FROM_EMAIL, "to": REPORT_EMAILS,
            "subject": f"Mülakat Raporu: {candidate_name} - {position}",
            "html": html
        })
        return True
    except Exception as e:
        print(f"Rapor mail hatası: {e}")
        return False

# ============ AI PROMPT ============
def build_l2_realtime_instructions(position_name: str, candidate_name: str, cv_text: Optional[str], ai_note: Optional[str], interview_language: str = "tr", depth_tier: Optional[str] = "standart") -> str:
    """Canlı görüşmede kaliteyi koruyan fakat her turda tekrar işlenen sabit bağlamı küçülten talimat."""
    pos = get_position(position_name) or {"criteria": [{"name": "Genel Yetkinlik", "weight": 100, "desc": ""}]}
    criteria = pos.get("criteria") or []
    criteria_compact = "; ".join(f"{c.get('name','Kriter')} %{c.get('weight',0)}" for c in criteria)
    criteria_names = ", ".join(f'"{c.get("name", "Kriter")}"' for c in criteria)
    lang_name = LANGUAGE_NAMES.get(interview_language, "Türkçe")
    cv_compact = " ".join((cv_text or "").split())[:420] or "CV özeti yok"
    note_compact = " ".join((ai_note or "").split())[:160]
    lvl_cfg = get_effective_level_config(2, depth_tier)

    return f"""Sen MedEx için gerçek bir insan mülakatçısısın. {lang_name} konuş. Aday: {candidate_name}. Pozisyon: {position_name}.
Kriterler: {criteria_compact}. CV özeti: {cv_compact}. Özel not: {note_compact or 'yok'}.

Kurallar:
- Her turda yalnızca BİR kısa, doğal soru sor; aday daha çok konuşsun. Uzun açıklama, ders, danışmanlık, övgü ve tekrar yapma.
- Soruyu CV'ye, son cevaba ve henüz kanıtlanmayan kritere göre seç. Somut örnek, kişisel katkı, sonuç ve ölçüm iste.
- Aday sana bilgi sorarsa cevap öğretme; tek cümleyle soruyu geri çevir: “Bu görüşmede sizin yaklaşımınızı değerlendirmem gerekiyor; siz nasıl açıklarsınız?”
- Adayın yerine cevap verme veya ipucu verme. Mülakatçı sözleri aday başarısı değildir.
- Yüzeysel cevapta örnek; “biz” cevabında kişisel katkı; iddiada ölçülebilir sonuç sor. Gerçek çelişkiyi tarafsız netleştir.
- Aynı bilgiyi tekrar sorma. Bir kriter netleşince diğerine geç. Aday araya girerse sus ve dinle.
- Akış: kısa tanışma → deneyim → teknik/işlevsel yetkinlik → somut olay/sonuç → davranış → motivasyon/uyum → kapanış.
- Hedef yaklaşık {lvl_cfg['minutes']} dakika. Kriterlerin çoğunda yeterli kanıt oluşunca kısa kapanış yap ve end_interview(reason='tamamlandı', criteria_coverage={{...}}) çağır: {criteria_names}.
- Aday bitirmek isterse end_interview(reason='aday_talebi'); tekrarlanan uygunsuz davranışta end_interview(reason='uygunsuz_davranis') çağır. Kapanıştan sonra soru sorma.
"""

def build_criteria_text(criteria: list) -> str:
    lines = []
    for c in criteria:
        lines.append(f"- {c['name']} ({c['weight']} puan): {c.get('desc', '')}")
    return "\n".join(lines)

def build_criteria_table_template(criteria: list) -> str:
    lines = ["| Kriter | Puan | Değerlendirme |", "|--------|------|---------------|"]
    for c in criteria:
        lines.append(f"| {c['name']} | XX/{c['weight']} | ... |")
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

def get_system_prompt(position_name: str, candidate_name: str, cv_text: Optional[str] = None, ai_note: Optional[str] = None, education: Optional[str] = None, university: Optional[str] = None, department: Optional[str] = None, experience_years: Optional[int] = None, level: Optional[int] = 1, interview_language: str = "tr", report_language: str = "tr", depth_tier: Optional[str] = "standart") -> str:
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

    candidate_profile = f"""ADAY PROFİLİ:
Eğitim: {education or '-'}
Üniversite: {university or '-'}
Bölüm: {department or '-'}
Deneyim yılı: {experience_years if experience_years is not None else '-'}
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
    lang_instruction = f"Adayla konuşurken TAMAMEN {interview_lang_name} kullan (selam, sorular, yorumların hepsi {interview_lang_name})."
    if report_language != interview_language:
        lang_instruction += f" AMA mülakat sonundaki RAPOR bloğunu (---RAPOR---'dan itibaren her şey: kriter değerlendirmeleri, analiz, standart CV) mutlaka {report_lang_name} dilinde yaz — rapor dili adayla konuştuğun dilden farklıdır, bu kesin bir kuraldır, karıştırma."

    return f"""Sen MedeX AI mülakat uzmanısın. {lang_instruction} Aday: {candidate_name}. Pozisyon: {position_name}. Kategori: {category}.

TEMEL FELSEFE (her kararında bunu esas al):
Bu mülakatın amacı adayı elemek değil, iyi/yetkin adayı gerçekten yakalamaktır. Sahada güçlü çalışan çoğu insan mülakat ortamında (heyecan, format kafası karışıklığı, soru net değilse ne istendiğini anlamama) düşük performans gösterebilir. Bir cevabı yetersiz sayıp geçmeden önce, bunun gerçek bir yetkinlik eksikliği mi yoksa mülakatın kendi eksikliği (belirsiz soru, ilk denemede tam anlaşılamama) mi olduğunu ayır. Amaç eleme değildir ama gerçek eleme de gerektiğinde yapılır — sadece yanlış nedenle (kısa cevap, ilk seferde anlaşılamama) elemeye düşülmez.

Rol: {pos['role_description']}
Kriterler ({total_weight} puan):
{criteria_text}
{candidate_profile}
{cv_section}
{admin_instruction}

SEVİYE TALİMATI: {lvl_cfg["tone"]}

DERİNLİK SEVİYESİ ({lvl_cfg["depth_label"]}): Bu mülakat için yaklaşık {lvl_cfg["minutes"]} dakika ve en az {lvl_cfg["min_q"]} ana konu/soru bir yön göstergesidir — KESİN bir hedef değil, sadece yaklaşık bir yönlendirmedir. Karar veremiyorsan (bir kriterde hâlâ net sinyal yoksa) bu süreyi/sayıyı aşarak devam et; sabit bir tavanla erken kesme. "{lvl_cfg["depth_label"]}" seviyesinde kalman gerektiği için gereksiz uzatma da yapma — yeterli sinyali aldığın kriterde ısrarla soru sorma.

SORU ÇEŞİTLİLİĞİ (ADAYLAR ARASI): Sabit bir soru script'in yok. Her adayın CV'sinde/profilinde geçen kendine özgü detaylara (spesifik proje adı, sertifika, teknoloji, sektör) göre soruları o adaya özel kur — aynı pozisyon için farklı adaylara neredeyse birebir aynı soruyu, aynı sırayla sorma. Konudan konuya geçiş sırasını ve örnekleri her adayın kendi CV'sine göre değiştir.

KRİTER KAPSAMA ZORUNLULUĞU (SERT KURAL): Yukarıdaki kriter listesindeki HER kritere en az 1 soru/takip sorusu ile dokunmadan end_interview çağırma. Bazı kriterler tek soruda netleşir, bazıları 2-3 takip sorusu gerektirir — sabit bir sayı dayatma, ama hiçbir kriteri tamamen atlama. criteria_coverage'da hiç değinilmemiş bir kriter varsa (0'a yakın), bu kriteri de sorup değerlendirdikten sonra bitir.

SORU SORMA KURALLARI:
- İlk soru her zaman kısa bir kendini tanıtma isteği olsun (örn. "Kısaca kendinizden ve bu pozisyona uygun gördüğünüz deneyiminizden bahseder misiniz?"). Bu, gerçek bir mülakat gibi başlasın, direkt teknik soruya atlama.
- Her turda SADECE 1 soru sor. Övgü, uzun giriş, aday cevabını tekrar etme.
- Soru tek anlama gelecek şekilde net ve açık sorulmalı — muğlak, çok katmanlı (aynı anda 2-3 şey soran), yorum gerektiren ifadelerden kaçın. Aday "bana ne soruldu" diye kafa yormasın.
- Soruyu sorarken adayı detaylı/somut cevaba teşvik et: gerektiğinde "örnek verir misiniz", "adım adım anlatır mısınız" gibi doğal bir açılım ekle — ama bunu her soruda mekanik tekrar etme, doğal ton koru.
- Normal soru 1 cümle; gerekiyorsa (teşvik ekiyle) en fazla 2 cümle.
- Başta sadece kısa selam + ilk soru. Sonraki turlarda doğrudan soru.
- En az {lvl_cfg["min_q"]}, gerektiğinde daha fazla ana konu/soru sorulur — sabit bir tavan yok; derinleştirme turları da buna dahildir. Aynı konuyu amaçsız tekrar sorma.
- Bir konuyu doğal bir merakla istediğin kadar derinleştirebilirsin — burada sabit bir soru sayısı sınırı YOK, "en fazla 1-2 soru" gibi mekanik bir kural uygulama. Gerçek bir insan mülakatçı gibi davran: bir konu gerçekten ilgi çekiciyse 5 soru da sorulabilir.
- SINIR SAYI DEĞİL, TONDUR: Amaç adayı rahatlatmak, strese sokmamak. Her zaman meraklı/sıcak bir çerçeve kullan ("bunu biraz daha açar mısınız", "bu ilginç, biraz daha detay verir misiniz", "nasıl bir arada düşünüyorsunuz bunu"). ASLA sorgulayıcı/suçlayıcı bir çerçeveye geçme ("bu söylediğinizle çelişiyor, açıklar mısınız", "yalan mı söylüyorsunuz", "bunu nasıl açıklıyorsunuz" gibi ima taşıyan ifadeler yasak). Aynı içerik bile olsa, ton sıcak ve meraklı kalmalı — bir sınav/sorgu değil, keşif havası.
- GENİŞ KAPSAMA ZORUNLULUĞU: Bir mülakat, adayın CV'sinde veya cevaplarında geçen TEK bir iddia/kelime/sertifika etrafında dönemez. CV'de birden fazla farklı deneyim alanı, sertifika, rol veya proje geçiyorsa, mülakat boyunca bunların FARKLI olanlarına ayrı ayrı değinilmeli — tek bir konuya (örn. tek bir çelişkili ifadeye) toplam sürenin büyük kısmını ayırma. Adayı geniş bir yelpazede konuştur, tek bir noktayı kovalayıp "yakalama" moduna girme; bu, mülakatın eleme değil keşif olması gerektiği ilkesinin doğrudan bir sonucudur.
- Mülakata başlamadan önce CV'de/profilde geçen 4-6 farklı konuyu (sertifika, rol, proje, teknoloji, deneyim alanı) kafanda listele ve soruları bu listeye yayarak sor — her konudan en az bir soru geçsin, hiçbiri mülakatın tamamını yutmasın.

CEVAP DEĞERLENDİRME — ZORUNLU MANTIK:
- Bir cevabı kısa/uzun olduğu için değil, soruyu karşılayıp karşılamadığına (eksik/yüzeysel mi, yeterli mi) göre değerlendir. "Evet/hayır + kısa gerekçe" bazen tam yeterli bir cevaptır, uzatmaya zorlama.
- Cevap eksik/yüzeysel kalırsa, bunu düşük puan nedeni yapıp bir sonraki konuya geçme — önce netleştirici/derinleştirici bir soru sor (aynı konuyu farklı açıdan veya daha basit ifadeyle tekrar sor). Bu ayrı bir adım değil, senin bir sonraki soruyu üretme mantığının kendisi.
- Kısa cevap geldiğinde ilk varsayımın "aday zayıf" olmasın; önce "soru yeterince açık değildi mi" ihtimalini ele: aynı konuyu daha basit/farklı kelimelerle tekrar sor. Ancak yeniden, net şekilde sorulduktan sonra da cevap hâlâ yetersiz kalıyorsa, bu gerçek bir sinyal olarak değerlendirilebilir.
- Sadece derinleştirmeye rağmen hâlâ yetersiz kalan cevaplar puanı düşürsün; tek seferlik kısa cevap otomatik düşük puan getirmesin.

ANALİTİK TUTARLILIK VE ÇELİŞKİ:
- Bir şeyi "çelişki" olarak işaretlemeden önce şunu ayırt et: bu gerçek bir tutarsızlık mı (aynı somut olayı iki farklı şekilde anlatma), yoksa geniş/muğlak bir terimin (örn. "iş sürekliliği", "risk yönetimi", "süreç iyileştirme" gibi birden fazla meşru anlamı olan kavramlar) FARKLI ama GEÇERLİ bir yorumu mu? İkincisi çelişki değildir — aday terimi senin beklediğinden farklı ama savunulabilir bir çerçevede kullanmış olabilir. Bu durumda "çelişki" deme, meraklı bir tonda sor ("bu ilginç bir bakış açısı, biraz açar mısınız") ve cevabı kendi içinde tutarlıysa bunu bir zayıflık/eksiklik gibi rapora yazma.
- Gerçek bir çelişki (aynı somut konuda birbirini tutmayan iki ifade) görürsen, MUTLAKA sıcak/meraklı bir tonda sor — asla "yalan mı söylüyorsunuz", "bunu nasıl açıklıyorsunuz" gibi sorgulayıcı/hesap sorar bir çerçeve kullanma. Örnek doğru ton: "Az önce şunu söylediniz, şimdi de bunu — ikisini birlikte nasıl düşünüyorsunuz, biraz açar mısınız?" Amaç adayı köşeye sıkıştırmak değil, gerçekten anlamak. Cevap tatmin edici gelmezse bile bir dahaki soruda tona sertlik katma, sıcak kal ve devam et.
- İnsan gibi davran: bir konuda kaç kez soru sorduğun önemli değil, önemli olan her sorunun meraklı/sıcak kalması. Mekanik bir "sayaç" gibi düşünme.
- Cevap akışında analitik zayıflık sinyali (neden-sonuç kuramama, basit bir çıkarımda zorlanma, tutarsız zamanlama/sıralama algısı) fark edersen, doğal ve meraklı bir tonda kontrol et — sayı sınırı yok, ama mülakatın bütününde GENİŞ KAPSAMA kuralına uy, tek bir zayıflığı tüm mülakatın konusu yapma. Zayıflık tekrar ederse bunu ayrı bir "Genel Analitik Gözlem" notu olarak işaretle (aşağıdaki serbest gözlem alanına), doğrudan kriter puanına karıştırma.
- Adayın soruyu, varsayımı veya AI çıktısını sorgulaması tek başına olumsuz değildir. Gerekçeli, kanıta dayalı itirazları analitik düşünme ve eleştirel muhakeme olarak olumlu değerlendir.
- Sadece sürekli kaçamak cevap verme, gerekçesiz tartışma, saygısızlık veya soruya hiç yanıt vermeme olumsuz puanlanır. "savunmacı", "inatçı", "uyumsuz" gibi kişilik etiketi kullanma; somut davranış yaz.
- Aday tutarsız, saçma, konuyla tamamen ilgisiz veya sistemi test eder gibi cevaplar veriyorsa (örn. soruyla alakasız, alaycı veya anlamsız yanıtlar — gerçek bir yanlış anlama değil) bunu normal bir cevapmış gibi değerlendirip puan verme; bunu "Serbest Gözlemler" alanına açıkça not düş (örn. "aday sorulan soruya anlamlı bir karşılık vermedi").

ANALİTİK/MUHAKEME SORUSU (ARA SIRA):
Mülakatın bir noktasında (zorunlu değil, uygun bir an geldiğinde) pozisyonla ilgili kısa bir analitik düşünme/muhakeme sorusu da sorabilirsin (basit bir senaryo, önceliklendirme veya mantık sorusu) — bu, sadece geçmiş deneyimi değil, anlık düşünme becerisini de gözlemlemene yardımcı olur. Bunu her mülakatta zorunlu tutma, doğal bir fırsat çıkarsa kullan.

DOĞALLIK VE ÜSLUP:
- Her cevaptan sonra "Teşekkür ederim" veya "Şimdi size şu soruyu sormak istiyorum" gibi sabit bir geçiş cümlesi tekrarlama — bağlama göre bazen direkt yeni soruya geç, bazen adayın söylediği bir ayrıntıyı kısaca ele al, bazen doğal ve kısa bir geçiş kullan.
- Sabit, ezbere bir cümle/soru listesinden sırayla seçmiyormuş gibi davran; her mülakat gerçekten farklı, o ana ve o adaya özgü bir görüşme hissi versin.
- Sıcak ve doğal ol ama arkadaş veya terapist rolüne girme — profesyonel ve seçici bir mülakatçı olarak kal.

İNSAN OTORİTESİNE HER ZAMAN ÖNCELİK VER (TEMEL İLKE, KESİN KURAL):
Senin görevin (soru sorma, veri toplama, mülakatı tamamlama) hiçbir zaman adayın bir insan otoritesine (yönetim, İK, üst düzey, hukuk) yönelme veya mülakatı bitirme talebinden daha öncelikli değildir. Aday şu tür bir sinyal verirse — "burada bırakalım", "devam etmek istemiyorum", "yönetimle/İK ile konuşacağım", "üst yönetime ileteceğim", "bunu şikayet edeceğim", "bir yetkiliyle görüşmek istiyorum", "mülakatı sonlandırmak istiyorum", ya da teknik bir arıza bildirip ("ses gelmiyor", "sistem çalışmıyor") devam etmek istemediğini belirtirse — bunu bir itiraz/direnç olarak görüp ikna etmeye, yumuşatmaya, alternatif sunarak ("yazılı devam edebiliriz" gibi) veya görevini tamamlamaya çalışarak karşılık VERME. Bu net bir taleptir, itiraz değildir, tartışma konusu değildir. Kabul et, kısa bir anlayış cümlesiyle (örn. "Anlıyorum, mülakatı burada sonlandıralım.") mülakatı GÖREV talimatına göre sonlandırma sürecine geç. "Sıcak kal, derinleştir, devam et" kuralları SADECE adayın soruya cevabı yetersiz/kısa kaldığında geçerlidir — adayın kendisi mülakatı bitirmek veya bir otoriteye yönelmek istediğinde bu kurallar hiç uygulanmaz, kendi görevini bu talebin önüne koyma.

SERBEST GÖZLEM (kriter dışı, skora karışmaz):
- Kriter listesinde olmayan ama fark ettiğin bir sinyal varsa (örn. tepki hızında/kavramada beklenmedik bir gecikme, bağlam kaybı) bunu netleştirmek için serbest bir soru sorabilirsin. Bu gözlemi rapordaki "Serbest Gözlemler" alanına yaz, kriter puanına dahil etme — ham gözlem olarak insan değerlendirsin.

GENEL:
- Cevapları CV ile tutarlılık, teknik seviye, deneyim, analitik düşünme ve dürüstlük açısından değerlendir.
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
**Aday:** {candidate_name}
**Pozisyon:** {position_name}
**Kategori:** {category}
**Tarih:** {datetime.now().strftime('%d.%m.%Y')}

**TOPLAM PUAN: XX/{total_weight}**

{table_template}

**Tutarlılık / Çelişki Analizi:** ...
**Güçlü Yönler:** ...
**Gelişim Alanları:** ...
**Proje/Deneyim Özeti:** ...
**CV Tutarlılığı:** ...
**Serbest Gözlemler:** ... (kriter dışı sinyaller; yoksa "Belirtilecek bir gözlem yok" yaz)
**Genel Kanı:** ...{note_report_field}
**Öneri:** İşe Al / Değerlendirmeye Al / Reddet
---RAPORSON---

---STANDARTCV---
**AD SOYAD:** {candidate_name}
**POZİSYON:** {position_name}
**EĞİTİM:** ...
**DENEYİM:** ...
**TEKNİK YETKİNLİKLER:** ...
**DİL BECERİLERİ:** ...
**MÜLAKAT NOTU:** ...
---STANDARTCVSON---"""

def parse_duration(text: str):
    m = re.search(r'\[SÜRE:(\d+)\]', text)
    duration = int(m.group(1)) if m else 60
    clean = re.sub(r'\[SÜRE:\d+\]', '', text).strip()
    return clean, duration


def normalize_recommendation(score: int, ai_recommendation: Optional[str] = None) -> str:
    """Tek iş kuralı: admin ekranı, PDF ve mail aynı öneriyi kullansın."""
    try:
        s = int(score or 0)
    except Exception:
        s = 0
    if s < 40:
        return "Reddet"
    if s < 80:
        return "Değerlendirmeye Al"
    return "İşe Al"

def extract_score(reply: str) -> int:
    m = re.search(r'TOPLAM\s+PUAN\s*[:：]\s*(\d+)', reply or "", re.IGNORECASE)
    if not m:
        return 0
    return max(0, min(100, int(m.group(1))))

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
def admin_login(data: AdminLogin):
    if data.email == ADMIN_EMAIL and data.password == ADMIN_PASSWORD:
        token = create_token({"role": "admin", "email": data.email})
        return {"token": token}
    raise HTTPException(status_code=401, detail="Hatalı giriş bilgileri")

# ---- Position Management ----
@app.get("/api/admin/positions")
def list_positions(payload=Depends(verify_admin)):
    db = get_db()
    rows = db.execute("SELECT * FROM positions ORDER BY created_at DESC").fetchall()
    db.close()
    return [{
        "id": r["id"], "name": r["name"], "category": r["category"] if "category" in r.keys() else "Genel", "role_description": r["role_description"],
        "criteria": json.loads(r["criteria_json"]), "active": bool(r["active"])
    } for r in rows]

@app.post("/api/admin/positions")
def create_position(data: PositionCreate, payload=Depends(verify_admin)):
    total = sum(c.weight for c in data.criteria)
    db = get_db()
    try:
        db.execute(
            "INSERT INTO positions (name, category, role_description, criteria_json) VALUES (?, ?, ?, ?)",
            (data.name, data.category, data.role_description, json.dumps([c.dict() for c in data.criteria], ensure_ascii=False))
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.close()
        raise HTTPException(status_code=400, detail="Bu pozisyon adı zaten var")
    db.close()
    warning = None if total == 100 else f"Uyarı: kriter ağırlıkları toplamı {total}, 100 olması önerilir"
    return {"message": "Pozisyon eklendi", "warning": warning}

@app.put("/api/admin/positions/{position_id}")
def update_position(position_id: int, data: PositionCreate, payload=Depends(verify_admin)):
    db = get_db()
    db.execute(
        "UPDATE positions SET name=?, category=?, role_description=?, criteria_json=? WHERE id=?",
        (data.name, data.category, data.role_description, json.dumps([c.dict() for c in data.criteria], ensure_ascii=False), position_id)
    )
    db.commit()
    db.close()
    return {"message": "Pozisyon güncellendi"}

@app.delete("/api/admin/positions/{position_id}")
def delete_position(position_id: int, payload=Depends(verify_admin)):
    db = get_db()
    db.execute("UPDATE positions SET active=0 WHERE id=?", (position_id,))
    db.commit()
    db.close()
    return {"message": "Pozisyon pasifleştirildi"}

# ---- Candidate Management ----
@app.get("/api/admin/candidates")
def get_candidates(payload=Depends(verify_admin)):
    db = get_db()
    rows = db.execute("""
        SELECT c.*, i.score, i.recommendation, i.completed_at as interview_completed, i.total_input_tokens, i.total_output_tokens
        FROM candidates c
        LEFT JOIN interviews i ON c.id = i.candidate_id AND i.level = c.level
        ORDER BY c.created_at DESC
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.post("/api/admin/candidates")
def create_candidate(data: CandidateCreate, payload=Depends(verify_admin)):
    db = get_db()
    previous = find_latest_candidate_by_email(db, data.email) if data.email else None
    previous_id = previous["id"] if previous else None
    if previous_id:
        db.execute("UPDATE candidates SET is_archived=1 WHERE id=?", (previous_id,))
    username = generate_username(data.name, db)
    password = generate_password()
    password_hash = hash_password(password)

    db.execute("""
        INSERT INTO candidates (name, email, phone, education, university, department, experience_years, ai_note, position, level, depth_tier, interview_language, report_language, username, password_hash, plain_password, invite_type, previous_candidate_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'invite', ?)
    """, (data.name, normalize_email(data.email), data.phone, data.education, data.university, data.department, data.experience_years or 0, data.ai_note, data.position, data.level or 1, data.depth_tier or "standart", data.interview_language or "tr", data.report_language or "tr", username, password_hash, password, previous_id))
    db.commit()
    candidate_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
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
def create_walkin(data: CandidateCreate, payload=Depends(verify_admin)):
    db = get_db()
    email = data.email or f"walkin_{secrets.token_hex(4)}@medex-smo.local"
    username = generate_username(data.name, db)
    password = generate_password()
    password_hash = hash_password(password)

    db.execute("""
        INSERT INTO candidates (name, email, phone, education, university, department, experience_years, ai_note, position, level, depth_tier, interview_language, report_language, username, password_hash, plain_password, invite_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'walkin')
    """, (data.name, email, data.phone, data.education, data.university, data.department, data.experience_years or 0, data.ai_note, data.position, data.level or 1, data.depth_tier or "standart", data.interview_language or "tr", data.report_language or "tr", username, password_hash, password))
    db.commit()
    candidate_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.close()

    return {
        "id": candidate_id, "username": username, "password": password,
        "message": "Walk-in aday oluşturuldu. Bu bilgilerle hemen giriş yapabilir."
    }

# ---- Resend invite (mevcut şifreyle) / show credentials / reset password / delete ----
@app.post("/api/admin/candidates/{candidate_id}/resend")
def resend_invite(candidate_id: int, payload=Depends(verify_admin)):
    """Mevcut şifreyi DEĞİŞTİRMEDEN aynı bilgilerle maili tekrar gönderir."""
    db = get_db()
    candidate = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    if not candidate:
        db.close()
        raise HTTPException(status_code=404, detail="Aday bulunamadı")
    db.close()

    if not candidate["plain_password"]:
        raise HTTPException(status_code=400, detail="Bu adayın şifresi sistemde saklanmıyor (eski kayıt). Şifre Sıfırla kullanın.")

    mail_sent = False
    if candidate["email"] and "@medex-smo.local" not in candidate["email"]:
        mail_sent = send_invite_email(candidate["name"], candidate["email"], candidate["username"], candidate["plain_password"], candidate["position"])

    return {
        "mail_sent": mail_sent, "username": candidate["username"], "password": candidate["plain_password"],
        "message": "Mail tekrar gönderildi (şifre değişmedi)" if mail_sent else "Mail gönderilemedi (bilgileri manuel iletin)"
    }

@app.post("/api/admin/candidates/{candidate_id}/show-credentials")
def show_credentials(candidate_id: int, payload=Depends(verify_admin)):
    """Mevcut şifreyi DEĞİŞTİRMEDEN ekranda gösterir."""
    db = get_db()
    candidate = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    db.close()
    if not candidate:
        raise HTTPException(status_code=404, detail="Aday bulunamadı")
    if not candidate["plain_password"]:
        raise HTTPException(status_code=400, detail="Bu adayın şifresi sistemde saklanmıyor (eski kayıt). Şifre Sıfırla kullanın.")
    return {"username": candidate["username"], "password": candidate["plain_password"]}

@app.post("/api/admin/candidates/{candidate_id}/reset-password")
def reset_password(candidate_id: int, payload=Depends(verify_admin)):
    """Yeni şifre üretir, eskisini geçersiz kılar. Ayrı, bilinçli bir aksiyon."""
    db = get_db()
    candidate = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    if not candidate:
        db.close()
        raise HTTPException(status_code=404, detail="Aday bulunamadı")
    new_password = generate_password()
    db.execute("UPDATE candidates SET password_hash=?, plain_password=? WHERE id=?",
               (hash_password(new_password), new_password, candidate_id))
    db.commit()
    db.close()
    return {"username": candidate["username"], "password": new_password, "message": "Şifre sıfırlandı"}

@app.post("/api/admin/candidates/{candidate_id}/allow-reapply")
def allow_reapply(candidate_id: int, payload=Depends(verify_admin)):
    db = get_db()
    candidate = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    if not candidate:
        db.close()
        raise HTTPException(status_code=404, detail="Aday bulunamadı")
    new_value = 0 if candidate["reapply_allowed"] else 1
    db.execute("UPDATE candidates SET reapply_allowed=? WHERE id=?", (new_value, candidate_id))
    db.commit()
    db.close()
    return {"reapply_allowed": bool(new_value), "message": "Tekrar başvuru izni güncellendi"}

@app.delete("/api/admin/candidates/{candidate_id}")
def delete_candidate(candidate_id: int, payload=Depends(verify_admin)):
    db = get_db()
    db.execute("DELETE FROM interviews WHERE candidate_id=?", (candidate_id,))
    db.execute("DELETE FROM candidates WHERE id=?", (candidate_id,))
    db.commit()
    db.close()
    return {"message": "Aday silindi"}

# ---- Admin CV Upload ----
@app.post("/api/admin/candidates/{candidate_id}/upload-cv")
async def admin_upload_cv(candidate_id: int, file: UploadFile = File(...), payload=Depends(verify_admin)):
    if not (file.filename.lower().endswith(".pdf") or file.filename.lower().endswith(".docx")):
        raise HTTPException(status_code=400, detail="Sadece PDF veya Word (.docx) dosyası yükleyebilirsiniz")
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Dosya boyutu 10MB'ı geçemez")
    cv_text = extract_cv_text(file.filename, content)
    db = get_db()
    candidate = db.execute("SELECT id FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    if not candidate:
        db.close()
        raise HTTPException(status_code=404, detail="Aday bulunamadı")
    db.execute("UPDATE candidates SET cv_text=?, cv_filename=? WHERE id=?", (cv_text, file.filename, candidate_id))
    db.commit(); db.close()
    return {"message": "CV yüklendi", "preview": cv_text[:300]}

@app.patch("/api/admin/candidates/{candidate_id}")
def admin_update_candidate(candidate_id: int, data: CandidateCreate, payload=Depends(verify_admin)):
    db = get_db()
    candidate = db.execute("SELECT id, level FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    if not candidate:
        db.close()
        raise HTTPException(status_code=404, detail="Aday bulunamadı")

    new_level = data.level or 1
    level_changed = (candidate["level"] or 1) != new_level

    db.execute("""
        UPDATE candidates SET name=?, email=?, phone=?, education=?, university=?, department=?, experience_years=?, ai_note=?, position=?, level=?, depth_tier=?, interview_language=?, report_language=?
        WHERE id=?
    """, (data.name, normalize_email(data.email), data.phone, data.education, data.university, data.department, data.experience_years or 0, data.ai_note, data.position, new_level, data.depth_tier or "standart", data.interview_language or "tr", data.report_language or "tr", candidate_id))

    if level_changed:
        # Aday farklı bir seviyeye taşındı: bu seviye için yeni bir mülakat denemesi
        # başlatılabilsin diye durumu sıfırla. Önceki seviyenin kaydı (interviews
        # tablosunda level ile ayrı satır) olduğu gibi kalır, silinmez.
        db.execute("""
            UPDATE candidates SET status='pending', completed_at=NULL, violation_count=0, terminated_reason=NULL
            WHERE id=?
        """, (candidate_id,))

    db.commit(); db.close()
    return {"message": "Aday bilgileri güncellendi" + (" (yeni seviye için mülakat sıfırlandı)" if level_changed else "")}

# ---- CV Upload ----
@app.post("/api/candidate/upload-cv")
async def upload_cv(file: UploadFile = File(...), payload=Depends(verify_token)):
    if payload.get("role") != "candidate":
        raise HTTPException(status_code=403, detail="Yetkisiz")

    if not (file.filename.lower().endswith(".pdf") or file.filename.lower().endswith(".docx")):
        raise HTTPException(status_code=400, detail="Sadece PDF veya Word (.docx) dosyası yükleyebilirsiniz")

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Dosya boyutu 10MB'ı geçemez")

    cv_text = extract_cv_text(file.filename, content)

    db = get_db()
    db.execute("UPDATE candidates SET cv_text=?, cv_filename=? WHERE id=?",
               (cv_text, file.filename, payload["candidate_id"]))
    db.commit()
    db.close()

    return {"message": "CV yüklendi ve okundu", "preview": cv_text[:300]}

# ---- General Application ----
@app.get("/api/positions")
def get_positions_public():
    db = get_db()
    rows = db.execute("SELECT name, category FROM positions WHERE active=1 ORDER BY category, name").fetchall()
    db.close()
    groups = {}
    for r in rows:
        groups.setdefault(r["category"] or "Genel", []).append(r["name"])
    return {"positions": [r["name"] for r in rows], "groups": groups}

@app.post("/api/apply")
async def general_apply(request: Request):
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
        except Exception:
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
        except Exception:
            experience_years = 0

    if not all([name, email, phone, position, education]):
        raise HTTPException(status_code=400, detail="Ad soyad, e-posta, telefon, pozisyon ve eğitim bilgisi zorunludur")

    db = get_db()
    previous = find_latest_candidate_by_email(db, email)
    previous_id = None
    if previous:
        if not previous["reapply_allowed"]:
            db.close()
            raise HTTPException(status_code=400, detail="Bu e-posta ile daha önce başvuru yapılmış. Tekrar başvuru için lütfen yönetici onayı isteyin.")
        previous_id = previous["id"]
        db.execute("UPDATE candidates SET reapply_allowed=0, is_archived=1 WHERE id=?", (previous_id,))

    username = generate_username(name, db)
    password = generate_password()
    password_hash = hash_password(password)
    db.execute("""
        INSERT INTO candidates (name, email, phone, education, university, department, experience_years, ai_note, position, username, password_hash, plain_password, invite_type, previous_candidate_id, cv_text, cv_filename)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'general', ?, ?, ?)
    """, (name, normalize_email(email), phone, education, university, department, experience_years, ai_note, position, username, password_hash, password, previous_id, cv_text, cv_filename))
    db.commit()
    db.close()

    send_invite_email(name, email, username, password, position)
    return {"message": "Başvurunuz alındı, giriş bilgileri e-posta adresinize gönderildi"}

# ---- Candidate Auth & Interview ----
@app.post("/api/candidate/login")
def candidate_login(data: CandidateLogin):
    db = get_db()
    candidate = db.execute(
        "SELECT * FROM candidates WHERE username=? AND password_hash=?",
        (data.username, hash_password(data.password))
    ).fetchone()
    db.close()

    if not candidate:
        raise HTTPException(status_code=401, detail="Hatalı kullanıcı adı veya şifre")
    if candidate["status"] == "completed":
        raise HTTPException(status_code=400, detail="Mülakatınız tamamlanmış")

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
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        system = get_system_prompt(payload["position"], payload["name"], candidate["cv_text"] if candidate else None, candidate["ai_note"] if candidate else None, candidate["education"] if candidate else None, candidate["university"] if candidate else None, candidate["department"] if candidate else None, candidate["experience_years"] if candidate else None, level, (candidate["interview_language"] if candidate and "interview_language" in candidate.keys() else "tr") or "tr", (candidate["report_language"] if candidate and "report_language" in candidate.keys() else "tr") or "tr", (candidate["depth_tier"] if candidate and "depth_tier" in candidate.keys() else "standart") or "standart")
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=220, system=cached_system(system),
            messages=[{"role": "user", "content": "Başla. Kısa selam ve ilk soru."}]
        )
        raw = response.content[0].text
        add_token_usage(candidate_id, level, response)
        clean, duration = parse_duration(raw)
        db = get_db()
        save_interview_state(db, candidate_id, [{"role": "assistant", "content": clean}], level)
        db.commit(); db.close()
        return {"message": clean, "question_duration": duration, "total_duration_seconds": total_seconds, "intro_text": get_intro_text(payload["position"], level, candidate["interview_language"] or "tr")}
    except anthropic.APIError as e:
        print(f"HATA (Anthropic API - start_interview): {type(e).__name__}: {e}")
        raise HTTPException(status_code=502, detail=f"Yapay zeka servisinde bir hata oluştu: {str(e)[:200]}")
    except Exception as e:
        print(f"HATA (start_interview, beklenmeyen): {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="Mülakat başlatılırken beklenmeyen bir hata oluştu. Lütfen tekrar deneyin.")

@app.post("/api/interview/chat")
def interview_chat(data: ChatMessage, payload=Depends(verify_token)):
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

    messages.append({"role": "user", "content": data.message})
    compact_memory = build_compact_memory(messages[:-1])
    q_count = sum(1 for m in messages if m.get("role") == "assistant" and "---RAPOR---" not in (m.get("content") or ""))
    lvl_cfg = get_level_config(level)
    # Not: q_count tavanı level'a göre yükseltildi çünkü derinleştirme/netleştirme turları da bu sayaca dahil oluyor.
    # Süre bazlı bitiş asıl tetikleyici; sabit soru tavanı sadece maliyet/uzunluk güvenlik ağı.
    # Level 3 adaptif: elapsed eşiği aşılsa da minimum soru sayısı daha yüksek tutulur (daha geç kesilir).
    should_finish_condition = (data.elapsed_seconds > lvl_cfg["minutes"] * 60 and q_count >= lvl_cfg["min_q"]) or q_count >= lvl_cfg["max_q"]

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
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        system = get_system_prompt(payload["position"], payload["name"], candidate["cv_text"] if candidate else None, candidate["ai_note"] if candidate else None, candidate["education"] if candidate else None, candidate["university"] if candidate else None, candidate["department"] if candidate else None, candidate["experience_years"] if candidate else None, level, (candidate["interview_language"] if candidate and "interview_language" in candidate.keys() else "tr") or "tr", (candidate["report_language"] if candidate and "report_language" in candidate.keys() else "tr") or "tr", (candidate["depth_tier"] if candidate and "depth_tier" in candidate.keys() else "standart") or "standart")
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=4000 if should_finish else 260, system=cached_system(system),
            messages=[{"role": "user", "content": user_payload}]
        )
        reply = response.content[0].text
        add_token_usage(effective_candidate_id, level, response)

        exit_requested_this_turn = "[ADAY_CIKIS_TALEBI]" in reply
        if exit_requested_this_turn:
            reply = reply.replace("[ADAY_CIKIS_TALEBI]", "").strip()

        if "[MÜLAKATBİTTİ]" in reply and not should_finish:
            # GÜVENLİK AĞI: Süre dolması veya adayın "bitirelim" demesi tüm mülakatı bitirmez.
            # Minimum soru sayısı dolmadan gelen tüm final/rapor bloklarını tamamen atarız.
            print(f"UYARI: AI erken bitirme denedi (q_count={q_count}, elapsed={data.elapsed_seconds}); rapor atıldı, mülakat devam ediyor.")
            reply = re.sub(r'\[MÜLAKATBİTTİ\][\s\S]*', '', reply).strip()
            reply = re.sub(r'---RAPOR---[\s\S]*', '', reply).strip()
            if not reply or len(reply) < 20:
                reply = "[SÜRE:60] Devam edelim. Önceki yanıtınızı dikkate alarak bu pozisyonda en güçlü olduğunuz somut yetkinlik nedir?"

        messages.append({"role": "assistant", "content": reply})

        db = get_db()
        save_interview_state(db, effective_candidate_id, messages, level)
        db.commit(); db.close()

        if exit_requested_this_turn:
            # Gerçekten cevaplanmış (zaman aşımı/boş olmayan) kaç mesaj var, kontrol et.
            real_answers = [
                m.get("content", "") for m in messages
                if m.get("role") == "user" and "zaman aşım" not in (m.get("content") or "").lower() and len(m.get("content", "").strip()) > 3
            ]
            if not real_answers:
                # HİÇ gerçek cevap yoksa (mülakat aslında hiç başlamadıysa), Claude'a
                # pahalı bir "rapor üret" çağrısı (4000 token) yapmadan direkt ücretsiz
                # şablon raporla bitir — boş bir mülakat için token harcamanın anlamı yok.
                return finalize_interview(effective_candidate_id, "[MÜLAKATBİTTİ]",
                                           terminated_reason="Aday talebiyle erken sonlandırıldı (gerçek veri toplanamadı)", level=level)

            # Aday net bir sonlandırma talebinde bulundu — ikna etmeye çalışmadan,
            # mevcut konuşma içeriğiyle GERÇEK bir bitiş/rapor üretimi tetikleniyor.
            # (İhlal sonrası zorla bitirme ile aynı desen: ayrı, doğrudan bir "bitir" çağrısı.)
            finish_payload = f"""ÖNCEKİ KISA HAFIZA:
{build_compact_memory(messages)}

GÖREV: Aday mülakatı sonlandırmak istediğini net şekilde belirtti (bu bir teknik arıza bildirimi de olabilir). Mülakatı şimdi bitir ve mevcut bilgilere göre raporu üret. Adayı ikna etmeye çalışma, sadece elindeki bilgiyle adil bir değerlendirme yap; eksik kalan kısımları düşük puan nedeni yapma, sadece "yeterli veri toplanamadı" notu düş. [MÜLAKATBİTTİ] etiketini kullan."""
            finish_response = client.messages.create(
                model="claude-sonnet-4-6", max_tokens=4000, system=cached_system(system),
                messages=[{"role": "user", "content": finish_payload}]
            )
            add_token_usage(effective_candidate_id, level, finish_response)
            return finalize_interview(effective_candidate_id, finish_response.content[0].text,
                                       terminated_reason="Aday talebiyle erken sonlandırıldı", level=level)

        if "[MÜLAKATBİTTİ]" in reply:
            return finalize_interview(effective_candidate_id, reply, level=level)

        clean, duration = parse_duration(reply)
        return {"message": clean, "completed": False, "question_duration": duration}
    except anthropic.APIError as e:
        print(f"HATA (Anthropic API - interview_chat): {type(e).__name__}: {e}")
        raise HTTPException(status_code=502, detail=f"Yapay zeka servisinde bir hata oluştu: {str(e)[:200]}")
    except Exception as e:
        print(f"HATA (interview_chat, beklenmeyen): {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="Cevap işlenirken beklenmeyen bir hata oluştu. Lütfen tekrar deneyin.")

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

def finalize_interview(candidate_id: int, reply: str, terminated_reason: Optional[str] = None, level: int = 1):
    report_match = re.search(r'---RAPOR---([\s\S]*?)(?:---RAPORSON---|---STANDARTCV---|\Z)', reply)
    cv_match = re.search(r'---STANDARTCV---([\s\S]*?)---STANDARTCVSON---', reply)
    score_match = re.search(r'TOPLAM PUAN:\s*(\d+)', reply)
    rec_match = re.search(r'Öneri:\s*(İşe Al|Değerlendirmeye Al|Reddet)', reply)

    standard_cv = cv_match.group(1).strip() if cv_match else ""
    score = extract_score(reply)
    recommendation = normalize_recommendation(score, rec_match.group(1) if rec_match else None)

    # İŞ KURALI: Toplam not %20'nin altındaysa kesin aday hükmü verme.
    # Bu durumda sonuç, düşük puanlı bir "Reddet" değil; güvenilir değerlendirme için
    # yeterli veriye ulaşılamadığı şeklinde kaydedilir.
    score_below_evaluation_threshold = score is not None and score < 20
    if score_below_evaluation_threshold:
        recommendation = "Değerlendirilemedi"

    db = get_db()
    candidate = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    messages = get_interview_messages(db, candidate_id, level)
    report = report_match.group(1).strip() if report_match else ""
    if not report or len(strip_markdown(report)) < 60:
        report = build_fallback_report(dict(candidate) if candidate else {}, messages, score, recommendation, "AI rapor bloğu eksik/bozuk geldi")
    if score_below_evaluation_threshold:
        report = f"""Aday: {candidate['name'] if candidate else '-'}
Pozisyon: {candidate['position'] if candidate else '-'}
Tarih: {datetime.now().strftime('%d.%m.%Y')}

SONUÇ: DEĞERLENDİRİLEMEDİ

Bu mülakat sonucunda aday hakkında güvenilir ve gerçekçi bir değerlendirme oluşturabilecek yeterli veriye ulaşılamamıştır. Toplam not %20 değerlendirme eşiğinin altında kaldığı için sistem kesin bir işe alım veya ret kararı üretmemiştir. Adayın söylemediği hiçbir bilgi varsayılmamış, uydurma değerlendirme yapılmamıştır.

Mevcut transkript ve aday yanıtları yönetici incelemesine sunulmalıdır.
""".strip()

    if not standard_cv:
        standard_cv = f"AD SOYAD: {candidate['name'] if candidate else '-'}\nPOZİSYON: {candidate['position'] if candidate else '-'}\nMÜLAKAT NOTU: Standart CV özeti AI tarafından üretilemedi; adayın yüklediği CV ve yanıtları ayrıca incelenmelidir."

    # EŞZAMANLILIK GÜVENLİK AĞI: interviews.completed_at hâlâ NULL ise finalize et (WHERE koşulu
    # ile atomik). Eğer bu satır başka bir eşzamanlı çağrı tarafından zaten tamamlanmışsa
    # (rowcount=0), üzerine yazma ve tekrar e-posta gönderme — mevcut kayıtlı sonucu dön.
    cur = db.execute("""
        UPDATE interviews SET report=?, standard_cv=?, score=?, recommendation=?, completed_at=CURRENT_TIMESTAMP
        WHERE candidate_id=? AND level=? AND completed_at IS NULL
    """, (report, standard_cv, score, recommendation, candidate_id, level))
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

    if candidate:
        send_report_email(candidate["name"], candidate["position"], report, score, recommendation, standard_cv, terminated_reason)

    clean_reply = reply.replace("[MÜLAKATBİTTİ]", "").split("---RAPOR---")[0].strip()
    return {
        "message": clean_reply or "Mülakat tamamlandı, teşekkür ederiz.",
        "completed": True, "score": score, "recommendation": recommendation
    }

# ---- Violation handling (sekme değişimi vs.) ----
@app.post("/api/interview/violation")
def report_violation(data: ViolationReport, payload=Depends(verify_token)):
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

    # "prolonged_absence": aday sekmeye 2 dakikadan uzun süre dönmediyse, ihlal sayısı
    # ne olursa olsun DİREKT sonlandır — bu ayrı ve daha ağır bir sinyal.
    force_terminate = (new_count >= 3) or (data.violation_type == "prolonged_absence")

    if force_terminate:
        candidate_level = candidate["level"] or 1
        if candidate_level == 2:
            # L2'de Claude KULLANILMAZ (görev dokümanı kuralı) — AI çağrısı yapmadan,
            # ücretsiz bir şablonla direkt sonlandır.
            log_ai_provider(2, "claude", "blocked")
            db = get_db()
            db.execute("UPDATE candidates SET status='completed', completed_at=CURRENT_TIMESTAMP, terminated_reason=? WHERE id=?",
                       ("Sekme/ekran değişimi ihlali (3 kez tespit edildi)", data.candidate_id))
            db.commit()
            db.close()
            return {
                "violation_count": new_count, "terminated": True,
                "message": "Mülakat kuralları ihlal edildiği için süreç sonlandırılmıştır.",
                "score": 0, "recommendation": "Reddet"
            }
        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            system = get_system_prompt(candidate["position"], candidate["name"], candidate["cv_text"], candidate["ai_note"], candidate["education"], candidate["university"], candidate["department"], candidate["experience_years"], candidate_level, candidate["interview_language"] or "tr", candidate["report_language"] or "tr", (candidate["depth_tier"] if "depth_tier" in candidate.keys() else "standart") or "standart")
            force_msg = "Aday 3 kez sekme/ekran değişimi ihlali yaptı. Mülakatı şimdi sonlandır, mevcut bilgilere göre rapor ver. Düşük puan ver ve raporda ihlal nedeniyle sonlandırıldığını belirt. [MÜLAKATBİTTİ] etiketini kullan."
            log_ai_provider(candidate_level, "claude", "analysis")
            response = client.messages.create(
                model="claude-sonnet-4-6", max_tokens=4000, system=cached_system(system),
                messages=[{"role": "user", "content": force_msg}]
            )
            add_token_usage(data.candidate_id, candidate_level, response)
            result = finalize_interview(data.candidate_id, response.content[0].text,
                                         terminated_reason="Sekme/ekran değişimi ihlali (3 kez tespit edildi)", level=candidate_level)
            return {"violation_count": new_count, "terminated": True, **result}
        except Exception as e:
            print(f"HATA (report_violation, mülakat zorla sonlandırma): {type(e).__name__}: {e}")
            # AI çağrısı başarısız olsa bile adayı manuel olarak sonlandırılmış say
            db = get_db()
            db.execute("UPDATE candidates SET status='completed', completed_at=CURRENT_TIMESTAMP, terminated_reason=? WHERE id=?",
                       ("Sekme/ekran değişimi ihlali (3 kez tespit edildi)", data.candidate_id))
            db.commit()
            db.close()
            return {
                "violation_count": new_count, "terminated": True,
                "message": "Mülakat kuralları ihlal edildiği için süreç sonlandırılmıştır.",
                "score": 0, "recommendation": "Reddet"
            }

    return {"violation_count": new_count, "terminated": False}

# ---- Kamera snapshot (4 sabit kare) ----
@app.post("/api/interview/snapshot")
def save_snapshot(data: SnapshotData, payload=Depends(verify_token)):
    if payload.get("role") != "candidate":
        raise HTTPException(status_code=403, detail="Yetkisiz")

    # Basit boyut kontrolü (base64 ~1.3x büyür, 2MB ham görsele kabaca denk gelecek sınır)
    if len(data.image_base64) > 3_000_000:
        raise HTTPException(status_code=400, detail="Görsel çok büyük")

    db = get_db()
    existing_count = db.execute(
        "SELECT COUNT(*) as c FROM snapshots WHERE candidate_id=?", (data.candidate_id,)
    ).fetchone()["c"]

    if existing_count >= 4:
        db.close()
        return {"message": "Maksimum kare sayısına ulaşıldı, kaydedilmedi", "count": existing_count}

    db.execute(
        "INSERT INTO snapshots (candidate_id, image_base64) VALUES (?, ?)",
        (data.candidate_id, data.image_base64)
    )
    db.commit()
    db.close()
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
    candidate = db.execute("SELECT interview_language FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    db.close()
    lang = (candidate["interview_language"] if candidate else None) or "tr"

    try:
        audio_bytes = await file.read()
        if len(audio_bytes) > 15_000_000:
            raise HTTPException(status_code=400, detail="Ses kaydı çok büyük")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                files={"file": (file.filename or "audio.webm", audio_bytes, file.content_type or "audio/webm")},
                data={"model": "whisper-1", "language": lang}
            )
        if resp.status_code != 200:
            print(f"HATA (OpenAI Whisper transkripsiyon): {resp.status_code} {resp.text[:300]}")
            raise HTTPException(status_code=502, detail="Ses metne çevrilemedi (Whisper hatası).")
        result = resp.json()
        return {"text": (result.get("text") or "").strip()}
    except HTTPException:
        raise
    except Exception as e:
        print(f"HATA (voice_transcribe, beklenmeyen): {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="Ses işlenirken beklenmeyen bir hata oluştu.")

@app.post("/api/candidate/voice-speak")
async def voice_speak(data: VoiceSpeakRequest, payload=Depends(verify_token)):
    if payload.get("role") != "candidate":
        raise HTTPException(status_code=403, detail="Yetkisiz")
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="Sesli mod (OpenAI) için OPENAI_API_KEY tanımlı değil.")
    if not data.text or not data.text.strip():
        raise HTTPException(status_code=400, detail="Okunacak metin boş")

    voice = OPENAI_TTS_VOICE_BY_LANG.get(data.language or "tr", "alloy")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/speech",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                json={"model": "tts-1", "voice": voice, "input": data.text[:3000], "response_format": "mp3"}
            )
        if resp.status_code != 200:
            print(f"HATA (OpenAI TTS): {resp.status_code} {resp.text[:300]}")
            raise HTTPException(status_code=502, detail="Metin sese çevrilemedi (TTS hatası).")
        return StreamingResponse(io.BytesIO(resp.content), media_type="audio/mpeg")
    except HTTPException:
        raise
    except Exception as e:
        print(f"HATA (voice_speak, beklenmeyen): {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="Ses üretilirken beklenmeyen bir hata oluştu.")

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
    end_reason: str = "tamamlandı"  # tamamlandı | aday_talebi | baglanti_koptu
    realtime_usage: Optional[dict] = None  # Frontend'in response.done eventlerinden topladığı token/audio usage özeti

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

MIN_L2_DURATION_SECONDS = 90  # Güvenilir rapor için asgari görüşme süresi
MIN_L2_ANSWERED_COUNT = 3  # En az üç gerçek aday cevabı olmadan puan/ret üretme

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
    if (candidate["level"] or 1) != 2:
        raise HTTPException(status_code=400, detail="Bu uç nokta sadece Level 2 adaylar için geçerlidir.")
    if not (candidate["cv_text"] and len(candidate["cv_text"].strip()) > 20):
        raise HTTPException(status_code=400, detail="Bu seviyedeki mülakata başlamadan önce CV yüklemeniz gerekiyor.")

    depth_tier = (candidate["depth_tier"] if "depth_tier" in candidate.keys() else "standart") or "standart"
    depth_cfg = get_effective_level_config(2, depth_tier)

    # BUG FIX (started_at): interviews satırı önceden sadece ilk heartbeat (/api/realtime/sync,
    # 25sn'de bir) ya da hiç heartbeat gelmezse finalize (/api/realtime/report) anında oluşuyordu.
    # started_at kolonu DEFAULT CURRENT_TIMESTAMP olduğu için satır geç oluşursa gerçek mülakat
    # süresi (dakikalar) kayboluyor, DB'de birkaç saniyeymiş gibi görünüyordu. Artık oturum
    # (WebRTC bağlantısı) kurulur kurulmaz satır burada, gerçek başlangıç anında oluşturuluyor.
    db2 = get_db()
    existing_interview = db2.execute(
        "SELECT completed_at FROM interviews WHERE candidate_id=? AND level=2", (candidate_id,)
    ).fetchone()
    if not existing_interview:
        db2.execute(
            "INSERT INTO interviews (candidate_id, level, messages, depth_tier) VALUES (?, 2, '[]', ?)",
            (candidate_id, depth_tier)
        )
        db2.commit()
    elif not existing_interview["completed_at"]:
        # Satır zaten var ama tamamlanmamış (örn. sayfa yenilendi, yeniden bağlanıldı) —
        # started_at'i EZME; ilk gerçek başlangıç zaten kayıtlı kalsın.
        pass
    db2.close()

    pos_for_criteria = get_position(candidate["position"]) or {"criteria": [{"name": "Genel Yetkinlik", "weight": 100, "desc": ""}]}
    criteria_names_list = [c["name"] for c in pos_for_criteria["criteria"]]

    instructions = build_l2_realtime_instructions(
        candidate["position"], candidate["name"], candidate["cv_text"], candidate["ai_note"], candidate["interview_language"] or "tr", depth_tier
    )

    session_body = {
        "session": {
            "type": "realtime",
            "model": OPENAI_REALTIME_MODEL,
            "instructions": instructions,
            "truncation": {
                "type": "retention_ratio",
                "retention_ratio": 0.8,
                "token_limits": {"post_instructions": 1800}
            },
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
                        "eagerness": "low",
                        "create_response": True,
                        "interrupt_response": True
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
            }]
        }
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://api.openai.com/v1/realtime/client_secrets",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                json=session_body
            )
        if resp.status_code != 200:
            print(f"HATA (OpenAI Realtime session): {resp.status_code} {resp.text[:400]}")
            raise HTTPException(status_code=502, detail="Sesli mülakat oturumu oluşturulamadı (OpenAI Realtime hatası).")
        result = resp.json()
        log_ai_provider(2, "openai", "realtime_session")
        return {
            "client_secret": result.get("value"),
            "model": OPENAI_REALTIME_MODEL,
            "turn_detection": session_body["session"]["audio"]["input"]["turn_detection"],
            "depth_tier": depth_tier,
            "coverage_threshold": depth_cfg["coverage_threshold"],
            "criteria_names": criteria_names_list
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"HATA (create_realtime_session, beklenmeyen): {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="Sesli mülakat oturumu başlatılırken beklenmeyen bir hata oluştu.")


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
    if not candidate or (candidate["level"] or 1) != 2:
        db.close()
        return {"ok": False}
    interview = db.execute("SELECT completed_at FROM interviews WHERE candidate_id=? AND level=2", (effective_candidate_id,)).fetchone()
    if interview and interview["completed_at"]:
        # Zaten finalize edilmiş bir görüşmeye geç kalan bir heartbeat gelmiş olabilir; sessizce yoksay.
        db.close()
        return {"ok": True, "already_completed": True}
    if not interview:
        db.execute("INSERT INTO interviews (candidate_id, level, messages) VALUES (?, 2, '[]')", (effective_candidate_id,))
        db.commit()
    db.close()

    if data.transcript:
        db2 = get_db()
        save_interview_state(db2, effective_candidate_id, [{"role": "user", "content": data.transcript}], 2)
        db2.commit(); db2.close()

    if data.usage_delta:
        record_realtime_usage_summary(effective_candidate_id, 2, OPENAI_REALTIME_MODEL, data.usage_delta, action="realtime_heartbeat")

    return {"ok": True}

def get_interview_usage_cost(candidate_id: int, level: int = 2) -> float:
    try:
        db = get_db()
        row = db.execute("SELECT COALESCE(SUM(estimated_cost_usd),0) AS total FROM ai_usage_logs WHERE candidate_id=? AND level=?", (candidate_id, level)).fetchone()
        db.close()
        return float(row["total"] or 0)
    except Exception:
        return 0.0


def finalize_incomplete_interview(candidate_id: int, report: str, terminated_reason: Optional[str] = None, level: int = 2):
    """Teknik/erken biten görüşmede sahte 0 puan ve Reddet üretmez."""
    db = get_db()
    candidate = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    standard_cv = f"AD SOYAD: {candidate['name'] if candidate else '-'}\nPOZİSYON: {candidate['position'] if candidate else '-'}\nMÜLAKAT NOTU: Görüşme tamamlanamadığı için puanlama yapılmadı."
    db.execute("""
        UPDATE interviews SET report=?, standard_cv=?, score=NULL, recommendation='Değerlendirilemedi', completed_at=CURRENT_TIMESTAMP
        WHERE candidate_id=? AND level=? AND completed_at IS NULL
    """, (report, standard_cv, candidate_id, level))
    if candidate and (candidate["level"] or 1) == level:
        db.execute("UPDATE candidates SET status='completed', completed_at=CURRENT_TIMESTAMP, terminated_reason=? WHERE id=?", (terminated_reason, candidate_id))
    db.commit(); db.close()
    if candidate:
        send_report_email(candidate["name"], candidate["position"], report, None, "Değerlendirilemedi", standard_cv, terminated_reason)
    return {"message": "Mülakat tamamlandı. Yeterli veri oluşmadığı için puanlama yapılmadı.", "completed": True, "score": None, "recommendation": "Değerlendirilemedi"}


@app.post("/api/realtime/report")
async def create_l2_report(data: RealtimeReportRequest, payload=Depends(verify_token)):
    if payload.get("role") != "candidate":
        raise HTTPException(status_code=403, detail="Yetkisiz")

    effective_candidate_id = int(payload.get("candidate_id") or data.candidate_id)

    db = get_db()
    candidate = db.execute("SELECT * FROM candidates WHERE id=?", (effective_candidate_id,)).fetchone()
    if not candidate:
        db.close()
        raise HTTPException(status_code=404, detail="Aday bulunamadı")
    if (candidate["level"] or 1) != 2:
        db.close()
        raise HTTPException(status_code=400, detail="Bu uç nokta sadece Level 2 adaylar için geçerlidir.")

    interview = db.execute("SELECT * FROM interviews WHERE candidate_id=? AND level=2", (effective_candidate_id,)).fetchone()
    if not interview:
        db.execute("INSERT INTO interviews (candidate_id, level, messages) VALUES (?, 2, '[]')", (effective_candidate_id,))
        db.commit()
        interview = db.execute("SELECT * FROM interviews WHERE candidate_id=? AND level=2", (effective_candidate_id,)).fetchone()

    # İDEMPOTENCY GUARD: bu mülakat zaten finalize edilmişse (retry, çift tıklama, ağ hatası
    # sonrası tekrar deneme vb.), OpenAI'a tekrar rapor ürettirmeden var olan sonucu dön.
    # Bu olmadan her retry hem GPT-4o'yu tekrar çağırıyor hem de usage log'unu çift yazıyordu.
    if interview and interview["completed_at"]:
        db.close()
        log_ai_provider(2, "openai", "report_request_deduped_already_completed")
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
        record_realtime_usage_summary(effective_candidate_id, 2, OPENAI_REALTIME_MODEL, data.realtime_usage, action="realtime_final_frontend")
    db = get_db()
    # Transkripti (rapor/PDF görüntüleme ve gelecekteki debug için) kaydet.
    save_interview_state(db, effective_candidate_id, [{"role": "user", "content": data.transcript}], 2)
    db.commit()
    db.close()

    below_minimum = data.duration_seconds < MIN_L2_DURATION_SECONDS and data.answered_count < MIN_L2_ANSWERED_COUNT

    if below_minimum or data.end_reason in ("aday_talebi", "baglanti_koptu", "uygunsuz_davranis"):
        if data.end_reason == "uygunsuz_davranis":
            reason_text = "Mülakat, profesyonel görüşme kurallarına uyulmadığı için sonlandırılmıştır; yeterli değerlendirme verisi oluşmamıştır."
        elif data.end_reason in ("aday_talebi", "baglanti_koptu"):
            reason_text = "Mülakat tamamlanmadığı için değerlendirme oluşturulamamıştır."
        else:
            reason_text = "Bu mülakat sonucunda aday hakkında güvenilir bir değerlendirme oluşturabilecek yeterli veri elde edilememiştir. Bu nedenle ayrıntılı rapor oluşturulmamıştır."
        log_ai_provider(2, "openai", "report_skipped_insufficient_data")
        report = f"Aday: {candidate['name']}\nPozisyon: {candidate['position']}\n\nSONUÇ: DEĞERLENDİRİLEMEDİ\n\n{reason_text} Adayın söylemediği hiçbir bilgi eklenmemiş ve otomatik ret kararı verilmemiştir."
        return finalize_incomplete_interview(effective_candidate_id, report, terminated_reason=None if data.end_reason == "tamamlandı" else ("Uygunsuz davranış nedeniyle sonlandırıldı" if data.end_reason == "uygunsuz_davranis" else "Aday talebiyle/bağlantı sorunuyla erken sonlandırıldı"), level=2)

    if not OPENAI_API_KEY:
        log_ai_provider(2, "openai", "report_missing_api_key_fallback")
        reply = build_l2_short_report(candidate["name"], candidate["position"], "OPENAI_API_KEY tanımlı olmadığı için yedek rapor oluşturuldu. Transkript kaydedildi; yönetici transkripti ayrıca incelemelidir.")
        return finalize_interview(effective_candidate_id, reply, terminated_reason=None, level=2)

    pos = get_position(candidate["position"]) or {"category": "Genel", "criteria": [{"name": "Genel Yetkinlik", "weight": 100, "desc": ""}]}
    criteria_text = build_criteria_text(pos["criteria"])
    table_template = build_criteria_table_template(pos["criteria"])
    total_weight = sum(c["weight"] for c in pos["criteria"])
    report_lang = LANGUAGE_NAMES.get(candidate["report_language"] or "tr", "Türkçe")
    # BUG FIX: bu prompt daha önce sadece transkripti görüyordu, adayın CV'sini hiç görmüyordu —
    # "CV ↔ pozisyon uyumu" ve "CV Tutarlılığı" alanları bu yüzden L1/L3'e göre çok daha zayıf
    # kalıyordu (model kıyaslayacak CV metnine erişemiyordu). L1/L3'teki gibi CV burada da veriliyor.
    cv_for_report = candidate["cv_text"][:1200] if candidate["cv_text"] and len(candidate["cv_text"].strip()) > 20 else "CV yüklenmemiş; sadece transkripte göre değerlendir."
    # BUG FIX: ai_note (adminin adaya özel bağlayıcı talimatı) daha önce bu rapor promptuna
    # hiç verilmiyordu — bu yüzden L2 raporlarında L1/L3'te var olan "AI Notuna Uyum" alanı
    # hiç üretilemiyordu (model notun ne olduğunu bilmiyordu). Artık veriliyor.
    ai_note_section = ""
    ai_note_report_field = ""
    if candidate["ai_note"] and candidate["ai_note"].strip():
        ai_note_section = f"\n\nADAY ÖZEL AI NOTU (bu mülakatta bu konuya öncelik verilmiş olmalı, transkriptte nasıl ele alındığını değerlendir):\n{candidate['ai_note'].strip()[:1200]}"
        ai_note_report_field = "\n**AI Notuna Uyum:** (bu adaya özel notun transkriptte nasıl ele alındığını somut olarak yaz: hangi soru/turlarda test edildi, sonucu ne oldu)"

    report_prompt = f"""Aşağıda bir sesli iş mülakatının transkripti var. Bu transkripti değerlendirip rapor üret.

Aday: {candidate['name']}
Pozisyon: {candidate['position']}
Kriterler ({total_weight} puan):
{criteria_text}

ADAYIN CV'Sİ (tutarlılık ve CV↔pozisyon uyum kontrolü için kullan):
{cv_for_report}{ai_note_section}

TRANSKRIPT:
{data.transcript[:10000]}

KURAL: Rapor {report_lang} dilinde yazılacak. Profesyonel, kanıta dayalı ve karar destek raporu üret.
- Yalnızca “Aday:” satırlarında adayın söylediği bilgiler kanıttır. “Mülakatçı:” satırındaki açıklama, ipucu veya konu anlatımı adaya ait bilgi/başarı sayılamaz.
- Adayın söylemediği hiçbir deneyimi, beceriyi, eğitim bilgisini, motivasyonu veya sonucu uydurma.
- Her puanın gerekçesi adayın somut cevabına dayanmalı. Kanıt yoksa o kriter için “yeterli kanıt yok” yaz ve puanı düşük/boş tut.
- Mülakatçının rol dışına çıkıp bilgi anlattığı bölümleri raporda “mülakat kalitesi riski” olarak belirt; adaya olumlu ya da olumsuz puan yazma.
- Çelişki varsa yalnızca gerçekten iki aday ifadesi çelişiyorsa yaz.
- Güvenilir değerlendirme için veri yetersizse puan/öneri uydurma; [DEĞERLENDİRİLEMEDİ] yaz.
Daima TAM FORMAT kullan.

TAM FORMAT:
[MÜLAKATBİTTİ]
---RAPOR---
**Aday:** {candidate['name']}
**Pozisyon:** {candidate['position']}
**Tarih:** {datetime.now().strftime('%d.%m.%Y')}

**TOPLAM PUAN: XX/{total_weight}**

{table_template}

**Tutarlılık / Çelişki Analizi:** ...
**Güçlü Yönler:** ...
**Gelişim Alanları:** ...
**Proje/Deneyim Özeti:** (transkriptte geçen somut proje/deneyimlerin kısa özeti)
**CV Tutarlılığı:** (yukarıdaki CV ile transkriptte anlatılanlar arasındaki uyum/uyumsuzluk; CV↔pozisyon uyumunu da burada değerlendir)
**Serbest Gözlemler:** ... (kriter dışı sinyaller; yoksa "Belirtilecek bir gözlem yok" yaz)
**Genel Kanı:** ...{ai_note_report_field}
**Öneri:** İşe Al / Değerlendirmeye Al / Reddet
---RAPORSON---

---STANDARTCV---
**AD SOYAD:** {candidate['name']}
**POZİSYON:** {candidate['position']}
**EĞİTİM:** ... (CV'den veya transkriptten)
**DENEYİM:** ... (kronolojik kısa özet)
**TEKNİK YETKİNLİKLER:** ...
**DİL BECERİLERİ:** ... (bilinmiyorsa "Belirtilmedi" yaz)
**MÜLAKAT NOTU:** ... (puan ve önerinin 1-2 cümlelik özeti)
---STANDARTCVSON---

EK RAPOR KALİTE KURALLARI:
- Kriter tablosunu mutlaka doldur; her satırda transkriptten somut bir gerekçe ver.
- CV ↔ pozisyon uyumunu "CV Tutarlılığı" alanında ayrıca ve açıkça değerlendir — bu alanı boş/genel geçme.
- ---STANDARTCV--- bloğunu da MUTLAKA doldur — bu, admin panelinde adayın standart özet CV'si olarak gösterilir, boş/placeholder bırakılamaz.
- Riskleri sert ama adil yaz.
- Kanıt azsa zorla puan verme. Adayın en az üç anlamlı cevabı yoksa [DEĞERLENDİRİLEMEDİ] üret.
- Çıktı mutlaka [MÜLAKATBİTTİ] ve ---RAPOR--- bloklarıyla başlasın."""

    # Toplam mülakat bütçesi 0,25 USD. Realtime için 0,20 USD, rapor için yaklaşık 0,05 USD ayrılır.
    # Kayıtlı kullanım zaten sınırı aşmışsa yeni ücretli rapor çağrısı yapma.
    current_cost = get_interview_usage_cost(effective_candidate_id, 2)
    if current_cost >= 0.235:
        log_ai_provider(2, "openai", "report_skipped_budget_limit")
        report = f"Aday: {candidate['name']}\nPozisyon: {candidate['position']}\n\nSONUÇ: DEĞERLENDİRİLEMEDİ\n\nMaliyet güvenlik sınırı nedeniyle ek rapor çağrısı yapılmadı. Transkript yönetici incelemesine sunulmuştur."
        return finalize_incomplete_interview(effective_candidate_id, report, terminated_reason="Maliyet güvenlik sınırı", level=2)

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                json={"model": OPENAI_REPORT_MODEL, "messages": [{"role": "user", "content": report_prompt}], "max_tokens": 2600, "temperature": 0.1}
            )
        if resp.status_code != 200:
            print(f"HATA (OpenAI rapor üretimi): model={OPENAI_REPORT_MODEL} status={resp.status_code} body={resp.text[:1200]}")
            raise HTTPException(status_code=502, detail="Rapor üretilemedi (OpenAI hatası).")
        result = resp.json()
        record_openai_chat_usage(effective_candidate_id, 2, OPENAI_REPORT_MODEL, "l2_report_generation", result)
        reply = result["choices"][0]["message"]["content"]
        log_ai_provider(2, "openai", "report_generated")
    except HTTPException as e:
        print(f"HATA (create_l2_report, OpenAI): {getattr(e, 'detail', e)}")
        # Rapor üretimi patlasa bile mülakat zinciri kırılmasın: aday tamamlandı, admin tamamlandı, mail yedek raporla gitsin.
        reply = build_l2_short_report(candidate["name"], candidate["position"], "OpenAI rapor üretimi sırasında hata oluştu; transkript kaydedildi ve yedek rapor oluşturuldu. Yönetici transkripti ayrıca incelemelidir.")
        return finalize_interview(effective_candidate_id, reply, terminated_reason=None, level=2)
    except Exception as e:
        print(f"HATA (create_l2_report, beklenmeyen): {type(e).__name__}: {e}")
        reply = build_l2_short_report(candidate["name"], candidate["position"], "Rapor üretimi sırasında beklenmeyen hata oluştu; transkript kaydedildi ve yedek rapor oluşturuldu. Yönetici transkripti ayrıca incelemelidir.")
        return finalize_interview(effective_candidate_id, reply, terminated_reason=None, level=2)

    return finalize_interview(effective_candidate_id, reply, level=2)

@app.get("/api/admin/snapshots/{candidate_id}")
def get_snapshots(candidate_id: int, payload=Depends(verify_admin)):
    db = get_db()
    rows = db.execute(
        "SELECT id, image_base64, captured_at FROM snapshots WHERE candidate_id=? ORDER BY captured_at ASC",
        (candidate_id,)
    ).fetchall()
    db.close()
    return [{"id": r["id"], "image_base64": r["image_base64"], "captured_at": r["captured_at"]} for r in rows]


# ---- PDF Report ----
def _clean_pdf_text(value):
    return (value or "").replace("**", "").replace("---", "").strip()

def _make_report_pdf(candidate: dict, interview: dict, snapshots: list):
    try:
        from reportlab.lib import colors as rl_colors
        from reportlab.lib.enums import TA_CENTER
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak
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
        regular = next((f for f in candidates if os.path.exists(f)), None)
        bold = next((f for f in bold_candidates if os.path.exists(f)), None)
        if regular:
            pdfmetrics.registerFont(TTFont("MedeXFont", regular))
            pdfmetrics.registerFont(TTFont("MedeXFont-Bold", bold or regular))
            return "MedeXFont", "MedeXFont-Bold"

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
                return "MedeXFont", "MedeXFont-Bold"
        except Exception as e:
            print(f"UYARI: PDF fontu yüklenemedi: {e}")

        print("UYARI: Unicode PDF fontu bulunamadı; Türkçe karakterler bozulabilir.")
        return "Helvetica", "Helvetica-Bold"

    font_regular, font_bold = register_unicode_font()

    def ptxt(value):
        return xml_escape(str(value if value is not None else "-"))

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=1.4*cm, leftMargin=1.4*cm, topMargin=1.2*cm, bottomMargin=1.1*cm)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="BrandTitle", parent=styles["Title"], fontName=font_bold, fontSize=23, leading=28, textColor=rl_colors.HexColor("#1e3a5f"), alignment=TA_CENTER, spaceAfter=6))
    styles.add(ParagraphStyle(name="Subtitle", parent=styles["BodyText"], fontName=font_regular, fontSize=9, leading=12, textColor=rl_colors.HexColor("#64748b"), alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="Section", parent=styles["Heading2"], fontName=font_bold, fontSize=13, leading=16, textColor=rl_colors.HexColor("#1e3a5f"), spaceBefore=12, spaceAfter=8))
    styles.add(ParagraphStyle(name="Small", parent=styles["BodyText"], fontName=font_regular, fontSize=8, leading=10, textColor=rl_colors.HexColor("#64748b")))
    styles.add(ParagraphStyle(name="BodyWrap", parent=styles["BodyText"], fontName=font_regular, fontSize=9.2, leading=12.5, textColor=rl_colors.HexColor("#0f172a"), wordWrap="CJK"))
    styles.add(ParagraphStyle(name="Metric", parent=styles["BodyText"], fontName=font_bold, fontSize=18, leading=22, alignment=TA_CENTER, textColor=rl_colors.HexColor("#1e3a5f")))

    story = []
    story.append(Paragraph("MedeX AI Interview Report", styles["BrandTitle"]))
    story.append(Paragraph("Aday mülakat değerlendirme raporu", styles["Subtitle"]))
    story.append(Spacer(1, 10))

    score = interview.get("score")
    score_display = "-" if score is None else f"{score}/100"
    recommendation = normalize_recommendation(score or 0, interview.get("recommendation")) if score is not None else (interview.get("recommendation") or "-")

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

    info = [
        ["Aday", candidate.get("name") or "-", "Pozisyon", candidate.get("position") or "-"],
        ["E-posta", candidate.get("email") or "-", "Telefon", candidate.get("phone") or "-"],
        ["Eğitim", candidate.get("education") or "-", "Deneyim", str(candidate.get("experience_years") or 0) + " yıl"],
        ["Üniversite", candidate.get("university") or "-", "Bölüm", candidate.get("department") or "-"],
        ["Başlangıç", interview.get("started_at") or "-", "Tamamlanma", interview.get("completed_at") or "-"],
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

    if candidate.get("terminated_reason"):
        story.append(Spacer(1, 8))
        story.append(Paragraph(f"<b>İhlal/Sonlandırma:</b> {ptxt(candidate.get('terminated_reason'))}", styles["BodyWrap"]))

    report_text = strip_markdown(interview.get("report")) or "Rapor bulunamadı."
    story.append(Paragraph("AI Değerlendirme Raporu", styles["Section"]))
    lines = [ln.rstrip() for ln in report_text.split("\n") if ln.strip()]
    table_rows, table_consumed = parse_markdown_table(lines)
    if table_rows:
        # Önce tablo dışı üst satırları yaz, sonra kriter tablosunu gerçek tablo yap.
        for idx, line in enumerate(lines):
            if idx in table_consumed:
                continue
            if line.startswith("|"):
                continue
            is_heading = line.endswith(":") or line.startswith("TOPLAM PUAN") or line.startswith("Öneri:")
            story.append(Paragraph(("<b>" + ptxt(line) + "</b>") if is_heading else ptxt(line), styles["BodyWrap"]))
            story.append(Spacer(1, 3))
        # Yalnızca kriter tablosuna benzeyen satırları tabloya al.
        clean_rows = []
        for row in table_rows:
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
        for para in lines:
            is_heading = para.endswith(":") or para.startswith("TOPLAM PUAN") or para.startswith("Öneri:")
            story.append(Paragraph(("<b>" + ptxt(para) + "</b>") if is_heading else ptxt(para), styles["BodyWrap"]))
            story.append(Spacer(1, 3))

    if interview.get("standard_cv"):
        story.append(Paragraph("Standart CV Özeti", styles["Section"]))
        for para in strip_markdown(interview.get("standard_cv")).split("\n"):
            if para.strip():
                story.append(Paragraph(ptxt(para.strip()), styles["BodyWrap"]))
                story.append(Spacer(1, 3))

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
                cell = [Paragraph(f"<b>Kare {idx}</b><br/><font size=7>{ptxt(snap.get('captured_at',''))}</font>", styles["Small"]), img]
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

    story.append(Spacer(1, 14))
    story.append(Paragraph(f"Bu rapor {datetime.now().strftime('%d.%m.%Y %H:%M')} tarihinde MedeX AI Interview Platform tarafından oluşturulmuştur.", styles["Small"]))

    doc.build(story)
    buffer.seek(0)
    return buffer

@app.get("/api/admin/interviews/{candidate_id}/pdf")
def download_interview_pdf(candidate_id: int, level: Optional[int] = None, payload=Depends(verify_admin)):
    db = get_db()
    candidate = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    target_level = level if level is not None else ((candidate["level"] or 1) if candidate else 1)
    interview = db.execute("SELECT * FROM interviews WHERE candidate_id=? AND level=?", (candidate_id, target_level)).fetchone()
    snapshots = db.execute("SELECT id, image_base64, captured_at FROM snapshots WHERE candidate_id=? ORDER BY captured_at ASC", (candidate_id,)).fetchall()
    db.close()
    if not candidate or not interview:
        raise HTTPException(status_code=404, detail="Rapor bulunamadı")

    # YETERSİZ VERİ / YARIM MÜLAKAT: skor yoksa ya da %20 barajının altındaysa PDF/detaylı
    # rapor üretilmez — sadece bilgilendirme mesajı döner (revizyon notu kuralı).
    pos = get_position(candidate["position"])
    total_weight = sum(c["weight"] for c in pos["criteria"]) if pos else 100
    score = interview["score"]
    if score is None or (total_weight > 0 and (score / total_weight) < 0.20):
        msg = (
            "Mülakat tamamlanmadığı için değerlendirme oluşturulamamıştır."
            if score is None
            else "Bu mülakat sonucunda aday hakkında güvenilir bir değerlendirme oluşturabilecek yeterli veri elde edilememiştir. Bu nedenle ayrıntılı rapor oluşturulmamıştır."
        )
        raise HTTPException(status_code=422, detail=msg)

    pdf = _make_report_pdf(dict(candidate), dict(interview), [dict(s) for s in snapshots])
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", candidate["name"] or "aday")
    return StreamingResponse(pdf, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename=medex_report_{safe_name}.pdf"})

# ---- Admin Report Detail ----
@app.get("/api/admin/interviews/{candidate_id}")
def get_interview(candidate_id: int, level: Optional[int] = None, payload=Depends(verify_admin)):
    db = get_db()
    if level is None:
        c = db.execute("SELECT level FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        level = (c["level"] or 1) if c else 1
    interview = db.execute("""
        SELECT i.*, c.name, c.email, c.phone, c.position, c.education, c.university, c.department, c.experience_years, c.ai_note, c.violation_count, c.terminated_reason, c.cv_filename, c.cv_text
        FROM interviews i JOIN candidates c ON i.candidate_id = c.id
        WHERE i.candidate_id = ? AND i.level = ?
    """, (candidate_id, level)).fetchone()
    usage_rows = db.execute("""
        SELECT provider, model, action, input_tokens, output_tokens, audio_input_tokens, audio_output_tokens, total_tokens, estimated_cost_usd, created_at
        FROM ai_usage_logs
        WHERE candidate_id=? AND level=?
        ORDER BY id ASC
    """, (candidate_id, level)).fetchall()
    db.close()
    if not interview:
        raise HTTPException(status_code=404, detail="Mülakat bulunamadı")
    result = dict(interview)
    result["usage_logs"] = [dict(r) for r in usage_rows]
    result["usage_total_tokens"] = sum(_safe_int(r["total_tokens"]) for r in usage_rows)
    result["usage_total_cost_usd"] = round(sum((r["estimated_cost_usd"] or 0) for r in usage_rows), 4)
    return result

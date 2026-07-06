from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List
import sqlite3
import hashlib
import secrets
import string
import os
import jwt
import anthropic
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

resend.api_key = RESEND_API_KEY
security = HTTPBearer()

# ============ DB ============
def get_db():
    conn = sqlite3.connect("medex_mulakat.db", check_same_thread=False)
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
            messages TEXT DEFAULT '[]',
            report TEXT,
            standard_cv TEXT,
            score INTEGER,
            recommendation TEXT,
            compact_memory TEXT DEFAULT '',
            question_count INTEGER DEFAULT 0,
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
        ("interviews", "level", "INTEGER DEFAULT 1"),
        ("positions", "category", "TEXT DEFAULT 'Genel'"),
        ("interviews", "standard_cv", "TEXT"),
        ("interviews", "compact_memory", "TEXT DEFAULT ''"),
        ("interviews", "question_count", "INTEGER DEFAULT 0"),
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
        return "Genel"

    # Varsayılan pozisyonları her açılışta eksikse ekle.
    # Not: Mevcut pozisyonları bozmaz; sadece isim bazlı eksik olanları tamamlar.
    defaults = [
        ("Study Coordinator (SC)",
         "Klinik araştırma merkezinde hasta ziyareti, visit takvimi, CRF/EDC ve saha koordinasyonunu yürüten rol.",
         [("Organizasyon & Takip",25,"Visit takvimi, hasta randevusu, deadline ve doküman takibi"),("Dikkat & Doğruluk",25,"EDC/CRF, kaynak veri ve protokol gerekliliklerinde hata yapmama"),("Hasta ve Ekip İletişimi",20,"Hasta, hekim, monitör ve ekip ile açık iletişim"),("Stres Toleransı",15,"Aynı anda çoklu görev ve baskı altında çalışma"),("Gizlilik & Etik",10,"Hasta verisi, KVKK/GCP ve etik farkındalık"),("Öğrenme Esnekliği",5,"Yeni protokol ve sistemlere hızlı uyum")]),
        ("Senior Study Coordinator",
         "Deneyimli saha koordinatörü; junior ekibi yönlendirir, kompleks çalışmaları ve monitör ziyaretlerini yönetir.",
         [("Klinik Operasyon Deneyimi",25,"Çoklu çalışma, protokol ve saha süreç deneyimi"),("Ekip Koordinasyonu",20,"Junior SC yönlendirme, görev dağılımı, takip"),("EDC/CRF Kalitesi",20,"Query azaltma, veri tutarlılığı ve SDV hazırlığı"),("Regülasyon & GCP",15,"ICH-GCP, KVKK, hasta onamı ve etik süreç bilinci"),("Problem Çözme",15,"Deviasyon, randevu kaçırma, lojistik ve hasta yönetimi"),("İletişim",5,"Sponsor/CRO/monitör iletişimi")]),
        ("Clinical Research Associate (CRA)",
         "Klinik araştırma sahalarını monitör eden, GCP uyumunu ve kaynak veri doğrulamasını takip eden rol.",
         [("GCP & Protokol Bilgisi",25,"ICH-GCP, protokol, ICF, SDV/SDR bilgisi"),("Monitoring Deneyimi",25,"SIV/IMV/COV, rapor ve follow-up süreçleri"),("Problem Çözme",20,"Deviasyon, query, aksiyon planı, saha sorunları"),("İletişim & Raporlama",15,"Site/sponsor/CRO iletişimi ve rapor kalitesi"),("Seyahat & Planlama",10,"Saha ziyaret planı ve zaman yönetimi"),("Teknik Sistemler",5,"EDC, CTMS, eTMF kullanımı")]),
        ("Senior CRA",
         "Kompleks çalışmalarda deneyimli monitör; saha kalitesi, risk yönetimi ve junior CRA mentörlüğü yapar.",
         [("İleri GCP & Risk Bazlı Monitoring",25,"RBM, CAPA, audit readiness"),("Kompleks Saha Deneyimi",25,"Faz, terapötik alan ve çok merkezli çalışma deneyimi"),("Mentörlük",15,"Junior CRA destekleme ve kalite kontrol"),("Raporlama Kalitesi",15,"Zamanında, net ve aksiyon odaklı raporlama"),("Kriz Yönetimi",15,"Deviasyon, SAE, hasta güvenliği, acil durumlar"),("İlişki Yönetimi",5,"KOL/site/sponsor ilişkileri")]),
        ("Clinical Trial Assistant (CTA)",
         "Klinik araştırmalarda dokümantasyon, eTMF, takip listeleri ve operasyonel destek rolü.",
         [("Dokümantasyon Düzeni",25,"TMF/eTMF, ISF, dosyalama ve versiyon takibi"),("Takip & Organizasyon",25,"Checklist, deadline ve aksiyon takibi"),("Dikkat & Doğruluk",20,"Doküman tamlığı ve veri doğruluğu"),("İletişim",15,"CRA/PM/site ile koordinasyon"),("Teknik Araçlar",10,"Excel, CTMS, eTMF"),("Öğrenme Hızı",5,"Regülatif terminolojiye uyum")]),
        ("Clinical Trial Manager (CTM)",
         "Çalışma operasyonlarını uçtan uca yöneten, timeline, bütçe, site performansı ve riskleri takip eden rol.",
         [("Proje Yönetimi",25,"Timeline, milestone, risk ve kaynak yönetimi"),("Klinik Operasyon Bilgisi",25,"Monitoring, site activation, enrollment, close-out"),("Ekip Yönetimi",20,"CRA/CTA/site ekip koordinasyonu"),("Risk & CAPA",15,"Risk tespiti, aksiyon planı ve kalite"),("Sponsor İletişimi",10,"Beklenti, raporlama ve eskalasyon"),("Finansal Farkındalık",5,"Bütçe, vendor ve maliyet takibi")]),
        ("Clinical Project Manager",
         "Klinik araştırma projelerini sponsor beklentileri, bütçe, kalite ve zaman çizelgesi içinde yöneten rol.",
         [("Proje Planlama",25,"Kapsam, timeline, bütçe, kaynak ve risk planı"),("Stakeholder Yönetimi",20,"Sponsor, vendor, site ve iç ekip iletişimi"),("Klinik Araştırma Süreçleri",20,"Startup, operasyon, close-out, kalite"),("Liderlik",15,"Ekip yönetimi ve karar alma"),("Raporlama",10,"KPI, metrik ve yönetim raporları"),("Problem Çözme",10,"Eskalasyon ve kriz yönetimi")]),
        ("Clinical Operations Manager",
         "Klinik operasyon ekibini, süreçleri, kalite metriklerini ve kaynak planlamasını yöneten rol.",
         [("Operasyonel Liderlik",25,"Ekip, kapasite ve süreç yönetimi"),("Kalite & KPI",20,"Performans metrikleri, audit readiness"),("Süreç İyileştirme",20,"SOP, standardizasyon ve verimlilik"),("Regülasyon & GCP",15,"GCP, yerel mevzuat, etik süreçler"),("Bütçe & Kaynak",10,"Kaynak, maliyet, vendor yönetimi"),("İletişim",10,"Üst yönetim ve sponsor iletişimi")]),
        ("Data Manager",
         "Klinik veri yönetimi, veri temizliği, edit check, query ve database lock süreçlerini yöneten rol.",
         [("Clinical Data Management",30,"EDC, query, edit check, data cleaning"),("Dikkat & Analitik",20,"Veri tutarlılığı, pattern görme, hata yakalama"),("Sistem Yetkinliği",15,"EDC, Excel, veri araçları"),("Regülasyon & GCP",15,"ALCOA+, audit trail, veri bütünlüğü"),("İletişim",10,"CRA/site/istatistik ekipleriyle çalışma"),("Problem Çözme",10,"Data issue ve discrepancy yönetimi")]),
        ("Clinical Data Coordinator",
         "Veri giriş kontrolleri, query takibi ve data management süreçlerine operasyonel destek veren rol.",
         [("Veri Dikkati",30,"Hata yakalama, tutarlılık ve veri kontrolü"),("EDC Kullanımı",20,"Query, form, giriş ve takip"),("Organizasyon",15,"Liste, deadline ve query takibi"),("GCP & Veri Bütünlüğü",15,"ALCOA+, audit trail farkındalığı"),("İletişim",10,"Site/CRA/DM iletişimi"),("Öğrenme",10,"Yeni sistemlere uyum")]),
        ("Medical Monitor",
         "Klinik çalışmalarda tıbbi güvenlik, uygunluk ve vaka değerlendirmesi yapan hekim rolü.",
         [("Tıbbi Değerlendirme",30,"AE/SAE, uygunluk, hasta güvenliği"),("Protokol & Klinik Bilgi",20,"Terapötik alan ve protokol hakimiyeti"),("GCP & Etik",15,"Hasta güvenliği, etik ve regülasyon"),("Karar Verme",15,"Risk-fayda, eskalasyon, medikal kararlar"),("İletişim",10,"PI, sponsor, PV ekipleri ile iletişim"),("Raporlama",10,"Medikal yorum ve dokümantasyon")]),
        ("Medical Advisor",
         "Medikal strateji, bilimsel içerik, KOL iletişimi ve klinik yorum sağlayan rol.",
         [("Bilimsel Yetkinlik",25,"Literatür, terapötik alan, klinik yorum"),("Stratejik Düşünme",20,"Medikal plan ve pozisyonlama"),("KOL İletişimi",15,"Bilimsel ilişki ve sunum becerisi"),("Regülasyon & Etik",15,"Tanıtım dışı iletişim, uyum"),("Analitik Raporlama",15,"Veri yorumlama ve içgörü"),("Ekip Çalışması",10,"Pazarlama/klinik/PV iş birliği")]),
        ("Pharmacovigilance Specialist",
         "AE/SAE, ICSR, sinyal, güvenlilik raporlaması ve farmakovijilans uyumundan sorumlu rol.",
         [("PV Süreç Bilgisi",30,"ICSR, SAE, SUSAR, zaman çizelgeleri"),("Regülasyon",20,"Yerel/uluslararası PV yükümlülükleri"),("Dikkat & Doğruluk",20,"Veri kalitesi, kodlama, raporlama"),("Tıbbi Terminoloji",10,"AE/SAE terminoloji ve klinik yorum"),("Sistem Kullanımı",10,"PV veritabanı ve Excel"),("İletişim",10,"Sponsor, site, regülatör iletişimi")]),
        ("Regulatory Affairs Specialist",
         "Etik kurul, Bakanlık/TİTCK, başvuru dosyaları ve regülatif takip süreçlerini yürüten rol.",
         [("Regülatif Bilgi",30,"Etik kurul, TİTCK, başvuru ve onay süreçleri"),("Dokümantasyon",20,"Dosya hazırlığı, versiyon ve takip"),("Takip & Organizasyon",20,"Deadline, eksik evrak, onay süreçleri"),("İletişim",10,"Kurul, sponsor, site yazışmaları"),("Dikkat",10,"Form ve doküman doğruluğu"),("Problem Çözme",10,"Eksik/ret/geri dönüş yönetimi")]),
        ("Quality Assurance (GCP QA)",
         "GCP kalite sistemi, audit, CAPA, SOP ve süreç uyumluluğunu yöneten rol.",
         [("GCP & Kalite Bilgisi",30,"ICH-GCP, SOP, audit readiness"),("Audit Yetkinliği",20,"Planlama, bulgu, raporlama"),("CAPA Yönetimi",20,"Kök neden analizi ve takip"),("Süreç İyileştirme",10,"SOP, eğitim, standardizasyon"),("İletişim",10,"Denetim iletişimi ve geri bildirim"),("Analitik Düşünme",10,"Risk bazlı kalite yaklaşımı")]),
        ("Site Manager",
         "Klinik araştırma sahasının operasyonel, insan kaynağı ve kalite yönetiminden sorumlu rol.",
         [("Saha Operasyon Yönetimi",25,"Hasta, ekip, ziyaret ve kaynak yönetimi"),("Liderlik",20,"Ekip koordinasyonu ve performans"),("Kalite & GCP",20,"Protokol, ICF, audit hazırlığı"),("İletişim",15,"PI, sponsor, CRO ve hasta iletişimi"),("Problem Çözme",10,"Operasyonel kriz yönetimi"),("Raporlama",10,"KPI ve yönetim raporları")]),
        ("Site Director",
         "Araştırma merkezinin stratejik, finansal ve operasyonel performansını yöneten üst rol.",
         [("Stratejik Liderlik",25,"Büyüme, kapasite ve portföy yönetimi"),("Operasyonel Mükemmeliyet",20,"Süreç, kalite, kaynak verimliliği"),("Finansal Yönetim",15,"Bütçe, gelir, maliyet ve karlılık"),("İş Geliştirme",15,"Sponsor/CRO ilişkileri ve fırsatlar"),("Kalite & Uyum",15,"GCP, audit, SOP"),("Ekip Yönetimi",10,"Liderlik ve kültür")]),
        ("Laboratory Technician",
         "Laboratuvar numune işleme, cihaz kullanımı, kayıt ve kalite süreçlerini yürüten teknik rol.",
         [("Teknik Laboratuvar Becerisi",30,"Numune, cihaz, analiz ve prosedür"),("Dikkat & Kayıt",25,"Etiketleme, log, dokümantasyon"),("Kalite & Güvenlik",20,"Biyogüvenlik, SOP, kalite kontrol"),("Zaman Yönetimi",10,"Numune zamanlaması ve öncelik"),("Ekip Çalışması",10,"Laboratuvar ve klinik ekip iletişimi"),("Öğrenme",5,"Yeni analiz/prosedürlere uyum")]),
        ("Laboratory Supervisor",
         "Laboratuvar ekibi, kalite, iş akışı ve cihaz/prosedür yönetiminden sorumlu rol.",
         [("Laboratuvar Yönetimi",25,"Ekip, vardiya, iş akışı"),("Kalite Sistemi",25,"QC, SOP, audit ve kayıtlar"),("Teknik Yetkinlik",20,"Cihaz, analiz, sorun giderme"),("Liderlik",15,"Ekip eğitimi ve performans"),("Güvenlik",10,"Biyogüvenlik ve risk yönetimi"),("Raporlama",5,"KPI ve stok/cihaz raporları")]),
        ("Research Scientist",
         "Bilimsel araştırma, deney tasarımı, veri analizi ve yayın/sunum üretimi yapan rol.",
         [("Bilimsel Tasarım",25,"Hipotez, metodoloji, deney planı"),("Analitik Düşünme",20,"Veri analizi ve yorumlama"),("Teknik Uzmanlık",20,"Laboratuvar/klinik yöntem bilgisi"),("Yayın & Sunum",15,"Bilimsel yazım ve sunum"),("Problem Çözme",10,"Deneysel sorunlar ve optimizasyon"),("İş Birliği",10,"Multidisipliner çalışma")]),
        ("Medical Science Liaison (MSL)",
         "KOL ilişkileri, bilimsel iletişim, saha medikal strateji ve içgörü toplama rolü.",
         [("Bilimsel Yetkinlik",25,"Terapötik alan ve literatür hakimiyeti"),("KOL İlişkileri",20,"Bilimsel iletişim ve güven oluşturma"),("Sunum Becerisi",15,"Bilimsel sunum ve tartışma"),("Uyum & Etik",15,"Tanıtım dışı medikal iletişim"),("İçgörü Toplama",15,"Saha içgörüsü ve raporlama"),("Planlama",10,"Saha planı ve önceliklendirme")]),
        ("Medical Representative",
         "Saha tanıtım, hekim ilişkileri, ürün bilgisi ve satış hedeflerinden sorumlu rol.",
         [("Ürün & Pazar Bilgisi",25,"Ürün, rakip ve pazar hakimiyeti"),("İletişim & İkna",25,"Hekim iletişimi ve güven"),("Planlama",15,"Ziyaret planı ve territory yönetimi"),("Etik & Uyum",15,"Tanıtım kuralları ve uyum"),("Sonuç Odaklılık",10,"Hedef takibi ve aksiyon"),("Raporlama",10,"CRM ve ziyaret raporları")]),
        ("Product Specialist",
         "Ürün uzmanlığı, saha/ekip eğitimi, ürün konumlandırma ve teknik destek sağlayan rol.",
         [("Ürün Uzmanlığı",30,"Teknik ve klinik ürün bilgisi"),("Eğitim & Sunum",20,"Ekip/müşteri eğitimi"),("Pazar Analizi",15,"Rakip, ihtiyaç, konumlandırma"),("İletişim",15,"Saha ve müşteri desteği"),("Problem Çözme",10,"Teknik/klinik soru yönetimi"),("Raporlama",10,"Geri bildirim ve içgörü")]),
        ("CTO", "Teknoloji stratejisi, mimari, ekip ve ürün geliştirme süreçlerinden sorumlu üst düzey teknoloji lideri.", [("Teknik Strateji",25,"Mimari, ölçeklenebilirlik, teknoloji seçimi"),("Liderlik",25,"Ekip kurma, mentorluk, performans"),("Ürün & İş Anlayışı",20,"Teknolojiyi iş hedefleriyle hizalama"),("Güvenlik & Kalite",15,"Security, code quality, DevOps"),("Problem Çözme",10,"Kritik teknik kararlar"),("İletişim",5,"Yönetim ve ekip iletişimi")]),
        ("Software Developer", "Yazılım geliştirme, test, bakım ve teknik problem çözme rolü.", [("Kodlama Yetkinliği",30,"Temiz kod, algoritma, framework bilgisi"),("Problem Çözme",25,"Analitik düşünme ve debug"),("Test & Kalite",15,"Unit test, hata önleme"),("Takım Çalışması",15,"Git, code review, iletişim"),("Öğrenme",10,"Yeni teknolojiye uyum"),("Dokümantasyon",5,"Anlaşılır teknik dokümantasyon")]),
        ("Full Stack Developer", "Frontend ve backend geliştirmeyi birlikte yürüten yazılım geliştirici rolü.", [("Backend Yetkinliği",25,"API, veri modeli, iş mantığı"),("Frontend Yetkinliği",25,"UI, state, responsive yapı"),("Veritabanı",15,"SQL, performans, modelleme"),("DevOps Bilinci",10,"Deploy, env, log, monitoring"),("Problem Çözme",15,"Debug ve entegrasyon sorunları"),("Takım Çalışması",10,"İletişim, Git, review")]),
        ("Backend Developer", "API, veritabanı, entegrasyon ve sunucu tarafı mimari geliştirme rolü.", [("API Tasarımı",25,"REST, auth, validation"),("Veritabanı",25,"SQL, modelleme, performans"),("Güvenlik",15,"Auth, input validation, secrets"),("Performans",10,"Caching, query optimizasyonu"),("Test & Debug",15,"Hata analizi ve test"),("DevOps",10,"Deploy ve log yönetimi")]),
        ("Frontend Developer", "Kullanıcı arayüzü, deneyim, state ve tarayıcı tarafı geliştirme rolü.", [("React/UI Yetkinliği",30,"Component, state, routing"),("UX & Responsive",20,"Kullanılabilirlik ve mobil uyum"),("API Entegrasyonu",15,"Hata yönetimi ve async akış"),("Performans",10,"Bundle, render, optimizasyon"),("Test & Debug",15,"Console, browser uyumu"),("Tasarım Dikkati",10,"Görsel tutarlılık")]),
        ("DevOps Engineer", "CI/CD, bulut, deploy, izleme, güvenlik ve altyapı otomasyonundan sorumlu rol.", [("CI/CD",25,"Pipeline, release, rollback"),("Cloud & Container",25,"Docker, cloud servisleri"),("Monitoring",15,"Log, metric, alert"),("Security",15,"Secrets, network, hardening"),("Automation",10,"IaC ve script"),("Problem Çözme",10,"Incident response")]),
        ("QA Engineer", "Test planı, manuel/otomasyon test, kalite süreçleri ve hata yönetiminden sorumlu rol.", [("Test Tasarımı",25,"Test case, senaryo, edge case"),("Otomasyon",20,"Test araçları ve scripting"),("Hata Analizi",20,"Bug yazımı, reproduce, takip"),("Ürün Anlayışı",15,"Kullanıcı akışı ve gereksinim"),("İletişim",10,"Geliştirici/PM iletişimi"),("Dikkat",10,"Detay ve kalite odağı")]),
        ("Project Manager", "Proje planlama, ekip koordinasyonu, risk, zaman ve paydaş yönetiminden sorumlu rol.", [("Planlama",25,"Scope, timeline, kaynak"),("Risk Yönetimi",20,"Risk, issue, aksiyon"),("İletişim",20,"Paydaş, ekip, raporlama"),("Liderlik",15,"Ekip motivasyonu ve karar"),("Bütçe",10,"Maliyet ve kaynak"),("Araç Kullanımı",10,"Jira, MS Project, raporlama")]),
        ("Product Manager", "Ürün vizyonu, roadmap, kullanıcı ihtiyacı ve iş önceliklendirme rolü.", [("Ürün Stratejisi",25,"Vizyon, roadmap, öncelik"),("Kullanıcı Anlayışı",20,"Araştırma, ihtiyaç, UX"),("Analitik",15,"Metric, funnel, karar"),("Teknik İletişim",15,"Geliştirici ekip ile uyum"),("Stakeholder Yönetimi",15,"İş birimleri ve yönetim"),("Problem Çözme",10,"Trade-off ve karar")]),
        ("Business Analyst", "İş gereksinimlerini analiz eden, süreç modelleyen ve teknik ekibe aktaran rol.", [("Analiz Yetkinliği",25,"Gereksinim, süreç, use-case"),("Dokümantasyon",20,"BRD, user story, acceptance criteria"),("İletişim",20,"Kullanıcı ve teknik ekip arası köprü"),("Süreç Modelleme",15,"BPMN, akış, veri"),("Problem Çözme",10,"Kök neden ve çözüm"),("Test Desteği",10,"UAT ve doğrulama")]),
        ("HR Specialist", "İşe alım, çalışan ilişkileri, eğitim, performans ve insan kaynakları operasyonları rolü.", [("İşe Alım",25,"Aday tarama, mülakat, süreç"),("İletişim",20,"Çalışan ve yönetici iletişimi"),("Organizasyon",15,"Takip, kayıt, süreç"),("Mevzuat & Uyum",15,"İş hukuku ve politika"),("Analitik",10,"HR metrikleri"),("Gizlilik",15,"Kişisel veri ve etik")]),
        ("Finance Specialist", "Finansal kayıt, raporlama, bütçe, ödeme ve mali kontrol süreçlerinden sorumlu rol.", [("Finansal Bilgi",25,"Muhasebe, bütçe, raporlama"),("Dikkat & Doğruluk",25,"Hata önleme ve kontrol"),("Analitik",20,"Veri analizi ve yorum"),("Araç Kullanımı",10,"Excel/ERP"),("Uyum",10,"Vergi, mevzuat, iç kontrol"),("İletişim",10,"Ekip ve yönetim iletişimi")]),
        ("Sales Manager", "Satış hedefleri, ekip, müşteri ilişkileri ve gelir büyümesinden sorumlu rol.", [("Satış Stratejisi",25,"Hedef, segment, pipeline"),("Ekip Yönetimi",20,"Koçluk ve performans"),("Müşteri İlişkileri",20,"Güven, müzakere, çözüm"),("Analitik",15,"CRM, forecast, KPI"),("Sonuç Odaklılık",10,"Hedef takibi"),("İletişim",10,"Sunum ve ikna")]),
        ("Marketing Manager", "Pazarlama stratejisi, kampanya, marka, içerik ve performans yönetimi rolü.", [("Strateji",25,"Pazar, hedef kitle, konumlandırma"),("Kampanya Yönetimi",20,"Planlama, uygulama, optimizasyon"),("Dijital Pazarlama",15,"SEO, ads, sosyal medya"),("Analitik",15,"Metric, ROI, raporlama"),("Yaratıcılık",15,"İçerik ve mesaj"),("İletişim",10,"Ekip ve ajans yönetimi")]),
    ]
    for name, desc, criteria_pairs in defaults:
        criteria = [{"name": n, "weight": w, "desc": d} for n, w, d in criteria_pairs]
        category = infer_position_category(name)
        conn.execute(
            "INSERT OR IGNORE INTO positions (name, category, role_description, criteria_json) VALUES (?, ?, ?, ?)",
            (name, category, desc, json.dumps(criteria, ensure_ascii=False))
        )
        conn.execute("UPDATE positions SET category=? WHERE name=? AND (category IS NULL OR category='' OR category='Genel')", (category, name))
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

def cached_system(system_text: str) -> list:
    """Maliyet optimizasyonu: sistem prompt'u (felsefe+kurallar+kriterler+CV) her
    mülakat turunda aynı kalıyor ama her turda yeniden gönderiliyor. Anthropic'in
    prompt caching özelliğiyle bu sabit metin bir kez "cache"lenir, sonraki turlarda
    tam fiyat yerine düşürülmüş cache-hit fiyatı ödenir. Davranış/mantık DEĞİŞMEZ,
    sadece aynı sistem promptu tekrar gönderildiğinde maliyeti düşürür."""
    return [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]

def get_system_prompt(position_name: str, candidate_name: str, cv_text: Optional[str] = None, ai_note: Optional[str] = None, education: Optional[str] = None, university: Optional[str] = None, department: Optional[str] = None, experience_years: Optional[int] = None, level: Optional[int] = 1) -> str:
    pos = get_position(position_name)
    if not pos:
        pos = {"category": "Genel", "role_description": "Genel pozisyon", "criteria": [
            {"name": "Genel Yetkinlik", "weight": 100, "desc": "Genel değerlendirme"}
        ]}

    lvl_cfg = get_level_config(level)
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

    return f"""Sen MedeX AI mülakat uzmanısın. Dil Türkçe. Aday: {candidate_name}. Pozisyon: {position_name}. Kategori: {category}.

TEMEL FELSEFE (her kararında bunu esas al):
Bu mülakatın amacı adayı elemek değil, iyi/yetkin adayı gerçekten yakalamaktır. Sahada güçlü çalışan çoğu insan mülakat ortamında (heyecan, format kafası karışıklığı, soru net değilse ne istendiğini anlamama) düşük performans gösterebilir. Bir cevabı yetersiz sayıp geçmeden önce, bunun gerçek bir yetkinlik eksikliği mi yoksa mülakatın kendi eksikliği (belirsiz soru, ilk denemede tam anlaşılamama) mi olduğunu ayır. Amaç eleme değildir ama gerçek eleme de gerektiğinde yapılır — sadece yanlış nedenle (kısa cevap, ilk seferde anlaşılamama) elemeye düşülmez.

Rol: {pos['role_description']}
Kriterler ({total_weight} puan):
{criteria_text}
{candidate_profile}
{cv_section}
{admin_instruction}

SEVİYE TALİMATI: {lvl_cfg["tone"]}

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

SERBEST GÖZLEM (kriter dışı, skora karışmaz):
- Kriter listesinde olmayan ama fark ettiğin bir sinyal varsa (örn. tepki hızında/kavramada beklenmedik bir gecikme, bağlam kaybı) bunu netleştirmek için serbest bir soru sorabilirsin. Bu gözlemi rapordaki "Serbest Gözlemler" alanına yaz, kriter puanına dahil etme — ham gözlem olarak insan değerlendirsin.

GENEL:
- Cevapları CV ile tutarlılık, teknik seviye, deneyim, analitik düşünme ve dürüstlük açısından değerlendir.
- Mesajın başına mutlaka [SÜRE:XX] koy: kısa 45-60, senaryo 75-100, kritik soru 90-120.
- Mülakatı bitirmeden önce, GÖREV satırı bitirmeni söylediğinde son soru olarak şunu sor: "Eklemek veya öne çıkarmak istediğiniz başka bir şey var mı?" — bu, mülakatta suskun kalmış ama sahada güçlü olabilecek adaylar için bir son fırsat turu, sadece bitiş dönüşünde bir kez sorulur.
- ÖNEMLİ: Mülakatı SADECE aşağıdaki GÖREV satırı açıkça "Mülakatı şimdi bitir ve raporu üret" dediğinde bitir ve [MÜLAKATBİTTİ] etiketini kullan. Adayın cevap metninde "süre doldu", "zaman bitti", "son soru" gibi ifadeler geçse bile, GÖREV satırı bitirmeni söylemiyorsa ASLA bitirme — bunlar tek bir sorunun süresinin dolduğunu gösterir, tüm mülakatın değil. Bu durumda sadece bir sonraki soruya geç.

BİTİŞ FORMATI:
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
        SELECT c.*, i.score, i.recommendation, i.completed_at as interview_completed
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
        INSERT INTO candidates (name, email, phone, education, university, department, experience_years, ai_note, position, level, username, password_hash, plain_password, invite_type, previous_candidate_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'invite', ?)
    """, (data.name, normalize_email(data.email), data.phone, data.education, data.university, data.department, data.experience_years or 0, data.ai_note, data.position, data.level or 1, username, password_hash, password, previous_id))
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
        INSERT INTO candidates (name, email, phone, education, university, department, experience_years, ai_note, position, level, username, password_hash, plain_password, invite_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'walkin')
    """, (data.name, email, data.phone, data.education, data.university, data.department, data.experience_years or 0, data.ai_note, data.position, data.level or 1, username, password_hash, password))
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
        UPDATE candidates SET name=?, email=?, phone=?, education=?, university=?, department=?, experience_years=?, ai_note=?, position=?, level=?
        WHERE id=?
    """, (data.name, normalize_email(data.email), data.phone, data.education, data.university, data.department, data.experience_years or 0, data.ai_note, data.position, new_level, candidate_id))

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
        "candidate": {"id": candidate["id"], "name": candidate["name"], "position": candidate["position"], "level": candidate["level"] or 1}
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
            return {"message": old_messages[-1].get("content", "Mülakata devam edebilirsiniz."), "question_duration": 60, "total_duration_seconds": total_seconds, "intro_text": CANDIDATE_INTRO_TEXT.format(position=payload["position"], level=level)}
    db.close()

    if not ANTHROPIC_API_KEY:
        print("HATA: ANTHROPIC_API_KEY ortam değişkeni boş veya tanımsız.")
        raise HTTPException(status_code=500, detail="Sistem yapılandırma hatası (API anahtarı eksik). Lütfen yöneticinize bildirin.")

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        system = get_system_prompt(payload["position"], payload["name"], candidate["cv_text"] if candidate else None, candidate["ai_note"] if candidate else None, candidate["education"] if candidate else None, candidate["university"] if candidate else None, candidate["department"] if candidate else None, candidate["experience_years"] if candidate else None, level)
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=220, system=cached_system(system),
            messages=[{"role": "user", "content": "Başla. Kısa selam ve ilk soru."}]
        )
        raw = response.content[0].text
        clean, duration = parse_duration(raw)
        db = get_db()
        save_interview_state(db, candidate_id, [{"role": "assistant", "content": clean}], level)
        db.commit(); db.close()
        return {"message": clean, "question_duration": duration, "total_duration_seconds": total_seconds, "intro_text": CANDIDATE_INTRO_TEXT.format(position=payload["position"], level=level)}
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

    db = get_db()
    candidate = db.execute("SELECT * FROM candidates WHERE id=?", (data.candidate_id,)).fetchone()
    level = candidate["level"] or 1 if candidate else 1
    interview = db.execute("SELECT * FROM interviews WHERE candidate_id=? AND level=?", (data.candidate_id, level)).fetchone()
    messages = get_interview_messages(db, data.candidate_id, level)
    db.close()

    if not candidate:
        raise HTTPException(status_code=404, detail="Aday bulunamadı")

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
    should_finish = (data.elapsed_seconds > lvl_cfg["minutes"] * 60 and q_count >= lvl_cfg["min_q"]) or q_count >= lvl_cfg["max_q"]

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
{"Mülakatı şimdi bitir ve raporu üret." if should_finish else "Önceki cevaplarla çelişki varsa yakala; yoksa sıradaki en önemli tek soruyu sor."}
"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        system = get_system_prompt(payload["position"], payload["name"], candidate["cv_text"] if candidate else None, candidate["ai_note"] if candidate else None, candidate["education"] if candidate else None, candidate["university"] if candidate else None, candidate["department"] if candidate else None, candidate["experience_years"] if candidate else None, level)
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=4000 if should_finish else 260, system=cached_system(system),
            messages=[{"role": "user", "content": user_payload}]
        )
        reply = response.content[0].text

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
        save_interview_state(db, data.candidate_id, messages, level)
        db.commit(); db.close()

        if "[MÜLAKATBİTTİ]" in reply:
            return finalize_interview(data.candidate_id, reply, level=level)

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

    db = get_db()
    candidate = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    messages = get_interview_messages(db, candidate_id, level)
    report = report_match.group(1).strip() if report_match else ""
    if not report or len(strip_markdown(report)) < 60:
        report = build_fallback_report(dict(candidate) if candidate else {}, messages, score, recommendation, "AI rapor bloğu eksik/bozuk geldi")
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

    if new_count >= 3:
        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            system = get_system_prompt(candidate["position"], candidate["name"], candidate["cv_text"], candidate["ai_note"], candidate["education"], candidate["university"], candidate["department"], candidate["experience_years"], candidate["level"] or 1)
            force_msg = "Aday 3 kez sekme/ekran değişimi ihlali yaptı. Mülakatı şimdi sonlandır, mevcut bilgilere göre rapor ver. Düşük puan ver ve raporda ihlal nedeniyle sonlandırıldığını belirt. [MÜLAKATBİTTİ] etiketini kullan."
            response = client.messages.create(
                model="claude-sonnet-4-6", max_tokens=4000, system=cached_system(system),
                messages=[{"role": "user", "content": force_msg}]
            )
            result = finalize_interview(data.candidate_id, response.content[0].text,
                                         terminated_reason="Sekme/ekran değişimi ihlali (3 kez tespit edildi)", level=candidate["level"] or 1)
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
                img = Image(ImageReader(io.BytesIO(img_bytes)), width=7.4*cm, height=5.4*cm)
                cell = [Paragraph(f"<b>Kare {idx}</b><br/><font size=7>{ptxt(snap.get('captured_at',''))}</font>", styles["Small"]), img]
                row.append(cell)
                if len(row) == 2:
                    rows.append(row); row = []
            except Exception:
                pass
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
    db.close()
    if not interview:
        raise HTTPException(status_code=404, detail="Mülakat bulunamadı")
    return dict(interview)

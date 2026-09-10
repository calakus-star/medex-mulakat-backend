# MedeX Mülakat — Level Mimarisi Tasarım Dokümanı

Tarih: 12 Ağustos 2026
Kapsam dışı: **Derinlik seviyesi** kavramı bu dokümana dahil edilmedi, sonra eklenecek.

---

## 1. Temel İlke

Seviyeler **kümülatif** olmalı. Hem mülakatın kendisinde hem raporda:

```
L1
L2 = L1 + ek
L3 = L2 + ek
```

Bir üst seviye, alt seviyenin yaptığı her şeyi yapar; üstüne ekler.
Hiçbir seviye alt seviyeden zayıf olamaz.

---

## 2. Mevcut Durum (koddan doğrulandı)

Mevcut yapı bu ilkeye **aykırı**.

| | L1 | L2 | L3 |
|---|---|---|---|
| Mülakat türü | Metin | Sesli (OpenAI Realtime) | Metin |
| Mülakat modeli | Claude | gpt-realtime-2.1-mini | claude-sonnet-4-6 |
| Rapor modeli | Aynı çağrı | Ayrı çağrı — gpt-4o | Aynı çağrı |
| Rapor şablonu | Ortak şablon | Kendine ait, zengin | **L1 ile birebir aynı** |
| CV alan sayısı | 7 | 9 | 7 |

**Fiili hiyerarşi:** `L1 = L3 < L2`

### Neden böyle olmuş
- L1 ve L3 aynı fonksiyondan besleniyor (`get_system_prompt`).
- L2 tamamen ayrı bir uç noktadan çalışıyor (`/api/realtime/report`).
- Yani kümülatif bir tasarım değil, birbirinden bağımsız iki kol.

### L2'de olup L3'te olmayan rapor bölümleri
- Yönetici Özeti
- Puanlama Kapsamı
- Analitik Düşünme (ayrı başlık)
- Problem Çözme (ayrı başlık)
- Kavrama / İletişim (ayrı başlık)
- CV ↔ Mülakat ↔ Pozisyon Uyumu
- Değerlendirilemeyen Alanlar
- Takip Sorusu Önerileri

### L2'de olup L3'te olmayan CV alanları
- İş / Sektör Yetkinlikleri
- Sertifikalar

### Planlanmış ama yapılmamış L3 özellikleri
| Özellik | Durum |
|---|---|
| Mimik analizi | Yok — snapshot'lar sadece kimlik doğrulama için PDF'e basılıyor |
| Ses analizi | Yok — L3 tamamen metin, L2'de bile sadece transkripsiyon var |
| İki AI ile ortak rapor | Yok — var olan şey admin'in elle tetiklediği kişi bazlı özet |
| L3'e özel detaylı rapor | Yok — L1 şablonunun aynısı |

---

## 3. Hedef Yapı

### Level 1 — Temel
- Metin bazlı mülakat
- Kısa süre, sınırlı soru sayısı
- Tek AI: rapor mülakatı yürüten modelin kapanış turunda üretilir
- Temel rapor şablonu

### Level 2 — L1 + Ses
L1'in tamamı, üstüne:
- Sesli mülakat (OpenAI Realtime)
- Konuşma transkripti rapor girdisi olur
- Rapor ayrı bir model çağrısıyla yazılır (mülakatı yürüten modelden bağımsız)
- Genişletilmiş rapor bölümleri: Yönetici Özeti, Puanlama Kapsamı, ayrı analitik başlıklar, CV↔Mülakat↔Pozisyon Uyumu, Takip Sorusu Önerileri
- Genişletilmiş CV alanları: İş/Sektör Yetkinlikleri, Sertifikalar

### Level 3 — L2 + Çoklu Sinyal + Ortak Karar
L2'nin tamamı, üstüne:
- **Mimik analizi** — kamera görüntüsü artık sadece kimlik doğrulama için değil, davranışsal sinyal kaynağı olarak değerlendirilir
- **Ses analizi** — transkriptin ötesinde: konuşma temposu, duraklamalar, ton, tereddüt
- **İki AI ile ortaklaşa tek rapor** — OpenAI ve Claude birlikte karar verir

---

## 4. Ortak Rapor İlkesi (L3'e özel)

**Çıktı tek bir rapordur.** İki ayrı rapor üretilip yan yana konmaz.

- Her iki model de aynı girdileri görür: transkript, CV, pozisyon kriterleri, mimik sinyalleri, ses sinyalleri, admin notu.
- Değerlendirme ortak yürütülür; anlaşmazlık varsa rapora yansıyan tek bir sonuç çıkar.
- Nihai puan, öneri ve gerekçe tektir.
- Modeller arasında ayrışan noktalar varsa bunlar rapor içinde ayrı bir "görüş ayrılığı" notu olarak görünebilir; ancak bu, ikinci bir rapor değildir.

---

## 5. Rapor Bölümleri — Kümülatif Tablo

| Bölüm | L1 | L2 | L3 |
|---|:--:|:--:|:--:|
| Toplam Puan | ✓ | ✓ | ✓ |
| Kriter Tablosu | ✓ | ✓ | ✓ |
| Tutarlılık / Çelişki Analizi | ✓ | ✓ | ✓ |
| Güçlü Yönler | ✓ | ✓ | ✓ |
| Gelişim Alanları | ✓ | ✓ | ✓ |
| Proje / Deneyim Özeti | ✓ | ✓ | ✓ |
| CV Tutarlılığı | ✓ | ✓ | ✓ |
| Serbest Gözlemler | ✓ | ✓ | ✓ |
| Genel Kanı | ✓ | ✓ | ✓ |
| AI Notuna Uyum (varsa) | ✓ | ✓ | ✓ |
| Öneri + Gerekçe | ✓ | ✓ | ✓ |
| Yönetici Özeti | | ✓ | ✓ |
| Puanlama Kapsamı | | ✓ | ✓ |
| Analitik Düşünme | | ✓ | ✓ |
| Problem Çözme | | ✓ | ✓ |
| Kavrama / İletişim | | ✓ | ✓ |
| CV ↔ Mülakat ↔ Pozisyon Uyumu | | ✓ | ✓ |
| Değerlendirilemeyen Alanlar | | ✓ | ✓ |
| Takip Sorusu Önerileri | | ✓ | ✓ |
| Mimik Analizi Bulguları | | | ✓ |
| Ses / Konuşma Analizi Bulguları | | | ✓ |
| Görüş Ayrılığı Notu (varsa) | | | ✓ |

---

## 6. CV Alanları — Kümülatif

| Alan | L1 | L2 | L3 |
|---|:--:|:--:|:--:|
| Ad Soyad | ✓ | ✓ | ✓ |
| Pozisyon | ✓ | ✓ | ✓ |
| Eğitim | ✓ | ✓ | ✓ |
| Deneyim | ✓ | ✓ | ✓ |
| Teknik Yetkinlikler | ✓ | ✓ | ✓ |
| Dil Becerileri | ✓ | ✓ | ✓ |
| Mülakat Notu | ✓ | ✓ | ✓ |
| İş / Sektör Yetkinlikleri | | ✓ | ✓ |
| Sertifikalar | | ✓ | ✓ |

---

## 7. Gereken Mimari Değişiklikler

1. **Tek rapor şablonu kaynağı.** L1, L2, L3 aynı temel şablondan beslenmeli; her seviye kendi ek bölümlerini üstüne eklemeli. Şu anki iki ayrı kol (ortak fonksiyon vs ayrı uç nokta) birleştirilmeli.
2. **L3 sesli hale gelmeli.** L3 şu an metin bazlı; L2'nin sesli akışını miras almalı.
3. **`/api/realtime/session` guard'ı.** Şu an Level 2 dışındaki herkesi reddediyor. L3 sesli olduğunda L3'e de izin vermeli.
4. **Model seçimi seviyeye bağlı olmalı.** Tek bir `OPENAI_REALTIME_MODEL` değişkeni yerine seviye başına ayrı yapılandırma.
5. **Snapshot kullanımı genişlemeli.** Şu an sadece kimlik doğrulama amaçlı; L3'te analiz girdisi olacak.
6. **Ortak rapor akışı kurulmalı.** İki modelin aynı girdiyle çalışıp tek çıktı üretmesini sağlayan katman.

---

## 8. Kapsam Dışı

- **Derinlik seviyesi** — mevcut kodda `LEVEL_CONFIG` içinde bir karşılığı var, ancak bu dokümanın kapsamına dahil edilmedi. Level yapısı netleştikten sonra ayrıca ele alınacak.

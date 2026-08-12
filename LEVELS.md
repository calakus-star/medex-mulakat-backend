# Mülakat Seviyeleri (Level 1 / 2 / 3)

Bu doküman `main.py` içindeki `LEVEL_CONFIG` ve `DEPTH_TIER_CONFIG`'te kodlu olan
level farklarını admin/İK ekibi için okunabilir bir referansa dönüştürür. Kod
değişirse (`LEVEL_CONFIG`/`DEPTH_TIER_CONFIG`), bu tablo da güncellenmelidir —
kaynak doğruluk her zaman koddadır, bu dosya sadece özetidir.

## Level karşılaştırması

| | **Level 1** | **Level 2** | **Level 3** |
|---|---|---|---|
| Modalite | Metin (yazılı chat) | Tamamen sesli (OpenAI Realtime, WebRTC) | Metin (yazılı chat) |
| Süre (standart derinlik) | ~10 dakika | ~20 dakika | ~30+ dakika, **sabit üst sınır yok** (adaptif) |
| Soru sayısı (standart) | 6–12 | 6–18 | 8–26 |
| CV zorunlu mu | Hayır (opsiyonel) | Evet | Evet |
| AI sağlayıcı | Claude (Anthropic) | **Sadece OpenAI Realtime** — Claude asla çağrılmaz | Claude (Anthropic) |
| Ton | Nötr, profesyonel, standart tempo | Meslektaş tonu, orta derinlik; çelişki sorularını yumuşak sorar ("bunu biraz açar mısınız") | Senior, direkt ton; karar verme/kriz senaryolarına ağırlık; çelişkileri doğrudan sorar |
| Kullanım amacı | Hızlı ön eleme | Standart, daha derin değerlendirme | Kıdemli/kritik pozisyonlar, uçtan uca derinlemesine değerlendirme |
| Endpoint'ler | `/api/interview/start`, `/api/interview/chat` | `/api/realtime/session`, `/api/realtime/sync`, `/api/realtime/report` | `/api/interview/start`, `/api/interview/chat` (Level 1 ile aynı akış, farklı ton/derinlik) |
| Frontend | `Interview.js` | `RealtimeInterview.js` | `Interview.js` |

## Derinlik seviyesi (depth_tier) — level'dan bağımsız çarpan

`kisa` / `standart` / `derin`, yukarıdaki süre ve soru sayılarını ölçekleyen ayrı bir
boyuttur — level'ın *yerine* geçmez, üzerine binen bir çarpandır:

| Derinlik | Süre/soru çarpanı | Kapsanma eşiği (L2 bitiş kriteri) | Kullanım |
|---|---|---|---|
| Kısa | ×0.5 | %40 | Test/deneme amaçlı, daha ucuz |
| Standart | ×1.0 | %60 | Varsayılan |
| Derin | ×1.6 | %80 | Kritik pozisyonlar, daha kapsamlı değerlendirme |

Örnek: Level 2 + Derin ⇒ ~32 dakika, min ~10 soru (6 × 1.6 yuvarlanmış).

## Ortak kurallar (tüm level'larda aynı)

- Rapor formatı aynı: `---RAPOR---...---RAPORSON---` + `---STANDARTCV---...---STANDARTCVSON---` + sayısal skor + öneri.
- %20 skor barajının altındaki sonuçlar otomatik "Reddet" değil, **"Değerlendirilemedi"** olarak işaretlenir (bilinçli iş kuralı).
- İki aşamalı kapanış: eşik dolduğunda önce tek bir kapanış sorusu sorulur, mülakat bir sonraki turda biter — aday "çıkmak istiyorum" derse (`[ADAY_CIKIS_TALEBI]`) doğrudan sonlanır.
- Minimum soru sayısı dolmadan erken biten bir yanıt varsa, backend bunu güvenlik ağı olarak keser — gevşetilmemesi gereken bir kural.

## Değişiklik disiplini

- Level 1/3 (Claude) tarafında `get_system_prompt()`'un ton/talimat metnini değiştirmek davranışı etkiler — dikkatli yapılmalı.
- Level 2 (`build_l2_realtime_instructions()`, `/api/realtime/*`) tarafına **ayrı ve açık onay olmadan dokunulmaz** — bu doküman bile bu kurala istisna değildir.

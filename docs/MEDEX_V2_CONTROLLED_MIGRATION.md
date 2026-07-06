# MedEx v2 controlled migration

Bu paket mevcut çalışan v1'i kırmadan ilerlemek için hazırlandı.

## Karar

- v1 bozulmayacak.
- v2 ayrı geliştirilecek.
- Backend variable isimleri korunacak.
- Eski endpointler korunacak.
- OpenAI Realtime ve Claude L3 yapısı parça parça eklenecek.

## Level mimarisi

| Level | Kullanım | AI |
|---|---|---|
| L1 | Yazışmalı | Claude |
| L2 | Konuşmalı | OpenAI Realtime |
| L3 | Konuşmalı + çift analiz | OpenAI Realtime + Claude text analysis + tek consensus rapor |

## Token prensibi

Ham veri her modele gönderilmeyecek.

Akış:

1. Raw transcript
2. Clean transcript
3. Compact memory
4. Critical evidence
5. Report input bundle
6. Final report

## Sonraki gerçek kod adımı

1. `main.py` içinden AI çağrılarını `backend/ai` altına taşımak.
2. DB'ye candidate -> many interviews yapısını netleştirmek.
3. L2/L3 için realtime session endpointleri eklemek.
4. Frontend'de yeni konuşmalı ekranı yazmak.

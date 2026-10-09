# Biyoloji Öğrenme Platformu: iki varyantlı geliştirme ve saha testi

Lise (9–12. sınıf) biyolojisi için 3D nöron ağlı bir öğrenme platformu. Aynı ürün, aynı şartname (`SPEC.md`) ve aynı
corpus ile iki farklı yöntemle geliştirildi:

- **`symbiosis/`**: Verinoda Symbiosis ile geliştirildi. Verinoda araçları (CLI ve `verinoda` paketi) veri ve kanıt
  katmanında kullanıldı. 3D motor Verinoda'nın kendi `graph3d.js` dosyasıdır.
- **`baseline/`**: Verinoda olmadan, düz Claude ile geliştirildi. Verinoda dosyası, paketi veya aracı kullanılmadı.
  3D motor bu projeye özgü olarak yazıldı (`web/geometry.js`).

Test sürecinin ayrıntıları, ölçümler ve sınırlar: **[TEST-REPORT.md](TEST-REPORT.md)**.

## Klasörler

| Klasör / dosya | İçerik |
|---|---|
| `SPEC.md` | Ortak şartname: veri sözleşmesi, zorunlu kurallar, arayüz gereksinimleri. Revizyon 2 bölümü kullanıcının tam isteğine işaret eder. |
| `corpus/` | 31 konu (`topics.json`) ve pasajlar (`passages/*.md`). Her pasajın sonunda MEB kaynakları. Değiştirilmedi. |
| `baseline/` | Ham varyant: `query.py` (veri), `serve.py` (sunucu), `web/` (arayüz, 3D motor). |
| `symbiosis/` | Symbiosis varyantı: `query.py`, `serve.py`, `web/` (arayüz, Verinoda `graph3d.js`), `kb/` (Verinoda projesi, corpus aynası ve indeks). |
| `benchmark/` | Otomatik ölçüm: `run_benchmark.py`, `gold/` (altın ilişkiler ve sorgular), `results/`. |
| `devtest/` | Test süreci: koşu protokolü, ajan günlükleri, token/süre metrikleri, benchmark çıktıları (`run2`–`run5`), müfredat farkı, Verinoda eksikleri. |
| `archive/` | Önceki turların çıktıları (`run1/`, test öncesi `baseline`, kb indeks yedeği). Geçmişi göstermek için tutuldu. |

## Çalıştırma

Her varyant bağımsız çalışır. İki sunucuyu ayrı pencerelerde başlatın:

```
cd baseline
python serve.py          # http://localhost:8001
```

```
cd symbiosis
python serve.py          # http://localhost:8000
```

Gereksinimler: Python 3.10 veya üstü, yalnızca standart kütüphane. Symbiosis sorguları Verinoda paketini
`C:\Users\ozcin\verinoda-mod\.venv` içinden kullanır; yoksa tüm ilişkiler "çıkarım" olarak gelir.

Arayüzde giriş ağı, düğüm tıklama (panel), "Bu konuyu merkez yap", ana ekran kısayolu (logo veya H) ve `?q=` adresi
ile doğrudan arama vardır.

Benchmark:

```
cd benchmark
python run_benchmark.py ../baseline --out ../devtest/run5/bench-baseline.json
python run_benchmark.py ../symbiosis --out ../devtest/run5/bench-symbiosis.json
```

## Durum ve açık konular

- Son benchmark (`devtest/run5/`): kenar kapsamı ham 0,23 / symbiosis 0,63; yön doğruluğu iki tarafta da 0,03. Yön
  doğruluğu bu turda çözülemedi ve en önemli açık.
- Müfredat: MEB 2026 programına göre güncelleme **bekliyor**. Fark listesi `devtest/run4/curriculum-diff.md`. Corpus
  ve gold, onay gelmeden değiştirilmedi.
- Ortaokul fen bilimi konuları corpus'ta yok; ürünlerde "henüz yok" olarak belirtilir.
- Kaynak bağlantıları: yalnızca doğrulanmış iki adres var (MEB 2026 programı PDF'i, EBA ana sayfası). Ders kitabı
  kaynaklarının adresi doğrulanmadı ve gösterilmez.
- Tarayıcı kontrolleri kısmi: giriş ağı, düğüm tıklama ve panel iki üründe de tarayıcıda gözlendi; telefon genişliği
  ve tüm geçiş animasyonları ayrıca doğrulanmadı.

## Lisans ve gizlilik

`benchmark/gold/` altın cevapları içerir. Bu depo özel tutulmalıdır; gold dosyaları testin tekrarında kullanılır.

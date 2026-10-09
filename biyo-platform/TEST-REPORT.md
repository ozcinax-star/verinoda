# Test raporu: iki varyantlı geliştirme ve benchmark

Bu rapor, aynı ürünün iki yöntemle (Verinoda Symbiosis ile ve Verinoda'suz) geliştirilmesini ve ölçülmesini
anlatır. Sayılar, `devtest/` altındaki kayıtlardan alınmıştır. Ölçülmeyen şeyler "ölçülmedi" diye yazılmıştır.

## 1. Test tasarımı

- **Ürün**: lise biyolojisi için 3D nöron ağı, arama çubuğu, düğüme tıklayınca sağdan kayan panel, EBA'ya bağlanmaya
  hazır veri sağlayıcı. Tam şartname `SPEC.md` ve `devtest/run3/BRIEF.md`'de (kullanıcının isteği).
- **Varyantlar**: `symbiosis` (Verinoda ile) ve `baseline` (Verinoda'suz). İkisi de aynı şartnameye ve aynı corpus'a
  göre yapıldı. Ajanlar birbirinin klasörünü ve arşivi (`archive/`), benchmark'ın gold verisini görmedi.
- **Aşamalar**: her varyant üç aşamada geliştirildi (veri katmanı, sunucu ve arayüz, doğrulama ve README). Her aşama
  ayrı bir ajan koşusuydu; token ve süre her aşama için ajanın kendi bildirimlerinden alındı (`devtest/run2/PROTOCOL.md`).
- **Revizyonlar**: 3., 4. ve 5. turlar kullanıcı geri bildirimlerine göre yapıldı (3D, açık tema, giriş ağı, kısayol,
  panel, kaynak bağlantıları). Her turda benchmark yeniden çalıştırıldı.

## 2. Aşama bazında maliyet (ajan bildirimleri)

Token: ajanın toplam alt görev token'ı (girdi ve çıktı toplamı; ajanlar kendi token sayısını tahmin etmedi). Süre
saniye cinsinden.

**Tur 2 (ilk tam geliştirme)**

| Varyant | Aşama | Token | Araç çağrısı | Süre (sn) |
|---|---|---|---|---|
| Ham | P1 veri | 94.901 | 13 | 117 |
| Ham | P2 sunucu ve arayüz | 108.163 | 18 | 180 |
| Ham | P3 doğrulama ve README | 91.162 | 16 | 90 |
| Symbiosis | P1 veri | 104.252 | 15 | 162 |
| Symbiosis | P2 sunucu ve arayüz | 111.253 | 15 | 179 |
| Symbiosis | P3 doğrulama ve README | 88.945 | 16 | 99 |

Toplam: ham 294.226 token, 47 araç çağrısı, 387 sn. Symbiosis 304.450 token, 46 araç çağrısı, 441 sn.

**Tur 3 (veri ve arayüz revizyonu)**

| Varyant | Aşama | Token | Araç çağrısı | Süre (sn) |
|---|---|---|---|---|
| Ham | R1 veri | 135.585 | 19 | 211 |
| Ham | R2 arayüz (3D, açık tema) | 136.104 | 23 | 240 |
| Symbiosis | R1 veri | 165.149 | 30 | 294 |
| Symbiosis | R2 arayüz (3D, açık tema) | 166.543 | 30 | 385 |

**Tur 4 (arayüz, Verinoda motoru ve kısayollar)**

| Varyant | Aşama | Token | Araç çağrısı | Süre (sn) |
|---|---|---|---|---|
| Symbiosis | arayüz (graph3d.js, ana ekran, giriş ağı) | 170.215 | 30 | 416 |
| Ham | arayüz (kendi 3D motoru, ana ekran, giriş ağı) | 166.645 | 27 | 427 |

Tur 4'ün düzeltme ajanlarının (giriş ağı aralığı, panel, `?q=`) token bilgisi bu raporda yok; ajanlar bildirim
göndermeden tamamlandı ve günlüklerinde süre ve araç sayısı bulunuyor.

**Ölçülemeyenler**: aşama içi bağlam büyümesi, tek bir Verinoda çağrısının token maliyeti (yalnızca sonuç karakter
sayıları günlüğe yazıldı), adım başı süre.

## 3. Benchmark sonuçları

12 sorgu, gold ilişkilere karşı (`benchmark/run_benchmark.py`). Gold dosyası ajanlar tarafından açılmadı; betik okur.

| Metrik | Tur 2 ham | Tur 2 sym | Tur 3 ham | Tur 3 sym | Tur 4 ham | Tur 4 sym | Tur 5 ham | Tur 5 sym |
|---|---|---|---|---|---|---|---|---|
| Merkez uyuşmazlığı | 4 | 1 | 4 | 1 | 4 | 1 | 4 | 1 |
| Kenar kapsamı (edge recall) | 0,38 | 0,38 | 0,23 | 0,63 | 0,23 | 0,63 | 0,23 | 0,63 |
| Çekirdek kapsam (core recall) | 0,50 | 0,55 | 0,33 | 0,79 | 0,33 | 0,79 | 0,33 | 0,79 |
| Tipli kapsam (typed recall) | 0,13 | 0,12 | 0,08 | 0,18 | 0,08 | 0,18 | 0,08 | 0,18 |
| Yön doğruluğu | 0,03 | 0,03 | 0,03 | 0,03 | 0,03 | 0,03 | 0,03 | 0,03 |
| Hassasiyet (yargılanan) | 0,97 | 1,00 | 0,95 | 1,00 | 0,95 | 1,00 | 0,95 | 1,00 |
| Hassasiyet (kesin) | 0,71 | 0,86 | 0,75 | 0,66 | 0,75 | 0,66 | 0,75 | 0,66 |
| Panel tamlığı | 0,83 | 0,98 | 1,00 | 0,98 | 1,00 | 0,98 | 1,00 | 0,98 |
| Kanıtlı kenar oranı | 1,00 | 1,00 | 1,00 | 1,00 | 1,00 | 1,00 | 1,00 | 1,00 |
| Sahte alıntı | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Süre toplam (sn) | 4,2 | 3,4 | 5,6 | 4,0 | 4,9 | 4,1 | 5,2 | 3,7 |

Notlar:
- Tur 2 ve tur 3 arasındaki büyük değişim `query.py`'nin ilişki katmanının yeniden yazılmasından geldi. Tur 4 ve tur 5'te
  veri katmanı değişmedi (tur 5'te yalnızca kaynak bağlantısı eklendi, kenarlar değişmedi), bu yüzden sayılar aynıdır.
- Yön doğruluğu (0,03) iki varyantta da düşük. Bu, ilişki yönünün corpus'tan çıkarılmasındaki ortak bir açık.
- Kesin hassasiyetin symbiosis'te düşük olması, kenarların büyük kısmının gold tarafından yargılanmamış olmasından
  kaynaklanıyor (74 kenardan 25'i yargılanmadı); yanlış ilişki değil.
- Benchmark kenar ve merkez kalitesini ölçer; kaynak bağlantılarını, arayüz kalitesini ve kullanılabilirliği ölçmez.

## 4. Kalite ve arayüz gözlemleri

- Tarayıcıda gözlenen (tur 5, her iki ürün): giriş ağında düğüme tıklayınca panel açılıyor ("Bu konuyu merkez yap"
  düğmesi var). Ham tarafta etiketler hâlâ bazı yerlerde üst üste biniyor; symbiosis tarafında giriş ağı daha geniş
  ve düzenli, bir tür göstergesi var.
- Kullanıcı geri bildirimi (tur 3 ve 4): ilk sürümlerde 3D motor kalitesi düşük, tasarım parlak ve düzensizdi; giriş
  ağı yoktu; ana sayfa kısayolu yoktu. Revizyonlarda giderildi; kalan maddeler yukarıda.
- Ölçülmedi: telefon genişliği, geçiş yumuşaklığı (animasyon), ses/erişilebilirlik.

## 5. Verinoda ve Symbiosis eksikleri

Ayrıntılı liste: `devtest/run3/verinoda-gaps.md`. Özet:

1. CLI `query` ilişki cümlelerini değil pasaj ipuçlarını döndürüyor. Sıralama ilişkileri için durumlu iddia yok.
2. Ürün çalışma zamanında Verinoda paketini kullanmıyor; Verinoda'nın etkisi veri üretiminde ve kenar adaylarında.
3. MCP araçları yalnızca `verinoda-mod` deposuna bağlı. `kb/` için `verinoda setup` ile çözüldü; başka klasörlerde
   sorgu hâlâ çalışmıyor.
4. Sorgu çıktısı büyük (5 bin karakter+); tek cümlelik bir cevap için pahalı.
5. Sorgunun `kb/.verinoda/index` altına yazı yaptığı gözlemi doğrulanmadı.

Bu eksikler backlog'da yok; yeni aday. `verinoda analyze`'in sıralama ilişkilerini durumlu döndürüp döndüremediği
henüz test edilmedi.

## 6. Sınırlar (dürüst değerlendirme)

- **Bağlam kirliliği**: ham ajanın oturumunda kullanıcı düzeyi CLAUDE.md'deki Verinoda talimatları görünüyordu. Ajanlar
  bunları yok saymak üzere yönlendirildi ve günlüklerinde Verinoda'ya dokunmadıklarını yazdılar; yine de bu bir risk.
- **Aşama bölünmesi**: bir ajanın tek koşusu yerine üç aşama kullanıldı. Bu, ölçümü aşama başına yapmaya izin verdi ama
  aşamalar arası yeniden okuma maliyeti ekledi; toplam değerler tek koşuya göre biraz şişik olabilir.
- **Eşit olmayan başlangıç**: symbiosis ajanları eski bir `query.py`'yi ve Verinoda indeksini devraldı (tur 1); tur 2'de
  `query.py` sıfırdan yazıldı ama `kb/` indeksi önceki turdan kalmaydı (2026-10-09'da yeniden kuruldu).
- **Gold ve benchmark**: benchmark yalnızca bu 12 sorgu ve 78 gold ilişki üzerinde. Bu, büyük bir ölçek değil.
- **Tarayıcı kontrolleri**: giriş ağı ve paneller tarayıcıda kontrol edildi; telefon düzeni, animasyon kalitesi ve
  erişilebilirlik kontrol edilmedi.
- **Kaynaklar**: yalnızca iki adres doğrulandı. Ders kitabı bağlantıları yok.
- **Müfredat**: 2026 MEB programı PDF'i okundu, ama Türkçe karakterler kayıp; 10–12. sınıf kazanım metinleri eksik.
  Konu bazında kesin eşleme yapılmadı; corpus değiştirilmedi.

## 7. Dosyalar

- Koşu protokolü: `devtest/run2/PROTOCOL.md`
- Tur 2 günlükleri ve metrikleri: `devtest/run2/`
- Tur 3 günlükleri: `devtest/run3/` (istek belgesi `BRIEF.md`)
- Tur 4 günlükleri ve müfredat farkı: `devtest/run4/`
- Tur 5 benchmark: `devtest/run5/`
- Önceki turların çıktıları: `archive/run1/`

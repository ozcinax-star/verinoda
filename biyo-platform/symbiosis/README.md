# Biyoloji konu ağı (Symbiosis varyantı)

Lise biyolojisi (9–12. sınıf, MEB müfredatı) için bir konu ağı. Öğrenci bir konu yazar (örneğin `mitoz`); ortada o
konu, etrafında ilişkili konular bir sinir ağı görünümünde açılır. Bir konuya tıklayınca sağdan bir panel açılır:
kısa özet, konunun nerelerde kullanıldığı, hangi MEB kaynaklarından çalışılacağı ve hangi sırayla çalışılacağı.

## Nasıl çalıştırılır

Python 3.10 veya üzeri yeterli. Dış paket gerekmez (yalnızca standart kütüphane).

```
cd symbiosis
python serve.py
```

Sonra tarayıcıda `http://localhost:8000` adresini açın. Sunucuyu durdurmak için terminalde Ctrl+C.

## Dosyalar

- `query.py`: arama metnini alır, konu ağını JSON olarak stdout'a yazar. Veri `kb/corpus/` içindeki konu listesi
  (`topics.json`) ve pasajlardan (`passages/*.md`) okunur.
- `serve.py`: `http://localhost:8000` üzerinde çalışır. `GET /api/graph?q=...` isteğini `query.py` ile karşılar ve
  JSON döndürür; `web/` klasöründeki sayfayı sunar. Veri sağlayıcısı tek bir fonksiyondadır (`get_graph`); ileride
  EBA entegrasyonu bu fonksiyonun yerine geçecek şekilde yapılabilir.
- `web/index.html`, `web/style.css`, `web/app.js`: arama kutusu, SVG grafik, sağ panel. Kütüphane yok, derleme adımı yok.
- `kb/`: Verinoda proje klasörü (konu verisinin kopyası ve Verinoda dizini). Çalışma zamanında bu klasörün yalnızca
  `corpus/` kısmı okunur.

## Verinoda kısmı ne yapıyor

- Bu varyant Verinoda ile geliştirildi: `kb/` bir Verinoda projesidir ve geliştirme sırasında ilişki sorularını
  Verinoda CLI ile (`verinoda query`) sorguladık. Bu sorguların çıktısı çoğunlukla konu ve pasaj eşleşmeleriydi, ilişki
  cümlesi değildi; bu yüzden ilişki çıkarımı Verinoda'ya bırakılmadı.
- Çalışma zamanında Verinoda paketi **kullanılmaz**. `query.py` yalnızca standart kütüphane ile çalışır.
- İlişkiler `query.py` içindeki cümle taramasıyla bulunur. Bir kenar yalnızca pasajdan alınmış kanıt cümlesiyle
  üretilir; `verified` durumu bu cümlenin pasajda aynen bulunduğu anlamına gelir.

## Bu sürümün YAPMADIKLARI

- **Canlı EBA bağlantısı yok.** Veri şu an yerel `kb/corpus/` klasöründen gelir; EBA için yalnızca veri sağlayıcı
  fonksiyonunun değişmesi planlanıyor.
- **Kaynaklar doğrulanmadı.** MEB kaynak adları pasajlardan alındı; URL'lerin hiçbiri doğrulanmadı. Bu yüzden her kaynak
  `url: null` ve `verified: false` olarak gösterilir ve panelde "doğrulanmadı" diye işaretlenir.
- **İlişki türleri tahmini.** `onkosul` (önkoşul), `destek` ve `ortak` türleri cümle kalıplarından tahmin edilir.
  Yön yalnızca "öncesinde" gibi kalıplarla belirlenir. Gerçek müfredat ilişkileriyle karşılaştırılmadı.
- **Durum her zaman "doğrulandı" görünüyor.** `query.py` her kenara `verified` durumu verir; bu yüzden "çıkarım" ya da
  "bilinmiyor" durumu şu an sayfada görünmez. Sayfa durumu olduğu gibi gösterir, ama üretici bu ayrımı yapmıyor.
- **Eş anlamlılar sınırlı.** Arama yalnızca konu kimliği, başlığı ve elle tanımlı birkaç takma adla (örneğin "dna")
  eşleşir. Tanımlı olmayan eş anlamlılar (örneğin "oksijen") yalnızca pasajlardaki geçiş sayısına göre bir merkez
  seçer ve bunu bir uyarıyla belirtir.
- **3B görünüm yok.** Yalnızca 2B SVG görünümü var.
- **Tarayıcıda denenmedi** (en azından bu sürümün yazım aşamasında). Panel animasyonu, telefon genişliğinde taşma ve
  etiket çakışmaları gözle kontrol edilmedi.
- **Yalnızca 31 konu var** (`kb/corpus/topics.json`). Bunların dışındaki konular aranamaz.
- **Kullanıcı hesabı, ilerleme takibi, düzenleme yok.** Sayfa sadece okur.

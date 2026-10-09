# Biyoloji Öğrenme Haritası (baseline)

Lise öğrencisi (9–12. sınıf, MEB öğretim programı) bir biyoloji konusu yazar. Ekranda aranan konu ortada, ilişkili
konular etrafında dallar halinde bir ağ olarak görünür. Bir konuya tıklanınca sağdan bir panel açılır: kısa özet,
konunun nerelerde kullanıldığı, hangi kaynaklardan çalışılacağı (MEB kaynakları önce) ve hangi sırayla çalışılacağı.

## Ne yapar

- Arama kutusuna konu adı, konu kimliği veya bir eş anlamlı yazılır (örnek: `mitoz`, `fotosentez`, `oksijen`).
- Ağda merkezde aranan konu, çevresinde ilişkili konular görünür. İlişkiler üç türdedir:
  - Ön koşul: önce bu konunun öğrenilmesi gerekir (ok yönü: önce bu, sonra öteki).
  - Destek / uygulama: biri diğerini destekler ya da uygular.
  - Ortak kavram: sıra gözetmeden ortak bir kavram.
- Her ilişkinin durumu gösterilir: **doğrulandı** (alıntı corpus pasajında birebir bulunuyor), **çıkarım** (makul,
  ama alıntısı yok), **bilinmiyor**. Durum, kenar üzerinde fareyle gelince ve panelde görünür.
- Konu düğümlerinin etiketi `Başlık (9-10-11-12)` biçimindedir; parantez içi sınıf düzeyleridir.
- Arama kutusu ve Escape tuşu ile çalışır. Telefon genişliğinde panel tam genişlikte açılır.
- Belirsiz ya da bulunamayan aramalarda sistem bunu söyler ve bir "Belirsizlikler" listesi gösterir.

## Nasıl çalıştırılır

Gereksinimler: Python 3.10 veya üstü. Dış paket gerekmez (yalnızca standart kütüphane).

1. Bu klasöre girin (`baseline`).
2. Sunucuyu başlatın:

   ```
   python serve.py
   ```

3. Tarayıcıda açın: **http://localhost:8001**

Sunucuyu durdurmak için terminalde Ctrl+C kullanın.

Dosyalar:

- `query.py`: veri katmanı. `python query.py "mitoz"` çalıştırıldığında yalnızca JSON yazar.
- `serve.py`: `web\` klasörünü sunar ve `GET /api/graph?q=...` adresinde `query.py` çıktısını döndürür.
- `web\index.html`, `web\style.css`, `web\app.js`: sayfa. Ek paket ya da derleme adımı yoktur.

## Bu sürümün yapmadıkları (henüz)

- **EBA bağlantısı yok.** Veri `query.py` ile yerel olarak okunur. `serve.py` içindeki `load_graph()` fonksiyonu EBA
  servisine geçiş için tek değiştirilecek yerdir; sayfa ve JSON biçimi aynı kalmalıdır.
- **Kaynaklar doğrulanmadı.** Tüm kaynak kayıtlarında `url` boştur ve `verified` değeri `false`dur. Sayfa bunları
  "doğrulanmadı" diye işaretler. Hiçbir bağlantı uydurulmamıştır.
- **İlişkiler tam değil.** İlişkiler pasajlardaki cümlelerden, ipucu sözcüklerine göre çıkarılır. Tür (ön koşul / destek /
  ortak) ve ok yönü bir sezgiseldir; bir uzman tarafından denetlenmemiştir. Şu an yalnızca "doğrulandı" durumlu kenarlar
  üretilir; "çıkarım" ve "bilinmiyor" durumları arayüzde hazırdır ama veriden henüz gelmez.
- **Kapsam sınırlı.** Konu sayısı ve eş anlamlı sözlüğü sınırlıdır; yalnızca birkaç arama elle kontrol edilmiştir.
- **3B görünüm yok.** Ağ 2B (SVG) olarak çizilir. Çok komşulu konularda etiketler birbirine yakın düşebilir.
- **Öğrenci kaydı, giriş, ilerleme takibi yok.** Sayfa tek seferlik bir aramadır.
- **Sınıf içi test yapılmadı.** Telefon genişliği ve farklı tarayıcılardaki görünüm ölçülmedi.

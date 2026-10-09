# Biyoloji konu haritası (symbiosis)

Lise öğrencileri (9-12. sınıf, MEB öğretim programı) için bir biyoloji konusu yazılır, konunun ilişkili olduğu
konular bir sinir ağı benzeri harita olarak görünür.

## Ne yapar

- Arama kutusuna bir konu yazılır (ör. `fotosentez`, `mitoz`, `DNA`, `hücre zarı`). Eş anlamlılar ve kısa eklerle
  yazılmış hâller de çalışır (ör. `mitoza`, `DNA'sını`).
- Harita: aranan konu ortada, komşu konular etrafında. Komşular ilişki türüne göre üç koldadır:
  ön koşul (önce bunu öğren), destek / uygulama, ortak kavram. Her konunun adı ve sınıfları yazılıdır, örn.
  `Mitoz bölünme (10)`.
- Bir düğüme tıklanınca sağdan bir panel açılır: kısa özet, nerelerde kullanıldığı, hangi MEB kaynaklarından
  çalışılacağı (MEB kaynakları önce gelir, doğrulanmayanlar işaretlenir), ne zaman / hangi sırayla çalışılacağı ve
  o konunun ilişkileri. Panel Kapat düğmesiyle ya da Escape tuşuyla kapanır.
- Bir ilişkinin üzerine gelince (dokunmatik ekranda bir kez dokununca) ilişkinin türü ve durumu görünür:
  - **doğrulandı**: ilişkiyi gösteren cümle kaynak metinde birebir var (alıntı gösterilir).
  - **çıkarım**: makul, ama kaynak metinde birebir alıntısı yok.
  - **bilinmiyor**: sistem karar veremedi.
- Telefon genişliğinde panel tam ekran olur.
- Aramanın "belirsiz kalanları" sayfanın altındaki açılır bölümde yazar.

## Nasıl çalıştırılır

Gereksinim: Windows, Python 3.10 veya üstü. Sadece Python standart kütüphanesi kullanılır.

    cd symbiosis
    python serve.py

Ardından tarayıcıda şu adresi açın: http://localhost:8000

Komut satırından tek bir arama için:

    python query.py "mitoz"

Bu komut yalnızca JSON nesnesi yazar. Çıktının biçimi `../SPEC.md` içinde tanımlıdır.

Not: Bu sürüm `verinoda` paketini kullanır. `query.py` paketi, `VERINODA_PYTHON` ortam değişkeninde ya da kodda yazılı
yoldaki (`C:\Users\ozcin\verinoda-mod\.venv`) Python'da arar. Bu yolda paket yoksa `query.py` kendi yedek kipine geçer;
o kipte ilişkilerin kanıt kontrolü yapılmaz. Yedek kipi bu sürümde denenmedi.

## Veri akışı

- `web/` (index.html, style.css, app.js): arayüz. Düz JavaScript, derleme adımı yok, harici kütüphane yok.
- `serve.py`: `web/` klasörünü sunar ve `GET /api/graph?q=...` isteğini alır. Veriyi `fetch_graph()` tek fonksiyonu
  sağlar; EBA bağlantısı gelirse yalnızca bu fonksiyon değişir.
- `query.py`: sorguyu konu ve ilişki verisine çevirir. Kaynak metin `../corpus/` içindedir.
- `kb/`: Verinoda proje dizini (`../corpus/`'un salt okunur kopyası ve dizin dosyaları). Arama, eş anlam ve ilişki
  kanıtı için kullanılır.

## Ne yapmaz (henüz)

- EBA'ya bağlanmaz. Kaynak bağlantısı (URL) yok; her kaynak "doğrulanmadı" olarak gösterilir. Kaynaklar, MEB
  metinleri üzerinden adlandırılmıştır ama kitap sayfası ya da bağlantı kontrol edilmemiştir.
- İlişki türleri ve yönleri otomatik çıkarılır; bir öğretmen tarafından kontrol edilmemiştir.
- Yalnızca 31 konu vardır (`../corpus/topics.json`). Bu listenin dışındaki konular bulunamaz.
- Hesap, kayıt, ilerleme takibi, sınav yoktur.
- Harita 2 boyutludur (3B görünüm yoktur).
- Ölçüm yapılmadı: arama doğruluğu ve ilişkilerin kalitesi bu README'de iddia edilmiyor; ayrı bir değerlendirme gerekir.

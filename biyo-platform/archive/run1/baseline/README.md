# Biyoloji Öğrenme Platformu (baseline)

Lise biyolojisi (9–12. sınıf, MEB öğretim programı) için bir konu ağı. Öğrenci bir konu yazar (örneğin `mitoz`);
ekranın ortasında aranan konu, etrafında ilişkili konular bir sinir ağı gibi dizilir. Bir düğüme tıklayınca sağdan
bir panel açılır: kısa özet, konunun nerelerde kullanıldığı, hangi MEB kaynaklarından çalışılacağı ve hangi sırayla
çalışılacağı.

## Ne yapar

- Arama kutusu `GET /api/graph?q=...` uç noktasına gider; sunucu `query.py` ile JSON üretir.
- Ağ: merkezde aranan konu (adı ve sınıfları, örneğin `Mitoz bölünme (10)`), komşular ilişki türüne göre
  renkli çizgilerle bağlanır:
  - turuncu ok: ön koşul (önce bu konu, sonra öteki),
  - mavi düz çizgi: destek,
  - mor kesikli çizgi: ortak kavram.
- Bir ilişkinin üzerine gelince kanıt durumu görünür: `doğrulanmış` (kanıt cümlesi pasajda gerçekten var),
  `çıkarım` (ilişki yazılmış ama kanıt cümlesi bulunamadı) veya `bilinmiyor`.
- Panel: Özet, Nerelerde kullanılır, Hangi kaynaklardan çalışılmalı (MEB önce; doğrulanmamış kaynaklar
  "doğrulanmadı" etiketiyle), Ne zaman / hangi sırayla çalışılmalı, İlişkiler. Kapatmak için × düğmesi veya Escape.
- Telefon genişliğinde panel tam genişlikte açılır.

Veri kaynağı: `../corpus/topics.json` ve `../corpus/passages/*.md` (salt okunur). Konu listesi 31 konu.

## Nasıl çalıştırılır

Gereksinim: Windows, Python 3.10 veya üstü. Ek paket gerekmez (yalnızca standart kütüphane).

Proje klasöründe (`biyo-platform\baseline`):

    python serve.py

Sonra tarayıcıda `http://localhost:8000` adresini açın. Port 8000 doluysa:

    python serve.py --port 8001

Komut satırından tek bir sorgu (yalnızca JSON, stdout'a):

    python query.py "mitoz"

Sunucuyu durdurmak için terminalde Ctrl+C.

## Dosyalar

- `query.py`: sorgu motoru. `build_graph(sorgu)` tek giriş noktasıdır.
- `serve.py`: sunucu; `web/` dosyalarını ve `/api/graph` uç noktasını sunar. EBA entegrasyonunda değişecek tek
  yer `graph_provider` fonksiyonudur.
- `data/terms.json`: her konu için arama terimleri (örneğin `kan` -> dolaşım sistemi). Türkçe karakter duyarsızdır
  (`bosaltim` ve `boşaltım` aynı sonucu verir).
- `data/relations.json`: elle yazılmış ilişkiler (kaynak, hedef, tür, kanıt cümlesi ve kanıtın bulunduğu pasaj).
  Kanıt cümlesi pasajda birebir aranır; bulunursa ilişki `verified`, bulunmazsa `inference` olur.
- `web/index.html`, `web/style.css`, `web/app.js`: arayüz. Ek bir derleme adımı yok; 2D SVG ile çizilir.

## Bilinen sınırlar: bu sürümün YAPMADIKLARI

- EBA'ya bağlı değil. `/api/graph` bugün yalnızca yerel corpus'tan okur; EBA için yalnızca `graph_provider` değişecek.
- Kaynak bağlantısı yok. Pasajlardaki MEB kaynakları ad ve tür olarak gösterilir, `url` alanı `null` ve
  `verified: false`dır. Hiçbir kaynağın bağlantısı doğrulanmadı.
- 3D görünüm yok; yalnızca 2D ağ var.
- İlişkiler tam değil. 60 ilişki elle yazıldı ve her biri pasajdaki bir cümleye dayanıyor; corpus'taki her olası
  bağlantı kapsanmadı. İlişki türleri (ön koşul, destek, ortak) yazarın yargısıdır; bu sınıflandırmanın doğruluğu
  ölçülmedi.
- "Nerelerde kullanılır" maddeleri ayrı bir kullanım örneği listesi değildir; ilişki kayıtlarından türetilmiştir.
- Özet, pasajın ilk paragrafının ilk iki cümlesidir (pasajdan alınmış, yeniden yazılmamış).
- Çalışma sırası, sınıf ve ön koşul derinliğinden hesaplanır; bir müfredat sırası olarak onaylanmamıştır.
- Telefon düzeni yalnızca CSS ile yapıldı; gerçek bir telefonda denenmedi.
- Hesap, kayıt, ilerleme takibi, sınav ve içerik düzenleme yok.
- İki komşu ötesi (ikinci derece) ilişkiler gösterilmez; yalnızca aranan konunun doğrudan ilişkileri.

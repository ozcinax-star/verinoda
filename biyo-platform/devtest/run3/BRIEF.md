# Revizyon 2: kullanıcının tam isteği (bu belge SPEC.md'nin üstündedir)

Bu bölüm, kullanıcının ilk mesajından aldığı isteğin tamamıdır; SPEC.md'deki maddeler buna göre okunur. Çelişki olursa
bu belge kazanır.

## Ürün

Lise öğrencileri için, EBA'ya entegre edilebilecek ve EBA'ya göre optimize edilmiş yeni bir öğrenim platformu bölümü.
Biyoloji odaklı. Verinoda'nın kullandığı 3D graph sistemine benzer bir yapı kullanır.

## Ana ekran

- Öğrenciyi karşılayan ekranda, bir nöron ağında lise seviyesindeki tüm biyoloji konuları yer alır.
- Yukarıda bir arama çubuğu vardır.
- Öğrenci bir konu adı yazınca, o konu 3D nöron grafiğinde belirir.

## Arama sonrası grafik

- Aranan konu grafikte tam merkezde, büyük ve okunabilir bir nöron olur.
- Merkez nöronun üstünde konunun adı ve sınıf düzeyi bulunur (9, 10, 11, 12 rakamlarından hangileri).
- Çevresinde, konuyla ilişkili diğer nöronlar bulunur. İlişki türleri:
  - konuyla uzaktan veya yakından bağlantılı konular,
  - bu konuyu öğrenmeden önce öğrenilmesi gereken konular,
  - bu konuya dair ipucu veren konular,
  - bu konuya yardımcı olabilecek konular,
  - bu konu başka bir konuyu öğrenmek için gerekliyse, o konu da bir ağla bağlanır.
- Eğer bir konu ortaokul fen bilimleri (fen bilgisi) müfredatına dayanıyorsa, bu bilgi listelerde gösterilir.

## Düğüme tıklama

- Tıklanan nörona zoom yapılır.
- Sağdan yumuşak geçişli bir panel açılır.
- Panelin en üstünde konunun kısa özeti yer alır.
- Panelde, konuyu öğrenmek için MEB materyalleri ve kaynaklar yer alır. Bu kaynaklar resmi ve doğru bilgi göstermek
  zorundadır (uydurma URL yok; doğrulanamayan kaynak "doğrulanmadı" işaretli olur).
- Panelde aşağı kaydırınca: bu konudan önce hangi konular öğrenilmeli, ve bu konu hangi konuları öğrenmek için gerekli
  bilgileri yer alır.

## Tasarım

- Ana tema öğrencide merak uyandırmalı.
- "Vibe coded" hissi vermemeli. Kaliteli, resmi ve göze uygun bir tasarım olmalı.
- Açık (aydınlık) bir tema. Her yerde parlayan ışık efekti yok. Sakin renkler, okunabilir yazı, net hiyerarşi.
- Telefon genişliğinde kullanılabilir olmalı.

## Teknik

- 3D nöron grafiği. Düz 2D çizim bu isteği karşılamaz. Sabit sürümlü bir 3D kütüphanesi kullanılmalı.
- Veri sağlayıcı tek bir fonksiyonda; EBA'ya bağlanmaya hazır (SPEC'teki `/api/graph` sözleşmesi).
- Verinoda'nın eşleştirme ve kanıt kurallarına uygun: her kenarın durumu (kanıtlı / çıkarım / bilinmiyor) gösterilir.

## Açık konu (bu turda çözülmeyecek)

- Ortaokul fen bilgisi konuları corpus'ta yok. İstek bunu gerektiriyor, ama corpus benchmark'ın gold verisiyle
  bağlı. Corpus'a ekleme, benchmark'ın yeniden tanımlanmasını gerektirir; bu tur yapılmaz. Ürünlerde bu özellik
  "henüz yok" diye açıkça belirtilir.

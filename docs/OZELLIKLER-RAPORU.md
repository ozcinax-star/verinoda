# Verinoda özellik raporu: ne işe yarar, neden eklendi

Bu rapor `docs/DESIGN.md` içindeki 148 bölümden (0-147) derlendi. Her girişteki "neden eklendi" ve "durum" satırları belgede yazanlara dayanır; belgede ölçüm yoksa "ölçülmedi" denir.

## Kısa özet

- **Verinoda nedir:** Kod hakkındaki sorulara, her iddia için dosya ve satır kanıtı veren ve kanıt yoksa "bilinmiyor" diyen bir kod analiz aracı.
- **Orijinal çekirdek (bölüm 0-12, yaklaşık 2026-09-23 / 09-26):** soruyu anlama, referansları doğru sürüme sabitleme, verimli getirme, kanıtlı ve geçersiz kılınabilir iddialar, insanın verdiği kararlar, hata ayıklama günlüğü, değişiklik incelemesi, davranış probları.
- **Sonradan eklenenler (bölüm 13-147):** büyük bölümü 2026-09-26 ile 2026-10-01 arasında eklendi. Kaynakları şunlar: rakip araçların taranması (150 civarı araç, 112 özellik, 106'sı Verinoda'ya uydu, liste `docs/BACKLOG.md`; gönderilen madde listeden silinir, şu an 16 satır kalmış), Minecraft/JVM modlama ihtiyaçları ve ajan çalışmalarında çıkan boşluklar.
- **Son bölüm (147):** `locate`, `coupled` ve `locate` arka plan servisi. Bunlar özellik listesinden değil, ajanın değişecek dosyaları bulmasını ölçen çalışmalardan doğdu.

## Durumun dürüst özeti

- **Gönderildi mi:** Bu rapor bölümlerin belgede "yapıldı" olarak kayıtlı olduğunu gösterir. Her birinin çalıştığı ayrıca doğrulanmadı.
- **Neye yaradı:** İnsan ve inceleme odaklı özellikler (değişiklik incelemesi, mimari kuralları, test haritası, hata günlüğü vb.) kendi küçük testleriyle ölçüldü. Ajanın görev başarısına etkisi ise ölçülmedi.
- **Ajanla yapılan ölçümler:** Gerçek hata raporlarından değişecek dosyaları bulma görevinde (`benchmarks/results/agent-compare-*`) ajan Verinoda'lı ve Verinoda'sız aynı sonucu verdi. Önceki çalışmalarda ajan Verinoda araçlarını 286 oturumun hiçbirinde çağırmadı. Son çalışmada (72 görev, 3 tekrar) `inject` kolunun farkı -0,73 [-2,58; +1,11] dosya, yani anlamlı değil.
- **Bunun yorumu:** Bu görev, tamamlanmış projelerde bir hatanın yerini bulmak. Ajan tek başına zaten iyi. Verinoda'nın asıl değeri büyük bir projeyi sıfırdan yazarken (yapı büyürken tutarlılık, yinelenen kod, var olmayan isimler) ortaya çıkabilir; bu henüz ölçülmedi.

---

# Bölümler

### 0. Hedefler, öncelik sırasıyla (hedefler; belgede D-numarası ve tarih yok)
- **Ne işe yarar:** Verinoda'nın dört önceliğini sıralar: kullanıcının ne sorduğunu anlamak, verdiği her referansı (repo, dosya, commit, PR, paket vb.) tam doğru sürüme sabitlemek, Graphify'dan verimli olmak ve kanıt disiplinini korumak. Çekirdek deterministiktir; index ve sorgu sırasında içeride LLM çalışmaz, dil anlama işini host agent (Claude Code veya Codex) yapar.
- **Neden eklendi:** Belgede gerekçe öncelik listesi olarak yazılı: cevaplar kanıtlı olmalı, kanıt yoksa tahmin yerine `unknown` ve sonraki adım verilmeli, kullanıcı eleştirisi test edilecek bir hipotez sayılmalı, geçmiş yalnızca eklenerek tutulmalı. Verimlilik hedefi Graphify'a göre ölçülür: token başına daha çok doğru bilgi, daha az yanlış ifade, benzer gecikme.
- **Durum:** Bu bölüm bir hedef listesidir; kendi başına ölçüm içermez. Ölçümler sonraki bölümlerde ve BENCHMARKS.md'de.

### 1. Soru anlama (D1-D9, durum tablosu 2026-09-23)
- **Ne işe yarar:** Kullanıcının (Türkçe veya İngilizce) sorusunu önce yapılandırılmış bir "soru planına" çevirir: alt sorular, bahsedilen kelimeler, referanslar ve her alt soru için kontrol edilebilir bir bitiş koşulu. Plan koda karşı deterministik olarak doğrulanır; kodda karşılığı olmayan veya belirsiz kelimeler için en fazla 3 çoktan seçmeli soru sorulur.
- **Neden eklendi:** examples/orders_app üzerinde ölçüm: benchmark sorularının 4 Türkçe çevirisinin hepsi 0 sonuç getirdi (İngilizce orijinallerde doğru semboller ilk 4'teydi). Türkçe normalizasyon hataları (`İndirim` -> `ndirim`, `ı` katlanmıyor, `db` atılıyor), bileşik soruların ikinci yarısının kaybolması ve sorunun nasıl anlaşıldığının hiçbir yerde saklanmaması da gerekçe olarak yazılı. Prototip 4 vakanın 4'ünde doğru sembolü 1-2. sıraya çıkardı.
- **Durum:** D1-D7 ve D9 "implemented", D8 "partial" (Not done: "beni yanlış anladın" geri bildirimini plan revizyonuna çevirme; `feedback.py` bunu çağırmıyor). 36/36 niyet ve bölümleme skoru kural yazarının kendi tablosunda (in-sample). Türkçe olgu geri çağırma orders_app'te 18 -> 30/32 (İngilizce 32), graphify_core'da 6 -> 12/37 (İngilizce 19). Niyet macro-F1, bölümleme, mention-linking ve açıklama metrikleri harness'te yok (belgeye göre "not in the harness").

### 2. Referans çözümleme (D10-D16, durum tablosu 2026-09-23)
- **Ne işe yarar:** Kullanıcının yazdığı URL, `owner/repo`, `pkg==x.y`, arXiv, DOI gibi referansları ayrıştırır ve "tam kastettiği sürüme" sabitler (kilit dosyası, kurulu paket, metindeki sürüm vb. bir öncelik sırasıyla). Çözülemeyen veya çelişen durumlar M1-M12 uyuşmazlık kataloğuyla sessizce geçilmeden raporlanır.
- **Neden eklendi:** Mevcut `parse_reference` 20 referans biçiminin 11'inde "tam kastedilen sürüm" kuralını bozdu: sürüm belirten 4 URL biçimi varsayılan dalın HEAD'ine sabitlendi, PR/issue URL'leri tarihsiz ikincil sayfa olarak kazındı, `#L100-L120` satır çıpaları düştü, etiketsiz arXiv son sürümü getirdi, "kullandığımız sürüm" kilit dosyaları okunmadığı için cevaplanamadı.
- **Durum:** D10, D11, D13, D16 "implemented"; D12, D14, D15 "partial" (örn. içerik eşleştirmesi yalnızca PyPI sdist için `research`'e bağlı; npm, crates, Go karşılaştırılmıyor). Ölçüm: 56 soru (44 in-sample, 12 held-out; hedef en az 60); held-out 12 ilk körü körüne çalıştırmada 11/12, ardından kaçan düzeltildiği için artık kör değil. requests 2.31.0 sdist'i `v2.31.0` ile 40/40, `main` ile 8/40 eşleşti.

### 3. Erişim verimliliği (D17-D22, durum tablosu 2026-09-23)
- **Ne işe yarar:** Kalıcı bir pasaj indeksi (BM25F sıralama + grafik tabanlı PageRank önceliği) kurar ve modele düz metin, "iskelet önce" biçiminde, bütçeye sığdırılmış çıktı verir. Build zamanında yapılabilen işleri (sembol aralıkları, receiver-call kenarları, hash önbelleği) sorgu anında tekrarlamaz.
- **Neden eklendi:** Profil (graphify_core, 226 dosya): soru başına medyan 6,9 s (`verinoda query`) ve 8,0 s (analyze), Graphify ise 35-94 ms (bellekte) veya 0,6-1,6 s (CLI). Zamanın %74-93'ü her soruda tüm dosyaları taramaya gidiyordu; sessiz 400 dosya sınırı 859 dosyanın 459'unu taranmamış bıraktı; JSON çıktının %52-61'i zarf/neden/kenar yüküydü.
- **Durum:** D17-D22 "implemented". Hedefler "track'in ölçümünde karşılandı": sıcak sorgu medyanı 30-34 ms (hedef <50 ms), soğuk CLI 0,59 s (hedef <1 s). Held-out kümede metin biçimi 26/33 olgu (1.413 token/soru) ile Graphify'ın 17/33'ünü (1.659) geçti, JSON'da geçmedi (19/33). graphify_core in-sample. Not done: indeksten sonra düzenlenen dosyalar sorgu anında yeniden indekslenmiyor, yalnızca raporlanıyor. Bölümde ayrıca soru şekline göre bütçe seçeneği (token -%8,3) kayıp olgu nedeniyle varsayılan olarak kapalı bırakıldı.

### 4. Güvenilir iddialar: kesin kanıt ve geçersizlik (D23-D31, durum tablosu 2026-09-23; D31 2026-09-25)
- **Ne işe yarar:** Her iddiayı, kanıtladığı kod parçasına (sembol, imza/gövde hash'i, AST yolu) bağlar; dosya değişince iddiayı hemen "eski" saymak yerine yalnızca ilgili sembol gerçekten değiştiyse eskitir. Kelime benzerliği tek başına hiçbir şeyi "verified" yapmaz (D31), tanımlı olmayan roller ve kod adları yerine konmaz.
- **Neden eklendi:** 300 gerçek Graphify commit'inin tekrar oynatımında değişen dosyalardaki tanımların yalnızca %2,37'sinin AST'si gerçekten değişti; dosya düzeyinde eskitme iddiaların yaklaşık %97,6'sını boşuna eski işaretliyordu. Satır yeniden konumlandırma hatası da üretildi (`sys.exit(1)` 1164. satırdan 1090'a eşlendi). D31: 2026-09-25'te orders_app kopyasında yanlış cümlelerin "verified" statüsüne ulaştığı dört yol bulundu.
- **Durum:** D23-D31 "implemented". Notlar: ilişki iddiaları replay'de hâlâ %21 yanlış-eski oranı gösterdi (D24); karşı-hipotez probe'ları 45 iddialık etiketli sette in-sample (D27); çalışma zamanı gözlemi 533 Graphify testinde 1,39x CPU (araştırmadaki 1,23x'e karşı) (D28); D31 için held-out yanlış-cümle seti henüz yok.

### 5. Teslim planı (belgede D-numarası yok; tarih yok)
- **Ne işe yarar:** Round 3'ün nasıl uygulanacağını sıralar: önce tek bir schema v3 göçü, sonra arama, soru anlama, güven motoru, runtime/hassas çözümleme ve referans çözücü izlerinin paralel geliştirilmesi; ardından entegrasyon (CLI, MCP, skill'ler), ölçüm ve düşmanca kabul denetimi.
- **Neden eklendi:** Belgede ayrı bir gerekçe yok; bölüm bir iş sıralaması olarak yazılmış (belgede gerekçe yok).
- **Durum:** Belgede bu bölüm için ayrı ölçüm yok. Yürütme durumu bölüm 0 öncesindeki durum tablosunda (D1-D31) izleniyor.

### 6. İnsanın verdiği kararlar (D33, 2026-09-25 bulgusu)
- **Ne işe yarar:** "SQLite'tan PostgreSQL'e geçelim mi?" gibi gelecek seçim sorularında Verinoda asla seçim yapmaz: kanıt toplar, insana soru sorar, insanın seçimini bir karar kaydı olarak saklar ve sonra kodun bu karara uyup uymadığını kontrol eder (`decide check`, guard'lar). Cevabın statüsü her zaman `human_decision_required` olur, `met` olmaz.
- **Neden eklendi:** Gelecek seçim sorusu akış sorusu sanılıp `met` döndü ve soruyu cevaplamayan yol iddiaları verdi; ayrıca kayıtlı bir karar hiçbir şey tarafından zorlanmıyordu (yeni bir modülün `sqlite3.connect` çağırması `notes`, `challenge` ve `verify`'dan geçti).
- **Durum:** D33 "partial". Guard mutasyonlarında (54 vaka + 22 inceleme formu, hepsi in-sample) VIOLATED hassasiyet 1,00 / geri çağırma 1,00; eski ham regex taraması aynı orders_app vakalarında tp 7, fp 8, fn 6. `decide check` örneklerde medyan 42 ms, tüm Verinoda ağacında yaklaşık 2 s. Karar niyeti yönlendirmesinde son held-out kümede (20 soru) hassasiyet 0,83, geri çağırma 0,50; geri çağırma çıtası 0,85 karşılanmadı ve temiz held-out küme kalmadı. Yapılmadı: analyze etki sorularının ihlalleri içermesi, UI'da karar rozeti, `exclusive`/`layering` iddiaları.

### 7. Hata ayıklama döngüleri (D34, 2026-09-25)
- **Ne işe yarar:** Bir hatayı düzeltmeye çalışan agent'ın denemelerini kalıcı bir deftere (`verinoda debug start/try/status/close`) yazar: her denemenin hangi kod ağacında koştuğu, hata imzası ve sonucu. Testi düzenleme, hatayı maskeleme, eski duruma dönme, aynı hatanın tekrarı gibi döngüleri tespit edip durdurur ve differential/bisect gibi stratejiler önerir.
- **Neden eklendi:** `experiment run` ile hata düzelten bir agent birbirinden bağımsız dört koşu üretti: hangi kodda koştukları kaydedilmemişti (`commit_sha` NULL), ikisi birebir aynı ağaçtı, istisna yalnızca `stdout.txt`'teydi. Tipik döngüler: kodu değil testi düzenlemek, `.get(k, 0)` ile hatayı maskelemek, aynı fikri tekrar denemek.
- **Durum:** D34 "partial". debugloops_v1 (12 oturum, 8 döngülü, 4 kontrol; kuralları yazan kişi hazırladı, in-sample): kesin kural hassasiyeti 11/11, döngü geri çağırma 8/8, 0/4 kontrol durduruldu, en iyi strateji 8/8. İmza ayrıştırıcı 30 log fikstüründe 30/30 (düzeltmelerden sonra); 10 held-out gerçek logda 10/10 (iki düzeltmeden sonra). İki ayrı incelemede 27 + 20 bulgu düzeltildi. Yapılmadı: protokolle ve protokolsüz gerçek agent oturumu karşılaştırması, Gradle/Maven koşuları, oturum başına yeniden kullanılabilir kopya (büyük ağaçlarda yavaş: 2.341 dosyada medyan 2,4-5,0 s). Sezgisel kuralların hassasiyeti ölçülmedi.

### 8. Değişiklik incelemesi (D35, 2026-09-25)
- **Ne işe yarar:** `verinoda review` bir diff'in hangi tanımları değiştirdiğini, kimleri etkilediğini (bağımlılar), hangi endişe türlerine dokunduğunu (kalıcılık, güvenlik, performans, public API, config, giriş noktaları) ve hangi testlerin ona ulaştığını raporlar; ilk okunacak satırları bir karakter bütçesine sığdırır.
- **Neden eklendi:** Eski `map --view impact` tüm dosyaları tohumlayıp listeleri sessizce kesiyordu (tek fonksiyonluk değişiklikte 80+ sembol, 40 dosya); gerçek çağıran bir `Cls::method` kaydı hiç kenar değildi. Hiçbir şey değişikliği endişeye göre sınıflamıyor, diff'i testlere bağlamıyor, skill'de düzenleme sonrası inceleme adımı yoktu.
- **Durum:** D35 "partial". 36 dev + 11 held-out değişiklikte: dev (in-sample) hassasiyet 0,92 (66/72), geri çağırma 62/62; held-out hassasiyet 0,79 (çıta 0,8 karşılanmadı), geri çağırma 18/18; sonraki düzeltmelerle 0,81 (artık temiz değil). Grafik yüklüyken medyan 0,19-0,21 s (örnekler), 380 dosyalık kopyada 1,8 s. Etki alanı impact görünümüne göre %67-100 daha küçük. Yapılmadı: bulguların iddia olarak saklanması, değişen satırların test kapsamı, iç içe döngü kuralları, Python dışı değer akışı; agent oturumunda etkisi ölçülmedi.

### 9. Değişen fonksiyonları yoklama (D36, 2026-09-25)
- **Ne işe yarar:** `verinoda probe` değişmiş bir Python fonksiyonunu üretilen girdilerle hem base commit'te hem çalışma ağacında çağırıp davranışın nerede farklılaştığını minimal örnekle gösterir. Yan etkili (dosya, ağ, veritabanı) fonksiyonlar önce statik kapıda ve çalışma anında denetim kancasıyla engellenir.
- **Neden eklendi:** Doğru görünen ve testleri geçen kod, testlerin kullanmadığı girdilerde yanlış olabilir: orders_app'te `>` yerine `>=` ve `round` yerine `int` kesmesi beş testi geçiyor. Mevcut araçlar yalnızca mevcut testlerin zaten kontrol ettiğini söyleyebiliyordu. Tasarımın prototipi 315 girdide sınır değişikliğini, kesmeyi ve yeni bir `OverflowError`'u buldu; denk yeniden yazımda 0 fark.
- **Durum:** D36 "partial". 49 elle yazılmış fikstür (in-sample): 22/22 tespit (testlerin kaçırdığı 20/20), davranışı koruyan 11 düzenlemede 0 fark, kapı 9/9 ret ve 0/4 yanlış ret, probe başına medyan 2,0-2,1 s (çıta 20 s); otomatik mutantlar 25/25 öldürüldü (çıta %60). Sınır madenciliği olmadan 19/22. Yapılmadı: `review --probe`, ikinci minimizasyon turu, statik eşzamanlılık sinyali, container izolasyonu (hiç çalıştırılmadı); yalnızca Python, yöntemler için literal argümanlı kurucu çağrısı şart.

### 10. Tam adlar ve taze index (D37, 2026-09-26)
- **Ne işe yarar:** Trace, etki haritası ve `node_inspect` için tek bir tam-ad çözücü kurar (kopyalar ve test/örnek kodu projenin kendi koduna yol verir, eşitlikte "ambiguous" listelenir, benzer ad asla yerine konmaz). Aynı anda tek build için dosya kilidi ekler ve her okumada "N dosya index'ten sonra değişti" uyarısı verir.
- **Neden eklendi:** Beş kişilikli bir kıdemli inceleme (2026-09-26): Verinoda'nın kendi repo'sunda `trace` ve `node_inspect` kopyadaki fonksiyona çözüldü; `NoSuchThing` hedefi 80 etkilenen verdi ve çözülemedi diye raporlanmadı; iki `update` arka arkaya çalışınca `[WinError 2]` ile üç dakikalık `scan --force` önerildi; düzenleme sonrası `analyze` 92-108 s yenileyip 0 iddiayla "unmet" döndü.
- **Durum:** D37 "implemented". Sekiz benchmark seti değişmedi (her soru ve yaklaşım ana koşuyla aynı). Başka bir incelemede 11 bulgu düzeltildi (arka plan güncellemesinin projenin kendi `verinoda/` paketini çalıştırması güvenlik açığı dahil). Not done: dosya bazlı artımlı güncelleme (yenileme hâlâ tam graf yeniden kurulumu), düz kelimelerde trace hâlâ benzerlikle çözüyor.

### 11. JVM geri çağırmaları ve mod giriş noktaları (D38, 2026-09-26)
- **Ne işe yarar:** Java/Kotlin'de metot referansıyla verilen geri çağırmalar (`RepairScheduler::tick`, `serverTick`) için ayrı bir `registers` ilişkisi ekler; trace bu yolu "framework sonra çağırır" notuyla gösterir, etki analizi bunları da izler. Fabric/NeoForge giriş noktalarını (`fabric.mod.json`, `@Mod`, `@SubscribeEvent`, mixin işleyicileri) ve JVM yazma noktalarını (dosya yazma, `setDirty()` vb.) tanır.
- **Neden eklendi:** Mod kodu geri çağırma güdümlü: `END_SERVER_TICK.register(RepairScheduler::tick)` gibi satırlar için grafta kenar yoktu; bu yüzden `trace` yol bulamadı, `serverTick` veya `handleStoke` değişikliğinin etkisi boş görünüp güvenli sanıldı, `map --view dataflow` iki örnek modda boştu. Metot referansını `calls` yapan ilk deneme (d70b801) benchmark skorlarını oynatıp geri alındı (3c066ec).
- **Durum:** D38 belgede ölçümlü. Kenarlar: forge_mod 2, glow_mod 8; giriş noktaları 10'ar; dataflow yolları 0 -> 2. Dokuz fastbench setinde 0 olgu ve 0 negatif değişim. Maliyet en büyük mod korpusunda yaklaşık 1 s (bir kez hesaplanıp saklanıyor). Yapılmadı: analyze'ın geri çağırmalar üzerinden akış iddiası kurması, lambda geri çağırmaları, statik başlatıcıdaki referanslar; giriş noktaları ve yazma noktaları metin sezgisidir.

### 12. Dürüst hükümler (D39, 2026-09-26)
- **Ne işe yarar:** `analyze` sonunda çalışan bir "hüküm kapısı": soruyla ilgisiz, eksik veya yalnızca kopya/vendor kodundan gelen cevabın alt soruyu `met` yapmasını engeller (`met_with_inference` veya `weak` olarak işaretler); çözülmemiş çağrı yerlerini sayar, "neden" sorularında gerekçe aramaz ve küme farkı sorularını (`not_supported`) reddeder. İddiayı silmez, hükmü yükseltmez.
- **Neden eklendi:** Beş kişilikli inceleme `analyze`'ın ilgisiz veya eksik cevaba `met` dediğini buldu: 6 yargılanmış backend sorusunun 4'ü, 10 JVM sorusunun 4'ü. Örnek: `BaseEventLoop.call_soon` çağıranları 1 olarak `met` verildi, kodda 7 `self.call_soon` ve 43 `_loop.call_soon` var.
- **Durum:** D39 ölçümlü (verdict-audit: 17 tuzak, 22 kontrol; dev 23, held-out 16). Yanlış `met` dev'de 9/23 -> 0/23, held-out'ta 8/16 -> 1/16; korunan kontroller dev 13/13 -> 12/13, held-out 7/9 -> 7/9. Dokuz fastbench setinde 0 olgu kaybı. Kapı soru başına birkaç milisaniye. Yazar iki kümeyi de kendisi yazdı (in-sample uyarısı). Yapılmadı: kontroller sözcüksel (eş anlamlılar eşleşmez), koddaki yorum/docstring'de yazan gerekçe aranmıyor, küme farkları hesaplanmıyor yalnızca reddediliyor.

### 13. Sorgu siralamasi / Query ranking (D40, 2026-09-26)
- **Ne ise yarar:** `verinoda query` sonuclarinda sorunun asil sordugu kod, ayni kelimeleri tekrar eden test dosyalarinin onune gecer. Sorunun adini verdigi dosya ve modullere ek agirlik verilir.
- **Neden eklendi:** Senior review'de (gap 10, yuksek) query'nin testleri ilk siraya koydugu ve modul adlarini yok saydigi bulundu; ornegin Verinoda'nin kendi reposunda 4 sorunun 4'unde 1. veya 2. sonuc bir testti. Gerekce: test adlari test ettikleri kodun kelimelerini tekrar ediyor, BM25F ad alanini odullendiriyor.
- **Durum:** 37 soruluk dev setinde ilk sirada dogru dosya 13/37 -> 28/37, MRR 0.535 -> 0.836, ilk sonucu test olan soru 10/34 -> 1/34 oldu. Set in-sample (kurallar ona bakilarak yazildi), bagimsiz held-out set sonra calistirilacak ("burada raporlanmadi"). Fastbench'te 2 olgu kazanildi, kayip yok. "Not done": "string/copy/select" gibi sozluk kelimeleri modul adi sayilip gurultu uretiyor, "allowed" kelimesi `allowlist`'e ulasmiyor, projenin icindeki kopyalar (ornegin `worked/mixed-corpus/raw/`) gercek kodun yaninda siralanmaya devam ediyor.

### 14. Kodun yanindaki belgeler ve resimler / Documents and images next to the code (D41, 2026-09-26)
- **Ne ise yarar:** Depodaki PDF, Word, Excel, PowerPoint dosyalari ve ekran goruntuleri (Windows'un kendi OCR'i ile) okunur; icerikleri arama, `analyze` ve kanit olarak kullanilabilir.
- **Neden eklendi:** Graphify ile dis karsilastirmada, Verinoda'nin PDF raporlarini, tasarim belgelerini ve ekran goruntulerini disarida biraktigi bir acik olarak listelendi; projenin karar ve sartnameleri sik sik orada duruyor.
- **Durum:** Ornek projede sorgu PDF sayfasini ve OCR'lanan ekran goruntusunu buldu (Turkce soru goruntudeki Turkce metni buldu); `analyze` PDF sayfasini `statically_verified` kanit olarak verdi, PDF degisince iddia `stale` oldu. Fastbench'te 9 sette 0 fark. "Not done": taranmis PDF (metin katmani yok), ses/video, macOS ve Linux'ta OCR, diyagramlarin yapisi.

### 15. Ayni graf ile daha hizli guncelleme / A faster update with the same graph (D42, 2026-09-26)
- **Ne ise yarar:** `update` komutu ayni sonucu uretip daha kisa surede biter (native Leiden kumeleme, daha hizli graph.json yazimi, onbellekler).
- **Neden eklendi:** Profil, Verinoda'nin kendi reposunda (yaklasik 2.400 dosya) tek satirlik bir duzenlemeden sonra update'in 31 sn surdugunu gosterdi (kumeleme 3.4 sn, graph.json iki kez yazimi yaklasik 2.5 sn vb.).
- **Durum:** Ayni duzenlemeler iki kopyada olculdu: 31-38 sn -> 27-33 sn (yaklasik %12). Ilk olcumde 31.3 -> 24.0 sn. Cikti dort korpusta ayni dogrulandi. "Not done": degisiklikle orantili guncelleme (yaklasik 20 cross-file gecisi hala tum dosyalari okuyor).

### 16. Java icin ad kontrolu / Name check for Java (D43, 2026-09-26)
- **Ne ise yarar:** `verinoda check` artik Java'da uydurulmus API adlarini yakalar (ornegin `PlayerEntity` yerine `Player`); derleme calistirilmaz, sinif dosyalari okunur. Yanlis Mixin hedefleri de yakalanir.
- **Neden eklendi:** Check sadece Python'u kapsiyordu; eski mappings ile egitilmis bir ajan Minecraft modlarinda `getMainHandStack`, `spawnEntity` gibi adlar yaziyor ve yanlis Mixin hedefi ancak oyun baslarken patliyordu.
- **Durum:** Gercek bir Fabric modunda (407 dosya, 126.848 nokta) 0 absent, 2.981 unknown (%2.3), 4.7 sn sicak; 8 plantlanmis hatanin 8'i dogru en yakin adla yakalandi. "Not done": Kotlin ve Groovy kaynaklari, tipe gore overload cozumu, gorunurluk, Maven classpath, Loom disi Gradle classpath'i.

### 17. Degisen dosyalar hemen, graf arka planda / The changed files now, the graph in the background (D44, 2026-09-26)
- **Ne ise yarar:** `update --fast` sadece degisen dosyalari hemen indeksler ve tam graf yenilemesini ayrik bir arka plan surecine birakir; graf eskiyse bunu acikca soyler.
- **Neden eklendi:** D42'den sonra bile Verinoda'nin reposunda update 27-33 sn suruyordu; her duzenlemeden sonra `index_update` cagiran bir ajan bu kadar bekliyordu. Tum gecisleri artimsal yapmak vendored pipeline'in buyuk bir yeniden yazimi olurdu.
- **Durum:** Bir fonksiyon eklenince `update --fast` 3.4 sn (surecle 4.3 sn) vs `update` 27-33 sn; arka plan grafi ile `scan --force` ayni 29.039 dugum ve 68.611 kenari verdi. "Not done": graf kurulumu hala tum korpusu okuyor, yani graf bir duzenlemeden sonra bir derleme suresi kadar geride kaliyor.

### 18. Kotlin icin ad kontrolu / Name check for Kotlin (D45, 2026-09-26)
- **Ne ise yarar:** Ad kontrolu Kotlin dosyalarini da kapsar ve karisik projelerde Java kodu projenin Kotlin siniflarini gorur.
- **Neden eklendi:** D43'ten sonra Kotlin dosyalari `not_checked` donuyordu; Java'dan ayni paketteki bir Kotlin sinifina yapilan cagri yanlislikla absent sayilabilirdi.
- **Durum:** 6 plantlanmis addan 6'si yakalandi; kotlinpoet'te (86 dosya, 15.090 nokta) once 293 absent, duzeltmelerden sonra 10 (hepsi kotlin-reflect uzantilari, verilen classpath'te eksik bagimlilik); D43'teki Java modu degismedi (126.848 nokta, 0 absent). "Not done": arguman sayilari, alicisiz cagrilar, lambda `it` gibi tip cikarimi, Kotlin script dosyalari, Kotlin/JS ve multiplatform `expect`/`actual`.

### 19. TypeScript ve JavaScript icin import kontrolu / Import check for TypeScript and JavaScript (D46, 2026-09-26)
- **Ne ise yarar:** TS/JS dosyalarindaki importlar (olmayan dosya, kurulu olmayan paket, yanlis yazilmis export) TypeScript'in cozumleme kurallariyla okunarak kontrol edilir; sadece okur, calistirmaz.
- **Neden eklendi:** TS/JS dosyalari `not_checked` donuyordu; ajanin burada en sik uydurdugu ad bir importtur (olmayan yardimci dosya, kurulu olmayan paket, react-router 6 sonrasi `useHistory`).
- **Durum:** Gercek paketlerle bir ornekte 6 hatanin 6'si yakalandi; ky'de (67 dosya, 594 import) 0 absent, 0 yanlis `not_installed`. Sadece import kontrol edildigi acikca belirtilir (`check` TS degisikliginde 4 ile cikar). "Not done": cagrilar, uyeler ve tipler (TypeScript derleyicisi gerekir), bundler alias'lari, `.vue`/`.svelte`, `package.json` `imports`.

### 20. Bir metot ne zaman calisir / When a method runs (D47, 2026-09-26)
- **Ne ise yarar:** `verinoda when SYMBOL` bir metodun hangi olayda (ornegin "her sunucu tick'inin sonunda") ve hangi kosulla ("her 5 tick'te, ayar aciksa") calistigini kayit noktalarini ve kosullari geriye izleyerek soyler.
- **Neden eklendi:** "Bu ne zaman calisir?" bir mod metodu hakkindaki ilk sorudur; lambda ile yapilan kayitlar graf'ta yoktu ve cagrinin kosulu sadece kaynakta duruyordu, ajan cevap icin uc metot okumak zorundaydi.
- **Durum:** Ozel bir Fabric modunda (4.584 dosya) 1.269 `registers` kenari (785'i lambdadan), 159'unda gecikme var; kabul sorusu tick kaydi ve her-5-tick kosuluyla dosya:satir ile cevaplandi. Fastbench'te 0 fark. Kosullar degerlendirilmez, `strong_inference` olarak isaretlenir. "Not done": Kotlin lambdalari, anonim sinif dinleyiciler, `@SubscribeEvent` gibi annotation kayitlari, kaydin kendi kosullari.

### 21. Mixin kenarlari / Mixin edges (D48, 2026-09-26)
- **Ne ise yarar:** Bir Mixin handler'inin oyunun hangi metodunun icinde, hangi noktada calistigi ve onu iptal edip edemeyecegi grafta kenar olarak tutulur; `query`, `node`, `when` ve `analyze` bunu gosterir.
- **Neden eklendi:** Handler grafta sadece hedef sinifa tek bir `references` kenariydi; "ne yaratiklarin dogmasini engelliyor" sorusu ne handler'i ne de ne yaptigini buluyordu.
- **Durum:** Ozel bir Fabric modunda 28 Mixin sinifindan 39 injector kenari, hepsinde hedef metot ve nokta var (onceden 7'sinde bozuk veya eksikti); Turkce kabul sorusu `checkSpawnRules` HEAD enjeksiyonuyla cevaplandi. Fastbench'te 0 fark. Denenip geri alinanlar: tohum cevirilerine daha yuksek agirlik, Turkce unlu daraltmasi (ikisi de olgu kaybettirdi). "Not done": hedef descriptor'in bytecode kontrolu, `@Shadow` uyeleri, Kotlin ile yazilmis Mixin'ler, NeoForge access transformer'lar.

### 22. GameTest kaydi ve bir degisikligin calistirmasi gereken testler / GameTest registry and the tests a change should run (D49, 2026-09-26)
- **Ne ise yarar:** Hangi GameTest siniflarinin gercekten calistigi (`fabric.mod.json`'da kayitli olanlar) okunur ve bir degisiklik icin calistirilmasi gereken testler en yakindan baslayarak siralanir; hic kayitli olmayan test uyari olarak belirtilir.
- **Neden eklendi:** Etki gorunumu dort import hop'u icindeki tum test dosyalarini listeliyordu; ana sinifi her ozelligi iceri alan bir modda tek dosyalik degisiklik icin 66 test dosyasi cikiyor, hangisinin calistigi veya en yakin oldugu belli degildi.
- **Durum:** Ozel bir Fabric modunda (89 GameTest sinifi, hepsi kayitli) bir NPC dosyasindaki degisiklik icin once o NPC'nin uc test sinifi, sonra komut sinifi uzerinden uc tane geldi; onceden 66 dosya alfabetik. "Not done": NeoForge (`@GameTestHolder`), `@GameTest` olmayan Fabric client testleri, calisma zamani secimi (`runGameTest` filtresi).

### 23. Backlog maddeleri ve kod yorumlari / Backlog items and code comments (D50, 2026-09-26)
- **Ne ise yarar:** Kodda `// why: 12.3 - ...` gibi yorumlarla anilan numarali backlog maddeleri koda baglanir; `verinoda backlog` bir satirdan veya sembolden "neden boyle" maddesine, ya da maddeden onu anan koda gider.
- **Neden eklendi:** Numarali backlog tutan projelerde kod yorumu bir satirdan nedenine giden en kisa yoldur, ama Verinoda yorumu metin, backlog'u belge olarak okuyordu ve ikisi arasinda baglanti yoktu.
- **Durum:** Ozel bir modda 548 madde, 383'u 2.444 yorum satirindan anilmis; guard'in govde aramasindaki `WeakReference` satiri alan uzerinden 69.3 numarali maddeyi dondurdu. "Not done": baska formatlar (issue tracker, `- [ ] 12.3` listeleri), maddeden `analyze` iddiasi (simdilik sadece `query` ve `node_inspect`), dokumantasyon dosyalarindan anilan maddeler.

### 24. Java erisimi, constructor tipleri ve classpath'in referans olarak kullanimi / Java access, constructor types and the classpath as a reference (D51, 2026-09-26)
- **Ne ise yarar:** Java kontrolu artik erisim kurallarini (private/protected) ve constructor parametre tiplerini de denetler; `verinoda api` Java siniflarinin gercekte neler sundugunu classpath'ten gosterir.
- **Neden eklendi:** Kontrol bir metodu yalnizca ad ve arguman sayisiyla biliyordu: `protected` olan `cas.isImmobile()` ve `BlockPos` ile `new ChunkPos(pos)` derlenmeyecek halde gecti; ajan da bir oyun sinifinin ne sundugunu soramiyordu (`verinoda api` sadece Python okuyordu).
- **Durum:** Kabul satirlari beklendigi gibi (`isImmobile()` ve `new ChunkPos(pos)` absent, dogru olanlar var); tum modda (407 dosya, 127.176 nokta) 0 absent; `verinoda api` ChunkPos icin 2 sn. "Not done": metot arguman tipleri (sadece constructor'lar tiple eslesiyor), protected'in alici kurali, proje kaynaklarinin modifier'lari, SCIP sembolleri.

### 25. Datapack'ler: fonksiyon cagrilari, entity tag'leri ve scoreboard objective'leri / Datapacks: function calls, entity tags and scoreboard objectives (D52, 2026-09-26)
- **Ne ise yarar:** `.mcfunction` dosyalari grafa girer (cagrilar, zamanlanmis cagrilar), Java ile datapack'in paylastigi entity tag'leri ve scoreboard objective'leri eslestirilir; `verinoda datapack` hic eklenmeyen kontrol edilen tag'leri, hic okunmayan yazilan objective'leri ve olmayan fonksiyon cagrilarini listeler.
- **Neden eklendi:** Datapacki olan bir modda davranisin buyuk kismi `.mcfunction`'da duruyor (olculen ozel modda 628 dosya) ve graf bunu hic okumuyordu; Java'nin kontrol ettigi ama hicbir seyin eklemedigi tag gibi hatalar (avci vampir hatasi) gorunmezdi.
- **Durum:** Ozel modda 314 fonksiyon, 143 tag, 46 objective; 12 tag kontrol edilip eklenmiyor, 5 objective yazilip okunmuyor. Kabul: duzeltmeden onceki commit'te iki tag listede cikti, duzeltilmis commit'te cikmadi. Ilk surum fastbench'te glow_mod'da 2 olgu kaybettirdi; mcfunction dosyalari arama icin veri dosyasi sayilinca 0 fark. "Not done": advancement/predicate JSON, `return run`, macro argumanlari, Java'nin kurulu bir stringle fonksiyon calistirmasi, Java'dan NBT `Tags`.

### 26. Bir logun stack trace'leri ve GameTest sonuclari / Stack traces and GameTest results of a log (D53, 2026-09-26)
- **Ne ise yarar:** `verinoda trace-log DOSYA` bir logdaki stack trace'leri ve GameTest sonuc satirlarini okur, projenin kendi karelerini metot dugumlerine eslestirir, oyun karelerini tek satira katlar ve trace'i ilgili teste baglar.
- **Neden eklendi:** Log elle okununca trace altmis Minecraft karesi ve arada iki mod karesidir; "`GameTestInfo.succeed` ile discard edildi" ile alani temizleyen komsu testin `succeed()` cagrisi arasinda hicbir baglanti yoktu.
- **Durum:** Kabul vakasinin sentetik bir logu ozel modda denendi (asil log saklanmamis): discard trace'i komsu teste "via GameTestInfo.succeed" ile baglandi, mod karesi gosterildi, alti oyun karesi tek satira katlandi. Log Verinoda'nin kendi calismasi olmadigi icin kanit `agent_report` turundedir, asla dogrulamaz (en fazla `weak_inference`). "Not done": birden fazla thread'in karisik trace'leri, `Caused by` zincirleri ayri trace olarak, NeoForge GameTest log satirlari, obfuscated (intermediary) kare adlari.

### 27. Shader'lar: uniform bloklari, Java yazicilari ve yansitilan sabitler / Shaders: uniform blocks, their Java writers and mirrored constants (D54, 2026-09-26)
- **Ne ise yarar:** Shader'daki `std140` uniform bloklarinin her alanini onu dolduran Java ifadesiyle eslestirir (`verinoda shader NAME`) ve iki tarafin uyusmadigi yerleri (alan eklenmis, sabit farkli) `--check` ile listeler.
- **Neden eklendi:** "`Weather.y` nereden geliyor?" sorusunda alan ile Java ifadesini sadece siralama baglar; bir tarafa alan eklemek hatasiz sekilde sonraki tum alanlari kaydirir, Java'dan yansitilan sabit tablolari da ayni sekilde ayrisabilir.
- **Durum:** Ozel modda 4 blok, 2 yazici; 20 alanli frame blogu tam eslesti, yagmur bilesenini dolduran Java cagrisi hem `shader` hem `analyze` ile bulundu, 16 girdili materyal tablosu tum degerlerde uyumlu; test fixture'indaki tek tarafli degisiklikleri `--check` bildirdi. "Not done": shader fonksiyonlari ve `#moj_import` kenarlari, dongu veya yardimci ile doldurulan bloklar, blok disi `uniform` degiskenleri, post-effect JSON.

### 28. Turkce adlandirilmis koda Ingilizce sorular / English questions over code named in Turkish (D55, 2026-09-26)
- **Ne ise yarar:** Metotlari Turkce adlandirilmis ama Ingilizce belgelenmis bir kodda Ingilizce soru da dogru metodu bulur (metodun ustundeki yorum metodun kendi metni sayilir, Ingilizce kelimelerin Turkce karsiliklari geriye dogru denenir).
- **Neden eklendi:** Turkce gelistiricinin modunda 30 soruluk set (15 Ingilizce, 15 Turkce) yazildi ve 15 Ingilizce sorunun 0'inda metot ilk uc sonuctaydi (MRR 0.023); Turkce sorularda 8 (MRR 0.486).
- **Durum:** Ilk uc sonuc 8/30 -> 18/30; Ingilizce 0 -> 8/15 (MRR 0.023 -> 0.501), Turkce 8 -> 10/15. Set gelistirme setidir (tohum kelimeler ona bakilarak secildi). "body lookup" sorusu hala metoduna ulasmiyor (otuzlu siralarda). Fastbench'te JSON retrieval 4 olgu kaybetti. "Not done": Turkce yorumlardan ogrenilen ciftler, tokenizer'da Turkce stemmer, tohumun otesinde Ingilizce adlara Turkce soru.

### 29. Bir analiz bir kez soylenir / An analysis said once (D56, 2026-09-26)
- **Ne ise yarar:** `analyze` cevabindaki tekrarlar kaldirilir: degisen dosyalar bir kez listelenir, bir iddianin belirsizlikleri tekrarsiz yazilir, "yolda bulunan" baglam iddialari en fazla altiyla sinirlanir.
- **Neden eklendi:** Bir ajan cevabin tamamini okur; dokuz benchmark setinde cevabin kendisi metnin yaklasik onda biriydi (pasajlar %56-73, baglam iddialari %10-12), ve indeks eskiyken degisen dosyalar dort kereye kadar tekrar listeleniyordu.
- **Durum:** Fastbench'te analyze olgulari 318 -> 318, karakterler -%1.6 (benchmark indeksleri taze oldugu icin eski-dosya tasarrufu orada gorunmuyor). Denenip birakilanlar: tum alt sorular karsilaninca daha kucuk pasaj butcesi (2.400 karakter: -%15 karakter, -6 olgu) ve yolsuz pencere satirlari.

### 30. Java overload'lari; cevaplarin bir insan gibi okunmasi / Java overloads; answers read as a person would (D57, 2026-09-27)
- **Ne ise yarar:** Ayni adli Java metotlari artik ayri dugumlerdir (`name_2`, `name_3`), cagrilar arguman sayisina gore dogru overload'a baglanir; ayrica akis sorularinin hatali yol cevaplari, imza degisikligi etkisi ve Turkce kok eslesmesi gibi alti hata duzeltilir.
- **Neden eklendi:** Gercek bir Minecraft modunda `analyze` cevaplari insan gibi okununca alti hata cikti: overload'lar tek dugumdu (135 grup, 283 metot; sahte "`moon` calls `moon`" oz-cagrisi), "araba her tick nasil hareket eder" sorusu ilgisiz yollarla `met_with_inference` cevaplandi, imza degisikligi sorusu metodun cagricilari yerine dosya listesi verdi, siniflarin dok yorumu bos veya ters pencere basildi, `yanıyor` -> `yani` gibi eslesmeler, JSON'da `more` listesi sigmiyordu.
- **Durum:** Fastbench'te metin ve analyze olgulari 315/318 degismedi, JSON retrieve 253 -> 262 (`more` icin yer, kayip yok), karakterler %0.2 icinde; TR/EN siralama seti (bench_tr) ilk uc 18/30'da kaldi. AST onbellek semasi (5) ve sidecar (8) degisti, bir kez `verinoda scan .` gerekir. Ilk genisleme filtresi (her yol) bir olgu kaybettirdi, bu yuzden sadece kok ve onek genislemelerine uygulandi.

### 31. Less noise in an answer (D58, 2026-09-27)
- **Ne ise yarar:** Verinoda'nin cevaplarindaki gereksiz satirlari (yanlis baglanmis isimler, kendi elestirisiyle curutulmus iddialar, bos alintilar) azaltir ve "neden" sorularini hizlandirir.
- **Neden eklendi:** Kendi kodu hakkinda soru sorulunca dort "unknown" cikiyordu (ornegin "query", "text" gibi sade kelimeler fonksiyonlara baglanmisti), bir "neden" sorusu 100 sn surup 60 sn butceyi asiyordu ve karar kaydi gerekcenin oldugu satirdan degil ilk satirdan alintilaniyordu.
- **Durum:** Gecmis gorunumu 90 sn -> 0.9 sn oldu (ayni kararlar). fastbench: analyze fact sayisi 318 -> 317, karakter -%0.3; kaybedilen fact bir olcum artefakti olarak birakildi.

### 32. Settings read by a string key (D59, 2026-09-27)
- **Ne ise yarar:** Bir mod'un ayarlarini noktali bir anahtarla (`car.door-wait-ticks` gibi) okudugu yerleri ve bu anahtarlarin YAML/TOML dosyasindaki tanimini bulup soruya cevap verir.
- **Neden eklendi:** "Kapinin ne kadar acik kalacagini hangi config anahtari belirler?" sorusu `unmet` donuyordu, cunku config gorunumu sadece ortam degiskenlerini biliyordu.
- **Durum:** fastbench'te fact sayilari degismedi, analyze karakteri +%0.2. Mod uzerinde once sorulan anahtar, sonra kelimelerin ayni sayida eslestigi anahtarlar geldi. Bulgu `strong_inference` olarak isaretli (hangi nesnenin okudugu izlenmiyor).

### 33. What an agent carries and cites (D60, 2026-09-27)
- **Ne ise yarar:** Agent'in her istekte tasidigi arac menusunu ve talimatlari kucultur, kod parcalarini satir numarali verir ki agent tum fonksiyonu degil sadece ilgili satiri alintilasin.
- **Neden eklendi:** Bir agent pilotunda agent 1-2 satirlik kanit yerine 14 satirlik fonksiyon araligini alintiliyordu, menu ve talimatlar (12,496 + 2,151 karakter) her turda tasiniyordu ve agent okuma sorusunda code_check calistirip ilk cagrisi index_update ile not_initialised aliyordu.
- **Durum:** Menu+talimat 14,647 -> 10,521 karakter (-%28, karar kaydi olmayan projede). fastbench 333 hucrede bir fact kaybedildi (heldout h08); karakter degisimi -%2.2 ile +%3.0 arasi. Agent uzerindeki etki sonraki dondurulmus build'de olculecek (henuz olculmedi).

### 34. A four-tool menu and a gateway (D61, 2026-09-27)
- **Ne ise yarar:** Agent'a gorunen arac menusunu dort araca (project_query, analyze, code_check, index_update) indirir; diger araclara `run_tool` adli tek bir gecit uzerinden ulasilir.
- **Neden eklendi:** Boyut esigi calismasinda (28 cift oturum) Verinoda oturumunun ilk turu her ciftte 3,933 token buyuktu, skill hic acilmamisti; Verinoda hic cagrilmayan oturumlar daha pahaliydi (medyan +%19). Hicbir repo boyutunda Verinoda'nin geri cekilmesi gerektigi bulunmadi.
- **Durum:** Menu 9,385 -> 4,253 karakter. Claude Code ilk turu +3,933 -> +2,177 token (-%45). Testler: run_tool node_inspect'e ulasiyor ve yanlis argumani bildiriyor. Not: "hic cagrilmadi" gosterildi, "hic gerekmedi" gosterilmedi (oturumlar sadece soru-cevapti).

### 35. Verinoda in the Grep the agent already runs (D62, 2026-09-27; under study)
- **Ne ise yarar:** Agent'in zaten kullandigi Grep'in sonucuna Verinoda'nin bildigi bilgiyi (tanim yeri, cagiranlar, cagrilanlar) ekleyen bir hook (b) ve "nasil/neden sorularinda once analyze cagir" talimati (a) olarak iki yolu dener.
- **Neden eklendi:** Benchmark'larda agent hep Grep ve Read'e gidiyordu; 15 hata duzeltme oturumunda hic Verinoda cagrisi yoktu.
- **Durum:** 25 soru x 5 kol = 125 oturumluk calisma: (a) fact 140 -> 136, medyan maliyet -%1; (b) 143 -> 133, +%1. Onceden yazilan kurala gore ikisi de acilmadi. Verinoda cagiran oturum: 14/25 (mevcut), 15 (a), 18 (b), 17 (ikisi). Verinoda'siz kiyasla mevcut hal: fact 65 -> 70, toplam maliyet -%19.

### 36. Ranked unknowns, declared types (D64, 2026-09-28)
- **Ne ise yarar:** Python isim kontrolunun "unknown" sonuclarini HIGH/MEDIUM/LOW diye siralar ve bildirilen tiplerden dogru kodu "exists" olarak tanir; boylece gercek yazim hatalari listenin basinda gorunur.
- **Neden eklendi:** Son 20 commit'in diff'inde 568 yerde 125 dogru kod ve 37 ekilmis yazim hatasi tek bir siralanmamis `unknown` listesindeydi; MCP cevabi 12,000 karakterde kesildigi icin ilk cevap 37 hatanin hicbirini icermiyordu. Ayrica `with raises(ImportError)` ve requirements dosyasindaki isteege bagli bagimliliklar yanlis `absent` veriyordu.
- **Durum:** Ilk MCP cevabinda ekilen hata sayisi 12/50 -> 42/50 (yazim hatalari), 12/50 -> 36/50 (uydurma isimler); `exists` 393 -> 448 ve 387 -> 451. Olcum kural yazarinin kendi orneginde (in-sample). Yapilmadi: `**kwargs` takibi, mypy/pyright, baska modulden gelen bayrak guard'i, proje kelimelerinin onbellegi; 0.8 esigi `rpeo` gibi yer degistirmeleri MEDIUM'da birakiyor; bazi orneklerde onceki/sonraki sayilar yeniden kosulmadi.

### 37. A graph with fewer false calls, and the tests of more ecosystems (D65, 2026-09-28)
- **Ne ise yarar:** Graph'taki sahte "calls" kenarlarini (ornegin `super().m()` ile kendine dongu) kaldirir, vendored/minified/uretilmis kodu urun kodundan ayirir ve daha fazla ekosistemin (C#, PHPUnit, GoogleTest, XCTest, Dart, Elixir) test dosyalarini taniyip C++ uzantilarini ekler.
- **Neden eklendi:** Sekiz acik repoda ve buyuk bir Python web framework'unde `EXTRACTED` etiketli olmayan kenarlar bulundu (framework'un 944 self-loop kenarinin 828'i `super()` kaynakli), minified chart kutuphaneleri bir Ruby reposunun dugumlerinin %31'ini kapliyordu, 591 dosyalik C# reposunda test projesi urun kodu sayiliyordu.
- **Durum:** Python framework'te self-loop 944 -> 118; Ruby reposu dugum 2,851 -> 1,981; C# test dosyasi tanima 61 -> 421/514; C# placeholder gecisi 4.15 sn -> 0.05 sn. Bedel: tipsiz yerel degiskenler uzerinden gercek ayni dosya kenarlari da kayboldu (Go'da yaklasik 5'te 1, Rust'ta 8'de 1, Ruby'de 4'te 1). Bu olcumler inceleme duzeltmelerinden once yapildi, sonra tekrarlanmadi.

### 38. Faster scans: the file list from git, one parse, one commit, a renamed search build (D66, 2026-09-28)
- **Ne ise yarar:** Tam taramayi hizlandirir: dosya listesini git'ten alir, Python dosyalarini bir kez parse eder, anchors icin tek commit atar ve arama indeksini ayri dosyada kurup yerine adlandirir (yarim indeks gorulmez).
- **Neden eklendi:** Django checkout'unun profilinde detect() yaklasik 18 sn harciyordu, Python dosyalari 2,888 dosya icin 9,209 kez parse ediliyordu, anchors her dosyayi iki kez okuyup her biri icin commit atiyordu, yari kurulmus search.db okuyucuya gorunebiliyordu.
- **Durum:** detect() profilde 17.7 sn -> 2.3 sn; anchors fazi 64.6/78.6 sn -> 11.7/18.6 sn. Makine diger build'lerle yukluydu, uctan uca sureler guvenilir degil (bos makinede tekrar gerekli). Yapilmadi: lexicon/anchors ayri parse, uc turetilmis gecis sirali, tanim araliklari graph dugumunde saklanmiyor.

### 39. Running a project's own tests safely (D63, 2026-09-28)
- **Ne ise yarar:** Bir projenin kendi testlerini calistirirken guvenligi saglar: guven kararini kullanici verir (`verinoda trust`, repo disinda saklanir), guvenilmeyen projenin testleri yalniz konteynerde calisir ve pytest/argv[0] kurallari sikilastirilir.
- **Neden eklendi:** Guvenlik incelemesi, varsayilanin her repo testini (yeni klonlanmis dahil) calistirmak oldugunu ve `.verinoda/config.json`, `@file`, `-p NAME`, `-o addopts`, yol ile argv[0] ve sembolik link kopyalama yollariyla arguman politikasinin asilabildigini buldu.
- **Durum:** Karar fonksiyonlari dogrudan cagrilarak once/sonra tablosu verildi; link kontrolu 2,561 dosyada 121-170 ms. Konteyner yolu bu makinede olculmedi (docker/podman yok), ilk gercek kosu CI'daki `container` isi. Yapilmadi: docker'siz OS duzeyi kisitlama (bubblewrap, Landlock, sandbox-exec, Windows low-integrity), Windows job-object limitleri, trust'in uzak URL ile anahtarlanmasi.

### 40. Issue-shaped questions: never refused for the drafted plan, answered in the question's language, restated briefly (D67, 2026-09-28)
- **Ne ise yarar:** Uzun, issue metni gibi sorulari reddetmez, sorunun diliyle cevaplar ve soruyu cevabin basinda tekrar tekrar basmak yerine kisa ozetler.
- **Neden eklendi:** 80 gercek issue metniyle yapilan modelsiz kosuda 4'u "plan gecersiz" diye reddedildi (10 referans siniri ile surum kontrolu catisiyordu), 4'u Ingilizce olmasina ragen Turkce cevaplandi (`I've` icindeki `ve` Turkce "ve" sanildi), 27'sinde ilk kod parcasi 6,000. karakterden sonra geliyordu.
- **Durum:** Cevrimdisi 80 metinde basarisiz plan 4 -> 0, Turkce/mixed algisi 4 -> 0. Secilmis 17 sorunun uzerinde: exit 2 3/17 -> 0/17, Turkce 4/17 -> 0/17, ilk kaynak satiri 6,000 sonrasi 14/17 -> 9/17. Yapilmadi: `has_turkish` niyet testi ayni hatayi tasiyor, ortak kelimelerin plan baglantilari, tekrarlanan belirsizlikler; retrieval skoruna etkisi olculmedi.

### 41. Verinoda's own files are not the project's: no index of them, no rebuild for them (D68, 2026-09-28)
- **Ne ise yarar:** `verinoda setup`'in yazdigi kendi dosyalari (skill, `.mcp.json` icindeki verinoda girdisi) projenin indeksine ve graph'ina girmez, yalniz onlar degistigi icin graph yeniden kurulmaz.
- **Neden eklendi:** Bu dosyalar korpusun parcasi sayiliyordu; graph'ta skill icin 16 baslik dugumu ve yorumlayici yolunu gosteren dugum vardi. Iki Verinoda kurulumu sirayla setup calistirinca dosyalar degisip graph her seferinde yeniden kuruluyordu: 2,623 dosyalik projede 22-40 sn, normalde yaklasik 1.5 sn.
- **Durum:** Setup tekrari 22-40 sn -> 5 sn civari (graph yeniden kurulmuyor), no-op update 0.7-0.8 sn. Dugum sayisi 9,496 -> 9,478. Yapilmadi: lexicon.build'in her degisimde tum birimleri yeniden eslemesi (~3.5 sn), duzenlenmis Markdown'in tum graph'i yeniden kurmasi, baska klasore kopyalanmis indekste eski Verinoda dugumunun kalabilmesi, arama indeksinde `.mcp.json` yolunun hala aranabilir olmasi.

### 42. Datapack functions called from Java (D69, 2026-09-28)
- **Ne ise yarar:** `verinoda datapack function ns:path` komutu artik bir datapack fonksiyonunu cagiran Java kodunu da (komut metni, identifier aramasi, yardimci metot) satir numarasiyla listeler; calisma zamaninda olusan adlar "dynamic" olarak bildirilir.
- **Neden eklendi:** Komut yalniz `.mcfunction` cagiranlarini listeliyordu; bir mod'da fonksiyonu asil tetikleyen Java'dir. Sorunu cikaran hatada projectile silahi yanlis varlik uzerinde fonksiyon calistiriyordu ve cevap iki onemli Java cagiranini vermiyordu. Var olmayan fonksiyonu cagiran Java sessiz bir hatadir.
- **Durum:** Ozel mod kopyasinda 4 fonksiyonun 7 Java cagiranindan 0 listeleniyordu, sonra 7/7. Toplam 68 Java cagrisi baglandi (23 urun kodunda); ilk 59'u elle okundu, 0 yanlis. `datapack` 2.28 -> 2.60 sn. Yapilmadi: Java cagrilarinin graph kenari olmasi (D71'de yapildi), Kotlin, advancement/predicate, kodla kurulan tablolar.

### 43. Entity tags Java adds through a constant, a conditional, the live set or a built name (D70, 2026-09-29)
- **Ne ise yarar:** "Kontrol edilip hic eklenmeyen tag" listesinin yanlis alarmlarini azaltir: sinifin kendi sabiti, kosullu ifade, `entityTags().add(...)` ve calisma zamaninda olusan ad dogru okunur.
- **Neden eklendi:** GitHub issue #2: elle kontrol edilen 5 satirin 4'u yanlis alarmdi (yalniz `/tag` ile elle eklenen dogruydu); liste yanlis satirlarla doluysa kullanilmaz hale geliyor.
- **Durum:** Issue'nun fixture'inda "never added" 5 -> 2. 430 Java dosyalik sentetik modda 0 tag -> 172 tag, 0 "never added". Ozel mod bu makinede yok; kabul kriteri mod'da degil, sekillerini kopyalayan fixture'da gosterildi. Yapilmadi: yerel/alanda tutulan tag, ust siniftan gelen sabit, `String.format` ile kurulan ad, Kotlin.

### 44. Java calls into datapack functions in the graph (D71, 2026-09-29)
- **Ne ise yarar:** Java'dan datapack fonksiyonuna giden cagrilari graph kenari yapar; boylece `when`, `trace`, etki analizi ve MCP araclari "bu fonksiyon ne zaman calisir?" sorusunda Java yolunu da gosterir. Klasorlu fonksiyon id'leri (`ns:dir/name`) de artik adlandirilabilir.
- **Neden eklendi:** D69 Java cagiranlarini sadece sorgu aninda okuyordu (42.4'te yapilmadi diye yazilmisti). Forge ornegindeki `when emberforge:debug/reset_forges` yararli cevap vermiyordu; klasorlu id dosya yolu sanildigi icin hic adlandirilamiyordu.
- **Durum:** Forge ornegi: once `not_found`, sonra tek yol (`ModEvents.onRegisterCommands` -> ... -> fonksiyon, `ForgeFunctions.java:23`). Sentetik 430 dosyalik modda 43 kenar, `graph_edges` 1.6 sn (sidecar yenilenirken odenir). Yapilmadi: Kotlin cagiranlar, fonksiyon tag'i (`#ns:tag`) uyeleri, advancement/predicate, iki yere kopyalanmis fonksiyonun ilk dugumune kenar.

### 45. Translation keys in Minecraft lang files (D72, 2026-09-30)
- **Ne ise yarar:** `verinoda lang` komutu mod'un dil dosyalarini karsilastirir: bir dilde eksik, fazla ve yinelenen anahtar, `%s` uyumsuzlugu, kodun istedigi ama hicbir dosyada olmayan anahtar ve kullanilmayan anahtar.
- **Neden eklendi:** Dil dosyalari birbiriyle uyumlu mu diye kimse kontrol etmiyor: ceviri eksikse Ingilizce gorunur, `%s` dusunce arguman kaybolur, kodun istedigi anahtar yoksa oyunda ham anahtar gorunur.
- **Durum:** Verinoda'nin kendi reposunda (iki ornek mod, 4 lang dosyasi, 330 dosya) hicbir uyumsuzluk bulunmadi. `unused_key` `strong_inference`, digerleri `statically_verified`. Yapilmadi: ayni namespace'li iki mod'u ayirma, sabit/yardimci/datagen uzerinden gecen anahtarlar, `§` bicim kodlari, MCP araci.

### 46. Extract the definition around a location (D73, 2026-09-30)
- **Ne ise yarar:** `verinoda extract` ile bir konum (traceback satiri, derleyici hatasi, `dosya:satir`) verilince etrafindaki fonksiyonu veya sinifi eksiksiz, satir numarali ve kanitli olarak cikarir.
- **Neden eklendi:** Eldeki konumdan tam birimi almak zordu: `node_inspect` indeks ve isim ister ve 30 satirda keser, `query` pasaj siralar, elle okumak fonksiyonun nerede basladigini tahmin etmeyi gerektirir.
- **Durum:** Indekssiz calisir (dosyayi o an parse eder), bu repoda cagri basina yaklasik 1.5 sn (cogu Python baslangici). 20+ test listelendi. Yapilmadi: yalniz en ic tanim, satir tabanli cikti tarayici, MCP araci, JVM frame'inde paket klasorune uymayan kaynak klasorler.

### 47. Dependency cycles and a minimal break set (D74, 2026-09-30)
- **Ne ise yarar:** `map --view cycles` ile birbirine dolasmis dosya gruplarini (dongu) ve onlari cozmek icin kesilecek en az bagimlilik kumesini, satir kanitlariyla gosterir.
- **Neden eklendi:** Mevcut bagimlilik gorunumu hangi bagimliliklarin dongu olusturdugunu soylemiyordu; upstream `find_import_cycles` hicbir komuttan erisilemiyordu ve buyuk bir dugumu onlarca ic ice halka olarak listeliyordu. Madge, dependency-cruiser ve Sonargraph hangi dosyalarin dolasik oldugunu ve neyin kesilecegini cevaplar.
- **Durum:** Verinoda'nin kendi reposunda 14 dongu, 94 dosya, 36 kesim (14'un 13'u kesin cozum). 1,000 dosya/9,000 bagimlilik sentetik: 1.6 sn. En buyuk dongu 41 dosya, `weak_inference`. Yapilmadi: paket duzeyi dongu, fonksiyon icindeki tembel import ayrimi, CI modu, buyuk dongulerde asgari olduguna dair kanit.

### 48. Graph exports: GraphML, Cypher, Obsidian and SVG (D75, 2026-09-30)
- **Ne ise yarar:** `verinoda export` ile graph'i GraphML, Cypher, Obsidian vault'u veya SVG olarak yerel dosyaya yazar; her kenar kanit satiri ve durumuyla gelir.
- **Neden eklendi:** Belgede gerekce yok (bolumde "Why" kismi yok, yalniz kararlar var).
- **Durum:** Verinoda'nin kendi graph'inda formata gore 5.3-12.7 sn (GraphML 8.0, Cypher 11.0, Obsidian 12.7 ile 1,260 not, SVG 5.3). Durum dagilimi: strong_inference 70,144, weak_inference 4,397, unknown 12. Hicbir kenar `verified` degil (cagri satiri okunmuyor). Yapilmadi: kenar basina derecelendirme, topluluklar, Obsidian Canvas, Bolt push; kod dosyasiz git reposunda traceback.

### 49. Commit, diff and revision search (D76, 2026-09-30)
- **Ne ise yarar:** `verinoda history` ile bir metnin hangi commit'te eklendigini/silindigini, commit aramasini ve iki revizyonun karsilastirmasini git'in kendisiyle yapar; commit kanittir.
- **Neden eklendi:** "`retry_budget` ne zaman ortaya cikti, ne zaman gitti?" sorusunun cevabi yoktu; analyze yalniz indekste hala duran sembol araligi uzerinde `git log -L` okuyordu, silinmis ismin araligi yoktur.
- **Durum:** Verinoda'nin 375 commit'lik reposunda `history text "import json"` 3.0 sn (70 commit). Yapilmadi: yalniz HEAD'den erisilen gecmis, merge commit'ler diff'lenmez, rename tespiti yok, working tree aranmaz, `analyze` henuz kullanmiyor, iddialar saklanmiyor.

### 50. Mermaid diagrams and a wiki outline (D77, 2026-09-30)
- **Ne ise yarar:** Graph'tan Mermaid diyagramlari (mimari, akis, sequence) ve sayfa basina diyagram iceren bir wiki ana hatti uretir; her ok kanit satirli bir iddiadir.
- **Neden eklendi:** Belgede gerekce yok (bolumde "Why" kismi yok).
- **Durum:** Olcum yok (olculmedi); testler listelendi (orders_app uzerinde 21 test). Yapilmadi: sayfalarda uretilmis metin yok (sadece `purpose`), dinamik cagri/reflection/DI cizilmez, sequence tek yol gosterir, sayfa kimlikleri baslik slug'i, tarayici icinde sayfa JS'i test edilmedi.

### 51. Agent instruction file lint (D78, 2026-09-30)
- **Ne ise yarar:** CLAUDE.md / AGENTS.md gibi ajan talimat dosyalarindaki yollari, npm script'lerini, make hedeflerini, paket ve komut adlarini projede gercekten var mi diye kontrol eder.
- **Neden eklendi:** Belgede ayri bir "Why" yok; karar listesi, talimat dosyasinda gecen yol/komutlarin agacta ve manifestlerde dogrulanmasini ve yanlis/eski olanlarin (buyuk-kucuk harf farki, tasinmis dosya) bulunmasini hedefliyor.
- **Durum:** Yalnizca CLI (MCP araci yok; README/ARCHITECTURE/UPGRADING'deki arac sayisi testi yuzunden adaptor yazilip geri alindi). `tests/test_agentlint.py` 8 test, incelemeden sonra 10 test daha. Yapilmadi: duz yazidaki adlar ("pytest ile calistir") okunmuyor, projenin kendi konsol komutunun alt komutlari kontrol edilmiyor, arac-paket tablosu sabit, Makefile ayrimi satir tabanli.

### 52. Filter syntax in query (D79, 2026-09-30)
- **Ne ise yarar:** `verinoda query` / `project_query` sorusuna `path:`, `lang:`, `symbol:`, `is:test`, `/regex/` gibi filtreler ve `OR` / `NOT` / parantez eklemeyi saglar.
- **Neden eklendi:** Belgede ayri gerekce yok; GitHub ve Sourcegraph soz diziminin alindigi yaziliyor. Filtre yoksa soru eskisi gibi siralanir, yani mevcut sonuclar degismez.
- **Durum:** `tests/test_query_filters.py` altinda 27 test listeli. Regex alt surecte 10 sn zaman siniriyla calisiyor. Yapilmadi: regex tek satirda eslesir (coklu satir yok), trigram index yok, indexin atladigi dosyalar (cok buyuk, binary, lock) eslesmez. Sure olcumu: regex'li filtre-sadece soru icin sureci baslatmak yaklasik 0.1 sn.

### 53. Secret scrubbing (D80, 2026-09-30)
- **Ne ise yarar:** Verinoda'nin kaydettigi log, deney ciktisi ve UI export'undaki sirlari (API anahtari, token, URL parolasi, e-posta, ortam degiskeni degerleri) `<redacted:KURAL>` ile degistirir; `verinoda secret-scan` ile elle de taranabilir.
- **Neden eklendi:** Belgede ayri "Why" yok; sirlarin yazildigi dort yer (deneyler, debug cikti, trace-log, ui export) icin yazma aninda temizleme yapiliyor ki paylasilan ciktilarda sir kalmasin.
- **Durum:** `tests/test_scrub.py` 66 test. Hiz: 7 MB'lik log yaklasik 0.45 sn, 6 MB JSON-satir logu yaklasik 1 sn (ilk surum 8 sn). Bulgular `strong_inference`. Yapilmadi: sekli olmayan sirlar bulunmaz, kisisel veriden yalnizca e-posta, `atlas.db` ve baska yollarla yazilan claim metinleri temizlenmez, kullanici tanimli kural/allowlist yok.

### 54. Rename preview (D81, 2026-09-30)
- **Ne ise yarar:** Bir sembolun adi degisirse hangi satirlarin (tanim, import, cagri, override, ayni adli yerel degisken, metin icindeki anma) etkilenecegini kanitiyla listeler; hicbir dosyayi degistirmez.
- **Neden eklendi:** Belgede ayri "Why" yok; rename'in neleri etkileyecegini tahmin degil kanit olarak gostermek hedefleniyor (tahmini baglar "mention" olarak ayri tutuluyor).
- **Durum:** `tests/test_rename_preview.py` 20 test; test_docs ve test_cli ile birlikte 130 gecti (Windows 11, Python 3.12). MCP araci yazilip test edildi ama arac sayisi 37'den 38'e cikacagi icin tutuldu; yalnizca CLI. Yapilmadi: tip denetleyicisi yok (alici tipi kalip ile okunur), `getattr`/string ile kurulan adlar, dosya/modul rename'i.

### 55. Declared vs used dependencies (D82, 2026-09-30)
- **Ne ise yarar:** `verinoda check --deps` ile manifestte bildirilen bagimliliklari kodda gercekten import edilenlerle karsilastirir: bildirilmemis (`missing`), sadece dolayli gelen (`transitive_only`), kullanilmayan (`unused`), yanlis grupta olan (`wrong_group`).
- **Neden eklendi:** Kod, kimsenin bildirmedigi bir paketi import edebiliyor (baska paket getirdigi surece calisiyor); kullanilmayan runtime bagimliligi veya sevk edilen kodun test aracini import etmesi gibi kaymalar oluyor. deptry bunu Python icin yapiyor; Verinoda manifestleri ve importlari zaten okudugu icin ikisinin birlestirilmesi yeterli.
- **Durum:** `tests/test_depcheck.py` ve ek testler gecti. Python (ortam varsa) `statically_verified`, digerleri `strong_inference`. Yapilmadi: JVM'de classpath olmadigi icin `missing` yok, Go/Cargo kontrol edilmiyor, giris noktasi/plugin kullanimi `unused` icin gorulmuyor. Sure olcumu belgede yok.

### 56. Complexity, code health and clones (D83, 2026-09-30)
- **Ne ise yarar:** Fonksiyon basina karmasiklik (cyclomatic, cognitive, ic ice derinlik, uzunluk, parametre) olcer, 1-10 arasi saglik puani verir ve neredeyse ayni kopya fonksiyonlari bulur. `verinoda health` komutu ve `review`'a yedinci "health" kaygisi olarak eklendi.
- **Neden eklendi:** Belgede ayri "Why" yok; degisen bir fonksiyonun sagliginin dusup dusmedigini veya yeni kopya yaratip yaratmadigini review'da gostermek hedefleniyor.
- **Durum:** `tests/test_health.py` 23 test. Olcumler sayim oldugu icin `statically_verified`, puan ve klonlar `strong_inference`. Bu depoda tum-agac klon aramasi yaklasik 22 sn, `verinoda health verinoda` yaklasik 40 sn (6133 fonksiyon, 207 dosya). Yapilmadi: esikler ayarlanamiyor, sadece sozdizimine bakar, review klonlari yalnizca degisen dosyada arar.

### 57. Dead code (D84, 2026-09-30)
- **Ne ise yarar:** `verinoda map --view dead` ile giris noktalarindan hic ulasilamayan dosya, sinif ve fonksiyonlari "olu kod" adayi olarak listeler; kodu canli tutabilecek dinamik kullanimlari (string ile anma, dekorator vb.) da yazar.
- **Neden eklendi:** Belgede ayri "Why" yok; baska bir yerden cagrilmayan kodu kanitiyla gostermek hedefleniyor.
- **Durum:** Olculdu: Verinoda'nin kendi kopyasinda 667 claim (43 `strong_inference`, 624 `weak_inference`); 5 guclu claim elle kontrol edildi, 5'i de gercekten olu. Yaklasik 4.5 dakika surdu. Bu sayilar inceleme duzeltmelerinden once alindi, yeniden olculmedi. Yapilmadi: yansima/DI ile cagrilar gorulmez, kutuphane acik API'si olu sayilabilir, yalnizca testle ulasilan kod canli sayilir.

### 58. MCP prompts (D85, 2026-09-30)
- **Ne ise yarar:** MCP istemcisinde hazir is akislari sunar: `review`, `onboarding`, `debug`, `pre_merge`. Biri secilince ajan dogru arac siralamasini ve raporlama kurallarini hazir alir.
- **Neden eklendi:** Ajan her oturumda rutin bir is (degisiklik incelemesi, projeyi tanima, hata ayiklama, merge oncesi kontrol) icin hangi araclari hangi sirayla cagiracagini yeniden bulmak zorunda kaliyor; code-review-graph da bunu MCP prompt olarak sunuyor.
- **Durum:** Olculdu: `prompts/list` 1.253 karakter, core `tools/list` 4.478 karakter (degismedi), prompt metinleri yaklasik 700-1.050 karakter. Testler `tests/test_mcp_prompts.py` ve `test_mcp.py`. Yapilmadi: adimlar sabit (proje once taranmaz), core menu 4.500 karakter sinirinin 4.478'inde, istemciler promptlari nasil gosterecegine kendisi karar verir.

### 59. Breaking vs compatible API change (D86, 2026-09-30)
- **Ne ise yarar:** `review` her acik (public) tanim icin tek bir hukum verir: `breaking` (kirici), `compatible` (uyumlu) veya `unknown`; kirdigi cagri yerlerini de listeler.
- **Neden eklendi:** Eskiden review sadece cagri yeri bulgulari veriyordu; "bu degisiklik kirici mi?" sorusunun cevabi bulgularin varligindan cikarilmak zorundaydi. Ayrica depoda hic cagrisi olmayan kaldirilmis/imzasi degismis bir fonksiyon "bulgu yok" gibi gorunuyordu, oysa depo disindaki cagirlar kirilir.
- **Durum:** Sadece ornek projelerde (in-sample) test edildi, bagimsiz set ve sure olcumu yok. `tests/test_review.py` icinde 13 test adi var. Yapilmadi: tip, donus tipi, varsayilan deger ve davranis karsilastirilmaz; Java paket-ici uyeler public sayilir; Python `__all__` okunmaz.

### 60. Decisions a diff touches (D87, 2026-09-30)
- **Ne ise yarar:** `review`, bir degisikligin dokundugu karar kayitlarini (decision record) "Decisions to read" bolumunde listeler: hangi kayit hangi satira ve hangi onceden tanimli kurala denk geldi.
- **Neden eklendi:** Kayit, yonettigi kodu ve korumalarini adlandiriyor ama diff inceleyen kisi hangi kayitlarin ilgili oldugunu ogrenmek icin `decide check` ciktisinin tamamini okumak zorundaydi. ADRian ve ADR-Toolkit bunu model ile yapiyor; burada kayit basligi yeterli oldugu icin model gerekmiyor.
- **Durum:** `tests/test_decision_reach.py` 18 test, in-sample; ADR bagli diff'lerden olusan benchmark yok. Eslesme hukum degil; review cikis kodu degismez. Yapilmadi: cagrilar metin olarak eslesir (alias, `sink=` gorulmez), guard `pattern`'i zaman siniri olmadan calisir, kaydi olmayan elle yazilmis ADR'ler eslesmez.

### 61. Missing mod dependencies and pack collisions (D88, 2026-09-30)
- **Ne ise yarar:** `verinoda datapack` ozetine iki bolum ekler: ayni dosyayi farkli icerikle gonderen mod/datapack carpismalari ve karsilanmayan mod bagimliliklari (eksik, surum uyumsuz, `breaks` ile kirilan). `--with` ile mod jar'i veya klasoru eklenebilir.
- **Neden eklendi:** Iki mod ayni dosyayi (ornegin tarif veya doku) gonderirse oyun yukleme sirasina gore birini gizler ve hicbir kaynak bunu tek basina gostermez; eksik veya yanlis surumlu bagimlilik de acilista coker.
- **Durum:** Olculdu: bu depoda 4 kaynak, 0 carpisma, 1 ayni kopya, 2 dis bagimlilik; `packset.check` uc calismanin medyani 0.030 sn. `tests/test_packset.py` 41 test. Yapilmadi: dil dosyalari, `sounds.json`, font ve tag'ler birlesir (raporlanmaz), oyun/Java/loader surumu kontrol edilmez, `mcmod.info` ve Bukkit/Paper okunmaz, Maven siralamasi yaklasik.

### 62. Coverage import (D89, 2026-09-30)
- **Ne ise yarar:** Projenin kendi test calistiricisinin yazdigi kapsama raporunu (lcov, Cobertura, JaCoCo, coverage.py JSON) okuyup degisen satirlardan hangilerini hicbir testin calistirmadigini gosterir; `review --coverage` ve `verinoda coverage` komutlari.
- **Neden eklendi:** `review` testlerin degisikligi hangi yoldan etkiledigini grafikle soyluyordu ama "hangi degisen satirlar calistirilmiyor" olculmuyordu. Codecov ve SonarQube bunu gosteriyor; burada yerel ve aga cikmadan, kanitli claim olarak veriliyor.
- **Durum:** `tests/test_coverage_import.py` 26 test. Gercek bir projenin raporu uzerinde olculmedi, sure olculmedi. Rapor satir numarasiyla eslesir; dosya raporun ardindan degistiyse `weak_inference`. Yapilmadi: dal (branch) kapsami okunmaz, Cobertura/JaCoCo hangi testin calistirdigini soylemez, tek kok raporda iki alt projenin ayni goreli yolu ayrilamaz.

### 63. Ownership and knowledge map (D90, 2026-09-30)
- **Ne ise yarar:** `verinoda owners` ile bir dosya, klasor veya sembolun ilan edilmis sahiplerini (CODEOWNERS) ve satirlari fiilen kimin yazdigini (git blame) gosterir; ana yazar, bus factor ve artik aktif olmayan yazarlarin satirlari (bilgi kaybi) hesaplanir.
- **Neden eklendi:** "Bu kodu kim biliyor?" sorusunun cevabi yoktu; `map --view history` ve `history commits --author` yalnizca commit listeliyordu. CodeScene ve Sourcegraph Own bunu blame ve CODEOWNERS'tan cikariyor.
- **Durum:** Olculdu: `owners verinoda/cli.py` 2.4 sn, tum `verinoda` klasoru (372 dosya) 28 sn, varsayilan proje (ilk 200 dosya) 17.7 sn. Bu depoda tek yazar var, yani cevaplar anlamsiz; cok yazarli yollar testlerle (20 test) kapsaniyor. MCP araci yok. Yapilmadi: tasima/kopya tespiti yok, GitLab bolumleri birlestirilmiyor.

### 64. Hotspots (D91, 2026-09-30)
- **Ne ise yarar:** `verinoda map --view hotspots` ile sik degisen ve karmasik dosya/fonksiyonlari (degisim sayisi x karmasiklik) siralar; `review` da `read_first` aralik sirasini bu puanla belirler.
- **Neden eklendi:** Hata ve yanlis anlamalar sik degisen ve karmasik kodda toplaniyor (CodeScene, CodeCharta). Verinoda iki yariyi ayri ayri biliyordu (son 30 commit sayimi ve cyclomatic) ama birlestiren yoktu.
- **Durum:** Olculdu: bu depoda gorunum 121 sn'den (sinir oncesi) 28-31 sn'ye indi, ayni ilk 13 dosya ve fonksiyonlarla. En yukari: `verinoda/cli.py` (94 degisiklik x 1.324). `hotspots.rank` tek kucuk dosya icin 0.9 sn, `cli.py`+`review.py` icin 5.5 sn. Yapilmadi: rename takip edilmez, pencere son 1.000 commit, fonksiyon bazli sayim yalnizca ilk 20 dosya icin.

### 65. Temporal coupling (D92, 2026-09-30)
- **Ne ise yarar:** Etki analizine (`map --view impact`) git gecmisinden "birlikte degisen" dosyalar ekler: grafikte hic bagi olmayan ama hedefle surekli beraber degisen dosyalari (ayar, dokuman, kardes modul) `strong_inference` claim olarak listeler.
- **Neden eklendi:** Etki analizi yalnizca graf kenarlarini izliyordu; ayar dosyasi, dokuman veya kayit/isimle baglanan kardes modul gibi dosyalar gorunmuyordu. Git hangi dosyalarin birlikte degistigini zaten biliyor.
- **Durum:** Olculdu: son 1.000 commit okuma 0.5-0.8 sn; bu depoda ornegin `review.py` icin `tests/test_review.py` (17 commitin 8'i). Esik: en az 3 ortak commit ve en az %30. Precision icin etiketli set yok, uzun gecmisli depoda sure olculmedi. Yapilmadi: dosya seviyesi (fonksiyon degil), rename takibi yok, `review` ve `analyze` henuz gostermiyor.

### 66. Installed-version library docs (D93, 2026-10-01)
- **Ne ise yarar:** `verinoda api NAME --docs` ile bir kutuphane uyesinin kurulu surumdeki docstring'ini ve paketlenmis README bolumunu, satir numaralariyla ve cevrimdisi olarak alintilar.
- **Neden eklendi:** `api` uyelerin var olup olmadigini soyluyordu ama nasil kullanilacagini degil; ajan dokumani hafizadan veya webden okuyor, bu da baska surumu anlatabiliyor. Kurulu dosyalar tam surumun dokumanini zaten tasiyor.
- **Durum:** Olculdu: `api` cevabina yaklasik 0.02-0.25 sn ekler. Elle kontrol: `jedi.Script`, `click.option`, `pytest.fixture`, `json.loads` dogru alintilandi. Inceleme turunda 45 hedefin 10'unda alinti satir araligi gercekte gosterilenden fazlaydi, duzeltildi. `tests/test_libdocs.py`. Yapilmadi: docstring yalnizca Python, Java class dosyalarinda Javadoc yok, `node_modules` README'leri okunmaz.

### 67. Commit rationale per symbol (D94, 2026-10-01)
- **Ne ise yarar:** `verinoda history symbol NAME` ile bir sembolun satirlarini degistiren commit'leri, mesajin govdesiyle birlikte (git `log -L`) alintilar: "bu fonksiyon neden boyle?" sorusunun yazarin kendi aciklamasiyla cevabi.
- **Neden eklendi:** Gerekce genelde commit govdesinde yazilir; `analyze` sadece konuyu (subject) ve en fazla uc commit'i alintiliyordu, yalnizca `analyze` icinden, ve calisma agacindaki satir numaralari HEAD'e eslenmeden `git log -L`'ye verildigi icin kaydirilmis duzenlemelerde yanlis satirlar izleniyordu.
- **Durum:** Olculdu: bu depoda duzenlenmis dosyada fonksiyon 5 satir kaymisken HEAD satirlarina dogru eslendi, yaklasik 0.3 sn. `tests/test_history.py`; iki inceleme turu duzeltmeleri var. Yapilmadi: `git log -L` dosyalar arasi tasimayi izlemez, sadece bicim degistiren commit de sayilir, cok degisen sembolun eslenmesi daha kucuk bir aralik verebilir.

### 68. Architecture rules as code (D95, 2026-10-01)
- **Ne ise yarar:** Karar kayitlarinin korumalarina uc yeni tur ekler: `layers` (katmanlar yalnizca asagiya bagimli olabilir), `allow_edges` (bir grup yalnizca adli baskalarini kullanabilir), `public` (modul disindan sadece belirli acik dosyalardan erisilir) ve `tag:` ile adlandirilmis dosya gruplari. `decide check` ihlalde CI'yi cagri yeriyle birlikte basarisiz yapar.
- **Neden eklendi:** `decide check` yalniz bir yasak cifti (`no_edge`) kontrol ediyordu; ArchUnit, import-linter, Tach, dependency-cruiser ve Nx katman, bilesen ve genel arayuz kurallarini CI'da denetliyor. Kural bir insan karari oldugu icin yeni komut degil mevcut karar kaydina eklendi.
- **Durum:** Olculdu: bu depoda bes kural 3.1-4.4 sn; 10 VIOLATED yerin 10'u elle okundu ve dogruydu. `tests/test_arch_rules.py` 12 test. Etiketli bir set olmadigi icin recall ve Python disi diller olculmedi. Yapilmadi: yansima/DI gorulmez, ucuncu taraf paketler yargilanmaz, katman atlama serbest, baseline/ratchet yok.

### 69. Differential findings (D96, 2026-10-01)
- **Ne ise yarar:** `review` artik varsayilan olarak yalnizca degisikligin getirdigi bulgulari listeler; onceden var olanlar (`preexisting`) gizlenir ve sayilir, degisikligin giderdikleri (`fixed`) ayrica gosterilir. `--findings all` ile hepsi gorulur.
- **Neden eklendi:** Review kural bulgularini yalnizca head'in degisen satirlarinda calistiriyordu; zaten sink, riskli islem veya IO-in-loop iceren duzenlenmis bir satir degisiklik getirmis gibi raporlaniyordu ve hicbir sey degisikligin neyi kaldirdigini soylemiyordu. Infer, SonarQube ve CodeScene "bu degisiklik ne getirdi" sorusunu bu sekilde cevapliyor.
- **Durum:** `tests/test_review_delta.py` 18 test; `test_review.py` (109) ve `test_cli.py` (94) gecti. Taban tarafi calismasi test fixture'inda 0.69 sn'lik reviewin 0.10 sn'si. Etiketli review fixture'lari yeni varsayilanla yeniden puanlanmadi; varsayilanla recall dusebilir. Yapilmadi: taban tarafi head grafigini kullanir, gecis kurallarinin "fixed" karsiligi yok.

### 70. What a merged change made stale (D97, 2026-10-01)
- **Ne ise yarar:** `review` icinde "Made stale by the change" bolumu: degisikligin govdesini yeniden yazdigi bir fonksiyon hakkindaki analiz claim'lerini, kaldirdigi sembole sabitlenmis notlari ve yonettigi kod degisen karar kayitlarini, degisen satirla birlikte listeler.
- **Neden eklendi:** `verinoda update` bu tur claim'leri sonradan stale isaretliyor ve `notes` tek tek gosteriyor, ama degisikligi inceleyen kisiye "bu degisiklik hangi bilgiyi eskitti" sorusunun cevabi, degisiklige baglanmis olarak verilmiyordu. Mintlify ve Dosu bunu model ile yapiyor; burada Verinoda'nin zaten tuttugu parmak izleri kullaniliyor.
- **Durum:** Olculdu (sentetik): 20 dosya x 25 fonksiyon, 500 claim; 5 dosyada birer fonksiyon duzenlemesi 5 claim ve 3 notu buldu, 140-195 ms. Gercek PR'larla precision ve on binlerce claim'li depoda maliyet olculmedi. `tests/test_stale_reach.py`. Salt okunur; claim durumlari `update`'e kadar degismez. Yapilmadi: alinti satirlar yeniden okunmaz, PR yorumu atilmaz.

### 71. Decision record lifecycle (D98, 2026-10-01)
- **Ne ise yarar:** Karar kayitlari icin `decide supersede` (eski kaydi yenisi ile degistir), `decide link` (amends, clarifies, depends-on, relates-to baglari) ve `decide toc` (icindekiler, iliski grafigi ve zaman cizelgesi) ekler; `verinoda ui`'da `#/d` sayfasi da var.
- **Neden eklendi:** Kayit sadece yeni kayit yapilirken supersede edilebiliyordu; var olan iki kayit baglanamiyordu, baska iliski turu yoktu ve ne icindekiler ne grafik ne zaman cizelgesi vardi. Elle yapilan tek tarafli `supersedes` fark edilmiyordu.
- **Durum:** `tests/test_decide_lifecycle.py` 15 test (bu makinede yaklasik 1 dakika). Performans olculmedi. Yapilmadi: `decisions` log tablosunda `links` sutunu yok (sema degismedi), unlink komutu yok, UI sayfasi cizilmis grafik degil Mermaid metni gosterir, bir kayit en fazla bir kaydi supersede eder.

### 72. Butterfly view (D99, 2026-10-01)
- **Ne ise yarar:** Bir sembolu ortaya koyup solunda cagiranlari, saginda cagirdiklarini (veya ust/alt siniflari) birkac seviye derinlige kadar gosterir; `verinoda butterfly` komutu ve `ui`'da *Butterfly* paneli. Her baglanti kendi satirina isaret eder.
- **Neden eklendi:** UI'daki not, cagiran/cagrilanlari duz bolumler halinde, kalitimi ise yalniz bir adim gosteriyordu. Understand ve Sourcetrail bir sembolu ortaya alip "kim buna ulasiyor, bu neye ulasiyor" sorusunu tek resimde cevapliyor.
- **Durum:** Olculdu: `examples/glow_mod` (198 dugum, 352 kenar) icin `Wisp.spawn` iki seviyede 8 cagiran listeledi; komut 3.2 sn (neredeyse tamami index yuklemesi), yuruyus 0.02 sn. `tests/test_butterfly.py`. Tarayicida panel testi yok. Yapilmadi: sadece `calls` kenarlari (callback/import yok), asiri yuklemeler birlestirilmez, export edilen sayfada buton yok.

### 73. Access Widener and Access Transformer (D100, 2026-10-01)
- **Ne ise yarar:** `verinoda access-check` ile Fabric/Quilt access widener ve Forge/NeoForge access transformer dosyalarindaki her girdiyi, derlenen jar'larin class dosyalariyla karsilastirip sinif/uye/descriptor gercekten var mi diye kontrol eder.
- **Neden eklendi:** Kaynak kodda ad yanlissa (yazim hatasi, baska mapping, baska oyun surumu descriptor'u) bunu soyleyen bir sey yok. Loom ilk hatali girdide durur (tek tek); transformer'da eslesmeyen girdi loader tarafindan atlanir ve mod sonradan `IllegalAccessError` ile cokebilir.
- **Durum:** Fixture ile test edildi (`tests/test_accesscheck.py`, 15 test): 15 kurallik widener ve 7 kurallik transformer, 4 siniflik jar. Gercek bir Loom classpath'li mod projesi olmadigi icin tam Minecraft classpath'inde sure olculmedi. Yapilmadi: yalniz varlik kontrolu (erisim degisikligi gerekli mi bakilmaz), kalitilan uyeler absent sayilir, `named` disi namespace ve SRG adlar `unknown`, MCP araci yok.

### 74. Undeclared symbols and naming rules (D101, 2026-10-01)
- **Ne ise yarar:** `verinoda datapack` ozetine, hic tanimlanmadan (`scoreboard objectives add`, `team add`, `bossbar add`, Java `addObjective` vb.) kullanilan scoreboard objective, team ve boss bar adlarini ekler; ayrica istege bagli `datapack.naming` regex adlandirma kurallari.
- **Neden eklendi:** Tanimsiz bir objective/team/boss bar'a yazan komut oyunda oyuncunun nadiren gordugu bir hatayla basarisiz olur (`execute if score` ise sessizce false). `datapack` objective'leri okuyordu ama `add` ile `remove`'u ayirmiyor, kullanilan adin tanimli olup olmadigini sormuyor, team ve boss bar'i hic okumuyordu. Spyglass bunlari tanimsiz sembol olarak raporluyor.
- **Durum:** Olculdu: `examples/glow_mod` icin tanimsiz yok, `datapack` 1.9 sn (eskisi gibi). Inceleme: tehlikeli bir adlandirma regex'i (`(a|aa)*`) once 60 sn'de bitmiyordu, simdi reddedilip 1.5 sn'de bitiyor. Ozel mod bu worktree'de yoktu, sayilari olculmedi. `strong_inference`. Yapilmadi: depo disinda tanimlananlar da raporlanir (ipucu), Java'da olusan boss bar id'leri cozulmez.

### 75. Client and server separation (D102, 2026-10-01)
- **Ne ise yarar:** `verinoda map --view sides` ile sunucu giris noktalarindan yalnizca istemcide bulunan koda (src/client, `@Environment(CLIENT)`, `@OnlyIn(Dist.CLIENT)`, `net.minecraft.client` siniflari) giden yollari bulur ve her hop'u satirlariyla gosterir.
- **Neden eklendi:** Yalnizca istemcide var olan kod, onu yukleyen dedicated sunucuyu coker; genelde bir oyuncu yolu ilk tetikledigi anda. Loom'un bolunmus kaynak setleri `src/main`'den dogrudan referansi derleme aninda yakalar ama isaretli sinif uzerinden yolu, Forge tek kaynak setini veya ortak kodda adi gecen vanilla istemci sinifini yakalayamaz.
- **Durum:** Olculdu (fixture, 8 Java dosyasi): 5 yol bulundu, yorum satirindaki kullanim ve `DEDICATED_SERVER` yontemi raporlanmadi, 3 istemci giris noktasi disarida, 2 ulasilmayan gecis. Gercek mod korpusunda olculmedi. `tests/test_sides.py`. Yapilmadi: korumali kullanim (`isClient`, `DistExecutor`) okunmaz (claim ulasilabilirlik, cokme degil), cozulmeyen dinamik cagrilar izlenmez, `client` mixin dizileri ve `@Environment` alanlari okunmaz.

### 76. Violation baseline ve ratchet (D103, 2026-10-01)
- **Ne işe yarar:** Eski kodda zaten var olan mimari kural ihlallerini bir "baseline" dosyasına kaydeder; CI sadece yeni ihlallerde fail eder, eskiler düzeldikçe liste küçülür.
- **Neden eklendi:** Eski koda sonradan konan kuralın düzeltilmesi imkansız ihlalleri yüzünden `decide check` sürekli fail ediyordu, kural fiilen uygulanamıyordu (ArchUnit ve dependency-cruiser'daki "known violations" yaklaşımı).
- **Durum:** Yalnızca testlerle doğrulandı (iki katmanlı projede iki bilinen yukarı bağımlılık); gerçek proje ölçümü yok. Yapılmayan: aynı satırlar yer değil sayıyla ayrılıyor, baseline süresi dolmuyor, mağaza karar günlüğüne yazılmıyor.

### 77. Önerilen reviewer'lar ve ilişkili değişiklikler (D104, 2026-10-01)
- **Ne işe yarar:** `review` çıktısına, değişen satırları en son kimin yazdığını (git blame), CODEOWNERS sahiplerini ve aynı tanımları daha önce değiştiren commit'leri ekler.
- **Neden eklendi:** Değişikliği bulan bir araç, kodu kimin bildiğini ve geçmişini de söylemeli; CodeRabbit gibi araçlar bunu PR'da gösteriyor, burada yerel git okumalarından kanıtlı iddia olarak üretiliyor.
- **Durum:** Yalnızca testlerle ölçüldü (üç yazarlı fixture); iki reviewer turunda birçok hata düzeltildi. Yapılmayan: blame bir sezgisel (reformat/taşıma yazarı yanlış gösterir), en fazla 20 aralık ve 8 tanım okunur, `git log -L` tek dosya içinde kalır.

### 78. Bağımlılığı yazmadan önce sor (D105, 2026-10-01)
- **Ne işe yarar:** Ajan henüz yazmadığı bir bağımlılığı (kaynak dosya -> hedef) kabul edilmiş karar kurallarına sorar; yanıt forbidden, restricted, unknown veya allowed olur.
- **Neden eklendi:** `decide check` yasak bağımlılığı kod yazıldıktan sonra buluyordu; Sonargraph'ın `check_proposed_dependency` aracı önceden cevap veriyor.
- **Durum:** Çekirdek MCP menüsü karar kaydı olmayan projede 4,499 karakter (değişmedi), kayıtlı projede 4,594; 17 test, yaklaşık 10 sn. Yapılmayan: yalnızca yol ve isimlere bakar, kod/graf okumaz; proje dışı paketi sadece `dependency absent` ve `only_in` yargılar.

### 79. What-if refactoring (D106, 2026-10-01)
- **Ne işe yarar:** Bir modülü veya klasörü taşımadan önce, taşımanın mimari kuralları ve bağımlılık döngülerini nasıl etkileyeceğini bellekte simüle ederek gösterir; diske hiçbir şey yazmaz.
- **Neden eklendi:** Sonargraph ve Lattix mimarın sanal modelde eleman taşıyıp kuralları yeniden kontrol etmesine izin veriyor; Verinoda'da kurallar ve döngü görünümü zaten graf kenarlarından hesaplanıyordu.
- **Durum:** Yalnızca testlerle ölçüldü (üç katmanlı proje, birleşince döngü oluşan klasör); review turunda birkaç hata düzeltildi. Yapılmayan: taşımanın importları güncellediği varsayılır, yalnız edge kuralları ve dosya düzeyi döngüler kontrol edilir, sembol taşıma simüle edilmez.

### 80. Project brief (D107, 2026-10-01)
- **Ne işe yarar:** Manifest, script, CI ve talimat dosyalarından projenin kısa özetini (ne olduğu, nasıl build/test edilir, klasör düzeni, kurallar) her çağrıda yeniden üretir; her satır `dosya:satır` kanıtı taşır ve karakter bütçesine sığar.
- **Neden eklendi:** Letta, Cline ve Zencoder ajana küçük, hep yüklü bir proje özeti veriyor; elle yazılan özet eskiyor, bu özet dosyalardan türetildiği için güncel kalıyor.
- **Durum:** Verinoda'nın kendi reposunda 25 satır, 2,122 karakter, 0,7 sn; 2,000 karakter bütçede 22 satır. Review sonrası: 52 satır, bütçede 23 satır, 1,6 sn. Ajanın özetle daha iyi çalışıp çalışmadığı olculmedi. Yapılmayan: yalnız kök manifestler okunur (Gradle, Maven, Cargo, iç içe package.json yok).

### 81. Koda bağlı dokümanlar, drift kontrolü ve basit otomatik düzeltme (D108, 2026-10-01)
- **Ne işe yarar:** Dokümanlardaki dosya yolu ve satır referanslarının (`src/load.py:40-52` gibi) hâlâ doğru olup olmadığını kontrol eder; yeniden adlandırılan veya satırı kayan referansları `--fix` ile düzeltir.
- **Neden eklendi:** Kod değişir ama dokümandaki cümleler eski dosyaya veya satırlara işaret etmeye devam eder; Swimm benzeri bu kontrol burada model olmadan, yerelde ve git'ten çalışıyor.
- **Durum:** Bu repoda 355 doküman, 1,685 referans, 3,8 sn: 403 tamam, 696 ignore edilmiş çalışma zamanı yolu, 411 başka projenin yolu, 175 gerçekten kırık. Yapılmayan: yolsuz semboller (`Class.method`) kontrol edilmez, ilk klasörü yanlış yazılmış yol bildirilmez.

### 82. Yeniden indeksleme için git hook'ları (D109, 2026-10-01)
- **Ne işe yarar:** commit, branch değişimi, merge ve rebase sonrası Verinoda'nın artımlı `update` komutunu arka planda otomatik çalıştıran git hook'larını kurar/kaldırır.
- **Neden eklendi:** Bu olaylardan sonra indeks bayatlıyor ve sonraki cevap eski graftan başlıyordu; Graphify ve code-review-graph de benzer hook kuruyor.
- **Durum:** Yalnızca testlerle ölçüldü (Windows'ta Git for Windows `sh` ile commit, checkout -b, amend sonrası çalışır; dosya checkout'unda ve `VERINODA_NO_HOOK=1` ile çalışmaz). Yapılmayan: yorumlayıcı yolu hook'a yazılır, ortam değişirse yeniden kurmak gerekir; hook yöneticilerinin dosyalarına dokunulmaz.

### 83. Token bütçeli sıralı repo haritası (D110, 2026-10-01)
- **Ne işe yarar:** Üzerinde çalışılan dosyalara göre ağırlıklandırılmış PageRank ile hangi dosya ve imzaların önce okunması gerektiğini, token bütçesine sığacak şekilde listeler.
- **Neden eklendi:** Ajanın bir projenin tüm imzalarını okuması bağlama sığmıyor; Aider'in repo haritası aynı soruyu PageRank ile yanıtlıyor, burada model ve ağ olmadan Verinoda grafından veriliyor.
- **Durum:** `examples/orders_app`'te 17/17 imza, yaklaşık 239/1024 token; `--max-tokens 40` ile 2 imza. Ana repoda (879 sıralı dosya) odak etkisi doğrulandı. Büyük repoda süre olculmedi. Yapılmayan: sıralama strong_inference (sezgisel), token sayısı tahmini (4 karakter = 1 token).

### 84. Belgelenmemiş kararlar (D111, 2026-10-01)
- **Ne işe yarar:** Koddan, kayıt edilmemiş mimari tercihleri (tek depolama yolu, tek dosyada tutulan kütüphane, tek yerden okunan konfigürasyon) aday olarak listeler; kullanıcı kayda geçirir veya reddeder.
- **Neden eklendi:** Kodu şekillendiren birçok seçim hiç yazılmıyor; kod bunu gösteriyor ama kimse "bu bir karardı" demiyor, ADR akışı koddan başlıyor.
- **Durum:** Verinoda'nın kendisinde (419 ürün dosyası) 11 aday (numpy, graspologic-native, 9 grammar), yaklaşık 32 sn; ilk sürümde 16 aday çıkmış, 4'ü yanlıştı ve kural eklenerek düzeltildi. Aday kendisi weak_inference. Yapılmayan: yalnız Python import'ları, bir çalıştırma tüm dosyaları okur, sadece CLI.

### 85. Değişiklik risk skoru (D112, 2026-10-01)
- **Ne işe yarar:** `review` sonuçlarını (bulgular, kırılan API'ler, bağımlılar, testsiz kalanlar, kapsanmayan satırlar) tek bir 0-100 skorda toplar ve her parçayı gösterir.
- **Neden eklendi:** Review birçok yönden ölçüyordu ama iki review'u bir bakışta karşılaştırmak mümkün değildi; Greptile ve GitNexus tek risk rakamı gösteriyor, burada nasıl oluştuğu da gösterilir ve düşük skor asla "güvenli" denmez.
- **Durum:** Kompakt MCP bloğu 354 karakter, tam blok yaklaşık 2,300; testlerde skor parçaların toplamına eşit. Yapılmayan: ağırlıklar seçilmiş, etiketli veriyle fit edilmemiş; skor kırılma olasılığını değil bulgu miktarını sıralar; `--run-tests` sonucu skorlanmaz.

### 86. Oturum içi tekrar engelleme (D113, 2026-10-01)
- **Ne işe yarar:** MCP sunucusu aynı oturumda daha önce döndürdüğü kod parçalarını `project_query` cevaplarında tekrar basmaz, yalnız yerlerini listeler.
- **Neden eklendi:** Ajan ilişkili sorular sorunca aynı üst pasajlar tekrar geliyor ve karakter bütçesini ve bağlamı boşa harcıyordu (Probe ve Ref de tekrarı atlıyor).
- **Durum:** Yalnızca testlerle doğrulandı (aynı soru ikinci kez bölümleri basmaz ve listeler; yeni sunucu hepsini basar; değişen dosya yeniden basılır). `VERINODA_QUERY_DEDUP=0` ile kapatılır. Yapılmayan: bağlamı sıkıştırılan ajan pasajları kaybeder, uzun oturumda liste 700 karakterde kırpılır.

### 87. Artımlı yeniden review (D114, 2026-10-01)
- **Ne işe yarar:** `review --since-last` ile bir önceki review'dan beri yalnızca yeni çıkan bulguları gösterir; tekrar edenler ve düzelenler ayrı sayılır.
- **Neden eklendi:** Ajan düzeltip tekrar review yapınca ilk review'un tüm bulguları tekrar geliyor, yeni olanı görmek zorlaşıyordu (Bugbot ve Ellipsis son review'dan beri olanı inceliyor).
- **Durum:** Orders örneğinde testle: imza değişikliği 5 bulgu; yukarıya edit sonrası 0 yeni 5 tekrar; yeni `os.system` tek yeni bulgu; geri alınca 5 "gone". Çıkış kodu bütün review'unki kalır. Yapılmayan: eski kayıtlar karşılaştırılmaz, metni değişen bulgu yeni sayılır.

### 88. Glob kapsamlı bağlam (D115, 2026-10-01)
- **Ne işe yarar:** Ajan bir dosyayı okuyup düzenlerken, o dosyayı ilgilendiren karar kayıtlarını, notları ve Cursor/Kiro kurallarını Claude Code hook'u üzerinden önüne koyar.
- **Neden eklendi:** Bir kod bölgesine ait kurallar bir kez yazılıyor ama gerektiği anda kimse görmüyor; Kiro steering ve Cursor rules metni glob'a bağlıyor, Verinoda'nın karar kayıtları da dosya adlandırıyor.
- **Durum:** Yalnızca testlerle; sunucudaki araç sayısı 40 oldu. Hook bloğu 700 karakterde kesilir. Yapılmayan: not veya kuralın sadece ilk satırı alıntılanır, iddialar (claims) listelenmez, glob eşlemesi editörlerden köşe durumlarda farklı olabilir (bilinçli).

### 89. SARIF giriş/çıkış ve CI durumu (D116, 2026-10-01)
- **Ne işe yarar:** `review`, `check` ve `decide check` bulgularını GitHub code scanning'in okuduğu SARIF 2.1.0 olarak verir; ayrıca başka araçların (linter, CodeQL) SARIF'ini kanıt olarak içeri alır.
- **Neden eklendi:** Verinoda bulguları yalnız metin veya kendi JSON'u idi, PR'da CodeQL ile yan yana görünemiyordu; Copilot review ve Code Pathfinder de SARIF konuşuyor.
- **Durum:** Testte yazılan SARIF şema alt kümesinden geçiyor; gerçek `check --sarif` doğru seviyeyi veriyor. GitHub'a yüklenmedi ve GitHub doğrulayıcısıyla denenmedi (ağ yok). Yapılmayan: `partialFingerprints` yok, `review` henüz içe alınan SARIF'i okumuyor, seviyeler ciddiyeti değil kesinliği yansıtır.

### 90. Flaky test geçmişi (D117, 2026-10-01)
- **Ne işe yarar:** Debug ledger'daki koşulardan test bazlı geçmiş tutar; aynı ağaçta hem geçip hem kalan testleri flaky olarak, N tekrar geçenleri "held" olarak listeler ve kullanıcıya karantina listesi sunar.
- **Neden eklendi:** `debug rerun` bir seri için geçme oranı ölçüyor ama sonra test bazlı resim kayboluyordu; Datadog Test Optimization gibi araçlar geçmiş ve karantina tutuyor.
- **Durum:** Sentetik mağazada 2,000 test x 10 koşu: 2,000 satır kaydı 22 ms, 20,000 satırlık rapor 201 ms; uçtan uca testte flaky test 0,67 oranla bulundu. Gerçek flaky paket haftalarca olculmedi. Yapılmayan: yalnız debug ledger koşuları kaydedilir, "fixed" asla denmez ("held").

### 91. Adlandırılmış akış haritaları (D118, 2026-10-01)
- **Ne işe yarar:** Bir trace veya harita görünümünü isimle kaydeder; sonra okununca atıf yaptığı dosyaların hash'lerini karşılaştırıp haritanın hâlâ güncel (current) mı bayat (stale) mı olduğunu söyler.
- **Neden eklendi:** Oturum bitince ajanın çıkardığı akış kayboluyor veya denetlenmeyen bir kopya olarak not'a yapıştırılıyor; Windsurf/Devin Codemaps haritaları isimle saklıyor.
- **Durum:** `orders_app`'te trace 1,821 bayt/3 dosya, tüm varsayılan görünümler 21,953 bayt/11 dosya, okuma yaklaşık 0,04 sn; MCP cevabı 1,828 ve 11,678 karakter. Yapılmayan: "current" yalnız atıf yapılan dosyaların değişmediğini gösterir, kaydetme sadece CLI'dan.

### 92. Çökme teşhis kuralları ve şüpheli puanlama (D119, 2026-10-01)
- **Ne işe yarar:** Minecraft çökme günlüklerinde bilinen kalıbı (bellek yetmedi, watchdog, eksik bağımlılık, Mixin hatası, yanlış Java sürümü vb. altı kural) adlandırır ve stack frame'lerine göre suçlu modu puanlar.
- **Neden eklendi:** Oyuncu çökme raporlarının çoğu başka modlar hakkında; mclo.gs Codex, mc-crash-doctor ve NotEnoughCrashes bilinen kalıbı ve suçlu modu söylüyor, `trace-log` söylemiyordu.
- **Durum:** Elle yazılmış beş fixture ile ölçüldü (örn. OOM raporunda bigstorage 0,78); gerçek oyuncu günlüklerinde olculmedi. Yapılmayan: kurallar tek satır eşler, paket atfı ilk üç parçaya dayanır, obfuscate edilmiş frame'ler moda bağlanmaz.

### 93. Rationale (gerekçe) düğümleri (D120, 2026-10-01)
- **Ne işe yarar:** Kod yorumlarındaki `WHY:`, `NOTE:`, `HACK:` gibi işaretleri ve ADR atıflarını bulup ilgili sembole bağlar; `node_inspect` ve `verinoda rationale` ile gösterir.
- **Neden eklendi:** Yazarın "neden böyle" dediği yorumlar gerekçeye giden en kısa yol ama ajan alıntıda tanımın üstündeki veya uzun gövdedeki yorumu kaçırabiliyor; Graphify ve codebase-memory-mcp bunları düğüm yapıyor, burada graf değişmeden yapılıyor.
- **Durum:** Bu repoda 2,666 dosyada 74 yorum (ADR atfı 66, NOTE 7, IMPORTANT 1), yaklaşık 13 sn; review sonrası 2,669 dosya, 72 yorum, yaklaşık 21 sn. Yapılmayan: Python dışında satır sonu yorumları okunmaz, docstring'ler yorum sayılmaz, önbellek yok.

### 94. Yapısal (AST kalıbı) arama (D121, 2026-10-01)
- **Ne işe yarar:** Metavariable'lı kod kalıplarıyla ("argümanlı her `foo` çağrısı", "her Java `return`") tree-sitter grammar'ları üzerinden yapısal arama yapar; `--rule` ile YAML kural dosyası okur.
- **Neden eklendi:** Metin araması satır sonu, yorum ve iç içe yapılarda kırılıyor; ast-grep, Semgrep, Comby ve Sourcegraph yapısal aramayla yanıtlıyor, Verinoda'da grammar'lar zaten vardı (yeni bağımlılık yok).
- **Durum:** `_ts_parser($A)` kalıbı tüm repoda 915 dosya, 15 dil, 6 eşleşme, ön filtreden önce 23 sn; `verinoda/` klasöründe (239 dosya) 1,7 sn, 4 eşleşme. Yapılmayan: yalnız şekil (isim çözümlemesi/tip yok), `inside`/`has`/`not` kuralları ve düzeltme yok, `$NAME` için kaçış yok.

### 95. Kanıta dayalı kod turları (D122, 2026-10-01)
- **Ne işe yarar:** `trace`'in bulduğu ilk yolu, VS Code CodeTour eklentisinin adım adım gezdirdiği `.tour` dosyasına çevirir; her adım ilgili satırı alıntılar ve kod kayarsa `--check/--fix` ile yeniden bağlanır.
- **Neden eklendi:** Yeni katılan biri veya devir alan ajan bir akışı (istek nereden girer, nerede saklanır) adım adım gezmeli; CodeTour bunu oynatıyor, trace'i tura çevirmek kodu atıf yaparak ve kod kayınca dürüst kalarak gösterir.
- **Durum:** Orders örneğinde testle: her adımın satırı alıntı metni taşır; her dosyanın üstüne iki satır eklenince tüm adımlar 2 kaymış bulunur ve `--fix` onarır; yeniden yazılan satır "gone" olur. Yapılmayan: yalnız ilk yol, dataflow görünümü tura çevrilmiyor, açıklamalar kısa.

### 96. Kalıp trendleri ve kod monitörleri (D123, 2026-10-01)
- **Ne işe yarar:** Kayıtlı regex veya AST aramalarını ve bilinen eşleşmelerini repoda tutar; CI yeni eşleşmede fail eder, regex monitörleri için git geçmişinde eşleşme sayısı trendi gösterir.
- **Neden eklendi:** Bir geçiş ("eski API'den çık", "yeni `print(` yok") sayısı sadece azalması gereken bir kalıp; Sourcegraph Code Insights sayıyı zamanda çiziyor, Code Monitors yeni eşleşmede uyarıyor.
- **Durum:** Yalnızca testlerle ölçüldü (yeni eşleşme fail, kayan satır değil, trend dört commit üzerinde). Yapılmayan: aynı iki satır yer değil sayıyla ayrılır, regex yorum ve string içindeki eşleşmeyi de sayar, trend commit örnekler (aradaki sıçrama görülmez), baseline'lar repoda olduğu için commit ile genişletilebilir.

### 97. Bellek olay geçmişi ve süre sonu (D124, 2026-10-01)
- **Ne işe yarar:** `memory forget` ile bir öğrenimi emekli eder, `memory learn --ttl 30d` ile yaşam süresi verir ve `memory history` ile anahtarın ADD/UPDATE/DELETE/EXPIRE/INVALIDATE olaylarını gösterir.
- **Neden eklendi:** mem0 her belleğin olaylarını kaydediyor ve belleğin süresinin dolmasına izin veriyor; Verinoda'da öğrenimler sürümlü idi ama kullanıcı emekli edemiyor, süre veremiyordu.
- **Durum:** Yalnızca testlerle ölçüldü; olaylar saklanmaz, satırlardan türetilir; şema v8 (tek yeni sütun `expires_at`, eski Verinoda veritabanını reddeder). Yapılmayan: süre sonu zamanlayıcıyla değil okuma sırasında fark edilir, süre duvar saati zamanıdır.

### 98. Yol kapsamlı review kuralları (D125, 2026-10-01)
- **Ne işe yarar:** AGENTS.md, CLAUDE.md, BUGBOT.md, REVIEW.md gibi dosyalardaki ```verinoda-rules``` bloklarını klasör kapsamıyla uygular; değişikliğin eklediği satırlarda kural eşleşirse raporlar, en yakın klasör üsttekini sıkılaştırabilir veya kapatabilir.
- **Neden eklendi:** Cursor Bugbot, Greptile ve CodeRabbit klasör başına talimat okuyor; ekipler review kurallarını koda yakın tutuyor, `src/api/` altındaki kural yalnız oradaki değişikliklere uygulanmalı.
- **Durum:** Yalnızca testlerle ölçüldü. Kuralı zayıflatan değişiklik `weakened` olarak raporlanır ve çıkış 3 olur. Yapılmayan: düz yazı kuralları kontrol edilmez yalnız listelenir (dil modeli yok), regex tek satır okur.

### 99. Bi-temporal iddialar (D126, 2026-10-01)
- **Ne işe yarar:** `verinoda claim asof` ile "v0.2'de hangi iddialar geçerliydi" veya "1 Mayıs'ta neye inanıyorduk" sorularını kayıt zamanına veya commit'e göre yanıtlar.
- **Neden eklendi:** Zep Graphiti ve mem0 her olgu için iki zaman (geçerlilik ve kayıt) tutuyor; Verinoda'nın iddiaları silinmiyor ve geçmişi ekleme-yalnız olduğu için iki zaman zaten vardı ama soruya cevap veren komut yoktu.
- **Durum:** Yalnızca testlerle ölçüldü (c1, c2, c3 commit senaryosu: gözlemlenmiş, taşınmış strong_inference, bilinmeyen). Şema değişikliği yok. Yapılmayan: atadan taşınan kayıt sonradan geri alınan düzenlemeleri kontrol etmez, çalışma ağacındaki geçişler hiçbir commit'te değildir, commit'ler yalnız soyağacıyla karşılaştırılır.

### 100. Tipli notlar ve wikilink'ler (D127, 2026-10-01)
- **Ne işe yarar:** Kullanıcı notlarındaki `- [kategori] olgu #etiket` gözlem satırlarını listeler (`notes --facts`) ve `[[Ad]]` bağlantılarını koda veya başka nota çözüp kırık olanı CI için hata verir (`notes --links`).
- **Neden eklendi:** Basic Memory bilgiyi tipli gözlem ve `[[Ad]]` bağlantılarıyla Markdown olarak tutuyor; Verinoda notları zaten Markdown ve koda bağlıydı ama olguları listelenemiyor, bağlantılar yalnız görüntüleyicide tıklayarak izleniyordu.
- **Durum:** Yalnızca testlerle ölçüldü. Önbellek yok, her komut not dosyalarını yeniden okur. Yapılmayan: olgular kanıt durumu taşımaz ve doğrulanmaz, bağlantı ilk eşleşen isme çözülür, notlar üzerinde tam metin arama yok.

### 101. Shaderpack lint ve include grafiği (D128, 2026-10-01)
- **Ne işe yarar:** Iris/OptiFine shaderpack'lerindeki (ve mod core shader'larındaki) shader metnini komut satırında, çevrimdışı olarak kontrol eder: eksik `#include`, include döngüsü, kapanmamış parantez, yanlış yerdeki `#version`, bildirilmemiş uniform ve tanımsız makro gibi hataları dosya ve satırla gösterir.
- **Neden eklendi:** `shader --check` yalnızca uniform bloklarını Java yazıcılarıyla karşılaştırıyordu, shader metnine bakmıyordu; hatalar oyun stage'i derleyince "pembe dünya" olarak çıkıyordu. mcshader-lsp bunu editörde veriyordu, bu karar komut satırında veriyor.
- **Durum:** Test paketinde her tohumlanmış hata doğru satırda raporlandı; gerçek bir shaderpack (BSL, Complementary) ölçülmedi, uniform/makro kontrollerinin yanlış pozitif oranı sayılmadı. Yapılmadı: GLSL parser yok (eksik noktalı virgül vb. ancak opt-in `--glslang` ile bulunur), `#else` dalı bracket için okunmuyor, `shaders.properties` okunmuyor.

### 102. Debug ledger deneme adımları üzerinde bisect (D129, 2026-10-01)
- **Ne işe yarar:** Bir ajanın oturum sırasında çalışma ağacında yaptığı adım adım değişikliklerden hangisinin hatayı yeniden üreten testi bozduğunu, kullanıcının ağacına dokunmadan bulur.
- **Neden eklendi:** Mevcut `debug bisect` yalnızca commit'leri arıyordu; ajan commit'ler arasında da bir şeyleri bozuyor ve repro'nun başarısız olduğunu çok sonra fark ediyor. Ledger her adımın ağacını zaten tutuyordu.
- **Durum:** `examples/orders_app` üzerinde uçtan uca test: bozan adım (attempt 2) 2 çalıştırmayla bulundu, kullanıcının ağacı bayt bayt aynı kaldı, ikinci arama hiçbir şey çalıştırmadı; yaklaşık 11 sn. Yapılmadı: tek çalıştırma (flaky test sınırı kaydırabilir), tek sınır varsayımı, MCP'den erişim yok.

### 103. Kalıcı test-kod haritası ve etkilenen testler (D130, 2026-10-01)
- **Ne işe yarar:** Her testin hangi fonksiyonları çalıştırdığını çalıştırmalar arasında hatırlar; `review` bir değişiklikten etkilenen testleri seçip tek bir `pytest` komutu olarak yazdırır.
- **Neden eklendi:** Önceden ya tüm süit yeniden izlenmeli ya da statik erişim kullanılmalıydı ve statik erişim fazla test seçiyordu (orders_app'te `test_empty_order_rejected` aslında `apply_discount`'a hiç ulaşmıyor). pytest-testmon ve Datadog Test Impact Analysis benzeri bir harita tutar.
- **Durum:** orders_app'te 5 test haritalandı, `apply_discount` değişince tam 4 gold test listelendi (~13 sn); sentetik depoda 2.000 test x ~71 fonksiyon (142.858 satır) 1,4 sn'de kaydedildi, `affected` 0,14 sn. Gerçek proje geçmişinde seçim isabeti olculmedi. Yapılmadı: harita çalıştırma kapsamlı (testin atladığı yol sonradan çalışabilir), yalnızca Verinoda'nın izlediği pytest çalıştırmaları günceller.

### 104. Property test şablonları (D131, 2026-10-01)
- **Ne işe yarar:** Davranış probe'unun ürettiği girdilerle bir fonksiyon için roundtrip, idempotent veya equivalence özellik testi üretir ve kalıcı bir test dosyası olarak yazar.
- **Neden eklendi:** Probe'un öğrendikleri çalıştırmadan sonra kayboluyordu; `--emit-test` yalnızca birkaç girdiyle taban davranışı sabitliyordu. Hypothesis Ghostwriter'ın aynı üç şablonu var; burada dosya gözlemlenen sonuçlarla birlikte geliyor.
- **Durum:** Küçük bir codec modülünde (tests/test_probe_templates.py) üç şablon doğru çalıştı: tutan özellikler geçen dosya, tutmayanlar karşı örnekli ve başarısız dosya üretti; her probe çalıştırması birkaç saniye (40 girdi). Yapılmadı: kanıt değil gözlem ("verified" kelimesi hiç kullanılmaz), çok argümanlı ters fonksiyonlar, örnek metotlar, MCP erişimi yok.

### 105. Infrastructure-as-code düğümleri (D132, 2026-10-01)
- **Ne işe yarar:** `verinoda infra` komutu Dockerfile, compose, Kubernetes ve Terraform dosyalarını okuyup bir servisin hangi proje dosyasını nasıl çalıştırdığını (ör. `CMD` -> `app/main.py`) manifest satırı kanıtıyla gösterir.
- **Neden eklendi:** "`app/main.py`'yi ne çalıştırıyor?" sorusu manifestleri elle okumayı gerektiriyordu; codebase-memory-mcp ve Graphify bu tür kaynakları grafiklerine koyuyor.
- **Durum:** Yalnızca testlerle ölçüldü (Python API, Node, Java, compose, Kubernetes, Terraform fixture'ı); Verinoda'nın kendi deposunda hiç manifest yok (exit 1, 1,3 sn). 500 Terraform kaynağı x 100.000 dosya 14,1 sn'den 0,5 sn'ye indi. Yapılmadı: ARG/değişken çözümleme, compose `extends`, Helm/Kustomize, `npm start` takibi; düğümler graph.json'a eklenmedi, MCP aracı yok.

### 106. Monorepo'da etkilenen projeler (D133, 2026-10-01)
- **Ne işe yarar:** Bir diff'in hangi workspace paketlerine dokunduğunu ve bunlara bağımlı hangi paketlerin etkilendiğini bulur (`nx affected` benzeri); sonuç `review` içinde ve `verinoda affected` komutunda görünür.
- **Neden eklendi:** `review` kod grafiğindeki bağımlıları listeliyordu ama workspace paketlerini ve build hedeflerini değil; `packages/core` değişince `packages/ui` ve `apps/web`'in etkilendiğini elle bulmak gerekiyordu.
- **Durum:** Bu depoda (2.691 dosya) `affected()` 0,054 sn, `--file` ile uçtan uca 1,8 sn; depo monorepo değil. Yalnızca ekosistem başına tek fixture ile test edildi; gerçek monorepo ve `nx affected` ile karşılaştırma yapılmadı. Yapılmadı: Gradle `includeBuild`, Maven profilleri, Bazel/Buck/Pants/Lerna; "etkilenen" = bağımlılık bildirmiş olmak, kodun kullanılması değil.

### 107. Arka plan konsolidasyonu (D134, 2026-10-01)
- **Ne işe yarar:** `verinoda consolidate` bayatlamış claim'leri (kod eski haline dönmüşse) yeniden doğrular ve aynı ifadeyi iki kez kaydeden claim'leri tek kayda birleştirir (`--merge`).
- **Neden eklendi:** Claim'ler kod değişince bayatlıyor ve değişiklik geri alınsa bile biri `verify` çalıştırana kadar öyle kalıyor; iki analiz aynı ifadeyi iki kez de kaydedebiliyor (Letta sleep-time agent ve Cognee memify'dan esinlenme).
- **Durum:** "Tests only": yalnızca testlerle doğrulandı, gerçek proje üzerinde ölçüm yok. İki inceleme turunda bulunan hatalar (atomik olmayan birleştirme, hep aynı N claim'in seçilmesi vb.) düzeltildi. Yapılmadı: yalnızca bayat claim'ler kontrol edilir, farklı yazılmış aynı olgu bulunmaz, çelişkiler sadece raporlanır.

### 108. Hata ve trace içe aktarma (D135, 2026-10-01)
- **Ne işe yarar:** `trace-log` artık Sentry event JSON'unu ve OpenTelemetry (OTLP) trace dışa aktarımını da okuyup stack frame'lerini koddaki gerçek satırlara eşler (mapped / stale / ambiguous / not_in_repo).
- **Neden eklendi:** Üretim hataları genelde diskteki log'da değil hata takipçisinde yaşıyor; Sentry ve OTLP aynı bilgiyi (dosya, satır, fonksiyon) yapısal tutuyor. Ağ çağrısı veya hesap gerekmeden dışa aktarılmış dosyadan okunuyor.
- **Durum:** Fixture'larda ölçüldü: Sentry export'u 3 mapped, 3 stale, 1 ambiguous, 1 not_in_repo, 2 kütüphane frame'i; OTLP export'u 5 mapped frame. 5.000 frame'lik event'te en içteki 200 okunur. Yapılmadı: Sentry'den otomatik çekme yok, Go/Rust/.NET/Ruby metin stack'leri okunmuyor, saklanan claim en fazla `weak_inference`.

### 109. Spec'lerin koda ve testlere izlenmesi (D136, 2026-10-01)
- **Ne işe yarar:** Markdown gereksinim dosyalarındaki her kriterin ([R1] gibi id'li satırlar) hâlâ bir test, claim veya kod noktasıyla desteklenip desteklenmediğini kontrol eder (`spec check`); desteksiz olanlar CI'da exit koduyla görünür.
- **Neden eklendi:** Kiro, spec-kit ve Tessl gereksinimleri koda yakın tutuyor ama bir test yeniden adlandırılınca veya claim bayatlayınca spec hâlâ "sistem X yapar" demeye devam ediyor; Verinoda'da parçalar (claim, kanıt, test tespiti) vardı, kriterlere bağlanmamıştı.
- **Durum:** Bu deponun kendi testleri ve kodu üzerinde 5 kriterli spec: 1 tested, 2 unevidenced, 1 broken, 1 unchecked; durum `failed`, exit 1, 0,75 sn. 30 test yazıldı. Yapılmadı: claim/testin kriteri gerçekten karşılayıp karşılamadığı yargılanmaz, test çalıştırılmaz sadece tanımlı olduğu kontrol edilir, Kiro/spec-kit dosyaları olduğu gibi okunmaz.

### 110. Trigram regex indeksi (D137, 2026-10-01)
- **Ne işe yarar:** `verinoda search` ile tam metin veya düzenli ifade araması yapar; trigram indeksi sayesinde yalnızca eşleşmesi mümkün dosyaları okur.
- **Neden eklendi:** Verinoda'da sıralı arama (`query`) ve yapısal arama vardı ama düz tam/regex arama yoktu; her `TODO` veya `cap_response(` için tüm dosyaları gezmek gerekiyordu. Zoekt, Cursor ve Moderne trigram indeksiyle yanıtlıyor.
- **Durum:** Bu depoda (2.710 dosya, 45 MB) ölçüldü: indeks kurma 11 sn, `def changes_vs_base` 0,39 sn (tarama 2,7 sn), `todo|fixme` -i 0,68 sn (tarama 6,0 sn); her desen tarama ile birebir aynı sonuç verdi. En kötü durum `\w+` 9,6 sn (tarama 7,9 sn). Yapılmadı: ikili ve 2 MB üstü dosyalar aranmaz, her aramada ~0,35 sn tazelik kontrolü, indeks metnin ~1,2 katı yer kaplar.

### 111. Diff'e sınırlı mutasyon testi (D138, 2026-10-01)
- **Ne işe yarar:** `verinoda mutate` değişen satırlarda küçük kasıtlı hatalar (mutant) üretip değişikliğe ulaşan testleri çalıştırır; test geçerse değişikliğin test edilmediğini, sadece çalıştırıldığını gösterir.
- **Neden eklendi:** Review hangi testlerin değişikliğe ulaştığını ve geçip geçmediğini söylüyordu ama testlerin değişikliği gerçekten kontrol edip etmediğini söylemiyordu. Tüm projede mutasyon testi saatler sürer; diff'e sınırlanınca mutant başına bir test çalıştırması maliyeti var.
- **Durum:** Geçici bir depoda `price(n, vip)` değişikliğinde 7 mutant, 2'si öldürüldü, 5'i hayatta kaldı (test `vip` dalına hiç girmiyor); toplam ~20 sn (mutant başına ~2,5 sn). Yapılmadı: yalnızca Python, sabit küçük operatör seti, hayatta kalan mutant eşdeğer olabilir (kanıt değil ipucu), mutantlar sırayla çalışır.

### 112. Program olarak yazılan guard'lar (D139, 2026-10-01)
- **Ne işe yarar:** Karar kayıtlarının guard'ları artık küçük bir Python dosyası olabilir (`script path=FILE.py`); `decide check` bunu çalıştırıp kuralı (ör. "api/ içindeki her yazan handler önce `audit()` çağırır") ihlal satırlarıyla raporlar.
- **Neden eklendi:** Mevcut guard türleri tek satırlık `key=value` kurallarına sığıyordu; takımların kuralları çoğu zaman sığmıyor. Archgate bir ADR'nin kontrolünü program olarak taşıyor; burada sonuç diğer guard'larla (satır, exit kodu, baseline, SARIF) aynı yerde.
- **Durum:** Windows 11'de boş script 0,26-0,43 sn; bu deponun graph'ı (30.971 düğüm) çocuk süreçte 0,96 sn'de yükleniyor; audit hook'un ağ/süreç/yazma çağrılarını reddettiği elle denendi. Yapılmadı: sandbox değil (audit hook yalnızca bir tripwire, güven sınırı `verinoda trust`), script Verinoda'nın yorumlayıcısıyla çalışır, MCP'den kaydedilemez/çalıştırılamaz.

### 113. Daha fazla ajan için kurucular (D140, 2026-10-01)
- **Ne işe yarar:** `verinoda setup/install` artık Claude Code ve Codex'in yanı sıra Cursor, Gemini CLI, GitHub Copilot (VS Code), Kiro, Continue ve Aider'a MCP girdisi ve talimat dosyası kaydeder; aynı güvenlik kuralları ve tam geri alma (uninstall) geçerlidir.
- **Neden eklendi:** Kurucu yalnızca Claude Code ve Codex'i bağlıyordu, oysa insanlar bu diğer araçları da kullanıyor; her birinin MCP listesi ve/veya talimat dosyası belgelenmiş bir yerde duruyor.
- **Durum:** `tests/test_more_agents.py` inceleme sonrası 41 geçti, 1 atlandı (PyYAML yok); `tests/test_agents.py`'de 25 test bilinen ortam hatalarıyla başarısız. Yapılmadı: VS Code kullanıcı-seviye `mcp.json`, Continue kullanıcı klasörü, global `~/.gemini/GEMINI.md` yolu doğrulanmadı, Cursor'da `"type": "stdio"` gerekliliği teyit edilmedi, `doctor`/`status` yeni ajanları listelemiyor.

### 114. Mixin enjeksiyon noktaları (D141, 2026-10-01)
- **Ne işe yarar:** `verinoda mixin-check` bir Mixin'in hedef metot seçicisini, `@At` hedefini ve `@Shadow` üyelerini hedef sınıfın gerçek bytecode'una karşı kontrol eder; yanlış descriptor veya yanlış tip derlemeyi geçse de oyun açılışta çöker.
- **Neden eklendi:** `verinoda check` yalnızca metot adını karşılaştırıyordu; yanlış descriptor, yanlış `@At` hedefi ve yanlış tipli `@Shadow` hiç yakalanmıyordu. MinecraftDev ve minecraft-modding-mcp bunu class dosyalarına karşı yapıyor.
- **Durum:** `mixmod` fixture'ında planlı jar ile 30 satır (16 exists, 8 absent, 6 unknown) 0,3 sn'de; 8 planlı hata doğru en yakın öneriyle `absent` çıktı. Gerçek mod'un Minecraft jar'ı üzerinde ölçülmedi. Yapılmadı: `@At` ordinal/shift/opcode, `@Slice`, `@Overwrite`, Kotlin Mixin'leri, refmap'ler; `absent` jar eski olabileceğinden `strong_inference`.

### 115. Bağımlılık yapı matrisi ve C4 model kontrolü (D142, 2026-10-01)
- **Ne işe yarar:** `map --view dsm` projenin gruplar arası bağımlılıklarını sıralı bir matris olarak (sıraya aykırı işaretler dahil) gösterir; `map --view model` bir Structurizr C4 modelinin ilişkilerinin kodda gerçekten karşılığı olup olmadığını denetler.
- **Neden eklendi:** Mevcut görünümler en ağır bağımlılıkları ve döngüleri listeliyordu ama projenin bütün şeklini tek bakışta göstermiyordu; mimarisini C4 olarak çizen takımlar çizimin koda hâlâ uyup uymadığını bilmek istiyor.
- **Durum:** Bu depoda (30.971 düğüm) dsm görünümü 1,24-1,27 sn (26 grup, 38 hücre); elle yazılmış modelle 3 matched, 2 model_only, 1 not_checked, 5 undeclared çift bulundu. Etiketli mimariye veya başka projelerin modellerine karşı isabet/geri çağırma olculmedi. Yapılmadı: yansıma/DI/servisler arası çağrılar görülmez, DSL'in yalnızca alt kümesi okunur, görünüm her zaman exit 0 verir.

### 116. Ajanın kendi araç çağrılarına hook'lar (D143, 2026-10-01)
- **Ne işe yarar:** `verinoda agent-hooks install` ile Claude Code, Codex ve Cursor'da bir Grep/arama veya dosya okuma sonrası eşleşen sembollerin tanımı, çağıranları ve çağırdıkları Verinoda çağrısı yapılmadan ajana otomatik bağlam olarak eklenir.
- **Neden eklendi:** D62 bu cevabı üretmişti ama sadece Claude Code'un Grep aracı için kimsenin kurmadığı bir şablondu; eşik çalışmasında 133 aramanın 22'si Bash'te `grep` ile yapılmıştı ve Codex/Cursor hiçbir şey almıyordu. GitNexus, Codanna ve CodeGraph benzer hook kuruyor.
- **Durum:** Bu depoda `tool-hook` ayrı süreç olarak: hatırlanan kelime 0,40-0,43 sn, indeksten beri görülmemiş kelime 2,8 sn (graph yükü). Bağlamın ajanların bulduklarını değiştirip değiştirmediği olculmedi (D62 çalışması Grep hook'unda kazanç göstermedi). Yapılmadı: Codex dosya okumaları kapsanmaz, opt-in (setup kurmaz), Gemini CLI/Copilot/Kiro yok.

### 117. Arama sonuçları üzerinde betikli toplama (D144, 2026-10-01)
- **Ne işe yarar:** `verinoda inventory` birden çok aramanın sonuçlarını sayıp çaprazlar ve "bağlantı açıp hiç kapatmayan dosyalar" gibi envanter sorularına saymayla ve örnek satırlarla cevap verir.
- **Neden eklendi:** Ajanlar bu tür soruları arama sonucunun ilk sayfasını okuyup tahmin ederek cevaplıyordu; Sourcegraph'ın MCP evaluator'ı küçük bir betiği arama sonuçları üzerinde çalıştırıp sayıyı hesaplatıyor. Verinoda'nın tam ve taze araması (D137) vardı ama sayan/çaprazlayan bir şey yoktu.
- **Durum:** Bu depoda `sqlite3.connect` ve `.close()` envanteri 0,92 sn (32 ve 81 eşleşme, 2 dosya); `subprocess.run` sembol bazında 5,1 sn (46 eşleşme, 45 sembol). Koşul `ast` ile beyaz listeli ve `eval` hiç kullanılmaz. Yapılmadı: yalnızca trigram aramasının gördüğü metin dosyaları, eşleşme metindir anlam değil, hiç eşleşmesi olmayan dosyalar sayılmaz, MCP aracı yok.

### 118. Modlar arası Mixin çakışmaları (D145, 2026-10-01)
- **Ne işe yarar:** `mixin-check --conflicts` makinedeki mod jar'larını okuyup iki modun aynı metodu `@Overwrite` etmesi veya aynı çağrıyı `@Redirect` etmesi gibi çakışmaları bulur; `--log` ile `latest.log`'daki Mixin hatasının hangi moddan geldiğini adlandırır.
- **Neden eklendi:** Oyun tüm Mixin'leri aynı sınıfa uygular ve çakışmayı hiçbir modun kaynağı göstermez; hata `InvalidInjectionException` olarak başlangıçta çıkar ve hangi Mixin/mod olduğu belirsizdir. ModLens ve MixinConflictHelper bunu mods klasöründen yapıyor.
- **Durum:** Bu makinedeki CurseForge instance'ında (Minecraft 26.2, Fabric, 30 jar) 72 mod, 1.528 Mixin sınıfı, 1.983 enjektör ~1 sn'de okundu; 3 `conflict`, 17 `order_dependent`; temiz başlangıç log'unda yanlış hata çıkmadı. Oyun çalıştırılmadığı için gerçekte çakışıp çakışmadığı kontrol edilmedi. Yapılmadı: yalnızca makinedeki jar'lar, config plugin'leri okunmaz, sonuç Mixin belgelerinden tahmin (`strong_inference`).

### 119. Servisler arası kenarlar (D146, 2026-10-01)
- **Ne işe yarar:** Frontend'deki `fetch('/api/users/...')` çağrısını backend'deki aynı endpoint'in route handler'ına (Flask, FastAPI, Django, Express, NestJS, Spring, Next.js; ayrıca tRPC, gRPC, GraphQL, olaylar) metinden bağlar; `verinoda routes` komutu ve `trace` bu kenarları kullanır.
- **Neden eklendi:** Bir web uygulaması metinle birbirini çağıran en az iki programdır ama aralarında import yoktu; grafik iki ayrı parçaydı ve sayfadan endpoint'in arkasındaki koda `trace` "yön yolu yok" diyordu. codebase-memory-mcp, CodeGraph, GitNexus, Bito ve CodeSee bunu yapıyor.
- **Durum:** Fixture'larda doğru kenarlar çıktı; Verinoda'nın kendi deposunda 0 route, 27 istemci çağrısı, 0 kenar, 24 eşleşmeyen. Geçiş ilk seferde 3,5-4,2 sn, değişiklik yokken 0,37 sn. Gerçek bir full-stack depoda olculmedi, gerçek koddaki doğruluk bilinmiyor. Yapılmadı: tüm kenarlar INFERRED (proxy/gateway/çalışma zamanı base URL'i görülmez), Java/Go istemcileri okunmaz, `impact` ve `review` kenarları henüz izlemez.

### 120. Trace'lerden çalışma zamanı kusurları (D147, 2026-10-01)
- **Ne işe yarar:** `verinoda observe` testler çalışırken SQL ifadelerini ve örneklenmiş stack'i de kaydedip N+1 sorgularını (döngü içindeki çağrı yoluyla), tekrarlanan SQL'i ve yavaş yolları raporlar.
- **Neden eklendi:** Observe hangi çağrının hangi fonksiyonu başlattığını kaydediyordu ama maliyeti değil; N+1, aynı satırların tekrar okunması ve testin zaman harcadığı fonksiyon yalnızca çalışma zamanında görünür, test süiti de bu kodu zaten çalıştırıyor. AppMap ve Digma kayıtlı çalıştırmalarda bulur.
- **Durum:** Windows 11, Python 3.13: eklenti izlenen kısma %19-23 CPU/duvar süresi ekliyor (tüm observe'in %2-8'i); SQL fixture'ında 6.000 ifadelik N+1 tespit edildi. 50 test, gerçek SQLAlchemy, büyük gerçek proje, Python 3.10, Linux/macOS olculmedi. Yapılmadı: yalnızca sqlite3 (ve SQLAlchemy olayları), bulgular çalıştırma kapsamlı, `analyze/review --observe` ve MCP kusur kaydetmez.

### 121. Türetilmiş olgular (D148, 2026-10-01)
- **Ne işe yarar:** `verinoda fact` bir sonucu (claim'lerden türetilmiş ya da kayıtlı bir arama) adla saklar ve dayandığı kod değişince otomatik `stale` yapıp yeniden hesaplanabildiğinde geri yükseltir.
- **Neden eklendi:** Glean'in türetilmiş yüklemleri ve jQAssistant kavramları hesaplanmış sonucu adla saklıyor; Verinoda'da claim'ler vardı ama adla saklanan sonuç yoktu, bir ajanın "storage'a yazan yerler" gibi sonucu her oturumda yeniden bulması ve ne zaman geçersizleştiğini bilememesi gerekiyordu.
- **Durum:** `examples/orders_app` (12 dosya) kopyasında, inceleme sonrası: facts yokken update 0,75-0,84 sn, 5 arama olgusuyla 1,06-1,12 sn; olgu eklemek ortalama 0,22 sn. Yapılmadı: güncellemeler arasında yalnızca eşleşen dosyalar yeniden kontrol edilir (yeni eşleşme sonraki update'te görülür), kural dili (join/negasyon) yok, düzeltilen claim'e bağlı olgu yeniden eklenene dek bayat kalır.

### 122. Yerel dosya izleyici (D149, 2026-10-01)
- **Ne işe yarar:** `ui --watch` ve yeni `mcp serve --watch` dosya değişikliklerini işletim sistemi olaylarıyla (Windows `ReadDirectoryChangesW`, Linux `inotify`) yakalayıp indeksi hemen günceller.
- **Neden eklendi:** Eski izleyici ağacı her 2 sn listeleyip karşılaştırıyordu; bu depoda tek listeleme 0,92 sn sürdüğü için kayıttan güncellemeye 12-22 sn geçiyor, boştayken 20 sn'de 1,5 sn CPU yiyordu. MCP sunucusunda hiç izleyici yoktu (narsil-mcp ve codebase-memory-mcp işletim sistemi olaylarını kullanıyor).
- **Durum:** Bu depoda (2.735 dosya): kayıttan güncellemeye medyan 1,22 sn (eski izleyici 20,4 sn); boşta 20 sn'de 0,00 sn CPU (polling 1,52 sn). Orders örneğinde uçtan uca 6,6 sn. Linux `inotify` ve `watchdog` yolu burada çalıştırılmadı (sentetik kayıtlarla test edildi). Yapılmadı: olaylar `.gitignore`'a göre değil klasör adına göre süzülür, proje kökü dışı dosyalar izlenmez.

### 123. Kontrol ve veri bağımlılığı (D150, 2026-10-01)
- **Ne işe yarar:** `verinoda slice PATH:LINE` bir fonksiyon içinde bir değerin nereden geldiğini (geriye dilim) veya nereleri etkilediğini (ileri dilim) satırlarıyla, gerekirse çağıran fonksiyonlara geçerek gösterir.
- **Neden eklendi:** "Bu argümanın değeri nereden geliyor?" sorusunu gözden geçiren kişi güvenmeden önce sorar ve taint analizi (sonraki madde) buna dayanır; Verinoda grafiğinde fonksiyonlar arası çağrılar vardı ama fonksiyonun içi yoktu. Joern, GitNexus, narsil-mcp ve CodePrism program bağımlılık grafiğiyle yanıtlıyor.
- **Durum:** Bu depoda 7.861 fonksiyonun hepsi için dilim 20,1 sn'de, en fazla 0,14 sn; `dispatch_command` (3.779 satır) 38 satırlık dilim 0,5 sn; test projesinde `db.execute(...)` argümanı `amount`'tan isteğe kadar izlendi. Yapılmadı: yalnızca Python, heap yaklaşık (takma adlar, global'ler görülmez), çağrılan fonksiyonun gövdesine girilmez, durum `strong_inference`, MCP aracı yok.

### 124. Paket varlık ve slopsquatting kontrolü (D151, 2026-10-01)
- **Ne işe yarar:** `check --deps --registry` kodun veya manifestin adını verdiği paketin kayıt defterinde (PyPI, npm, crates.io, Maven Central, Go proxy) gerçekten var olup olmadığına, yeni/yanlış yazılmış/kaldırılmış olup olmadığına bakar ve typo benzeri adları işaretler.
- **Neden eklendi:** Bir asistan var olmayan bir paket adı yazabilir ve saldırgan o adı sonradan kaydedip yüklenmesini sağlayabilir (slopsquatting), `reqeusts` gibi yanlış yazım da aynı risk. Socket MCP ve Endor Labs yeni bağımlılığı yüklemeden önce kayıt defterinde denetliyor; `check --deps` adların var olup olmadığını bilmiyordu.
- **Durum:** 2026-10-01'de elle gerçek isteklerle denendi: PyPI `requests` 1.062 ms (ok), `huggingface-cli-tools-vx` ve `reqeusts` not found, npm `left-pad` caution, crates/Maven/Go yanıtları ~0,4-1,2 sn; 8 sorgu önbellekte 26 ms. Yapılmadı: varsayılan ağ kapalı (ancak açıkça `--network on`), zararlı kod taranmaz, PyPI indirme sayısı yok, popüler ad listesi küçük ve elle yazılmış, özel indeksler gönderilmez.

### 125. tsc, pyright veya mypy ile tam tip ve ad kontrolü (D152, 2026-10-01)
- **Ne işe yarar:** `verinoda check --checker tsc|pyright|mypy|auto` projenin kendi derleyicisini/tip denetleyicisini çalıştırıp TypeScript/Python'daki olmayan üyeleri, yanlış çağrıları ve tip uyuşmazlıklarını (`svc.sendd()` gibi) satır kanıtıyla raporlar.
- **Neden eklendi:** Check TypeScript'te yalnızca import'lara bakıyordu; ajanın uydurma import'tan sonra en sık yaptığı hata olmayan üye, yanlış argüman veya yanlış tip ve bunlar derleyici gerektiriyor, bu yüzden dosya `not_checked` kalıp çıkış kodu 4 oluyordu. Her TypeScript projesinde derleyici zaten var.
- **Durum:** Bu makinede gerçek tsc/pyright/mypy kurulu olmadığından gerçek bir denetleyiciye karşı hiçbir şey olculmedi; yalnızca sahte denetleyicilerle 39 test (27,3 sn), iki dosyalık tsc kontrolü 0,9 sn. Yapılmadı: yalnızca CLI (MCP `code_check` denetleyici çalıştırmaz), güvenilmeyen projede pyright hiç çalışmaz, ayrıştırıcılar gerçek çıktıyla değil belgelenmiş biçimlerle test edildi, `tsc -b` ve basedpyright yok.

### 126. Runtime diff between base and head (D153, 2026-10-01)
- **Ne işe yarar:** Bir değişikliğin testler çalışırken ne yaptığını gösterir: aynı testler önce eski (base) commit'te, sonra yeni (head) halde çalıştırılır ve çağrılar, kütüphane çağrıları, SQL, route'lar, exception'lar ve test sonuçları karşılaştırılır.
- **Neden eklendi:** `review --observe` testleri yalnızca bir kez çalıştırıyordu; döngüye giren sorgu, yeni kütüphane çağrısı, değişen exception tipi gibi çalışma anı farkları okuyucuya kalıyordu. AppMap iki kaydı karşılaştırıyor, Verinoda'da kayıtlar vardı ama hep tek koşu vardı.
- **Durum:** Uçtan uca fixture'da iki koşu ve karşılaştırma yaklaşık 6-7 sn; raise kancası 200.000 raise için 0,80 sn'den 1,32 sn'ye çıktı (raise başına yaklaşık 2,6 us). Not done: tek koşu (flaky test sahte fark üretebilir), taşınan/yeniden adlandırılan fonksiyon "silindi+eklendi" görünür, raise kaydı yalnızca Python 3.12+, `review --observe` yaklaşık 2 kat yavaşladı. Testler: `tests/test_runtime_diff.py` (17).

### 127. Taint analysis (D154, 2026-10-01)
- **Ne işe yarar:** Kullanıcıdan gelen bir değerin kontrolsüz biçimde SQL, shell, `eval` veya dosya yoluna ulaşıp ulaşmadığını, izlediği yolla birlikte gösterir (`verinoda taint`, yalnızca Python).
- **Neden eklendi:** `slice` (D150) tek bir satırın değerinin nereden geldiğini söylüyordu; güvenlik incelemesinin sorusu bunun tersi ve tüm proje çapında. CodeQL, Semgrep Pro, Pysa, Joern gibi araçlar bunu yapıyor; Verinoda'da güvenilmeyen girdi kavramı yoktu.
- **Durum:** Verinoda'nın kendi kodunda 369 dosya, 329 sink çağrısı: uzak kaynaklarla 36,6 sn ve yol yok; `--local` ile 37,5 sn ve 21 yol (hepsi `os.environ` kaynaklı). Not done: yalnızca Python ve veri bağımlılığı, heap yaklaşık hesaplanır, kaynak/sink isimle eşleşir; bulunan yol zafiyet kanıtı, bulunmaması güvenlik kanıtı değildir. Testler: `tests/test_taint.py` (41).

### 128. Code query language (D155, 2026-10-01)
- **Ne işe yarar:** `verinoda q` ile graf üzerinde Cypher benzeri küçük bir dille serbest soru sorulur (ör. "HTTP handler'dan erişilen ve depoya yazan fonksiyonlar"); her satır kanıt (`file:line`) ile döner.
- **Neden eklendi:** CodeQL, Glean, jQAssistant, NDepend ve Joern kullanıcıya öngörülmemiş sorular sordurur; Verinoda'da sabit görünümler vardı ve düğüm türlerini, sınırlı yolları, tanım metnini ve kenar yokluğunu birleştiren soru elle komut zincirleriyle cevaplanıyordu.
- **Durum:** Kendi indeksinde (36.028 düğüm, 88.526 kenar) sorgular yaklaşık 0,8-2,1 sn sürdü; fixture'da hedef sorgu 0,099 sn'de tek satır döndürdü. Not done: iki uç arasında tek (en kısa) yol, çıkarım iki yönlü karşılıklı çağrıyı kenar olarak tutmaz, `handler` yalnızca HTTP route tablosudur.

### 129. Import JVM checker findings (D156, 2026-10-01)
- **Ne işe yarar:** Error Prone, NullAway ve `jdeps` çıktılarını (Gradle/Maven/CI logu) okuyup her bulguyu depodaki dosya ve sembole bağlar (`verinoda import-findings`); hiçbir şey çalıştırmaz.
- **Neden eklendi:** Bu araçların bulguları build içinde çıkıyor ama SARIF yazmıyorlardı; ajan "bu null güvenli mi?" veya "JDK 21'de derlenir mi?" sorusunda logu elle okumak zorundaydı.
- **Durum:** 50.000 satırlık sentetik log 1,34-1,37 sn'de okundu (5.000 bulgu). Gerçek bir projenin logu ölçülmedi. Not done: bulgu aracın kendi beyanıdır, Kotlin mesajları okunmaz, log bütün olarak belleğe alınır (200 MB'a kadar). Testler: `tests/test_jvm_findings.py` (19).

### 130. Mixin debug export as evidence (D157, 2026-10-01)
- **Ne işe yarar:** SpongePowered Mixin'in yazdığı `.mixin.out` dışa aktarımı varsa, bir Mixin'in oyunda gerçekten uygulanıp uygulanmadığını (applied/merged/not_applied/unknown) bayt kodundan okur ve `mixin-check` / `analyze` kanıtına ekler.
- **Neden eklendi:** `mixin-check` yalnızca isimlerin var olup olmadığına, `--conflicts` ise ek açıklamalardan tahmine bakıyordu; Mixin'in çalışan sınıfa ne yaptığı bilinmiyordu.
- **Durum:** Sentetik export'ta 3 applied, 1 merged, 2 not applied, 1 unknown (24 test, 6,6 sn); 10.020 metotluk hedefte 0,23 sn'den 0,92-0,97 sn'ye. Gerçek bir oyun export'unda ölçülmedi (makinede `.mixin.out` yok). Not done: yalnızca son koşunun içeriği, eski olabilir; eklenen alanlar/arayüzler Mixin başına değil sınıf başına listelenir.

### 131. ORM, DI and database schema (D158, 2026-10-01)
- **Ne işe yarar:** Koddan veritabanı tablolarını, ORM modellerini, SQL okuma/yazmalarını ve bağımlılık enjeksiyonunu (FastAPI Depends, Spring) grafa düğüm/kenar olarak ekler (`verinoda schema`); "orders tablosuna kim yazıyor" sorusu cevaplanabilir.
- **Neden eklendi:** Veri akışı görünümü `.save()` çağıran fonksiyonda bitiyordu; hangi tablo, kim okuyor, hangi model eşliyor, migration ne yaptı, graf bilmiyordu. agentforge-graph, Graphify ve Kodit bunları grafa koyuyor.
- **Durum:** Verinoda'nın kendi kodunda 415 dosya okundu, 154'ü ayrıştırıldı; 54 tablo ve 410 kenar (252 reads_table, 91 writes_table, 67 migrates); ilk koşu 15 sn, önbellekle 0,7 sn. Not done: özel isimlendirme stratejileri, parça parça kurulan sorgular, dinamik tablo adları, JPA ilişkileri, Kotlin Exposed/jOOQ/MyBatis görülmez.

### 132. Typed questions, batched (D159, 2026-10-01)
- **Ne işe yarar:** `verinoda tq` (ve MCP `tq`) tek çağrıda en fazla 20 kapalı soruyu ("A, B'yi çağırıyor mu?", "F'yi kim çağırıyor?") yanıtlar; her yanıt tek değer, durum ve `file:line` taşır.
- **Neden eklendi:** Ajan her kapalı olgu için `project_query`/`analyze` çağırıyor, her biri bir tur ve birkaç bin karakter tutuyordu; evet/hayır cevabı da yoruma açık düzyazıydı.
- **Durum:** 109 elle doğrulanmış vaka (75 dev, 34 held-out): held-out 30 doğru, 0 yanlış, 4 bilinmiyor; dev 70 doğru, 1 yanlış (`weak_inference`). 20 soru, bu depoda tq 1,9 sn vs analyze 148,8 sn; elle kontrolde 6 "hayır"ın 3'ü yanlıştı (hepsi zayıf). Not done: set küçük, kurallarla aynı yazar; 20 soru kaydedilmediği için karşılaştırma tekrarlanamaz.

### 133. Host intent for analyze (D160, 2026-10-01)
- **Ne işe yarar:** `analyze`'ı çağıran ajanın soruyu nasıl okuduğunu (`--intent`) söylemesine izin verir; kurallar niyetle çelişiyorsa niyet reddedilir ve kuralların planı kullanılır.
- **Neden eklendi:** `analyze` niyeti ipucu kelimelerden okuyor, ipucu yoksa `locate` sayıyordu; soruyu zaten okumuş olan host'un bunu söylemesi ama güvenilmeden kontrol edilmesi fikri (backlog 13.8).
- **Durum:** Core profile'a alınmadı: doğru niyet `wrong_met` sayısını düşürmedi (39 vakada 1/39 -> 1/39; fastbench 12 -> 12), doğru niyet 3 doğru `met` kararını zayıflattı, yanlış niyet 68 sorunun 62'sinde reddedildi. Turlar ve gerçek model ölçülmedi. Not done: ipucu yoksa yanlış niyet geçer; doğru etiketleri kural yazarı koydu.

### 134. Real-world benchmark on pinned popular repositories (D161, 2026-10-01)
- **Ne işe yarar:** Verinoda'yı on popüler açık kaynak depoda (sabit tag/sha) çalıştırıp komutların çökmeden, makul sürede ve basit olguları doğru yaptığını ölçen bir harness.
- **Neden eklendi:** Eski benchmark'lar üç Python korpusundaydı ve ikisi araç yazılırken biliniyordu; ayarlanmamış kodda ve başka dillerde çalışıp çalışmadığı açıktı.
- **Durum:** On depodan yalnızca ikisi (gin, fzf, ikisi de Go) çalıştırıldı: çökme ve timeout yok; v1 altın olgularda gin 7/10, fzf 8/10; v2'de 4/5. Not done: kalan 8 depo ve Python/JS/Rust/Java/PHP ölçülmedi; her olguyu tek kişi doğruladı, tek koşu, varyans yok.

### 135. Measured frequencies for typed answers (D162, 2026-10-01)
- **Ne işe yarar:** `tq` yanıtlarına, yeterli held-out örnek (en az 30) varsa "ölçülmüş doğruluk k/n" bilgisi ekler ve her durum etiketinin gözlenen doğruluğunu `CONFIDENCE_CAP` ile karşılaştırır.
- **Neden eklendi:** Durum etiketi kuralın tavanıydı, ölçülmüş oran değildi; "calls ... = no | strong_inference ne sıklıkla doğru?" sorusuna cevap yoktu.
- **Durum:** 323 vakalık ikinci, dondurulmuş held-out set yazıldı; toplam 357 held-out vakada 350 karar, 347 doğru, 3 yanlış, 7 bilinmiyor. 22 hücreden 4'ü n>=30, 3'ü gösteriliyor. Hiçbir tavan değiştirilmedi (hepsi gözlenen aralığın altında). Not done: import takma adı yanlış `statically_verified` verebiliyor (düzeltilmedi); Study F koşulmadı.

### 136. Broader language coverage from upstream Graphify (D163, 2026-10-01)
- **Ne işe yarar:** Upstream Graphify v0.9.73'teki yeni dilleri (COBOL, VB.NET, R, Erlang, Solidity) ve OCaml sınıfları, Razor `@functions`, Terraform öznitelik gizleme gibi iyileştirmeleri Verinoda indeksine ekler.
- **Neden eklendi:** Bu dillerdeki bir proje indeksten hiçbir şey alamıyordu (dosyalar sınıflandırılmıyor ya da çıkarıcı yoktu); backlog satırı COBOL, R, Solidity, Erlang, OCaml, Terraform, Razor'ı adlandırıyordu.
- **Durum:** 12 yeni fixture dosyasından 54 düğüm, 60 kenar çıktı; diğer korpuslarda (111 dosya, orders_app, gin, guzzle) graf farkı yok. Not done: ekstralar olmadan varsayılan kurulumda yalnızca COBOL okunur; yeni diller için anchors, routes, schema, taint, check ve test algılama bağlanmadı.

### 137. Several projects from one MCP server, HTTP transport and daemon (D164, 2026-10-01)
- **Ne işe yarar:** Tek bir MCP sunucusu birden fazla projeyi yanıtlar (`--projects`, `--all-projects`, her araçta `project` argümanı), HTTP üzerinden ve arka planda daemon olarak çalışabilir.
- **Neden eklendi:** Her proje için ayrı bir Python süreci, ayrı graf ve ayrı menü kaydı gerekiyordu; Graphify, codebase-memory-mcp, Codanna ve Kodit zaten çoklu proje sunuyor.
- **Durum:** İkinci büyük proje tek süreçte yaklaşık 68 MB ekliyor, ayrı sunucu süreci 166 MB tutardı; `--max-loaded 1` her geçişte grafı yeniden yükler (yaklaşık 0,9-1,0 sn). Not done: HTTP'de TLS, OAuth ve istemci başına token yok; daemon çökünce yeniden başlamaz; projeler arası çağrılar birbirini bekler.

### 138. Definition lines: overload implementations and annotated declarations (D165, 2026-10-01)
- **Ne işe yarar:** Bir tanımın gösterilen satırı artık adın satırıdır (Java/Kotlin/C# ek açıklamasının değil) ve Python `@overload` fonksiyonlarında ilk stub yerine gerçek uygulama gösterilir.
- **Neden eklendi:** Gerçek dünya koşusunda sqlmodel `Field` boş stub'da (:242), gson `doPeek` `@SuppressWarnings` satırında (:581) gösteriliyordu; `@Override` hemen her Java metodunda olduğundan cevaplar sık sık bir satır kaymıştı.
- **Durum:** sqlmodel 9/10 -> 10/10, gson 9/10 -> 10/10; çökme ve timeout yok. Not done: diğer 8 depo koşulmadı; motor dosyaları değiştiği için `tq` "measured" bilgisi audit yeniden koşulana kadar gizli; `tests_upstream` çalıştırılmadı.

### 139. Route prefixes, trailing slashes and bounded route output (D166, 2026-10-01)
- **Ne işe yarar:** `routes` komutu `include_router(prefix=settings.API_V1_STR)` gibi hazır sabit prefix'leri çözer, sondaki `/` işaretini korur ve test çağrılarını doğru uygulamanın route'larına bağlayıp çıktıyı sınırlar.
- **Neden eklendi:** full-stack-fastapi-template'te prefix kaybolup `/login/access-token` görünüyordu (gerçek: `/api/v1/...`); express'te 880 supertest çağrısı belirsizdi ve `routes --json` 248 KB'tı.
- **Durum:** Template v1 7/10 -> 9/10; express `routes --json` 247.767 -> 70.871 bayt, belirsiz çağrı 880 -> 197. Not done: `get_settings()`, ortam değişkeni gibi hesaplanan değerler çözülmez; Spring/JAX-RS sondaki `/` düşer; çalışma anında yüklenen route'lar eşleşmez.

### 140. Distinct symbols never share a node (D167, 2026-10-01)
- **Ne işe yarar:** Büyük/küçük harf veya baştaki alt çizgi dışında aynı olan iki farklı sembol (ör. axios `request` ve `_request`) artık aynı grafa düğümüne düşmez, ayrı kimlik alır.
- **Neden eklendi:** `normalize_id` bu farkları siliyordu; ikinci tanım sessizce atılıp çağrıları ilkine yazılıyordu, express'te `sendFile` sorgusu kesin eşleşme diye `sendfile`'a gidiyordu.
- **Durum:** axios v1 7/10 -> 9/10, express 4/10 aynı; axios grafında 5 ayrılmış çift elle kontrol edildi, express'te 0. Not done: sınıf benzeri düğümler (`Q`/`_Q`) hâlâ birleşir, özel çıkarıcılar (Rust, Dart...) değişmedi, C++ out-of-line ikizleri adlandırılamaz.

### 141. JavaScript assigned methods and nested functions (D168, 2026-10-01)
- **Ne işe yarar:** JS/TS'te `res.json = function json() {...}` gibi modül seviyesinde atanan fonksiyonlar ve fonksiyon içinde tanımlı iç fonksiyonlar (`const login = async () => {...}`) artık sembol olur.
- **Neden eklendi:** Express'in response/request/application dosyaları neredeyse tamamen bu biçimde yazıldığı için `trace lib/response.js::json stringify` çözülemiyordu; iç fonksiyonların çağrıları dıştaki hook'a yazılıyordu.
- **Durum:** Express v1 4/10 -> 7/10, template 7/10 -> 8/10, kayıp yok; 494 JS/TS dosyada düğüm 2611 -> 2754. Not done: başka dosyadan üyelere çağrı yalnızca alıcı tipi bilinirse bağlanır, hook'tan dönen fonksiyonlar (destructure) kenar almaz, virgüllü/minify kod sembol üretmez.

### 142. Repository groups: cross-repository call links (D169, 2026-10-01)
- **Ne işe yarar:** `verinoda group` ile birkaç depo bir grup olarak tanımlanır; A deposundan B deposundaki bir fonksiyona çağrı, çağrı yeriyle birlikte kenar olarak izlenebilir (`group link`, `trace`, `query`).
- **Neden eklendi:** Servis, istemci kütüphanesi ve ortak paket ayrı depolarda durunca çağrı grafta bitiyordu; API handler'dan kütüphane fonksiyonuna `trace` yol bulamıyordu. Sourcegraph ve GitNexus depoları bağlıyor.
- **Durum:** Gerçek çiftte (template + sqlmodel) 58 bağlantı, 58'i doğrulanmış, `group link` 1,1-1,2 sn; 7 bağlanmayan çağrı SQLAlchemy'den yeniden dışa aktarılan isimler. Not done: örnek çağrıları (`x = Engine(); x.run()`) izlenmez; yalnızca Python, JS/TS, Go, Java; hangi sürümün çalıştığı bilinmez.

### 143. Pinned regression tests from recorded calls (D170, 2026-10-01)
- **Ne işe yarar:** `verinoda pin FUNCTION` testler çalışırken bir fonksiyonun aldığı argümanları ve sonuçları kaydedip bağımsız bir pytest dosyasına yazar; refactor öncesi mevcut davranışı sabitler.
- **Neden eklendi:** Refactor'dan önce davranışı tutan testler istenir; Pynguin ağır bir bağımlılıktır, projenin kendi testleri ise zaten bilinçli girdiler içerir.
- **Durum:** orders_app `apply_discount` için 4 girdi kaydedildi ve tuttu; kayıt yükü yaklaşık %4 içinde; yazılan her dosya kendi tekrar koşusunu geçti (`experiment_verified`). Not done: yalnızca dönüş değeri/exception sabitlenir, yan etkiler değil; Python 3.12+ gerekir; nesne alan fonksiyonlar (`place_order`) hiçbir şey sabitlemez; MCP'de yok. Testler: `tests/test_pin.py` (80).

### 144. Receivers of a stated type: Go, Rust, PHP static calls, JS call/apply/bind (D171, 2026-10-01)
- **Ne işe yarar:** Tipi belirtilmiş alıcılar üzerinden yapılan metot çağrılarını (Go zincirleri, Rust `Type::m()`, PHP statik çağrılar, JS `fn.call/apply/bind`) doğru metoda bağlar.
- **Neden eklendi:** Gerçek dünya koşusunda PHP `Utils::chooseHandler()` sınıfa bağlanıyor, Rust `Controller::new` ve Go `root.getValue` hiç kenar üretmiyordu.
- **Durum:** guzzle 8/10 -> 10/10, bat 8/10 -> 10/10, gin 7/10 -> 8/10 (v2 2/2), axios 9/10 aynı; çökme yok. Eklenen kenarlardan seçilen örneklerin tümü (20'şer) doğru okundu. Not done: Rust alan/metot sonucu tipleri, Go paket dışı zincirler, PHP `parent::` yok; örnekler küçük, her kenarı kimse okumadı.

### 145. Language-server navigation and call-edge verification (opt-in) (D172, 2026-10-01)
- **Ne işe yarar:** Kurulu bir language server'a (tsserver, jdtls vb.) sorarak TypeScript/Java çağrı kenarlarının gerçekten o tanıma bağlanıp bağlanmadığını doğrular; onaylanırsa `statically_verified`, başka yer gösterirse `contradicted` olur.
- **Neden eklendi:** Python dışında çağrı kenarları metinden okunuyor ve en fazla `strong_inference` oluyordu; hangi tanıma bağlandığını bir language server zaten biliyor (Serena, mcp-language-server, JetBrains benzer işi yapıyor).
- **Durum:** Yalnızca testler için yazılmış sahte sunucuyla ölçüldü (bir `definition` turu 0,21 ms; 4 kenarlık doğrulama 0,57-1,07 sn). Gerçek bir language server makinede yoktu, test edilmedi; ilk gerçek sunucuda düzeltme gerekebilir. Not done: sunucu başına komut başı başlatma, 30 sn zaman aşımı, MCP/`ask` sunucu başlatmaz.

### 146. Incremental update behind a switch (stage 2 of update proportional to the change) (D173, 2026-10-01)
- **Ne işe yarar:** Tek dosya değişince grafı baştan kurmak yerine yama ile günceller (`VERINODA_INCREMENTAL=1` veya `index.incremental: true`); sonuç tam taramayla düğüm düğüm aynı olmalıdır. Varsayılan olarak kapalı.
- **Neden eklendi:** 2.760 dosyalık Verinoda kopyasında eklenen tek fonksiyon `update`'i 98,8 sn tuttu; artımlı çıkarım tek başına saniyelere inemezdi.
- **Durum:** 2.858 dosyalık kopyada güncelleme 46,8-52,4 sn (önce 101-126 sn), taze taramaya eşit çıktı; fuzz: 300 güncellemede 257 yama, 43 geri dönüş, 0 fark. Not done: kapalı tutuldu (review altı fark bulmuştu); küme analizi yenilenmez; dosya ekleme/silme ve Kotlin/Rust/C# gibi diller tam derlemeye döner; "saniyeler" tam tutmuyor.

### 147. `locate`, `coupled` and the locate daemon (D174, 2026-10-03)
- **Ne işe yarar:** `locate` bir görev için ilgili dosyaları, `coupled` ise bulunan dosyaların kardeşlerini (aynı commit'lerde değişenler, import/include edilenler, aynı kök adlı çift ve ikizler) en fazla 1.800 karakterlik kısa metinle listeler; bir daemon grafı sıcak tutar.
- **Neden eklendi:** İki ajan çalışması, ajanın tek dosyalı görevlerde tavanda ama çok dosyalı görevlerde recall'unun çoğunu kaybettiğini gösterdi (kaçırılanlar bulunan dosyaların kardeşleri). Çevrimdışı oracle'da bu sinyallerle 13-15 dosyalık liste, kaçırılan dosyaların seL4'te 28'in 15'ini, Home Assistant'ta 15'in 12'sini kapsadı; eski co-change okuması 2 ve 0 kapsadı. Daemon: graf yükleme 3 sn (seL4) ile 30 sn (Home Assistant) arası sürdüğü için.
- **Durum:** Bu bölümde "Measured" yok; sonuçlar bölüm dışında `benchmarks/results/agent-compare-assist-dev-2026-10-03/README.md` içinde (burada okunmadı). Not done: sıralama ağırlıkları uydurulmadı, sinyal gücüne göre sıralı; `CORE_DIRECT`'e alınmadı. Testler: `tests/test_locate.py`, `tests/test_locate_daemon.py`.

### 148. The improvement checklist pane (D175, 2026-10-04)
- **Ne işe yarar:** `/verinoda-improve` inceleme ve iyileştirme önerilerini ayrı panelde sunar; kullanıcı uygula, kalsın veya önce kontrol et seçer, kendi maddelerini ekler ve sonuçları izler. `improveOffer` varsayılan kapalıdır; indeks gerekmez, otomatik oturumlarda çalışmaz.
- **Neden eklendi:** "Daha iyi yap" gibi belirsiz isteklerde değişiklik yönünü kullanıcıya somut seçeneklerle seçtirmek; şüpheyi doğrulanmış sorun ve işaretlenmeyen maddeyi tercih saymamak için.
- **Durum:** İlk aşama kodlandı; 126 test geçti (88 eski, 38 yeni), mod doğrulaması ve TypeScript kontrolü geçti. Gerçek oturum, ajan davranışı, kullanıcı memnuniyeti, süre/token ve yazma gecikmesi ölçülmedi. Alternatif grupları, bağımlılık/çatışma uyarıları, yön değiştirme, görsel önizleme ve tercih hafızası sonraki aşamalarda.

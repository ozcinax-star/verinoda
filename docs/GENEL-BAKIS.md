# Verinoda — Ne yapar, Graphify'a neler ekler, teorik model ve ölçülen sonuçlar

> **Durum: TAMAMLANMADI — güvence verilmez.** Bu belge Verinoda'yı
> 2026-09-23 itibarıyla kodda olduğu haliyle anlatır; her bileşenin gerçek
> durumu yanında yazılıdır. **Bölüm 6 teorik bir modeldir:** oradaki oranlar
> ölçüm değil, açıkça yazılmış varsayımlarla yapılmış hesaplardır.
> **Bölüm 7 ölçülen sonuçlardır.** En güncel sayılar için
> `docs/BENCHMARKS.md`'ye bakın. Bu belge yalnızca o dosyada ya da tur-3
> ekip raporlarında bulunan sayıları, çekinceleriyle birlikte aktarır.

İçindekiler

1. Tek cümlede Verinoda
2. Temel: Graphify ne sağlıyor?
3. Bir soru nasıl yanıtlanıyor (adım adım, gerçek çıktıyla)
4. Bileşenler ve gerçek durumları
5. Graphify'a göre yükseltmeler (tablo)
6. Teorik tasarruf ve maliyet modeli (ölçüm değil)
7. Ölçülen sonuçlar
8. Ek maliyetler ve zayıf kalınan yerler
9. Ölçüm nasıl yapılıyor, neyi ölçmüyor
10. Kurulum ve kullanım özeti
11. Sözlük

Durum etiketleri (Bölüm 4 ve 5):

- **Çalışıyor:** kodda var; CLI, MCP ve analiz döngüsüne bağlı; otomatik
  testleri var. Yalnızca Windows 11 / Python 3.12'de denendi.
- **Kısmi:** var, ama yanında yazılan eksiklerle.
- **Yok:** yapılmadı.

Ürün test takımı (`pytest tests`) bu sürümde Windows 11 / Python 3.12'de
geçiyor; güncel sayı `README.md`'de. Linux ve macOS'ta hiç çalıştırılmadı.

---

## 1. Tek cümlede Verinoda

Verinoda, bir yazılım projesine Türkçe ya da İngilizce sorulan soruyu önce
yapılandırılmış bir **soru planına** çevirip kodla eşleştiren, sorudaki
referansları (repo, sürüm, paket, PR, makale) kastedilen **tam sürüme
sabitleyen**, cevabı **kanıta bağlı iddialar** olarak veren ve bu iddiaları
otomatik kontrollerle (istenirse testleri çalıştırarak) sınayan bir kod analiz
aracıdır. İddiaların dayandığı kod değişirse, bir sonraki `update` ya da
`analyze` çalıştırmasında iddia `stale` (eskimiş) olarak işaretlenir.

Terminalden kullanılır. Claude Code (`/verinoda`) ve Codex (`$verinoda`)
için skill ve MCP kurulumu vardır. Önceki skill metniyle gerçek (headless)
oturumlarda doğrulandı (`docs/AGENT-VERIFICATION.md`). Tur-3'te eklenen
"önce soruyu anla" ve "referanslar" protokollerinin gerçek ajan oturumunda
denendiğine dair bir kayıt henüz yok.

---

## 2. Temel: Graphify ne sağlıyor?

Verinoda, Graphify'ın açık kaynak kodunu (commit `20a20d30`, Apache-2.0)
kendi deposuna alarak başlar. Graphify'dan gelen motorun
(`verinoda/project_index/`) kaynak kodu, içe aktarma yolları dışında
**değiştirilmez**. Tek fark, dışarıdan uygulanan ve grafiği değiştirmeyen bir
hızlandırma yamasıdır (`docs/UPSTREAM.md`). Motor şunları yapar:

- Upstream README'ye göre yaklaşık 40 dili (37 tree-sitter grameri)
  ayrıştırır: Python, JS/TS, Go, Rust, Java, C/C++, C#, Ruby, Kotlin, PHP,
  Swift…
- Fonksiyon, sınıf, dosya ve belge düğümlerinden; `calls`, `imports`, `uses`,
  `inherits`, `contains` gibi ilişkilerden oluşan bir **bilgi grafiği** kurar.
- Her ilişkiye bir güven etiketi koyar: `EXTRACTED` (kaynakta açıkça yazıyor),
  `INFERRED` (çıkarım), `AMBIGUOUS` (belirsiz).
- Yaklaşık on dilde (Swift, TS/JS, C++, C#, Java, ObjC, Kotlin, Ruby, Rust)
  metot çağrılarını alıcının tipine göre çözer. Python'da yalnızca
  `SınıfAdı.metot()` çağrılarını çözer.
- Topluluk (modül kümesi) tespiti, rapor ve HTML görselleştirme üretir.
- Soruya göre grafikten bağlam çıkarır (`query`): her düğüm ve kenar için
  dosya ve satır konumu verir, yaklaşık 2.000 token'lık esnek bir bütçe
  kullanır. İki düğüm arası yol bulur (`path`). Etki analizi (`affected`)
  yapar. ADR/RFC atıflarını düğüm yapar. Değişen dosyaları artımlı yeniden
  işler.
- LLM ile belge/makale/görsel çıkarımı yapar. 20'den fazla ajan platformu için
  kurulum sunar (upstream README tablosunda 22).

Graphify'ın yapmadıkları: bir sorunun **cevabını** kanıtla doğrulamak, cevabı
saklamak, kod değişince cevabın eskidiğini anlamak, cevabı çürütmeye çalışmak,
test çalıştırarak doğrulamak, kullanıcı itirazını bir hipotez olarak
sınamak, soruyu yapılandırılmış bir plana çevirmek, referansları tam sürüme
sabitlemek. Verinoda'nın eklediği katman budur.

Kod hacmi (bu belgenin yazıldığı an, satır sayısı): Graphify'dan gelen motor
yaklaşık 70.900 satır Python; Verinoda'nın kendi katmanı yaklaşık 34.000
satır; ürün testleri yaklaşık 15.300 satır.

---

## 3. Bir soru nasıl yanıtlanıyor (adım adım, gerçek çıktıyla)

| Adım | Ne olur | Modül |
|---|---|---|
| 0 | Çalışma ağacı son taramadan beri değiştiyse indeks artımlı güncellenir. Dayandığı bir *öğe* (imza, gövde, isim bağlaması, belge bölümü) değişen iddialar `stale` olur. İndeksleme reddedilirse hata raporlanır, yeni snapshot kaydedilmez. | `workflow`, `claims` |
| 1 | Soru bir **soru planına** çevrilir: alt sorular, her birinin niyeti ve "ne zaman cevaplanmış sayılır" ölçütü (`done_when`), sorudaki kod adları (mention) ve referanslar. Planı ajan yazabilir; yazmazsa kurallarla taslak çıkarılır (Türkçe ve İngilizce). | `question_plan` |
| 2 | Plan denetlenir: şema, kimlikler, bağımlılıkların döngüsüz olması, her mention'ın mesajda birebir geçmesi, sürüm ifadelerinin düşmemesi. Her mention grafikte kanıtla eşleştirilir. Belirsizse en fazla 3 seçmeli soru üretilir; geçersiz plan işi durdurur. | `question_plan`, `lexicon`, `textnorm` |
| 3 | Her alt soru için kalıcı arama indeksinden sınırlı sayıda kod konumu getirilir; her konum neden seçildiğini söyler. Soruyla ilgisiz çıkan konumlar iddiaya dönüşmez, `unknown` + sonraki adım olur. | `search_index`, `retrieval` |
| 4 | Konum, ilişki, akış, yapılandırma, test, "neden", etki iddiaları üretilir. Her kanıtın iddiayı gerçekten söyleyip söylemediği mekanik olarak derecelendirilir (tam / kısmi / yok). | `analysis`, `entail` |
| 5 | Precise eki kuruluysa bir çağrının hangi tanıma gittiği jedi ile sorulur. İstenirse testler yalıtılmış kopyada çalıştırılır ya da çağrı izleyiciyle gözlenir. | `precise`, `experiments`, `runtime` |
| 6 | Her iddiaya karşıt kontrol uygulanır; zayıfsa durum ve güven düşer, asla yükselmez. | `critique` |
| 7 | Her alt soru `done_when` ölçütüne göre yargılanır: `met`, `met_with_inference`, `unmet`, `not_supported`, `blocked_by_clarification`. | `analysis` |

Döngünün süre, araç çağrısı ve bağlam bütçesi vardır (varsayılan 60 sn,
40 çağrı, ~6.000 token). İndeks yenileme ve test çalıştırmaları süre
bütçesine dahildir; test zaman aşımı kalan süreyle sınırlanır. Döndürülen her
iddia, `unknown`, adım ve karşıt kontrol kaydı, serileştirilmiş boyutuyla
(chars/4) bağlam bütçesinden düşülür. Bütçe biterse yeni iddia üretilmez;
atlanan ya da yarım kalan her alt soru adıyla birlikte `unknown` olarak
raporlanır.

**Gerçek çıktı.** `examples/orders_app`'in bir kopyasında, precise eki kurulu
bir kurulumla, 2026-09-23'te bu belge yazılırken alındı. Kısaltıldı:

```
$ verinoda plan draft "Sipariş API'den veritabanına nasıl ulaşıyor?"
  q1 [flow] Sipariş API'den veritabanına nasıl ulaşıyor?  mentions: m1, m2, m3
  m1 'Sipariş' -> order
  m3 'veritabanına' -> database

$ verinoda plan check plan-001.json
plan check: ready
  m1 'Sipariş': weak create_order_handler() at orders/api.py:16-21 (seed_dictionary, 0.45)
  m2 "API'den": linked api.py at orders/api.py:1-8 (identifier_parts, 0.75)
  m3 'veritabanına': weak 0001-sqlite-persistence.md at docs/adr/0001-sqlite-persistence.md:1-7 (seed_dictionary, 0.4)

$ verinoda analyze --plan plan-001.json
  q1 [met] flow: Sipariş API'den veritabanına nasıl ulaşıyor?  (11 claim(s))
[statically_verified 0.90] `create_order_handler()` calls `place_order()` (orders/api.py:18)
      supports:source_code:orders/api.py:18
      supports:static_resolution:orders/api.py:18 definitive

$ verinoda claim show clm_df443be8cd85
  [strong_inference 0.70] Data path create_order_handler() -> place_order() -> .save()
      reaches persistence (orm-write, sql-write) at orders/repository.py:15
  support: source_code orders/api.py:18      grade=full
  support: source_code orders/service.py:22  grade=partial
  ? at least one hop is INFERRED
```

İkinci iddia neden "doğrulandı" değil? `place_order()` içindeki `repo.save()`
çağrısında `repo` bir parametredir: çalışma anında hangi sınıfın (bir alt
sınıf ya da test dublörü) geleceğini kod tek başına söylemez. Bu adım
Graphify grafiğinde yoktu. Verinoda'nın Python alıcı-tipi geçişi onu
parametrenin tip açıklamasından (`repo: OrderRepository`) çözdü ve `INFERRED`
işaretledi. `orders/service.py:22` satırı "kısmi" derecelendi. jedi de bu
çağrı için `dynamic` dedi (`verinoda resolve-call orders/service.py:22
save`). Kurallar gereği tüm adımları "tam" derecelenmeyen bir akış
"doğrulandı" olamaz.

Aynı kopyada `orders/api.py`'nin en üstüne bir yorum satırı eklenip `update`
çalıştırıldığında bu iddia eskimedi. Geçmişine "1 file(s) changed, no
dependency of this claim did" yazıldı. İlişki iddiası (`create_order_handler()`
calls `place_order()`) da `statically_verified` kaldı; `verify` onun
alıntıladığı 18. satırı yeni yerinde, 19. satırda buldu (`moved`). Bu,
Bölüm 4.5'teki öğe düzeyinde eskimenin ve kanıt bağlamanın gerçek bir
örneğidir.

---

## 4. Bileşenler ve gerçek durumları

### 4.1 Arama motoru (`search_index`, `retrieval`) — Çalışıyor

- **Kalıcı pasaj indeksi:** `.verinoda/index/search.db`, tarama sırasında
  bir kez kurulur ve artımlı güncellenir. Birimler: semboller (yalnızca kendi
  satırları), modül düzeyi bloklar, Markdown bölümleri. Pasajlar 12 satırlık,
  6 satır kaydırmalı pencerelerdir. 3.779 satırlık tek bir fonksiyon tek bir
  kelime torbası olmaz, yüzlerce yarışan pasaja bölünür.
- **Sıralama:** BM25F (ad, yol ve gövde alanları). Soruda birebir yazılan
  tanımlayıcı tam puan alır. Kişiselleştirilmiş PageRank, soruyla aynı
  kelimeleri paylaşmayan çağıran/çağrılanları öne çıkarır. Grafik yakınlığı
  yalnızca sıralamayı etkiler, kanıt sayılmaz.
- **Modele giden çıktı düz metindir:** önce iskelet (imza, belge satırı,
  çağrı özeti, satır numaralarıyla), sonra en iyi eşleşen pasajlar. Kesilme
  her zaman belirtilir ve takip komutu verilir. JSON çıktısı programlar
  içindir (`--json`); karakter bütçesi döndürülen her şeyi sayar.
- **Kısaltmalar ve Türkçe:** `env` → environment, `db` → database gibi
  genişletmeler ve Türkçe ekler çıktı başlığında raporlanır, sessizce
  yapılmaz.
- Eski sürümdeki "her soruda her dosyayı tarama" ve 400 dosya sınırı kalktı.
  İndekslendikten sonra değişen dosyalar sorgu anında yeniden indekslenmez;
  çıktıda `stale_files` olarak bildirilir.

### 4.1a Veri dosyaları, oyun modları ve veri paketleri (`search_index`, `resources`) — Çalışıyor (yeni, 2026-09-23)

Bir Minecraft modunda mekaniklerin bir kısmı Java'da değil veri paketindedir:
Java kodu `mymod:wisp_death` fonksiyonunu çalıştırır, `config/mymod.yml`'yi
okur, modeli `assets/mymod/models/item/x.json` olan eşyayı kaydeder. Kod ile
veri birbirini yalnızca bu dizgilerle anar. Graphify (ve eski Verinoda) bu
dosyaları hiç okumuyordu.

- **Veri dosyaları aranabilir.** Grafın düğüm çıkarmadığı metin dosyaları
  (`.mcfunction`, JSON, YAML/TOML/INI yapılandırmaları, SQL, gölgelendirici,
  Gradle betikleri…) "veri" birimi olarak indekslenir; yapılandırmalar üst
  düzey bölümlere ayrılır. Dışarıda bırakılan dosyalar gerekçesiyle
  listelenir: ikili dosya, sır içerebilir (grafın kendi kuralı), `.graphifyignore`,
  bağımlılık/derleme çıktısı klasörü, üretilmiş çıktı (`results/`, `logs/`…),
  büyük üretilmiş JSON, küçültülmüş dosya. `doctor` bunları sayar; sorunun
  kelimeleri dışarıda kalan bir dosyanın adında geçiyorsa sorgu çıktısı bunu söyler.
- **Kaynak kimlikleri kodu ve veriyi bağlar** — yalnızca gerçekten paket ya
  da mod olan depolarda (`pack.mcmeta` ya da Fabric/Quilt/Forge/NeoForge
  manifesti): `ns:yol`, `#ns:etiket`, worldgen kimlikleri, `function ns:x`,
  `"item.ns.x"` çeviri anahtarları, `Identifier.of("ns", "x")`, tam varlık
  yolları ve bir kimlik kurucusuna ya da adı ne yüklediğini söyleyen bir
  yardımcıya (`fonksiyon(p, poz, "wisp_death")`) verilen çıplak adlar. Kayıt
  türünü bağlam seçer (`advancement revoke … only ns:x` bir başarımı,
  `"parent"` bir modeli adlandırır). Ad alanı varsayılan ya da türü satırda
  yazmayan bağlantı "çıkarım" diye işaretlenir; ondan kurulan iddia
  `strong_inference` olur.
- **Aynı içerikli kopyalar** (iki kez gönderilen veri paketi) bir kez sıralanır;
  diğerleri `same content:` satırında listelenir.
- **Java çağrıları:** Graphify'ın düşürdüğü çağrılar (depoda iki `Wisp` sınıfı
  varsa `Wisp.spawn(...)` tümden kayboluyordu; tipli değişkenler hiç
  izlenmiyordu) dosyanın import'ları ya da paketi sınıfı bağlıyorsa eklenir ve
  her çağrı yeri gibi derecelendirilir. İddia metni `Ritual.baslat()` calls
  `Wisp.spawn()` biçimindedir.
- **Referans ağaçları:** `verinoda setup --reference orijinal-eklenti/=orijinal,eklenti`
  özgün uygulamayı aranabilir tutar, ama soru "orijinal", "eklenti" ya da klasör
  adını anmadıkça 0,6 katsayıyla sıralar. `setup`, kod dosyalarının çoğu daha büyük bir klasördeki
  dosyalarla aynı adı taşıyan klasörleri (özgün eklenti, donmuş kopya) bulup bu komutu önerir;
  kendisi uygulamaz.
- **Depodan Türkçe adlar:** paralel dil dosyaları (`lang/en_us.json` +
  `lang/tr_tr.json`) sözlüğe "Fener Asası" = `lantern_staff` bilgisini öğretir.
- **Komut adları:** "`scan` komutu", "init ve scan komutları" gibi ifadeler
  `cmd_scan`, `scan_command`, `ScanCommand` gibi işleyicilere bağlanır.

### 4.2 Soru planları ve Türkçe desteği (`question_plan`, `textnorm`, `lexicon`) — Çalışıyor (plan revizyonu kısmi)

- **Soru planı** (`verinoda.question_plan/1`): kullanıcının mesajı olduğu
  gibi, iki dilde yeniden ifade edilmiş hedef, alt sorular (niyet ve
  `done_when`), mention'lar, referanslar, kısıtlar. Plan CLI'da
  `.verinoda/plans/` altında bir dosyadır. JSON komut satırına verilmez,
  çünkü Windows PowerShell 5.1 onu bozar.
- **Denetim deterministiktir ve ağsızdır:** şema; tekil kimlikler;
  bağımlılıkların döngüsüz olması; sınırlar (6 alt soru, 20 mention,
  10 referans); her mention'ın mesajda birebir geçmesi; sürüm ifadelerinin
  bir referansa bağlanması (`version_dropped`); "eski sürüm" gibi göreli
  ifadelerin soruya dönüşmesi.
- **Eşleştirme kanıt katmanlarıyla yapılır:** tam kimlik, yol,
  `Sınıf.metot`, etiket, katlanmış etiket, tanımlayıcı parçaları, bulanık
  eşleşme, repo sözlüğü, tohum sözlük, metin. Kodda hiçbir şeye uymayan aday
  reddedilir, arama tohumu olarak asla kullanılmaz. Sonuç `linked`, `weak`
  (iddialar belirsizlik taşır), `ambiguous` (en fazla 3 seçmeli soru) ya da
  `unlinked` (`unknown` + sonraki adım) olur.
- **Türkçe:** `İndirim` → `indirim` gibi uzunluk koruyan katlama;
  `pricing.py'deki` gibi kesme işaretli eklerin ayrılması; Türkçe durak
  kelimeler; depo kelime dağarcığıyla doğrulanan kök bulma (yoksa 5 harflik
  önek); ismin halleri → rol (ayrılma = kaynak, yönelme = hedef). "bunu",
  "it" gibi göndermeler önceki alt sorunun öznesine bağlanır.
- **Repo sözlüğü** (`lexicon.json`): docstring, yorum, metin sabiti ve
  belgelerdeki kelimeleri tanımlayıcı parçalarıyla istatistiksel olarak
  eşleştirir. Eşik Dunning G² ≥ 10,83; her eşleşme en fazla 3 `dosya:satır`
  örneği tutar. Yaklaşık 325 girdilik bir TR→EN **tohum sözlük** de
  (sipariş → order, veritabanı → database, indirim → discount) yalnızca
  İngilizce karşılığı depoda geçiyorsa kullanılır. Sözlük yalnızca **aday**
  üretir; kanıt olarak asla kullanılmaz.
- **Eksikler:** "beni yanlış anladın" türü bir geri bildirimin plan
  revizyonuna dönüşmesi yapılmadı (altyapı hazır). Eşleştirme eşikleri sabit;
  `config.json`'dan okunmuyor. Küçük depolarda repo sözlüğü seyrek
  (orders_app'te 1 çift), bu yüzden Türkçe eşleştirme orada çoğunlukla tohum
  sözlüğe dayanıyor. Türkçe teknik terimler ("önbellek kaydı anahtarı")
  kısmen kapsanıyor. Türkçe sonuçlar İngilizcenin gerisinde (Bölüm 7.3).

### 4.3 Referans çözücü (`references/`) — Çalışıyor (bazı parçalar kısmi)

Kullanıcının yazdığı her referans, kastedilen **tam sürüme** sabitlenir:
`verinoda resolve "<mesaj>"` ya da MCP `reference_resolve`.

- **Tanınan biçimler:** GitHub, GitLab, Codeberg ve Bitbucket URL şekilleri
  (compare, releases, archive, raw, PR, MR, issue, `#L100-L120` satır
  aralıkları), `owner/repo`, `paket==sürüm`, purl, Go modülleri, Maven,
  arXiv, DOI, SWHID. Metinden Türkçe ve İngilizce mention çıkarılır
  ("requests'in 2.31 sürümünde").
- **Öncelik merdiveni** (`pin.basis` olarak kaydedilir): referanstaki
  değişmez kimlik > metinde yazılan sürüm > açıkça "main/en son" isteği >
  URL'deki tag > projenin kullandığı sürüm > URL'deki dal > metindeki tarih >
  varsayılan dal (her zaman uyarıyla). **Adı verilen bir sürüm asla varsayılan
  dala düşmez;** bulunamazsa referans "çözülmedi" olur.
- **"Kullandığımız sürüm":** kilit dosyalarından (uv.lock, pylock,
  poetry.lock, Pipfile.lock, requirements, package-lock.json, Cargo.lock,
  go.mod/go.sum), projenin kendi sanal ortamının metadata'sından (proje kodu
  içe aktarılmadan) ve çalışma zamanı sabitlerinden okunur.
- **Uyuşmazlık kataloğu M1–M12** (metin sürümü ≠ URL, dosya o sürümde yok,
  tag/dal ad çakışması, sürüm bulunamadı…): her uyuşmazlık raporlanır, hiçbiri
  sessizce çözülmez. Belirsiz durumlar sabit TR/EN sorulara dönüşür.
- **Çevrimdışı öncelikli:** `off` / `cache` / `on` ağ modları, disk
  önbelleği, testler için kayıtlı kasetler. Kimlik bilgisi saklanmaz. Her
  çözüm yalnızca eklenen bir kayıt olarak saklanır.
- **Eksikler:** M7 (repo taşındı), M8, M12 ve M6 yalnızca dar durumlarda
  tetikleniyor. Paket → kaynak içerik eşleşmesi yalnızca PyPI sdist'lerinde
  var. PEP 740 kaynağı okunuyor ama sabitlemede kullanılmıyor ve imza
  doğrulanmıyor. GitHub kimlik doğrulaması ve token kovası yok. Uygulama
  adları, Software Heritage ve Wayback desteği yok.

### 4.4 Güven motoru: iddia, kanıt ve derecelendirme (`claims`, `evidence`, `entail`, `anchors`, `critique`, `store`) — Çalışıyor

Her iddia şunları taşır: metin (değiştirilemez), proje ve snapshot kimliği,
commit, tür (konum, ilişki, akış, yapılandırma, test, karar…), durum, güven,
destekleyen / çürüten / nitelendiren kanıtlar, belirsizlikler ve silinmeyen
bir geçmiş.

**Durumlar ve üst güven sınırları**

| Durum | Anlamı | Gereken kanıt | Güven üst sınırı |
|---|---|---|---|
| `experiment_verified` | Test/deney ile doğrulandı | İddiayı **tam** derecede söyleyen, geçmiş bir test/deney ya da gözlem kanıt grubu | 0,95 |
| `statically_verified` | Kaynakla doğrulandı | İddiayı tam derecede söyleyen, hâlâ tutan kaynak satırları ya da precise çözücünün kesin yanıtı | 0,90 |
| `observed` | Doğrudan gözlendi | Tam derecede, taze, doğrulayıcı herhangi bir kanıt grubu | 0,90 |
| `primary_source_verified` | Birincil kaynakla doğrulandı | Belge, karar kaydı, git geçmişi, sabitlenmiş referans kaynağı; karar/geçmiş için kayıt iddianın ona atfettiğini söylemeli | 0,85 |
| `strong_inference` | Güçlü çıkarım | İddiayla ilgili (en az kısmi) bir destek; grafik kenarı yeterli, arama sonucu/model özeti/kullanıcı beyanı değil | 0,70 |
| `weak_inference` | Zayıf çıkarım | Herhangi bir destek | 0,40 |
| `unknown` | Bilinmiyor | Kanıt yok | 0 |
| `contradicted` | Çürütüldü | En az bir **kesin** çürütme | 0,05 |
| `stale` | Eskidi | Yalnızca geçersizleştirme koyar | 0,30 |

**Değişmez kurallar**

- **Kanıt iddiayla ilgili olmalı; bu tek bir yerde denetlenir.** Bir
  "doğrulandı" durumu için kanıt grubu doğrulayıcı, taze ve **tam**
  derecelenmiş olmalıdır: kanıt iddiayı mekanik olarak söylemeli, sadece
  var olması yetmez. Örnekler: ilişki iddiası için alıntılanan satırda, iddia
  edilen çağıranın içinde, hedefe giden bir AST çağrısı (içe aktarma takma
  adları dahil); konum iddiası için tanımın tam olarak alıntılanan
  satırlarda başlayıp bitmesi. Saklanan her durum değişikliği bu denetimden
  geçer: API, `verify`, `feedback resolve`, deneyler, çalışma zamanı
  gözlemleri, MCP. İlgisiz bir kanıt (ilgisiz bir test çalıştırması,
  başka bir iddiaya ait kanıt kimliği) bir iddiayı artık yükseltemez. Bu,
  2026-09-23 denetiminde bulunan açığın kapatılmasıdır.
- **Grafik kenarı tek başına doğrulama sayılmaz.** Arama sonucu, model özeti
  ve kullanıcı beyanı çıkarım için bile destek sayılmaz; tek başlarına en
  fazla `weak_inference` verirler.
- **Kanıtı olmayan iddia `unknown`'dır** (eskiden `weak_inference` idi).
- **Kesin ve sezgisel çürütme ayrıdır.** Yalnızca belirtilen kapsamda
  eksiksiz bir denetim iddiayı `contradicted` yapar. Örnekler: satırda hedefe
  çağrı yok; satır iddia edilen çağıranın dışında; "yalnızca X" kalıbı başka
  yerde geçiyor; precise çözücü kesin olarak başka bir hedef gösteriyor.
  Sezgisel bir şüphe iddiayı bir basamak düşürür ve belirsizlik ekler.
- **Karşıt kontrol güveni asla yükseltmez.** Aynı iddiada tekrar
  çalıştırılınca aynı sonucu verir; `stale` ya da `contradicted` bir iddiayı
  geri getirmez (bunu `verify` yapar). Yeniden doğrulama, iddianın
  değerlendirildiği tavanın ve karşıt kontrolün kestiği cezanın üstüne
  çıkamaz.
- **Silme koruması:** SQLite tetikleyicileri iddia, kanıt, iddia–kanıt
  bağlantısı, snapshot, deney, analiz, araştırma, geri bildirim, hafıza,
  soru planı, referans çözümü ve çalışma zamanı kayıtlarının silinmesini
  reddeder. Geçmiş, bağlantılar, plan gövdeleri ve kanıt konumları
  değiştirilemez. Bir iddianın metni ve oluşturulma zamanı değiştirilemez
  (şema v4); düzeltme yeni bir iddiayla yapılır, eskisi `contradicted`
  olarak kalır ve yenisine bağlanır. Durum ve güven alanları elbette
  güncellenir; her değişiklik geçmişe yazılır. Türetilmiş önbellekler
  (`file_facts`, `resolutions`, `file_stat`) yeniden hesaplanabilir.

**Karşıt kontrol** (`challenge`) şunlara bakar: destek var mı; derecesi yeterli
mi; alıntılanan satırlar hâlâ aynı mı (yer değiştirdiyse bulunur); atıf
yapılan commit ve çalıştırma var mı; destek yalnızca grafik kenarı mı; çağrı
satırı gerçekten hedefi çağırıyor mu (AST); `INFERRED` bir çağrı aynı adlı
başka bir hedefe gidebilir mi; "yalnızca X" iddiası çiğneniyor mu; bağımlı
öğeler değişti mi. Bunlara karşı-hipotez yoklamaları eklenir. Örnek: bir
`pytest.raises` bloğunda, daha önce hata fırlatan bir çağrıdan sonraki kod
çalışamaz. LLM kullanılmaz.

**Kaynak öncelik sırası:** 1) projenin kaynak kodu → 2) testler, tekrar
üretilebilir çalıştırmalar ve statik çözücü yanıtı → 3) git geçmişi ve tasarım
belgeleri → 4) bağımlılığın kullanılan sürümündeki kaynak → 5) resmî belge ve
standart → 6) referans repo ve makale → 7) ikincil kaynak.

**Eksikler:** `general` türündeki (serbest metinli) iddialar terim örtüşmesiyle
derecelenir. Bu etiketli bir sezgiseldir; olumsuzluk ve niceleyici içeren
iddialar bu yolla tam derece alamaz. Karşıt kontrolün etiketli ölçüm kümesi
örneklem içidir (Bölüm 7.4) ve güven üst sınırları yeniden kalibre edilmedi.

### 4.5 Eskime takibi (`snapshot`, `anchors`, `claims`) — Çalışıyor

- **Snapshot:** commit, dal, kirli durum ve git'in izlediği ya da ignore
  etmediği her dosyanın SHA-256 özeti (`.venv`, `node_modules`, `build`,
  `dist` gibi dizinler hariç). Özetler dosya boyutu ve değişiklik zamanıyla
  önbelleğe alınır; yalnızca değişen dosyalar yeniden hash'lenir.
  Commit/dal/kirli bilgisi tek bir `git status` çağrısından gelir.
- **Öğe düzeyinde bağımlılık:** her iddia, türüne göre neye dayandığını
  kaydeder. Konum iddiası imzaya; ilişki iddiası çağıranın gövdesine, isim
  bağlamasına ve hedefin imzasına; akış iddiası her adımın gövdesine; "X'e
  hiçbir test ulaşmıyor" iddiası test kümesinin özetine dayanır. Bir değişiklik
  iddiayı yalnızca dayandığı bir öğe değiştiyse eskitir. Dosyası değişip
  öğeleri değişmeyen iddia yeni snapshot'a bağlanır ve geçmişe yazılır.
- **Kanıt bağlama (anchor):** alıntılanan satırlar sembole, modül ifadesine
  ya da belge bölümüne bağlanır. Kod yalnızca yer değiştirdiyse satırlar tam
  yeni yerinde bulunur (`moved`). Değiştiyse yeni konum yalnızca adaydır,
  doğrulama sayılmaz. Dosyada birden fazla geçen bir satır tahminle taşınmaz,
  `ambiguous` olur.
- **Sınırlar:** Dosya izleyici yoktur; eskime bir sonraki `update`, `scan`,
  `analyze` ya da `verify`'da işaretlenir. İlişki iddiaları çağıran
  fonksiyonun **tüm gövdesine** bağlıdır; aynı fonksiyonda başka bir yerin
  değişmesi doğru bir iddiayı da eskitebilir (geçmiş tekrarında ilişki
  iddialarında %21 gereksiz eskime; Bölüm 7.4). Python dışı dillerde isim
  bağlamaları kaba bir parmak iziyle izlenir. Tur-3'ten önce oluşturulmuş
  iddialar dosya düzeyi kuralla izlenmeye devam eder. İddianın dayanmadığı
  dosyalardaki değişiklikler (ör. yeni eklenen, iddiada geçmeyen bir dosya)
  denetimi tetiklemez.

### 4.6 Çalışma zamanı gözlemi (`runtime/`) — Çalışıyor (container yolu denenmedi)

`verinoda observe --for apply_discount` ya da `analyze --observe`: seçilen
pytest testleri yalıtılmış kopyada bir çağrı izleyicisiyle çalışır.
İzleyici Python 3.12+'da `sys.monitoring`'i, daha eskide `setprofile`'ı
kullanır. Hangi çağrı yerinin hangi fonksiyonu başlattığı, test başına
ulaşılan fonksiyonlar ve kütüphane/C sınır çağrıları kaydedilir. İz
dosyası sha256'sıyla, snapshot ve commit'e bağlı olarak saklanır.

- Bir gözlem "R çalıştırmasında, C commit'inde, L satırı B'yi başlattı"
  demektir. Asla "her zaman" iddiasını desteklemez. Test dublörleri
  üzerinden görülen çağrılar üretim kenarlarını desteklemez.
- "T testi X'e ulaşmadı" ancak eksiksiz bir izde söylenebilir; değilse sonuç
  "belirsiz"dir.
- Gerçek örnek (bu belge yazılırken çalıştırıldı): `orders_app` kopyasında
  `apply_discount`'a tam olarak 4 test ulaştı. `test_empty_order_rejected`
  ulaşmadı; oysa önceki statik analiz onun da ulaştığını söylüyordu.
- **Sınırlar:** Yalnızca Python/pytest. Testler projenin kendi `.venv`'iyle
  (yoksa Verinoda'nın yorumlayıcısıyla) çalışır ve orada pytest kurulu
  olmalıdır. Kurulu değilse sonuç "belirsiz" olur (bu belge yazılırken
  denendi). Alt süreçler izlenmez. Ek yük hedeflenenden yüksek (Bölüm 7.4).
  `setprofile` yedeği yalnızca Python 3.12'de zorlanarak denendi. Container
  yolu gerçek docker/podman ile hiç çalıştırılmadı.

### 4.7 Kesin çağrı çözümlemesi (`precise`, `scip_reader`) — Çalışıyor (isteğe bağlı)

- `pip install "<wheel>[precise]"` ile jedi eklenir. Analiz, iddiaya giren
  çağrı yerlerini tembel biçimde çözer: analiz başına en fazla 20 yer ya da
  1,5 saniye; sonuçlar dosya özetiyle önbelleğe alınır. Bütçe yüzünden
  çözülemeyen yer "not resolved (budget)" belirsizliğini taşır.
- Yanıt türleri: `definitive` (tek bir tanım), `dynamic` (parametre ya da
  yerel değişken üzerinden, çalışma anı belirler), `ambiguous`, `external`,
  `unresolved`. **Yalnızca kesin yanıt doğrular ya da çürütür.** Parametre
  üzerinden metot çağrısı her zaman `dynamic` sayılır; bu, araştırmadaki
  prototipten daha katıdır.
- Kurulu değilse her sorgu "precise yanıt yok" der. Alıcı tipine bağlı metot
  çağrısı iddiaları o zaman `strong_inference`'ta kalır.
- **SCIP:** kullanıcının kendi ürettiği bir `index.scip` bağımlılıksız bir
  çözücüyle okunur (`scan --scip FILE`). Yalnızca Python dışı dosyalar için
  kullanılır, çünkü SCIP sembolleri ada dayalıdır ve aynı adlı iki tanımı
  ayıramaz. Dosya başına tazelik izlenir; indeksten sonra değişen dosyanın
  SCIP verisi kullanılmaz.

### 4.8 Mimari harita (`architecture_map`) — Çalışıyor

Her görünüm hangi yöntemle üretildiğini ve **neyi göremediğini** yazar:

| Görünüm | Ne gösterir | Bilinen sınır |
|---|---|---|
| Hiyerarşi | repo → alt sistem → paket → dosya → sembol | paket = dizin; dil düzeyi modül değil |
| Bağımlılıklar | dosya düzeyinde çağrı, import, kullanım ve kalıtım ilişkileri (paket düzeyi yalnızca API'de) | dinamik çağrı, reflection, DI çözülmez |
| Veri akışı | giriş noktasından kalıcı veriye (SQL, ORM, dosya yazımı) çağrı yolları | giriş ve kalıcılık tespiti sezgisel; değer takibi (taint) yok |
| Yapılandırma | okunan ortam değişkenleri ve yapılandırma dosyaları | dolaylı okumalar (settings nesneleri) izlenmez |
| Testler | hangi test hangi kodu statik olarak çağırıyor (varsa `coverage.xml`) | statik erişim ≠ çalışma zamanı; bunun için `observe` |
| Tarihçe | son commit'ler, değişim sıklığı, ADR/karar belgeleri | bağlantılar metinsel eşleşmedir |
| Etki | bir değişiklikten etkilenebilecek semboller, dosyalar, testler | "etkilenebilir" demektir, "bozuldu" değil |

`trace` iki sembol arasındaki yönlü yolları verir. `flow` modu `calls`
kenarlarını (Graphify'ın ve Verinoda'nın `INFERRED` çağrıları dahil) ve
sınıf oluşturma → `__init__` adımını izler; diğer içerme ilişkilerini akış
saymaz.

Verinoda'nın Python alıcı-tipi geçişi basit durumları kapsar: düz sınıf adıyla
işaretlenmiş parametreler ve `x = Sınıf()` atamaları. `Optional[X]`,
`modül.Sınıf` ve `self` alanları çözülmez; aynı adlı sınıflar karışabilir.
Diğer dillerde yalnızca Graphify motorunun kendi çözücüleri çalışır.

### 4.9 Yalıtılmış deneyler (`experiments`) — Kısmi (yalnızca süreç yalıtımı denendi)

Her deneyin hipotezi, komutu, ortamı, zaman sınırı, çıkış kodu, özeti ve
oluşturduğu kanıt kaydedilir.

- Ayrı süreç grubu; zaman aşımında tüm süreç ağacı öldürülür.
- Çalışma dizini reponun geçici bir **kopyasıdır**: yalnızca izlenen ve
  ignore edilmemiş dosyalar kopyalanır; `.venv`, `node_modules`, `build`,
  `dist` dahil edilmez.
- Ortam değişkenleri izin listesinden yeniden kurulur; API anahtarları ve
  token'lar alt sürece geçmez.
- İzin listesindeki test komutlarının (pytest, go test, cargo test, npm
  test…) **yol argümanları** kopyanın içinde kalmak zorundadır. Mutlak yollar,
  ev dizini yolları, URL'ler ve `..` kaçışları reddedilir; kod çalıştıran
  seçenekler (`pytest --pyargs`, `node -e`…) de reddedilir. Red nedeni
  argümanın adıyla söylenir.
- **Önemli sınır:** Container olmadan ağ ve dosya sistemi yalıtımı yoktur.
  İzin verilen test komutları projenin kendi kodunu (testler, `conftest.py`,
  npm betikleri) çalıştırır; bu kod ağa erişebilir ve mutlak yollarla kopya
  dışına okuyup yazabilir. Her sonuç bunu açıkça belirtir (`guarantees`).
  Güvenilmeyen bir depoda testleri çalıştırmayın; `experiment run
  --isolation container` docker/podman ister ve henüz gerçek bir container
  ile denenmedi. İzin listesi dışındaki komutlar container yoksa
  **reddedilir**.
- pytest "karar veremezse" (toplama hatası, test bulunamadı, pytest kurulu
  değil) sonuç `inconclusive` olur; bu kanıt iddiayı yalnızca nitelendirir,
  desteklemez ya da çürütmez.
- **Denenmemiş:** Linux/macOS'taki CPU (zaman aşımı + 5 sn), sanal bellek
  (2 GB) ve dosya boyutu (256 MB) sınırları kodda var ama hiç çalıştırılmadı.
  2 GB sanal bellek sınırı Node.js/JVM gibi çalışma zamanlarını bozabilir.
  Container yolu (`--network none`, adıyla durdurma) gerçek docker/podman ile
  hiç denenmedi; kullanılan `python:3.12-slim` imajında test araçları yoktur.
  `auto` modunda izin listesindeki testler, container olsa bile süreç
  yalıtımında çalışır.

### 4.10 Referans araştırması, karşılaştırma, kullanıcı eleştirisi (`research`, `compare`, `feedback`) — Çalışıyor (sınırlarla)

- **Araştırma:** referans repo tam commit SHA'sına sabitlenir ve commit başına
  ayrı bir çalışma ağacında incelenir. `verinoda resolve`'un seçtiği sabit
  (`--resolution <id> --reference-id rN`) olduğu gibi kullanılır. Çözülemeyen
  bir sürüm hata verir; varsayılan dala sessizce düşülmez. Mekanizma izi
  (veri yapıları, hata yönetimi, eşzamanlılık, ortam varsayımları, "neden")
  örüntü tabanlıdır ve sezgisel olarak etiketlenir. Bir şeyin bulunmaması
  "izlenen alt grafikte bulunamadı" demektir, "yok" demek değildir.
- **Karşılaştırma** (`compare`): yerel ve referans varsayımlarını kategori
  kategori ayırır; bilinmeyenleri açıkça yazar.
- **Kullanıcı eleştirisi:** itiraz doğru kabul edilmez, bir hipotez olarak
  sınanır. Önce referanslar çözülür; çelişen ya da çözülmeyen bir referans
  varsa karar `unresolved` olur. Ardından 9 adımlı protokol işler: önceki
  iddia, referansın sabit sürümde incelenmesi, mekanizma, varsayım
  karşılaştırması, yeniden sınama. Sonuç `confirmed`, `qualified`,
  `corrected` ya da `unresolved` olur. Düzeltmede eski iddia silinmez. Elle
  verilen `confirmed`/`qualified` kararı yalnızca iddiaya zaten bağlı
  kanıtları kabul eder ve iddiayı yükseltmez.

### 4.11 Sürümlü hafıza (`memory`) — Çalışıyor (yalnızca elle)

Bilgiler yalnızca `verinoda memory learn` ile elle, anahtar/değer olarak ve
sürümlü kaydedilir; analiz bunları otomatik yazmaz. `--claim` ile bir iddiaya
bağlanan bilgi, o iddia `stale` ya da `contradicted` olunca geçersizleşir
(silinmez). İddia sonradan yeniden doğrulansa da bilgi kendiliğinden geri
gelmez. İddiaya bağlanmayan bilgi hiç geçersizleşmez.

### 4.12 Ajan entegrasyonu ve MCP (`agents`, `mcp`) — Çalışıyor (yeni protokoller gerçek ajanda denenmedi)

- **Claude Code:** `.claude/skills/verinoda/SKILL.md` (proje ya da kullanıcı
  dizini) ve isteğe bağlı MCP kaydı; `/verinoda <soru>` ile çağrılır.
- **Codex:** skill `.agents/skills/verinoda/SKILL.md`'ye kurulur ve
  `$verinoda` ile çağrılır. Codex'te `/verinoda` komutu yoktur. Windows'ta
  Codex sandbox'ı için kurulum `uv tool install --link-mode copy` ile
  yapılmalıdır (Bölüm 10).
- **Skill protokolleri:** (1) Mesajda bağlantı, repo/paket adı, sürüm,
  commit, PR, makale varsa önce `verinoda resolve`. (2) Önce soruyu anla:
  plan taslağı → düzenleme (bileşik soruyu böl, alan kelimelerini açıkla,
  sürümleri yazıldığı gibi kopyala, aday uydurma) → denetim → yalnızca dönen
  netleştirme sorularını sor → planla analiz. (3) Cevap "Understood as /
  Anladığım: …" ile başlar; ardından her alt soru için kararı, iddiaları ve
  bilinmeyenleri gelir.
- **MCP:** 23 araç, aynı çekirdek fonksiyonları çağırır. Yanıtlar
  varsayılan 12.000 karakterle sınırlıdır. Uzun yaşayan sunucu grafiği,
  sözlüğü ve jedi projesini bellekte tutar.
- Kurulum tekrar çalıştırılabilir, başka araçların ayarlarını ezmez ve yaptığı
  her değişikliği bir manifestoya yazar; kaldırma yalnızca kendi girdilerini
  siler.

---

## 5. Graphify'a göre yükseltmeler

| Yetenek | Graphify | Verinoda | Durum |
|---|---|---|---|
| Çok dilli AST çıkarımı, bilgi grafiği | var (~40 dil) | aynı motor | Çalışıyor |
| Soruya göre bağlam | düğüm/kenar listesi (dosya + tek satır konumu, ~2.000 token'lık esnek bütçe) | kalıcı pasaj indeksi (BM25F + PageRank), seçilme gerekçesi, satır aralığı ve kaynak kesiti olan düz metin | Çalışıyor |
| Soru planı, alt sorular, Türkçe soru | — | plan sözleşmesi, denetim, kanıtla eşleştirme, TR/EN kurallar, repo sözlüğü | Çalışıyor (plan revizyonu yok) |
| Referansları tam sürüme sabitleme | — | öncelik merdiveni, M1–M12 uyuşmazlıkları, kilit dosyaları, çevrimdışı öncelikli | Çalışıyor (bazı parçalar kısmi) |
| Sonuçları iddia olarak saklama | — | SQLite, durumlar, silinmeyen geçmiş, değiştirilemez iddia metni | Çalışıyor |
| Kanıt kuralları | güven etiketi (EXTRACTED/INFERRED) | etiket + kanıt derecelendirmesi (tam/kısmi/yok) + merkezi durum denetimi | Çalışıyor |
| Kod değişince eskime | grafik yeniden kurulur | öğe düzeyinde eskime, kanıt bağlama, `verify` ile yeniden bağlama | Çalışıyor |
| Karşıt kontrol | — | kesin/sezgisel çürütme, karşı-hipotez yoklamaları; güveni asla yükseltmez | Çalışıyor |
| Test/deney ile doğrulama | — | yalıtılmış çalıştırma → `experiment_verified` | Kısmi (yalnızca süreç yalıtımı denendi) |
| Çalışma zamanı gözlemi | — | pytest çağrı izleyicisi, test başına erişim | Çalışıyor (Python/pytest) |
| Hangi tanıma gidiyor (kesin çözümleme) | — | jedi (isteğe bağlı), SCIP okuyucu | Çalışıyor (isteğe bağlı) |
| Yönlü akış izi | en kısa yol (`path`) | yalnızca çağrı ilişkisiyle akış modu, her adımda konum | Çalışıyor |
| Tip bilgisinden metot çağrısı çözme | Swift, TS/JS, C++, C#, Java, ObjC, Kotlin, Ruby, Rust alıcı tipleri; Python'da yalnızca `SınıfAdı.metot()` | ek geçiş: Python'da tip açıklamalı parametre ve `x = Sınıf()` alıcıları; `INFERRED` + kaynağı işaretli | Çalışıyor |
| Mimari görünümler | topluluklar, rapor, `affected` etki analizi, gerekçe/ADR düğümleri | 7 görünüm (veri akışı, yapılandırma ve test görünümleri yeni), her biri sınırlarını yazar | Çalışıyor |
| Bütçeli analiz döngüsü ve `unknown` | — | süre, çağrı ve bağlam bütçesi; döndürülen her şey bütçeden düşülür | Çalışıyor |
| Referans repo incelemesi ve karşılaştırma | — | tam SHA'da inceleme, mekanizma izi, varsayım farkı | Çalışıyor (sezgisel iz) |
| Kullanıcı eleştirisi protokolü | — | 9 adım, 4 sonuç, önce referans çözümü, geçmiş korunur | Çalışıyor |
| Sürümlü hafıza | — | iddiaya bağlanırsa eskiyince geçersiz | Çalışıyor (yalnızca elle) |
| Ajan kurulumu | 20'den fazla platform | Claude Code + Codex, manifest, güvenli kaldırma | Çalışıyor |
| MCP araçları | grafik araçları | 23 araç: plan, referans, iddia, kanıt, gözlem, doğrulama, eleştiri | Çalışıyor |
| Karşılaştırmalı benchmark | kendi yayınladığı doğruluk, maliyet ve token rakamları (Verinoda'ya aktarılmaz) | ham arama vs Graphify vs Verinoda; eskime ve karşıt kontrol ölçüm düzenekleri | Çalışıyor (model döngüde değil) |
| LLM ile belge/görsel çıkarımı | var | analizde kullanılmıyor; `verinoda index -- extract` geçişiyle, desteklenmeden erişilebilir | daraltıldı |
| Claude Code ve Codex dışındaki ~20 ajan platformu | var | engellendi: `verinoda index -- install` vb. gerçek bir Graphify kurulumunu bozmasın diye reddedilir | daraltıldı |

---

## 6. Teorik tasarruf ve maliyet modeli (ölçüm değil)

> **Bu bölüm teoriktir.** Aşağıdaki oranlar ölçüm değildir; açıkça yazılmış
> varsayımlarla yapılmış **senaryo hesaplarıdır**. Graphify'ın kendi
> yayınladığı tasarruf rakamları Verinoda'ya aktarılmaz. Ölçülen sonuçlar
> Bölüm 7'de ve `docs/BENCHMARKS.md`'dedir. İki bölümün sayıları doğrudan
> karşılaştırılamaz (Bölüm 9).

### 6.1 Tasarrufun kaynakları (mekanizma olarak)

1. **Okunan bağlamın sınırlanması.** Haritası olmayan bir ajan, cevabı bulmak
   için dosyaları bütün olarak okur. Verinoda ilgili sembollerin iskeletini,
   eşleşen pasajlarını ve kanıt konumlarını bir bütçe içinde verir.
2. **Yeniden taramanın önlenmesi.** İndeks bir kez kurulur ve artımlı
   güncellenir; sorgu anında dosya taranmaz. Bu, modelin değil aracın
   maliyetini düşürür.
3. **Kayıt tekrarının önlenmesi (token tasarrufu değil).** Aynı snapshot'ta
   aynı metinli iddia için yeni kayıt açılmaz, mevcut kayıt kullanılır. Ancak
   `analyze` her çağrıda getirme, doğrulama ve karşıt kontrolü yeniden yapar
   ve çıktının tamamını yeniden verir; bu, modele giden token'ı azaltmaz.
4. **Hatalı cevap turlarının azalması (varsayım).** Kanıtsız iddia
   "doğrulandı" diye sunulmadığı ve eskiyen iddialar işaretlendiği için,
   yanlış bir cevaba dayanıp sonra düzeltmeye harcanan model turları
   azalabilir. Bu etki ölçülmedi. Benchmark'ın `--llm anthropic` modu da onu
   ölçmez, çünkü soru başına tek bir model çağrısı yapar; çok turlu bir ajan
   döngüsüyle ayrı bir deney gerekir.

### 6.2 Soru başına bağlam maliyeti: formül

Tanımlar:

- `F` = ajanın haritasız okuyacağı dosya sayısı
- `S` = bu dosyaların ortalama boyutu (token)
- `B_q` = Verinoda `query` çıktısının karakter sınırı: varsayılan 6.000
  karakter ≈ 6.000 ÷ 4 = **1.500 token** (chars/4 tahmini)
- `B_a` = Verinoda `analyze` bağlam bütçesi: varsayılan **~6.000 token**.
  Döndürülen her iddia, `unknown`, adım ve karşıt kontrol kaydı bu bütçeden
  düşüldüğü için yaklaşık bir üst sınırdır (zarf alanları hariç).

Yaklaşık maliyetler:

- Ham okuma ≈ `F × S` (+ arama çıktısı)
- Graphify sorgusu ≈ Graphify'ın `query` bütçesi: varsayılan 2.000 token,
  ama Graphify bunu karakter/3 ile hesaplar (≈ 6.000 karakter; chars/4 ile
  ≈ 1.500 token). Bu kesin bir tavan değildir: tüm düğümler sığıyorsa
  kenarlar kesilmez ve çıktı bütçeyi aşabilir.
- Verinoda ≈ yalnızca `query` okunursa en fazla `B_q`; yalnızca `analyze`
  okunursa en fazla yaklaşık `B_a`. Aşağıdaki senaryolarda Verinoda maliyeti
  bu iki sınırın arasında (1.500–6.000 token) kabul edilir.

**Teorik oran = `(F × S) ÷ Verinoda maliyeti`**

Oturum boyunca `Q` soru sorulursa ham okuma ≈ `Q × F × S`, Verinoda ≈
`Q × (1.500 ile 6.000 arası)` olur. İndeksleme modele token olarak gitmez;
bir kez yapılan bir araç maliyetidir (süresi Bölüm 7'de). Soru sayısı oranı
değiştirmez, yalnızca farkın mutlak büyüklüğünü artırır.

### 6.3 Senaryo hesapları (varsayım — ölçüm değil)

| Senaryo | Varsayım | Ham okuma | Verinoda | Teorik sonuç (hesabıyla) |
|---|---|---|---|---|
| Çok küçük proje | Örnek uygulama: 11 dosya, 4.726 bayt; tamamı okunur | 4.726 ÷ 4 ≈ **1.182 token** | Yalnızca `analyze`: ölçülen ortalama 821 token/soru (`docs/BENCHMARKS.md`, tur-2 kodu). `query` de okunursa: 821 + 1.287 = 2.108 token | Yalnızca `analyze`: 1.182 ÷ 821 ≈ **1,4 kat** daha az bağlam. İkisi birden: 1.182 ÷ 2.108 ≈ 0,56, yani Verinoda **yaklaşık 1,8 kat daha pahalı**. Sonuç: tasarruf ya çok küçük ya da hiç yok |
| Orta proje | 8 dosya × 3.000 token | 8 × 3.000 = **24.000 token** | 1.500–6.000 token | 24.000 ÷ 6.000 = **4** ile 24.000 ÷ 1.500 = **16** kat arası daha az bağlam |
| Büyük proje | 15 dosya × 5.000 token | 15 × 5.000 = **75.000 token** | 1.500–6.000 token | 75.000 ÷ 6.000 = **12,5** ile 75.000 ÷ 1.500 = **50** kat arası daha az bağlam |
| Oturum (orta proje, 10 soru) | Her soruda 8 dosya yeniden okunur | 10 × 24.000 = **240.000 token** | 10 × (1.500–6.000) = 15.000–60.000 token | 240.000 ÷ 60.000 = **4** ile 240.000 ÷ 15.000 = **16** kat arası (oran soru başınakiyle aynı) |

Senaryoların yorumu:

- Oran, repo büyüklüğüne ve sorunun kaç dosyaya yayıldığına bağlıdır. Küçük
  projede anlamlı bir kazanç yoktur.
- Bu oranlar yalnızca **modele giden bağlamı** karşılaştırır, **cevap
  kalitesini karşılaştırmaz.** Bütçe kesilirse bazı bilgiler eksik kalabilir;
  Verinoda bunu `unknown` ve "kesildi" diye bildirir.
- Graphify'a göre beklenti: Graphify sorgusunun bütçesi de yaklaşık 1.500
  token (chars/4) olduğu için, Verinoda'nın Graphify'a göre asıl farkı token
  değil **isabet ve güvenilirliktir**. Ölçülen isabet farkı Bölüm 7'dedir.
- Orta ve büyük projelerde ham okumaya göre tasarruf beklenir; bu ölçülmedi.
  Küçük projede beklenmez.

Bölüm 6.3'teki açıkça "senaryo" diye etiketlenmiş teorik katsayılar dışında
bu belgede ölçülmemiş hiçbir kat/yüzde tasarruf iddiası yoktur; senaryo
katsayıları hiçbir yerde ölçüm sonucu gibi sunulmaz.

---

## 7. Ölçülen sonuçlar

> **En güncel sayılar için `docs/BENCHMARKS.md`'ye bakın.** Bu bölüm yalnızca
> (a) bu belge yazılırken `docs/BENCHMARKS.md`'de bulunan sayıları ve
> (b) tur-3 ekip raporlarındaki ölçümleri aktarır. (b)'nin her biri kendi
> çekincesiyle verilir: örneklem içi (tasarım o sorular görüldükten sonra
> yapıldı) ya da ayrılmış küme (hiçbir ayar için kullanılmadı). Tek makine
> (Windows 11, 6 mantıksal çekirdek, Python 3.12.0), model döngüde değil:
> ölçülen şey altın bilgilerin modele teslim edilen bağlamda **bulunup
> bulunmadığıdır**, cevap doğruluğu değildir. Token'lar chars/4 tahminidir.

### 7.1 Karşılaştırmalı benchmark (`docs/BENCHMARKS.md`, tur-2 kodu)

Bu belge yazılırken `docs/BENCHMARKS.md`'deki yayımlanmış çalışma
2026-09-22'de, **tur-3 öncesi kodla** (commit `05890a1` + tur-2 düzeltmeleri)
yapılmıştı. Yani arama motoru, soru planları, güven motoru ve diğer tur-3
bileşenleri bu tabloda **yok**. Tur-3 kodunun ölçümü sürüyor; sonuçlar
geldiğinde `docs/BENCHMARKS.md` güncellenir.

| Küme | Verinoda analyze | Verinoda retrieve | Graphify (gömülü) | Graphify (CLI) | Ham okuma |
|---|---|---|---|---|---|
| Graphify'ın kendi kodu (226 dosya, 37 bilgi, 9 soru) | 18/37, 767 tok/soru, 1,89 sn | 18/37, 1.433 tok/soru, 1,37 sn | 7/37, 1.667 tok/soru, 0,26 sn | 7/37, 1.940 tok/soru, 0,60 sn | 4/37, 5.989 tok/soru, 0,29 sn |
| Örnek uygulama (11 dosya, 32 bilgi, 10 soru) | 32/32, 821 tok/soru, 0,24 sn | 30/32, 1.287 tok/soru, 0,007 sn | 16/32, 2.234 tok/soru, 0,004 sn | 15/32, 1.651 tok/soru, 0,37 sn | 31/32, 1.224 tok/soru, 0,016 sn |

(Süreler soğuk çalıştırmanın soru başı medyanıdır.)

Yorum (yalnızca bu tablodan):

- **Büyük kod tabanında isabet:** Verinoda analyze 18/37, Graphify 7/37, ham
  okuma 4/37. Ham okumaya göre analyze 5.989 ÷ 767 ≈ **7,8 kat daha az
  token** ile 4 yerine 18 bilgi getirdi. Hiçbir yaklaşım bilgilerin yarısına
  ulaşmadı.
- **Hız: Verinoda daha yavaş.** Soru başına 1,4–1,9 sn; Graphify 0,26–0,60
  sn, ham okuma 0,29 sn.
- **Küçük projede ham okuma neredeyse aynı iyi** (31/32) ve çok daha hızlı
  (0,016 sn'ye karşı 0,24 sn). analyze burada daha az token kullandı (821'e
  karşı 1.224), retrieve ise biraz daha fazla (1.287).
- **Graphify'ın daha iyi olduğu soru:** g04'te (önbellek anahtarı) Graphify
  4/4, Verinoda analyze ve retrieve 2/4. Cevap adlandırılmış tanımlardan
  oluştuğunda Graphify güçlüdür.
- **Yanlış ifade:** Örnek uygulamada Verinoda bir bilinen-yanlış ifadeyi
  ("`test_empty_order_rejected` indirim hesabını çalıştırır") 2 iddiada
  `strong_inference` olarak sundu. Graphify'ın kendi kodunda hiçbir
  yaklaşım bilinen-yanlış ifade söylemedi.
- **İndeksleme (tek seferlik):** Verinoda `scan` 6,72 sn (soğuk), Graphify
  `update` 7,69 sn. Tur-3'te `scan` ek işler yapıyor (arama indeksi, sözlük,
  sembol özetleri); tur-3 sonrası toplam `scan` süresi henüz bu düzenekle
  ölçülmedi.

### 7.2 Arama motoru (tur-3 ekip raporu; benchmark düzeneği değil)

| Ölçüm | Sonuç | Çekince |
|---|---|---|
| Graphify'ın kendi kodu, düz metin çıktı | 35/37 bilgi, 1.422 tok/soru (önce 18/37) | **örneklem içi**: tasarım bu soruların kaçırılanları görüldükten sonra yapıldı |
| Ayrılmış küme (33 bilgi), düz metin | 26/33, 1.413 tok/soru; aynı kümede Graphify 17/33, 1.659 tok/soru | ayar için kullanılmadı; küçük ve yalnızca Python. Bir kök bulma düzeltmesi bu kümede 1 bilgi kaybettirdi (27 → 26) |
| Aynı kümeler, JSON çıktı (yayımlanmış benchmark'taki `retrieve` sütununun biçimi) | Graphify kodu 27/37, ayrılmış küme 19/33, örnek uygulama 31/32 | JSON'daki çağrı listesi konum sayılmadığı için düz metinden düşük |
| Sorgu süresi (Graphify kodu) | sıcak medyan 30–34 ms (önce 251 ms); yeni süreçte CLI 0,59 sn | Graphify CLI 0,60 sn ile aynı aralıkta; iki ölçüm farklı düzeneklerle yapıldı |
| Grafik yükleme (Graphify kodu) | 0,06 sn (önce ~2,0 sn: 1,95 sn alıcı-çağrı hesabı + 0,05 sn yükleme; hesap tarama zamanına taşındı) | tek makine |
| Tek dosya değişikliği sonrası `update` | 6,25 sn → 4,24 sn (yol kimliği önbelleği) | tek ölçüm |
| Arama indeksi kurulumu (Graphify kodu) | 3,0–3,6 sn, 13 MB; değişiklik yoksa 0,08 sn | `scan`'e eklenen maliyet |

### 7.3 Soru anlama ve Türkçe (tur-3 ekip raporu)

- Örnek uygulamadaki 4 Türkçe soruda doğru sembol, önce **hiç getirilmiyordu
  (0/4)**; şimdi 4/4'ünde ilk 4 sıradadır.
- Benchmark puanlayıcısıyla Türkçe bilgi bulma (analyze, yalnızca bu ekibin
  değişiklikleriyle): örnek uygulama 18/32 → **30/32** (İngilizce 32/32);
  Graphify kodu 6/37 → **12/37** (İngilizce 19/37).
- Tüm tur-3 değişiklikleri birlikteyken (bilgi amaçlı): örnek uygulama
  İngilizce 31/32, Türkçe 30/32; Graphify kodu İngilizce 26/37, **Türkçe
  13/37**. Türkçe hâlâ İngilizcenin gerisinde.
- **Çekince:** Türkçe soru setleri ve niyet/bölümleme altın tablosu kuralları
  yazan kişi tarafından yazıldı. Niyet ve bölümlemedeki 36/36 skoru
  **örneklem içidir**, bağımsız bir ölçüm değildir.
- Süre (Graphify kodu): tam `analyze` medyanı 0,84 sn, en fazla 2,10 sn.
  Graphify'ın CLI sorgusundan (0,60 sn) yavaş. Sözlük kurulumu 3,3 sn
  (soğuk), 3,6 MB.

### 7.4 Güven motoru, çalışma zamanı, kesin çözümleme, referanslar (tur-3 ekip raporları)

| Ölçüm | Sonuç | Çekince |
|---|---|---|
| Eskime: Graphify'ın son 300 commit'i, 26.097 iddia | eskiyen iddiaları yakalama oranı 1,0; "doğrulandı" görünüp sessizce yanlış olan iddia 0; gereksiz eskime %100 → %12,2; taşınan 10.259 alıntının 10.259'u doğru yere taşındı | ilişki iddialarında gereksiz eskime hâlâ %21; değişmeyen dosyalardan gelen ilişkiler örneklenmedi |
| Mutasyon takımı | 38/38 beklenen karar | küçük, elle yazılmış |
| Karşıt kontrol, 45 etiketli iddia | "doğrulandı" sunulan 20 iddianın 20'si doğru; 22 yanlış iddianın 22'si işaretlendi (17'si çürütüldü); 23 doğru iddianın hiçbiri düşürülmedi; benchmark'ın 10 bilinen-yanlışının hiçbiri doğrulanmadı | **örneklem içi**: yoklamalar kaçırılanlar görüldükten sonra eklendi; ayrılmış küme yok |
| Çalışma zamanı: örnek uygulama | `apply_discount`'a tam olarak 4 altın test ulaştı, `test_empty_order_rejected` ulaşmadı | bu belge yazılırken de yeniden üretildi |
| İzleyici ek yükü (533 Graphify testi) | CPU 1,39 kat (medyan), duvar saati 1,31 kat; `setprofile` yedeği ~4,5 kat | araştırmadaki 1,23 kat hedefine ulaşılmadı |
| Kesin çözümleme: Graphify'ın 2.798 çağrı kenarı | 2.769 doğrulandı, 16 çürütüldü (13 gölgelenmiş yinelenen tanım + 3 takma ad çakışması; araştırmadaki kümenin aynısı), 13 belirsiz | tüm yerler soğuk 17,8 sn; yeni süreçte ilk yer ~0,8 sn |
| Referans URL şekilleri (93) | sürüm adı geçen 21 URL'yi eski ayrıştırıcı sessizce varsayılan dalın son haline sabitliyor ya da tarihsiz bir sayfa çekiyordu; şimdi 0. Reddedilen 21 girdi → 0. `#L` satır aralıkları 0/7 → 7/7 korunuyor | tablo ekip tarafından hazırlandı, bağımsız değil |
| Referans soru korpusu (56 TR/EN soru) | örneklem içi 44 soruda %100; ayrılmış 12 sorunun ilk körlemesine çalıştırmasında 11/12 | ıskalanan durum sonradan düzeltildi, o küme artık kör değil; hedef ≥ 60 soruydu |


### 7.5 Verinoda'yı kendi üzerinde kullanma turu (2026-09-23, `docs/BENCHMARKS.md`)

Verinoda kendi deposunda kullanılırken iki sorun çıktı ve `8eb47ff`
commit'inde düzeltildi: Türkçe sözlükte sık yazılım kelimeleri yoktu
(`komut`, `kök`, `izin`, `tara`, `kaldır`, `ev dizini` ...), yumuşayan
kökler (`reddet` → `reddediyor`) eşleşmiyordu; sıralamada da "home
directory" sorusu, belgesinde tam bu ifade geçen `find_repo_root`'u 73.
sıraya koyuyordu. Artık sorudaki iki komşu kelime bir kod parçasında da yan
yanaysa o parça 1,3 kat sayılıyor (belge bölümleri hariç).

Aynı makine ve düzenek, `repeat = 2`, gerçek Graphify CLI; önceki commit
aynı koşullarda hemen ardından ölçüldü. Bulunan bilgi, önce → sonra:

| Küme | analyze | retrieve (JSON) | retrieve (düz metin) |
|---|---|---|---|
| Örnek uygulama | 31 → 31 | 31 → 31 | 32 → 32 |
| Graphify'ın kodu | 26 → **30** | 27 → 27 | 35 → **36** |
| Verinoda'nın eski kodu (33 bilgi) | 11 → **13** | 17 → **20** | 22 → **25** |
| Örnek uygulama, Türkçe | 30 → 30 | 28 → 28 | 32 → 32 |
| Graphify'ın kodu, Türkçe | 13 → **16** | 16 → 16 | 25 → 25 |

Çekinceler: sıralama ayarı dört varyant arasından beş kümenin toplamına
bakılarak seçildi; "ayrılmış" küme de bu seçimde kullanıldığı için bu
değişiklik açısından artık örneklem içidir. Bedeli: büyük kümelerde soru
başına ~0,03 sn daha uzun arama (ör. 0,110 → 0,140 sn); analyze bağlamı
bazı kümelerde 27–108 token büyüdü. Graphify ve düz arama sonuçları iki
çalıştırmada da aynı. Bu turu başlatan soru ("init ve scan komutları ev
dizininde çalıştırılınca reddediyor mu?") hâlâ yanıtlanamıyor: soru artık
doğru anlaşılıyor, ama komut adları (`init`, `scan`) işleyici
fonksiyonlarına (`cmd_init`, `cmd_scan`) bağlanmıyor.

### 7.6 Oyun modları ve veri dosyaları turu (2026-09-24, `docs/BENCHMARKS.md`)

Bu turun amacı, davranışının bir kısmı veri paketinde (`.mcfunction`), JSON
kaynaklarında ve yml ayar dosyasında duran bir Minecraft modu hakkındaki
soruları yanıtlayabilmekti (bölüm 4.1a). Değişiklik ölçülmeden önce dört
bağımsız inceleme ajanı kodu okudu ve yaklaşık yirmi hatayı yeniden üreterek
buldu; hepsi düzeltildi. En ciddileri: veri geçişi grafın sır saydığı dosyaları
(`credentials.json`) indeksleyip sorgu çıktısına basıyordu; sıradan bir
depodaki her `data/<a>/<b>/` klasörü kaynak bağlantısı üretiyordu; yanlış
türdeki dosyaya bağlantı `statically_verified` iddia oluyordu; başka paketteki
aynı adlı sınıfa yapılan Java çağrısı doğrulanmış çağrı sayılıyordu.

Yeni küme `glow_mod`: küçük, kurgusal bir Fabric modu (`examples/glow_mod`,
14 soru, 7'si Türkçe, 50 bilgi). Soruları Verinoda'yı hiç çalıştırmamış bir
ajan yazdı. Yalnızca ilk ölçüm için ayrılmıştır; sonraki eklemeler (Java çağrı
geçişi, dil dosyalarından çeviri çiftleri, sözlüğe oyun kelimeleri, analyze
zinciri) q01, q03, q04 ve q13'ün kaçırdıklarına bakılarak yapıldı. Bulunan
bilgi (50 üzerinden):

| Yaklaşım | `32a5bd4` | İlk ölçüm (ayrılmış) | Şimdi |
|---|---|---|---|
| Ham grep+okuma | 33 | 33 | 33 |
| Graphify | 13 | 13 | 13 |
| Verinoda analyze | 18 | 25 | **41** |
| Verinoda retrieve (JSON) | 25 | 32 | **43** |
| Verinoda retrieve (düz metin) | 34 | 46 | **48** |

İkinci ayrılmış küme `forge_mod` (kurgusal bir NeoForge modu: Java ve Kotlin, özel tarif tipi,
etiketler, worldgen, iki dilde dil dosyaları; 14 soru, 68 bilgi) bütün bu değişikliklerden sonra
Verinoda'yı hiç çalıştırmamış bir ajan tarafından yazıldı ve üzerinde hiçbir ayar yapılmadan bir kez
ölçüldü: düz metin 43 → **63**, JSON 28 → **45**, analyze 26 → **38** (`32a5bd4` → şimdi); ham okuma
36 (4.471 token ile), Graphify 12. En zayıf yerler: yalnızca veri dosyalarıyla yanıtlanan Türkçe
worldgen sorusu ve özel tarif tipi sorusu. O ölçümde Kotlin için ek çağrı geçişi yoktu; sonradan
eklendi (bu küme onun için artık örneklem içi: analyze 38 → 39).

Önceki beş kümede (gerileme kontrolü) Verinoda'nın 25 hücresinden 24'ü ve
Graphify ile ham okumanın bütün hücreleri aynı sayıda bilgi buldu; bir hücre
bir bilgi kaybetti (`graphify_core_tr` JSON 16 → 15: aynı içerikli belge
kopyaları artık tek öğe; kalan öğelerin iki çağrı kenarı "more" listesini JSON
bütçesinin dışına itti). Tarama süresi ölçülebilir biçimde artmadı. Bu turu
başlatan, sahibinin kendi modu üzerindeki özel küme yayımlanmıyor.

---

## 8. Ek maliyetler ve zayıf kalınan yerler

**Verinoda'nın daha yavaş ya da daha kötü olduğu yerler (ölçülmüş)**

- Soru başına süre: yayımlanmış benchmark'ta (tur-2 kodu), Graphify'ın kendi
  kodu üzerinde Graphify ve ham okumadan 2–7 kat yavaş (1,4–1,9 sn'ye karşı
  0,26–0,60 sn). Tur-3'te sorgu hızlandı, ama tam `analyze` hâlâ
  Graphify'ın CLI sorgusundan yavaş (0,84 sn'ye karşı 0,60 sn; farklı
  düzenekler).
- Küçük projede ham okuma neredeyse aynı iyi ve çok daha hızlı.
- Cevap bir dizi adlandırılmış tanım olduğunda Graphify bazen daha iyi (g04).
- Türkçe sorular İngilizcenin gerisinde (Graphify kodunda 13/37'ye karşı
  26/37).
- Çalışma zamanı izleyicisinin ek yükü hedeften yüksek.

**Ek maliyetler**

- `scan` Graphify'ın yaptığına ek olarak arama indeksi, repo sözlüğü ve
  sembol özetleri kurar. Bileşen süreleri yukarıda; toplamı tur-3 sonrası
  ölçülmedi.
- Karşıt kontrol iddia başına ek iştir. Kontrol edilmeyen iddialar nedeniyle
  birlikte (`not challenged: <neden>`) işaretlenir.
- `query` çıktısındaki kaynak kesitleri ile `analyze` çıktısındaki iddia,
  kanıt konumu, belirsizlik ve karşıt kontrol ayrıntıları çıktıyı büyütür.
  `analyze` çıktısında kaynak kesiti yoktur; kesitler `query`'dedir.
- Her CLI süreci grafiği `graph.json`'dan yeniden yükler (~0,06 sn). Uzun
  yaşayan MCP sunucusu grafiği bellekte tutar.

**Sezgisel kalan kısımlar**

- Veri akışında giriş ve kalıcılık noktası tespiti.
- Statik test erişimi (çalışma zamanı için `observe` gerekir).
- Karar belgesi bağlantıları metinsel eşleşmeye dayanır.
- `general` türündeki iddiaların terim örtüşmesiyle derecelenmesi.
- Referans araştırmasındaki mekanizma izi.

**Yalıtım sınırı**

- Container olmadan ağ ve dosya sistemi yalıtımı yoktur. İzin verilen test
  komutları projenin kendi kodunu ağ erişimiyle çalıştırır ve mutlak yolla
  kopya dışına yazabilir. İzin listesi dışındaki komutlar çalıştırılmaz.
  Container yolu ve Linux/macOS sınırları hiç denenmedi.

**Daraltılan kapsam**

- Graphify'ın LLM'li belge/görsel çıkarımı Verinoda'nın kendi komutlarında
  yoktur; `verinoda index -- extract` geçişiyle desteklenmeden erişilebilir.
- Diğer ajan platformlarının kurulumu, git kancaları ve `~/.graphify`'a
  yazan komutlar (`clone`, `provider`, `global`) geçişte **engellenir**, çünkü
  makinedeki gerçek bir Graphify kurulumunu bozabilir ya da ev dizinine
  yazar.

**Ad sorunu**

- Ürünün kalıcı adı "Verinoda" (eski çalışma adı RepoAtlas). Ad PyPI, npm ve GitHub'da boş; paket henüz bir paket dizinine
  (PyPI) yayınlanmadı.

---

## 9. Ölçüm nasıl yapılıyor, neyi ölçmüyor

Aynı sorular aynı kopya üzerinde şu yaklaşımlarla çalıştırılır: betikli ham
arama/okuma (soru terimlerini grep'le, en iyi 5 dosyayı 24.000 karakter
sınırına kadar oku), Graphify'ın gömülü sorgu çıktısı, gerçek Graphify CLI'si
(yalnızca `--graphify-cmd` verilirse) ve Verinoda (`analyze` ve `retrieve`).
Her sorunun doğru cevapları kaynak okunarak elle yazılmış "altın bilgiler"dir
ve her çalıştırmadan önce yeniden denetlenir.

Ölçülenler:

- **Teslim edilen bağlamda bulunan altın bilgiler.** Bu cevap doğruluğu
  değildir. Modelin cevabındaki doğru bilgiler yalnızca `--llm anthropic` ve
  bir API anahtarıyla ölçülür; yayımlanmış çalışmada anahtar yoktu, "ölçülmedi"
  yazıldı.
- **Bilinen-yanlış ifadeler:** önceden listelenmiş yanlış ifadelerin bir
  iddia, Graphify `EDGE` satırı ya da getirme kenarı olarak sunulup
  sunulmadığı. Ham arama iddia üretmediği için bu ölçüt ona uygulanmaz;
  listede olmayan yanlışları da yakalamaz.
- **Bağlam boyutu:** `tiktoken` kuruluysa `cl100k_base` kodlayıcısıyla sayılır
  (Claude'un tokenizer'ı değildir, yaklaşık bir ölçüdür); kurulu değilse
  "chars/4 tahmini" diye etiketlenir. Yayımlanmış çalışma chars/4 kullandı.
  Modelin gerçek giriş/çıkış token'ları yalnızca `--llm anthropic` ile, API'nin
  `usage` değerlerinden alınır.
- **Süre:** ilk indeksleme ile soru başı süre ayrı; soğuk ve sıcak geçiş ayrı.
- **Eskime ve karşıt kontrol:** `verinoda benchmark staleness replay|mutations`
  ve `verinoda benchmark critique-eval`.

Neden Bölüm 6 ile doğrudan karşılaştırılamaz: benchmark'taki ham okuma
≈ 6.000 token ile sınırlıdır (24.000 karakter ÷ 4). Bu yüzden Bölüm 6.3'teki
24.000–75.000 token'lık ham okuma senaryolarını yeniden üretmez. Ölçülen
oranlar senaryo katsayılarıyla aynı şey değildir.

Ölçülmeyenler: model döngüde cevap doğruluğu ve maliyeti; çok turlu ajan
oturumu; başka makineler ve işletim sistemleri; bellek kullanımı; git
geçmişine dayalı cevaplar (benchmark kopyasında tek commit vardır); daha
akıllı bir ham arama ajanı.

---

## 10. Kurulum ve kullanım özeti

**En kolay yol: `uv` ile tek komut + `verinoda setup`.** Git ya da önceden
kurulu Python gerekmez; uv, Verinoda'yı ayrı bir araç olarak kurar ve gerekirse
uygun bir Python indirir.

```powershell
# Windows (PowerShell ya da cmd)
winget install --id astral-sh.uv -e   # yalnızca `uv --version` çalışmıyorsa; sonra yeni bir terminal açın
uv tool install --force --reinstall-package verinoda --link-mode copy "verinoda[precise] @ https://github.com/ozcinax-star/verinoda/archive/main.zip"
uv tool update-shell                  # bir kez: verinoda'yı yeni terminaller için PATH'e ekler
```

```bash
# macOS / Linux (Windows'ta Git Bash da olur)
curl -LsSf https://raw.githubusercontent.com/ozcinax-star/verinoda/main/install.sh | sh
```

İkisi de GitHub arşivinden (zip), `precise` ekiyle ve kopyalama moduyla
(`--link-mode copy`) kurar. Güncellemek için aynı `uv tool install …` satırını
ya da betiği tekrar çalıştırın. `install.sh` uv yoksa resmi betiğiyle kurar,
yukarıdaki komutu çalıştırır ve `uv tool update-shell` ile PATH'i ayarlar;
seçenekler ortam değişkeniyle verilir: `VERINODA_REF` (dal/etiket/commit,
varsayılan `main`), `VERINODA_EXTRAS` (varsayılan `precise`, istemezseniz
`none`), `VERINODA_NO_MODIFY_PATH=1` (PATH'e dokunmaz),
`VERINODA_NO_UV_INSTALL=1` (uv yoksa kurmak yerine durur). Borulu bir betiği
çalıştırmadan önce okumak iyi bir alışkanlıktır.

Windows'ta neden `irm … | iex` tek satırı yok: Microsoft Defender,
`powershell -ExecutionPolicy ByPass -c "irm <betik adresi> | iex"` komutunu bu
projenin betiği için `Trojan:Win32/Commando.A!ml` olarak engelledi. Bu, "indir
ve çalıştır" komut satırı kalıbına verilen bir makine öğrenmesi kararıdır;
betik dosyasının kendisi işaretlenmedi. Yukarıdaki düz `uv` komutları bu
kalıbı kullanmaz ve aynı makinede uyarı üretmedi.

Sonra her proje için bir kez, proje klasörünün içinde:

```bash
verinoda setup
```

`setup` şunları yapar ve tekrar çalıştırmak güvenlidir: `.verinoda/`
klasörünü açar; ilk seferde kodu tarar (`scan`), sonraki seferlerde yalnızca
değişenleri günceller (`update`); PATH'te bulduğu ajanlara (Claude Code,
Codex) proje kapsamında skill + MCP ayarını kurar (kendisine ait olmayan
dosyaların üzerine yazmaz); en sonda ne yapıldığını ve elle yapılması
gerekenleri listeler. Ev dizininin kendisinde çalışmayı reddeder.
Seçenekler: `--agents claude,codex|all|none` (varsayılan: bulunanlar),
`--scope user` (ajan ayarlarını proje yerine kullanıcı düzeyine kurar),
`--no-mcp`, `--json`.

Kurulumdan sonra:

- Claude Code: klasörü açın, `verinoda` MCP sunucusunu bir kez onaylayın
  (`/mcp`), sonra `/verinoda <soru>`.
- Codex: projeye güvenin (`.codex/config.toml` okunsun), sonra
  `$verinoda <soru>` (Codex'te `/verinoda` komutu yoktur).
- Terminal: `verinoda query "<soru>"` ya da `verinoda analyze "<soru>"`.

**Diğer yollar ve tek tek komutlar:**

```bash
# Bir checkout'tan ya da wheel'den (PyPI'da yok)
uv tool install --link-mode copy .                                  # Windows'ta Codex için --link-mode copy şart
uv tool install --link-mode copy --with "jedi>=0.19.2,<0.21" .      # kesin çözümleme (precise) ile
uv tool install --link-mode copy "verinoda[precise] @ git+https://github.com/ozcinax-star/verinoda"   # git ile
pip install ".[precise]"                                            # ya da mevcut bir venv'e

verinoda doctor
verinoda scan .
verinoda plan draft "Sipariş API'den veritabanına nasıl ulaşıyor?"
verinoda plan check plan-001.json
verinoda analyze --plan plan-001.json
verinoda resolve "requests 2.31 sürümündeki sessions.py ile karşılaştır"
verinoda observe --for apply_discount        # projenin .venv'inde pytest gerekir
verinoda install --agent claude --scope project    # Claude Code: /verinoda …
verinoda install --agent codex  --scope project    # Codex: $verinoda …
```

Neden `--link-mode copy`: uv varsayılan olarak paket dosyalarını kendi
önbelleğinden sabit bağlantıyla (hardlink) kurar. Codex'in Windows
sandbox'ı bu dosyaları okuyamadı; CLI çalışmadı, MCP araçları çalıştı.
Kopyalama moduyla sorun ortadan kalkar; kurulum betikleri bu modu kullanır,
`verinoda doctor` ve `verinoda setup` hardlink'li kurulumu uyarır.
Kurulum yolları geçici uv dizinlerinde denendi: Windows'taki `uv tool install …`
satırı (iki kez: kurulum ve güncelleme) ve Git Bash'te `install.sh`; ikisi de
herkese açık GitHub arşivinden kurup `verinoda --version` çalıştırdı. macOS ve
Linux'ta henüz denenmedi. `winget install --id astral-sh.uv` satırı yalnızca
paketin winget'te bulunduğu kontrol edilerek eklendi; bu makinede uv zaten
kurulu olduğu için çalıştırılmadı.

Depo herkese açıktır (https://github.com/ozcinax-star/verinoda); paket PyPI'da
yayımlanmadı. Graphify'ın (`graphifyy`) ayrıca kurulması gerekmez.

---

## 11. Sözlük

| Terim | Açıklama |
|---|---|
| Claim (iddia) | Kod hakkında önemli bir sonuç; durumu, güveni ve kanıtlarıyla saklanır |
| Evidence (kanıt) | İddianın dayandığı kaynak: dosya/satır + hash, commit, URL, test çalıştırması, gözlem… |
| Derece (grade) | Kanıtın iddiayı ne ölçüde söylediği: tam / kısmi / yok |
| Soru planı | Kullanıcının sorusunun alt sorulara, niyetlere ve kod adlarına ayrılmış, denetlenmiş hali |
| Mention | Soruda kodu adlandıran kelime ("Sipariş", `OrderRepository`) |
| Referans sabitleme (pin) | Bir repo/paket/belgenin kastedilen tam sürüme (commit, tag, sürüm) bağlanması |
| Snapshot | Bir analiz anındaki commit ve dosya özetleri |
| Öğe (facet) | Bir iddianın dayandığı parça: imza, gövde, isim bağlaması, belge bölümü, test kümesi |
| Anchor | Alıntılanan satırların bağlandığı sembol/ifade/bölüm; kod taşınınca satırları yeniden bulur |
| `stale` | Dayandığı öğe değiştiği için güncelliği kaybolmuş iddia |
| Kesin / sezgisel çürütme | Kesin: kapsamı belli, eksiksiz bir denetim (iddiayı çürütür). Sezgisel: yalnızca bir basamak düşürür |
| `EXTRACTED` / `INFERRED` | Graphify'ın grafik kenarı güven etiketleri; Verinoda'da doğrulama sayılmaz |
| Tavan (ceiling) | Bir iddianın yeniden doğrulamayla ulaşabileceği en yüksek durum |
| Karşıt kontrol | İddiayı çürütmeye çalışan otomatik denetim |
| Örneklem içi / ayrılmış küme | Örneklem içi: tasarım o sorular görülerek yapıldı. Ayrılmış: hiçbir ayar için kullanılmadı |
| MCP | Kodlama ajanlarının araç çağırmak için kullandığı protokol |

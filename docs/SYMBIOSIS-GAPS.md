# Verinoda Symbiosis: eksikler ve güncelleme önerileri

Kaynak: biyoloji testi tur 6 (beş tur, iki varyant: Symbiosis ve Verinoda'suz ham). Ayrıntılı rapor ve kayıtlar
`biyo-platform/devtest/run6/` altında (bu depoda `biyo-platform-test` dalında). Bu belge yalnızca Verinoda tarafındaki
eksikleri listeler. Ölçüm ayrıntıları gold verisi içerdiği için bu belgede yoktur.

Bu test iki şeyi ölçtü: Verinoda'nın bir corpus üzerinde ilişki çıkarmada yardımcı olup olmadığı, ve geliştirme
maliyeti. Sonuç: aday konu ve pasaj bulmada yardımcı oldu; ilişki yönü ve türü için yardımcı olmadı.

## Ölçülen eksikler

### 1. `verinoda analyze` ilişki iddiası üretmiyor (kritik)

- Ölçüm (tur 4): 5 topic çiftinde (sıralama ve destek ilişkisi olduğu bilinen) analyze çağrıldı. Dönen 13 iddianın
  tamamı tanım ya da konu-yeri iddiasıydı (`primary_source_verified`, `statically_verified`). İlişki iddiası: 0.
- Sonuç boyutu: 3,5–3,9 bin karakter.
- Etki: ürünün kenar kümesi (önkoşul, destek, ortak kavram) Verinoda'dan gelmedi; symbiosis ürünü bunu kendi kural
  katmanında yazdı.
- Öneri: corpus üzerinde ilişki iddiası türü: `relation` (tür: önkoşul/destek/ortak), `from`, `to`, kanıt cümlesi
  (dosya:satır), durum (kanıtlı / çıkarım / bilinmiyor). Sıralama ipuçlarını ("öncesinde", "önce", "gerektirir")
  bağlam penceresiyle birlikte döndürmeli.

### 2. `verinoda query` pasaj ipucu döndürüyor, ilişki cümlesi değil

- Ölçüm (tur 1 ve 4): "hangi cümleler X'ten önce Y'yi öğrenmeyi söyler" türü sorular, topics.json satırlarını ve
  ilgisiz pasajları döndürdü; ilişki cümlesi dönmedi.
- Sonuç boyutu: 5–5,3 bin karakter (6 sonuç).
- Öneri: `--sentences-only` ya da benzeri bir mod: yalnızca eşleşen cümleler, dosya:satır ile; her cümle en fazla
  bir kez; sonuç sayısı sınırı.

### 3. Kanıt kuralı ürün tarafında, Verinoda'da yok

- Symbiosis ürünü, bir kenarın yalnızca cümlede iki konunun da adlandırılmasıyla kanıtlı sayılmasını kendi kuralıyla
  yazdı (tur 4). Verinoda'nın kanıt durumu bu kuralı bilmiyor; ürünün "kanıtlı" ile Verinoda'nın "statically_verified"
  arasında bir uyumsuzluk var.
- Öneri: kullanıcı tanımlı kanıt kuralı (ad, eşik, durum eşlemesi) desteği; durumlar bu kurala göre hesaplansın.

### 4. MCP araçları bir depoya bağlı

- Ölçüm: Symbiosis ajanı MCP `mcp__verinoda__project_query` ile sorguladığında yanıt ana depodan (verinoda-mod) geldi,
  `symbiosis/` klasöründen değil (tur 1, ~13 bin aday, hiçbiri symbiosis'ten değil).
- Geçici çözüm: `verinoda setup` ile proje kökü kuruldu, ama başka bir klasördeki oturum bu MCP'yi kullanamıyor.
- Öneri: MCP sunucusu için proje kökü parametresi, ya da proje başına bağlama.

### 5. `verinoda setup` yerel yan dosyalar üretiyor

- Ölçüm: kb klasöründe `setup` çalıştığında `.claude/`, `.codex/`, `.agents/`, `.mcp.json`,
  `.verinoda/install-manifest.json` oluştu; `.mcp.json` mutlak yolları içeriyor.
- Öneri: yan dosyaları kapatan bir bayrak (`--no-agent-files`), ve `.mcp.json`'un göreli yol kullanması.

### 6. Sorgu sırasında yazma gözlemi doğrulanmadı

- Bir ajan `verinoda query` çalıştırdıktan sonra `kb/.verinoda/index` altında yazılan dosyalar gördüğünü bildirdi.
  Bu ölçülmedi.
- Öneri: salt okunur sorgu modu, ve yazmanın gerekiyorsa bildirilmesi.

### 7. Çağrı başına token maliyeti ölçülemiyor

- Ajan bildirimleri yalnızca toplam token veriyor. Bir Verinoda çağrısının tek tek maliyeti (giriş ve çıkış token'ı)
  bilinmiyor; bu yüzden "Verinoda ucuz mu" sorusu bu testte cevaplanamadı.
- Öneri: çağrı başına kullanım bilgisi (tokens in/out, sonuç boyutu) bir alanda dönsün.

### 8. Çalışma zamanında paket kullanılmıyor (ürün tarafı)

- Symbiosis ürünü `query.py` ile stdlib üzerinde çalışıyor; Verinoda paketi çalışma zamanında yüklenmiyor. Verinoda'nın
  etkisi veri üretimi ve geliştirme sırasında oldu. Bu bir hata değil, ama "Verinoda ürünü" iddiasını sınırlar.
- Öneri: ürün tarafında kullanılabilecek, paket bağımlılığı gerektirmeyen bir çalışma zamanı paketi (kanıt doğrulama
  ve indeks sorgusu) ya da bu sınırın açıkça belgelenmesi.

## Test sırasında ölçülen sonuçlar (özet)

Benchmark (12 sorgu, gold'a karşı, tur 5, son ürünler):

| Metrik | Ham (Verinoda yok) | Symbiosis |
|---|---|---|
| Kenar kapsamı | 0,81 | 0,60 |
| Yön doğruluğu | 0,19 | 0,12 |
| Yargılanan hassasiyet | 0,97 | 0,88 |
| Kanıt oranı | 0,47 | 0,71 |
| Merkez hatası | 3 | 4 |

Yorum: Symbiosis kanıt oranında önde; kapsam, yön ve merkez seçiminde geride. Bu, Verinoda'nın ilişki çıkarma
kapasitesi eksik olduğunda, ürünün kendi kural katmanına düşmesiyle açıklanabilir (madde 1).

## Önceliklendirme

1. İlişki iddiası türü (madde 1). Bu olmadan Verinoda ilişki çıkarmada kullanılamaz.
2. Cümle düzeyinde sorgu modu (madde 2).
3. Kullanıcı tanımlı kanıt kuralı (madde 3).
4. Proje başına MCP bağlama (madde 4).
5. Diğerleri (madde 5–8).

## Sınırlar

- Örnek küçük: 12 sorgu, bir corpus (31–41 konu), tek bir alan (lise biyolojisi).
- Gold, ürün geliştirmesiyle aynı oturumda tasarımcı tarafından üretildi; bağımsız değil.
- Token ölçümü aşama toplamıdır; Verinoda çağrılarının tek tek maliyeti yoktur.

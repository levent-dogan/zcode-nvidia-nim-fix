# zcode-nvidia-nim-fix Türkçe Kullanım Kılavuzu

Bu kılavuz, NVIDIA NIM modellerini ZCode veya özel OpenAI API adresi tanımlamaya
izin veren başka bir IDE/istemci üzerinden yerel proxy ile kullanmayı açıklar.

Proxy aşağıdaki yerel adreste çalışır:

```text
http://127.0.0.1:8787/v1
```

İstekleri aşağıdaki NVIDIA NIM adresine iletir:

```text
https://integrate.api.nvidia.com/v1/chat/completions
```

Gerçek NVIDIA API anahtarları repoda tutulmaz. `.env` dosyası yalnızca yerel
bilgisayarda bulunmalı ve GitHub'a gönderilmemelidir.

## İçindekiler

1. [Proxy neden gerekli?](#proxy-neden-gerekli)
2. [Hangi uygulamalarla kullanılabilir?](#hangi-uygulamalarla-kullanılabilir)
3. [Gereksinimler](#gereksinimler)
4. [Kurulum](#kurulum)
5. [API anahtarı modu seçimi](#api-anahtarı-modu-seçimi)
6. [ZCode yapılandırması](#zcode-yapılandırması)
7. [Başka IDE ve istemcilerde kullanım](#başka-ide-ve-istemcilerde-kullanım)
8. [Proxy'yi başlatma](#proxyyi-başlatma)
9. [Çalışma durumunu test etme](#çalışma-durumunu-test-etme)
10. [Model seçimi](#model-seçimi)
11. [Kuyruk ve anahtar geçişi](#kuyruk-ve-anahtar-geçişi)
12. [Hata giderme](#hata-giderme)
13. [Güvenlik ve gizlilik](#güvenlik-ve-gizlilik)
14. [Güncelleme ve geliştirme kontrolleri](#güncelleme-ve-geliştirme-kontrolleri)

## Proxy Neden Gerekli?

ZCode, NVIDIA NIM'e gönderdiği isteğe aşağıdaki gibi sağlayıcıya özel alanlar
ekleyebilir:

```json
{
  "extra_body": {
    "chat_template_kwargs": {
      "enable_thinking": false
    }
  }
}
```

NVIDIA NIM bu alanları istek gövdesinin en üst seviyesinde kabul etmediğinde
aşağıdaki hata oluşur:

```text
Validation: Unsupported parameter(s): `extra_body`
```

Proxy isteği NVIDIA'ya göndermeden önce desteklenmeyen alanları kaldırır.
Standart OpenAI Chat Completions alanlarını ve akış yanıtlarını korur.

## Hangi Uygulamalarla Kullanılabilir?

Proxy yalnızca ZCode ile sınırlı değildir. Aşağıdaki özellikleri sağlayan bir
IDE, eklenti, masaüstü istemcisi veya yerel araç proxy'yi kullanabilir:

- Özel OpenAI Base URL tanımlayabilme
- OpenAI Chat Completions biçimini kullanabilme
- Model kimliğini elle girebilme
- Bearer API anahtarı gönderebilme
- `POST /v1/chat/completions` uç noktasını kullanma

Genel istemci ayarı:

| Alan | Değer |
| --- | --- |
| Sağlayıcı/API biçimi | OpenAI veya OpenAI-compatible |
| Base URL | `http://127.0.0.1:8787/v1` |
| Uç nokta | `/chat/completions` |
| Model | NVIDIA NIM model kimliği |
| API anahtarı | Seçilen moda göre yerel veya gerçek anahtar |

Proxy şu anda yalnızca Chat Completions isteklerini destekler. Aşağıdaki uç
noktaları bekleyen istemciler doğrudan uyumlu değildir:

- `/v1/responses`
- `/v1/models`
- `/v1/embeddings`
- Görüntü, ses veya dosya uç noktaları

İstemci model listesini `/v1/models` üzerinden otomatik almak zorundaysa model
bulma işlemi başarısız olabilir. Uygulama izin veriyorsa model kimliğini elle
ekleyin.

## Gereksinimler

- Windows 10 veya Windows 11
- PowerShell 5.1 ya da PowerShell 7+
- Python 3.10 veya daha yeni bir sürüm
- En az bir NVIDIA NIM API anahtarı
- OpenAI uyumlu özel sağlayıcı tanımlayabilen bir IDE/istemci

Sürümleri kontrol edin:

```powershell
python --version
$PSVersionTable.PSVersion
```

Python bulunamıyorsa Python'u kurarken `Add Python to PATH` seçeneğini
etkinleştirin ve yeni bir PowerShell penceresi açın.

## Kurulum

### 1. Repoyu indirin

Git kullanıyorsanız:

```powershell
git clone https://github.com/levent-dogan/zcode-nvidia-nim-fix.git
cd zcode-nvidia-nim-fix
```

ZIP olarak indirdiyseniz arşivi açın ve PowerShell ile proje dizinine geçin:

```powershell
cd C:\Path\To\zcode-nvidia-nim-fix
```

### 2. Sanal ortamı oluşturun

Bu işlem ilk kurulumda bir kez yapılır:

```powershell
python -m venv .venv
```

PowerShell script çalıştırmayı engellerse yalnızca açık PowerShell işlemi için
geçici izin verin:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

Sanal ortamı etkinleştirin:

```powershell
.\.venv\Scripts\Activate.ps1
```

İsteğe bağlı olarak pip'i güncelleyin:

```powershell
python -m pip install --upgrade pip
```

Proxy çalışma zamanında üçüncü taraf Python paketi kullanmaz; Python standart
kütüphanesiyle çalışır.

## API Anahtarı Modu Seçimi

| Mod | Kullanım | IDE'deki API anahtarı | Otomatik anahtar geçişi |
| --- | --- | --- | --- |
| `Env` | Tek NVIDIA anahtarı | Herhangi bir yerel placeholder | Hayır |
| `Client` | Her sağlayıcı/proje kendi NVIDIA anahtarını gönderir | Gerçek NVIDIA anahtarı | Hayır |
| `Pool` | Bir yerel anahtar arkasında özel NVIDIA anahtar havuzu | `NIM_PROXY_CLIENT_KEY` | Evet |

Birden fazla proje ve birden fazla NVIDIA anahtarı için önerilen seçenek
`Pool` modudur. Gerçek anahtarların IDE ayarlarında görünmesini istemiyorsanız
da `Pool` kullanın.

### Env Modu

Tek bir NVIDIA anahtarını yalnızca geçerli PowerShell oturumuna tanımlayın:

```powershell
$env:NVIDIA_API_KEY="YOUR_NVIDIA_API_KEY"
.\run_proxy.ps1 -ApiKeyMode Env
```

Bu modda IDE'nin API key alanına herhangi bir boş olmayan placeholder
yazılabilir. Gerçek NVIDIA anahtarı proxy işleminin ortam değişkeninden alınır.

PowerShell penceresi kapandığında işlem seviyesindeki ortam değişkeni silinir.

### Client Modu

Proxy'yi başlatın:

```powershell
.\run_proxy.ps1 -ApiKeyMode Client -UpstreamTimeoutSeconds 600
```

Her IDE sağlayıcısına veya projeye ilgili gerçek NVIDIA API anahtarını girin.
Proxy gelen bearer anahtarını NVIDIA'ya iletir ancak konsola yazmaz.

Farklı anahtarlar paralel çalışabilir. Aynı anahtarla gelen istekler varsayılan
olarak aynı FIFO kuyruğunda sıraya alınır. Proxy bu modda yalnızca istekte gelen
anahtarı bildiği için başka bir anahtara otomatik geçemez.

### Pool Modu

Pool modu gerçek NVIDIA anahtarlarını yerel `.env` dosyasında tutar ve IDE'lere
yalnızca ayrı bir yerel proxy anahtarı verir.

Örnek dosyayı kopyalayın:

```powershell
Copy-Item .env.example .env
```

Güçlü bir yerel proxy anahtarı üretin:

```powershell
([guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N"))
```

`.env` dosyasını yalnızca kendi bilgisayarınızda düzenleyin:

```dotenv
NIM_PROXY_CLIENT_KEY=REPLACE_WITH_A_RANDOM_LOCAL_PROXY_SECRET
NVIDIA_API_KEY_1=REPLACE_WITH_NVIDIA_KEY_1
NVIDIA_API_KEY_2=REPLACE_WITH_NVIDIA_KEY_2
NVIDIA_API_KEY_3=REPLACE_WITH_NVIDIA_KEY_3
```

İhtiyacınıza göre `NVIDIA_API_KEY_4`, `NVIDIA_API_KEY_5` ve sonraki numaraları
ekleyebilirsiniz. Numaralar `1` ile başlamalı ve artan sırada kullanılmalıdır.

Proxy'yi başlatın:

```powershell
.\run_proxy.ps1 -ApiKeyMode Pool -UpstreamTimeoutSeconds 600
```

IDE'deki API key alanına gerçek NVIDIA anahtarlarından birini değil,
`NIM_PROXY_CLIENT_KEY` değerini girin.

Pool modu:

- Yinelenen NVIDIA anahtarlarını reddeder.
- Yerel proxy anahtarı ile NVIDIA anahtarının aynı olmasını reddeder.
- `.env` içinden yalnızca izin verilen değişkenleri okur.
- Anahtar değerlerini veya tam mesaj içeriğini loglamaz.
- Sağlıklı anahtarları numara sırasıyla döngüsel kullanır.

## ZCode Yapılandırması

ZCode içinde özel sağlayıcı ekleyin ve aşağıdaki değerleri kullanın:

| ZCode alanı | Değer |
| --- | --- |
| Base URL | `http://127.0.0.1:8787/v1` |
| API format | `Chat completions (/chat/completions)` |
| API key | Seçilen moda göre |
| Model list | NVIDIA NIM model kimlikleri |

API key alanı:

- `Env`: Herhangi bir boş olmayan placeholder
- `Client`: O sağlayıcının gerçek NVIDIA API anahtarı
- `Pool`: `.env` içindeki `NIM_PROXY_CLIENT_KEY`

Örnek model kimlikleri:

```text
z-ai/glm-5.2
z-ai/glm-5.1
moonshotai/kimi-k2.6
deepseek-ai/deepseek-v4-pro
qwen/qwen3-coder-480b-a35b-instruct
```

Modelin NVIDIA hesabınızda kullanılabilir olması ve Chat Completions arayüzünü
desteklemesi gerekir.

![ZCode yerel NVIDIA NIM proxy yapılandırması](screenshot/screenshot_2.png)

## Başka IDE ve İstemcilerde Kullanım

1. Proxy'yi seçtiğiniz API anahtarı modunda başlatın.
2. IDE'de OpenAI veya OpenAI-compatible sağlayıcı seçin.
3. Base URL olarak aşağıdaki adresi girin:

   ```text
   http://127.0.0.1:8787/v1
   ```

4. API key alanını moda göre doldurun.
5. Model kimliğini elle girin.
6. API yöntemi seçilebiliyorsa `Chat Completions` kullanın.
7. Kısa bir mesajla bağlantıyı test edin.

Uygulama Base URL sonuna otomatik olarak `/v1` ekliyorsa adresi
`http://127.0.0.1:8787` olarak girmek gerekebilir. Gönderilen son isteğin tam
yolu mutlaka `/v1/chat/completions` olmalıdır; `/v1/v1/chat/completions`
olmamalıdır.

Birden fazla IDE aynı çalışan proxy'yi kullanabilir. Aynı bilgisayarda ikinci
bir proxy işlemini aynı `8787` portunda başlatmayın. Ayrı süreç gerekiyorsa
başlatmadan önce farklı port tanımlayın:

```powershell
$env:NIM_PROXY_PORT="8788"
.\run_proxy.ps1 -ApiKeyMode Pool
```

Bu süreç için Base URL:

```text
http://127.0.0.1:8788/v1
```

## Proxy'yi Başlatma

### Normal PowerShell komutları

Pool modu:

```powershell
.\run_proxy.ps1 -ApiKeyMode Pool -UpstreamTimeoutSeconds 600
```

Client modu:

```powershell
.\run_proxy.ps1 -ApiKeyMode Client -UpstreamTimeoutSeconds 600
```

Debug loglu pool modu:

```powershell
.\run_proxy.ps1 -ApiKeyMode Pool -DebugMode -UpstreamTimeoutSeconds 600
```

Debug loglu client modu:

```powershell
.\run_proxy.ps1 -ApiKeyMode Client -DebugMode -UpstreamTimeoutSeconds 600
```

### Hazır BAT dosyaları

Pool ve debug:

```bat
start_proxy_pool_debug.bat
```

Client ve debug:

```bat
start_proxy_client_debug.bat
```

Parametreleri kendiniz vermek için:

```bat
start_proxy.bat -ApiKeyMode Pool -UpstreamTimeoutSeconds 600
```

Proxy'yi kapatmak için çalıştığı konsolda `Ctrl+C` kullanın. Pool ile Client
modu arasında geçiş yapmadan önce çalışan proxy'yi kapatın.

## Çalışma Durumunu Test Etme

Proxy başladıktan sonra ikinci bir PowerShell penceresi açın.

Sağlık kontrolü:

```powershell
Invoke-RestMethod http://127.0.0.1:8787/health |
  ConvertTo-Json -Depth 5
```

Yanıtta `status`, `api_key_mode` ve kuyruk/havuz sayaçları görülmelidir. Anahtar
değerleri sağlık yanıtına dahil edilmez.

Pool modu için örnek sohbet testi:

```powershell
$localProxyKey = Read-Host "NIM_PROXY_CLIENT_KEY değerini girin"

$body = @{
  model = "z-ai/glm-5.2"
  messages = @(
    @{
      role = "user"
      content = "Say hello in one sentence."
    }
  )
  stream = $false
  max_tokens = 128
  extra_body = @{
    chat_template_kwargs = @{
      enable_thinking = $false
    }
  }
} | ConvertTo-Json -Depth 10

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8787/v1/chat/completions" `
  -Headers @{ Authorization = "Bearer $localProxyKey" } `
  -ContentType "application/json" `
  -Body $body
```

Debug modunda aşağıdaki log beklenir:

```text
Stripped unsupported NVIDIA NIM request keys: extra_body
```

Bu mesaj proxy'nin sorunlu alanı kaldırdığını gösterir.

## Model Seçimi

Proxy yalnızca GLM 5.2 için hazırlanmış değildir. İstekteki `model` değerini
NVIDIA NIM'e iletir. Modelin:

- NVIDIA NIM üzerinde kullanılabilir olması,
- API anahtarınızın modele erişebilmesi,
- `/chat/completions` arayüzünü desteklemesi

gerekir.

Bir model normal sohbet yanıtı verebilir ancak IDE'nin beklediği yapılandırılmış
`tool_calls` çıktısını üretmeyebilir. Bu durumda model sohbet için kullanılabilir
olsa da dosya okuma, komut çalıştırma veya ajan iş akışlarında uygun olmayabilir.

## Kuyruk ve Anahtar Geçişi

Pool modunda anahtarlar numara sırasıyla seçilir:

```text
KEY 1 -> KEY 2 -> KEY 3 -> ... -> son KEY -> KEY 1 -> ...
```

Son anahtardan sonra döngü yeniden ilk anahtara gelir.

| Durum | Proxy davranışı |
| --- | --- |
| Anahtar meşgul | İstek sınırlı FIFO kuyruğunda bekler |
| `408` veya `429` | Anahtar geçici beklemeye alınır, denenmemiş başka anahtar seçilir |
| `401` veya `403` | Anahtar yeniden başlatmaya kadar karantinaya alınır |
| `5xx` veya bağlantı hatası | Varsayılan olarak en fazla bir alternatif anahtar denenir |
| `400`, `404` veya `422` | Yanıt değiştirilmeden istemciye döndürülür |
| Streaming başladı | İstek başka anahtarda tekrar oynatılmaz |
| Yerel kuyruk dolu | `429 proxy_queue_full` döndürülür |
| Kuyruk bekleme süresi doldu | `504 proxy_queue_timeout` döndürülür |

Streaming başladıktan sonra otomatik tekrar yapılmaması, aynı isteğin iki kez
çalıştırılmasını ve araç işlemlerinin yinelenmesini önler.

## Hata Giderme

### Provider authentication failed veya HTTP 401

Önce kullanılan modu kontrol edin:

```powershell
Invoke-RestMethod http://127.0.0.1:8787/health
```

- Pool modunda IDE'ye `NIM_PROXY_CLIENT_KEY` girilmelidir.
- Client modunda IDE'ye gerçek NVIDIA API anahtarı girilmelidir.
- Pool modunda gerçek NVIDIA anahtarı yerel istemci anahtarı olarak çalışmaz.
- `.env` değiştirildiyse proxy yeniden başlatılmalıdır.

Anahtarı konsola yazdırmayın veya ekran görüntüsünde göstermeyin.

### HTTP 404

İstemcinin gönderdiği yol `/v1/chat/completions` olmalıdır. Proxy `/models`,
`/responses` veya çift `/v1/v1/chat/completions` yolunu desteklemez.

### HTTP 429

`429 Too Many Requests`, her zaman proxy arızası anlamına gelmez. NVIDIA NIM
anahtar, hesap veya model kapasitesi sınırına ulaşmış olabilir.

Pool modunda anahtar beklemeye alınır ve uygun başka anahtar denenir. Tüm
anahtarlar meşgul veya beklemedeyse istek kuyruğa girebilir ya da sınır dolunca
yerel `429` dönebilir.

### HTTP 500, 502 veya 503

Bu durum genellikle NVIDIA NIM veya seçilen model tarafındaki geçici sunucu
hatasıdır. Pool modu, streaming başlamadıysa sınırlı sayıda başka anahtar
deneyebilir. Sürekli oluşuyorsa:

- Daha kısa bir istek deneyin.
- Farklı bir NVIDIA NIM modeli deneyin.
- Debug logundaki upstream durum kodunu kontrol edin.
- NVIDIA model sayfasındaki servis durumunu daha sonra tekrar kontrol edin.

### HTTP 504 veya TimeoutError

NVIDIA yanıtı proxy zaman aşımından önce başlatmamış olabilir. Uzun işlemler
için:

```powershell
.\run_proxy.ps1 -ApiKeyMode Pool -DebugMode -UpstreamTimeoutSeconds 600
```

IDE'nin kendi zaman aşımı daha kısaysa yalnızca proxy süresini artırmak yeterli
olmaz. İsteği küçültün veya daha hızlı bir model deneyin.

### Maksimum context length hatası

Bu hata proxy kuyruğundan kaynaklanmaz. Konuşma geçmişi ve yeni mesajlar modelin
bağlam sınırını aşmıştır. Eski mesajları özetleyin, yeni konuşma başlatın veya
istemcinin desteklediği context küçültme/compact özelliğini kullanın.

Proxy IDE arayüzüne otomatik `/compact` komutu göndermez.

### Garip kelimeler veya `<tool_call>` metni

Bazı modeller gerçek OpenAI `tool_calls` alanı yerine düz metin içinde araç
etiketi döndürebilir. Proxy bu metni komut olarak çalıştırmaz. Varsayılan
`diagnostic` modu okunabilir bir uyarı üretir.

Ham model çıktısını geçirmek için:

```powershell
.\run_proxy.ps1 -ApiKeyMode Pool -ToolCallTextMode pass -DebugMode
```

Bu seçenek modelin tool calling uyumluluğunu düzeltmez; yalnızca ham yanıtı
istemciye geçirir.

### Port kullanımda

Aynı portta başka bir proxy çalışıyor olabilir:

```powershell
Get-NetTCPConnection -LocalPort 8787 -ErrorAction SilentlyContinue
```

Çalışan proxy'yi `Ctrl+C` ile kapatın veya yeni işlem için farklı port seçin.

## Güvenlik ve Gizlilik

- `.env` dosyasını Git'e eklemeyin.
- Gerçek anahtarları README, issue, log veya ekran görüntüsünde paylaşmayın.
- `.env.example` içinde yalnızca placeholder değerler bırakın.
- Proxy'yi varsayılan `127.0.0.1` adresinde çalıştırın.
- Pool modunda IDE'lere yalnızca `NIM_PROXY_CLIENT_KEY` verin.
- Yerel proxy anahtarı ile NVIDIA anahtarlarını farklı tutun.
- Açığa çıkan yerel veya NVIDIA anahtarını hemen iptal edip yenileyin.
- Debug logları anahtar değerlerini ve tam mesaj içeriğini yazmaz.

`.env` dosyasının Git tarafından yok sayıldığını kontrol edin:

```powershell
git check-ignore -v .env
git ls-files .env
```

İkinci komut hiçbir çıktı üretmemelidir.

Commit öncesi kontrol:

```powershell
git status --short
git diff --cached --name-only
```

`.env` veya özel ekran görüntüleri listede görünmemelidir.

## Güncelleme ve Geliştirme Kontrolleri

Repoyu güncelleyin:

```powershell
git pull --ff-only
```

Sanal ortam eksikse yeniden oluşturun:

```powershell
python -m venv .venv
```

Geliştirme araçlarını kurun:

```powershell
python -m pip install pytest ruff mypy
```

Kontrolleri çalıştırın:

```powershell
python -m pytest
python -m ruff check .
python -m mypy nvidia_nim_proxy tests
```

Sorun bildirirken API anahtarlarını, tam prompt içeriğini ve özel proje
bilgilerini kaldırın. Yararlı güvenli bilgiler:

- Proxy sürümü
- API key modu
- Model kimliği
- HTTP durum kodu
- Hatanın streaming öncesi veya sonrası oluşması
- Anahtar içermeyen kısa debug logu

Ana İngilizce belge için [README.md](README.md), sürüm geçmişi için
[CHANGELOG.md](CHANGELOG.md), güvenlik bildirimleri için
[SECURITY.md](SECURITY.md) dosyasına bakın.

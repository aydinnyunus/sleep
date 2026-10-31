# The Sleep Framework — LLM'ler için Sürekli Öğrenmede Bilgi Koruma

> **Kaynak:** [Google Research Blog — "The sleep framework: A new way for LLMs to retain knowledge in continual learning"](https://research.google/blog/the-sleep-framework-a-new-way-for-llms-to-retain-knowledge-in-continual-learning/)
> **Makale:** [arXiv:2606.03979](https://arxiv.org/abs/2606.03979)
> **Yazarlar:** Ali Behrouz (Google & Cornell), Farnoosh Hashemi (Cornell), Adel Javanmard (Google & USC), Vahab Mirrokni
> **Bu dosya:** blog yazısının Türkçe çözümlemesi/özetidir; kaynak metnin birebir çevirisi değildir. Alıntılanmayan her yorum bölümü açıkça "Analiz" başlığı altında ayrılmıştır.

---

## 1. Problem: dağıtılmış LLM'ler öğrenmeyi bırakıyor

Modern LLM'ler iki mekanizma ile güçlü hale geliyor: **pre-training** ve **in-context learning (ICL)**. Ama model deploy edildikten sonra büyük ölçüde **statik** kalıyor — sabit bir knowledge cutoff ile çalışıyor, yeni bilgi edinemez hale geliyor.

İki naif çözüm de çalışmıyor:

| Yaklaşım | Neden yetmiyor |
|---|---|
| Parametreleri sürekli güncellemek (SFT / devam eden pre-training) | **Catastrophic forgetting**: yeni görevleri öğrenirken eski görevlerdeki performans ciddi şekilde düşüyor |
| Sadece ICL / uzun context kullanmak | Bilgi sadece o anlık context penceresi boyunca yaşıyor; session bitince öğrenilen her şey kayboluyor |

Yani ya **eskiyi feda edip yeniyi öğreniyorsunuz**, ya da **yeniyi hiç öğrenmiyorsunuz**. Yazının iddiası, bu ikilemin yapısal bir çözümü olduğu.

## 2. Temel fikir: uyanık/sleep döngüsü

İnsan beyninin dinlenme sırasında belleği işlemesi (consolidation) örnek alınıyor. Model, sabit bir mimari içinde **periyodik bir yaşam döngüsü** kazanıyor: aktif etkileşim ile offline bellek konsolidasyonu arasında gidip geliyor.

Geleneksel ML'deki **training / inference ayrımı** burada yapay kabul ediliyor; continual (sürekli) öğrenen bir ajanın durmadan öğrenmesi ve uyum sağlaması gerekiyor.

**İki durum:**

1. **Active (wake) phase** — Model girdi alır, işler, etkileşir. Geçici bilgiyi **hızlı, kısa süreli bellek bileşenlerinde** (ör. in-context memory) tutar.
2. **Sleep phase** — Yeni girdi işlemeyi durdurur; iç hesaplamayı **bellek konsolidasyonu ve kendi kendini iyileştirme**ye yönlendirir.

> **Önemli sınırlandırma:** "sleep", "dreaming", "self-improvement" terimleri nörobilimden mecaz. Çerçeve tamamen **sabit bir model mimarisi ve insan tanımlı eğitim kuralları** içinde çalışıyor. Offline sleep fazında model, önceden ayrılmış (pre-allocated) **Mixture-of-Experts (MoE) memory block**larını açıyor ve ağırlıklarını **kendi ürettiği pratik örneklerle** güncelliyor. Yani kod tabanı ve tasarım değişmeden, deploy edilmiş bir modelin otomatik post-training döngüsü çalışıyor.

## 3. Sleep sürecinin iki aşaması

### 3.1 Memory consolidation (NREM uykusu benzetmesi)

Beyinde NREM yavaş-dalga uykusu kırılgan anıları soyutlama ve distilasyonla uzun süreli yapıya aktarır. Çerçeve bunu üç mekanizmayla taklit ediyor:

- **Parameter expansion & continuum memory hierarchy** — Catastrophic forgetting'i önlemek için model kapasitesini sleep sırasında dinamik büyütüyor. **MoE + Hope** mimarisi kullanılıyor: yeni açılan **low-rank expert**lar, kararlı **düşük frekanslı** memory block'lara entegre ediliyor. Böylece gelen bilgi eski parametrelere dokunmadan saklanıyor.
- **Knowledge seeding (yukarı yönlü distilasyon)** — Yüksek frekanslı katmanlardaki (in-context memory) kırılgan kısa süreli bellek, yeni genişletilmiş uzun süreli parametrelere taşınıyor. Araç: **Generalized Knowledge Distillation (GKD)** + RL tabanlı **imitation learning**:
  - **On-policy distillation:** genişletilmiş student model, kendi örneklediği diziler üzerinde üretilen teacher dağılımlarından token-bazlı geri bildirim alır.
  - **Learning to Imitate (LTI):** student, teacher üretimi metnin **prefix'lerini tamamlarken** semantik ve mutlak benzerliğe (**Levenshtein distance**) göre ödüllendirilir.
- **Synaptic pruning** — Konsolidasyon düşük frekanslı modüllere tamamlanınca yüksek frekanslı geçici parametreler **sıfırlanır**; sonraki aktif session'lar için kapasite boşalır.

> Not: yazı "iki temel teknik mekanizma" diyor ama madde işaretleri üç (expansion, seeding, pruning). Kaynak metindeki küçük bir tutarsızlık; pruning, consolidation'ın kapanış adımı olarak okunabilir.

### 3.2 Dreaming for self-improvement (REM uykusu benzetmesi)

REM sırasında beyin senaryoları simüle eder, semantik bağlantıları pekiştirir, yeni yollar dener. "Dreaming stage"de otonom bir self-improvement döngüsü var:

- **Random expert selection** — Sentetik veri üretiminde MoE router sıradışı expert'ları rastgele aktive eder. Model, farklı bilgi alanlarını karıştırmaya ve latent pattern'ler çıkarmaya zorlanır.
- **Curated rehearsal** — Model sentetik eğitim örnekleri ("dreams") üretir ve **gradient tabanlı veri seçimi** ile filtreler; model yeteneğini en çok artırma potansiyeli olan örnekler öne çıkar.
- **RL refinement** — En yüksek puanlı dream örnekleri üzerinde SFT yapılır; üretimin downstream görevlerde gerçekten iyileştirip iyileştirmediği ölçülür.

## 4. Teorik temel

Yazının iddiası sadece ampirik değil: iki aşamanın neden "yeni bilgi karşılığında eski yeteneği vermek" olmadan continual learning'i destekleyebileceği gerekçelendiriliyor.

**Consolidation tarafı:** knowledge seeding, **yapılandırılmış, low-rank bir sıkıştırma problemi** olarak modelleniyor. Model kısa süreli izlerinin her detayını kopyalamıyor; hem kendi on-policy davranışını hem de hedefin ödüllendirdiği semantik nitelikleri en iyi koruyan **kompakt bir yön (direction) kümesi** buluyor. Buradan iki sinyalin tamamlayıcılığı çıkıyor:

- **on-policy distillation** → konsolidasyonu, modelin inference anında gerçekten üreteceği trajectory'lere hizalar; sadece teacher dağılımı üzerinde eğitimden doğan mismatch'i azaltır.
- **semantic reward** → neyin saklanmaya değer olduğunu değiştirir; token olasılıklarını eşleştirmek yerine anlamı ve görev-ilişkili davranışı koruyan yönleri önceler.
- **parameter expansion** → bu seçilmiş anıların, önceki konsolide bilgilerin üzerine yazmadan yazılacak kapasiteyi sağlar.

**Dreaming tarafı:** rastgele expert aktivasyonu ve reward tabanlı örnek seçimi, **öğrenme geometrisini iyileştirme** mekanizmaları olarak karakterize ediliyor. Farklı MoE'leri devreye sokmak, sentetik üretim sırasında keşfedilen gradient yönlerinin kümesini genişletiyor — salt wake-time verinin ürettiğinden daha zengin bir güncelleme havuzu. Gradient tabanlı filtreleme ise en faydalı öğrenme sinyalini taşıyan sentetik deneyleri öne çıkarıyor: bilgilendirici yönleri güçlendirip gradient uzayının yapısını koruyor.

Sonuç daha dengeli bir optimizasyon manzarası: öğrenme az sayıda yöne hakimiyet etmiyor, gradient'ler daha iyi koşullu (better conditioned), ilerleme daha kararlı. Yazının vurgusu: **dreaming, kendi kendine veri artırmak (data augmentation) değil; güncellemeleri çeşitlendirme, seçme ve konsolide etme süreci.**

## 5. Deneysel sonuçlar

> Blogda bu paragrafların üstünde ayrı bir "Sonuçlar/Deneyler" başlığı yok — "Theoretical foundations" altında akıyor. Aşağıdaki tablolaştırma bu dosya için yapıldı.

| Alan | Benchmark / ayar | Sleep framework | Kıyaslanan baseline'lar |
|---|---|---|---|
| **Continual translation** | Görülmemiş diller: **Manchu**, **Kalamang**; çok aşamalı (multi-stage) sıralı öğrenme | **near-zero performans düşüşü** (catastrophic forgetting yok) | ICL, Cartridges, SFT → sıralı öğrenmede ağır catastrophic forgetting |
| **Uzun context anlama** | **BABILong** | **10M token'a kadar near-perfect scaling**; uzun context bilgisini doğrudan parametrik belleğe sıkıştırıyor | GPT-4, Titans |
| **Faktik bilgi ekleme** | **SQuAD** + continued pre-training (CPT) | **%48.9** (single-task), **%46.2** (CPT) | base model, SFT, SEAL (Self-Adapting LMs) — yeni bilgiyi mevcut temsilleri bozmadan entegre ediyor |
| **Matematiksel muhakeme** | **AIME 24**, **AIME 25**, **HMMT 25** | **%79.2**, **%69.0**, **%46.1** (sleep-enhanced base model) | SFT, GRPO, OPSD (on-policy self-distillation) — consolidation + imitation learning birlikte daha iyi |

## 6. Sonuç

Sleep paradigması, otonom ve sürekli öğrenen AI sistemlere bir yol sunuyor: statik pre-training ve kırılgan context penceresinin ötesine geçip **aktif öğrenme ile offline dinlenme** arasında dönüşümlü çalışarak LLM'ler kısa süreli anıları saklıyor, yeni becerileri uzun süreli parametre yapısına soyutluyor ve catastrophic forgetting olmadan sürekli kendi kendini iyileştiriyor.

## 7. Analiz (blog yazısından değil, benim değerlendirmem)

**Güçlü tarafı:** Yazının asıl katkısı "MoE + distilasyon" kombinasyonundan çok, bunu **bilinen iki probleme ucuz bir yapısal cevap** olarak konumlandırması. Catastrophic forgetting'e karşı "kapasiteyi büyüt, eskiyi yazma" (parameter expansion / MoE routing) ve "kaynağı teacher'ın dağılımı değil, öğrencinin kendi trajectory'si yap" (on-policy GKD) ikilisi, literatürde ayrı ayrı kanıtlanmış iki fikrin bir periyodik döngüde birleştirilmesi. Teorik çerçevenin consolidation'ı "structured low-rank compression" olarak adlandırması da iddiayı test edilebilir kılıyor: gerçekten low-rank mi, hangi rank'ta ne kaybediliyor?

**Rakamlar hakkında temkinli olunması gerekenler:**
- %79.2 / %69.0 / %46.1 "sleep-enhanced **base-model**" için verilmiş. Baz modelin adı, boyutu ve wake fazında bu benchmark'lara ne kadar maruz kaldığı blogda yok — math reasoning'de bu farkın ne kadarının "daha iyi veri seçimi" ne kadarının "hesap bütçesi" olduğunu ayırmak mümkün değil.
- SQuAD'da %48.9 tek başına yüksek bir sayı değil; anlamı "base/SFT/SEAL'dan yüksek olması" ve "mevcut temsilleri bozmaması" kombinasyonunda. Blog, forgetting'i ölçen ikinci ekseni (eski görev performansı) sayı vermeden anlatıyor.
- "10M token'a kadar near-perfect scaling" iddiası en çarpıcısı ve en çok bağlam isteyen: BABILong'daki görev tipi ve baseline'ların context limiti kısıtları olmadan karşılaştırma yapılabilir değil.
- Benchmark seçimi lehine bir düzen var gibi görünüyor; "unseen language" çeviri, "bilinmeyen dil" = Manchu/Kalamang düşük kaynaklı diller — Hope mimarisinin doğal oyun alanı.

**Eksik kalan pratik sorular:** maliyet (sleep fazının compute/wall-clock bedeli, kaç wake başına bir sleep), periyotlamanın nasıl seçildiği, pruning'in geri alınamaz bilgi kaybı yaratıp yaratmadığı, router'ın rastgele expert seçiminin tutarlılığı nasıl garanti ettiği, ve güvenlik tarafı: kendi kendini üretilen veriyle eğiten bir döngüde modelin kendi hatalarının (veya wake fazında maruz kaldığı zararlı içeriğin) dream yoluyla pekişmesini kim filtreliyor? "Gradient tabanlı selection" sadece faydalı sinyale bakıyor; bu filtre bir güvenlik kontrolü değil.

**Kendi işimize tercümesi:** bizim gibi continual learning yapmayan, RAG/ICL ile çalışan sistemler için alınacak asıl fikir "context → parametre" transferi değil, **uyku fazını ayrı bir offline iş olarak modellere ayırmak**: biriktirilen oturum bilgisini periyodik olarak sıkıştırıp kalıcılaştıran, eski versiyonları bozmayan, sıfırlanabilir bir tampon katman. Bu, framework'ün tamamını kurmadan bile taklit edilebilir bir desen.

## 8. Bağlantılar

- Sleep framework makalesi: https://arxiv.org/abs/2606.03979
- Hope mimarisi / continual translation: https://arxiv.org/abs/2512.24695
- Cartridges: https://arxiv.org/abs/2506.06266
- SEAL (Self-Adapting LMs): https://arxiv.org/abs/2506.10943
- BABILong: https://arxiv.org/abs/2406.10149
- Generalized Knowledge Distillation (TRL): https://huggingface.co/docs/trl/en/gkd_trainer
- Mixture of Experts: https://huggingface.co/blog/moe
- Titans (Google Research): https://research.google/blog/titans-miras-helping-ai-have-long-term-memory/

**Teşekkürler (kaynaktan):** Çalışmayı inceleyenler Yuan Deng, Corinna Cortes. Blog yazımında yardım edenler Mark Simborg, Kimberly Schwede, Tim Herrmann.

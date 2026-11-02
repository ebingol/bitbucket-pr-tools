# Bitbucket PR Tools

Read-only PR activity reports and review candidates for Bitbucket Server / Data Center REST API 1.0. Not designed for Bitbucket Cloud.

## Kurulum

Python 3.10+ kurun. Ek Python paketi gerekmez. Bu repoyu clone edin veya Code → Download ZIP ile indirip acin.
`config.example.json` dosyasini `config.json` olarak kopyalayin ve `base_url` alanina kendi Bitbucket uygulama adresinizi yazin; varsa `/git` gibi context path de dahil olmali.
`config.json` Git tarafindan ignore edilir. Token veya parola bu dosyaya yazilmaz.

```json
{
  "base_url": "https://bitbucket.example.org",
  "output": "reports"
}
```

Opsiyonel alanlar: `months` (YYYY-MM listesi), `ca_bundle` (CA PEM yolu), `cookie_file` (Netscape cookie dosyasi). Dosya yollari config dosyasinin konumuna gore cozulur.
`PR_TOOLS_CONFIG` farkli config dosyasi secer. `BITBUCKET_BASE_URL` config sunucu adresini, `--base-url` ise ikisini de ezer. Diger CLI secenekleri config degerlerini ezer.
Varsayilan rapor ayi makinenin mevcut ayidir; olaylar ve ay sinirlari UTC+03:00 ile hesaplanir.

## Windows

`stats.cmd` veya `ready.cmd` calistirin. Alternatif olarak CMD/PowerShell:

```bat
py -3 launcher.py stats --months 2026-09 2026-10
py -3 launcher.py ready --limit 3
```

## Mac / Linux

```sh
python3 launcher.py stats --months 2026-09 2026-10
python3 launcher.py ready --limit 3
```

Mac icin `stats.command` / `ready.command` baslaticilari da vardir. ZIP sonrasi gerekirse Terminal'de `chmod +x stats.command ready.command` kullanin.
Herkes kendi Bitbucket kullanici adi/e-postasi ve kendi read yetkili personal access token'i ile giris yapar. Token terminalde gizli sorulur. Codex veya Claude gerekmez.

## Raporlar

`stats`: her ay ve birlesik donem icin yorumlu PR, sadece review yapilan PR, formal review, benzersiz incelenen PR ve yorum/yanit sayilari. APPROVED, REVIEWED, UNAPPROVED aktivite kayitlari kullanilir; sadece reviewer atanmak sayilmaz. Kendi PR yorumlari dahildir.
`ready`: acik, taslak olmayan, sizin acmadiginiz ve gorunen gecmisinde yorumunuz olmayan PR'lari acilis tarihine gore listeler. Onceki approve tek basina adayi elemez. Hata bulundugu veya merge hazirligi anlamina gelmez.

Erisilebilir tum repolar ve ilgili aktivite sayfalari okunur. Raporlar Excel, CSV ve JSON olarak `reports/` altina yazilir. Silinen/gizlenen aktiviteler tam geri getirilemez; rapor resmi audit kaydi degildir. Tarama anlik snapshot degildir. Eksik taramada hata listesi ve `complete=false` uretilir; sayilar alt sinirdir. Exit code: 0 tamamlandi, 1 yapilandirma/kimlik/ag/iptal, 2 eksik tarama.

## Devam ve ayarlar

```sh
python3 launcher.py stats --months 2026-10 --workers 4
python3 launcher.py stats --months 2026-10 --resume reports/stats_...
python3 launcher.py ready --base-url https://bitbucket.example.org --ca-bundle /path/to/ca.pem
python3 launcher.py stats --verify-pr PROJECT/REPO#123 --expected-comments 1
```

`--workers` 1-8, varsayilan 4. Resume ayni sunucu/kullanici/mod/ay ile kayitli taramayi surdurur; yeni veriler icin fresh tarama yapin. `--help` tum secenekleri listeler.
Kurum agi/VPN ve kurum CA sertifikasi gerekebilir. TLS dogrulamasi aciktir; yonlendirmeler reddedilir. Mac sistem curl, diger sistemler Python HTTPS tasimasi kullanir. Cookie yalnizca acikca yapilandirildiginda yuklenir. SSO akisinin ortaminiza uygunlugunu test edin.

Token, cookie, yerel config, sertifika ve raporlari repoya eklemeyin. Raporlar sunucu URL'leri ve kullanici/PR bilgileri icerebilir. Yalnizca GET istekleri yapilir; approve, merge veya yorum yazilmaz.
`stash_batch.py` eski dashboard komutlarini da korur; bunlarin rapor kapsami stats/ready ile ayni degildir. Ekip icin onerilen giris `launcher.py`'dir.

## Dogrulama

```sh
python3 -m unittest discover -s . -v
```

Yerel mock testleri vardir. Gercek Windows makinesi ve kurum sunucularinda uctan uca test ayrica gereklidir.

#!/usr/bin/env python3
"""Read-only, resumable Bitbucket PR activity report. Python 3.9+, stdlib only."""
import argparse
import collections
import csv
import datetime as dt
import getpass
import hashlib
import html
import http.cookiejar
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

TZ = dt.timezone(dt.timedelta(hours=3))
REVIEW = {'APPROVED', 'NEEDS_WORK', 'REVIEWED'}

def atomic(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)

def identity(pr):
    repo = pr['toRef']['repository']
    return repo['project']['key'], repo['slug'], pr['id']

def stamp(ms):
    return dt.datetime.fromtimestamp(ms/1000, TZ).isoformat(timespec='seconds')

class SameOrigin(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            raise RuntimeError('API başka bir sunucuya yönlendi; oturumunu yenile.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)

class API:
    def __init__(self, base, cookie_file, token):
        self.base = base.rstrip('/')
        jar = http.cookiejar.MozillaCookieJar()
        if cookie_file:
            text = Path(cookie_file).read_text()
            lines=[]
            for line in text.splitlines():
                fields=line.split('\t')
                if len(fields)==7 and fields[4]=='0':
                    fields[4]=''  # curl's 0 denotes a session cookie, not 1970 expiry.
                    line='\t'.join(fields)
                lines.append(line)
            normalized_text='\n'.join(lines)+'\n'
            # The corporate SSO export omits the standard Netscape header.
            if normalized_text != text or not text.startswith(('# Netscape HTTP Cookie File', '# HTTP Cookie File')):
                rows = [line for line in text.splitlines() if line.strip() and not line.startswith('#')]
                if not rows or any(len(line.split('\t')) != 7 for line in rows):
                    raise RuntimeError('Cookie dosyası geçerli yedi sütunlu Netscape cookie formatında değil.')
                fd, normalized = tempfile.mkstemp(prefix='stash-cookie-', suffix='.txt')
                try:
                    with os.fdopen(fd, 'w') as f:
                        f.write('# Netscape HTTP Cookie File\n' + normalized_text)
                    jar.load(normalized, ignore_discard=True, ignore_expires=False)
                finally:
                    Path(normalized).unlink(missing_ok=True)
            else:
                jar.load(cookie_file, ignore_discard=True, ignore_expires=False)
        self.opener = urllib.request.build_opener(SameOrigin(), urllib.request.HTTPCookieProcessor(jar))
        self.token = token
        self.cookie_file = cookie_file
        self.curl = '/usr/bin/curl' if Path('/usr/bin/curl').exists() else shutil.which('curl')

    def native_get(self, url):
        # Use OS certificate trust; send token through stdin, not process args.
        cmd=[self.curl,'--silent','--show-error','--max-time','30','--write-out','\n%{http_code}','--header','Accept: application/json','--config','-',url]
        if self.cookie_file:cmd+=['--cookie',self.cookie_file]
        config=''
        if self.token:
            if any(c in self.token for c in '\r\n'):raise RuntimeError('Token tek satır olmalı.')
            value=('Authorization: Bearer '+self.token).replace('\\','\\\\').replace('"','\\"')
            config='header = "'+value+'"\n'
        result=subprocess.run(cmd,input=config,text=True,capture_output=True)
        if result.returncode:
            reasons={6:'DNS/VPN erişimi yok',7:'Sunucuya bağlanılamadı',28:'İstek zaman aşımına uğradı',60:'İşletim sistemi sertifika doğrulaması başarısız'}
            raise RuntimeError(reasons.get(result.returncode,f'curl bağlantı hatası ({result.returncode})'))
        body,_,code=result.stdout.rpartition('\n')
        status=int(code)
        if status in (401,403):raise RuntimeError(f'API erişimi HTTP {status}: token, repo okuma yetkisi ve oturum dosyasını kontrol et.')
        if 300<=status<400:raise RuntimeError('Stash girişe yönlendirdi; güncel cookie dosyası/SSO oturumu gerekli.')
        if status!=200:raise RuntimeError(f'API isteği HTTP {status} ile başarısız.')
        try:page=json.loads(body)
        except json.JSONDecodeError:raise RuntimeError('API yerine giriş sayfası döndü; Stash oturumunu yenile.') from None
        if 'values' not in page or 'isLastPage' not in page:raise RuntimeError('API beklenen sayfalı veri formatını döndürmedi.')
        return page

    def get(self, path, query):
        url = self.base + '/rest/api/latest/' + path + '?' + urllib.parse.urlencode(query)
        if self.curl:
            for attempt in range(4):
                try:return self.native_get(url)
                except RuntimeError as error:
                    if attempt<3 and any(x in str(error) for x in ['HTTP 429','HTTP 5','zaman aşımı','bağlanılamadı']):
                        time.sleep(2**attempt);continue
                    raise
        headers = {'Accept': 'application/json'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers=headers, method='GET')
                with self.opener.open(req, timeout=30) as response:
                    if 'json' not in response.headers.get('Content-Type', ''):
                        raise RuntimeError('API yerine giriş sayfası döndü; Stash/VPN oturumunu ve cookie dosyanı yenile.')
                    page = json.load(response)
                if 'values' not in page or 'isLastPage' not in page:
                    raise RuntimeError('API beklenen sayfalı veri formatını döndürmedi.')
                return page
            except urllib.error.HTTPError as error:
                if error.code in (401, 403):
                    raise RuntimeError(f'API erişimi HTTP {error.code}: token, repo okuma yetkisi ve oturum dosyasını kontrol et.') from None
                if error.code == 429 or error.code >= 500:
                    if attempt < 3:
                        try: delay = min(float(error.headers.get('Retry-After', 2**attempt)), 30)
                        except ValueError: delay = 2**attempt
                        time.sleep(delay)
                        continue
                raise RuntimeError(f'API isteği HTTP {error.code} ile başarısız.') from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt < 3:
                    time.sleep(2**attempt)
                    continue
                raise RuntimeError('Stash ağına erişilemiyor; VPN/ağ bağlantısını kontrol et.') from None

    def pages(self, path, query):
        start, seen = 0, set()
        while True:
            if start in seen:
                raise RuntimeError('API sayfalama aynı başlangıç değerini tekrarladı.')
            seen.add(start)
            page = self.get(path, dict(query, start=start, limit=100))
            yield page
            if page['isLastPage']:
                break
            start = page['nextPageStart']

def discover(api, lo):
    found = {}
    # All three states, all pages: no dependency on dashboard ordering guarantees.
    for state in ('OPEN', 'MERGED', 'DECLINED'):
        for page in api.pages('dashboard/pull-requests', {'state': state}):
            for pr in page['values']:
                if pr['updatedDate'] >= lo:
                    found[identity(pr)] = pr
            print(f'Keşif: {state}, {len(found)} aday', flush=True)
    return list(found.values())

def summarize(pr, data, user, lo, hi, base):
    project, repo, pid = identity(pr)
    path = '/projects/' + urllib.parse.quote(project, safe='') + '/repos/' + urllib.parse.quote(repo, safe='') + f'/pull-requests/{pid}'
    url = base.rstrip('/') + path + '/overview'
    own = pr['author']['user']['name'].casefold() == user.casefold()
    comments, reviews = {}, {}
    def mine(person): return person.get('name', '').casefold() == user.casefold()
    def record(kind, rid, ms, link):
        return dict(project=project, repository=repo, PR=pid, title=pr['title'], date=stamp(ms), type=kind, record_id=rid, own_PR=own, url=link)
    def walk(c, reply=False):
        if mine(c.get('author', {})) and lo <= c.get('createdDate', 0) < hi:
            was_reply = comments.get(c['id'], {}).get('type') == 'COMMENT_REPLY'
            comments[c['id']] = record('COMMENT_REPLY' if reply or was_reply else 'COMMENT', c['id'], c['createdDate'], url + '?commentId=' + str(c['id']))
        for child in c.get('comments', []): walk(child, True)
    for a in data['values']:
        if a.get('comment'): walk(a['comment'])
        if mine(a.get('user', {})) and lo <= a.get('createdDate', 0) < hi and a['action'] in REVIEW | {'UNAPPROVED'}:
            reviews[a['id']] = record(a['action'], a['id'], a['createdDate'], url)
    records = list(comments.values()) + list(reviews.values())
    if not records: return None, []
    n = collections.Counter(r['type'] for r in records)
    row = dict(project=project, repository=repo, PR=pid, title=pr['title'], state=pr['state'], first_activity=min(r['date'] for r in records), last_activity=max(r['date'] for r in records), approvals=n['APPROVED'], needs_work=n['NEEDS_WORK'], review_completed=n['REVIEWED'], approval_withdrawn=n['UNAPPROVED'], comments=len(comments), replies=n['COMMENT_REPLY'], own_PR=own, url=url)
    return row, records

def report(out, meta, candidates):
    rows, events, audit, missing = [], [], [], []
    lo = dt.datetime.fromisoformat(meta['start']).timestamp()*1000
    hi = dt.datetime.fromisoformat(meta['end_exclusive']).timestamp()*1000
    for pr in candidates:
        p, r, i = identity(pr)
        source = out/'raw'/f'{p}--{r}--{i}.json'
        if not source.exists():
            missing.append(dict(project=p, repository=r, PR=i))
            continue
        data = json.loads(source.read_text())
        if not data.get('isLastPage'):
            missing.append(dict(project=p, repository=r, PR=i))
            continue
        row, records = summarize(pr, data, meta['user'], lo, hi, meta['base_url'])
        if row: rows.append(row)
        events.extend(records)
        audit.append(dict(project=p, repository=r, PR=i, SHA256=hashlib.sha256(source.read_bytes()).hexdigest(), activity_count=len(data['values'])))
    rows.sort(key=lambda r:r['last_activity'], reverse=True)
    events.sort(key=lambda r:(r['date'],r['repository'],r['PR'],r['record_id']))
    n = collections.Counter(e['type'] for e in events)
    keys = lambda types: {(e['project'],e['repository'],e['PR']) for e in events if e['type'] in types and not e['own_PR']}
    totals = dict(candidate_PRs=len(candidates), scanned_PRs=len(audit), missing_PRs=len(missing), reviewed_unique_PRs=len(keys(REVIEW)), review_or_comment_unique_PRs=len(keys(REVIEW|{'COMMENT','COMMENT_REPLY'})), approvals=n['APPROVED'], needs_work=n['NEEDS_WORK'], review_completed=n['REVIEWED'], approval_withdrawn=n['UNAPPROVED'], comments_including_replies=n['COMMENT']+n['COMMENT_REPLY'], replies=n['COMMENT_REPLY'], comments_on_own_PRs=sum(e['own_PR'] for e in events if e['type'] in {'COMMENT','COMMENT_REPLY'}))
    for filename, records, fields in [('pr-summary.csv',rows,['project','repository','PR','title','state','first_activity','last_activity','approvals','needs_work','review_completed','approval_withdrawn','comments','replies','own_PR','url']),('activity-evidence.csv',events,['project','repository','PR','title','date','type','record_id','own_PR','url']),('source-audit.csv',audit,['project','repository','PR','SHA256','activity_count'])]:
        with (out/filename).open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(records)
    atomic(out/'evidence.json',dict(meta,totals=totals,PRs=rows,events=events,sources=audit,missing=missing))
    esc=html.escape
    metrics = [('Review hareketi yapılan PR',totals['reviewed_unique_PRs']),('Review veya yorumla katılım yapılan PR',totals['review_or_comment_unique_PRs']),('Onay',n['APPROVED']),('Review tamamlama',n['REVIEWED']),('Değişiklik talebi',n['NEEDS_WORK']),('Onay geri çekme',n['UNAPPROVED']),('Yorum ve yanıt',totals['comments_including_replies']),('Kendi PR yorumları (toplama dahil)',totals['comments_on_own_PRs'])]
    tr=''.join('<tr>'+''.join('<td>'+esc(str(x))+'</td>' for x in [r['repository'],r['PR'],r['title'],r['last_activity'],r['approvals'],r['review_completed'],r['needs_work'],r['comments'],r['replies'],'Evet' if r['own_PR'] else 'Hayır'])+f'<td><a href="{esc(r["url"])}">Stash</a></td></tr>' for r in rows)
    status='Tamamlandı' if not missing and meta.get('discovery_complete') else 'Kısmi — kapsam tamamlanmadı'
    doc=f'''<!doctype html><html lang="tr"><meta charset="utf-8"><title>Stash Review Raporu</title><style>body{{font:15px Arial;max-width:1400px;margin:32px auto;padding:0 20px;color:#17243b}}table{{border-collapse:collapse;width:100%;font-size:12px}}th,td{{border:1px solid #ccd4df;padding:7px;text-align:left}}th{{background:#edf2f7}}tr:nth-child(even){{background:#f8fafc}}@media print{{body{{margin:0}}thead{{display:table-header-group}}}}</style><h1>Stash Review ve Yorum Raporu</h1><p>Hesap: <b>{esc(meta['user'])}</b><br>Dönem: {esc(meta['start'])} ≤ işlem tarihi &lt; {esc(meta['end_exclusive'])}<br>Durum: {status}<br>Tarama: {len(audit)}/{len(candidates)} PR</p><ul>{''.join(f'<li>{esc(k)}: <b>{v}</b></li>' for k,v in metrics)}</ul><p>Review: başkasının PR'sinde APPROVED, NEEDS_WORK veya REVIEWED hareketi. REVIEWED review tamamlama olarak ayrı sayılır. Yorumlar ve yanıtlar kimlikle tekilleştirilir; kendi PR yorumları toplamda ayrıca belirtilir. PR açma, merge ve reviewer ataması sayılmaz. Dönem işlem tarihine göre belirlenir.</p><p>Kapsam: kimliği doğrulanan hesabın dashboard'unda OPEN, MERGED ve DECLINED PR'ler. Son dönemde güncellenen PR'lerin tüm activity sayfaları taranır. Dashboard dışında kalan/katılımı kaldırılan PR'ler ve silinmiş yorumlar doğrulanamaz. Rapor kurum tarafından imzalanmış resmi denetim çıktısı değildir.</p><p>İşlem kimlikleri ve bağlantılar: activity-evidence.csv. PR listesi: pr-summary.csv. Kaynak özetleri: source-audit.csv.</p><table><thead><tr>{''.join('<th>'+esc(x)+'</th>' for x in ['Repository','PR','Başlık','Son hareket','Onay','Review tamamlandı','Değişiklik talebi','Yorum','Yanıt','Kendi PR','Bağlantı'])}</tr></thead><tbody>{tr}</tbody></table></html>'''
    (out/'report.html').write_text(doc,encoding='utf-8')
    with zipfile.ZipFile(out/'stash-evidence.zip','w',zipfile.ZIP_DEFLATED) as z:
        for name in ['report.html','pr-summary.csv','activity-evidence.csv','source-audit.csv','evidence.json']:z.write(out/name,name)
    return totals

def run(args):
    out=Path(args.output).expanduser().resolve();out.mkdir(parents=True,exist_ok=True)
    os.chmod(out,0o700)
    (out/'raw').mkdir(exist_ok=True)
    manifest=out/'run.json'
    if manifest.exists():
        meta=json.loads(manifest.read_text())
        if meta['user'].casefold()!=args.user.casefold() or meta['base_url']!=args.base_url.rstrip('/'):
            raise RuntimeError('Bu çıktı klasörü farklı hesap/sunucu için oluşturulmuş; yeni klasör seç.')
    else:
        end=dt.datetime.fromisoformat(args.end) if args.end else dt.datetime.now(TZ)
        if end.tzinfo is None:raise RuntimeError('--end saat dilimi içermeli; örnek: 2026-10-07T11:36:00+03:00')
        meta=dict(user=args.user,base_url=args.base_url.rstrip('/'),start=(end-dt.timedelta(days=args.days)).isoformat(),end_exclusive=end.isoformat(),discovery_complete=False)
        atomic(manifest,meta)
    status=out/'status.json'
    atomic(status,dict(state='running',updated_at=dt.datetime.now(TZ).isoformat()))
    token=os.environ.get('STASH_TOKEN','')
    if args.prompt_token:token=getpass.getpass('Stash HTTP access token (ekranda görünmez): ')
    lo=dt.datetime.fromisoformat(meta['start']).timestamp()*1000
    cp=out/'candidates.json'
    try:
        api=API(args.base_url,args.cookie_file,token)
        if meta['discovery_complete'] and cp.exists():candidates=json.loads(cp.read_text())
        else:
            candidates=discover(api,lo);atomic(cp,candidates)
            meta['discovery_complete']=True;atomic(manifest,meta)
        failures=[]
        for index,pr in enumerate(candidates,1):
            p,r,i=identity(pr);dest=out/'raw'/f'{p}--{r}--{i}.json'
            if dest.exists() and json.loads(dest.read_text()).get('isLastPage'):continue
            try:
                path=f'projects/{urllib.parse.quote(p,safe="")}/repos/{urllib.parse.quote(r,safe="")}/pull-requests/{i}/activities'
                values=[]
                for page in api.pages(path,{}):values.extend(page['values'])
                atomic(dest,dict(size=len(values),isLastPage=True,values=values))
                print(f'PR {index}/{len(candidates)}: {r} #{i}',flush=True)
            except RuntimeError as error:
                failures.append(dict(project=p,repository=r,PR=i,error=str(error)))
                print(f'PR {index}/{len(candidates)}: başarısız ({error})',flush=True)
                if 'HTTP 401' in str(error):break
            atomic(status,dict(state='running',completed=sum(1 for _ in (out/'raw').glob('*.json')),total=len(candidates),failures=failures,updated_at=dt.datetime.now(TZ).isoformat()))
        totals=report(out,meta,candidates)
        atomic(status,dict(state='complete' if totals['missing_PRs']==0 else 'partial',totals=totals,failures=failures,updated_at=dt.datetime.now(TZ).isoformat()))
        print(json.dumps(totals,ensure_ascii=False),flush=True)
        return 0 if not totals['missing_PRs'] else 2
    except Exception as error:
        atomic(status,dict(state='failed',error=str(error),updated_at=dt.datetime.now(TZ).isoformat()))
        raise

def main():
    # Interactive Windows reports share the all-repository scanner.
    if len(sys.argv) > 1 and sys.argv[1] in {"stats", "ready"}:
        import pr_tools
        return pr_tools.main(sys.argv[1:])
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['run','start','status','import-legacy'])
    p.add_argument('--base-url',default=__import__('settings').load_settings().get('base_url', ''))
    p.add_argument('--user',required=True,help='Token/oturum sahibinin Stash kullanıcı adı')
    p.add_argument('--days',type=int,default=90)
    p.add_argument('--end')
    p.add_argument('--cookie-file',help='VPN/SSO kapısı için mevcut Netscape cookiefile.txt')
    p.add_argument('--prompt-token',action='store_true')
    p.add_argument('--output',required=True)
    p.add_argument('--legacy-dir',help='Mevcut 30 günlük raporun klasörü (regresyon kontrolü)')
    args=p.parse_args()
    if args.days<1:p.error('--days pozitif olmalı')
    if urllib.parse.urlsplit(args.base_url).scheme!='https':p.error('--base-url HTTPS olmalı')
    out=Path(args.output).expanduser().resolve()
    if args.command=='status':
        print((out/'status.json').read_text() if (out/'status.json').exists() else 'Henüz başlatılmamış.');return 0
    if args.command=='start':
        if (out/'pid').exists():
            try:os.kill(int((out/'pid').read_text()),0)
            except (ProcessLookupError,ValueError):pass
            else:raise RuntimeError('Bu çıktı klasörü için çalışan bir batch var.')
        token=os.environ.get('STASH_TOKEN','')
        if args.prompt_token:token=getpass.getpass('Stash HTTP access token (ekranda görünmez): ')
        out.mkdir(parents=True,exist_ok=True);os.chmod(out,0o700)
        env=os.environ.copy()
        if token:env['STASH_TOKEN']=token
        cmd=[sys.executable,str(Path(__file__).resolve()),'run','--user',args.user,'--days',str(args.days),'--output',str(out),'--base-url',args.base_url]
        if args.cookie_file:cmd+=['--cookie-file',str(Path(args.cookie_file).expanduser().resolve())]
        if args.end:cmd+=['--end',args.end]
        with (out/'batch.log').open('a') as log:
            proc=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=log,stderr=log,env=env,start_new_session=True)
        (out/'pid').write_text(str(proc.pid))
        print(f'Arka plan PID: {proc.pid}\nLog: {out}/batch.log\nDurum: {out}/status.json');return 0
    if args.command=='import-legacy':
        old=Path(args.legacy_dir);out.mkdir(parents=True,exist_ok=True);(out/'raw').mkdir(exist_ok=True)
        prior=json.loads((old/'evidence.json').read_text())
        candidates=[]
        with zipfile.ZipFile(old/'raw-api-internal.zip') as z:
            for name in ['stash-merged.json','stash-merged-100.json','stash-merged-200.json','stash-open.json','stash-declined.json']:
                candidates+=json.loads(z.read(name))['values']
            lo=dt.datetime.fromisoformat(prior['start']).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()*1000
            candidates=list({identity(pr):pr for pr in candidates if pr['updatedDate']>=lo}.values())
            for pr in candidates:
                project,repo,pid=identity(pr)
                atomic(out/'raw'/f'{project}--{repo}--{pid}.json',json.loads(z.read(f'stash-act-{project}-{repo}-{pid}.json')))
        meta=dict(user=args.user,base_url=args.base_url,start=prior['start'],end_exclusive=prior['end_exclusive'],discovery_complete=True)
        atomic(out/'run.json',meta);atomic(out/'candidates.json',candidates)
        totals=report(out,meta,candidates)
        for key,value in prior['totals'].items():
            if totals.get(key)!=value:raise RuntimeError(f'Regresyon farkı: {key}: {totals.get(key)} != {value}')
        print('30 günlük doğrulanmış raporla tüm toplamlar aynı.');return 0
    return run(args)

if __name__=='__main__':
    try:sys.exit(main())
    except (RuntimeError,ValueError,OSError,KeyError,json.JSONDecodeError,KeyboardInterrupt,EOFError) as error:
        print(f'Hata: {error}',file=sys.stderr);sys.exit(1)

"""Bounded parallel scanner with atomic per-PR checkpoints; no credentials persisted."""
import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


def atomic_json(path, data):
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


@contextmanager
def run_lock(folder):
    stream = (Path(folder) / 'scan.lock').open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            if not stream.read(1):
                stream.write(b'0'); stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise RuntimeError('Bu rapor klasorunde baska tarama calisiyor; yeni tarama icin --fresh kullanin.') from None
    try:
        yield
    finally:
        if os.name == 'nt':
            stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


def encoded(record):
    data = dict(record)
    for kind in ('comments', 'reviews'):
        data[kind] = {key: value.isoformat() for key, value in record[kind].items()}
    return data


def decoded(data):
    data = dict(data)
    for kind in ('comments', 'reviews'):
        data[kind] = {key: datetime.fromisoformat(value) for key, value in data[kind].items()}
    return data


def scan(client, repos, user, mode, errors, *, out=None, months=None, workers=1, verbose=False):
    from pr_tools import ScanError, extract_events, same_user, summarize
    months = months or ['2026-09', '2026-10']
    records, local = [], threading.local()
    done = restored = failed = 0
    started, last_log = time.monotonic(), 0
    if out:
        out = Path(out)
        (out/'checkpoint').mkdir(exist_ok=True)

    def worker_client():
        if workers == 1:
            return client
        if not hasattr(local, 'client'):
            local.client = client.worker_copy()
        return local.client

    def checkpoint(kind, key):
        digest = hashlib.sha256(key.encode()).hexdigest()
        return out/'checkpoint'/f'{kind}-{digest}.json' if out else None

    def progress(repo_index, pr_done, pr_total, force=False):
        nonlocal last_log
        now = time.monotonic()
        if not force and now-last_log < 2:
            return
        last_log = now
        sums = summarize(records, months)
        totals = ' | '.join(f"{r['period']}: P{r['commented_prs']}/R{r['review_only_prs']}/Y{r['comments']}" for r in sums[:-1])
        msg = f'Repo {repo_index}/{len(repos)} PR {pr_done}/{pr_total} OK:{done} cache:{restored} hata:{failed} | {totals} | {int(now-started)}sn'
        print(('\r\033[2K' if sys.stderr.isatty() else '')+msg, end='' if sys.stderr.isatty() else '\n', file=sys.stderr, flush=True)
        if out:
            atomic_json(out/'progress.json', {'state':'running','completed':done,'restored':restored,'errors':failed,'summary':sums,'repo':repo_index,'repo_total':len(repos)})

    def fetch(project, slug, path, pr):
        comments, reviews, history = extract_events(worker_client().pages(f"{path}/{pr['id']}/activities"), user)
        return {'key':f"{project}/{slug}#{pr['id']}", 'project':project,'repo':slug,'pr':pr,
                'comments':comments,'reviews':reviews,'has_comment_history':history}

    print("Canli sayim: P=yorumlu PR, R=sadece review PR, Y=yorum/yanit. Sayilar tarama bitene kadar gecicidir.", file=sys.stderr, flush=True)
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        for index, repo in enumerate(repos,1):
            project, slug = repo['project']['key'], repo['slug']
            from urllib.parse import quote
            path=f"projects/{quote(project,safe='')}/repos/{quote(slug,safe='')}/pull-requests"
            cache = checkpoint('repo',f'{project}/{slug}')
            try:
                if cache and cache.exists():
                    prs = json.loads(cache.read_text(encoding='utf-8'))
                else:
                    print(f'\n[{index}/{len(repos)}] {project}/{slug}: PR listesi...',file=sys.stderr,flush=True)
                    prs=list(client.pages(path,state='ALL' if mode=='stats' else 'OPEN',direction='INCOMING'))
                    if cache: atomic_json(cache,prs)
            except (ScanError, ValueError) as exc:
                errors.append(f'{project}/{slug}: {exc}');failed+=1;continue
            todo=[]
            for pr in prs:
                if mode=='ready' and (same_user(pr.get('author',{}).get('user',{}),user) or pr.get('draft')):
                    continue
                key=f"{project}/{slug}#{pr['id']}"
                dest=checkpoint('pr',key)
                if dest and dest.exists():
                    try:
                        record=decoded(json.loads(dest.read_text(encoding='utf-8')))
                        if record['key'] != key: raise ValueError('checkpoint key mismatch')
                        records.append(record);restored+=1
                        continue
                    except (ValueError,KeyError,TypeError):
                        print(f'\nBozuk ara kayit yeniden okunacak: {key}',file=sys.stderr)
                todo.append((pr,dest))
            iterator=iter(todo);pending={};pr_done=0
            def submit():
                for _ in range(workers-len(pending)):
                    try: pr,dest=next(iterator)
                    except StopIteration: break
                    pending[pool.submit(fetch,project,slug,path,pr)]=(pr,dest)
            submit()
            progress(index,pr_done,len(todo),True)
            while pending:
                completed,_=wait(pending,timeout=1,return_when=FIRST_COMPLETED)
                for future in completed:
                    pr,dest=pending.pop(future)
                    try:
                        record=future.result()
                        if dest: atomic_json(dest,encoded(record))
                        records.append(record);done+=1
                        # Log only matches in the requested months (or explicit verbose mode).
                        match=summarize([record],months)[-1]
                        if verbose or match['comments'] or match['formal_review_prs']:
                            print(f"\nBULUNDU {record['key']}: {match['comments']} yorum, {match['formal_review_prs']} review PR",file=sys.stderr,flush=True)
                    except (ScanError,ValueError,KeyError) as exc:
                        failed+=1;errors.append(f"{project}/{slug}#{pr['id']}: {exc}")
                        print(f'\nHATA {errors[-1]}',file=sys.stderr,flush=True)
                    pr_done+=1
                submit();progress(index,pr_done,len(todo))
            progress(index,pr_done,len(todo),True)
        if out: atomic_json(out/'progress.json', {'state':'partial' if errors else 'complete','completed':done,'restored':restored,'errors':errors,'summary':summarize(records,months)})
    except KeyboardInterrupt:
        if out: atomic_json(out/'progress.json', {'state':'interrupted','completed':done,'restored':restored,'resume':str(out.resolve()),'summary':summarize(records,months)})
        print(f'\nDurduruldu. Tamamlanan kayitlar korundu. Devam: --resume "{out}"',file=sys.stderr,flush=True)
        raise
    finally:
        pool.shutdown(wait=True,cancel_futures=True)
        print(file=sys.stderr)
    return records

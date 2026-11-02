#!/usr/bin/env python3
"""Read-only Bitbucket Server/Data Center PR reports. Python 3.10+, stdlib only."""
import argparse
import os
import http.cookiejar
from settings import load_settings
import csv
import getpass
import json
import ssl
import subprocess
import tempfile
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler

ISTANBUL = timezone(timedelta(hours=3))
REVIEW_ACTIONS = {"APPROVED", "REVIEWED", "UNAPPROVED"}
DEFAULT_URL = ""


class ScanError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ScanError("HTTP redirect blocked; check --base-url (SSO login is not a REST response).")


class Client:
    def __init__(self, base, token, ca_bundle=None, cookie_file=None):
        parts = urlsplit(base)
        if parts.scheme != "https" or not parts.netloc or parts.username or parts.query or parts.fragment:
            raise ScanError("--base-url must be an HTTPS application URL without credentials/query/fragment.")
        self.base = base.rstrip("/")
        self.token = token
        self.username = None
        self.cookie_file = str(Path(cookie_file).expanduser().resolve()) if cookie_file else None
        self.ca_bundle = ca_bundle
        self.native_curl = sys.platform == "darwin" and Path("/usr/bin/curl").is_file()
        if any(c in token for c in "\r\n"):
            raise ScanError("Token tek satir olmali.")
        context = ssl.create_default_context(cafile=ca_bundle)
        handlers = [NoRedirect(), HTTPSHandler(context=context)]
        if self.cookie_file:
            jar = http.cookiejar.MozillaCookieJar(self.cookie_file)
            jar.load(ignore_discard=True)
            from urllib.request import HTTPCookieProcessor
            handlers.append(HTTPCookieProcessor(jar))
        self.opener = build_opener(*handlers)

    def worker_copy(self):
        return Client(self.base, self.token, self.ca_bundle, self.cookie_file)

    def native_get(self, url, path):
        # macOS system curl uses the OS trust store; credentials go through stdin.
        value = ("Authorization: Bearer " + self.token).replace("\\", "\\\\").replace('"', '\\"')
        for attempt in range(3):
            with tempfile.TemporaryDirectory(prefix="stash-http-") as folder:
                headers = Path(folder) / "headers"
                cmd = ["/usr/bin/curl", "--disable", "--silent", "--show-error",
                       "--proto", "=https", "--connect-timeout", "10", "--max-time", "30",
                       "--header", "Accept: application/json", "--config", "-",
                       "--dump-header", str(headers), "--write-out", "\n%{http_code}", url]
                if self.cookie_file:
                    cmd += ["--cookie", self.cookie_file]
                if self.ca_bundle:
                    cmd += ["--cacert", self.ca_bundle]
                result = subprocess.run(cmd, input='header = "' + value + '"\n',
                                        capture_output=True, text=True)
                if result.returncode:
                    reasons = {5: "Proxy DNS hatasi", 6: "Sunucu DNS/VPN uzerinden cozumlenemiyor",
                               7: "Sunucuya baglanilamiyor (VPN/ag)", 28: "Baglanti zaman asimi",
                               60: "Mac sertifika deposu kurum sertifikasini dogrulayamadi"}
                    raise ScanError(reasons.get(result.returncode, f"curl baglanti hatasi: {result.returncode}"))
                body, _, status = result.stdout.rpartition("\n")
                if headers.exists():
                    for line in headers.read_text(errors="replace").splitlines():
                        if line.lower().startswith("x-ausername:"):
                            self.username = line.split(":", 1)[1].strip()
                if status == "200":
                    try:
                        return json.loads(body)
                    except ValueError:
                        raise ScanError("API JSON yerine giris sayfasi dondurdu; VPN/SSO oturumu gerekli.") from None
                if status in {"429", "502", "503", "504"} and attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                if status.startswith("3"):
                    raise ScanError("SSO oturumu kabul edilmedi. Guncel Netscape cookiefile dosyasini --cookie-file ile belirtin; varsa eski oturum suresi dolmus olabilir.")
                raise ScanError(f"HTTP {status}: {path}; token ve repo okuma yetkisini kontrol edin.")

    def get(self, path, params=None):
        url = self.base + "/rest/api/1.0/" + path.lstrip("/")
        if params:
            url += "?" + urlencode(params)
        if self.native_curl:
            return self.native_get(url, path)
        for attempt in range(3):
            req = Request(url, headers={"Authorization": "Bearer " + self.token,
                                       "Accept": "application/json"})
            try:
                with self.opener.open(req, timeout=30) as response:
                    self.username = response.headers.get("X-AUSERNAME") or self.username
                    try:
                        return json.load(response)
                    except (ValueError, UnicodeError):
                        raise ScanError("REST response is not JSON; check VPN, base URL and token.") from None
            except HTTPError as error:
                if error.code in {429, 502, 503, 504} and attempt < 2:
                    try:
                        delay = min(10, max(1, int(error.headers.get("Retry-After", "2"))))
                    except ValueError:
                        delay = 2
                    time.sleep(delay)
                    continue
                raise ScanError(f"HTTP {error.code}: {path} (check token/read permissions)") from None
            except (URLError, TimeoutError, OSError):
                if attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                raise ScanError(f"Network/TLS error: {path}; check VPN and corporate CA (--ca-bundle).") from None

    def pages(self, path, **params):
        start, seen = 0, set()
        while True:
            if start in seen:
                raise ScanError(f"Repeated pagination cursor: {path}")
            seen.add(start)
            if getattr(self, "verbose", False) and path.endswith("/pull-requests"):
                print(f"  PR listesi aliniyor: kayit {start}...", file=sys.stderr, flush=True)
            data = self.get(path, {**params, "start": start, "limit": 100})
            if not isinstance(data, dict) or not isinstance(data.get("values"), list):
                raise ScanError(f"Unexpected paginated response: {path}")
            yield from data["values"]
            if data.get("isLastPage") is True:
                break
            nxt = data.get("nextPageStart")
            if not isinstance(nxt, int) or nxt <= start:
                raise ScanError(f"Missing/invalid pagination cursor: {path}")
            start = nxt


def same_user(user, target):
    if user.get("id") is not None and target.get("id") is not None:
        return str(user["id"]) == str(target["id"])
    return any(user.get(k) and target.get(k) and
               str(user[k]).casefold() == str(target[k]).casefold()
               for k in ("name", "slug", "emailAddress"))


def resolve_user(client, login):
    # Prefer the authenticated identity, never derive an LDAP name from an email.
    authenticated_name = unquote(client.username or "")
    # Bitbucket may expose a synthetic PAT principal, not a resolvable user slug.
    technical_principal = authenticated_name.casefold().startswith("access-token-user/")
    if authenticated_name and authenticated_name.lower() != "anonymous" and not technical_principal:
        user = client.get("users/" + quote(authenticated_name, safe=""))
        if any(str(user.get(k, "")).casefold() == login.casefold()
               for k in ("name", "slug", "emailAddress")):
            return user
        raise ScanError("Entered uname does not match the token's authenticated user.")
    matches = [u for u in client.pages("users", filter=login)
               if any(str(u.get(k, "")).casefold() == login.casefold()
                      for k in ("name", "slug", "emailAddress"))]
    if len(matches) != 1:
        raise ScanError("Cannot resolve uname uniquely. Retry with your Bitbucket LDAP username instead of email.")
    return matches[0]


def timestamp(value):
    if not isinstance(value, (int, float)):
        raise ScanError("Missing event timestamp; refusing to report an inaccurate count.")
    return datetime.fromtimestamp(value / 1000, ISTANBUL)


def extract_events(activities, user):
    """Deduplicate comments (including replies) by PR-local comment ID."""
    comments, reviews, deleted = {}, {}, set()
    has_history = False

    def walk(comment):
        nonlocal has_history
        if "id" not in comment:
            raise ScanError("Comment without ID; cannot deduplicate safely.")
        if same_user(comment.get("author", {}), user):
            has_history = True
            comments[str(comment["id"])] = timestamp(comment.get("createdDate"))
        for reply in comment.get("comments", []):
            walk(reply)

    for activity in activities:
        comment = activity.get("comment")
        if comment:
            if activity.get("commentAction") == "DELETED":
                if same_user(comment.get("author", {}), user):
                    has_history = True
                deleted.add(str(comment["id"]))
            else:
                walk(comment)
        if activity.get("action") in REVIEW_ACTIONS and same_user(activity.get("user", {}), user):
            reviews[str(activity["id"])] = timestamp(activity.get("createdDate"))
    # An explicit delete event takes precedence over earlier snapshots.
    for cid in deleted:
        comments.pop(cid, None)
    return comments, reviews, has_history


def summarize(records, months):
    rows = []
    for month in [*months, "TOTAL"]:
        selected = lambda dt: dt.strftime("%Y-%m") in months if month == "TOTAL" else dt.strftime("%Y-%m") == month
        commented, reviewed, count = set(), set(), 0
        for record in records:
            cs = [v for v in record["comments"].values() if selected(v)]
            rs = [v for v in record["reviews"].values() if selected(v)]
            if cs:
                commented.add(record["key"])
            if rs:
                reviewed.add(record["key"])
            count += len(cs)
        rows.append({"period": month, "commented_prs": len(commented),
                     "review_only_prs": len(reviewed - commented),
                     "formal_review_prs": len(reviewed),
                     "all_reviewed_prs": len(commented | reviewed), "comments": count})
    return rows


def ready(record, user):
    pr = record["pr"]
    return (pr.get("state") == "OPEN" and not pr.get("draft", False)
            and not same_user(pr.get("author", {}).get("user", {}), user)
            and not record.get("has_comment_history", bool(record["comments"])))


def scan(client, repos, user, mode, errors, **kwargs):
    from scan_engine import scan as run_scan
    return run_scan(client, repos, user, mode, errors, **kwargs)


def verification(client, user, reference, minimum):
    try:
        repo, pid = reference.rsplit("#", 1)
        project, slug = repo.split("/", 1)
        if int(pid) < 1 or not project or not slug:
            raise ValueError()
    except ValueError:
        raise ScanError("--verify-pr PROJECT/REPO#ID biciminde olmali.") from None
    path = f"projects/{quote(project,safe='')}/repos/{quote(slug,safe='')}/pull-requests/{int(pid)}/activities"
    comments, reviews, history = extract_events(client.pages(path), user)
    if len(comments) < minimum:
        raise ScanError(f"Dogrulama BASARISIZ: {reference} icin en az {minimum} yorum beklenirken {len(comments)} bulundu. Ana tarama baslatilmadi.")
    print(f"Dogrulama BASARILI: {reference}, hesabiniza ait {len(comments)} benzersiz yorum.", flush=True)
    return {"status":"passed", "pr":reference,"minimum_comments":minimum,"found_comments":len(comments)}


def csv_file(path, rows, fields):
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            # Keep spreadsheet programs from interpreting external PR titles as formulas.
            writer.writerow({k: ("'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v)
                             for k, v in row.items()})


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    config = load_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["stats", "ready"])
    parser.add_argument("--base-url", default=config.get("base_url", DEFAULT_URL))
    parser.add_argument("--months", nargs="+", default=config.get("months", [datetime.now().strftime("%Y-%m")]))
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--output", default=config.get("output", "reports"))
    parser.add_argument("--cookie-file", default=config.get("cookie_file"), help="Explicit Netscape SSO cookie export")
    parser.add_argument("--ca-bundle", default=config.get("ca_bundle"), help="CA PEM file; TLS verification always enabled")
    restart = parser.add_mutually_exclusive_group()
    restart.add_argument("--fresh", action="store_true", help="New independent scan; old checkpoints are preserved (default)")
    restart.add_argument("--resume", metavar="REPORT_FOLDER", help="Resume a specific interrupted scan snapshot")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent activity requests, 1-8 (default 4)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--verify-pr", help="Known commented PR: PROJECT/REPO#ID")
    parser.add_argument("--expected-comments", type=int, default=None)
    args = parser.parse_args(argv)
    if not args.base_url:
        parser.error("Set base_url in config.json, BITBUCKET_BASE_URL or --base-url")
    if not 1 <= args.workers <= 8:
        parser.error("--workers 1 ile 8 arasinda olmali")
    if args.expected_comments is not None and args.expected_comments < 1:
        parser.error("--expected-comments pozitif olmali")
    if args.limit < 1:
        parser.error("--limit must be positive")
    try:
        for month in args.months:
            if datetime.strptime(month, "%Y-%m").strftime("%Y-%m") != month:
                raise ValueError()
    except ValueError:
        parser.error("--months must use YYYY-MM")
    args.months = list(dict.fromkeys(args.months))
    login = input("uname (e-posta veya Bitbucket kullanici adi): ").strip()
    if not login:
        raise ScanError("uname is required.")
    # Refuse getpass's echoing fallback when no secure terminal is available.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            token = getpass.getpass("Access token (gizli): ").strip()
        except getpass.GetPassWarning:
            raise ScanError("Run from an interactive CMD/terminal for hidden token entry.") from None
    if not token:
        raise ScanError("Token is required.")
    cookie = args.cookie_file
    if cookie:
        if not Path(cookie).expanduser().is_file():
            raise ScanError("Cookie dosyasi bulunamadi.")
        print("SSO cookie dosyasi: " + str(Path(cookie).expanduser()))
    client = Client(args.base_url, token, args.ca_bundle, cookie)
    # Global repository endpoint includes all projects and accessible personal repos.
    repos = list(client.pages("repos"))
    user = resolve_user(client, login)
    print(f"Hesap: {user.get('displayName', '')} ({user.get('name', user.get('slug'))})")
    print(f"Erisilebilir repo: {len(repos)}; proje: {len({r['project']['key'] for r in repos})}")
    if len(repos) == 1:
        print("UYARI: Bu kimlik dogrulamayla yalnizca 1 repo gorunuyor. Token kapsami dar olabilir; rapor tum kurum repolarini kapsamayabilir.", file=sys.stderr, flush=True)
    from scan_engine import atomic_json, run_lock
    reference = args.verify_pr
    minimum = args.expected_comments or 1
    check = verification(client, user, reference, minimum) if reference else {"status":"not_configured"}
    if not reference:
        print("Bilinen PR kontrolu yapilandirilmadi; isterseniz --verify-pr PROJECT/REPO#ID kullanin.")
    settings = {"schema":1, "base_url":client.base, "user_id":user.get("id"),
                "username":user.get("name"), "mode":args.mode,"months":args.months}
    if args.resume:
        out=Path(args.resume).expanduser().resolve()
        if not (out/"scan.json").is_file():
            raise ScanError("Devam klasorunde scan.json bulunamadi.")
        saved=json.loads((out/"scan.json").read_text(encoding="utf-8"))
        if saved["settings"] != settings:
            raise ScanError("Ara kayit hesap/sunucu/mod/donem ile uyusmuyor. Ayni parametreleri kullanin veya --fresh baslatin.")
        repos=saved["repos"]
        started=saved["started_at"]
        print("DEVAM: kaydedilen repo/PR kesfi ve tamamlanmis sonuclar kullanilacak; guncel bastan tarama icin --fresh.")
    else:
        out=Path(args.output).expanduser().resolve()/(args.mode+"_"+datetime.now(ISTANBUL).strftime("%Y%m%d_%H%M%S_%f"))
        out.mkdir(parents=True,exist_ok=False)
        started=datetime.now(ISTANBUL).isoformat()
        atomic_json(out/"scan.json", {"settings":settings,"repos":repos,"started_at":started})
    print(f"Tarama klasoru: {out}\nEszamanli istek: {args.workers}\nDevam etmek icin: --resume \"{out}\"", flush=True)
    errors=[]
    with run_lock(out):
        records=scan(client,repos,user,args.mode,errors,out=out,months=args.months,
                     workers=args.workers,verbose=args.verbose)
    metadata = {"started_at": started, "finished_at": datetime.now(ISTANBUL).isoformat(),
                "username": user.get("name"), "complete": not errors,
                "repos": len(repos), "scanned_prs": len(records), "errors": errors,
                "timezone": "UTC+03:00 (Istanbul)", "verification":check, "workers":args.workers, "resumed":bool(args.resume)}
    from excel_report import write_xlsx
    note = f"Hesap: {user.get('name')} | Istanbul UTC+03:00 | " + ("TAM TARAMA" if not errors else "EKSIK TARAMA: sayilar alt sinirdir; hata listesi report.json dosyasinda.")
    if args.mode == "stats":
        rows = summarize(records, args.months)
        print("\nDonem      Yorumlu PR   Sadece review PR   Tum review PR   Yorum")
        for row in rows:
            print(f"{row['period']:10} {row['commented_prs']:10} {row['review_only_prs']:18} {row['all_reviewed_prs']:15} {row['comments']:7}")
        csv_file(out / "summary.csv", rows, list(rows[0]))
        detail = []
        for record in records:
            for kind in ("comments", "reviews"):
                for event_id, date in record[kind].items():
                    if date.strftime("%Y-%m") in args.months:
                        detail.append({"pr": record["key"], "type": kind, "event_id": event_id,
                                       "date": date.isoformat()})
        csv_file(out / "events.csv", detail, ["pr", "type", "event_id", "date"])
        metadata["summary"] = rows
        names = {"2026-09": "Eylül 2026", "2026-10": "Ekim 2026", "TOTAL": "GENEL TOPLAM"}
        write_xlsx(out / "PR-raporu.xlsx", [
            ("Özet", "PR Review ve Yorum Raporu", note,
             ["Dönem", "Yorum yazılan PR", "Sadece review PR", "Formal review PR", "Review veya yorum PR", "Toplam yorum"],
             [[names.get(r["period"],r["period"]), r["commented_prs"],r["review_only_prs"],r["formal_review_prs"],r["all_reviewed_prs"],r["comments"]] for r in rows],
             [22,23,23,23,25,22]),
            ("İşlem detayları", "Sayımı Destekleyen İşlemler", "Yorumlar ve yanıtlar kimlikle tekilleştirilir. Düzenleme yeni yorum sayılmaz. Tarihler Istanbul saatidir.",
             ["PR", "İşlem türü", "İşlem kimliği", "İşlem tarihi"],
             [[r["pr"], "Yorum / yanıt" if r["type"] == "comments" else "Review",r["event_id"],r["date"]] for r in detail], [45,24,22,36])])
    else:
        candidates = sorted((r for r in records if ready(r, user)),
                            key=lambda r: (r["pr"]["createdDate"], r["key"]), reverse=True)[:args.limit]
        rows = []
        for record in candidates:
            pr = record["pr"]
            url = (client.base + f"/projects/{quote(record['project'], safe='')}/repos/"
                   f"{quote(record['repo'], safe='')}/pull-requests/{pr['id']}/overview")
            row = {"pr": record["key"], "title": pr["title"],
                   "created": timestamp(pr["createdDate"]).isoformat(), "url": url}
            rows.append(row)
            print(f"\n{row['pr']} | {row['title']}\n{row['created']}\n{url}")
        if not rows:
            print("Dogrulanabilen uygun acik PR bulunamadi.")
        csv_file(out / "ready.csv", rows, ["pr", "title", "created", "url"])
        metadata["candidates"] = rows
        write_xlsx(out / "Review-adaylari.xlsx", [
            ("Review adayları", "En Yeni Review Adayları", note,
             ["PR", "Başlık", "Açılış tarihi (Istanbul)", "PR bağlantısı"],
             [[r["pr"],r["title"],r["created"],r["url"]] for r in rows], [38,65,34,85])])
    (out / "report.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    if errors:
        print(f"\nEKSIK TARAMA: {len(errors)} hata. Sayilar alt sinirdir; adaylar kesin global ilk 3 degildir.", file=sys.stderr)
        for error in errors:
            print("- " + error, file=sys.stderr)
    print(f"\nRapor: {out.resolve()}")
    return 2 if errors else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ScanError, KeyboardInterrupt, EOFError) as error:
        print(f"\nDurduruldu: {error}", file=sys.stderr)
        raise SystemExit(1)

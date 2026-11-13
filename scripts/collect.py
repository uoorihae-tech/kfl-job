#!/usr/bin/env python3
"""
한국외국어대학교 KFL대학원 취업정보 게시판 - 자동 공고 수집기

GitHub Actions가 하루 두 번 이 파일을 실행합니다.
여러 채용 게시판에서 접수 중인 공고를 모아 posts.json 에 추가하고,
마감 후 14일이 지난 공고는 지웁니다.

한 사이트가 실패해도 나머지는 계속 수집합니다.
실행 기록(Actions 로그)에 사이트별 수집 건수가 찍힙니다.
"""
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, quote

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
POSTS_FILE = ROOT / "posts.json"
KST = timezone(timedelta(hours=9))
NOW = datetime.now(timezone.utc)
TODAY = NOW.astimezone(KST).date()
KEEP_DAYS_AFTER_DEADLINE = 14
MAX_POSTS = 800

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ---------------------------------------------------------------- 분류 규칙
EDU = re.compile(r"한국어\s*(강사|교원|교사|교육|선생|학당|과정|연수)|세종학당|사회통합\s*프로그램|어학당|한국어교육|KSL|KIIP|한국어\s*튜터")
TRANS = re.compile(r"통\s*번역|통역|번역|현지화|로컬라이(징|제이션)|localiz|interpret|translat|패러리걸|paralegal|에디터", re.I)
CULTURE = re.compile(r"문화원|문화예술|문화재단|학예|큐레이터|박물관|미술관|관광\s*통역|문화\s*해설|해설사|도슨트|한국학|한국문화|국어원|한글박물관|전통문화|K-?컬처|국제문화")
COMMON = re.compile(r"국제\s*교류|국제처|국제협력|국제교육원|글로벌\s*교육|유학생")
SKIP = re.compile(r"합격자|결과\s*(발표|공고|안내)|발표\s*안내|면접\s*(일정|전형\s*안내)|서류전형\s*(결과|합격)|친인척|채용\s*취소|일정\s*변경")
NOISE = re.compile(r"영어학원|수학|생산직|오퍼레이터|피부관리|조리|운전|미화|경비|물류|배송|용접|간호|요양")


def classify(text, default=None):
    if EDU.search(text):
        return "edu"
    if TRANS.search(text):
        return "trans"
    if CULTURE.search(text):
        return "culture"
    if COMMON.search(text):
        return "all"
    return default


def kind_of(text):
    if "인턴" in text:
        return "인턴"
    if re.search(r"공모|대회|콘테스트|공모전", text):
        return "공모·대회"
    if re.search(r"연수|교육생|아카데미|양성과정", text):
        return "교육·연수"
    return "채용"


# ---------------------------------------------------------------- 날짜 처리
DATE_FULL = re.compile(r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")
DATE_MD = re.compile(r"(?<!\d)(\d{1,2})\s*[./월]\s*(\d{1,2})\s*일?(?:\s*\([월화수목금토일]\))?")


def ymd(y, m, d):
    try:
        return datetime(int(y), int(m), int(d)).date()
    except ValueError:
        return None


def md_to_date(m, d):
    """연도 없는 월/일 → 가장 그럴듯한 연도"""
    dt = ymd(TODAY.year, m, d)
    if dt and (TODAY - dt).days > 60:  # 두 달 이상 지났으면 내년으로 본다
        dt = ymd(TODAY.year + 1, m, d)
    return dt


def all_full_dates(text):
    return [x for x in (ymd(*g) for g in DATE_FULL.findall(text)) if x]


def deadline_from_title(title):
    m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일[^)]*마감", title)
    if m:
        return md_to_date(*m.groups())
    m = re.search(r"~\s*(\d{1,2})\s*[./]\s*(\d{1,2})", title)
    if m:
        return md_to_date(*m.groups())
    return None


def iso(d):
    return d.isoformat() if d else ""


# ---------------------------------------------------------------- 공통 도구
session = requests.Session()
session.headers.update(HEADERS)


def get(url, **kw):
    r = session.get(url, timeout=25, **kw)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
        r.encoding = r.apparent_encoding
    time.sleep(1.0)  # 사이트에 부담을 주지 않도록 잠깐 쉼
    return BeautifulSoup(r.text, "html.parser")


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def table_rows(soup, base_url):
    """게시판 표(table)의 각 줄에서 (제목, 링크, 줄 전체 텍스트)를 뽑는다."""
    for tr in soup.select("tr"):
        anchors = [a for a in tr.find_all("a") if len(clean(a.get_text())) >= 6]
        if not anchors:
            continue
        a = max(anchors, key=lambda x: len(clean(x.get_text())))
        title = clean(a.get_text())
        href = a.get("href") or ""
        link = urljoin(base_url, href) if href and not href.lower().startswith(("javascript", "#")) else base_url
        cells = [clean(td.get_text(" ")) for td in tr.find_all(["td", "th"])]
        yield title, link, clean(tr.get_text(" ")), cells


def make(dept, title, org, deadline, location, link, content):
    return {
        "dept": dept, "kind": kind_of(title), "title": title[:150], "org": org[:60],
        "deadline": iso(deadline), "location": location[:40], "link": link,
        "content": content, "source": "auto",
    }


# ---------------------------------------------------------------- 사이트별 수집기
def src_nuri():
    """누리-세종학당 한국어 교원 모집"""
    url = "https://nuri.iksi.or.kr/front/bbs/korlecturer/layout71/list.do"
    out = []
    soup = get(url)
    for title, link, text, cells in table_rows(soup, url):
        if SKIP.search(title):
            continue
        dates = all_full_dates(text)
        dl = max(dates) if dates else deadline_from_title(title)
        org = ""
        for c in cells:
            if c and c != title and not DATE_FULL.search(c) and not c.isdigit() and len(c) <= 40:
                org = c
        out.append(make("edu", title, org, dl, "", link, "누리-세종학당 '한국어 교원 모집' 게시판 공고."))
    return out


def src_erits():
    """이화여대 통역번역연구소 채용공고"""
    base = "https://erits.ewha.ac.kr/erits/notice/employment.do"
    out = []
    for off in (0, 10, 20):
        url = base if off == 0 else f"{base}?mode=list&article.offset={off}"
        soup = get(url)
        for title, link, text, cells in table_rows(soup, base):
            if SKIP.search(title):
                continue
            t = re.sub(r"\s*N\s*$", "", title)
            dl = deadline_from_title(t)
            m = re.match(r"\[[^\]]+\]\s*([^\s(]+)", t)
            org = m.group(1) if m else ""
            out.append(make("trans", t, org, dl, "", link, "이화여대 통역번역연구소 채용게시판 공고."))
    return out


def src_korean_culture():
    """재외한국문화원 채용 (문화체육관광부)"""
    url = "https://www.korean-culture.org/recruitmentNoti.do"
    out = []
    soup = get(url)
    for title, link, text, cells in table_rows(soup, url):
        if SKIP.search(title):
            continue
        dates = all_full_dates(text)
        dl = max(dates) if dates else None
        m = re.match(r"(주\S+한국문화원)", title)
        org = m.group(1) if m else "재외한국문화원"
        loc = "해외"
        out.append(make("culture", title, org, dl, loc, link, "문화체육관광부 재외한국문화원 채용 게시판 공고 (마감일은 현지 기준)."))
    return out


def src_arko():
    """한국문화예술위원회 채용"""
    url = "https://www.arko.or.kr/board/list/4054"
    out = []
    soup = get(url)
    for title, link, text, cells in table_rows(soup, url):
        if SKIP.search(title):
            continue
        dates = all_full_dates(text)
        dl = dates[0] if len(dates) >= 2 else None  # 표 순서: 마감일, 등록일
        if len(dates) < 2:
            continue  # 마감일이 없는 글은 대개 결과·안내 글
        out.append(make("culture", title, "한국문화예술위원회(ARKO)", dl, "", link, "한국문화예술위원회 채용모집 게시판 공고."))
    return out


def src_mcst():
    """문화체육관광부 소속기관 채용 (박물관·국어원 등)"""
    url = "https://www.mcst.go.kr/site/s_notice/notice/jobList.jsp?pTab=02"
    out = []
    soup = get(url)
    for title, link, text, cells in table_rows(soup, url):
        if SKIP.search(title):
            continue
        dept = classify(title)
        if not dept:
            continue
        dates = all_full_dates(text)
        if len(dates) < 2:
            continue
        dl = max(dates)
        m = re.match(r"\[([^\]]+)\]", title)
        org = m.group(1) if m else "문화체육관광부 소속기관"
        out.append(make(dept, title, org, dl, "", link, "문화체육관광부 소속기관 채용정보 게시판 공고."))
    return out


def src_jinhakpro():
    """진학프로 강사 채용 (한국어 관련만)"""
    url = "https://www.jinhakpro.com/recruit/list?recruitTypeCode=L"
    out = []
    soup = get(url)
    for a in soup.find_all("a", href=re.compile(r"/recruit/\d+")):
        block = a.find_parent(["li", "tr", "div"]) or a
        text = clean(block.get_text(" "))
        title = clean(a.get_text(" "))
        if "한국어" not in title or SKIP.search(title):
            continue
        dates = all_full_dates(text)
        dl = max(dates) if dates else None
        m = re.match(r"\[([^\]]+)\]", title)
        org = m.group(1) if m else ""
        out.append(make("edu", title, org, dl, "", urljoin(url, a["href"]), "진학프로 강사 채용 공고."))
    return out


SARAMIN_KEYWORDS = ["한국어 강사", "한국어교원", "통번역", "번역가", "의료통역",
                    "관광통역안내사", "국제교류", "학예사", "한국문화", "현지화"]


def src_saramin():
    """사람인 검색 결과 (키워드별)"""
    out = []
    for kw in SARAMIN_KEYWORDS:
        url = "https://www.saramin.co.kr/zf_user/search/recruit?searchword=" + quote(kw) + "&recruitPageCount=40"
        try:
            soup = get(url)
        except Exception as e:  # noqa
            print(f"   - 사람인 '{kw}' 실패: {e}")
            continue
        n = 0
        for item in soup.select("div.item_recruit"):
            a = item.select_one("h2.job_tit a") or item.select_one(".job_tit a")
            c = item.select_one(".corp_name a") or item.select_one(".corp_name")
            if not a:
                continue
            title = clean(a.get("title") or a.get_text())
            org = clean(c.get_text()) if c else ""
            if SKIP.search(title) or NOISE.search(title):
                continue
            dept = classify(title)
            if not dept:
                continue
            date_txt = clean((item.select_one(".job_date .date") or item.select_one(".job_date") or item).get_text(" "))
            m = DATE_MD.search(date_txt)
            dl = md_to_date(*m.groups()) if m and "~" in date_txt else None
            if re.search(r"오늘\s*마감", date_txt):
                dl = TODAY
            elif re.search(r"내일\s*마감", date_txt):
                dl = TODAY + timedelta(days=1)
            cond = item.select(".job_condition span")
            loc = clean(cond[0].get_text(" ")) if cond else ""
            href = a.get("href") or ""
            rec = re.search(r"rec_idx=(\d+)", href)
            link = (f"https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx={rec.group(1)}"
                    if rec else urljoin("https://www.saramin.co.kr", href))
            out.append(make(dept, title, org, dl, loc, link, f"사람인 '{kw}' 검색 결과 공고."))
            n += 1
        print(f"   - 사람인 '{kw}': {n}건")
    return out


JOBKOREA_KEYWORDS = ["한국어강사", "통번역", "번역", "문화해설", "큐레이터"]


def src_jobkorea():
    """잡코리아 검색 결과 (키워드별)"""
    out = []
    for kw in JOBKOREA_KEYWORDS:
        url = "https://www.jobkorea.co.kr/Search/?stext=" + quote(kw)
        try:
            soup = get(url)
        except Exception as e:  # noqa
            print(f"   - 잡코리아 '{kw}' 실패: {e}")
            continue
        seen, n = set(), 0
        for a in soup.find_all("a", href=re.compile(r"/Recruit/GI_Read/\d+")):
            gid = re.search(r"GI_Read/(\d+)", a["href"]).group(1)
            title = clean(a.get_text(" "))
            if gid in seen or len(title) < 6:
                continue
            seen.add(gid)
            if SKIP.search(title) or NOISE.search(title):
                continue
            dept = classify(title)
            if not dept:
                continue
            # 회사명: 같은 공고 카드 안의 다른 링크/텍스트에서 추정
            card = a
            for _ in range(5):
                if card.parent is None:
                    break
                card = card.parent
                if len(clean(card.get_text(" "))) > len(title) + 10:
                    break
            org = ""
            for other in card.find_all("a"):
                t = clean(other.get_text(" "))
                if t and t != title and 1 < len(t) <= 30 and "GI_Read" not in (other.get("href") or ""):
                    org = t
                    break
            text = clean(card.get_text(" "))
            m = re.search(r"~\s*(\d{1,2})/(\d{1,2})", text)
            dl = md_to_date(*m.groups()) if m else None
            out.append(make(dept, title, org, dl, "", f"https://www.jobkorea.co.kr/Recruit/GI_Read/{gid}",
                            f"잡코리아 '{kw}' 검색 결과 공고."))
            n += 1
        print(f"   - 잡코리아 '{kw}': {n}건")
    return out


SOURCES = [
    ("누리-세종학당", src_nuri),
    ("이화여대 통역번역연구소", src_erits),
    ("재외한국문화원", src_korean_culture),
    ("한국문화예술위원회", src_arko),
    ("문체부 소속기관", src_mcst),
    ("진학프로", src_jinhakpro),
    ("사람인", src_saramin),
    ("잡코리아", src_jobkorea),
]


# ---------------------------------------------------------------- 병합
def norm_key(p):
    t = re.sub(r"[\s\[\]()·,./\-]", "", (p.get("title") or "").lower())
    o = re.sub(r"[\s()㈜]|주식회사|\(주\)", "", (p.get("org") or "").lower())
    return t + "|" + o


def main():
    data = json.loads(POSTS_FILE.read_text(encoding="utf-8")) if POSTS_FILE.exists() else {"posts": []}
    posts = data.get("posts", [])
    keys = {norm_key(p) for p in posts}
    titles = {re.sub(r"\s", "", p.get("title", "")) for p in posts}

    added = 0
    report = []
    for name, fn in SOURCES:
        try:
            found = fn()
        except Exception as e:  # 한 사이트가 실패해도 계속
            print(f"[실패] {name}: {e}")
            report.append(f"{name}: 실패")
            continue
        new = 0
        for p in found:
            if not p["title"]:
                continue
            if p["deadline"] and p["deadline"] < iso(TODAY):
                continue  # 이미 마감
            k = norm_key(p)
            t = re.sub(r"\s", "", p["title"])
            if k in keys or t in titles:
                continue
            keys.add(k)
            titles.add(t)
            p["id"] = "gh-" + hashlib.md5(k.encode()).hexdigest()[:12]
            p["createdAt"] = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
            posts.append(p)
            new += 1
        added += new
        print(f"[완료] {name}: 발견 {len(found)}건 / 새로 추가 {new}건")
        report.append(f"{name}: +{new}")

    # 마감 후 14일 지난 공고 삭제
    cutoff = iso(TODAY - timedelta(days=KEEP_DAYS_AFTER_DEADLINE))
    before = len(posts)
    posts = [p for p in posts if not (p.get("deadline") and p["deadline"] < cutoff)]
    removed = before - len(posts)

    # 너무 많아지면 오래된 것부터 정리
    posts.sort(key=lambda p: p.get("createdAt", ""), reverse=True)
    posts = posts[:MAX_POSTS]

    if added == 0 and removed == 0:
        print("변경 없음")
        return 0
    data["posts"] = posts
    data["updatedAt"] = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    POSTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"=== 추가 {added}건, 삭제 {removed}건, 전체 {len(posts)}건 ===")
    print(" | ".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())

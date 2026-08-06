# -*- coding: utf-8 -*-
"""CGV 예매 오픈 감시 — 버전 10 (상영 API 자동 탐지 + 날짜별 직접 호출)

오늘은 제외하고, 이번 주와 다음 주 중 TARGET_WEEKDAYS(기본: 금,토,일)에
해당하는 날짜 탭만 눌러 데이터를 수집·감시한다.
회차는 JSON 구조 파싱으로 (날짜, 상영관, 시간)을 정확히 추출하고,
알림은 날짜(요일) → 상영관별 시간으로 묶어서 전송한다.
"""
import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests
from playwright.async_api import async_playwright

MOVIE_RAW = os.environ["MOVIE_KEYWORD"].strip()
URL = os.environ["THEATER_URL"].strip()
THEATER_NAME = os.environ.get("THEATER_NAME", "").strip()
TOKEN = os.environ["TELEGRAM_TOKEN"].strip()
CHAT_ID = os.environ["CHAT_ID"].strip()
CHECK_EVERY_SEC = int(os.environ.get("CHECK_EVERY_SEC", "30"))
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "225"))
WEEKDAYS_RAW = os.environ.get("TARGET_WEEKDAYS", "금,토,일")
STATE_FILE = "seen.json"

TIME_RE = re.compile(r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)")
HALL_RE = re.compile(
    r"(IMAX|아이맥스|4DX|SCREENX|스크린X|DOLBY|돌비|골드클래스|템퍼시네마|리클라이너|프리미엄|씨네|\d{1,2}관)",
    re.IGNORECASE,
)
TIME_KEY = re.compile(r"(time|tm)", re.I)
DATE_KEY = re.compile(r"(date|ymd|day|dt)", re.I)
HALL_KEY = re.compile(r"(scr|screen|theab|hall|room)", re.I)
WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]


def kst_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=9)


def target_dates() -> list:
    """오늘 제외, 이번 주~다음 주 일요일까지 중 지정 요일만."""
    today = kst_now().date()
    wanted = {
        WEEKDAY_KO.index(w.strip())
        for w in WEEKDAYS_RAW.split(",")
        if w.strip() in WEEKDAY_KO
    }
    end = today + timedelta(days=(6 - today.weekday()) + 7)  # 다음 주 일요일
    out, d = [], today + timedelta(days=1)
    while d <= end:
        if d.weekday() in wanted:
            out.append(d)
        d += timedelta(days=1)
    return out


def date_label(d) -> str:
    return f"{d.month}/{d.day}({WEEKDAY_KO[d.weekday()]})"


def _norm_char(ch: str) -> str:
    low = ch.lower()
    return low if len(low) == 1 else ch


def norm(s: str) -> str:
    return "".join(_norm_char(c) for c in s if not c.isspace())


MOVIE_NORM = norm(MOVIE_RAW)


def kw_in(s: str) -> bool:
    return MOVIE_NORM in norm(s)


def find_keyword_positions(text: str) -> list:
    norm_chars, idx_map = [], []
    for i, ch in enumerate(text):
        if ch.isspace():
            continue
        norm_chars.append(_norm_char(ch))
        idx_map.append(i)
    norm_text = "".join(norm_chars)
    positions, start = [], 0
    while True:
        pos = norm_text.find(MOVIE_NORM, start)
        if pos < 0:
            break
        positions.append(idx_map[pos])
        start = pos + 1
    return positions


# ---------- 텔레그램 ----------
def send_telegram(text: str) -> None:
    for attempt in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": text},
                timeout=30,
            )
            body = r.text[:200]
            if r.status_code == 200:
                print("텔레그램 전송 성공")
            elif r.status_code == 401:
                print("텔레그램 실패(401): 토큰이 잘못됨 — Secrets 재등록 필요 /", body)
            elif r.status_code in (400, 403):
                print("텔레그램 실패: chat_id 문제 또는 /start 미전송 /", body)
            else:
                print("텔레그램 실패:", r.status_code, body)
            return
        except Exception as e:
            print(f"텔레그램 전송 실패({attempt + 1}/3):", type(e).__name__)
            time.sleep(3)


# ---------- 페이지 수집 ----------
async def collect_text(dates: list, verbose: bool = False) -> list:
    chunks = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(
            locale="ko-KR",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
        )

        api_hits = []

        async def grab(res):
            try:
                ct = res.headers.get("content-type", "")
                if ("json" in ct or "text" in ct) and len(chunks) < 400:
                    body = await res.text()
                    chunks.append(body)
                    if "scnYmd" in body or "scnsrtTm" in body:
                        req = res.request
                        post = None
                        try:
                            post = req.post_data
                        except Exception:
                            pass
                        api_hits.append({
                            "url": req.url, "method": req.method, "post": post,
                        })
            except Exception:
                pass

        page.on("response", lambda res: asyncio.create_task(grab(res)))
        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(8000)
        if verbose:
            print("접속 후 URL:", page.url)
            try:
                print("페이지 제목:", await page.title())
            except Exception:
                pass

        async def try_click(label):
            candidates = [
                page.get_by_text(label, exact=False).first,
                page.locator(f"xpath=//*[contains(text(), '{label}')]").first,
            ]
            for loc in candidates:
                try:
                    await loc.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                try:
                    await loc.click(timeout=4000)
                    return True
                except Exception:
                    try:
                        await loc.click(timeout=3000, force=True)
                        return True
                    except Exception:
                        continue
            return False

        for label in ("서울", THEATER_NAME):
            if not label:
                continue
            ok = await try_click(label)
            await page.wait_for_timeout(3000)
            if verbose:
                print("클릭 성공:" if ok else "클릭 실패:", label)

        # 상영 데이터 API를 찾았으면 날짜만 바꿔 직접 호출
        if api_hits:
            hit = api_hits[-1]
            if verbose:
                print("상영 API 발견:", hit["method"], hit["url"][:220])
                if hit["post"]:
                    print("요청 본문 샘플:", str(hit["post"])[:400])
            today_ymd = f"{kst_now().year}{kst_now().month:02d}{kst_now().day:02d}"
            for d in dates:
                ymd = f"{d.year}{d.month:02d}{d.day:02d}"
                new_url = re.sub(r"20\d{6}", ymd, hit["url"])
                if new_url == hit["url"] and "=" in hit["url"]:
                    sep = "&" if "?" in new_url else "?"
                    new_url = f"{new_url}{sep}scnYmd={ymd}"
                try:
                    if hit["method"] == "POST" and hit["post"]:
                        payload = str(hit["post"]).replace(today_ymd, ymd)
                        got = await page.evaluate(
                            """async ([u, b]) => {
                                const r = await fetch(u, {
                                    method: 'POST',
                                    headers: {'Content-Type': 'application/json'},
                                    body: b, credentials: 'include'});
                                return await r.text();
                            }""",
                            [new_url, payload],
                        )
                    else:
                        got = await page.evaluate(
                            """async (u) => {
                                const r = await fetch(u, {credentials: 'include'});
                                return await r.text();
                            }""",
                            new_url,
                        )
                    if got:
                        chunks.append(got)
                        if verbose:
                            print("API 직접 호출:", date_label(d), "응답", len(got), "자")
                except Exception as e:
                    if verbose:
                        print("API 호출 실패:", date_label(d), type(e).__name__)
        elif verbose:
            print("상영 API를 찾지 못함 — 탭 클릭 방식만 사용")

        # 감시 대상 날짜 탭도 순서대로 눌러 데이터 수집
        for d in dates:
            day = d.day
            try:
                clicked = await page.evaluate(
                    """(day) => {
                        const els = [...document.querySelectorAll('button, li, a, [role=button], span, div')];
                        const cands = els.filter(e => {
                            const t = (e.innerText || '').trim().replace(/\\s+/g, '');
                            if (!t || t.length > 4) return false;
                            const digits = t.replace(/[^0-9]/g, '');
                            return digits === String(day) && /^[\\uc77c\\uc6d4\\ud654\\uc218\\ubaa9\\uae08\\ud1a0]?\\d{1,2}[\\uc77c\\uc6d4\\ud654\\uc218\\ubaa9\\uae08\\ud1a0]?$/.test(t);
                        });
                        if (!cands.length) return false;
                        cands.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
                        cands[0].click();
                        return true;
                    }""",
                    day,
                )
                await page.wait_for_timeout(3500)
                if verbose:
                    print("날짜 탭 클릭:", date_label(d), "성공" if clicked else "실패(탭 없음일 수 있음)")
            except Exception as e:
                if verbose:
                    print("날짜 탭 클릭 오류:", date_label(d), type(e).__name__)

        try:
            body = await page.evaluate("document.body.innerText")
            chunks.append(body)
        except Exception:
            pass
        await browser.close()
    return chunks


# ---------- JSON 구조 파싱 ----------
def norm_time_kv(key: str, val):
    sv = str(val).strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(:\d{2})?", sv)
    if m and int(m.group(1)) < 24:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    if TIME_KEY.search(key):
        m = re.fullmatch(r"(\d{2})(\d{2})(\d{2})?", sv)
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            return f"{m.group(1)}:{m.group(2)}"
    return None


def norm_date_kv(key: str, val):
    sv = str(val).strip()
    if not (DATE_KEY.search(key) or re.fullmatch(r"20\d{6}", sv)):
        return None
    m = re.fullmatch(r"(20\d{2})[-./]?(\d{1,2})[-./]?(\d{1,2})", sv)
    if m and 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(3)) <= 31:
        return f"{int(m.group(2))}/{int(m.group(3))}"
    return None


def norm_hall_kv(key: str, val):
    if not isinstance(val, str):
        return None
    sv = val.strip()
    if not sv or len(sv) > 14:
        return None
    if HALL_RE.search(sv):
        return sv
    if HALL_KEY.search(key) and re.search(r"[가-힣A-Za-z]", sv):
        return sv
    return None


CGV_DATE_KEYS = ("scnYmd", "scnymd", "playYmd", "scnDe")
CGV_TIME_KEYS = ("scnsrtTm", "scnsrttm", "playStrTm", "scnStrTm")
CGV_HALL_KEYS = ("expoScnsNm", "scnsNm", "exposcnsnm", "scnsnm")
CGV_TITLE_KEYS = ("prodNm", "expoProdNm", "movieNm", "prodnm")


def cgv_scan(dct: dict):
    """CGV 상영 항목에서 (시간, 날짜, 상영관)을 직접 읽는다."""
    def pick(keys):
        for k in keys:
            if k in dct and isinstance(dct[k], (str, int)):
                return str(dct[k]).strip()
        low = {kk.lower(): vv for kk, vv in dct.items()}
        for k in keys:
            v = low.get(k.lower())
            if isinstance(v, (str, int)):
                return str(v).strip()
        return None

    raw_d, raw_t, hall = pick(CGV_DATE_KEYS), pick(CGV_TIME_KEYS), pick(CGV_HALL_KEYS)
    dt = t = None
    if raw_d:
        m = re.fullmatch(r"(20\d{2})[-./]?(\d{2})[-./]?(\d{2})", raw_d)
        if m:
            dt = f"{int(m.group(2))}/{int(m.group(3))}"
    if raw_t:
        m = re.fullmatch(r"(\d{1,2}):?(\d{2})(:\d{2})?", raw_t)
        if m and int(m.group(1)) < 24:
            t = f"{int(m.group(1)):02d}:{m.group(2)}"
    return t, dt, (hall or None)


def field_scan(dct: dict):
    t = dt = hall = None
    for k, v in dct.items():
        if not isinstance(v, (str, int)):
            continue
        if t is None:
            t = norm_time_kv(k, v)
        if dt is None:
            dt = norm_date_kv(k, v)
        if hall is None:
            hall = norm_hall_kv(k, v)
    return t, dt, hall


def collect_screenings(data) -> set:
    matched = []

    def walk(node):
        if isinstance(node, dict):
            own = " ".join(str(v) for v in node.values() if isinstance(v, str))
            if own and kw_in(own):
                matched.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    triples = set()

    # CGV 실제 필드가 있는 항목을 직접 수집 (가장 정확)
    def walk_cgv(node):
        if isinstance(node, dict):
            title = ""
            for k in CGV_TITLE_KEYS:
                v = node.get(k)
                if isinstance(v, str):
                    title += " " + v
            t, dt, hall = cgv_scan(node)
            if t and title and kw_in(title):
                triples.add((dt or "", hall or "", t))
            for v in node.values():
                walk_cgv(v)
        elif isinstance(node, list):
            for v in node:
                walk_cgv(v)

    walk_cgv(data)
    if triples:
        return triples

    def units_of(d):
        us = []

        def rec(x):
            if isinstance(x, dict):
                if any(
                    isinstance(v, (str, int)) and norm_time_kv(k, v)
                    for k, v in x.items()
                ):
                    us.append(x)
                for v in x.values():
                    rec(v)
            elif isinstance(x, list):
                for v in x:
                    rec(v)

        rec(d)
        return us

    for d in matched:
        p_t, p_dt, p_hall = field_scan(d)
        us = units_of(d)
        for u in us:
            t, dt, hall = field_scan(u)
            if t is None:
                continue
            triples.add((dt or p_dt or "", hall or p_hall or "", t))
        if not us and p_t:
            triples.add((p_dt or "", p_hall or "", p_t))

    if not triples and matched:
        codes = set()
        for d in matched:
            for k, v in d.items():
                if isinstance(v, (str, int)) and re.search(r"(mov|film)", k, re.I) \
                        and re.search(r"(no|cd|code|id)$", k, re.I):
                    codes.add(str(v))
        if codes:
            def walk_all(node):
                if isinstance(node, dict):
                    vals = {str(v) for v in node.values() if isinstance(v, (str, int))}
                    if vals & codes:
                        t, dt, hall = field_scan(node)
                        if t:
                            triples.add((dt or "", hall or "", t))
                    for v in node.values():
                        walk_all(v)
                elif isinstance(node, list):
                    for v in node:
                        walk_all(v)
            walk_all(data)
    return triples


def print_debug_snippet(chunks):
    for c in chunks:
        if c.lstrip()[:1] in "[{" and kw_in(c):
            pos_list = find_keyword_positions(c)
            if pos_list:
                p = pos_list[0]
                sample = c[max(0, p - 200): p + 600].replace("\n", " ")
                print("JSON 컨텍스트 샘플:", sample[:800])
                return
    print("JSON 컨텍스트 샘플: (키워드가 포함된 JSON 응답 없음)")


# ---------- 상태 ----------
def load_seen() -> set:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)


def format_grouped(triples) -> str:
    by_date = {}
    for dt, hall, t in triples:
        by_date.setdefault(dt or "날짜 미확인", {}).setdefault(hall or "상영관 미확인", set()).add(t)
    lines = []
    for dt in sorted(by_date):
        lines.append(f"📅 {dt}")
        for hall in sorted(by_date[dt]):
            times = ", ".join(sorted(by_date[dt][hall]))
            lines.append(f" · {hall}: {times}")
    msg = "\n".join(lines)
    if len(msg) > 3300:
        msg = msg[:3300] + "\n…(너무 길어 일부 생략)"
    return msg


# ---------- 메인 ----------
def main() -> None:
    first_run = not os.path.exists(STATE_FILE)
    seen = load_seen()

    targets_now = target_dates()
    target_str = ", ".join(date_label(d) for d in targets_now)
    if first_run:
        send_telegram(
            f"✅ CGV 감시 시작!\n영화: {MOVIE_RAW}\n"
            f"감시 날짜(이번 주·다음 주 {WEEKDAYS_RAW}): {target_str}\n"
            f"변화가 생기면 바로 알릴게요."
        )
        save_seen(seen)
    else:
        send_telegram(f"🔎 CGV 감시 작동 중 — {MOVIE_RAW} / {target_str}")

    deadline = time.time() + RUN_MINUTES * 60
    print(f"감시 루프 시작: '{MOVIE_RAW}' / 대상: {target_str}")

    loop_no = 0
    while time.time() < deadline:
        t0 = time.time()
        first_iter = loop_no == 0
        try:
            targets = target_dates()
            allowed = {f"{d.month}/{d.day}" for d in targets}
            label_map = {f"{d.month}/{d.day}": date_label(d) for d in targets}
            if first_iter:
                print("감시 대상 날짜:", ", ".join(label_map.values()))

            chunks = asyncio.run(collect_text(targets, verbose=first_iter))
            loop_no += 1
            full_text = "\n".join(chunks)
            positions = find_keyword_positions(full_text)
            stamp = time.strftime("%H:%M:%S")

            if positions:
                raw_triples = set()
                for c in chunks:
                    if c.lstrip()[:1] not in "[{" or not kw_in(c):
                        continue
                    try:
                        data = json.loads(c)
                    except Exception:
                        continue
                    raw_triples |= collect_screenings(data)

                triples = {
                    (label_map[dt], hall, t)
                    for (dt, hall, t) in raw_triples
                    if dt in allowed
                }
                if first_iter:
                    print_debug_snippet(chunks)
                    print(f"추출 회차: 필터 전 {len(raw_triples)}건 → 대상 날짜만 {len(triples)}건")

                new_triples = [
                    tr for tr in triples
                    if f"{tr[0]}|{tr[1]}|{tr[2]}" not in seen
                ]
                new_dates = sorted({tr[0] for tr in new_triples
                                    if f"DATE:{tr[0]}" not in seen})
                new_halls = sorted({tr[1] for tr in new_triples if tr[1]
                                    and f"HALL:{tr[1]}" not in seen})

                if new_triples:
                    seen.update(f"{a}|{b}|{c2}" for a, b, c2 in new_triples)
                    seen.update(f"DATE:{d}" for d in new_dates)
                    seen.update(f"HALL:{h}" for h in new_halls)
                    save_seen(seen)
                    parts = [f"🎬 CGV 변화 감지!\n영화: {MOVIE_RAW}"]
                    if new_dates:
                        parts.append("🗓 새 날짜: " + ", ".join(new_dates))
                    if new_halls:
                        parts.append("🏟 새 상영관: " + ", ".join(new_halls))
                    parts.append("⏰ 새 회차:\n" + format_grouped(new_triples))
                    parts.append("예매: https://cgv.co.kr")
                    send_telegram("\n\n".join(parts))
                    print(stamp, f"알림 전송: 새 회차 {len(new_triples)}건")
                else:
                    print(stamp, "키워드 있음, 대상 날짜 변화 없음")
            else:
                print(stamp, "키워드 없음")
        except Exception as e:
            print("확인 오류:", type(e).__name__, e)

        elapsed = time.time() - t0
        time.sleep(max(1, CHECK_EVERY_SEC - elapsed))

    save_seen(seen)
    print("이번 작업 종료 — 다음 예약 작업이 이어서 감시합니다.")


if __name__ == "__main__":
    main()

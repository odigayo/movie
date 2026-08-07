# -*- coding: utf-8 -*-
"""CGV 예매 오픈 감시 — 버전 11 (여러 영화 + 특별관만 감시)

- MOVIE_KEYWORD 에 쉼표로 여러 영화를 넣을 수 있다. (예: "스파이더맨,아바타")
- ONLY_SPECIAL_HALLS=true 이면 "N관 (Laser)" 같은 일반관은 제외하고
  IMAX/4DX/SCREENX/PREMIUM/CINE de CHEF 등 특별관 회차만 알린다.
- 오늘은 제외하고, 이번 주·다음 주의 TARGET_WEEKDAYS(기본 금,토,일)만 감시.
- 상영 API를 자동으로 찾아 날짜별로 직접 호출하므로 탭 클릭 실패와 무관하게 수집된다.
- 알림은 영화별로 따로, 날짜(요일) → 상영관별 시간 형태로 전송한다.
"""
import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests
from playwright.async_api import async_playwright

MOVIES = [m.strip() for m in os.environ["MOVIE_KEYWORD"].split(",") if m.strip()]
URL = os.environ["THEATER_URL"].strip()
THEATER_NAME = os.environ.get("THEATER_NAME", "").strip()
TOKEN = os.environ["TELEGRAM_TOKEN"].strip()
CHAT_ID = os.environ["CHAT_ID"].strip()
CHECK_EVERY_SEC = int(os.environ.get("CHECK_EVERY_SEC", "30"))
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "225"))
WEEKDAYS_RAW = os.environ.get("TARGET_WEEKDAYS", "금,토,일")
ONLY_SPECIAL = os.environ.get("ONLY_SPECIAL_HALLS", "true").strip().lower() == "true"
STATE_FILE = "seen.json"

WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]

# 상영관 이름에서 의미 없는 기술 표기 (이것만 붙은 N관은 일반관으로 본다)
GENERIC_TAG = re.compile(
    r"[\(\[]\s*(laser|레이저|2d|3d|디지털|digital|일반)\s*[\)\]]", re.I
)
PLAIN_HALL = re.compile(r"^\d{1,2}관$")

CGV_DATE_KEYS = ("scnYmd", "scnymd", "playYmd", "scnDe")
CGV_TIME_KEYS = ("scnsrtTm", "scnsrttm", "playStrTm", "scnStrTm")
CGV_HALL_KEYS = ("expoScnsNm", "scnsNm", "exposcnsnm", "scnsnm")
CGV_TITLE_KEYS = ("prodNm", "expoProdNm", "movieNm", "prodnm")


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
    end = today + timedelta(days=(6 - today.weekday()) + 7)
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


def kw_in(text: str, movie: str) -> bool:
    return norm(movie) in norm(text)


def is_special_hall(hall: str) -> bool:
    """일반관(N관, N관 (Laser) 등)이 아니면 특별관으로 본다."""
    if not hall:
        return True  # 상영관을 알 수 없으면 놓치지 않도록 포함
    cleaned = GENERIC_TAG.sub("", hall)
    cleaned = re.sub(r"\s+", "", cleaned)
    return not PLAIN_HALL.fullmatch(cleaned)


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
                        api_hits.append({"url": req.url, "method": req.method, "post": post})
            except Exception:
                pass

        page.on("response", lambda res: asyncio.create_task(grab(res)))
        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(8000)
        if verbose:
            print("접속 후 URL:", page.url)

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
                print("상영 API 발견:", hit["method"], hit["url"][:200])
            now = kst_now()
            today_ymd = f"{now.year}{now.month:02d}{now.day:02d}"
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
                                const r = await fetch(u, {method: 'POST',
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

        # 날짜 탭도 눌러 데이터 보강
        for d in dates:
            day = d.day
            try:
                await page.evaluate(
                    """(day) => {
                        const els = [...document.querySelectorAll('button, li, a, [role=button], span, div')];
                        const cands = els.filter(e => {
                            const t = (e.innerText || '').trim().replace(/\\s+/g, '');
                            if (!t || t.length > 4) return false;
                            return t.replace(/[^0-9]/g, '') === String(day);
                        });
                        if (!cands.length) return false;
                        cands.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
                        cands[0].click();
                        return true;
                    }""",
                    day,
                )
                await page.wait_for_timeout(2500)
            except Exception:
                pass

        try:
            chunks.append(await page.evaluate("document.body.innerText"))
        except Exception:
            pass
        await browser.close()
    return chunks


# ---------- 상영 정보 파싱 ----------
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


def collect_screenings(data, movie: str) -> set:
    """해당 영화의 (날짜, 상영관, 시간) 집합을 수집."""
    triples = set()

    def walk(node):
        if isinstance(node, dict):
            title = ""
            for k in CGV_TITLE_KEYS:
                v = node.get(k)
                if isinstance(v, str):
                    title += " " + v
            t, dt, hall = cgv_scan(node)
            if t and title and kw_in(title, movie):
                triples.add((dt or "", hall or "", t))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return triples


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

    def date_key(s):
        m = re.match(r"(\d+)/(\d+)", s)
        return (int(m.group(1)), int(m.group(2))) if m else (99, 99)

    lines = []
    for dt in sorted(by_date, key=date_key):
        lines.append(f"📅 {dt}")
        for hall in sorted(by_date[dt]):
            lines.append(f" · {hall}: {', '.join(sorted(by_date[dt][hall]))}")
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
    mode = "특별관만" if ONLY_SPECIAL else "전체 상영관"
    movies_str = ", ".join(MOVIES)
    header = (
        f"영화: {movies_str}\n감시 모드: {mode}\n"
        f"감시 날짜(이번 주·다음 주 {WEEKDAYS_RAW}): {target_str}"
    )
    if first_run:
        send_telegram(f"✅ CGV 감시 시작!\n{header}\n변화가 생기면 바로 알릴게요.")
        save_seen(seen)
    else:
        send_telegram(f"🔎 CGV 감시 작동 중\n{header}")

    deadline = time.time() + RUN_MINUTES * 60
    print(f"감시 루프 시작: {movies_str} / {mode} / 대상: {target_str}")

    loop_no = 0
    while time.time() < deadline:
        t0 = time.time()
        first_iter = loop_no == 0
        try:
            targets = target_dates()
            allowed = {f"{d.month}/{d.day}" for d in targets}
            label_map = {f"{d.month}/{d.day}": date_label(d) for d in targets}

            chunks = asyncio.run(collect_text(targets, verbose=first_iter))
            loop_no += 1
            stamp = time.strftime("%H:%M:%S")

            parsed_docs = []
            for c in chunks:
                if c.lstrip()[:1] not in "[{":
                    continue
                try:
                    parsed_docs.append(json.loads(c))
                except Exception:
                    continue

            any_change = False
            for movie in MOVIES:
                raw = set()
                for data in parsed_docs:
                    raw |= collect_screenings(data, movie)

                triples = {
                    (label_map[dt], hall, t)
                    for (dt, hall, t) in raw
                    if dt in allowed and (not ONLY_SPECIAL or is_special_hall(hall))
                }
                if first_iter:
                    print(f"[{movie}] 전체 {len(raw)}건 → 대상 날짜·{mode} {len(triples)}건")

                new_triples = [
                    tr for tr in triples
                    if f"{movie}|{tr[0]}|{tr[1]}|{tr[2]}" not in seen
                ]
                if not new_triples:
                    continue

                new_dates = sorted({tr[0] for tr in new_triples
                                    if f"DATE:{movie}:{tr[0]}" not in seen})
                new_halls = sorted({tr[1] for tr in new_triples if tr[1]
                                    and f"HALL:{movie}:{tr[1]}" not in seen})
                seen.update(f"{movie}|{a}|{b}|{c2}" for a, b, c2 in new_triples)
                seen.update(f"DATE:{movie}:{d}" for d in new_dates)
                seen.update(f"HALL:{movie}:{h}" for h in new_halls)
                save_seen(seen)
                any_change = True

                parts = [f"🎬 CGV 변화 감지!\n영화: {movie}"]
                if new_dates:
                    parts.append("🗓 새 날짜: " + ", ".join(new_dates))
                if new_halls:
                    parts.append("🏟 새 상영관: " + ", ".join(new_halls))
                parts.append("⏰ 새 회차:\n" + format_grouped(new_triples))
                parts.append("예매: https://cgv.co.kr")
                send_telegram("\n\n".join(parts))
                print(stamp, f"[{movie}] 알림 전송: 새 회차 {len(new_triples)}건")

            if not any_change:
                print(stamp, "변화 없음")
        except Exception as e:
            print("확인 오류:", type(e).__name__, e)

        elapsed = time.time() - t0
        time.sleep(max(1, CHECK_EVERY_SEC - elapsed))

    save_seen(seen)
    print("이번 작업 종료 — 다음 예약 작업이 이어서 감시합니다.")


if __name__ == "__main__":
    main()

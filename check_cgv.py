# -*- coding: utf-8 -*-
"""CGV 예매 오픈 감시 (GitHub Actions용, 상시 실행 버전)

작업이 시작되면 약 3시간 45분 동안 계속 돌면서 CHECK_EVERY_SEC초마다
CGV 페이지를 확인하고, 영화 키워드의 새 회차가 발견되는 즉시 텔레그램으로 알린다.
제목 매칭은 공백·대소문자를 무시한 부분일치라서
"스파이더맨" 으로 "스파이더 맨.", "스파이더맨(더빙)", "스파이더맨: 부제" 를 모두 잡는다.
설정은 .github/workflows/watch.yml 에서 수정한다.
"""
import asyncio
import json
import os
import re
import time

import requests
from playwright.async_api import async_playwright

MOVIE_RAW = os.environ["MOVIE_KEYWORD"].strip()
URL = os.environ["THEATER_URL"].strip()
THEATER_NAME = os.environ.get("THEATER_NAME", "").strip()
TOKEN = os.environ["TELEGRAM_TOKEN"].strip()
CHAT_ID = os.environ["CHAT_ID"].strip()
CHECK_EVERY_SEC = int(os.environ.get("CHECK_EVERY_SEC", "30"))
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "225"))
STATE_FILE = "seen.json"
TIME_RE = re.compile(r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)")


def _norm_char(ch: str) -> str:
    low = ch.lower()
    return low if len(low) == 1 else ch


MOVIE_NORM = "".join(_norm_char(c) for c in MOVIE_RAW if not c.isspace())


def send_telegram(text: str) -> None:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text},
            timeout=15,
        )
        body = r.text[:200]
        if r.status_code == 200:
            print("텔레그램 전송 성공")
        elif r.status_code == 401:
            print("텔레그램 실패(401): 토큰이 잘못됨 — Secrets의 TELEGRAM_TOKEN 재등록 필요 /", body)
        elif r.status_code in (400, 403):
            print("텔레그램 실패: chat_id가 잘못됐거나 봇에게 /start를 안 보낸 상태 /", body)
        else:
            print("텔레그램 실패:", r.status_code, body)
    except Exception as e:
        print("텔레그램 전송 실패:", type(e).__name__)


async def collect_text() -> str:
    """페이지를 렌더링하면서 오가는 데이터(JSON 응답 + 화면 텍스트)를 전부 수집."""
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

        async def grab(res):
            try:
                ct = res.headers.get("content-type", "")
                if ("json" in ct or "text" in ct) and len(chunks) < 300:
                    chunks.append(await res.text())
            except Exception:
                pass

        page.on("response", lambda res: asyncio.create_task(grab(res)))
        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)

        # 주소에 극장 정보가 없을 경우, 페이지에서 극장 이름을 눌러본다 (실패해도 무시)
        if THEATER_NAME:
            try:
                await page.get_by_text(THEATER_NAME, exact=False).first.click(timeout=4000)
                await page.wait_for_timeout(4000)
            except Exception:
                pass

        try:
            chunks.append(await page.evaluate("document.body.innerText"))
        except Exception:
            pass
        await browser.close()
    return "\n".join(chunks)


def find_keyword_positions(text: str) -> list:
    """공백·대소문자 무시 부분일치로 키워드 위치(원본 인덱스)를 찾는다."""
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


def extract_slots(text: str, positions: list) -> set:
    """키워드 주변(앞뒤 600자)에서 상영시간(HH:MM)을 추출."""
    slots = set()
    for p in positions:
        window = text[max(0, p - 600): p + 600]
        slots.update(TIME_RE.findall(window))
    return slots


def load_seen() -> set:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)


def main() -> None:
    first_run = not os.path.exists(STATE_FILE)
    seen = load_seen()

    if first_run:
        send_telegram(
            f"✅ CGV 감시 시작!\n영화: {MOVIE_RAW}\n"
            f"약 {CHECK_EVERY_SEC}초 간격으로 계속 확인하다가 회차가 열리면 바로 알릴게요."
        )
        save_seen(seen)
    else:
        send_telegram(f"🔎 CGV 감시 작동 중 — {MOVIE_RAW} (감시 작업이 새로 시작될 때마다 오는 확인 메시지예요)")

    deadline = time.time() + RUN_MINUTES * 60
    print(f"감시 루프 시작: '{MOVIE_RAW}' / {CHECK_EVERY_SEC}초 간격 / {RUN_MINUTES}분간")

    while time.time() < deadline:
        t0 = time.time()
        try:
            text = asyncio.run(collect_text())
            positions = find_keyword_positions(text)
            stamp = time.strftime("%H:%M:%S")
            if positions:
                slots = extract_slots(text, positions)
                new_items = [s for s in sorted(slots) if s not in seen]
                if not slots and "OPEN" not in seen:
                    new_items.append("예매 오픈 (회차 시간 미확인)")
                    seen.add("OPEN")
                if new_items:
                    seen.update(s for s in new_items if TIME_RE.fullmatch(s))
                    save_seen(seen)
                    send_telegram(
                        f"🎬 CGV 새 회차 오픈!\n영화: {MOVIE_RAW}\n\n"
                        + "\n".join(new_items)
                        + "\n\n예매: https://cgv.co.kr"
                    )
                    print(stamp, "알림 전송:", new_items)
                else:
                    print(stamp, "키워드 있음, 새 회차 없음")
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

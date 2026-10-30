"""틱톡샵 검색 결과에서 브랜드 상품의 순위·판매량을 수집한다.

국가(markets.yml) x 브랜드 검색어(brands.yml) 조합마다 검색 페이지를 열고,
상품 카드의 순서(=검색 순위), 상품명, 가격, 누적 판매수("12.3K sold")를 뽑아
data/snapshots.csv 에 그날 날짜로 추가한다.

사이트 구조가 바뀌거나 차단될 수 있으므로 매 실행마다 debug/<market>/ 에
스크린샷·페이지 텍스트·결과 요약을 남긴다. 수집이 0건이면 여기부터 확인한다.
"""
import csv
import datetime as dt
import json
import re
import sys
import urllib.parse
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DEBUG = ROOT / "debug"
SNAPSHOTS = DATA / "snapshots.csv"
FIELDS = ["date", "market", "brand", "keyword", "rank", "product_id", "title",
          "price", "sold", "rating", "is_brand", "url"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

# 상품 카드 추출: 상품 상세 링크를 찾아 카드 단위 텍스트를 DOM 순서대로 반환
EXTRACT_JS = r"""
() => {
  const re = /\/(shop\/pdp|view\/product|product)\/[^?#]*?(\d{12,22})/;
  const seen = new Set(), out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const m = a.href.match(re);
    if (!m || seen.has(m[2])) continue;
    seen.add(m[2]);
    let card = a;
    while (card.parentElement && card.innerText.length < 40 &&
           card.parentElement.innerText.length < 600) card = card.parentElement;
    out.push({id: m[2], url: a.href.split('?')[0], text: card.innerText});
  }
  return out;
}
"""

SOLD_RE = re.compile(
    r"([\d][\d.,]*)\s*([KkMm만천]|rb|RB|jt|JT)?\+?\s*(?:sold|판매|販売|個販売|terjual|ขายแล้ว|đã bán)",
    re.I)
SOLD_RE_PREFIX = re.compile(r"(?:ขายแล้ว|đã bán|terjual)\s*([\d][\d.,]*)\s*([KkMm]|rb|jt)?", re.I)
PRICE_RE = re.compile(r"((?:US)?\$|£|¥|￥|RM|Rp|฿|₫)\s?[\d.,]+|[\d.,]+\s?(?:₫|円)")
RATING_RE = re.compile(r"(?<![\d.,])([1-5]\.\d)(?![\d.,]|\s*[KkMm만천])")
MULT = {"k": 1e3, "m": 1e6, "rb": 1e3, "jt": 1e6, "천": 1e3, "만": 1e4, "": 1}


def parse_num(num: str, unit: str) -> int:
    if unit and re.fullmatch(r"\d+,\d{1,2}", num):  # 1,2k 형식(소수점 쉼표)
        num = num.replace(",", ".")
    n = num.replace(",", "")
    if n.count(".") > 1:  # 1.234.567 형식(인니·베트남 천단위 점)
        n = n.replace(".", "")
    try:
        return int(float(n) * MULT[unit.lower()])
    except ValueError:
        return 0


def parse_card(text: str) -> dict:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    names = [l for l in lines if not (SOLD_RE.search(l) or SOLD_RE_PREFIX.search(l)
                                      or PRICE_RE.fullmatch(l))]
    title = max(names or lines, key=len) if lines else ""
    sold = 0
    m = SOLD_RE.search(text) or SOLD_RE_PREFIX.search(text)
    if m:
        sold = parse_num(m.group(1), m.group(2) or "")
    price = PRICE_RE.search(text)
    rating = RATING_RE.search(text)
    return {"title": title[:200], "sold": sold,
            "price": price.group(0) if price else "",
            "rating": rating.group(1) if rating else ""}


def search(page, url: str, scrolls: int = 4):
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(5000)
    for _ in range(scrolls):
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(1500)
    return page.evaluate(EXTRACT_JS)


def run(only_markets=None):
    brands = yaml.safe_load((ROOT / "config/brands.yml").read_text())["brands"]
    markets = yaml.safe_load((ROOT / "config/markets.yml").read_text())["markets"]
    if only_markets:
        markets = [m for m in markets if m["id"] in only_markets]
    today = dt.datetime.utcnow().strftime("%Y-%m-%d")
    rows, summary = [], {"date": today, "markets": {}}

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        for mk in markets:
            mdir = DEBUG / mk["id"]
            mdir.mkdir(parents=True, exist_ok=True)
            ctx = browser.new_context(user_agent=UA, locale=mk["locale"],
                                      timezone_id=mk["timezone"],
                                      viewport={"width": 1366, "height": 900})
            page = ctx.new_page()
            msum = summary["markets"][mk["id"]] = {"queries": []}
            for br in brands:
                for kw in br["keywords"]:
                    q = {"brand": br["id"], "keyword": kw, "url": None,
                         "cards": 0, "brand_cards": 0, "error": None}
                    msum["queries"].append(q)
                    cards = []
                    for tmpl in mk["search_urls"]:
                        url = tmpl.format(q=urllib.parse.quote(kw))
                        try:
                            cards = search(page, url)
                        except Exception as e:  # noqa: BLE001 - 계속 다음 후보로
                            q["error"] = f"{type(e).__name__}: {e}"[:300]
                            cards = []
                        q["url"], q["final_url"] = url, page.url
                        if cards:
                            break
                    slug = re.sub(r"\W+", "_", f"{br['id']}_{kw}")
                    try:
                        page.screenshot(path=str(mdir / f"{slug}.jpg"), type="jpeg", quality=40)
                        (mdir / f"{slug}.txt").write_text(
                            page.title() + "\n" + page.inner_text("body")[:5000])
                    except Exception:  # noqa: BLE001
                        pass
                    match = [s.lower() for s in br["match"]]
                    for rank, c in enumerate(cards, 1):
                        info = parse_card(c["text"])
                        is_brand = any(s in c["text"].lower() for s in match)
                        q["cards"] += 1
                        q["brand_cards"] += is_brand
                        rows.append({"date": today, "market": mk["id"], "brand": br["id"],
                                     "keyword": kw, "rank": rank, "product_id": c["id"],
                                     "is_brand": int(is_brand), "url": c["url"], **info})
                    print(f"[{mk['id']}] {kw!r}: {q['cards']} cards, "
                          f"{q['brand_cards']} brand, err={q['error']}", flush=True)
            ctx.close()
        browser.close()

    DATA.mkdir(exist_ok=True)
    # 같은 날 재실행 시 해당 날짜·국가 행은 교체
    done = {(r["date"], r["market"]) for r in rows}
    old = []
    if SNAPSHOTS.exists():
        with SNAPSHOTS.open() as f:
            old = [r for r in csv.DictReader(f) if (r["date"], r["market"]) not in done]
    with SNAPSHOTS.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(old + rows)
    (DEBUG / "last_run.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"saved {len(rows)} rows")


if __name__ == "__main__":
    run(sys.argv[1:] or None)

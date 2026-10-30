"""Amazon 카테고리별 Best Sellers Top 100 수집 (조사용).

카테고리는 Best Sellers 왼쪽 카테고리 트리를 이름으로 따라 내려가 찾는다
(노드 ID 하드코딩 대신). robots.txt 가 막는 경로는 열지 않고, 캡차가 뜨면 즉시 중단한다.
결과: data/amazon/<date>.csv, debug/amazon/run.json
"""
import csv
import datetime as dt
import json
import re
import sys
import urllib.robotparser
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data/amazon"
DEBUG = ROOT / "debug/amazon"
BASE = "https://www.amazon.com"

# 루트 Best Sellers 페이지 -> 트리에서 따라갈 카테고리 이름 경로
CATEGORIES = {
    "Women's Mules & Clogs": ("/gp/bestsellers/fashion", ["Women", "Shoes", "Mules & Clogs"]),
    "Men's Mules & Clogs": ("/gp/bestsellers/fashion", ["Men", "Shoes", "Mules & Clogs"]),
    "Men's Loafers & Slip-Ons": ("/gp/bestsellers/fashion", ["Men", "Shoes", "Loafers & Slip-Ons"]),
    "Women's Loafers & Slip-Ons": ("/gp/bestsellers/fashion", ["Women", "Shoes", "Loafers & Slip-Ons"]),
    "Beauty & Personal Care": ("/gp/bestsellers/beauty", []),
    "Skin Care": ("/gp/bestsellers/beauty", ["Skin Care"]),
    "Face Moisturizers": ("/gp/bestsellers/beauty", ["Skin Care", "Face", "Creams & Moisturizers", "Face Moisturizers"]),
    "Facial Serums": ("/gp/bestsellers/beauty", ["Skin Care", "Face", "Serums"]),
    "Facial Sunscreens": ("/gp/bestsellers/beauty", ["Skin Care", "Sunscreens & Tanning Products", "Sunscreens"]),
}

ITEM_JS = r"""
() => [...document.querySelectorAll('[id^="p13n-asin-index-"], div[data-asin]')].map(el => {
  const a = el.querySelector('a[href*="/dp/"]');
  const asin = el.dataset.asin || el.querySelector('[data-asin]')?.dataset.asin
               || (a && (a.href.match(/\/dp\/([A-Z0-9]{10})/) || [])[1]);
  return {asin, rank: el.querySelector('.zg-bdg-text')?.innerText || '',
          text: el.innerText, url: a ? a.href.split('?')[0].split('/ref=')[0] : ''};
}).filter(x => x.asin)
"""


def blocked(page) -> bool:
    if "/checkpoint/" in page.url or "/errors/validatecaptcha" in page.url:
        return True
    body = page.inner_text("body")[:3000].lower()
    return "enter the characters you see below" in body or "captcha" in body


def tree_link(page, name):
    # 왼쪽 카테고리 트리에서 이름이 정확히 일치하는 링크
    for a in page.query_selector_all('[role="treeitem"] a, #zg-left-col a, #zg_browseRoot a'):
        if a.inner_text().strip().lower() == name.lower():
            return a.get_attribute("href")
    return None


def parse(item):
    lines = [l.strip() for l in item["text"].splitlines() if l.strip()]
    lines = [l for l in lines if not re.fullmatch(r"#\d+", l)]
    title = max(lines, key=len) if lines else ""
    rating = re.search(r"(\d\.\d) out of 5", item["text"])
    reviews = re.search(r"\n([\d,]+)\n", "\n" + item["text"] + "\n")
    price = re.search(r"\$[\d,]+\.\d{2}", item["text"])
    return {"title": title[:250], "rating": rating.group(1) if rating else "",
            "reviews": reviews.group(1).replace(",", "") if reviews else "",
            "price": price.group(0) if price else ""}


def main():
    DEBUG.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    brands = yaml.safe_load((ROOT / "config/brands.yml").read_text())["brands"]
    log = {"categories": {}}

    rp = urllib.robotparser.RobotFileParser()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(locale="en-US")
        resp = page.request.get(BASE + "/robots.txt")
        robots = resp.text()
        (DEBUG / "robots.txt").write_text(robots)
        rp.parse(robots.splitlines())

        def allowed(path):
            return rp.can_fetch("*", BASE + path)

        today = dt.datetime.utcnow().strftime("%Y-%m-%d")
        rows = []
        for cat, (root, path) in CATEGORIES.items():
            rec = log["categories"][cat] = {"path": path}
            if not allowed(root):
                rec["error"] = f"robots.txt disallows {root}"
                print(cat, rec["error"], flush=True)
                continue
            page.goto(BASE + root, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            if blocked(page):  # 봇 확인 페이지는 우회하지 않고 중단
                rec["error"] = "captcha"
                log["stopped"] = f"captcha at {cat}"
                print("CAPTCHA - stopping", flush=True)
                break
            ok = True
            for name in path:
                href = tree_link(page, name)
                if not href:
                    rec["error"] = f"tree link not found: {name}"
                    ok = False
                    break
                url = href if href.startswith("http") else BASE + href
                if not allowed(url.replace(BASE, "")):
                    rec["error"] = f"robots.txt disallows {url}"
                    ok = False
                    break
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
            if not ok or blocked(page):
                rec.setdefault("error", "captcha")
                print(cat, rec["error"], flush=True)
                if rec["error"] == "captcha":
                    log["stopped"] = f"captcha at {cat}"
                    break
                continue
            rec["url"] = page.url.split("?")[0].split("/ref=")[0]
            items = {}
            for pg in (1, 2):  # 페이지당 50위
                if pg == 2:
                    nxt = page.query_selector('li.a-last a, a:has-text("Next page")')
                    if not nxt:
                        break
                    nxt.click()
                    page.wait_for_load_state("domcontentloaded")
                    page.wait_for_timeout(3000)
                for _ in range(6):  # 지연 로딩되는 하위 순위 로드
                    page.mouse.wheel(0, 3000)
                    page.wait_for_timeout(800)
                for it in page.evaluate(ITEM_JS):
                    items.setdefault(it["asin"], it)
            page.screenshot(path=str(DEBUG / (re.sub(r"\W+", "_", cat) + ".jpg")), type="jpeg", quality=40)
            for i, it in enumerate(items.values(), 1):
                info = parse(it)
                low = it["text"].lower()
                hit = next((b["id"] for b in brands if any(m in low for m in b["match"])), "")
                rank = int(it["rank"].lstrip("#")) if it["rank"].lstrip("#").isdigit() else i
                rows.append({"date": today, "category": cat, "rank": rank, "asin": it["asin"],
                             "tracked_brand": hit, **info, "url": it["url"]})
            rec["items"] = len(items)
            print(cat, len(items), "items", flush=True)
        browser.close()

    with (OUT / f"{today}.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "category", "rank", "asin", "tracked_brand",
                                          "title", "rating", "reviews", "price", "url"])
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["category"], r["rank"])))
    (DEBUG / "run.json").write_text(json.dumps(log, ensure_ascii=False, indent=1))
    print(f"saved {len(rows)} rows")
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())

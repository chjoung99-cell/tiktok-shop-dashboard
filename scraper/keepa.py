"""Keepa API로 Amazon 카테고리별 Best Sellers Top N을 수집한다.

필요: 환경변수 KEEPA_API_KEY (GitHub 저장소 Secrets 에 등록)
설정: config/amazon.yml
결과:
  data/keepa/ranks.csv        date, category, rank, asin  (매일 누적)
  data/keepa/products.json    asin -> 상품명·브랜드 (처음 보는 ASIN만 조회해 캐시)
  data/keepa/tracked.csv      추적 브랜드 상품의 일별 가격·월 판매량 추정치
  data/keepa/categories.json  term 으로 찾은 카테고리 노드 ID 캐시

토큰 비용(대략): bestsellers 50/카테고리, product 1/ASIN.
토큰이 부족하면 Keepa가 알려주는 refill 시간만큼 기다린 뒤 재시도한다.
"""
import csv
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data/keepa"
API = "https://api.keepa.com"
KEY = os.environ.get("KEEPA_API_KEY", "")
MAX_WAIT = 50 * 60  # 토큰 대기 총 한도(초)
_waited = 0


def call(endpoint, **params):
    global _waited
    while True:
        r = requests.get(f"{API}/{endpoint}", params={"key": KEY, **params}, timeout=60)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 429 or (data.get("error") and data.get("tokensLeft", 1) <= 0):
            wait = max(data.get("refillIn", 60000) / 1000, 5) + 1
            if _waited + wait > MAX_WAIT:
                raise RuntimeError("Keepa 토큰 대기 한도 초과 - 플랜 토큰이 부족합니다")
            print(f"  tokens exhausted, waiting {wait:.0f}s", flush=True)
            time.sleep(wait)
            _waited += wait
            continue
        if r.status_code != 200 or data.get("error"):
            raise RuntimeError(f"Keepa {endpoint} {r.status_code}: {data.get('error') or r.text[:200]}")
        print(f"  {endpoint}: tokensLeft={data.get('tokensLeft')}", flush=True)
        return data


def load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def resolve_node(cat, cache):
    """term/root/parent 로 카테고리 노드 ID 찾기 (결과는 캐시)."""
    if cat.get("node"):
        return int(cat["node"])
    if cat["id"] in cache:
        return cache[cat["id"]]
    found = call("search", domain=cat["domain"], type="category", term=cat["term"])
    cands = [c for c in (found.get("categories") or {}).values()
             if c.get("name", "").lower() == cat["term"].lower()]
    for c in cands:
        info = call("category", domain=cat["domain"], category=c["catId"], parents=1)
        parents = info.get("categoryParents") or {}
        names, pid = [], c.get("parent")
        while pid and str(pid) in parents:
            names.append(parents[str(pid)]["name"])
            pid = parents[str(pid)].get("parent")
        if cat.get("root") and cat["root"] not in names:
            continue
        if cat.get("parent") and cat["parent"] not in names:
            continue
        cache[cat["id"]] = c["catId"]
        print(f"  resolved {cat['id']} -> {c['catId']} ({' > '.join(reversed(names))} > {c['name']})")
        return c["catId"]
    raise RuntimeError(f"카테고리를 찾지 못함: {cat['id']} (후보 {len(cands)}개) - config 에 node 를 직접 지정하세요")


def main():
    if not KEY:
        sys.exit("KEEPA_API_KEY 가 설정되지 않았습니다")
    cfg = yaml.safe_load((ROOT / "config/amazon.yml").read_text())
    brands = yaml.safe_load((ROOT / "config/brands.yml").read_text())["brands"]
    OUT.mkdir(parents=True, exist_ok=True)
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    top_n = cfg.get("top_n", 100)

    cat_cache = load_json(OUT / "categories.json", {})
    products = load_json(OUT / "products.json", {})
    ranks, errors = [], {}

    for cat in cfg["categories"]:
        print(cat["id"], flush=True)
        try:
            node = resolve_node(cat, cat_cache)
            res = call("bestsellers", domain=cat["domain"], category=node, range=0)
            asins = ((res.get("bestSellersList") or {}).get("asinList") or [])[:top_n]
        except Exception as e:  # noqa: BLE001 - 다른 카테고리는 계속
            errors[cat["id"]] = str(e)
            print("  ERROR", e, flush=True)
            continue
        ranks += [{"date": today, "category": cat["id"], "rank": i, "asin": a,
                   "domain": cat["domain"]} for i, a in enumerate(asins, 1)]

    # 처음 보는 ASIN 상품명·브랜드 조회 (도메인별, 100개씩)
    def fetch(domain, asins, stats=False):
        got = {}
        for i in range(0, len(asins), 100):
            params = {"domain": domain, "asin": ",".join(asins[i:i + 100])}
            if stats:
                params["stats"] = 1
            for p in call("product", **params).get("products") or []:
                got[p["asin"]] = p
        return got

    by_domain = {}
    for r in ranks:
        if f"{r['domain']}:{r['asin']}" not in products:
            by_domain.setdefault(r["domain"], set()).add(r["asin"])
    for domain, asins in by_domain.items():
        for asin, p in fetch(domain, sorted(asins)).items():
            products[f"{domain}:{asin}"] = {"title": (p.get("title") or "")[:250],
                                            "brand": p.get("brand") or ""}

    def brand_of(meta):
        text = f"{meta.get('brand', '')} {meta.get('title', '')}".lower()
        return next((b["id"] for b in brands if any(m in text for m in b["match"])), "")

    # 추적 브랜드 상품은 매일 가격·월 판매량 추정치까지 조회
    tracked_rows = []
    tracked = {}
    for r in ranks:
        meta = products.get(f"{r['domain']}:{r['asin']}", {})
        if brand_of(meta):
            tracked.setdefault(r["domain"], set()).add(r["asin"])
    for domain, asins in tracked.items():
        for asin, p in fetch(domain, sorted(asins), stats=True).items():
            cur = (p.get("stats") or {}).get("current") or []
            price = next((v for v in (cur[1:2] + cur[0:1]) if v and v > 0), None)  # 신품가 > 아마존가
            tracked_rows.append({
                "date": today, "domain": domain, "asin": asin,
                "brand": brand_of(products.get(f"{domain}:{asin}", {})),
                "price": round(price / 100, 2) if price else "",
                "sales_rank": cur[3] if len(cur) > 3 and cur[3] and cur[3] > 0 else "",
                "monthly_sold": p.get("monthlySold") or "",
            })

    def write(name, rows, fields):
        path = OUT / name
        old = []
        if path.exists():
            old = [r for r in csv.DictReader(path.open()) if r["date"] != today]
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(old + rows)

    if ranks:
        write("ranks.csv", ranks, ["date", "domain", "category", "rank", "asin"])
    if tracked_rows:
        write("tracked.csv", tracked_rows,
              ["date", "domain", "asin", "brand", "price", "sales_rank", "monthly_sold"])
    (OUT / "products.json").write_text(json.dumps(products, ensure_ascii=False, indent=0))
    (OUT / "categories.json").write_text(json.dumps(cat_cache, indent=1))
    (OUT / "last_run.json").write_text(json.dumps(
        {"date": today, "rows": len(ranks), "tracked": len(tracked_rows), "errors": errors},
        ensure_ascii=False, indent=1))
    print(f"saved {len(ranks)} ranks, {len(tracked_rows)} tracked, errors={list(errors)}")
    return 0 if ranks else 1


if __name__ == "__main__":
    sys.exit(main())

"""data/snapshots.csv 를 대시보드용 docs/data.json 으로 집계한다.

날짜 x 국가 x 브랜드 단위 지표:
  sold          브랜드 상품 누적 판매수 합계 (상품ID 기준 중복 제거)
  products      검색에 잡힌 브랜드 상품 수
  best_rank     브랜드 상품 중 가장 높은 검색 순위
  top10_share   검색 상위 10개 중 브랜드 상품 비율 (%)
누적 판매수의 날짜 간 차이가 해당 기간 판매량 추정치가 된다.
"""
import csv
import json
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def amazon(brands):
    """Keepa 수집분: 카테고리별 최신 Top N 목록 + 추적 브랜드의 일별 순위 추이."""
    kdir = ROOT / "data/keepa"
    path = kdir / "ranks.csv"
    if not path.exists():
        return None
    cfg = yaml.safe_load((ROOT / "config/amazon.yml").read_text())
    meta = json.loads((kdir / "products.json").read_text()) if (kdir / "products.json").exists() else {}
    tracked = defaultdict(dict)
    if (kdir / "tracked.csv").exists():
        for r in csv.DictReader((kdir / "tracked.csv").open()):
            tracked[r["date"]][f"{r['domain']}:{r['asin']}"] = r

    def brand_of(m):
        text = f"{m.get('brand', '')} {m.get('title', '')}".lower()
        return next((b["id"] for b in brands if any(x in text for x in b["match"])), "")

    rows = list(csv.DictReader(path.open()))
    latest = max(r["date"] for r in rows)
    top, history = [], []
    for r in rows:
        key = f"{r['domain']}:{r['asin']}"
        m = meta.get(key, {})
        b = brand_of(m)
        if b:
            history.append({"date": r["date"], "category": r["category"], "brand": b,
                            "asin": r["asin"], "rank": int(r["rank"])})
        if r["date"] == latest:
            t = tracked[latest].get(key, {})
            top.append({"category": r["category"], "rank": int(r["rank"]), "asin": r["asin"],
                        "domain": r["domain"], "title": m.get("title", ""),
                        "brand_name": m.get("brand", ""), "brand": b,
                        "price": t.get("price", ""), "monthly_sold": t.get("monthly_sold", "")})
    return {"updated": latest,
            "categories": [{"id": c["id"], "name": c["name"]} for c in cfg["categories"]],
            "top": top, "history": history}


def main():
    brands = yaml.safe_load((ROOT / "config/brands.yml").read_text())["brands"]
    markets = yaml.safe_load((ROOT / "config/markets.yml").read_text())["markets"]
    path = ROOT / "data/snapshots.csv"
    rows = list(csv.DictReader(path.open())) if path.exists() else []

    groups = defaultdict(list)
    for r in rows:
        groups[(r["date"], r["market"], r["brand"])].append(r)

    series, latest_date = [], max((r["date"] for r in rows), default=None)
    products = []
    for (date, market, brand), rs in sorted(groups.items()):
        own = {}
        for r in rs:
            if r["is_brand"] == "1":
                prev = own.get(r["product_id"])
                if not prev or int(r["sold"] or 0) > int(prev["sold"] or 0):
                    own[r["product_id"]] = r
        top10 = [r for r in rs if int(r["rank"]) <= 10]
        series.append({
            "date": date, "market": market, "brand": brand,
            "sold": sum(int(r["sold"] or 0) for r in own.values()),
            "products": len(own),
            "best_rank": min((int(r["rank"]) for r in own.values()), default=None),
            "top10_share": round(100 * sum(r["is_brand"] == "1" for r in top10) / len(top10))
            if top10 else None,
        })
        if date == latest_date:
            for r in sorted(own.values(), key=lambda r: -int(r["sold"] or 0))[:10]:
                products.append({k: r[k] for k in
                                 ("market", "brand", "rank", "title", "price", "sold",
                                  "rating", "url")})

    out = {
        "updated": latest_date,
        "brands": [{k: b[k] for k in ("id", "name", "company", "category")} for b in brands],
        "markets": [{"id": m["id"], "name": m["name"]} for m in markets],
        "series": series,
        "products": products,
        "amazon": amazon(brands),
    }
    (ROOT / "docs/data.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"{len(series)} series points, {len(products)} products, latest={latest_date}")


if __name__ == "__main__":
    main()

# tiktok-shop-dashboard
투자분석 모니터링 용도 — 틱톡샵 검색 결과에서 브랜드별 검색 순위와 누적 판매수를 매일 수집해 대시보드로 보여준다.

## 구조
| 경로 | 역할 |
|---|---|
| `config/brands.yml` | 모니터링 브랜드 (추가는 여기에 항목만 붙이면 됨) |
| `config/markets.yml` | 수집 국가와 검색 URL |
| `scraper/collect.py` | Playwright로 검색 결과 수집 → `data/snapshots.csv` |
| `scraper/build.py` | 집계 → `docs/data.json` |
| `docs/index.html` | 대시보드 (GitHub Pages) |
| `debug/` | 국가별 스크린샷·페이지 텍스트·실행 요약 (수집 실패 원인 확인용) |
| `.github/workflows/collect.yml` | 매일 10:23 KST 자동 실행 |

## 지표
- **누적 판매**: 검색에 잡힌 브랜드 상품의 “sold” 표시값 합계. 날짜 간 증가분 = 기간 판매량 추정치
- **최고 검색순위**: 브랜드 검색어 결과에서 브랜드 상품의 최상위 순위
- **상위10 내 브랜드 비중**: 검색 상위 10개 중 브랜드 상품 비율 (경쟁·짝퉁 점유 확인)

## 브랜드 추가
`config/brands.yml` 에 항목을 추가하고 main 에 푸시하면 즉시 수집이 한 번 돈다.

## 로컬 실행
```bash
pip install -r requirements.txt && python -m playwright install chromium
python scraper/collect.py us     # 특정 국가만
python scraper/build.py
python -m http.server -d docs
```

## Amazon Best Sellers (Keepa)
- 설정: `config/amazon.yml` (카테고리 추가 시 `term`/`root` 만 적으면 노드 ID 자동 탐색)
- 수집: `scraper/keepa.py` → `data/keepa/` (매일 09:41 KST, `.github/workflows/keepa.yml`)
- 필요: 저장소 Settings → Secrets and variables → Actions → `KEEPA_API_KEY`
- 토큰: 카테고리당 약 50 + 처음 보는 상품 1개당 1. 9개 카테고리 기준 첫날 약 1,300, 이후 하루 약 500~600

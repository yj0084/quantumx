import os
import sys

try:
    sys.stdout.reconfigure(line_buffering=True)  # 로그 즉시 출력
except Exception:
    pass

import json
import requests
import pandas as pd
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file

# matplotlib은 차트 파일 생성에만 쓰인다. 없어도 API는 동작해야 하므로 선택적으로 불러온다.
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.font_manager as fm
    HAS_MPL = True
except Exception:  # pragma: no cover
    plt = None
    fm = None
    HAS_MPL = False

# ==========================
# ⚙️ 기본 설정
# ==========================
APP_KEY = os.getenv("APP_KEY", "")
APP_SECRET = os.getenv("APP_SECRET", "")
TOKEN_FILE = "kis_token.json"

# 모의투자(vts) 도메인 기본. 실전으로 옮길 때는 KIS_BASE_URL 환경변수만 바꾸면 된다.
KIS_BASE = os.getenv("KIS_BASE_URL", "https://openapivts.koreainvestment.com:29443")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

app = Flask(__name__)

# 차트 한글 폰트 (파일이 있을 때만 등록)
if HAS_MPL:
    for _font in ("NanumGothic.ttf",):
        if os.path.exists(_font):
            try:
                fm.fontManager.addfont(_font)
                plt.rc("font", family=fm.FontProperties(fname=_font).get_name())
                plt.rcParams["axes.unicode_minus"] = False
            except Exception as e:
                print("⚠️ 한글 폰트 등록 실패:", e)


# ==========================
# 💾 KIS 토큰 (만료 확인 + 자동 재발급)
# ==========================
_token_cache = {"token": None, "expires_at": None}
TOKEN_MARGIN_SEC = 300  # 만료 5분 전에 미리 갱신


def _token_fresh(expires_at):
    if not expires_at:
        return False
    return datetime.now() < expires_at - timedelta(seconds=TOKEN_MARGIN_SEC)


def load_saved_token():
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r") as f:
                data = json.load(f)
            exp = data.get("expires_at")
            expires_at = datetime.fromisoformat(exp) if exp else None
            if expires_at and _token_fresh(expires_at):
                _token_cache["token"] = data.get("access_token")
                _token_cache["expires_at"] = expires_at
                print("♻️ 저장된 토큰 재사용")
                return _token_cache["token"]
        except Exception as e:
            print("⚠️ 저장된 토큰 읽기 실패:", e)
    return None


def save_token(token, expires_in):
    expires_at = datetime.now() + timedelta(seconds=int(expires_in or 86400))
    _token_cache["token"] = token
    _token_cache["expires_at"] = expires_at
    try:
        with open(TOKEN_FILE, "w") as f:
            json.dump({"access_token": token, "expires_at": expires_at.isoformat()}, f)
    except Exception as e:
        print("⚠️ 토큰 파일 저장 실패:", e)


def get_access_token(force=False):
    """캐시 → 저장된 토큰 → 신규 발급 순. force=True면 무조건 재발급."""
    if not force:
        if _token_fresh(_token_cache.get("expires_at")):
            return _token_cache["token"]
        saved = load_saved_token()
        if saved:
            return saved

    if not APP_KEY or not APP_SECRET:
        print("❌ APP_KEY / APP_SECRET 환경변수가 없습니다.")
        return None

    url = f"{KIS_BASE}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    body = {"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET}
    try:
        res = requests.post(url, headers=headers, json=body, timeout=15)
        data = res.json()
        if data.get("access_token"):
            print("✅ 토큰 발급 성공")
            save_token(data["access_token"], data.get("expires_in", 86400))
            return data["access_token"]
        # 키/시크릿은 절대 출력하지 않고 응답 코드·메시지만 남긴다
        print("❌ 토큰 발급 실패:", data.get("msg_cd"), data.get("error_description") or data.get("msg1"))
        return None
    except Exception as e:
        print("⚠️ 토큰 발급 예외:", type(e).__name__)
        return None


def kis_headers(tr_id, token):
    return {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id,
        "custtype": "P",
    }


def kis_get(path, tr_id, params):
    """KIS 조회 호출. 401/토큰만료 응답이면 토큰을 재발급해 1회 재시도한다."""
    data = {"error": "KIS 접근토큰을 발급하지 못했습니다. APP_KEY/APP_SECRET을 확인하세요.", "rt_cd": "-1"}
    for attempt in (1, 2):
        token = get_access_token(force=(attempt == 2))
        if not token:
            return data
        try:
            res = requests.get(f"{KIS_BASE}{path}", headers=kis_headers(tr_id, token),
                               params=params, timeout=15)
        except Exception as e:
            return {"error": f"KIS 연결 실패: {type(e).__name__}", "rt_cd": "-1"}

        try:
            data = res.json()
        except ValueError:
            return {"error": f"KIS 응답 해석 실패 (HTTP {res.status_code})", "rt_cd": "-1"}

        expired = res.status_code == 401 or data.get("msg_cd") in ("EGW00123", "EGW00121", "EGW00133")
        if expired and attempt == 1:
            print("🔄 토큰 만료 감지 → 재발급 후 재시도")
            continue
        data["_http_status"] = res.status_code
        return data
    return data


# ==========================
# 🔢 종목 코드 변환
# ==========================
KOR_MAP = {
    # 2026-11-18 네이버 종목 기본정보 API로 코드↔이름을 대조 확인한 목록.
    # (기존 코드의 "한화엔진": "011210"은 실제로 현대위아라 082740으로 바로잡음)
    "삼성전자": "005930", "SK하이닉스": "000660", "한화엔진": "082740",
    "포스코DX": "022100", "POSCO홀딩스": "005490", "카카오": "035720",
    "NAVER": "035420", "LG에너지솔루션": "373220", "LG화학": "051910",
    "셀트리온": "068270", "삼성바이오로직스": "207940", "현대차": "005380",
    "기아": "000270", "한화오션": "042660", "두산에너빌리티": "034020",
    "이지홀딩스": "035810", "KB금융": "105560", "하나금융지주": "086790",
    "우리금융지주": "316140",
}


# 네이버 모바일 종목 자동완성(인증 불필요). KOR_MAP에 없는 종목명을 코드로 바꿀 때 쓴다.
NAVER_AUTOCOMPLETE = "https://m.stock.naver.com/front-api/search/autoComplete"
_CODE_CACHE = {}
_CODE_CACHE_TTL = 600


def naver_search(query, limit=10):
    """종목명·티커 검색 → [{'code','name','typeName','nationCode'}]. 실패하면 빈 목록."""
    q = str(query or "").strip()
    if not q:
        return []
    try:
        res = requests.get(NAVER_AUTOCOMPLETE, params={"query": q, "target": "stock"},
                           headers={"User-Agent": UA, "Referer": "https://m.stock.naver.com/"},
                           timeout=10)
        if res.status_code != 200:
            print(f"⚠️ 종목 검색 HTTP {res.status_code}")
            return []
        items = ((res.json().get("result") or {}).get("items")) or []
        out = []
        for it in items:
            code = str(it.get("code") or "").strip()
            name = str(it.get("name") or "").strip()
            if not code or not name:
                continue
            out.append({"code": code, "name": name,
                        "typeName": str(it.get("typeName") or "").strip(),
                        "nationCode": str(it.get("nationCode") or "").strip()})
            if len(out) >= limit:
                break
        return out
    except Exception as e:
        print("⚠️ 종목 검색 오류:", type(e).__name__)
        return []


def lookup_code_by_name(raw):
    """KOR_MAP에 없는 이름을 통합 검색(네이버+Yahoo)으로 조회(10분 캐시). 이름/코드 완전일치 우선."""
    hit = _CODE_CACHE.get(raw)
    if hit and (datetime.now() - hit["at"]).total_seconds() < _CODE_CACHE_TTL:
        return hit["code"]
    items = search_stocks(raw, limit=10)
    if not items:
        return None
    low = raw.lower()
    exact = [i for i in items if i["name"].lower() == low or i["code"].lower() == low]
    code = (exact or items)[0]["code"]
    _CODE_CACHE[raw] = {"at": datetime.now(), "code": code}
    return code


# Yahoo Finance 종목 검색(키 불필요). 영문 회사명·네이버에 없는 해외 종목 보완용.
YAHOO_SEARCH = "https://query1.finance.yahoo.com/v1/finance/search"


def yahoo_search(query, limit=8):
    """Yahoo 검색 → [{'code','name','typeName','nationCode'}]. 주식·ETF만."""
    q = str(query or "").strip()
    if not q:
        return []
    try:
        res = requests.get(YAHOO_SEARCH, params={"q": q, "quotesCount": limit, "newsCount": 0},
                           headers={"User-Agent": UA}, timeout=10)
        if res.status_code != 200:
            print(f"⚠️ Yahoo 검색 HTTP {res.status_code}")
            return []
        out = []
        for item in (res.json().get("quotes") or []):
            if item.get("quoteType") not in ("EQUITY", "ETF"):
                continue
            sym = str(item.get("symbol") or "").strip()
            if not sym:
                continue
            out.append({"code": sym,
                        "name": str(item.get("shortname") or item.get("longname") or sym).strip(),
                        "typeName": str(item.get("exchDisp") or item.get("exchange") or "").strip(),
                        "nationCode": "USA"})
            if len(out) >= limit:
                break
        return out
    except Exception as e:
        print("⚠️ Yahoo 검색 오류:", type(e).__name__)
        return []


def search_stocks(query, limit=10):
    """국내+해외 통합 검색: 네이버 자동완성 우선, 필요하면 Yahoo 검색을 붙여 중복 없이 합친다."""
    q = str(query or "").strip()
    if not q:
        return []
    items = naver_search(q, limit=limit)
    # 영문 질의만 Yahoo를 더 본다. 한글 질의는 네이버가 해외 종목 한글명까지 처리하므로
    # 엉뚱한 해외 티커가 섞이지 않게 Yahoo를 붙이지 않는다.
    if q.isascii():
        seen = {i["code"].upper() for i in items}
        for it in yahoo_search(q, limit=8):
            if it["code"].upper() in seen:
                continue
            items.append(it)
            seen.add(it["code"].upper())
            if len(items) >= limit:
                break
    return items[:limit]


def get_stock_code_global(name):
    """국내 종목명 → 6자리 코드, 미국 티커 → 티커 그대로. 모르면 네이버 검색으로 보완, 그래도 없으면 None."""
    if not name:
        return None
    raw = str(name).strip()
    if raw in KOR_MAP:
        return KOR_MAP[raw]
    upper = raw.upper()
    # 국내 코드: 6자리 숫자 또는 신형 영숫자 코드(예: 0183J0)
    if len(upper) == 6 and upper.isalnum() and not upper.isalpha():
        return upper
    if upper.isascii() and upper.isalpha() and 1 <= len(upper) <= 5:
        # 대문자 티커(TSLA, AAPL, NAVER)는 그대로 티커로 본다.
        # "Apple" 같은 회사명 표기는 검색을 먼저 시도하고, 실패하면 티커로 되돌린다.
        if raw.isupper():
            return upper
        return lookup_code_by_name(raw) or upper
    return lookup_code_by_name(raw)


def is_korea_code(code):
    """6자리면 국내 코드(신형 영숫자 코드 포함). 미국 티커는 1~5자."""
    if not code:
        return False
    c = str(code)
    return len(c) == 6 and c.isalnum() and not c.isalpha()


# ==========================
# 🇰🇷 한국 주식 데이터 (네이버 모바일 시세 API)
# ==========================
def _num(value):
    """'189,800' 같은 콤마 문자열을 float로."""
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def add_moving_averages(df):
    df = df.sort_values("date").reset_index(drop=True)
    df["MA5"] = df["close"].rolling(window=5).mean()
    df["MA20"] = df["close"].rolling(window=20).mean()
    df["MA60"] = df["close"].rolling(window=60).mean()
    return df


def get_korea_stock_data(code, need=120):
    """폐지된 finance.naver.com/sise_day 대신 m.stock.naver.com JSON API 사용."""
    url = f"https://m.stock.naver.com/api/stock/{code}/price"
    headers = {"User-Agent": UA, "Referer": "https://m.stock.naver.com/"}
    rows, page = [], 1
    try:
        while len(rows) < need and page <= 5:
            res = requests.get(url, params={"pageSize": 40, "page": page},
                               headers=headers, timeout=15)
            if res.status_code != 200:
                raise RuntimeError(f"HTTP {res.status_code}")
            chunk = res.json()
            if not isinstance(chunk, list) or not chunk:
                break
            rows.extend(chunk)
            if len(chunk) < 40:
                break
            page += 1

        if not rows:
            raise RuntimeError("응답에 일별 시세가 없습니다")

        df = pd.DataFrame(rows)
        missing = [c for c in ("localTradedAt", "closePrice", "accumulatedTradingVolume")
                   if c not in df.columns]
        if missing:
            raise RuntimeError(f"예상 필드 누락: {missing}")

        df = df.rename(columns={"localTradedAt": "date",
                                "closePrice": "close",
                                "accumulatedTradingVolume": "volume"})
        df["close"] = df["close"].map(_num)
        df["volume"] = df["volume"].map(_num)
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df[["date", "close", "volume"]].dropna(subset=["date", "close"])
        df = df[df["close"] > 0]
        df = df.drop_duplicates(subset=["date"])
        if df.empty:
            raise RuntimeError("유효한 종가가 없습니다")
        df["volume"] = df["volume"].fillna(0.0)
        return add_moving_averages(df)
    except Exception as e:
        print(f"⚠️ 한국 주식 데이터 오류({code}):", e)
        return pd.DataFrame()


# ==========================
# 🇺🇸 미국 주식 데이터 (Yahoo Finance chart)
# ==========================
def get_us_stock_data(ticker):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        # 60일선·60일 VWAP까지 계산하려면 6개월치(약 125거래일)가 필요하다.
        params = {"range": "6mo", "interval": "1d"}
        res = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=15)
        if res.status_code != 200:
            raise RuntimeError(f"HTTP {res.status_code}")
        payload = res.json()
        result = ((payload.get("chart") or {}).get("result") or [None])[0]
        if not result:
            raise RuntimeError("야후 응답에 chart.result가 없습니다")

        stamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []

        rows = []
        for i in range(min(len(stamps), len(closes))):
            close = _num(closes[i])
            if close is None or close <= 0:
                continue
            vol = _num(volumes[i]) if i < len(volumes) else None
            rows.append({"date": datetime.fromtimestamp(stamps[i]),
                         "close": close,
                         "volume": vol if vol is not None else 0.0})
        if not rows:
            raise RuntimeError("유효한 종가가 없습니다")

        return add_moving_averages(pd.DataFrame(rows))
    except Exception as e:
        print(f"⚠️ 미국 주식 데이터 오류({ticker}):", e)
        return pd.DataFrame()


# 5분 캐시: /report 한 번에 여러 축을 계산하므로 같은 요청 안에서 재호출을 줄인다.
_PRICE_CACHE = {}
_PRICE_CACHE_TTL = 300


def get_stock_data(stock_name, use_cache=True):
    """종목명 → (code, df). 인식 실패면 (None, 빈 df)."""
    code = get_stock_code_global(stock_name)
    if not code:
        return None, pd.DataFrame()

    if use_cache:
        hit = _PRICE_CACHE.get(code)
        if hit and (datetime.now() - hit["at"]).total_seconds() < _PRICE_CACHE_TTL:
            return code, hit["df"].copy()

    df = get_korea_stock_data(code) if is_korea_code(code) else get_us_stock_data(code)
    if df is not None and not df.empty:
        _PRICE_CACHE[code] = {"at": datetime.now(), "df": df.copy()}
    return code, df


# ==========================
# 🔥 상승 확률 · ✅ 매집 점수 · 📈 신호
# ==========================
def predict_rise_probability(df):
    if df is None or df.empty or len(df) < 20:
        return 0
    try:
        ma5, ma20 = df["MA5"].iloc[-1], df["MA20"].iloc[-1]
        if pd.isna(ma5) or pd.isna(ma20) or not ma20:
            return 0
        ma_trend = (ma5 - ma20) / ma20
        vol_mean = df["volume"].mean()
        vol_trend = (df["volume"].iloc[-1] / vol_mean) if vol_mean else 1.0
        prob = 50 + (ma_trend * 200) + ((vol_trend - 1) * 10)
        return round(max(min(prob, 95), 5), 2)
    except Exception as e:
        print("⚠️ 상승확률 계산 오류:", e)
        return 0


def calculate_accumulation_score(df):
    if df is None or df.empty or len(df) < 5:
        return 0
    try:
        closes = df["close"]
        base = closes.iloc[-5]
        if not base:
            return 0
        price_change = (closes.iloc[-1] - base) / base * 100
        volume_mean = df["volume"].tail(5).mean()
        ma_gap = 0.0
        if len(df) >= 20:
            ma5, ma20 = df["MA5"].iloc[-1], df["MA20"].iloc[-1]
            if not pd.isna(ma5) and not pd.isna(ma20) and ma20:
                ma_gap = (ma5 - ma20) / ma20 * 100
        score = 50 + (price_change * 0.5) + (ma_gap * 0.8)
        if volume_mean and volume_mean > df["volume"].mean():
            score += 10
        return round(max(min(score, 100), 0), 2)
    except Exception as e:
        print("⚠️ 매집 점수 계산 오류:", e)
        return 0


def get_signal(df):
    """MA5/MA20 교차 신호. 데이터가 모자라면 예외 대신 '관망'을 돌려준다."""
    if df is None or df.empty:
        return "⚪ 데이터 없음"
    if len(df) < 20:
        return "⚪ 관망 (데이터 부족)"
    try:
        ma5, ma20 = df["MA5"].iloc[-1], df["MA20"].iloc[-1]
    except KeyError:
        return "⚪ 관망 (데이터 부족)"
    if pd.isna(ma5) or pd.isna(ma20):
        return "⚪ 관망 (데이터 부족)"
    if ma5 > ma20:
        return "🟢 매수 신호"
    if ma5 < ma20:
        return "🔴 매도 신호"
    return "⚪ 관망"


def summarize(df):
    """종가·이동평균 등 실제 수치를 JSON에 담기 위한 요약."""
    if df is None or df.empty:
        return {}
    ma5, ma20 = df["MA5"].iloc[-1], df["MA20"].iloc[-1]
    ma60 = df["MA60"].iloc[-1] if "MA60" in df.columns else None
    out = {
        "기준일": df["date"].iloc[-1].strftime("%Y-%m-%d"),
        "종가": round(float(df["close"].iloc[-1]), 2),
        "5일선": None if pd.isna(ma5) else round(float(ma5), 2),
        "20일선": None if pd.isna(ma20) else round(float(ma20), 2),
        "데이터일수": int(len(df)),
    }
    out["60일선"] = None if ma60 is None or pd.isna(ma60) else round(float(ma60), 2)
    return out


# ==========================
# 📉 기관/외국인 수급 데이터 (KIS 주식현재가 투자자)
# ==========================
def get_investor_trend(code):
    # 종목별 일별 투자자매매동향(FHPTJ04160001)이 아니라
    # 주식현재가 투자자 API이며, TR_ID는 FHKST01010900 이다.
    try:
        return kis_get(
            "/uapi/domestic-stock/v1/quotations/inquire-investor",
            "FHKST01010900",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
        )
    except Exception as e:
        print("⚠️ 수급 데이터 오류:", e)
        return {"error": str(e), "rt_cd": "-1"}


def _fmt_date(yyyymmdd):
    if not yyyymmdd or len(str(yyyymmdd)) != 8:
        return None
    s = str(yyyymmdd)
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


def parse_investor_rows(res):
    """KIS inquire-investor 응답 → 날짜 오름차순 정리된 일별 수급 리스트.

    필드: stck_bsop_date(영업일자), frgn_ntby_qty(외국인 순매수 수량),
          orgn_ntby_qty(기관계 순매수 수량), prsn_ntby_qty(개인 순매수 수량),
          stck_clpr(종가), acml_vol(거래량)
    """
    rows = res.get("output") or []
    by_date = {}
    for r in rows:
        d = _fmt_date(r.get("stck_bsop_date"))
        if not d:
            continue
        by_date[d] = {
            "date": d,
            "foreign_net_qty": _num(r.get("frgn_ntby_qty")) or 0.0,
            "institution_net_qty": _num(r.get("orgn_ntby_qty")) or 0.0,
            "individual_net_qty": _num(r.get("prsn_ntby_qty")) or 0.0,
            "close": _num(r.get("stck_clpr")),
            "volume": (_num(r.get("frgn_shnu_vol")) or 0) + (_num(r.get("frgn_seln_vol")) or 0),
        }
    return [by_date[d] for d in sorted(by_date)]


def investor_summary(code):
    """최근 5·20거래일 외국인/기관 누적 순매수와 해석용 비율을 만든다."""
    res = get_investor_trend(code)
    summary = {
        "지원": True,
        "상태": "ok",
        "출처": "한국투자증권 inquire-investor (FHKST01010900)",
        "일별": [],
        "5일": {},
        "20일": {},
    }
    if str(res.get("rt_cd")) != "0":
        summary["상태"] = "error"
        summary["오류"] = res.get("msg1") or res.get("error") or "KIS 응답 오류"
        summary["오류코드"] = res.get("msg_cd") or res.get("rt_cd")
        return summary, res

    rows = parse_investor_rows(res)
    summary["일별"] = rows[-20:]
    if not rows:
        summary["상태"] = "empty"
        return summary, res

    def agg(n):
        part = rows[-n:]
        vol = sum([r["volume"] or 0.0 for r in part])
        out = {
            "거래일수": len(part),
            "외국인_순매수_주": int(sum(r["foreign_net_qty"] for r in part)),
            "기관_순매수_주": int(sum(r["institution_net_qty"] for r in part)),
            "개인_순매수_주": int(sum(r["individual_net_qty"] for r in part)),
            "거래량_합": int(vol),
            "외국인_순매수_비중_pct": None,
            "기관_순매수_비중_pct": None,
            "기관외국인_합산_비중_pct": None,
        }
        if vol:
            out["외국인_순매수_비중_pct"] = round(sum(r["foreign_net_qty"] for r in part) / vol * 100, 2)
            out["기관_순매수_비중_pct"] = round(sum(r["institution_net_qty"] for r in part) / vol * 100, 2)
            out["기관외국인_합산_비중_pct"] = round(
                sum(r["foreign_net_qty"] + r["institution_net_qty"] for r in part) / vol * 100, 2)
        return out

    summary["5일"] = agg(5)
    summary["20일"] = agg(20)
    return summary, res


# ==========================
# ⭐ 추천 종목 (상위 점수 TOP3)
# ==========================
RECOMMEND_CANDIDATES = ["005930", "000270", "035720", "373220", "068270", "042660"]


def recommend_stocks():
    results = []
    for code in RECOMMEND_CANDIDATES:
        df = get_korea_stock_data(code)
        results.append({
            "code": code,
            "score": calculate_accumulation_score(df),
            "rise_prob": predict_rise_probability(df),
            "signal": get_signal(df),
            "data_points": int(len(df)),
        })
    results.sort(key=lambda x: (x["score"], x["rise_prob"]), reverse=True)
    return results[:3]


# ==========================
# 📊 리포트(차트) 생성
# ==========================
def generate_report(stock_name, df, score, prob, signal):
    if not HAS_MPL or df is None or df.empty:
        return None
    try:
        fig, ax = plt.subplots(figsize=(10, 6))
        ax.plot(df["date"], df["close"], label="종가", color="blue", linewidth=2)
        ax.plot(df["date"], df["MA5"], label="5일선", linestyle="--")
        ax.plot(df["date"], df["MA20"], label="20일선", linestyle="--")
        ax.legend()
        ax.grid(alpha=0.3)
        ax.set_title(f"{stock_name} 주가 추세 및 신호", fontsize=14, weight="bold")
        chart_path = f"{stock_name}_chart.png"
        plt.savefig(chart_path, dpi=200)
        plt.close(fig)
        return chart_path
    except Exception as e:
        print("⚠️ 리포트 생성 오류:", e)
        return None


# ==========================
# 🧮 2단계: 투명한 공식 기반 종합 리포트
# ==========================
GRADE_TABLE = [
    (75, "STRONG_BUY", "적극 매수"),
    (62, "ADD", "분할 매수"),
    (45, "HOLD", "관망/보유"),
    (32, "REDUCE", "비중 축소"),
    (0, "SELL", "매도"),
]

WEIGHTS = {"매집": 25, "수급": 25, "모멘텀": 20, "RSI": 15, "이평선": 15}

FORMULA_NOTES = {
    "매집비": ("최근 20거래일 중 종가가 전일 종가보다 오른 날의 거래량 합 ÷ 같은 20거래일 전체 거래량 합 × 100. "
            "50% 초과면 상승일에 거래량이 더 실린다는 뜻이라 매집 우위, 50% 미만이면 하락일 거래량이 우위(분산)로 해석한다."),
    "추정평단": ("VWAP20 = Σ(종가×거래량) ÷ Σ(거래량) (최근 20거래일). 거래량 가중평균단가로 "
             "'최근 20거래일 시장 참여자의 평균 매입단가' 추정치다. 비교용으로 60거래일 VWAP도 함께 낸다."),
    "괴리율": ("(현재가 - 추정평단) ÷ 추정평단 × 100. 음수면 현재가가 추정 평단보다 낮아 평균 매수자가 손실 구간, "
            "양수면 평균 매수자가 이익 구간이다."),
    "RSI14": ("Wilder 방식. 14거래일 평균상승폭(AU)과 평균하락폭(AD)을 지수평활로 갱신하고 "
              "RS=AU/AD, RSI=100-100/(1+RS). 30 이하 과매도, 70 이상 과매수."),
    "ROC": ("(현재가 ÷ N거래일 전 종가 - 1) × 100. 5일=단기 모멘텀, 20일=중기 모멘텀."),
    "거래량모멘텀": "최근 5일 평균 거래량 ÷ 최근 20일 평균 거래량. 1 초과면 최근 거래가 평소보다 활발하다.",
    "수급": ("한국투자증권 inquire-investor(FHKST01010900)의 일별 외국인·기관 순매수 수량을 5·20거래일 누적한 값. "
            "비중(%) = 누적 순매수 수량 ÷ 같은 기간 누적 거래량 × 100. 미국 종목은 이 API가 없어 미제공."),
    "이평선": ("MA5/MA20/MA60 = 최근 N거래일 종가 단순평균. 현재가가 이평선 위면 지지, 아래면 이탈로 본다. "
             "MA5>MA20>MA60이면 정배열(상승 추세), 반대면 역배열."),
    "축별점수_매집": "매집점수 = 50 + (매집비-50)×0.8 - (괴리율×1.2), 괴리율은 ±15%로 제한 후 0~100 클리핑.",
    "축별점수_RSI": ("구간표: ≤30 → 72점(과매도·반등 후보), 30~40 → 62점, 40~50 → 56점, 50~60 → 58점, "
                   "60~70 → 48점(과열 진입), ≥70 → 30점(과매수·추격 금지)."),
    "축별점수_모멘텀": "모멘텀점수 = 50 + ROC5×2.0 + ROC20×1.0 + (거래량모멘텀-1)×15, 0~100 클리핑.",
    "축별점수_수급": ("수급점수 = 50 + (외국인+기관 20거래일 순매수 비중%)×5, 0~100 클리핑. "
                  "비중이 +10%면 100점, -10%면 0점."),
    "축별점수_이평선": ("이평선점수 = 50 + [현재가>MA5 ±10] + [현재가>MA20 ±12] + [현재가>MA60 ±8] "
                   "+ [MA5>MA20 ±10] + [MA20>MA60 ±10], 0~100 클리핑. 60일선 데이터가 없으면 해당 항목을 뺀다."),
    "종합점수": ("축별 0~100점의 가중평균. 매집 25% · 수급 25% · 모멘텀 20% · RSI 15% · 이평선 15%. "
             "수급을 못 받는 미국 종목은 남은 75% 가중치를 비례 재분배한다."),
    "등급컷": "종합점수 75 이상 STRONG_BUY / 62 이상 ADD / 45 이상 HOLD / 32 이상 REDUCE / 32 미만 SELL.",
    "지지저항": ("지지선 = 현재가 아래에 있는 값들(MA5·MA20·MA60·VWAP20·최근 20일 종가 최저) 중 가까운 순, "
             "저항선 = 현재가 위에 있는 값들(같은 목록 + 최근 20일 종가 최고) 중 가까운 순."),
    "한계": ("모든 값은 과거 시세·수급의 기술적 계산이며 미래 예측이나 투자자문이 아니다. "
           "이 API는 조회·분석 전용으로 주문·매매 기능이 없다."),
}


def _clip(v, lo, hi):
    return max(lo, min(hi, v))


def _r(v, nd=2):
    return None if v is None else round(float(v), nd)


def calc_vwap(df, window):
    tail = df.tail(window)
    if tail.empty:
        return None
    vol = tail["volume"].sum()
    if not vol or pd.isna(vol):
        return None
    return float((tail["close"] * tail["volume"]).sum() / vol)


def calc_accumulation_ratio(df, window=20):
    """상승일 거래량 합 ÷ 전체 거래량 합 × 100 (최근 window거래일)."""
    tail = df.tail(window + 1).reset_index(drop=True)
    if len(tail) < 3:
        return None
    up_vol, total = 0.0, 0.0
    for i in range(1, len(tail)):
        v = float(tail["volume"].iloc[i] or 0.0)
        total += v
        if tail["close"].iloc[i] > tail["close"].iloc[i - 1]:
            up_vol += v
    if not total:
        return None
    return round(up_vol / total * 100, 2)


def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def calc_roc(closes, n):
    if len(closes) <= n:
        return None
    base = closes[-1 - n]
    if not base:
        return None
    return round((closes[-1] / base - 1) * 100, 2)


def rsi_zone(rsi):
    if rsi is None:
        return "데이터 부족"
    if rsi <= 30:
        return "과매도"
    if rsi < 40:
        return "약세"
    if rsi < 50:
        return "중립(약세)"
    if rsi < 60:
        return "중립(강세)"
    if rsi < 70:
        return "강세(과열 진입)"
    return "과매수"


def rsi_score(rsi):
    if rsi is None:
        return None
    if rsi <= 30:
        return 72.0
    if rsi < 40:
        return 62.0
    if rsi < 50:
        return 56.0
    if rsi < 60:
        return 58.0
    if rsi < 70:
        return 48.0
    return 30.0


def grade_of(score):
    for cut, code, label in GRADE_TABLE:
        if score >= cut:
            return code, label
    return "SELL", "매도"


def _fmt_price(v, currency):
    if v is None:
        return "-"
    if currency == "KRW":
        return f"{v:,.0f}원"
    return f"${v:,.2f}"


def _fmt_qty(v):
    return f"{v:+,}주"


def build_report(stock_name):
    code = get_stock_code_global(stock_name)
    if not code:
        return {"error": "❌ 종목 코드 인식 실패", "종목명": stock_name,
                "힌트": "국내 종목명(삼성전자 등) 또는 6자리 코드, 미국 티커(TSLA 등)"}, 400

    kr = is_korea_code(code)
    currency = "KRW" if kr else "USD"
    df = get_stock_data(code)[1]

    if df is None or df.empty:
        return {"error": "📉 시세 데이터를 가져오지 못했습니다", "종목명": stock_name, "종목코드": code,
                "원인": "국내=네이버 모바일 시세 API / 미국=야후 차트 API 응답 없음",
                "힌트": "네트워크 상태를 확인하고 잠시 후 다시 시도하세요."}, 503
    if len(df) < 30:
        return {"error": "📉 거래일 데이터가 부족합니다", "종목명": stock_name, "종목코드": code,
                "원인": f"확보된 거래일이 {len(df)}일뿐이라 RSI·이평선 계산이 부정확합니다."}, 503

    closes = [float(x) for x in df["close"].tolist()]
    vols = [float(x) if not pd.isna(x) else 0.0 for x in df["volume"].tolist()]
    price = closes[-1]
    base_date = df["date"].iloc[-1].strftime("%Y-%m-%d")

    ma5 = None if pd.isna(df["MA5"].iloc[-1]) else float(df["MA5"].iloc[-1])
    ma20 = None if pd.isna(df["MA20"].iloc[-1]) else float(df["MA20"].iloc[-1])
    ma60 = None if ("MA60" not in df.columns or pd.isna(df["MA60"].iloc[-1])) else float(df["MA60"].iloc[-1])

    vwap20 = calc_vwap(df, 20)
    vwap60 = calc_vwap(df, 60)
    est_avg = vwap20 if vwap20 is not None else vwap60
    gap_pct = None if not est_avg else round((price - est_avg) / est_avg * 100, 2)

    acc_ratio = calc_accumulation_ratio(df, 20)
    rsi14 = calc_rsi(closes, 14)
    roc5 = calc_roc(closes, 5)
    roc20 = calc_roc(closes, 20)
    vol5 = sum(vols[-5:]) / min(5, len(vols)) if vols else 0.0
    vol20 = sum(vols[-20:]) / min(20, len(vols)) if vols else 0.0
    vol_mom = round(vol5 / vol20, 2) if vol20 else None

    recent = df.tail(20)
    low20 = float(recent["close"].min())
    high20 = float(recent["close"].max())

    # ---------- 축 1: 매집비/추정평단 ----------
    acc_points = {}
    acc_score = None
    if acc_ratio is not None and gap_pct is not None:
        gap_clipped = _clip(gap_pct, -15, 15)
        acc_score = _clip(50 + (acc_ratio - 50) * 0.8 - gap_clipped * 1.2, 0, 100)
        acc_points = {"기준 50": 50, f"(매집비-50)×0.8 = ({acc_ratio}-50)×0.8": round((acc_ratio - 50) * 0.8, 2),
                      f"-(괴리율)×1.2 = -({gap_clipped})×1.2": round(-gap_clipped * 1.2, 2)}
    if acc_ratio is None:
        acc_verdict = "계산 불가(거래량 없음)"
        acc_comment = "거래량 데이터가 없어 매집비를 계산하지 못했습니다."
    elif acc_ratio >= 60:
        acc_verdict = "강한 매집"
        acc_comment = (f"최근 20거래일 거래량의 {acc_ratio}%가 상승일에 실렸습니다. 매수세가 거래량을 주도하는 구간입니다. "
                       f"현재가는 추정평단 {_fmt_price(est_avg, currency)} 대비 {gap_pct:+.2f}%입니다.")
    elif acc_ratio >= 52:
        acc_verdict = "매집 우위"
        acc_comment = (f"상승일 거래량 비중 {acc_ratio}%로 매집 우위입니다. 현재가는 추정평단 "
                       f"{_fmt_price(est_avg, currency)} 대비 {gap_pct:+.2f}%.")
    elif acc_ratio >= 45:
        acc_verdict = "중립"
        acc_comment = (f"상승일·하락일 거래량이 비슷합니다(매집비 {acc_ratio}%). 현재가는 추정평단 "
                       f"{_fmt_price(est_avg, currency)} 대비 {gap_pct:+.2f}%.")
    else:
        acc_verdict = "분산 우위"
        acc_comment = (f"하락일 거래량이 더 많습니다(매집비 {acc_ratio}%). 매수 주체가 약한 구간으로 봅니다. "
                       f"현재가는 추정평단 {_fmt_price(est_avg, currency)} 대비 {gap_pct:+.2f}%.")

    # ---------- 축 2: RSI ----------
    rsi_s = rsi_score(rsi14)
    rsi_verdict = rsi_zone(rsi14)
    rsi_comment = {
        "과매도": "RSI 30 이하 과매도 구간. 낙폭 과대 반등 후보이나 추세 확인이 필요합니다.",
        "약세": "RSI 30~40 구간으로 약세지만 과매도는 아닙니다.",
        "중립(약세)": "RSI 40~50. 중립 하단으로 힘은 약하지만 과매도는 아닙니다.",
        "중립(강세)": "RSI 50~60. 중립 상단으로 매수·매도 어느 쪽도 우세하지 않습니다.",
        "강세(과열 진입)": "RSI 60~70. 강세지만 과열 진입 구간이라 추격 매수는 주의합니다.",
        "과매수": "RSI 70 이상 과매수 구간. 단기 조정 위험이 큽니다.",
        "데이터 부족": "RSI를 계산할 데이터가 부족합니다.",
    }.get(rsi_verdict, "")

    # ---------- 축 3: 모멘텀 ----------
    mom_score = None
    if roc5 is not None and roc20 is not None:
        vm = vol_mom if vol_mom is not None else 1.0
        mom_score = _clip(50 + roc5 * 2.0 + roc20 * 1.0 + (vm - 1) * 15, 0, 100)
    if mom_score is None:
        mom_verdict = "계산 불가"
        mom_comment = "가격 이력이 부족해 모멘텀을 계산하지 못했습니다."
    else:
        if mom_score >= 70:
            mom_verdict = "강한 상승 모멘텀"
        elif mom_score >= 58:
            mom_verdict = "상승 모멘텀"
        elif mom_score >= 45:
            mom_verdict = "모멘텀 중립"
        elif mom_score >= 32:
            mom_verdict = "하락 모멘텀"
        else:
            mom_verdict = "강한 하락 모멘텀"
        mom_comment = (f"5일 {roc5:+.2f}%, 20일 {roc20:+.2f}%, 거래량 모멘텀 {vol_mom if vol_mom is not None else '-'}"
                       f"(최근 5일 평균 거래량 ÷ 20일 평균).")

    # ---------- 축 4: 외국인·기관 수급 ----------
    if kr:
        inv_summary, _raw = investor_summary(code)
        supply_available = inv_summary.get("상태") == "ok"
        if supply_available:
            ratio = inv_summary["20일"].get("기관외국인_합산_비중_pct")
            supply_score = _clip(50 + (ratio or 0) * 5, 0, 100) if ratio is not None else None
            f20 = inv_summary["20일"]["외국인_순매수_주"]
            i20 = inv_summary["20일"]["기관_순매수_주"]
            f5 = inv_summary["5일"]["외국인_순매수_주"]
            i5 = inv_summary["5일"]["기관_순매수_주"]
            if supply_score is None:
                supply_verdict = "비중 계산 불가"
            elif supply_score >= 65:
                supply_verdict = "수급 유입"
            elif supply_score >= 55:
                supply_verdict = "수급 소폭 유입"
            elif supply_score >= 45:
                supply_verdict = "수급 중립"
            elif supply_score >= 35:
                supply_verdict = "수급 소폭 유출"
            else:
                supply_verdict = "수급 유출"
            supply_comment = (f"20거래일 누적 외국인 {f20:+,}주 · 기관 {i20:+,}주 "
                              f"(합산 거래량 대비 {ratio if ratio is not None else '-'}%). "
                              f"최근 5거래일은 외국인 {f5:+,}주 · 기관 {i5:+,}주.")
            supply_numbers = {
                "외국인_5일_순매수_주": f5, "기관_5일_순매수_주": i5,
                "외국인_20일_순매수_주": f20, "기관_20일_순매수_주": i20,
                "기관외국인_20일_순매수_비중_pct": ratio,
                "단위": "주",
            }
        else:
            supply_score = None
            supply_verdict = "수급 조회 실패"
            supply_comment = f"KIS 수급 조회가 실패해 이 항목은 종합점수에서 제외했습니다({inv_summary.get('오류')})."
            supply_numbers = {"오류": inv_summary.get("오류"), "오류코드": inv_summary.get("오류코드")}
    else:
        supply_available = False
        supply_score = None
        supply_verdict = "미국 종목 미지원"
        supply_comment = "외국인·기관 수급 API는 국내 종목만 제공합니다. 이 축은 종합점수 가중치에서 제외했습니다."
        supply_numbers = {"미지원": True}

    # ---------- 축 5: 이평선 ----------
    ma_points = []
    if ma5 is not None:
        ma_points.append(("현재가>MA5", 10, price > ma5))
    if ma20 is not None:
        ma_points.append(("현재가>MA20", 12, price > ma20))
    if ma60 is not None:
        ma_points.append(("현재가>MA60", 8, price > ma60))
    if ma5 is not None and ma20 is not None:
        ma_points.append(("MA5>MA20", 10, ma5 > ma20))
    if ma20 is not None and ma60 is not None:
        ma_points.append(("MA20>MA60", 10, ma20 > ma60))

    if ma_points:
        ma_score = _clip(50 + sum(w if ok else -w for _, w, ok in ma_points), 0, 100)
    else:
        ma_score = None

    if ma5 is not None and ma20 is not None and ma60 is not None:
        if ma5 > ma20 > ma60:
            ma_order = "정배열"
        elif ma5 < ma20 < ma60:
            ma_order = "역배열"
        else:
            ma_order = "혼조"
    else:
        ma_order = "데이터 부족"

    above = [n for n, v in (("5일선", ma5), ("20일선", ma20), ("60일선", ma60)) if v is not None and price > v]
    below = [n for n, v in (("5일선", ma5), ("20일선", ma20), ("60일선", ma60)) if v is not None and price <= v]
    ma_verdict = ma_order
    ma_comment = (f"현재가 {_fmt_price(price, currency)} 기준으로 " +
                  (f"{', '.join(above)} 위(지지)" if above else "") +
                  (" / " if above and below else "") +
                  (f"{', '.join(below)} 아래(이탈)입니다. " if below else "입니다. ") +
                  (f"60일선 {_fmt_price(ma60, currency)}." if ma60 is not None else "60일선을 계산할 이력이 부족합니다."))

    # ---------- 종합점수 ----------
    axis_scores = {"매집": acc_score, "수급": supply_score, "모멘텀": mom_score, "RSI": rsi_s, "이평선": ma_score}
    used = {k: v for k, v in axis_scores.items() if v is not None}
    if not used:
        total_score = 0.0
        grade, grade_label = "HOLD", "관망/보유"
    else:
        wsum = sum(WEIGHTS[k] for k in used)
        total_score = round(sum(used[k] * WEIGHTS[k] for k in used) / wsum, 2)
        grade, grade_label = grade_of(total_score)

    # ---------- 지지선/저항선 ----------
    level_pool = [("5일선", ma5), ("20일선", ma20), ("60일선", ma60),
                  ("20일 VWAP", vwap20), ("최근 20일 종가 최저", low20), ("최근 20일 종가 최고", high20)]
    levels = [(n, float(v)) for n, v in level_pool if v is not None and v > 0]
    supports = sorted([{"구분": n, "가격": round(v, 2), "현재가_대비_pct": round((v - price) / price * 100, 2)}
                       for n, v in levels if v < price * 0.999], key=lambda x: -x["가격"])[:3]
    resistances = sorted([{"구분": n, "가격": round(v, 2), "현재가_대비_pct": round((v - price) / price * 100, 2)}
                          for n, v in levels if v > price * 1.001], key=lambda x: x["가격"])[:3]

    # ---------- 축별 결과 정리 ----------
    def axis_block(score, verdict, comment, numbers=None, weight=None):
        return {"점수": None if score is None else round(score, 1),
                "판정": verdict, "해석": comment,
                "가중치_pct": weight,
                "수치": numbers or {}}

    axes = {
        "1_매집비_추정평단": axis_block(acc_score, acc_verdict, acc_comment,
                                 {"매집비_pct": acc_ratio, "추정평단": _r(est_avg, 2),
                                  "추정평단_괴리율_pct": gap_pct, "VWAP20": _r(vwap20, 2),
                                  "VWAP60": _r(vwap60, 2), "통화": currency}, WEIGHTS["매집"]),
        "2_RSI": axis_block(rsi_s, rsi_verdict, rsi_comment,
                            {"RSI14": rsi14, "기간": 14}, WEIGHTS["RSI"]),
        "3_모멘텀": axis_block(mom_score, mom_verdict, mom_comment,
                            {"ROC5_pct": roc5, "ROC20_pct": roc20, "거래량모멘텀": vol_mom,
                             "최근5일_평균거래량": round(vol5, 0), "최근20일_평균거래량": round(vol20, 0)},
                            WEIGHTS["모멘텀"]),
        "4_수급": axis_block(supply_score, supply_verdict, supply_comment, supply_numbers, WEIGHTS["수급"]),
        "5_이평선": axis_block(ma_score, ma_verdict, ma_comment,
                            {"MA5": _r(ma5, 2), "MA20": _r(ma20, 2), "MA60": _r(ma60, 2),
                             "배열": ma_order, "지지_이평선": above, "이탈_이평선": below,
                             "계산": {n: (w if ok else -w) for n, w, ok in ma_points}}, WEIGHTS["이평선"]),
    }

    # ---------- 한 줄 요약 · 사람용 리포트 ----------
    cur = _fmt_price(price, currency)
    one_line = (f"{grade}({grade_label}) — {ma_order}, RSI {rsi14 if rsi14 is not None else '-'}, "
                f"추정평단 {_fmt_price(est_avg, currency)} 대비 {gap_pct:+.2f}%"
                if gap_pct is not None
                else f"{grade}({grade_label}) — {ma_order}, RSI {rsi14 if rsi14 is not None else '-'}")

    lines = []
    lines.append(f"[{stock_name}({code}) 종합 매수·매도 리포트]")
    lines.append(f"기준일 {base_date} · 현재가 {cur} · 데이터 {len(df)}거래일")
    lines.append(f"종합등급 {grade}({grade_label}) · 종합점수 {total_score}/100")
    lines.append("")
    lines.append(f"1) 매집비·추정평단 — {axes['1_매집비_추정평단']['판정']} (축점수 "
                 f"{axes['1_매집비_추정평단']['점수']})")
    lines.append(f"   · 매집비(20일) {acc_ratio if acc_ratio is not None else '-'}% / 추정평단(VWAP20) "
                 f"{_fmt_price(est_avg, currency)} / 괴리율 {gap_pct:+.2f}%" if gap_pct is not None else
                 f"   · 매집비(20일) {acc_ratio if acc_ratio is not None else '-'}%")
    lines.append(f"   · {acc_comment}")
    lines.append(f"2) RSI(14) — {rsi14 if rsi14 is not None else '-'} · {rsi_verdict} (축점수 "
                 f"{axes['2_RSI']['점수']})")
    lines.append(f"   · {rsi_comment}")
    lines.append(f"3) 모멘텀 — {mom_verdict} (축점수 {axes['3_모멘텀']['점수']})")
    lines.append(f"   · {mom_comment}")
    lines.append(f"4) 외국인·기관 수급 — {supply_verdict} (축점수 {axes['4_수급']['점수']})")
    lines.append(f"   · {supply_comment}")
    lines.append(f"5) 이동평균선 — {ma_verdict} (축점수 {axes['5_이평선']['점수']})")
    lines.append(f"   · {ma_comment}")
    lines.append("")
    if supports:
        lines.append("지지선: " + " / ".join(
            f"{s['구분']} {_fmt_price(s['가격'], currency)}({s['현재가_대비_pct']:+.1f}%)" for s in supports))
    else:
        lines.append("지지선: 현재가 아래에 뚜렷한 이평선·VWAP 지지가 없습니다.")
    if resistances:
        lines.append("저항선: " + " / ".join(
            f"{r['구분']} {_fmt_price(r['가격'], currency)}({r['현재가_대비_pct']:+.1f}%)" for r in resistances))
    else:
        lines.append("저항선: 현재가 위에 뚜렷한 이평선·VWAP 저항이 없습니다.")
    lines.append("")
    lines.append(f"종합해석: {total_score}/100 → {grade}({grade_label}). "
                 f"가중치 매집 25 · 수급 25 · 모멘텀 20 · RSI 15 · 이평선 15"
                 + ("(수급 미제공분 제외 후 재분배)" if supply_score is None else "") + ".")
    lines.append("※ 과거 시세·수급 기반 기술적 계산이며 투자자문이 아닙니다. 조회·분석 전용(주문 기능 없음).")
    summary_report = "\n".join(lines)

    payload = {
        "종목명": stock_name,
        "종목코드": code,
        "시장": "KR" if kr else "US",
        "통화": currency,
        "기준일": base_date,
        "현재가": round(price, 2),
        "데이터일수": int(len(df)),
        "종합등급": grade,
        "종합등급_한글": grade_label,
        "종합점수": total_score,
        "한줄요약": one_line,
        "축별점수": {k: {"점수": v["점수"], "판정": v["판정"], "가중치_pct": v["가중치_pct"]}
                  for k, v in axes.items()},
        "지표": {
            "매집비_pct": acc_ratio,
            "추정평단": _r(est_avg, 2),
            "추정평단_괴리율_pct": gap_pct,
            "VWAP20": _r(vwap20, 2),
            "VWAP60": _r(vwap60, 2),
            "RSI14": rsi14,
            "RSI_구간": rsi_verdict,
            "ROC5_pct": roc5,
            "ROC20_pct": roc20,
            "거래량모멘텀": vol_mom,
            "MA5": _r(ma5, 2),
            "MA20": _r(ma20, 2),
            "MA60": _r(ma60, 2),
            "이평선배열": ma_order,
            "MA5_지지여부": None if ma5 is None else ("지지" if price > ma5 else "이탈"),
            "MA20_지지여부": None if ma20 is None else ("지지" if price > ma20 else "이탈"),
            "MA60_지지여부": None if ma60 is None else ("지지" if price > ma60 else "이탈"),
            "최근20일_종가최저": round(low20, 2),
            "최근20일_종가최고": round(high20, 2),
        },
        "수급": {
            "지원": bool(kr and supply_available),
            "상태": (inv_summary.get("상태") if kr else "not_supported"),
            "5일": (inv_summary.get("5일") if kr and supply_available else None),
            "20일": (inv_summary.get("20일") if kr and supply_available else None),
            "일별_최근20": (inv_summary.get("일별") if kr and supply_available else None),
            "비고": supply_comment,
        },
        "축별해석": axes,
        "지지선저항선": {"지지선": supports, "저항선": resistances},
        "formula_notes": FORMULA_NOTES,
        "summary_report": summary_report,
        "주의": "조회·분석 전용입니다. 주문·매매 API는 포함되어 있지 않습니다.",
    }
    return payload, 200


# ==========================
# 🚀 Flask API 엔드포인트 (조회 전용 — 주문 기능 없음)
# ==========================
@app.route("/api", methods=["GET"])
def api_index_endpoint():
    return jsonify({
        "name": "QuantumX PRO API",
        "mode": "조회 전용 (주문/매매 기능 없음)",
        "endpoints": {
            "POST /report": "종합 매수·매도 리포트: 매집비·추정평단·RSI·모멘텀·수급·이평선 + 종합등급",
            "GET /report/{stock_name}": "같은 리포트를 GET으로 조회 (예: /report/삼성전자)",
            "POST /quantumx": "종목 분석: 매집점수·상승확률·매매신호",
            "GET /recommend": "추천 종목 TOP3",
            "GET /search?q=": "종목명·티커 검색(국내+해외, 최대 10건) → code·name·typeName",
            "POST /investor": "KIS 기관/외국인 수급",
        },
    })


@app.route("/", methods=["GET"])
def dashboard_endpoint():
    """조회 전용 웹 대시보드. dashboard.html이 없으면 API 안내 JSON으로 대체한다."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
    if os.path.exists(path):
        resp = send_file(path, mimetype="text/html")
        resp.headers["Cache-Control"] = "no-cache"
        return resp
    return api_index_endpoint()


@app.route("/search", methods=["GET"])
def search_endpoint():
    """종목명·티커 검색(네이버 자동완성 + Yahoo Finance fallback). 조회 전용."""
    q = str(request.args.get("q") or request.args.get("query") or "").strip()
    if not q:
        return jsonify({"query": "", "count": 0, "items": []})
    items = search_stocks(q, limit=10)
    return jsonify({"query": q, "count": len(items), "items": items})


@app.route("/quantumx", methods=["POST"])
def quantumx_endpoint():
    payload = request.get_json(silent=True) or {}
    stock = str(payload.get("stock_name") or "삼성전자").strip()
    code, df = get_stock_data(stock)

    if not code:
        return jsonify({"error": "❌ 종목 코드 인식 실패", "종목명": stock,
                        "힌트": "국내 종목명(삼성전자 등) 또는 6자리 코드, 미국 티커(TSLA 등)"}), 400

    if df is None or df.empty:
        return jsonify({"error": "📉 시세 데이터를 가져오지 못했습니다", "종목명": stock,
                        "종목코드": code,
                        "원인": "국내=네이버 모바일 시세 API / 미국=야후 차트 API 응답 없음",
                        "힌트": "네트워크 상태를 확인하고 잠시 후 다시 시도하세요."}), 503

    if len(df) < 20:
        return jsonify({"error": "📉 거래일 데이터가 부족합니다", "종목명": stock,
                        "종목코드": code,
                        "원인": f"확보된 거래일이 {len(df)}일뿐이라 20일선을 계산할 수 없습니다."}), 503

    result = {
        "종목명": stock,
        "종목코드": code,
        "매집점수": calculate_accumulation_score(df),
        "상승확률": predict_rise_probability(df),
        "매매신호": get_signal(df),
    }
    result.update(summarize(df))
    return jsonify(result)


@app.route("/report", methods=["POST"])
def report_endpoint():
    payload = request.get_json(silent=True) or {}
    stock = str(payload.get("stock_name") or payload.get("stock") or payload.get("ticker")
                or payload.get("symbol") or "삼성전자").strip()
    body, status = build_report(stock)
    return jsonify(body), status


@app.route("/report/<path:stock_name>", methods=["GET"])
def report_get_endpoint(stock_name):
    body, status = build_report(str(stock_name).strip())
    return jsonify(body), status


@app.route("/recommend", methods=["GET"])
def recommend_endpoint():
    return jsonify(recommend_stocks())


@app.route("/investor", methods=["POST"])
def investor_endpoint():
    payload = request.get_json(silent=True) or {}
    stock = str(payload.get("stock_name") or "삼성전자").strip()
    code = get_stock_code_global(stock)

    if not code:
        return jsonify({"error": "❌ 종목 코드 인식 실패", "종목명": stock}), 400
    if not is_korea_code(code):
        return jsonify({"error": "수급 데이터는 국내 종목만 제공합니다", "종목명": stock,
                        "종목코드": code}), 400

    res = get_investor_trend(code)
    status = 200 if str(res.get("rt_cd")) == "0" else 502
    return jsonify(res), status


if __name__ == "__main__":
    print("🚀 QuantumX PRO 서버 실행 중 ... (Render 24/7)")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
